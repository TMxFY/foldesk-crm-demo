import json
import re
import sqlite3

from app.accounts import check_daily_limit, increment_daily_sent
from app.db import as_dict, normalize_status, utc_now
from app.telegram_gateway import GatewayError


def _is_synthetic_peer_id(peer_id) -> bool:
    try:
        return 900000 <= int(peer_id or 0) < 1000000
    except (TypeError, ValueError):
        return False


def _ensure_lead_events_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def _record_lead_event_safe(
    conn,
    lead_id: int,
    event_type: str,
    title: str,
    meta: dict | None = None,
    now: str | None = None,
) -> None:
    if not lead_id:
        return
    ts = now or utc_now()
    meta_str = json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else None
    try:
        _ensure_lead_events_table(conn)
        conn.execute(
            """
            INSERT INTO lead_events (lead_id, event_type, title, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(lead_id), str(event_type), str(title), meta_str, ts),
        )
    except Exception:
        pass


def _account_provider(conn, account_id: int, *, require_exists: bool = False) -> str:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        if require_exists:
            raise LookupError("account")
        return "telegram"
    data = as_dict(row)
    prov = (data.get("provider") or "telegram").strip().lower()
    return prov if prov in ("telegram", "whatsapp") else "telegram"


def _extract_dialog_phone(item: dict, peer_id: int | None, provider: str) -> str | None:
    raw = (item.get("phone") or "").strip()
    if raw:
        digits = re.sub(r"\D", "", raw)
        if digits and len(digits) >= 7:
            return raw if raw.startswith("+") else f"+{digits}"
        return raw
    if provider == "whatsapp" and peer_id is not None:
        peer_str = str(peer_id).strip()
        if peer_str.isdigit() and 7 <= len(peer_str) <= 15 and not peer_str.startswith("900"):
            return f"+{peer_str}"
    return None


def _public(row) -> dict:
    data = as_dict(row)
    provider = data.get("provider") or "telegram"
    phone = data.get("phone")
    if not phone and provider == "whatsapp":
        peer_str = str(data.get("peer_id") or "").strip()
        if peer_str.isdigit() and 7 <= len(peer_str) <= 15 and not peer_str.startswith("900"):
            phone = f"+{peer_str}"
    out = {
        "id": data["id"],
        "dialog_id": data["id"],
        "account_id": data["account_id"],
        "telegram_user_id": data["peer_id"],
        "username": data["username"],
        "name": data["title"] or "",
        "phone": phone,
        "provider": provider,
        "peer_type": data["peer_type"],
        "unread_count": data["unread_count"],
        "muted": data["muted"],
        "is_group": data["is_group"],
        "last_message_at": data["last_message_at"],
        "last_message_preview": data["last_message_preview"],
        "last_from_peer": data["last_from_peer"],
        "lead_id": data["lead_id"],
    }
    if "account_phone" in data:
        out["account_phone"] = data["account_phone"]
    if "account_name" in data:
        out["account_name"] = data["account_name"]
    if "account_username" in data:
        out["account_username"] = data["account_username"]
    if "lead_status" in data and data["lead_status"] is not None:
        out["lead_status"] = data["lead_status"]
    return out


def stop_outreach_on_reply(
    conn,
    account_id: int | None = None,
    peer_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
    lead_id: int | None = None,
    preview: str | None = None,
    force_event: bool = False,
    min_sent_at: str | None = None,
) -> int:
    now = utc_now()
    clean_min_sent = str(min_sent_at).strip() if min_sent_at else None
    clean_user = str(username).strip().lstrip("@") if username else None
    clean_phone = str(phone).strip() if phone else None
    phone_digits = re.sub(r"\D", "", clean_phone or "")
    phone_variants = set()
    if clean_phone:
        phone_variants.add(clean_phone)
    if phone_digits and len(phone_digits) >= 7:
        phone_variants.add(phone_digits)
        phone_variants.add(f"+{phone_digits}")
    phone_peer_id = int(phone_digits[-12:]) if (phone_digits and len(phone_digits) >= 7) else None
    clean_peer_id = int(peer_id) if peer_id is not None else phone_peer_id
    clean_lead_id = int(lead_id) if lead_id is not None else None

    if not any((clean_lead_id, clean_peer_id, clean_user, phone_variants)):
        return 0

    # Find matching leads first so we can also match campaign_recipients by lead_id
    lead_clauses = []
    lead_params: list = []
    if clean_lead_id is not None:
        lead_clauses.append("id = ?")
        lead_params.append(clean_lead_id)
    if clean_peer_id is not None:
        lead_clauses.append("telegram_user_id = ?")
        lead_params.append(clean_peer_id)
    if clean_user:
        lead_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
        lead_params.append(clean_user)
    if phone_variants:
        ph_v = ",".join("?" for _ in phone_variants)
        lead_clauses.append(f"(phone IS NOT NULL AND phone IN ({ph_v}))")
        lead_params.extend(sorted(phone_variants))

    matched_lead_ids: set[int] = set()
    prev_lead_statuses: dict[int, str] = {}
    if clean_lead_id is not None:
        matched_lead_ids.add(clean_lead_id)

    if lead_clauses:
        lead_rows = conn.execute(
            f"SELECT id, status FROM leads WHERE {' OR '.join(lead_clauses)}",
            lead_params,
        ).fetchall()
        for lr in lead_rows:
            lid = int(lr["id"])
            matched_lead_ids.add(lid)
            prev_lead_statuses[lid] = str(lr["status"] or "")

    # Also find matching campaign_recipients
    rec_clauses = []
    rec_params: list = []
    if matched_lead_ids:
        ph = ",".join("?" for _ in matched_lead_ids)
        rec_clauses.append(f"lead_id IN ({ph})")
        rec_params.extend(sorted(matched_lead_ids))
    if clean_peer_id is not None:
        rec_clauses.append("telegram_user_id = ?")
        rec_params.append(clean_peer_id)
    if clean_user:
        rec_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
        rec_params.append(clean_user)
    if phone_variants:
        ph_v = ",".join("?" for _ in phone_variants)
        rec_clauses.append(f"(phone IS NOT NULL AND phone IN ({ph_v}))")
        rec_params.extend(sorted(phone_variants))

    stopped_count = 0
    stopped_campaign_ids: set[int] = set()
    had_active_recs = False
    if rec_clauses:
        rec_rows = conn.execute(
            f"""
            SELECT id, campaign_id, lead_id, status, last_sent_at FROM campaign_recipients
            WHERE status IN ('pending', 'in_progress', 'completed')
              AND ({' OR '.join(rec_clauses)})
            """,
            rec_params,
        ).fetchall()
        had_active_recs = len(rec_rows) > 0
        if clean_min_sent:
            rec_rows = [
                rr
                for rr in rec_rows
                if not rr["last_sent_at"] or str(rr["last_sent_at"]) <= clean_min_sent
            ]
        for rr in rec_rows:
            if rr["lead_id"]:
                lid = int(rr["lead_id"])
                matched_lead_ids.add(lid)
                if lid not in prev_lead_statuses:
                    lrow = conn.execute("SELECT status FROM leads WHERE id = ?", (lid,)).fetchone()
                    if lrow is not None:
                        prev_lead_statuses[lid] = str(lrow["status"] or "")
            if rr["campaign_id"]:
                stopped_campaign_ids.add(int(rr["campaign_id"]))
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'replied', updated_at = ?
                WHERE id = ?
                """,
                (now, int(rr["id"])),
            )
            stopped_count += 1

    if clean_min_sent and had_active_recs and stopped_count == 0 and not force_event:
        return 0

    if matched_lead_ids:
        primary_lid = sorted(matched_lead_ids)[0]
        if account_id is not None and clean_peer_id is not None:
            conn.execute(
                """
                UPDATE dialogs
                SET lead_id = COALESCE(lead_id, ?)
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (primary_lid, int(account_id), int(clean_peer_id)),
            )

        ph = ",".join("?" for _ in matched_lead_ids)
        conn.execute(
            f"""
            UPDATE leads
            SET status = CASE WHEN status IN ('new', 'waiting') THEN 'talking' ELSE status END,
                airtable_status = CASE WHEN status IN ('new', 'waiting') THEN 'In conversation' ELSE airtable_status END,
                next_step = CASE
                    WHEN status IN ('new', 'waiting')
                    THEN 'Клиент ответил — цепочка остановлена, ответьте вручную'
                    WHEN status NOT IN ('won', 'lost')
                    THEN COALESCE(next_step, 'Клиент ответил — цепочка остановлена, ответьте вручную')
                    ELSE next_step
                END,
                updated_at = ?
            WHERE id IN ({ph})
            """,
            [now, *sorted(matched_lead_ids)],
        )

        for lid in sorted(matched_lead_ids):
            old_status = prev_lead_statuses.get(lid)
            status_moved = old_status in ("new", "waiting")
            if stopped_count > 0 or status_moved or force_event:
                snippet = f": «{str(preview).strip()[:80]}»" if preview and str(preview).strip() else ""
                if stopped_count > 0:
                    title = f"Получен ответ от клиента{snippet} — рассылка остановлена, стадия «Разговор»"
                elif status_moved:
                    title = f"Получен ответ от клиента{snippet} — сделка переведена в «Разговор»"
                else:
                    title = f"Входящее сообщение от клиента{snippet}"
                _record_lead_event_safe(
                    conn,
                    lead_id=lid,
                    event_type="reply_received",
                    title=title,
                    meta={
                        "account_id": account_id,
                        "peer_id": clean_peer_id,
                        "username": clean_user,
                        "stopped_recipients": stopped_count,
                        "campaign_ids": sorted(stopped_campaign_ids),
                        "old_status": old_status,
                        "new_status": "talking" if status_moved else old_status,
                        "preview": str(preview)[:120] if preview else None,
                    },
                    now=now,
                )

    return stopped_count



def remember_dialogs(conn, account_id: int, people: list) -> list:
    acc_provider = _account_provider(conn, account_id)
    now = utc_now()
    saved = []
    for person in people:
        peer_id = person.get("telegram_user_id")
        if peer_id is None and person.get("phone"):
            digits = re.sub(r"\D", "", str(person["phone"]))
            if len(digits) >= 7:
                peer_id = int(digits[-12:])
        if peer_id is None:
            continue
        title = (person.get("name") or "").strip()
        username = person.get("username")
        if username is not None:
            username = str(username).strip().lstrip("@") or None
        provider = str(person.get("provider") or acc_provider or "telegram").strip().lower()
        phone = _extract_dialog_phone(person, int(peer_id), provider)
        unread = person.get("unread_count") if "unread_count" in person else None
        if unread is not None:
            unread = max(0, int(unread))
        preview = person.get("last_message_preview")
        if preview is not None:
            preview = str(preview)[:120]
        sent_at = person.get("last_message_at")
        if sent_at is not None:
            sent_at = str(sent_at)
        last_from = person.get("last_from_peer") if "last_from_peer" in person else None
        if last_from is not None:
            last_from = 1 if last_from else 0
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, phone, provider,
                is_group, unread_count, last_message_at, last_message_preview,
                last_from_peer, muted, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                username = COALESCE(excluded.username, dialogs.username),
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
                phone = COALESCE(excluded.phone, dialogs.phone),
                provider = COALESCE(excluded.provider, dialogs.provider),
                unread_count = COALESCE(?, dialogs.unread_count),
                last_message_at = COALESCE(excluded.last_message_at, dialogs.last_message_at),
                last_message_preview = COALESCE(excluded.last_message_preview, dialogs.last_message_preview),
                last_from_peer = COALESCE(?, dialogs.last_from_peer),
                updated_at = excluded.updated_at
            """,
            (
                account_id,
                int(peer_id),
                username,
                title,
                phone,
                provider,
                0 if unread is None else unread,
                sent_at,
                preview,
                0 if last_from is None else last_from,
                now,
                now,
                unread,
                last_from,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM dialogs
            WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
            """,
            (account_id, int(peer_id)),
        ).fetchone()
        if row and int(row["last_from_peer"] or 0) == 1:
            stop_outreach_on_reply(
                conn,
                account_id=account_id,
                peer_id=int(row["peer_id"]),
                username=row["username"],
                phone=row["phone"],
                lead_id=row["lead_id"],
                preview=row["last_message_preview"],
            )
            row = conn.execute(
                "SELECT * FROM dialogs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        saved.append(_public(row))
    conn.commit()
    return saved


def apply_incoming(conn, account_id: int, message: dict) -> dict:
    if not isinstance(message, dict) or message.get("is_group"):
        raise ValueError("Это не личный диалог")
    if (message.get("peer_type") or "user") != "user":
        raise ValueError("Это не личный диалог")
    if (message.get("direction") or "in") != "in":
        raise ValueError("Нужно входящее")
    peer_id = message.get("telegram_user_id")
    if peer_id is None and message.get("phone"):
        digits = re.sub(r"\D", "", str(message["phone"]))
        if len(digits) >= 7:
            peer_id = int(digits[-12:])
    if peer_id is None:
        raise ValueError("Это не личный диалог")
    acc_provider = _account_provider(conn, account_id, require_exists=True)
    provider = str(message.get("provider") or acc_provider or "telegram").strip().lower()
    phone = _extract_dialog_phone(message, int(peer_id), provider)
    now = utc_now()
    title = (message.get("name") or "").strip()
    username = message.get("username")
    if username is not None:
        username = str(username).strip().lstrip("@") or None
    text = message.get("text")
    preview = "" if text is None else str(text)[:120]
    sent_at = str(message.get("sent_at") or now)
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, 1, ?, ?, 1, 0, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            unread_count = dialogs.unread_count + 1,
            last_message_at = excluded.last_message_at,
            last_message_preview = excluded.last_message_preview,
            last_from_peer = 1,
            updated_at = excluded.updated_at
        """,
        (account_id, int(peer_id), username, title, phone, provider, sent_at, preview, now, now),
    )
    row = conn.execute(
        """
        SELECT * FROM dialogs
        WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
        """,
        (account_id, int(peer_id)),
    ).fetchone()
    mid = message.get("telegram_message_id")
    if mid is not None:
        body = None if text is None else str(text)
        try:
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, sent_at,
                    reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, 'in', ?, ?, NULL, ?, ?)
                ON CONFLICT(dialog_id, telegram_message_id) DO UPDATE SET
                    text = excluded.text,
                    sent_at = excluded.sent_at,
                    updated_at = excluded.updated_at
                """,
                (row["id"], int(mid), body, sent_at, now, now),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("Не удалось записать входящее") from exc
    stop_outreach_on_reply(
        conn,
        account_id=account_id,
        peer_id=int(row["peer_id"]),
        username=row["username"],
        phone=row["phone"],
        lead_id=row["lead_id"],
        preview=preview,
        force_event=True,
    )
    row = conn.execute(
        "SELECT * FROM dialogs WHERE id = ?",
        (row["id"],),
    ).fetchone()
    conn.commit()
    return _public(row)


def sync_inbox(conn, account_id: int, people: list) -> list:
    acc_provider = _account_provider(conn, account_id, require_exists=True)
    now = utc_now()
    saved = []
    try:
        for person in people or []:
            if not isinstance(person, dict) or person.get("is_group"):
                continue
            if (person.get("peer_type") or "user") != "user":
                continue
            peer_id = person.get("telegram_user_id")
            if peer_id is None and person.get("phone"):
                digits = re.sub(r"\D", "", str(person["phone"]))
                if len(digits) >= 7:
                    peer_id = int(digits[-12:])
            if peer_id is None:
                continue
            title = (person.get("name") or "").strip()
            username = person.get("username")
            if username is not None:
                username = str(username).strip().lstrip("@") or None
            provider = str(person.get("provider") or acc_provider or "telegram").strip().lower()
            phone = _extract_dialog_phone(person, int(peer_id), provider)
            unread = person.get("unread_count") if "unread_count" in person else None
            if unread is not None:
                unread = max(0, int(unread))
            preview = person.get("last_message_preview")
            if preview is not None:
                preview = str(preview)[:120]
            sent_at = person.get("last_message_at")
            if sent_at is not None:
                sent_at = str(sent_at)
            last_from = person.get("last_from_peer") if "last_from_peer" in person else None
            if last_from is not None:
                last_from = 1 if last_from else 0
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, phone, provider,
                    is_group, unread_count, last_message_at, last_message_preview,
                    last_from_peer, muted, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                    username = COALESCE(excluded.username, dialogs.username),
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
                    phone = COALESCE(excluded.phone, dialogs.phone),
                    provider = COALESCE(excluded.provider, dialogs.provider),
                    unread_count = COALESCE(?, dialogs.unread_count),
                    last_message_at = COALESCE(excluded.last_message_at, dialogs.last_message_at),
                    last_message_preview = COALESCE(excluded.last_message_preview, dialogs.last_message_preview),
                    last_from_peer = COALESCE(?, dialogs.last_from_peer),
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    int(peer_id),
                    username,
                    title,
                    phone,
                    provider,
                    0 if unread is None else unread,
                    sent_at,
                    preview,
                    0 if last_from is None else last_from,
                    now,
                    now,
                    unread,
                    last_from,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM dialogs
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (account_id, int(peer_id)),
            ).fetchone()
            if row and int(row["last_from_peer"] or 0) == 1:
                stop_outreach_on_reply(
                    conn,
                    account_id=account_id,
                    peer_id=int(row["peer_id"]),
                    username=row["username"],
                    phone=row["phone"],
                    lead_id=row["lead_id"],
                    preview=row["last_message_preview"],
                )
                row = conn.execute(
                    "SELECT * FROM dialogs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
            saved.append(_public(row))
        conn.commit()
    except (sqlite3.IntegrityError, TypeError, ValueError) as exc:
        conn.rollback()
        raise ValueError("Не удалось записать входящие") from exc
    return saved


class InboxResultList(list):
    def __init__(self, items=(), counts: dict | None = None):
        super().__init__(items)
        self.counts = counts or {
            "waiting": 0,
            "our_turn": 0,
            "unread": 0,
            "groups": 0,
            "muted": 0,
        }


_INBOX_SELECT_SQL = """
    SELECT
        d.*,
        COALESCE(d.provider, a.provider, 'telegram') AS provider,
        d.phone AS phone,
        a.phone AS account_phone,
        COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
        a.telegram_username AS account_username,
        COALESCE(
            (SELECT l.status FROM leads l WHERE l.id = d.lead_id LIMIT 1),
            (SELECT l.status FROM leads l WHERE l.telegram_user_id = d.peer_id AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE l.telegram_user_id = d.peer_id ORDER BY l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') ORDER BY l.id LIMIT 1)
        ) AS lead_status
    FROM dialogs d
    JOIN accounts a ON a.id = d.account_id
"""


def _resolve_missing_lead_status_by_phone(conn, dialogs: list[dict]) -> None:
    need_phone = [
        d for d in dialogs
        if not d.get("lead_status") and d.get("phone")
    ]
    if not need_phone:
        return
    lead_rows = conn.execute(
        "SELECT id, account_id, phone, status FROM leads WHERE phone IS NOT NULL AND phone != '' ORDER BY id ASC"
    ).fetchall()
    if not lead_rows:
        return
    by_digits: dict[str, list[dict]] = {}
    for lr in lead_rows:
        ld = as_dict(lr)
        digits = re.sub(r"\D", "", str(ld.get("phone") or ""))
        if len(digits) >= 7:
            by_digits.setdefault(digits, []).append(ld)
            if len(digits) > 10:
                by_digits.setdefault(digits[-10:], []).append(ld)
    for d in need_phone:
        digits = re.sub(r"\D", "", str(d.get("phone") or ""))
        if len(digits) < 7:
            continue
        candidates = by_digits.get(digits) or (by_digits.get(digits[-10:]) if len(digits) > 10 else None)
        if not candidates:
            continue
        acc_id = d.get("account_id")
        best = sorted(
            candidates,
            key=lambda c: (
                0 if c.get("account_id") == acc_id else (1 if c.get("account_id") is None else 2),
                int(c.get("id") or 0),
            ),
        )[0]
        if best.get("status"):
            d["lead_status"] = best["status"]
        if not d.get("lead_id") and best.get("id"):
            d["lead_id"] = best["id"]


def _query_inbox_rows(
    conn,
    account_id: int | None = None,
    q: str = "",
    provider: str | None = None,
    status: str | None = None,
) -> list[dict]:
    where_clauses = ["(d.peer_type = 'user' OR d.is_group = 1)"]
    params: list = []

    if account_id is not None and int(account_id) != 0:
        acc_id = int(account_id)
        account = conn.execute("SELECT id FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        if account is None:
            raise LookupError("account")
        where_clauses.append("d.account_id = ?")
        params.append(acc_id)

    clean_provider = (provider or "").strip().lower()
    if clean_provider in ("telegram", "whatsapp"):
        where_clauses.append("lower(COALESCE(d.provider, a.provider, 'telegram')) = ?")
        params.append(clean_provider)

    sql = (
        f"{_INBOX_SELECT_SQL} "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY (d.last_message_at IS NULL), d.last_message_at DESC, d.updated_at DESC, d.id DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    found = [_public(row) for row in rows]
    _resolve_missing_lead_status_by_phone(conn, found)

    raw_status = (status or "").strip()
    if raw_status:
        try:
            clean_status = normalize_status(raw_status, conn).strip().lower()
        except ValueError:
            clean_status = raw_status.lower()
        found = [
            row
            for row in found
            if row.get("lead_status")
            and (
                str(row["lead_status"]).strip().lower() == clean_status
                or normalize_status(str(row["lead_status"]), conn).strip().lower() == clean_status
            )
        ]

    needle = (q or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if needle:
        folded = needle.casefold()
        needle_digits = re.sub(r"\D", "", needle)
        matched = []
        for row in found:
            phone_str = row.get("phone") or ""
            phone_digits = re.sub(r"\D", "", phone_str)
            blob = " ".join((
                row.get("name") or "",
                row.get("username") or "",
                phone_str,
                phone_digits,
                str(row.get("telegram_user_id") or ""),
                row.get("account_name") or "",
                row.get("account_phone") or "",
                row.get("provider") or "",
            )).casefold()
            if folded in blob or (needle_digits and len(needle_digits) >= 3 and needle_digits in phone_digits):
                matched.append(row)
        found = matched

    return found


def compute_inbox_counts(dialogs: list[dict]) -> dict:
    return {
        "waiting": sum(1 for d in dialogs if int(d.get("last_from_peer") or 0) == 1),
        "our_turn": sum(1 for d in dialogs if int(d.get("last_from_peer") or 0) == 0),
        "unread": sum(1 for d in dialogs if int(d.get("unread_count") or 0) > 0),
        "groups": sum(1 for d in dialogs if int(d.get("is_group") or 0) == 1),
        "muted": sum(1 for d in dialogs if int(d.get("muted") or 0) == 1),
    }


def get_inbox_counts(
    conn,
    account_id: int | None = None,
    q: str = "",
    provider: str | None = None,
    status: str | None = None,
) -> dict:
    base_rows = _query_inbox_rows(
        conn, account_id=account_id, q=q, provider=provider, status=status
    )
    return compute_inbox_counts(base_rows)


def _matches_chips(row: dict, chips: set[str]) -> bool:
    if "waiting" in chips and int(row.get("last_from_peer") or 0) != 1:
        return False
    if "our_turn" in chips and int(row.get("last_from_peer") or 0) != 0:
        return False
    if "unread" in chips and int(row.get("unread_count") or 0) <= 0:
        return False
    if "groups" in chips and int(row.get("is_group") or 0) != 1:
        return False
    if "muted" in chips and int(row.get("muted") or 0) != 1:
        return False
    return True


def list_inbox(
    conn,
    account_id: int | None = None,
    q: str = "",
    chip: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    with_counts: bool = False,
) -> list | dict:
    base_rows = _query_inbox_rows(
        conn, account_id=account_id, q=q, provider=provider, status=status
    )
    counts = compute_inbox_counts(base_rows)

    chips = {c.strip().lower() for c in (chip or "").split(",") if c.strip()}
    if chips:
        filtered = [row for row in base_rows if _matches_chips(row, chips)]
    else:
        filtered = base_rows

    has_explicit_filter = bool(
        chips
        or (q or "").strip()
        or (provider or "").strip()
        or (status or "").strip()
    )
    if has_explicit_filter:
        cap = 500
    else:
        cap = 60 if (account_id is None or int(account_id) == 0) else 30
    capped = filtered[:cap]

    if with_counts:
        return {"dialogs": capped, "counts": counts}
    return InboxResultList(capped, counts=counts)


def _load_dialog_public(conn, dialog_id: int) -> dict:
    row = conn.execute(
        f"{_INBOX_SELECT_SQL} WHERE d.id = ?",
        (int(dialog_id),),
    ).fetchone()
    if row is None:
        raise LookupError("dialog")
    return _public(row)


def mark_dialog_unread(conn, dialog_id: int) -> dict:
    did = int(dialog_id)
    existing = conn.execute("SELECT id, account_id FROM dialogs WHERE id = ?", (did,)).fetchone()
    if existing is None:
        raise LookupError("dialog")
    now = utc_now()
    conn.execute(
        """
        UPDATE dialogs
        SET unread_count = MAX(1, COALESCE(unread_count, 0)),
            last_from_peer = 1,
            updated_at = ?
        WHERE id = ?
        """,
        (now, did),
    )
    conn.commit()
    out = _load_dialog_public(conn, did)
    out["counts"] = get_inbox_counts(conn, account_id=out["account_id"])
    out["all_counts"] = get_inbox_counts(conn, account_id=0)
    return out


def mark_dialog_read(conn, dialog_id: int) -> dict:
    did = int(dialog_id)
    existing = conn.execute("SELECT id, account_id FROM dialogs WHERE id = ?", (did,)).fetchone()
    if existing is None:
        raise LookupError("dialog")
    now = utc_now()
    conn.execute(
        """
        UPDATE dialogs
        SET unread_count = 0,
            updated_at = ?
        WHERE id = ?
        """,
        (now, did),
    )
    conn.commit()
    out = _load_dialog_public(conn, did)
    out["counts"] = get_inbox_counts(conn, account_id=out["account_id"])
    out["all_counts"] = get_inbox_counts(conn, account_id=0)
    return out


def open_dialog_by_lead(
    conn,
    lead_id: int | None = None,
    account_id: int | None = None,
    telegram_user_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    now = utc_now()
    explicit_acc_id = int(account_id) if (account_id is not None and int(account_id) > 0) else None
    explicit_acc = None
    if explicit_acc_id is not None:
        acc_check = conn.execute("SELECT * FROM accounts WHERE id = ?", (explicit_acc_id,)).fetchone()
        if acc_check is None:
            raise LookupError("account")
        explicit_acc = as_dict(acc_check)

    clean_user = str(username).strip().lstrip("@") if username and str(username).strip() else None
    clean_phone = str(phone).strip() if phone and str(phone).strip() else None
    clean_tg_id = (
        int(telegram_user_id)
        if telegram_user_id is not None and str(telegram_user_id).strip() != ""
        else None
    )
    clean_name = str(name).strip() if name and str(name).strip() else None

    lead_row = None
    if lead_id is not None and int(lead_id) > 0:
        lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if lead_row is None and clean_tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (clean_tg_id,),
        ).fetchone()
    if lead_row is None and clean_user:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE username IS NOT NULL AND ltrim(lower(username), '@') = lower(?) ORDER BY id LIMIT 1",
            (clean_user,),
        ).fetchone()
    if lead_row is None and clean_phone:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
            (clean_phone,),
        ).fetchone()
        if lead_row is None:
            p_digits = re.sub(r"\D", "", clean_phone)
            if len(p_digits) >= 7:
                p_peer_id = int(p_digits[-12:])
                lead_row = conn.execute(
                    "SELECT * FROM leads WHERE telegram_user_id = ? OR phone = ? OR phone = ? ORDER BY id LIMIT 1",
                    (p_peer_id, p_digits, f"+{p_digits}"),
                ).fetchone()

    if lead_row is not None:
        lead = as_dict(lead_row)
        lid = int(lead["id"])
        if clean_tg_id is not None and not lead.get("telegram_user_id"):
            lead["telegram_user_id"] = clean_tg_id
        if clean_user and not lead.get("username"):
            lead["username"] = clean_user
        if clean_phone and not lead.get("phone"):
            lead["phone"] = clean_phone
        if clean_name and not lead.get("name"):
            lead["name"] = clean_name
    elif any((clean_tg_id is not None, clean_user, clean_phone, clean_name)):
        lead = {
            "id": None,
            "account_id": explicit_acc_id,
            "telegram_user_id": clean_tg_id,
            "username": clean_user,
            "phone": clean_phone,
            "name": clean_name,
            "provider": "telegram",
            "dialog_id": None,
            "note": None,
            "description": None,
        }
        lid = None
    else:
        raise LookupError("lead")

    lead_provider = (lead.get("provider") or "telegram").strip().lower()
    lead_phone = (lead.get("phone") or "").strip() or None
    phone_digits = re.sub(r"\D", "", lead_phone or "")
    wa_peer_id = int(phone_digits[-12:]) if (phone_digits and len(phone_digits) >= 7) else None

    if explicit_acc is not None:
        wanted_provider = (explicit_acc.get("provider") or "telegram").strip().lower()
    else:
        wanted_provider = lead_provider

    # 1. Try existing dialog on requested account (or any account if not specified)
    existing_dlg = None
    match_lid = lid if lid is not None else -1
    if explicit_acc_id is not None:
        existing_dlg = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.account_id = ?
              AND d.peer_type = 'user'
              AND (
                  d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.phone IS NOT NULL AND d.phone = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY d.id LIMIT 1
            """,
            (
                explicit_acc_id,
                match_lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                wa_peer_id,
                wa_peer_id,
                lead_phone,
                lead_phone,
                lead.get("username"),
                lead.get("username"),
            ),
        ).fetchone()
    else:
        existing_dlg = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.peer_type = 'user'
              AND COALESCE(d.provider, a.provider, 'telegram') = ?
              AND (
                  d.id = ?
                  OR d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.phone IS NOT NULL AND d.phone = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY (d.lead_id = ?) DESC, d.id LIMIT 1
            """,
            (
                wanted_provider,
                lead.get("dialog_id") or -1,
                match_lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                wa_peer_id,
                wa_peer_id,
                lead_phone,
                lead_phone,
                lead.get("username"),
                lead.get("username"),
                match_lid,
            ),
        ).fetchone()

    if existing_dlg is not None:
        dlg_id = int(existing_dlg["id"])
        conn.execute(
            """
            UPDATE dialogs
            SET lead_id = COALESCE(?, lead_id),
                phone = COALESCE(phone, ?),
                provider = COALESCE(provider, ?)
            WHERE id = ?
            """,
            (lid, lead_phone, wanted_provider, dlg_id),
        )
        if lid is not None:
            conn.execute("UPDATE leads SET dialog_id = ? WHERE id = ?", (dlg_id, lid))
        conn.commit()
        refreshed = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.id = ?
            """,
            (dlg_id,),
        ).fetchone()
        return _public(refreshed)

    # 2. Determine target account for creating a new dialog
    target_acc_id = explicit_acc_id
    if target_acc_id is None and lead.get("account_id"):
        acc_row = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND status = 'connected'",
            (int(lead["account_id"]),),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE status = 'connected' AND COALESCE(provider, 'telegram') = ? ORDER BY id LIMIT 1",
            (wanted_provider,),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE COALESCE(provider, 'telegram') = ? ORDER BY id LIMIT 1",
            (wanted_provider,),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE status = 'connected' ORDER BY id LIMIT 1"
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        raise ValueError("Подключите хотя бы один Telegram-аккаунт для открытия чата")

    acc_provider = _account_provider(conn, target_acc_id)
    is_whatsapp = (wanted_provider == "whatsapp" or acc_provider == "whatsapp")
    dialog_provider = "whatsapp" if is_whatsapp else "telegram"

    fallback_synth = 900000 + (lid if lid is not None else (abs(hash(clean_user or clean_phone or clean_name or "0")) % 99999 + 1))
    if dialog_provider == "whatsapp":
        if wa_peer_id is not None:
            peer_id = wa_peer_id
        elif lead.get("telegram_user_id"):
            peer_id = int(lead["telegram_user_id"])
        else:
            peer_id = fallback_synth
    else:
        peer_id = int(lead.get("telegram_user_id") or wa_peer_id or fallback_synth)

    username = (lead.get("username") or "").strip().lstrip("@") or None
    title = (lead.get("name") or "").strip() or username or lead_phone or (f"Лид #{lid}" if lid is not None else "Контакт")
    preview = (lead.get("note") or lead.get("description") or "Контакт из CRM · готов к диалогу")[:120]

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, ?, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN dialogs.title IS NULL OR dialogs.title = '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            lead_id = COALESCE(excluded.lead_id, dialogs.lead_id),
            updated_at = excluded.updated_at
        """,
        (target_acc_id, peer_id, username, title, lead_phone, dialog_provider, now, preview, lid, now, now),
    )
    created_row = conn.execute(
        """
        SELECT d.*,
               COALESCE(d.provider, a.provider, 'telegram') AS provider,
               d.phone AS phone,
               a.phone AS account_phone,
               COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
               a.telegram_username AS account_username
        FROM dialogs d
        JOIN accounts a ON a.id = d.account_id
        WHERE d.account_id = ? AND d.peer_type = 'user' AND d.peer_id = ?
        """,
        (target_acc_id, peer_id),
    ).fetchone()
    dlg_id = int(created_row["id"])
    if lid is not None:
        try:
            conn.execute(
                """
                UPDATE leads
                SET dialog_id = ?,
                    account_id = COALESCE(account_id, ?),
                    telegram_user_id = COALESCE(telegram_user_id, ?),
                    provider = COALESCE(provider, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (dlg_id, target_acc_id, peer_id, dialog_provider, now, lid),
            )
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE leads SET dialog_id = ?, updated_at = ? WHERE id = ?",
                (dlg_id, now, lid),
            )
    conn.commit()
    return _public(created_row)


