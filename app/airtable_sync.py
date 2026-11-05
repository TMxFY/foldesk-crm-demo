from __future__ import annotations

import json
import os
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


def _default_urlopen(req: urllib.request.Request | str, timeout: float = 30) -> Any:
    cafile: str | None = None
    try:
        import certifi
        cafile = certifi.where()
    except Exception:
        if os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)

from app.db import (
    AIRTABLE_PIPELINE_STAGES,
    KEY_TO_AIRTABLE_STATUS,
    ROOT,
    as_dict,
    ensure_airtable_pipeline_stages,
    list_pipeline_stages,
    normalize_status,
    status_to_airtable,
    utc_now,
)
from app.events import record_lead_event
from app.leads import (
    _clean_amount,
    _clean_username,
    _parse_offer_type,
    _serialize_offer_type,
    create_lead,
    update_lead,
)

AIRTABLE_API_BASE = "https://api.airtable.com/v0"
DEFAULT_TABLE_NAME = "Leads"
TASKS_TABLE_NAME = "Tasks"
PROJECTS_TABLE_NAME = "Projects"
DEFAULT_REQUESTS_PER_SECOND = 5.0
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_FACTOR = 2.0

ALLOWED_LEAD_SOURCES = (
    "Website",
    "Referral",
    "Event",
    "Social Media",
    "Email Campaign",
    "Other",
    "Cold outreach",
)
_LEAD_SOURCE_BY_LOWER = {s.lower(): s for s in ALLOWED_LEAD_SOURCES}

COMPUTED_READONLY_FIELDS = frozenset(
    {
        "Days Since Last Contacted",
        "Lead Summary (AI)",
        "Lead Qualification Score (AI)",
        "Number of Associated Tasks",
        "Number of Associated Projects",
        "Created Date",
        "Total Revenue",
        "Total Expenses",
        "Net Profit",
        "Margin",
    }
)


def _read_dotenv_values() -> dict[str, str]:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            k = key.strip()
            v = val.strip().strip('"').strip("'")
            if k:
                out[k] = v
    except Exception:
        return {}
    return out


def _resolve_credentials(
    api_key: str | None = None,
    base_id: str | None = None,
    table_name: str | None = None,
) -> tuple[str | None, str | None, str]:
    dotenv_vals = _read_dotenv_values()
    resolved_key = (
        api_key
        or os.environ.get("AIRTABLE_API_KEY")
        or os.environ.get("AIRTABLE_TOKEN")
        or dotenv_vals.get("AIRTABLE_API_KEY")
        or dotenv_vals.get("AIRTABLE_TOKEN")
        or ""
    ).strip() or None
    resolved_base = (
        base_id
        or os.environ.get("AIRTABLE_BASE_ID")
        or dotenv_vals.get("AIRTABLE_BASE_ID")
        or ""
    ).strip() or None
    resolved_table = (
        table_name
        or os.environ.get("AIRTABLE_TABLE_NAME")
        or dotenv_vals.get("AIRTABLE_TABLE_NAME")
        or DEFAULT_TABLE_NAME
    ).strip() or DEFAULT_TABLE_NAME
    return resolved_key, resolved_base, resolved_table


def get_airtable_config(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    api_key, base_id, table_name = _resolve_credentials()
    last_sync_at: str | None = None
    last_sync_summary: dict[str, Any] | None = None
    stages: list[dict[str, Any]] = [
        {"key": k, "label": lbl, "position": pos, "color": col}
        for k, lbl, pos, col in AIRTABLE_PIPELINE_STAGES
    ]

    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value, updated_at FROM kv_settings WHERE key = 'airtable_last_sync'"
            ).fetchone()
            if row is not None:
                last_sync_at = row["updated_at"]
                raw_val = row["value"]
                if raw_val:
                    parsed = json.loads(raw_val)
                    if isinstance(parsed, dict):
                        last_sync_summary = parsed
                        if parsed.get("synced_at"):
                            last_sync_at = str(parsed["synced_at"])
        except Exception:
            pass
        try:
            db_stages = list_pipeline_stages(conn)
            if db_stages:
                stages = db_stages
        except Exception:
            pass

    return {
        "configured": bool(api_key and base_id),
        "base_id": base_id,
        "table_name": table_name,
        "last_sync_at": last_sync_at,
        "last_sync_summary": last_sync_summary,
        "stages": stages,
    }


class _SyncTokenBucket:
    def __init__(
        self,
        rate: float = DEFAULT_REQUESTS_PER_SECOND,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = max(float(rate), 0.1)
        self._capacity = float(capacity) if capacity is not None else self._rate
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = self._clock()

    def acquire(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return
        wait_seconds = (1.0 - self._tokens) / self._rate
        self._sleep(wait_seconds)
        self._tokens = 0.0
        self._updated = self._clock()


class AirtableRestClient:
    """Synchronous Airtable REST API v0 client with rate-limiting, retries, and injectable transport."""

    def __init__(
        self,
        api_key: str | None = None,
        base_id: str | None = None,
        table_name: str | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        transport: Callable[..., Any] | None = None,
        requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_key, resolved_base, resolved_table = _resolve_credentials(
            api_key=api_key, base_id=base_id, table_name=table_name
        )
        if not resolved_key or not resolved_base:
            raise ValueError("Не заданы AIRTABLE_API_KEY и AIRTABLE_BASE_ID в .env")
        self.api_key = resolved_key
        self.base_id = resolved_base
        self.table_name = resolved_table
        self._opener = transport or opener or _default_urlopen
        self._max_attempts = max(1, int(max_attempts))
        self._sleep = sleep
        self._limiter = _SyncTokenBucket(
            requests_per_second, clock=clock, sleep=sleep
        )

    def _table_url(self, table: str, record_id: str | None = None) -> str:
        encoded_table = urllib.parse.quote(str(table), safe="")
        url = f"{AIRTABLE_API_BASE}/{self.base_id}/{encoded_table}"
        if record_id:
            encoded_rec = urllib.parse.quote(str(record_id), safe="")
            url = f"{url}/{encoded_rec}"
        return url

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        body_bytes: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        attempt = 0
        while True:
            self._limiter.acquire()
            req = urllib.request.Request(
                url, data=body_bytes, headers=headers, method=method.upper()
            )
            try:
                resp = self._opener(req, timeout=30)
                if hasattr(resp, "__enter__"):
                    with resp as r:
                        raw = r.read()
                else:
                    raw = resp.read()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                attempt += 1
                status = exc.code
                retryable = status == 429 or (status is not None and status >= 500)
                if not retryable or attempt >= self._max_attempts:
                    err_body = ""
                    try:
                        err_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Ошибка Airtable API ({status} {method} {url}): {err_body or exc}"
                    ) from exc
                delay = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))
                if status == 429 and exc.headers is not None:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            delay = max(float(retry_after), 0.0)
                        except ValueError:
                            pass
                self._sleep(delay)

    def list_records(
        self,
        table: str | None = None,
        *,
        filter_formula: str | None = None,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        target_table = table or self.table_name
        base_url = self._table_url(target_table)
        records: list[dict[str, Any]] = []
        offset: str | None = None

        while True:
            params: dict[str, str] = {}
            if offset:
                params["offset"] = offset
            if filter_formula:
                params["filterByFormula"] = filter_formula
            if max_records is not None:
                params["maxRecords"] = str(int(max_records))
            url = f"{base_url}?{urllib.parse.urlencode(params)}" if params else base_url
            data = self._request_json("GET", url)
            batch = data.get("records") or []
            if isinstance(batch, list):
                records.extend(batch)
            offset = data.get("offset")
            if not offset:
                break
        return records

    get_records = list_records

    def get_record(self, table: str, record_id: str) -> dict[str, Any]:
        return self._request_json("GET", self._table_url(table, record_id))

    def create_record(
        self,
        table: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        clean_fields = {
            k: v
            for k, v in fields.items()
            if k not in COMPUTED_READONLY_FIELDS and v is not None
        }
        payload: dict[str, Any] = {
            "fields": clean_fields,
            "typecast": bool(typecast),
        }
        return self._request_json("POST", self._table_url(table), payload)

    def update_record(
        self,
        table: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        clean_fields = {
            k: v
            for k, v in fields.items()
            if k not in COMPUTED_READONLY_FIELDS and v is not None
        }
        payload: dict[str, Any] = {
            "fields": clean_fields,
            "typecast": bool(typecast),
        }
        return self._request_json("PATCH", self._table_url(table, record_id), payload)


AirtableClient = AirtableRestClient


def _ensure_dirty_tracking_trigger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_leads_bump_updated_at_after_sync
        AFTER UPDATE ON leads
        FOR EACH ROW
        WHEN NEW.airtable_synced_at IS OLD.airtable_synced_at
             AND OLD.airtable_synced_at IS NOT NULL
             AND NEW.updated_at <= OLD.airtable_synced_at
        BEGIN
            UPDATE leads
            SET updated_at = OLD.airtable_synced_at || '.1'
            WHERE id = NEW.id;
        END;
        """
    )


def _clean_date(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return s[:10] if len(s) >= 10 else s


def parse_airtable_contact(raw_contact: str | None) -> tuple[str | None, str | None]:
    """Extract (@username without '@', phone) from Airtable 'Contact' field."""
    if not raw_contact:
        return None, None
    text = str(raw_contact).strip()
    if not text:
        return None, None

    username: str | None = None
    phone: str | None = None

    # 1. Explicit @handle
    m_at = re.search(r"@([A-Za-z0-9_]{3,32})", text)
    if m_at:
        username = m_at.group(1)
    else:
        # 2. t.me/handle or telegram.me/handle
        m_tme = re.search(
            r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,32})",
            text,
            flags=re.IGNORECASE,
        )
        if m_tme:
            username = m_tme.group(1)

    # 3. Phone number
    text_without_handle = re.sub(
        r"(?:https?://)?(?:t\.me|telegram\.me)/[A-Za-z0-9_]+|@[A-Za-z0-9_]+",
        " ",
        text,
    )
    for m_phone in re.finditer(r"\+?\d[\d\s\-\(\)]{5,}\d", text_without_handle):
        candidate = m_phone.group(0).strip()
        digits = re.sub(r"\D", "", candidate)
        if 7 <= len(digits) <= 15:
            phone = candidate
            break

    # 4. Bare single-word handle (e.g. "narekgevorgyan") when no phone and no spaces/@
    if not username and not phone and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", text):
        username = text

    return _clean_username(username), phone


def format_airtable_contact(
    username: str | None,
    phone: str | None,
    existing_contact: str | None = None,
) -> str | None:
    if existing_contact and str(existing_contact).strip():
        return str(existing_contact).strip()
    clean_u = _clean_username(username)
    clean_p = (phone or "").strip() or None
    parts: list[str] = []
    if clean_u:
        parts.append(f"@{clean_u}")
    if clean_p:
        parts.append(clean_p)
    return ", ".join(parts) if parts else None


def normalize_lead_source_for_airtable(raw_source: str | None) -> str:
    if not raw_source:
        return "Cold outreach"
    cleaned = str(raw_source).strip()
    if not cleaned:
        return "Cold outreach"
    return _LEAD_SOURCE_BY_LOWER.get(cleaned.lower(), "Cold outreach")


def map_airtable_record_to_lead_fields(
    record: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    rec_id = str(record.get("id") or "").strip() or None
    fields = record.get("fields") or {}

    raw_name = (fields.get("Lead Name") or "").strip() or None
    company = (fields.get("Company") or "").strip() or None
    username, phone = parse_airtable_contact(fields.get("Contact"))

    raw_status = (fields.get("Status") or "New").strip() or "New"
    norm_status = normalize_status(raw_status, conn)
    airtable_status = KEY_TO_AIRTABLE_STATUS.get(norm_status, raw_status)

    amount = _clean_amount(fields.get("Value"))
    monthly_value = _clean_amount(fields.get("Monthly Value"))
    url = (fields.get("Website") or "").strip() or None
    niche = (fields.get("Niche") or "").strip() or None
    offer_type = _parse_offer_type(fields.get("Offer Type"))
    offer_details = (fields.get("Offer Details") or "").strip() or None
    next_step = (fields.get("Next Step") or "").strip() or None
    next_follow_up = _clean_date(fields.get("Next Follow-up"))
    last_contacted = _clean_date(fields.get("Last Contacted"))
    lead_source = (fields.get("Lead Source") or "").strip() or None
    note = (fields.get("Notes") or "").strip() or None

    name = raw_name or company or (f"@{username}" if username else None) or phone
    return {
        "airtable_record_id": rec_id,
        "name": name,
        "company": company,
        "status": norm_status,
        "airtable_status": airtable_status,
        "amount": amount,
        "monthly_value": monthly_value,
        "username": username,
        "phone": phone,
        "url": url,
        "niche": niche,
        "offer_type": offer_type,
        "offer_details": offer_details,
        "next_step": next_step,
        "next_follow_up": next_follow_up,
        "last_contacted": last_contacted,
        "lead_source": lead_source,
        "note": note,
    }


def map_lead_to_airtable_fields(lead: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    lead_name = (
        (lead.get("name") or "").strip()
        or (lead.get("company") or "").strip()
        or (f"@{lead['username']}" if lead.get("username") else "")
        or (lead.get("phone") or "").strip()
        or f"Lead #{lead.get('id') or ''}".strip()
    )
    fields["Lead Name"] = lead_name
    fields["Status"] = status_to_airtable(lead.get("status"))
    fields["Lead Source"] = normalize_lead_source_for_airtable(lead.get("lead_source"))

    if lead.get("company"):
        fields["Company"] = str(lead["company"]).strip()

    amt = int(lead.get("amount") or 0)
    if amt > 0:
        fields["Value"] = amt

    monthly = int(lead.get("monthly_value") or 0)
    if monthly > 0:
        fields["Monthly Value"] = monthly

    contact = format_airtable_contact(lead.get("username"), lead.get("phone"))
    if contact:
        fields["Contact"] = contact

    if lead.get("url"):
        fields["Website"] = str(lead["url"]).strip()

    if lead.get("niche"):
        fields["Niche"] = str(lead["niche"]).strip()

    offer_types = _parse_offer_type(lead.get("offer_type"))
    if offer_types:
        fields["Offer Type"] = offer_types

    if lead.get("offer_details"):
        fields["Offer Details"] = str(lead["offer_details"]).strip()

    if lead.get("next_step"):
        fields["Next Step"] = str(lead["next_step"]).strip()

    next_fu = _clean_date(lead.get("next_follow_up"))
    if next_fu:
        fields["Next Follow-up"] = next_fu

    last_ct = _clean_date(lead.get("last_contacted"))
    if last_ct:
        fields["Last Contacted"] = last_ct

    if lead.get("note"):
        fields["Notes"] = str(lead["note"]).strip()

    for ro_field in COMPUTED_READONLY_FIELDS:
        fields.pop(ro_field, None)

    return fields


def _find_matching_crm_lead(
    conn: sqlite3.Connection,
    mapped: dict[str, Any],
) -> dict[str, Any] | None:
    rec_id = mapped.get("airtable_record_id")
    if rec_id:
        row = conn.execute(
            "SELECT * FROM leads WHERE airtable_record_id = ? ORDER BY id ASC LIMIT 1",
            (rec_id,),
        ).fetchone()
        if row is not None:
            return as_dict(row)

    username = mapped.get("username")
    if username:
        row = conn.execute(
            "SELECT * FROM leads WHERE username IS NOT NULL AND lower(username) = lower(?) ORDER BY id ASC LIMIT 1",
            (username,),
        ).fetchone()
        if row is not None:
            return as_dict(row)

    phone = mapped.get("phone")
    if phone:
        row = conn.execute(
            "SELECT * FROM leads WHERE phone IS NOT NULL AND phone = ? ORDER BY id ASC LIMIT 1",
            (phone,),
        ).fetchone()
        if row is not None:
            return as_dict(row)
        phone_digits = re.sub(r"\D", "", phone)
        if len(phone_digits) >= 7:
            for cand in conn.execute(
                "SELECT * FROM leads WHERE phone IS NOT NULL ORDER BY id ASC"
            ).fetchall():
                cand_digits = re.sub(r"\D", "", str(cand["phone"] or ""))
                if cand_digits and cand_digits == phone_digits:
                    return as_dict(cand)

    name = mapped.get("name")
    company = mapped.get("company")
    if name:
        if company:
            row = conn.execute(
                """
                SELECT * FROM leads
                WHERE name IS NOT NULL AND lower(name) = lower(?)
                  AND company IS NOT NULL AND lower(company) = lower(?)
                ORDER BY id ASC LIMIT 1
                """,
                (name, company),
            ).fetchone()
            if row is not None:
                return as_dict(row)
        row = conn.execute(
            "SELECT * FROM leads WHERE name IS NOT NULL AND lower(name) = lower(?) ORDER BY id ASC LIMIT 1",
            (name,),
        ).fetchone()
        if row is not None:
            return as_dict(row)

    return None


def _mark_lead_synced(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    airtable_record_id: str | None,
    airtable_status: str | None,
    synced_at: str,
) -> None:
    conn.execute(
        """
        UPDATE leads
        SET airtable_record_id = COALESCE(?, airtable_record_id),
            airtable_status = COALESCE(?, airtable_status),
            updated_at = ?,
            airtable_synced_at = ?
        WHERE id = ?
        """,
        (airtable_record_id, airtable_status, synced_at, synced_at, int(lead_id)),
    )


def _resolve_related_lead_id(
    conn: sqlite3.Connection,
    related_lead_field: Any,
) -> int | None:
    if not related_lead_field:
        return None
    rec_ids: list[str] = []
    if isinstance(related_lead_field, list):
        rec_ids = [str(x).strip() for x in related_lead_field if str(x).strip()]
    elif isinstance(related_lead_field, str):
        rec_ids = [related_lead_field.strip()]
    for rec_id in rec_ids:
        row = conn.execute(
            "SELECT id FROM leads WHERE airtable_record_id = ? LIMIT 1",
            (rec_id,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None


def _upsert_calendar_event(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    title: str,
    event_type: str,
    start_at: str,
    lead_id: int | None,
    notes: str | None,
    status: str,
    now: str,
) -> int:
    existing = conn.execute(
        "SELECT id FROM calendar_events WHERE source = ? AND external_id = ? LIMIT 1",
        (source, external_id),
    ).fetchone()
    if existing is not None:
        ev_id = int(existing["id"])
        conn.execute(
            """
            UPDATE calendar_events
            SET title = ?,
                event_type = ?,
                start_at = ?,
                all_day = 1,
                lead_id = COALESCE(?, lead_id),
                notes = COALESCE(?, notes),
                status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (title, event_type, start_at, lead_id, notes, status, now, ev_id),
        )
        return ev_id

    cur = conn.execute(
        """
        INSERT INTO calendar_events (
            title, event_type, start_at, end_at, all_day,
            lead_id, meet_link, notes, status, source, external_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, NULL, 1, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (title, event_type, start_at, lead_id, notes, status, source, external_id, now, now),
    )
    return int(cur.lastrowid)


def _sync_tasks_and_projects(
    conn: sqlite3.Connection,
    client: Any,
    now: str,
) -> tuple[int, int]:
    synced_tasks = 0
    synced_projects = 0

    # 1. Tasks
    try:
        task_records = client.list_records(TASKS_TABLE_NAME)
    except Exception:
        task_records = []

    for rec in task_records or []:
        rec_id = str(rec.get("id") or "").strip()
        fields = rec.get("fields") or {}
        title = (fields.get("Task Name") or fields.get("Name") or "").strip()
        due_date = _clean_date(fields.get("Due Date"))
        if not rec_id or not title or not due_date:
            continue
        lead_id = _resolve_related_lead_id(conn, fields.get("Related Lead"))
        notes = (fields.get("Description") or fields.get("Notes") or "").strip() or None
        raw_status = str(fields.get("Status") or "").strip().lower()
        status = (
            "completed"
            if raw_status in ("completed", "done", "сделано", "завершено")
            else ("cancelled" if raw_status in ("cancelled", "canceled") else "scheduled")
        )
        _upsert_calendar_event(
            conn,
            source="airtable_task",
            external_id=rec_id,
            title=title,
            event_type="task_deadline",
            start_at=due_date,
            lead_id=lead_id,
            notes=notes,
            status=status,
            now=now,
        )
        synced_tasks += 1

    # 2. Projects
    try:
        project_records = client.list_records(PROJECTS_TABLE_NAME)
    except Exception:
        project_records = []

    for rec in project_records or []:
        rec_id = str(rec.get("id") or "").strip()
        fields = rec.get("fields") or {}
        title = (fields.get("Project Name") or fields.get("Name") or "").strip()
        end_date = _clean_date(fields.get("End Date"))
        if not rec_id or not title or not end_date:
            continue
        lead_id = _resolve_related_lead_id(conn, fields.get("Related Lead"))
        notes = (
            fields.get("Project Description")
            or fields.get("Description")
            or fields.get("Notes")
            or ""
        ).strip() or None
        raw_status = str(fields.get("Status") or "").strip().lower()
        status = (
            "completed"
            if raw_status in ("completed", "done", "сделано", "завершено")
            else ("cancelled" if raw_status in ("cancelled", "canceled") else "scheduled")
        )
        _upsert_calendar_event(
            conn,
            source="airtable_project",
            external_id=rec_id,
            title=title,
            event_type="project_deadline",
            start_at=end_date,
            lead_id=lead_id,
            notes=notes,
            status=status,
            now=now,
        )
        synced_projects += 1

    return synced_tasks, synced_projects


def pull_from_airtable(
    conn: sqlite3.Connection,
    *,
    client: Any = None,
    preserve_dirty: bool = False,
) -> dict[str, Any]:
    _ensure_dirty_tracking_trigger(conn)
    stages = ensure_airtable_pipeline_stages(conn)
    if client is None:
        client = AirtableRestClient()

    table_name = getattr(client, "table_name", None) or DEFAULT_TABLE_NAME
    records = client.list_records(table_name)
    now = utc_now()

    pulled_leads = 0
    created_in_crm = 0
    updated_in_crm = 0

    for record in records or []:
        if not isinstance(record, dict):
            continue
        mapped = map_airtable_record_to_lead_fields(record, conn)
        if not any(
            (
                mapped.get("name"),
                mapped.get("username"),
                mapped.get("phone"),
                mapped.get("company"),
            )
        ):
            continue

        pulled_leads += 1
        existing = _find_matching_crm_lead(conn, mapped)

        if existing is not None:
            lead_id = int(existing["id"])
            is_dirty = bool(
                existing.get("airtable_record_id")
                and existing.get("airtable_synced_at")
                and str(existing.get("updated_at") or "")
                > str(existing.get("airtable_synced_at") or "")
            )
            if preserve_dirty and is_dirty:
                continue

            patch: dict[str, Any] = {
                "status": mapped["status"],
                "airtable_status": mapped["airtable_status"],
                "airtable_record_id": mapped["airtable_record_id"],
            }
            for key in (
                "name",
                "company",
                "username",
                "phone",
                "url",
                "niche",
                "offer_details",
                "next_step",
                "next_follow_up",
                "last_contacted",
                "lead_source",
                "note",
            ):
                if mapped.get(key) is not None:
                    patch[key] = mapped[key]
            if mapped.get("offer_type"):
                patch["offer_type"] = mapped["offer_type"]
            if mapped.get("amount") is not None:
                patch["amount"] = mapped["amount"]
            if mapped.get("monthly_value") is not None:
                patch["monthly_value"] = mapped["monthly_value"]

            update_lead(conn, lead_id, **patch)
            _mark_lead_synced(
                conn,
                lead_id,
                airtable_record_id=mapped["airtable_record_id"],
                airtable_status=mapped["airtable_status"],
                synced_at=now,
            )
            record_lead_event(
                conn,
                lead_id,
                "airtable_synced",
                f"Синхронизирован с Airtable ({mapped['airtable_status']})",
                {
                    "airtable_record_id": mapped["airtable_record_id"],
                    "airtable_status": mapped["airtable_status"],
                    "direction": "pull",
                },
                created_at=now,
            )
            updated_in_crm += 1
        else:
            created = create_lead(
                conn,
                name=mapped["name"],
                username=mapped["username"],
                phone=mapped["phone"],
                status=mapped["status"],
                note=mapped["note"],
                next_step=mapped["next_step"],
                url=mapped["url"],
                amount=mapped["amount"],
                company=mapped["company"],
                niche=mapped["niche"],
                offer_type=mapped["offer_type"],
                offer_details=mapped["offer_details"],
                monthly_value=mapped["monthly_value"],
                lead_source=mapped["lead_source"],
                next_follow_up=mapped["next_follow_up"],
                last_contacted=mapped["last_contacted"],
                airtable_record_id=mapped["airtable_record_id"],
            )
            lead_id = int(created["id"])
            _mark_lead_synced(
                conn,
                lead_id,
                airtable_record_id=mapped["airtable_record_id"],
                airtable_status=mapped["airtable_status"],
                synced_at=now,
            )
            record_lead_event(
                conn,
                lead_id,
                "airtable_synced",
                f"Импортирован из Airtable ({mapped['airtable_status']})",
                {
                    "airtable_record_id": mapped["airtable_record_id"],
                    "airtable_status": mapped["airtable_status"],
                    "direction": "pull",
                },
                created_at=now,
            )
            created_in_crm += 1

    synced_tasks, synced_projects = _sync_tasks_and_projects(conn, client, now)
    conn.commit()

    return {
        "ok": True,
        "pulled_leads": pulled_leads,
        "created_in_crm": created_in_crm,
        "updated_in_crm": updated_in_crm,
        "synced_tasks": synced_tasks,
        "synced_projects": synced_projects,
        "stages": stages,
        "synced_at": now,
    }


def _find_remote_airtable_record(
    client: Any,
    table_name: str,
    lead: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        records = client.list_records(table_name)
    except Exception:
        return None

    target_username = _clean_username(lead.get("username"))
    target_name = (lead.get("name") or "").strip().casefold()

    for rec in records or []:
        fields = rec.get("fields") or {}
        rec_u, _ = parse_airtable_contact(fields.get("Contact"))
        if target_username and rec_u and rec_u.casefold() == target_username.casefold():
            return rec
        rec_name = str(fields.get("Lead Name") or "").strip().casefold()
        if target_name and rec_name and rec_name == target_name:
            return rec
    return None


def push_lead_to_airtable(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    client: Any = None,
) -> dict[str, Any]:
    _ensure_dirty_tracking_trigger(conn)
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if row is None:
        raise LookupError("lead")
    lead = as_dict(row)

    if client is None:
        client = AirtableRestClient()
    table_name = getattr(client, "table_name", None) or DEFAULT_TABLE_NAME

    fields = map_lead_to_airtable_fields(lead)
    rec_id = (lead.get("airtable_record_id") or "").strip() or None

    if rec_id:
        remote_rec = client.update_record(table_name, rec_id, fields, typecast=True)
        action = "updated"
    else:
        matched_remote = _find_remote_airtable_record(client, table_name, lead)
        if matched_remote and matched_remote.get("id"):
            rec_id = str(matched_remote["id"])
            remote_rec = client.update_record(table_name, rec_id, fields, typecast=True)
            action = "updated"
        else:
            remote_rec = client.create_record(table_name, fields, typecast=True)
            rec_id = str(remote_rec.get("id") or "").strip() or None
            action = "created"

    now = utc_now()
    airtable_st = fields.get("Status") or status_to_airtable(lead.get("status"))
    _mark_lead_synced(
        conn,
        int(lead_id),
        airtable_record_id=rec_id,
        airtable_status=airtable_st,
        synced_at=now,
    )
    record_lead_event(
        conn,
        int(lead_id),
        "airtable_synced",
        f"Отправлен в Airtable ({airtable_st})",
        {
            "airtable_record_id": rec_id,
            "airtable_status": airtable_st,
            "direction": "push",
            "action": action,
        },
        created_at=now,
    )
    conn.commit()
    return {
        "ok": True,
        "action": action,
        "lead_id": int(lead_id),
        "airtable_record_id": rec_id,
        "fields": fields,
        "record": remote_rec,
    }


def push_calendar_date_to_airtable(
    conn: sqlite3.Connection,
    *,
    item_kind: str,
    item_id: int,
    new_date: str,
    client: Any = None,
) -> dict[str, Any]:
    _ensure_dirty_tracking_trigger(conn)
    clean_date = _clean_date(new_date)
    if not clean_date:
        raise ValueError("Укажите корректную дату YYYY-MM-DD")

    kind = str(item_kind or "").strip().lower()
    now = utc_now()

    if kind in ("lead_followup", "followup", "lead"):
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(item_id),)).fetchone()
        if row is None:
            raise LookupError("lead")
        lead = as_dict(row)
        conn.execute(
            "UPDATE leads SET next_follow_up = ?, updated_at = ? WHERE id = ?",
            (clean_date, now, int(item_id)),
        )
        rec_id = (lead.get("airtable_record_id") or "").strip() or None
        pushed = False
        if rec_id:
            if client is None:
                client = AirtableRestClient()
            table_name = getattr(client, "table_name", None) or DEFAULT_TABLE_NAME
            client.update_record(
                table_name,
                rec_id,
                {"Next Follow-up": clean_date},
                typecast=True,
            )
            _mark_lead_synced(
                conn,
                int(item_id),
                airtable_record_id=rec_id,
                airtable_status=lead.get("airtable_status") or status_to_airtable(lead.get("status")),
                synced_at=now,
            )
            pushed = True
        conn.commit()
        return {
            "ok": True,
            "item_kind": "lead_followup",
            "item_id": int(item_id),
            "new_date": clean_date,
            "external_id": rec_id,
            "pushed": pushed,
        }

    ev_row = conn.execute(
        "SELECT * FROM calendar_events WHERE id = ?",
        (int(item_id),),
    ).fetchone()
    if ev_row is None:
        raise LookupError("calendar_event")
    ev = as_dict(ev_row)
    source = str(ev.get("source") or "").strip().lower()
    ext_id = (ev.get("external_id") or "").strip() or None

    conn.execute(
        "UPDATE calendar_events SET start_at = ?, updated_at = ? WHERE id = ?",
        (clean_date, now, int(item_id)),
    )

    resolved_kind = kind
    if resolved_kind not in ("airtable_task", "airtable_project"):
        if source in ("airtable_task", "airtable_project"):
            resolved_kind = source
        elif ev.get("event_type") == "task_deadline":
            resolved_kind = "airtable_task"
        elif ev.get("event_type") == "project_deadline":
            resolved_kind = "airtable_project"

    pushed = False
    if ext_id and resolved_kind == "airtable_task":
        if client is None:
            client = AirtableRestClient()
        client.update_record(
            TASKS_TABLE_NAME,
            ext_id,
            {"Due Date": clean_date},
            typecast=True,
        )
        pushed = True
    elif ext_id and resolved_kind == "airtable_project":
        if client is None:
            client = AirtableRestClient()
        client.update_record(
            PROJECTS_TABLE_NAME,
            ext_id,
            {"End Date": clean_date},
            typecast=True,
        )
        pushed = True

    conn.commit()
    return {
        "ok": True,
        "item_kind": resolved_kind,
        "item_id": int(item_id),
        "new_date": clean_date,
        "external_id": ext_id,
        "pushed": pushed,
    }


def sync_airtable(
    conn: sqlite3.Connection,
    *,
    direction: str = "both",
    client: Any = None,
) -> dict[str, Any]:
    norm_dir = str(direction or "both").strip().lower()
    if norm_dir not in ("both", "pull", "push"):
        raise ValueError("direction должен быть 'both', 'pull' или 'push'")

    _ensure_dirty_tracking_trigger(conn)
    stages = ensure_airtable_pipeline_stages(conn)
    if client is None:
        client = AirtableRestClient()

    pulled_leads = 0
    created_in_crm = 0
    updated_in_crm = 0
    pushed_to_airtable = 0
    synced_tasks = 0
    synced_projects = 0

    if norm_dir in ("both", "pull"):
        pull_res = pull_from_airtable(
            conn,
            client=client,
            preserve_dirty=(norm_dir == "both"),
        )
        pulled_leads = int(pull_res.get("pulled_leads") or 0)
        created_in_crm = int(pull_res.get("created_in_crm") or 0)
        updated_in_crm = int(pull_res.get("updated_in_crm") or 0)
        synced_tasks = int(pull_res.get("synced_tasks") or 0)
        synced_projects = int(pull_res.get("synced_projects") or 0)

    if norm_dir in ("both", "push"):
        dirty_rows = conn.execute(
            """
            SELECT id FROM leads
            WHERE airtable_record_id IS NULL
               OR trim(airtable_record_id) = ''
               OR updated_at > COALESCE(airtable_synced_at, '')
            ORDER BY id ASC
            """
        ).fetchall()
        for r in dirty_rows:
            push_lead_to_airtable(conn, int(r["id"]), client=client)
            pushed_to_airtable += 1

    synced_at = utc_now()
    summary = {
        "ok": True,
        "direction": norm_dir,
        "pulled_leads": pulled_leads,
        "created_in_crm": created_in_crm,
        "updated_in_crm": updated_in_crm,
        "pushed_to_airtable": pushed_to_airtable,
        "synced_tasks": synced_tasks,
        "synced_projects": synced_projects,
        "stages": stages,
        "synced_at": synced_at,
    }
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('airtable_last_sync', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (json.dumps(summary, ensure_ascii=False), synced_at),
    )
    conn.commit()
    return summary