def list_messages(conn, dialog_id: int, limit: int | None = None) -> list:
    dialog = conn.execute("SELECT id FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    if limit is None:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE dialog_id = ?
            ORDER BY sent_at, id
            """,
            (dialog_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE dialog_id = ?
                ORDER BY sent_at DESC, id DESC
                LIMIT ?
            ) ORDER BY sent_at, id
            """,
            (dialog_id, int(limit)),
        ).fetchall()
    return [as_dict(row) for row in rows]


def _cache_messages(conn, dialog_id: int, items: list) -> None:
    now = utc_now()
    normalized = [_normalize_message(item, now) for item in items]
    try:
        for message_id, direction, text, sent_at, reply_to in normalized:
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, sent_at,
                    reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dialog_id, telegram_message_id) DO UPDATE SET
                    direction = excluded.direction,
                    text = excluded.text,
                    sent_at = excluded.sent_at,
                    reply_to_telegram_message_id = excluded.reply_to_telegram_message_id,
                    updated_at = excluded.updated_at
                """,
                (dialog_id, message_id, direction, text, sent_at, reply_to, now, now),
            )
        if normalized:
            latest = max(normalized, key=lambda m: (m[3], m[0]))
            _, last_dir, last_text, last_at, _ = latest
            last_preview = "" if last_text is None else str(last_text)[:120]
            if last_dir == "in":
                conn.execute(
                    """
                    UPDATE dialogs
                    SET last_from_peer = 1,
                        last_message_at = COALESCE(?, last_message_at),
                        last_message_preview = CASE WHEN ? != '' THEN ? ELSE last_message_preview END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (last_at, last_preview, last_preview, now, dialog_id),
                )
                dlg_row = conn.execute(
                    "SELECT * FROM dialogs WHERE id = ?", (dialog_id,)
                ).fetchone()
                if dlg_row is not None:
                    stop_outreach_on_reply(
                        conn,
                        account_id=int(dlg_row["account_id"]),
                        peer_id=int(dlg_row["peer_id"]),
                        username=dlg_row["username"],
                        phone=dlg_row["phone"],
                        lead_id=dlg_row["lead_id"],
                        preview=last_preview,
                        min_sent_at=last_at,
                    )
            else:
                conn.execute(
                    """
                    UPDATE dialogs
                    SET last_from_peer = 0,
                        last_message_at = CASE WHEN last_message_at IS NULL OR last_message_at <= ? THEN ? ELSE last_message_at END,
                        last_message_preview = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (last_at, last_at, last_preview, now, dialog_id),
                )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Не удалось записать историю") from exc


def _normalize_message(item, now: str):
    if not isinstance(item, dict) or item.get("telegram_message_id") is None:
        raise GatewayError("История без номера сообщения")
    direction = item.get("direction") or "in"
    if direction not in ("in", "out"):
        raise GatewayError("Непонятное направление сообщения")
    text = item.get("text")
    if text is not None:
        text = str(text)
    sent_at = item.get("sent_at") or now
    reply = item.get("reply_to_telegram_message_id")
    return (
        int(item["telegram_message_id"]),
        direction,
        text,
        str(sent_at),
        int(reply) if reply else None,
    )


async def get_thread(conn, dialog_id: int, gateway, limit: int = 50) -> list:
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    if dialog["peer_type"] != "user" or int(dialog["is_group"] or 0):
        raise ValueError("Это не личный диалог")
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
    ).fetchone()
    if account is None:
        raise LookupError("account")
    account = as_dict(account)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    cap = 50 if limit is None else max(1, min(int(limit), 50))
    peer_id = int(dialog["peer_id"])
    if _is_synthetic_peer_id(peer_id) and dialog.get("username") and hasattr(gateway, "search_dialogs"):
        try:
            found = await gateway.search_dialogs(
                account["session_path"], f"@{dialog['username']}", limit=1
            )
            if isinstance(found, list) and found and found[0].get("telegram_user_id"):
                real_id = int(found[0]["telegram_user_id"])
                if real_id != peer_id:
                    try:
                        conn.execute(
                            "UPDATE dialogs SET peer_id = ?, updated_at = ? WHERE id = ?",
                            (real_id, utc_now(), dialog_id),
                        )
                        if dialog.get("lead_id"):
                            conn.execute(
                                "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                (real_id, utc_now(), int(dialog["lead_id"])),
                            )
                        conn.commit()
                        peer_id = real_id
                    except sqlite3.IntegrityError:
                        conn.rollback()
        except Exception:
            pass
    if _is_synthetic_peer_id(peer_id):
        return list_messages(conn, dialog_id, cap)
    fetched = await gateway.fetch_messages(account["session_path"], peer_id, cap)
    if not isinstance(fetched, list):
        raise GatewayError("История без списка")
    _cache_messages(conn, dialog_id, fetched[-cap:])
    return list_messages(conn, dialog_id, cap)


async def send_reply(conn, dialog_id: int, text: str, gateway, reply_to=None) -> dict:
    body = (text or "").strip()
    if not body:
        raise ValueError("Напишите сообщение")
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
    ).fetchone()
    if account is None:
        raise LookupError("account")
    account = as_dict(account)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    check_daily_limit(conn, account["id"])
    if gateway is None:
        next_row = conn.execute(
            "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone()
        sent = {
            "telegram_message_id": int(next_row["next_id"]),
            "text": body,
            "sent_at": utc_now(),
        }
    else:
        peer_target = int(dialog["peer_id"])
        if _is_synthetic_peer_id(peer_target) and dialog.get("username"):
            if hasattr(gateway, "search_dialogs"):
                try:
                    found = await gateway.search_dialogs(
                        account["session_path"], f"@{dialog['username']}", limit=1
                    )
                    if isinstance(found, list) and found and found[0].get("telegram_user_id"):
                        real_id = int(found[0]["telegram_user_id"])
                        peer_target = real_id
                        try:
                            conn.execute(
                                "UPDATE dialogs SET peer_id = ?, updated_at = ? WHERE id = ?",
                                (real_id, utc_now(), dialog_id),
                            )
                            if dialog.get("lead_id"):
                                conn.execute(
                                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                    (real_id, utc_now(), int(dialog["lead_id"])),
                                )
                            conn.commit()
                        except sqlite3.IntegrityError:
                            conn.rollback()
                except Exception:
                    pass
            if isinstance(peer_target, int) and _is_synthetic_peer_id(peer_target):
                peer_target = f"@{dialog['username']}"
        sent = await gateway.send_message(
            account["session_path"], peer_target, body, reply_to
        )
    if not isinstance(sent, dict) or sent.get("telegram_message_id") is None:
        raise GatewayError("Ответ без номера сообщения")
    now = utc_now()
    sent_at = sent.get("sent_at") or now
    message_text = (sent.get("text") or body).strip() or body
    try:
        cur = conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, sent_at,
                reply_to_telegram_message_id, created_at, updated_at
            ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?)
            """,
            (
                dialog_id,
                int(sent["telegram_message_id"]),
                message_text,
                sent_at,
                int(reply_to) if reply_to else None,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Такое сообщение уже есть") from exc
    preview = message_text[:120]
    conn.execute(
        """
        UPDATE dialogs
        SET last_message_at = ?, last_message_preview = ?, last_from_peer = 0, updated_at = ?
        WHERE id = ?
        """,
        (sent_at, preview, now, dialog_id),
    )
    increment_daily_sent(conn, account["id"])
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


async def start_direct_dialog(
    conn,
    handle: str,
    account_id: int | None = None,
    name: str | None = None,
    gateway=None,
) -> dict:
    raw = (handle or "").strip()
    if not raw:
        raise ValueError("Введите @username или номер телефона")
    clean_raw = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "@", raw, flags=re.IGNORECASE).strip()

    target_acc = None
    if account_id is not None and int(account_id) > 0:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        row = conn.execute(
            "SELECT * FROM accounts WHERE status = 'connected' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        raise ValueError("Подключите хотя бы один аккаунт Telegram или WhatsApp")

    acc_id = int(target_acc["id"])
    provider = str(target_acc.get("provider") or "telegram").strip().lower()
    if provider not in ("telegram", "whatsapp"):
        provider = "telegram"

    phone = None
    username = None
    resolved_tg_id = None
    resolved_name = (name or "").strip() or None

    digits = re.sub(r"\D", "", clean_raw)
    if clean_raw.startswith("+") or (digits and len(digits) >= 7 and not re.search(r"[A-Za-zА-Яа-я]", clean_raw)):
        phone = f"+{digits}"
    else:
        username = clean_raw.lstrip("@").split("/")[0].split("?")[0].strip() or None

    session_path = str(target_acc.get("session_path") or "")
    if (
        gateway is not None
        and target_acc.get("status") == "connected"
        and not session_path.startswith("data/sessions/demo")
        and hasattr(gateway, "search_dialogs")
    ):
        search_q = f"@{username}" if username else (phone or clean_raw)
        try:
            found = await gateway.search_dialogs(session_path, search_q, limit=1)
            if isinstance(found, list) and found:
                first = found[0]
                if first.get("telegram_user_id"):
                    resolved_tg_id = int(first["telegram_user_id"])
                if first.get("username"):
                    username = str(first["username"]).strip().lstrip("@")
                if first.get("name") and not resolved_name:
                    resolved_name = str(first["name"]).strip()
        except Exception:
            pass

    now = utc_now()
    lead_row = None
    if resolved_tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (resolved_tg_id,),
        ).fetchone()
    if lead_row is None and username:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE lower(username) = lower(?) ORDER BY id LIMIT 1",
            (username,),
        ).fetchone()
    if lead_row is None and phone:
        phone_peer_id = int(digits[-12:]) if (digits and len(digits) >= 7) else None
        lead_row = conn.execute(
            """
            SELECT * FROM leads
            WHERE phone = ?
               OR phone = ?
               OR (? IS NOT NULL AND telegram_user_id = ?)
            ORDER BY id LIMIT 1
            """,
            (phone, digits, phone_peer_id, phone_peer_id),
        ).fetchone()
        if lead_row is None and digits:
            for cand in conn.execute(
                "SELECT * FROM leads WHERE phone IS NOT NULL AND phone != '' ORDER BY id"
            ).fetchall():
                if re.sub(r"\D", "", str(cand["phone"] or "")) == digits:
                    lead_row = cand
                    break

    if lead_row is not None:
        lead_id = int(lead_row["id"])
        if resolved_tg_id is not None and not lead_row["telegram_user_id"]:
            try:
                conn.execute(
                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                    (resolved_tg_id, now, lead_id),
                )
            except sqlite3.IntegrityError:
                pass
    else:
        display_name = resolved_name or (f"@{username}" if username else phone) or clean_raw
        cur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, phone,
                status, note, next_step, created_at, updated_at, provider
            ) VALUES (?, ?, ?, ?, ?, 'talking', ?, ?, ?, ?, ?)
            """,
            (
                acc_id,
                resolved_tg_id,
                username,
                display_name,
                phone,
                "Открыт прямой чат в Telegram Web",
                "Написать сообщение в чате",
                now,
                now,
                provider,
            ),
        )
        lead_id = int(cur.lastrowid)
        if resolved_tg_id is None:
            resolved_tg_id = int(digits[-12:]) if (provider == "whatsapp" and digits and len(digits) >= 7) else (900000 + lead_id)
            try:
                conn.execute(
                    "UPDATE leads SET telegram_user_id = ? WHERE id = ?",
                    (resolved_tg_id, lead_id),
                )
            except sqlite3.IntegrityError:
                pass
        conn.commit()

    dialog = open_dialog_by_lead(conn, lead_id, acc_id)
    updated_lead = as_dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
    return {"dialog": dialog, "lead": updated_lead}
