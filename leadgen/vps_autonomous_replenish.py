from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
import urllib.parse

logger = logging.getLogger(__name__)


class _SigtermInterrupt(BaseException):
    """Raised on SIGTERM so inner except-Exception handlers cannot swallow shutdown."""

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

import app.digest as app_digest
from app.db import ROOT, as_dict, connect, init_db, resolve_session, utc_now
from app.leadgen_settings import (
    _clean_chat_list,
    _resolve_state_dir,
    get_leadgen_config,
    push_discovered_now,
    resolve_target_b2b_tg_campaign_name,
)
from app.outreach import preview_outreach_batch
from leadgen.supply_side import sanitize_person_and_company, sniper_reject_reason
from leadgen.tg_to_gsheet import (
    admit_discovered_row,
    apply_master_merge,
    email_of_text,
    linkedin_of,
    whatsapp_url,
    x_profile_url,
)
from leadgen.monid_client import check_monid_ready as _client_check_monid_ready, monid_bin
from leadgen.treg_client import check_treg_ready as _client_check_treg_ready, treg_bin

DEFAULT_DB_PATH = "/opt/crmtg/data/crm.db"
DEFAULT_SCRAPER_SESSIONS_DIR = "/opt/crmtg/data/scraper_sessions"
_NOISE_TG_HANDLES = frozenset({"joinchat", "share", "addstickers", "proxy", "iv", "s", "c", "boost", "socks"})


def check_treg_ready() -> bool:
    return _client_check_treg_ready(binary_fn=treg_bin)


def check_monid_ready() -> bool:
    return _client_check_monid_ready(binary_fn=monid_bin)


def _count_jsonl_lines(p: Path) -> int:
    if not p.is_file() or p.stat().st_size == 0:
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    out: list[dict[str, Any]] = []
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    return out


def _lead_retry_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for id_field in ("id", "telegram_user_id"):
        rid = row.get(id_field)
        if rid not in (None, "", 0):
            keys.add(f"{id_field}:{rid}")
    uname = (
        str(
            row.get("username")
            or row.get("telegram_username")
            or row.get("tg_username")
            or ""
        )
        .strip()
        .lstrip("@")
        .lower()
    )
    if uname:
        keys.add(f"u:{uname}")
    purl = (
        str(
            row.get("project_url")
            or row.get("website")
            or row.get("site")
            or row.get("url")
            or ""
        )
        .strip()
        .rstrip("/")
        .lower()
    )
    if purl:
        keys.add(f"p:{purl}")
    fx = x_profile_url(row)
    if fx:
        keys.add(f"x:{fx.lower()}")
    for extra in ("email", "phone"):
        val = str(row.get(extra) or "").strip().lower()
        if val:
            keys.add(f"{extra}:{val}")
    return keys


def _write_pending_qualify_queue(queue_path: Path, rows: list[dict[str, Any]]) -> None:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rkeys = _lead_retry_keys(row)
        if rkeys and (rkeys & seen_keys):
            continue
        seen_keys.update(rkeys)
        deduped.append(row)
    if not deduped:
        queue_path.unlink(missing_ok=True)
        return
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = queue_path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in deduped) + "\n",
        encoding="utf-8",
    )
    tmp.replace(queue_path)


def _count_personal_tg_rows(jsonl_path: Path) -> int:
    if not jsonl_path.is_file():
        return 0
    cnt = 0
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        cand = obj.get("username") or obj.get("telegram_username") or obj.get("tg_username")
        if _is_real_personal_tg_handle(cand):
            cnt += 1
    return cnt


def _is_real_personal_tg_handle(raw_username: Any) -> bool:
    u = str(raw_username or "").strip().lstrip("@")
    if not u or len(u) < 4 or len(u) > 32:
        return False
    low = u.lower()
    if low in _NOISE_TG_HANDLES or low.endswith("bot") or low.endswith("_bot"):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u))


def _direct_x_dedup_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    em = email_of_text(row.get("work_email") or row.get("email") or "")
    if em:
        keys.append(f"email:{em.casefold()}")
    fx = x_profile_url(row)
    if fx:
        keys.append(f"x:{fx.casefold()}")
    fli = linkedin_of(row)
    if fli:
        keys.append(f"li:{fli.casefold()}")
    purl = str(row.get("project_url") or row.get("website") or row.get("site") or "").strip()
    if purl:
        host = (urllib.parse.urlparse(purl if "://" in purl else f"https://{purl}").hostname or "").lower().removeprefix("www.")
        if host and "." in host:
            keys.append(f"dom:{host}")
    return keys


def split_tg_and_direct_x_leads(final_jsonl: Path, direct_x_jsonl: Path) -> dict[str, int]:
    """Keep TG/WA leads (icp_score >= 80) in final_jsonl; move Email/X/LinkedIn-only leads to direct_x_leads.jsonl."""
    if not final_jsonl.is_file() or final_jsonl.stat().st_size == 0:
        if final_jsonl.is_file():
            final_jsonl.unlink(missing_ok=True)
        return {"tg_wa_kept": 0, "direct_x_added": 0, "dropped": 0}

    existing_dx_keys: set[str] = set()
    if direct_x_jsonl.is_file():
        for line in direct_x_jsonl.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                prev_obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(prev_obj, dict):
                existing_dx_keys.update(_direct_x_dedup_keys(prev_obj))

    tg_wa_rows: list[dict[str, Any]] = []
    new_direct_x_rows: list[dict[str, Any]] = []
    dropped = 0

    for raw_line in final_jsonl.read_text(encoding="utf-8").splitlines():
        s = raw_line.strip()
        if not s:
            continue
        try:
            item = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        row = dict(item)
        raw_score = row.get("icp_score")
        try:
            score = 85 if raw_score is None else int(round(float(raw_score)))
        except (TypeError, ValueError):
            score = 0
        if score < 80 or str(row.get("monetization") or "").strip().lower() == "none":
            dropped += 1
            continue
        if sniper_reject_reason(row):
            dropped += 1
            continue

        site_val = str(row.get("project_url") or row.get("website") or row.get("site") or "").strip()
        clean_n, clean_p = sanitize_person_and_company(
            row.get("name"),
            row.get("project") or row.get("company"),
            site=site_val,
            bio=row.get("bio") or row.get("what_they_do") or row.get("their_angle"),
        )
        if row.get("name") is not None and clean_n:
            row["name"] = clean_n
        if clean_p:
            row["project"] = clean_p
            if "company" in row:
                row["company"] = clean_p
        row["icp_score"] = score

        raw_tg = row.get("username") or row.get("telegram_username") or row.get("tg_username") or ""
        has_real_tg = _is_real_personal_tg_handle(raw_tg)
        wa = whatsapp_url(row.get("whatsapp") or "") or (
            whatsapp_url(row.get("phone") or "") if str(row.get("provider") or "").lower() == "whatsapp" else ""
        )
        if has_real_tg or wa:
            if has_real_tg:
                row["username"] = str(raw_tg).strip().lstrip("@")
                row["provider"] = "telegram"
            elif wa:
                wa_digits = re.sub(r"\D", "", wa)
                row["whatsapp"] = f"https://wa.me/{wa_digits}"
                row["phone"] = f"+{wa_digits}"
                row["provider"] = "whatsapp"
            tg_wa_rows.append(row)
            continue

        # No personal TG and no WhatsApp -> check Direct/X channels
        row["username"] = ""
        row.pop("telegram_username", None)
        row.pop("tg_username", None)
        em = email_of_text(row.get("work_email") or row.get("email") or "")
        fx = x_profile_url(row)
        fli = linkedin_of(row)
        if em or fx or fli:
            comp_val = str(row.get("company") or row.get("project") or "").strip()
            angle_val = str(row.get("their_angle") or row.get("what_they_do") or "").strip()
            if comp_val:
                row["company"] = comp_val
                row["project"] = comp_val
            if site_val:
                row["url"] = site_val
                row["project_url"] = site_val
            if angle_val:
                row["their_angle"] = angle_val
                row["what_they_do"] = angle_val
            if em:
                row["work_email"] = em
                row["email"] = em
            if fx:
                row["founder_x"] = fx
                row["x_url"] = fx
            if fli:
                row["founder_linkedin"] = fli
                row["linkedin_url"] = fli
            row["route_channel"] = "x" if (fx and not em and not fli) else "direct"
            d_keys = _direct_x_dedup_keys(row)
            if d_keys and any(k in existing_dx_keys for k in d_keys):
                continue
            existing_dx_keys.update(d_keys)
            new_direct_x_rows.append(row)
        else:
            dropped += 1

    if new_direct_x_rows:
        direct_x_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with direct_x_jsonl.open("a", encoding="utf-8") as dxfh:
            for dx_row in new_direct_x_rows:
                dxfh.write(json.dumps(dx_row, ensure_ascii=False) + "\n")

    if tg_wa_rows:
        final_jsonl.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in tg_wa_rows) + "\n",
            encoding="utf-8",
        )
    else:
        final_jsonl.unlink(missing_ok=True)

    return {
        "tg_wa_kept": len(tg_wa_rows),
        "direct_x_added": len(new_direct_x_rows),
        "dropped": dropped,
    }


def _norm_quarantine_token(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        return ""
    for prefix in ("tg_groups:", "vacancies:", "chat:", "tg:", "group:", "channel:"):
        if val.lower().startswith(prefix):
            val = val[len(prefix) :].strip()
    cleaned = _clean_chat_list([val])
    if cleaned:
        return cleaned[0].lower()
    return val.lstrip("@").strip("/").lower()


def load_quarantined_sources(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None:
        return set()
    try:
        row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'quarantined_leadgen_sources'"
        ).fetchone()
    except sqlite3.Error:
        return set()
    if not row or not row["value"]:
        return set()

    raw_val = str(row["value"]).strip()
    if not raw_val:
        return set()

    extracted: list[Any] = []
    try:
        parsed = json.loads(raw_val)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    for k in ("source", "chat", "group", "name", "username", "id"):
                        if item.get(k):
                            extracted.append(item[k])
                elif item is not None:
                    extracted.append(item)
        elif isinstance(parsed, dict):
            if isinstance(parsed.get("sources"), list):
                extracted.extend(parsed["sources"])
            elif isinstance(parsed.get("items"), list):
                extracted.extend(parsed["items"])
            else:
                for k, v in parsed.items():
                    if v:
                        extracted.append(k)
        elif isinstance(parsed, str):
            extracted.extend(re.split(r"[\n,;]+", parsed))
    except json.JSONDecodeError:
        extracted.extend(re.split(r"[\n,;]+", raw_val))

    out: set[str] = set()
    for item in extracted:
        tok = _norm_quarantine_token(item)
        if tok:
            out.add(tok)
    return out


def apply_quarantined_sources(
    cfg: dict[str, Any], quarantined: set[str]
) -> dict[str, Any]:
    effective = copy.deepcopy(cfg)
    if not quarantined:
        return effective

    b2b = effective.setdefault("b2b", {})
    for list_key in ("tg_groups_old", "tg_groups_new", "tg_comment_channels"):
        raw_list = b2b.get(list_key) or []
        b2b[list_key] = [
            g for g in raw_list if _norm_quarantine_token(g) not in quarantined
        ]

    vac = effective.setdefault("vacancies", {})
    raw_extra = vac.get("extra_chats") or []
    vac["extra_chats"] = [
        g for g in raw_extra if _norm_quarantine_token(g) not in quarantined
    ]
    return effective


def _session_path_variants(raw_path: str | Path) -> tuple[set[str], set[str]]:
    raw_str = str(raw_path or "").strip()
    if not raw_str:
        return set(), set()

    expanded = Path(raw_str).expanduser()
    resolved_base = resolve_session(str(expanded))
    with_ext = (
        resolved_base
        if resolved_base.suffix.lower() == ".session"
        else Path(f"{resolved_base}.session")
    )
    without_ext = (
        resolved_base.with_suffix("")
        if resolved_base.suffix.lower() == ".session"
        else resolved_base
    )

    path_tokens: set[str] = set()
    name_tokens: set[str] = set()
    for p in (expanded, resolved_base, with_ext, without_ext):
        abs_s = os.path.abspath(str(p))
        real_s = os.path.realpath(str(p))
        path_tokens.add(abs_s)
        path_tokens.add(real_s)
        if p.name:
            name_tokens.add(p.name.lower())
        stem = p.stem if p.suffix.lower() == ".session" else p.name
        if stem:
            name_tokens.add(stem.lower())
            name_tokens.add(f"{stem.lower()}.session")
    return path_tokens, name_tokens


def _extract_sqlite_auth_key_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size < 100:
            return None
        with path.open("rb") as fh:
            magic = fh.read(16)
        if not magic.startswith(b"SQLite format 3\x00"):
            return None
        uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as sconn:
            row = sconn.execute(
                "SELECT auth_key FROM sessions WHERE auth_key IS NOT NULL LIMIT 1"
            ).fetchone()
        if row and row[0]:
            key_bytes = bytes(row[0])
            if len(key_bytes) == 256:
                return hashlib.sha256(key_bytes).hexdigest()
    except Exception:
        return None
    return None


def _extract_blob_auth_key_sha256(blob: Any) -> str | None:
    if not blob:
        return None
    try:
        raw = bytes(blob)
    except Exception:
        return None
    if len(raw) < 100 or not raw.startswith(b"SQLite format 3\x00"):
        return None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tf:
            tf.write(raw)
            tmp_path = Path(tf.name)
        return _extract_sqlite_auth_key_sha256(tmp_path)
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _extract_string_session_auth_key_sha256(session_string: Any) -> str | None:
    s = str(session_string or "").strip()
    if len(s) < 300:
        return None
    try:
        from telethon.sessions import StringSession

        sess = StringSession(s)
        ak = getattr(sess, "auth_key", None)
        key = getattr(ak, "key", None) if ak is not None else None
        if key and len(bytes(key)) == 256:
            return hashlib.sha256(bytes(key)).hexdigest()
    except Exception:
        pass
    # Telethon StringSession stores a 1-char version header ('1') followed by base64
    # ending with the 256-byte MTProto auth_key (>B4sH256s or >B16sH256s).
    for payload in (s[1:], s):
        try:
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            if len(decoded) >= 256:
                key_bytes = decoded[-256:]
                if len(key_bytes) == 256:
                    return hashlib.sha256(key_bytes).hexdigest()
        except Exception:
            continue
    return None


def _collect_outreach_session_dirs(conn: sqlite3.Connection) -> list[Path]:
    dirs: list[Path] = [ROOT / "data" / "sessions"]
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            db_file = str(row[2] or "").strip()
            if db_file:
                db_parent = Path(db_file).resolve().parent
                dirs.append(db_parent / "sessions")
                dirs.append(db_parent / "data" / "sessions")
    except Exception:
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        key = os.path.realpath(str(d))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def validate_isolated_scraper_session(
    conn: sqlite3.Connection,
    session_path: str | Path | None = None,
    *,
    scraper_sessions_dir: str | Path | None = None,
) -> Path | None:
    candidate_raw = str(session_path or "").strip()
    if not candidate_raw:
        candidate_raw = os.environ.get("SCRAPER_TG_SESSION", "").strip()

    if not candidate_raw:
        sess_dir = Path(
            str(
                scraper_sessions_dir
                or os.environ.get("SCRAPER_SESSIONS_DIR")
                or DEFAULT_SCRAPER_SESSIONS_DIR
            ).strip()
        ).expanduser()
        if sess_dir.is_dir():
            found = sorted(sess_dir.glob("*.session"))
            if found:
                candidate_raw = str(found[0])

    if not candidate_raw:
        return None

    cand_paths, cand_names = _session_path_variants(candidate_raw)
    acc_rows = conn.execute(
        "SELECT id, phone, session_path, telegram_username, session_blob, session_string FROM accounts"
    ).fetchall()
    for row in acc_rows:
        acc_sess = str(row["session_path"] or "").strip()
        if not acc_sess:
            continue
        acc_paths, acc_names = _session_path_variants(acc_sess)
        if (cand_paths & acc_paths) or (cand_names & acc_names):
            raise ValueError(
                f"SCRAPER_TG_SESSION ({candidate_raw}) совпадает с боевой сессией рассылки "
                f"(account #{row['id']}: {acc_sess}). Использование боевого аккаунта запрещено!"
            )

    cand_resolved = resolve_session(str(Path(candidate_raw).expanduser()))
    if cand_resolved.suffix.lower() != ".session":
        with_suffix = Path(f"{cand_resolved}.session")
        if with_suffix.exists():
            cand_resolved = with_suffix

    # Fingerprint guard: inspect 256-byte MTProto auth_key in read-only mode and compare SHA-256
    # against (a) accounts.session_blob, (b) accounts.session_string, and (c) data/sessions/*.session on disk.
    cand_auth_sha = _extract_sqlite_auth_key_sha256(cand_resolved)
    if cand_auth_sha is not None:
        live_auth_shas: set[str] = set()
        for row in acc_rows:
            blob_sha = _extract_blob_auth_key_sha256(row["session_blob"])
            if blob_sha:
                live_auth_shas.add(blob_sha)
            str_sha = _extract_string_session_auth_key_sha256(row["session_string"])
            if str_sha:
                live_auth_shas.add(str_sha)
            acc_sess = str(row["session_path"] or "").strip()
            if acc_sess:
                acc_p = resolve_session(str(Path(acc_sess).expanduser()))
                if acc_p.suffix.lower() != ".session":
                    acc_p_ext = Path(f"{acc_p}.session")
                    if acc_p_ext.is_file():
                        acc_p = acc_p_ext
                file_sha = _extract_sqlite_auth_key_sha256(acc_p)
                if file_sha:
                    live_auth_shas.add(file_sha)

        for sess_dir in _collect_outreach_session_dirs(conn):
            if not sess_dir.is_dir():
                continue
            for disk_sess in sorted(sess_dir.glob("*.session")):
                disk_sha = _extract_sqlite_auth_key_sha256(disk_sess)
                if disk_sha:
                    live_auth_shas.add(disk_sha)

        if cand_auth_sha in live_auth_shas:
            raise ValueError("Scraper session shares MTProto auth_key with outreach account")

    return cand_resolved


def _find_campaign_by_name(
    conn: sqlite3.Connection, name: str
) -> dict[str, Any] | None:
    clean = str(name or "").strip().lower()
    if not clean:
        return None
    rows = conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    for r in rows:
        d = as_dict(r)
        if str(d.get("name") or "").strip().lower() == clean:
            return d
    return None


def _count_pending(conn: sqlite3.Connection, campaign_id: int | None) -> int:
    if campaign_id is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM campaign_recipients
        WHERE campaign_id = ?
          AND COALESCE(current_step, 0) = 0
          AND status = 'pending'
        """,
        (int(campaign_id),),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _parse_iso_utc(raw: str | None) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        norm = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        return None


def _emit_critical_low_water_alert(
    conn: sqlite3.Connection,
    *,
    campaign: dict[str, Any],
    pending_count: int,
    critical_low_mark: int,
    has_scraper_session: bool,
    has_llm_key: bool,
    missing_treg_cli: bool = False,
    digest_opener: Any = None,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S")

    reasons: list[str] = []
    reason_labels: list[str] = []
    if not has_scraper_session:
        reasons.append("missing_scraper_tg_session")
        reason_labels.append("нет SCRAPER_TG_SESSION")
    if not has_llm_key:
        reasons.append("missing_relayfast_api_key")
        reason_labels.append("нет RELAYFAST_API_KEY")
    if missing_treg_cli:
        reasons.append("missing_treg_cli")
        reason_labels.append("нет treg CLI / TREG_API_KEY")
    if not reasons:
        reasons.append("sources_exhausted")
        reason_labels.append("источники исчерпаны")

    reason_text = ", ".join(reason_labels)
    prev_sent_at = ""
    try:
        prev_row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'leadgen_low_water_alert'"
        ).fetchone()
        if prev_row and prev_row["value"]:
            prev_obj = json.loads(prev_row["value"])
            if isinstance(prev_obj, dict):
                prev_sent_at = str(prev_obj.get("last_alert_sent_at") or "").strip()
    except Exception:
        prev_sent_at = ""

    prev_dt = _parse_iso_utc(prev_sent_at)
    within_24h = (
        prev_dt is not None and (now_dt - prev_dt).total_seconds() < 86400
    )

    alert_sent = False
    alert_error: str | None = None
    last_alert_sent_at = prev_sent_at

    if not within_24h:
        alert_body = (
            f"⚠️ Leadgen Low-Water Alert: кампания «{campaign['name']}» (#{campaign['id']})\n"
            f"Остаток pending (Step 0): {pending_count} (порог critical_low_mark: {critical_low_mark}).\n"
            f"Причина: {reason_text}."
        )
        try:
            token, chat_id = app_digest._operator(conn)
            payload = {"chat_id": str(chat_id).strip(), "text": alert_body}
            app_digest._post(
                app_digest.SEND_URL.format(token=token), payload, digest_opener
            )
            alert_sent = True
            last_alert_sent_at = now_iso
        except Exception as exc:
            alert_error = str(exc)

    diagnostic = {
        "campaign_id": int(campaign["id"]),
        "campaign_name": str(campaign["name"]),
        "pending": int(pending_count),
        "critical_low_mark": int(critical_low_mark),
        "reason": reasons[0],
        "reasons": reasons,
        "reason_text": reason_text,
        "alert_sent": alert_sent,
        "alert_skipped": "already_sent_within_24h" if within_24h else None,
        "alert_error": alert_error,
        "last_alert_sent_at": last_alert_sent_at or None,
        "updated_at": now_iso,
    }
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_low_water_alert', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(diagnostic, ensure_ascii=False), now_iso),
    )
    conn.commit()
    return diagnostic


def _merge_discovered_jsonl_into_raw(
    source_files: list[Path], raw_jsonl: Path
) -> int:
    added = 0
    raw_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not raw_jsonl.exists():
        raw_jsonl.write_text("", encoding="utf-8")
    with raw_jsonl.open("a", encoding="utf-8") as out_fh:
        for path in source_files:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    row = dict(row)
                    if not row.get("telegram_username") and (
                        row.get("tg_username") or row.get("site_tg")
                    ):
                        row["telegram_username"] = row.get("tg_username") or row.get("site_tg")
                    if not row.get("project_url") and row.get("site"):
                        row["project_url"] = row.get("site")
                    if not row.get("project") and row.get("company"):
                        row["project"] = row.get("company")
                    orig_src = str(row.get("lead_source") or row.get("source") or "").strip()
                    if not orig_src and row.get("group"):
                        orig_src = "tg_groups"
                else:
                    orig_src = ""
                admitted = admit_discovered_row(row)
                if not admitted:
                    continue
                if orig_src:
                    if not str(admitted.get("source") or "").strip():
                        admitted["source"] = orig_src
                    if not str(admitted.get("lead_source") or "").strip():
                        admitted["lead_source"] = str(admitted.get("source") or orig_src)
                admitted.setdefault("icp_match", True)
                out_fh.write(json.dumps(admitted, ensure_ascii=False) + "\n")
                added += 1
    return added


_PROTECTED_JSONL_NAMES = frozenset(
    {
        "master_ids.jsonl",
        "direct_x_leads.jsonl",
        "ofm_review_leads.jsonl",
        "quarantined_unqualified.jsonl",
        "pending_qualify_queue.jsonl",
        "leadgen_run_history.jsonl",
    }
)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_leadgen_state_jsonl(
    state_dir: Path,
    *,
    max_age_days: int = 14,
    max_total_bytes: int = 20 * 1024 * 1024,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Enforce retention (<=14d ephemeral runs, <=20MB total *.jsonl) without wiping active SSOT files."""
    if not state_dir.is_dir():
        return {"deleted_files": [], "trimmed_files": [], "total_bytes_after": 0}
    now_sec = float(time.time() if now_ts is None else now_ts)
    cutoff_sec = now_sec - max(1, int(max_age_days)) * 86400.0
    deleted: list[str] = []
    trimmed: list[str] = []

    def _all_jsonl_stats() -> list[tuple[float, int, Path]]:
        items: list[tuple[float, int, Path]] = []
        for p in state_dir.glob("*.jsonl*"):
            if not p.is_file() or ".jsonl" not in p.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((float(st.st_mtime), int(st.st_size), p))
        return items

    # 1) Remove ephemeral staging/run .jsonl files older than max_age_days
    for mtime, _size, p in _all_jsonl_stats():
        if p.name in _PROTECTED_JSONL_NAMES:
            continue
        if mtime < cutoff_sec:
            try:
                p.unlink(missing_ok=True)
                deleted.append(p.name)
            except OSError:
                pass

    # 2) Enforce total <= max_total_bytes by evicting oldest ephemeral artifacts first
    stats = _all_jsonl_stats()
    total_bytes = sum(sz for _, sz, _ in stats)
    if total_bytes > max_total_bytes:
        ephemeral = sorted(
            [(mt, sz, p) for mt, sz, p in stats if p.name not in _PROTECTED_JSONL_NAMES],
            key=lambda x: x[0],
        )
        for _mt, sz, p in ephemeral:
            if total_bytes <= max_total_bytes:
                break
            try:
                p.unlink(missing_ok=True)
                deleted.append(p.name)
                total_bytes = max(0, total_bytes - sz)
            except OSError:
                pass

    # 3) If protected quarantine/pending logs still push over budget, trim oldest lines only
    if total_bytes > max_total_bytes:
        for trim_name in ("quarantined_unqualified.jsonl", "pending_qualify_queue.jsonl", "leadgen_run_history.jsonl"):
            if total_bytes <= max_total_bytes:
                break
            tp = state_dir / trim_name
            if not tp.is_file():
                continue
            try:
                old_sz = tp.stat().st_size
                lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
                # Keep newest half (or newest lines within leftover budget)
                keep_from = max(len(lines) // 2, len(lines) - 200)
                kept = lines[keep_from:]
                new_text = "\n".join(kept) + ("\n" if kept else "")
                tmp = tp.with_suffix(".jsonl.trim_tmp")
                tmp.write_text(new_text, encoding="utf-8")
                tmp.replace(tp)
                new_sz = tp.stat().st_size
                total_bytes = max(0, total_bytes - max(0, old_sz - new_sz))
                trimmed.append(trim_name)
            except OSError:
                pass

    final_total = sum(sz for _, sz, _ in _all_jsonl_stats())
    return {"deleted_files": deleted, "trimmed_files": trimmed, "total_bytes_after": final_total}


def _update_run_history_and_detect(
    state_dir: Path,
    run_entry: dict[str, Any],
    *,
    max_history: int = 30,
) -> tuple[int, list[dict[str, Any]]]:
    hist_path = state_dir / "leadgen_run_history.jsonl"
    prev_entries: list[dict[str, Any]] = []
    if hist_path.is_file():
        for ln in hist_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    prev_entries.append(obj)
            except json.JSONDecodeError:
                continue

    entries = (prev_entries + [run_entry])[-max_history:]
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = hist_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )
        tmp.replace(hist_path)
    except OSError:
        pass

    # Count trailing streak of runs with tg_bridged_count == 0
    zero_streak = 0
    for item in reversed(entries):
        try:
            cnt = int(item.get("tg_bridged_count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt == 0:
            zero_streak += 1
        else:
            break

    # Detect non-empty per-source JSONLs whose SHA-256 hash matches the previous run
    repeats: list[dict[str, Any]] = []
    cur_hashes = run_entry.get("source_hashes") or {}
    if isinstance(cur_hashes, dict) and len(entries) >= 2:
        for src, sha in cur_hashes.items():
            if not sha:
                continue
            streak = 1
            for prev in reversed(entries[:-1]):
                prev_hashes = prev.get("source_hashes") or {}
                if isinstance(prev_hashes, dict) and prev_hashes.get(src) == sha:
                    streak += 1
                else:
                    break
            if streak >= 2:
                repeats.append({"source": src, "sha256": sha, "repeat_runs": streak})

    return zero_streak, repeats


def _enforce_rule5_quarantine_on_new_recipients(
    conn: sqlite3.Connection,
    *,
    campaign: dict[str, Any] | None,
    pre_existing_ids: set[int],
    stage_errors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[int]]:
    if campaign is None:
        return None, []
    camp_id = int(campaign["id"])
    camp_name = str(campaign["name"])
    current_ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM campaign_recipients WHERE campaign_id = ?", (camp_id,)
        ).fetchall()
    }
    newly_inserted_ids = current_ids - pre_existing_ids
    if not newly_inserted_ids:
        return None, []

    preview = preview_outreach_batch(conn, camp_id, strict=False)
    violations = list(preview.get("violations") or [])
    bad_new_ids: set[int] = set()
    has_campaign_level_violation = False

    if not preview.get("ok", True) or int(preview.get("forbidden_pattern_count") or 0) > 0:
        for v in violations:
            rid = v.get("recipient_id")
            if rid is not None:
                rid_int = int(rid)
                if rid_int in newly_inserted_ids:
                    bad_new_ids.add(rid_int)
            elif v.get("code") not in ("no_fresh_untouched_leads", "legacy_1300_base_only"):
                has_campaign_level_violation = True
        if has_campaign_level_violation:
            bad_new_ids |= newly_inserted_ids

    # Also inspect raw persisted copy on newly inserted rows in case render-time scrubbing masked legacy CreatorOS
    legacy_brand_re = re.compile(r"\bCreator\s*OS\b|creatoros", re.IGNORECASE)
    placeholders = ",".join("?" for _ in newly_inserted_ids)
    raw_new_rows = conn.execute(
        f"SELECT id, step1_text, step2_text, step3_text, hook FROM campaign_recipients WHERE id IN ({placeholders})",
        tuple(sorted(newly_inserted_ids)),
    ).fetchall()
    for r in raw_new_rows:
        rid_int = int(r["id"])
        combined_raw = "\n".join(
            str(r[col] or "") for col in ("step1_text", "step2_text", "step3_text", "hook")
        )
        if legacy_brand_re.search(combined_raw):
            bad_new_ids.add(rid_int)
            violations.append(
                {
                    "code": "legacy_brand_creatoros",
                    "recipient_id": rid_int,
                    "message": f"В сыром тексте получателя #{rid_int} обнаружен устаревший бренд CreatorOS",
                }
            )

    if bad_new_ids:
        quarantined_ids = sorted(bad_new_ids)
        now_q = utc_now()
        conn.executemany(
            """
            UPDATE campaign_recipients
            SET status = 'completed',
                error_text = 'Карантин Rule 5 Dry-Run',
                updated_at = ?
            WHERE id = ?
            """,
            [(now_q, rid) for rid in quarantined_ids],
        )
        conn.commit()
        err_msg = "; ".join(str(v.get("message") or v.get("code") or "") for v in violations)
        logger.error(
            "Rule 5 Dry-Run quarantined %d newly inserted recipient(s) in campaign %s (#%d): %s",
            len(quarantined_ids),
            camp_name,
            camp_id,
            err_msg,
        )
        stage_errors.append(
            {
                "stage": "rule5_dry_run_quarantine",
                "campaign_id": camp_id,
                "campaign_name": camp_name,
                "quarantined_recipient_ids": quarantined_ids,
                "error": err_msg or "rule5_violation",
                "at": now_q,
            }
        )
        if _count_pending(conn, camp_id) > 0:
            preview = preview_outreach_batch(conn, camp_id, strict=False)
        return preview, quarantined_ids

    return preview, []


def run_autonomous_replenish(
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., Any] = subprocess.run,
    low_water_mark: int = 50,
    vac_low_water_mark: int = 10,
    critical_low_mark: int = 15,
    stage_timeout: int = 300,
    scraper_session: str | Path | None = None,
    digest_opener: Any = None,
) -> dict[str, Any]:
    cfg = get_leadgen_config(conn)
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "disabled_in_config"}

    # 1. Hard session guard (fails fast with ValueError if colliding with any live outreach session)
    validated_session = validate_isolated_scraper_session(conn, scraper_session)
    isolated_session = (
        validated_session
        if (validated_session is not None and validated_session.is_file())
        else None
    )
    has_llm_key = bool(
        (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    )

    # 2. Subtract quarantined sources
    quarantined = load_quarantined_sources(conn)
    eff_cfg = apply_quarantined_sources(cfg, quarantined)
    b2b_cfg = eff_cfg["b2b"]
    vac_cfg = eff_cfg["vacancies"]

    b2b_name = resolve_target_b2b_tg_campaign_name(
        conn, b2b_cfg.get("target_campaign_tg"), fallback_default="Leadgen Auto (TG)"
    )
    vac_name = str(vac_cfg.get("campaign_name") or "TG Вакансии").strip() or "TG Вакансии"

    b2b_camp = _find_campaign_by_name(conn, b2b_name)
    vac_camp = _find_campaign_by_name(conn, vac_name)

    pending_b2b_before = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
    pending_vac_before = _count_pending(conn, vac_camp["id"] if vac_camp else None)
    pending_b2b = pending_b2b_before
    pending_vac = pending_vac_before

    b2b_active = bool(b2b_cfg.get("enabled", True)) and (
        b2b_camp is None or str(b2b_camp.get("status") or "") != "completed"
    )
    vac_active = bool(vac_cfg.get("enabled", False)) and (
        vac_camp is None or str(vac_camp.get("status") or "") != "completed"
    )

    needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
    needs_vac = vac_active and pending_vac < int(vac_low_water_mark)

    ingested_total = 0
    direct_x_routed = 0
    stages_run: list[str] = []
    stage_errors: list[dict[str, Any]] = []
    replenish_reasons: list[str] = []
    rule5_previews: dict[str, Any] = {}
    rule5_quarantined: dict[str, list[int]] = {}
    per_source_counts: dict[str, int] = {}
    source_hashes: dict[str, str] = {}
    tg_bridged_count = 0
    treg_ready = check_treg_ready()
    monid_ready = check_monid_ready()

    if needs_b2b or needs_vac:
        pre_existing_recipient_ids: set[int] = {
            int(r[0]) for r in conn.execute("SELECT id FROM campaign_recipients").fetchall()
        }

        # Step 1: Ingest any already-prepared local .jsonl / job_replies without mutating limits
        push_1 = push_discovered_now(conn)
        ingested_total += int(push_1.get("ingested") or 0)

        b2b_camp = _find_campaign_by_name(conn, b2b_name)
        vac_camp = _find_campaign_by_name(conn, vac_name)
        pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
        pending_vac = _count_pending(conn, vac_camp["id"] if vac_camp else None)

        still_needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
        still_needs_vac = vac_active and pending_vac < int(vac_low_water_mark)

        # Step 2: Run server-available discovery stages via injectable runner
        if still_needs_b2b or still_needs_vac:
            state_dir = _resolve_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            eff_cfg_path = state_dir / "effective_leadgen_config.json"
            eff_cfg_path.write_text(
                json.dumps(eff_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            sub_env = dict(os.environ)
            sub_env["LEADGEN_STATE_DIR"] = str(state_dir)
            sub_env["LEADGEN_CONFIG_PATH"] = str(eff_cfg_path)
            if isolated_session is not None:
                sub_env["TG_SESSION"] = str(isolated_session)
                sub_env["TG_JOBS_SESSION"] = str(isolated_session)

            def _exec_stage(stage_name: str, cmd: list[str]) -> bool:
                stages_run.append(stage_name)
                try:
                    proc = runner(
                        cmd,
                        env=sub_env,
                        timeout=stage_timeout,
                        cwd=str(ROOT),
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    rc = int(getattr(proc, "returncode", 0) or 0)
                    if rc != 0:
                        stderr_snip = str(getattr(proc, "stderr", "") or "").strip()[-500:]
                        stage_errors.append(
                            {
                                "stage": stage_name,
                                "returncode": rc,
                                "error": stderr_snip or f"exit_code_{rc}",
                                "at": utc_now(),
                            }
                        )
                        logger.error(
                            "Leadgen stage %s failed (rc=%d): %s", stage_name, rc, stderr_snip
                        )
                        return False
                    return True
                except subprocess.TimeoutExpired as exc:
                    stage_errors.append(
                        {
                            "stage": stage_name,
                            "returncode": -1,
                            "timeout": stage_timeout,
                            "error": f"timeout_after_{stage_timeout}s: {exc}",
                            "at": utc_now(),
                        }
                    )
                    logger.error(
                        "Leadgen stage %s timed out after %ds", stage_name, stage_timeout
                    )
                    return False
                except Exception as exc:
                    stage_errors.append(
                        {
                            "stage": stage_name,
                            "returncode": -1,
                            "error": str(exc),
                            "at": utc_now(),
                        }
                    )
                    logger.error("Leadgen stage %s crashed: %s", stage_name, exc)
                    return False

            py_bin = sys.executable
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if still_needs_b2b:
                raw_jsonl = state_dir / f"raw_{ts}.jsonl"
                master_jsonl = state_dir / "master_ids.jsonl"
                buyer_public_out = state_dir / f"buyer_public_{ts}.jsonl"
                catalogs_out = state_dir / f"buyer_catalogs_{ts}.jsonl"
                high_yield_out = state_dir / f"high_yield_{ts}.jsonl"
                meta_ads_out = state_dir / f"meta_ads_{ts}.jsonl"
                social_out = state_dir / f"social_{ts}.jsonl"
                channel_graph_out = state_dir / f"channel_graph_{ts}.jsonl"
                ad_spy_out = state_dir / f"tg_ad_spy_{ts}.jsonl"
                external_out = state_dir / f"external_{ts}.jsonl"
                yc_ph_out = state_dir / f"yc_ph_{ts}.jsonl"
                fb_groups_out = state_dir / f"fb_groups_{ts}.jsonl"
                b2b_tg_groups_out = state_dir / f"b2b_tg_groups_{ts}.jsonl"
                ofm_review_path = state_dir / "ofm_review_leads.jsonl"
                pre_ofm_count = _count_jsonl_lines(ofm_review_path) if ofm_review_path.is_file() else 0
                sources_cfg = b2b_cfg.get("sources") or {}
                limits_cfg = b2b_cfg.get("limits") or {}

                # Web / API discovery stages (no TG session needed)
                if sources_cfg.get("buyer_catalogs", True) and "buyer_catalogs" not in quarantined:
                    if "buyer_public" not in quarantined:
                        _exec_stage(
                            "buyer_public",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "buyer_public.py"),
                                "--output",
                                str(buyer_public_out),
                            ],
                        )
                    _exec_stage(
                        "buyer_catalogs",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "buyer_catalogs.py"),
                            "--output",
                            str(catalogs_out),
                            "--seen",
                            str(state_dir / "buyer_catalogs_seen.json"),
                        ],
                    )

                enabled_veins: list[str] = []
                if (
                    sources_cfg.get("whop_content_rewards", False)
                    and "whop_content_rewards" not in quarantined
                ):
                    enabled_veins.append("whop")
                if sources_cfg.get("tiktok_ads", False) and "tiktok_ads" not in quarantined:
                    enabled_veins.append("tiktok_ads")
                if sources_cfg.get("tiktok_shop", False) and "tiktok_shop" not in quarantined:
                    enabled_veins.append("tiktok_shop")
                if sources_cfg.get("app_stores", False) and "app_stores" not in quarantined:
                    enabled_veins.append("app_stores")
                if sources_cfg.get("ats_hiring", False) and "ats_hiring" not in quarantined:
                    enabled_veins.append("ats_hiring")
                if sources_cfg.get("ph_detailed", False) and "ph_detailed" not in quarantined:
                    enabled_veins.append("ph_detailed")
                if sources_cfg.get("d2c_whatsapp", False) and "d2c_whatsapp" not in quarantined:
                    enabled_veins.append("d2c_whatsapp")
                if enabled_veins and "high_yield_veins" not in quarantined:
                    if treg_ready or monid_ready or runner is not subprocess.run:
                        _exec_stage(
                            "high_yield_veins",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "high_yield_veins.py"),
                                "--output",
                                str(high_yield_out),
                                "--seen",
                                str(state_dir / "high_yield_seen.json"),
                                "--veins",
                                ",".join(enabled_veins),
                                "--max-runtime-sec",
                                str(min(200, max(60, int(stage_timeout) - 60))),
                            ],
                        )
                    elif "missing_treg_cli" not in replenish_reasons:
                        replenish_reasons.append("missing_treg_cli")

                if (
                    sources_cfg.get("facebook_groups", False)
                    and "facebook_groups" not in quarantined
                    and (treg_ready or runner is not subprocess.run)
                ):
                    fb_cmd = [
                        py_bin,
                        str(ROOT / "leadgen" / "ofm_and_groups.py"),
                        "--mode",
                        "fb_groups",
                        "--output",
                        str(fb_groups_out),
                        "--ofm-out",
                        str(ofm_review_path),
                    ]
                    fb_urls = [str(u).strip() for u in (b2b_cfg.get("fb_group_urls") or []) if str(u).strip()]
                    if fb_urls:
                        fb_cmd.extend(["--fb-urls", ",".join(fb_urls), "--max-groups", str(max(5, len(fb_urls)))])
                    _exec_stage("facebook_groups", fb_cmd)

                if sources_cfg.get("meta_ads", True) and "meta_ads" not in quarantined:
                    if treg_ready or runner is not subprocess.run:
                        _exec_stage(
                            "meta_ad_leads",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "meta_ad_leads.py"),
                                "--output",
                                str(meta_ads_out),
                                "--seen",
                                str(state_dir / "meta_ads_seen.json"),
                            ],
                        )
                    elif "missing_treg_cli" not in replenish_reasons:
                        replenish_reasons.append("missing_treg_cli")
                if sources_cfg.get("social_x_reddit", True) and "social_x_reddit" not in quarantined:
                    if treg_ready:
                        _exec_stage(
                            "treg_social_leads",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "treg_social_leads.py"),
                                "--output",
                                str(social_out),
                                "--seen",
                                str(state_dir / "treg_social_seen.json"),
                            ],
                        )
                    elif runner is subprocess.run and "missing_treg_cli" not in replenish_reasons:
                        replenish_reasons.append("missing_treg_cli")
                if (
                    sources_cfg.get("yc_ph", True)
                    and "yc_ph" not in quarantined
                    and "yc_ph_leads" not in quarantined
                ):
                    yc_lim = max(1, min(12, int(limits_cfg.get("yc_limit") or 12)))
                    ph_lim = max(0, min(10, int(limits_cfg.get("ph_limit") or 0)))
                    yc_cmd = [
                        py_bin,
                        str(ROOT / "leadgen" / "yc_ph_leads.py"),
                        "--yc-limit",
                        str(yc_lim),
                        "--ph-limit",
                        str(ph_lim),
                        "--betalist-limit",
                        "5",
                        "--microlaunch-limit",
                        "5",
                        "--uneed-limit",
                        "5",
                        "--indiehackers-limit",
                        "5",
                        "--no-llm",
                        "--no-sheet",
                        "--persist-seen",
                    ]
                    if isolated_session is None:
                        yc_cmd.append("--no-tg")
                    tmp_yc = Path("/tmp/yc_ph_leads.jsonl")
                    prev_yc_mtime = tmp_yc.stat().st_mtime_ns if tmp_yc.is_file() else -1
                    if _exec_stage("yc_ph_leads", yc_cmd) and tmp_yc.is_file():
                        cur_yc_st = tmp_yc.stat()
                        if cur_yc_st.st_size > 0 and cur_yc_st.st_mtime_ns != prev_yc_mtime:
                            yc_ph_out.write_bytes(tmp_yc.read_bytes())

                ext_limit = min(10, max(0, int(limits_cfg.get("external_limit") or 10)))
                if ext_limit > 0 and "creator_external_leads" not in quarantined:
                    _exec_stage(
                        "creator_external_leads",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "creator_external_leads.py"),
                            "--output",
                            str(external_out),
                            "--limit",
                            str(ext_limit),
                            "--seen",
                            str(state_dir / "creator_external_seen.json"),
                            "--verify-telegram",
                            "--no-qualify",
                        ],
                    )

                # TG scrapers ONLY if valid isolated SCRAPER_TG_SESSION is available
                effective_groups: list[str] = []
                for g in [*(b2b_cfg.get("tg_groups_new") or []), *(b2b_cfg.get("tg_groups_old") or [])]:
                    if g not in effective_groups:
                        effective_groups.append(g)

                if (
                    isolated_session is not None
                    and sources_cfg.get("tg_groups", True)
                    and effective_groups
                ):
                    scrape_cmd = [
                        py_bin,
                        str(ROOT / "leadgen" / "tg_scrape_posters.py"),
                        "--session",
                        str(isolated_session),
                        "--groups",
                        ",".join(effective_groups),
                        "--mode",
                        "posters",
                        "--msg-limit",
                        str(min(400, max(50, int(limits_cfg.get("msg_limit") or 400)))),
                        "--delay",
                        "0.8",
                        "--max-runtime-sec",
                        "180",
                        "--output",
                        str(raw_jsonl),
                        "--append",
                    ]
                    if master_jsonl.is_file():
                        scrape_cmd.extend(["--exclude", str(master_jsonl)])
                    _exec_stage("tg_scrape_posters", scrape_cmd)

                    raw_b2b_new = b2b_cfg.get("tg_groups_new")
                    b2b_group_list = _clean_chat_list(
                        ["productcircle_chat", "solofounderschat", "saasfounders"]
                        if raw_b2b_new is None
                        else raw_b2b_new
                    )
                    if b2b_group_list:
                        _exec_stage(
                            "b2b_tg_groups",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "ofm_and_groups.py"),
                                "--mode",
                                "b2b_tg",
                                "--session",
                                str(isolated_session),
                                "--groups",
                                ",".join(b2b_group_list),
                                "--output",
                                str(b2b_tg_groups_out),
                                "--seen",
                                str(state_dir / "b2b_tg_groups_seen.json"),
                            ],
                        )

                if (
                    isolated_session is not None
                    and sources_cfg.get("ofm_groups", False)
                    and "ofm_groups" not in quarantined
                ):
                    ofm_group_list = _clean_chat_list(b2b_cfg.get("ofm_tg_groups") or [])
                    ofm_cmd = [
                        py_bin,
                        str(ROOT / "leadgen" / "ofm_and_groups.py"),
                        "--mode",
                        "ofm_tg",
                        "--session",
                        str(isolated_session),
                        "--ofm-out",
                        str(ofm_review_path),
                        "--seen",
                        str(state_dir / "ofm_seen.json"),
                    ]
                    if ofm_group_list:
                        ofm_cmd.extend(["--groups", ",".join(ofm_group_list)])
                    _exec_stage("ofm_groups", ofm_cmd)

                if (
                    isolated_session is not None
                    and sources_cfg.get("channel_graph", True)
                    and "channel_graph" not in quarantined
                ):
                    _exec_stage(
                        "tg_channel_graph",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_channel_graph.py"),
                            "--session",
                            str(isolated_session),
                            "--output",
                            str(channel_graph_out),
                            "--state",
                            str(state_dir / "tg_channel_graph_seen.json"),
                            "--max-runtime-sec",
                            str(min(180, max(60, int(stage_timeout) - 60))),
                        ],
                    )

                if (
                    isolated_session is not None
                    and sources_cfg.get("tg_ad_spy", True)
                    and "tg_ad_spy" not in quarantined
                ):
                    _exec_stage(
                        "tg_ad_spy",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_ad_spy.py"),
                            "--session",
                            str(isolated_session),
                            "--output",
                            str(ad_spy_out),
                            "--seen",
                            str(state_dir / "tg_ad_spy_seen.json"),
                            "--max-runtime-sec",
                            str(min(180, max(60, int(stage_timeout) - 60))),
                        ],
                    )

                per_source_counts["tg_groups"] = _count_jsonl_lines(raw_jsonl) + _count_jsonl_lines(b2b_tg_groups_out)
                per_source_counts["ofm_groups"] = max(
                    0, _count_jsonl_lines(ofm_review_path) - pre_ofm_count
                )
                for src_label, src_path in (
                    ("buyer_public", buyer_public_out),
                    ("buyer_catalogs", catalogs_out),
                    ("high_yield_veins", high_yield_out),
                    ("facebook_groups", fb_groups_out),
                    ("meta_ads", meta_ads_out),
                    ("social_x_reddit", social_out),
                    ("external", external_out),
                    ("yc_ph", yc_ph_out),
                    ("channel_graph", channel_graph_out),
                    ("tg_ad_spy", ad_spy_out),
                ):
                    per_source_counts[src_label] = _count_jsonl_lines(src_path)
                    if src_path.is_file() and src_path.stat().st_size > 0:
                        try:
                            source_hashes[src_label] = _file_sha256(src_path)
                        except OSError:
                            pass

                pending_qualify_queue = state_dir / "pending_qualify_queue.jsonl"
                llm_failed_file = state_dir / "llm_failed_batches.jsonl"
                _merge_discovered_jsonl_into_raw(
                    [
                        pending_qualify_queue,
                        buyer_public_out,
                        catalogs_out,
                        high_yield_out,
                        fb_groups_out,
                        b2b_tg_groups_out,
                        meta_ads_out,
                        social_out,
                        external_out,
                        yc_ph_out,
                        channel_graph_out,
                        ad_spy_out,
                    ],
                    raw_jsonl,
                )
                apply_master_merge(str(raw_jsonl), str(master_jsonl), commit_master=False)
                new_jsonl = Path(f"{raw_jsonl}.new")
                if not new_jsonl.exists():
                    new_jsonl.write_text("", encoding="utf-8")

                has_new_rows = new_jsonl.stat().st_size > 0
                enrich_ok = False
                qual_ok = False
                curate_ok = True
                if has_new_rows or runner is not subprocess.run:
                    enriched_jsonl = state_dir / f"raw_{ts}.enriched.jsonl"
                    enrich_ok = _exec_stage(
                        "tg_enrich_leads",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_enrich_leads.py"),
                            "--input",
                            str(new_jsonl),
                            "--output",
                            str(enriched_jsonl),
                            "--fetch",
                            "--skip-copy",
                        ],
                    )
                    if not enrich_ok:
                        if has_new_rows:
                            _write_pending_qualify_queue(
                                pending_qualify_queue,
                                _read_jsonl_dicts(new_jsonl),
                            )
                    else:
                        enrich_src = (
                            enriched_jsonl
                            if enriched_jsonl.is_file() and enriched_jsonl.stat().st_size > 0
                            else new_jsonl
                        )
                        if has_llm_key:
                            llm_failed_file.unlink(missing_ok=True)
                            qualified_jsonl = state_dir / f"raw_{ts}.qualified.jsonl"
                            curated_jsonl = state_dir / f"raw_{ts}.curated_tmp.jsonl"
                            final_jsonl = state_dir / f"raw_{ts}.final.jsonl"
                            qual_ok = _exec_stage(
                                "tg_llm_qualify",
                                [
                                    py_bin,
                                    str(ROOT / "leadgen" / "tg_llm_qualify.py"),
                                    "--input",
                                    str(enrich_src),
                                    "--output",
                                    str(qualified_jsonl),
                                ],
                            )
                            # Fail-Closed: if tg_llm_qualify failed or produced 0 qualified leads,
                            # NEVER fall back to enrich_src — 0 leads advance to tg_curate / CRM!
                            if (
                                qual_ok
                                and qualified_jsonl.is_file()
                                and qualified_jsonl.stat().st_size > 0
                            ):
                                curate_ok = _exec_stage(
                                    "tg_curate",
                                    [
                                        py_bin,
                                        str(ROOT / "leadgen" / "tg_curate.py"),
                                        "--input",
                                        str(qualified_jsonl),
                                        "--output",
                                        str(curated_jsonl),
                                    ],
                                )
                                if curate_ok:
                                    pre_wf_tg_cnt = (
                                        _count_personal_tg_rows(curated_jsonl)
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0
                                        else 0
                                    )
                                    if treg_ready and curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                        treg_tmp_jsonl = state_dir / f"raw_{ts}.treg_tmp.jsonl"
                                        treg_ok = _exec_stage(
                                            "treg_enrich",
                                            [
                                                py_bin,
                                                str(ROOT / "leadgen" / "treg_enrich.py"),
                                                "--input",
                                                str(curated_jsonl),
                                                "--output",
                                                str(treg_tmp_jsonl),
                                                "--cache",
                                                str(state_dir / "treg_market.json"),
                                            ],
                                        )
                                        if (
                                            treg_ok
                                            and treg_tmp_jsonl.is_file()
                                            and treg_tmp_jsonl.stat().st_size > 0
                                        ):
                                            treg_tmp_jsonl.replace(curated_jsonl)
                                        elif treg_tmp_jsonl.is_file():
                                            treg_tmp_jsonl.unlink(missing_ok=True)
                                    if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                        wf_tmp_jsonl = state_dir / f"raw_{ts}.waterfall_tmp.jsonl"
                                        wf_ok = _exec_stage(
                                            "contact_waterfall",
                                            [
                                                py_bin,
                                                str(ROOT / "leadgen" / "contact_waterfall.py"),
                                                "--input",
                                                str(curated_jsonl),
                                                "--output",
                                                str(wf_tmp_jsonl),
                                                "--max-budget-usd",
                                                "0.08",
                                            ],
                                        )
                                        if (
                                            wf_ok
                                            and wf_tmp_jsonl.is_file()
                                            and wf_tmp_jsonl.stat().st_size > 0
                                        ):
                                            wf_tmp_jsonl.replace(curated_jsonl)
                                        elif wf_tmp_jsonl.is_file():
                                            wf_tmp_jsonl.unlink(missing_ok=True)
                                    if (
                                        (treg_ready or monid_ready or runner is not subprocess.run)
                                        and sources_cfg.get("tg_identity_bridge", True)
                                        and "tg_identity_bridge" not in quarantined
                                        and curated_jsonl.is_file()
                                        and curated_jsonl.stat().st_size > 0
                                    ):
                                        bridge_tmp_jsonl = state_dir / f"raw_{ts}.bridge_tmp.jsonl"
                                        bridge_ok = _exec_stage(
                                            "tg_identity_bridge",
                                            [
                                                py_bin,
                                                str(ROOT / "leadgen" / "tg_identity_bridge.py"),
                                                "--input",
                                                str(curated_jsonl),
                                                "--output",
                                                str(bridge_tmp_jsonl),
                                            ],
                                        )
                                        if (
                                            bridge_ok
                                            and bridge_tmp_jsonl.is_file()
                                            and bridge_tmp_jsonl.stat().st_size > 0
                                        ):
                                            bridge_tmp_jsonl.replace(curated_jsonl)
                                        elif bridge_tmp_jsonl.is_file():
                                            bridge_tmp_jsonl.unlink(missing_ok=True)
                                    if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                        post_tg_cnt = _count_personal_tg_rows(curated_jsonl)
                                        bridged_tag_cnt = sum(
                                            1
                                            for r in _read_jsonl_dicts(curated_jsonl)
                                            if str(r.get("tg_bridge_match") or "").strip()
                                        )
                                        tg_bridged_count = max(
                                            0, post_tg_cnt - pre_wf_tg_cnt, bridged_tag_cnt
                                        )
                                        split_res = split_tg_and_direct_x_leads(
                                            curated_jsonl, state_dir / "direct_x_leads.jsonl"
                                        )
                                        direct_x_routed += int(split_res.get("direct_x_added") or 0)
                                    if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                        curated_jsonl.replace(final_jsonl)
                                    else:
                                        curated_jsonl.unlink(missing_ok=True)
                                        if final_jsonl.is_file():
                                            final_jsonl.unlink(missing_ok=True)
                                else:
                                    curated_jsonl.unlink(missing_ok=True)
                                    if final_jsonl.is_file():
                                        final_jsonl.unlink(missing_ok=True)
                            elif final_jsonl.is_file():
                                final_jsonl.unlink(missing_ok=True)

                        pipeline_ok = has_llm_key and enrich_ok and qual_ok and curate_ok
                        if pipeline_ok:
                            failed_rows = _read_jsonl_dicts(llm_failed_file) if has_llm_key else []
                            llm_failed_file.unlink(missing_ok=True)
                            if failed_rows and raw_jsonl.is_file():
                                failed_key_set: set[str] = set()
                                for fr in failed_rows:
                                    failed_key_set.update(_lead_retry_keys(fr))
                                raw_commit_tmp = state_dir / f"raw_{ts}.commit_tmp.jsonl"
                                kept_raw = [
                                    r
                                    for r in _read_jsonl_dicts(raw_jsonl)
                                    if not (_lead_retry_keys(r) & failed_key_set)
                                ]
                                raw_commit_tmp.write_text(
                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in kept_raw)
                                    + ("\n" if kept_raw else ""),
                                    encoding="utf-8",
                                )
                                try:
                                    apply_master_merge(
                                        str(raw_commit_tmp), str(master_jsonl), commit_master=True
                                    )
                                finally:
                                    raw_commit_tmp.unlink(missing_ok=True)
                                    Path(f"{raw_commit_tmp}.new").unlink(missing_ok=True)
                                _write_pending_qualify_queue(pending_qualify_queue, failed_rows)
                            else:
                                if raw_jsonl.is_file():
                                    apply_master_merge(
                                        str(raw_jsonl), str(master_jsonl), commit_master=True
                                    )
                                pending_qualify_queue.unlink(missing_ok=True)
                        elif has_new_rows:
                            _write_pending_qualify_queue(
                                pending_qualify_queue,
                                _read_jsonl_dicts(new_jsonl),
                            )
                else:
                    pending_qualify_queue.unlink(missing_ok=True)

            # Vacancy scraper ONLY if valid isolated SCRAPER_TG_SESSION is available
            if still_needs_vac and isolated_session is not None:
                mon_out = state_dir / f"job_monitor_{today_str}.json"
                rep_out = state_dir / f"job_replies_{today_str}.json"
                _exec_stage(
                    "job_monitor",
                    [
                        py_bin,
                        str(ROOT / "leadgen" / "job_monitor.py"),
                        "--hours",
                        "24",
                        "--out",
                        str(mon_out),
                    ],
                )
                if has_llm_key and (
                    (mon_out.is_file() and mon_out.stat().st_size > 0)
                    or runner is not subprocess.run
                ):
                    _exec_stage(
                        "job_reply",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "job_reply.py"),
                            str(mon_out),
                            "--out",
                            str(rep_out),
                            "--limit",
                            str(int(vac_cfg.get("daily_limit") or 20)),
                            "--no-crm",
                        ],
                    )

            push_2 = push_discovered_now(conn)
            ingested_total += int(push_2.get("ingested") or 0)

            b2b_camp = _find_campaign_by_name(conn, b2b_name)
            vac_camp = _find_campaign_by_name(conn, vac_name)
            wa_name = str(b2b_cfg.get("target_campaign_wa") or "Leadgen Auto (WA)").strip() or "Leadgen Auto (WA)"
            wa_camp = _find_campaign_by_name(conn, wa_name)
            ofm_camp = None
            if b2b_cfg.get("ofm_outreach_enabled", False):
                ofm_name = str(b2b_cfg.get("target_campaign_ofm") or "SoftUGC — OFM (TG)").strip() or "SoftUGC — OFM (TG)"
                ofm_camp = _find_campaign_by_name(conn, ofm_name)

        # Step 3: Rule 5 Dry-Run quarantine rollback BEFORE finalizing replenish state
        for camp_obj in (b2b_camp, vac_camp, wa_camp, ofm_camp):
            if camp_obj is None:
                continue
            preview_obj, q_ids = _enforce_rule5_quarantine_on_new_recipients(
                conn,
                campaign=camp_obj,
                pre_existing_ids=pre_existing_recipient_ids,
                stage_errors=stage_errors,
            )
            if q_ids:
                rule5_quarantined[str(camp_obj["name"])] = q_ids
                ingested_total = max(0, ingested_total - len(q_ids))
            if preview_obj is not None:
                rule5_previews[str(camp_obj["name"])] = preview_obj

        pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
        pending_vac = _count_pending(conn, vac_camp["id"] if vac_camp else None)

    # Persist stage_errors in kv_settings for observability
    now_err_stamp = utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_stage_errors', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(stage_errors, ensure_ascii=False), now_err_stamp),
    )
    conn.commit()

    # 4. Early-Warning Alert 24h before queue exhaustion on active running B2B campaign
    low_water_alert: dict[str, Any] | None = None
    if (
        b2b_camp is not None
        and str(b2b_camp.get("status") or "").strip().lower() == "running"
        and pending_b2b < int(critical_low_mark)
    ):
        low_water_alert = _emit_critical_low_water_alert(
            conn,
            campaign=b2b_camp,
            pending_count=pending_b2b,
            critical_low_mark=int(critical_low_mark),
            has_scraper_session=(isolated_session is not None),
            has_llm_key=has_llm_key,
            missing_treg_cli=("missing_treg_cli" in replenish_reasons),
            digest_opener=digest_opener,
        )

    # 5. Update cloud_persistence_dirty_at if leads were added
    if ingested_total > 0:
        now_stamp = utc_now()
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('cloud_persistence_dirty_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (now_stamp, now_stamp),
        )
        conn.commit()

    ofm_review_added = int(per_source_counts.get("ofm_groups") or 0)
    st_dir = _resolve_state_dir()
    prune_stats = prune_leadgen_state_jsonl(st_dir)
    run_timestamp = utc_now()
    zero_streak = 0
    static_repeats: list[dict[str, Any]] = []
    if stages_run or ingested_total > 0 or direct_x_routed > 0 or ofm_review_added > 0:
        zero_streak, static_repeats = _update_run_history_and_detect(
            st_dir,
            {
                "timestamp": run_timestamp,
                "tg_bridged_count": tg_bridged_count,
                "ingested_total": ingested_total,
                "direct_x_routed": direct_x_routed,
                "source_hashes": source_hashes,
                "stage_error_count": len(stage_errors),
            },
        )
    result_payload = {
        "ok": True,
        "timestamp": run_timestamp,
        "ingested_total": ingested_total,
        "direct_x_routed": direct_x_routed,
        "ofm_review_added": ofm_review_added,
        "tg_bridged_count": tg_bridged_count,
        "tg_bridged_zero_streak": zero_streak,
        "per_source_counts": per_source_counts,
        "source_hashes": source_hashes,
        "static_dump_repeats": static_repeats,
        "prune_stats": prune_stats,
        "pending_b2b": pending_b2b,
        "pending_vacancies": pending_vac,
        "quarantined_sources": sorted(quarantined),
        "stages_run": stages_run,
        "stage_errors": stage_errors,
        "reasons": replenish_reasons,
        "rule5_previews": rule5_previews,
        "rule5_quarantined": rule5_quarantined,
        "low_water_alert": low_water_alert,
    }
    if stages_run or ingested_total > 0 or direct_x_routed > 0 or ofm_review_added > 0:
        try:
            st_dir.mkdir(parents=True, exist_ok=True)
            summary_tmp = st_dir / "last_run_summary.json.tmp"
            summary_tmp.write_text(
                json.dumps(
                    {
                        "timestamp": result_payload["timestamp"],
                        "ingested_total": ingested_total,
                        "direct_x_routed": direct_x_routed,
                        "ofm_review_added": ofm_review_added,
                        "tg_bridged_count": tg_bridged_count,
                        "tg_bridged_zero_streak": zero_streak,
                        "per_source_counts": per_source_counts,
                        "source_hashes": source_hashes,
                        "static_dump_repeats": static_repeats,
                        "prune_stats": prune_stats,
                        "pending_b2b": pending_b2b,
                        "pending_vacancies": pending_vac,
                        "stages_run": stages_run,
                        "stage_error_count": len(stage_errors),
                        "stage_errors": stage_errors,
                        "reasons": replenish_reasons,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            summary_tmp.replace(st_dir / "last_run_summary.json")
        except OSError:
            pass

    return result_payload


def _load_dotenv_if_allowed(env_path: Path) -> None:
    try:
        from app.runtime_config import should_skip_dotenv

        if should_skip_dotenv() or not env_path.is_file():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
                val = val[1:-1]
            if key:
                os.environ.setdefault(key, val)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_allowed(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Autonomous Low-Water Mark Leadgen Replenishment for VPS"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CRMTG_DB", DEFAULT_DB_PATH),
        help="Path to SQLite CRM DB",
    )
    parser.add_argument("--low-water-mark", type=int, default=50)
    parser.add_argument("--vacancies-low-water-mark", type=int, default=10)
    parser.add_argument("--critical-low-mark", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    def _handle_sigterm(_signum, _frame):
        raise _SigtermInterrupt("crmtg-leadgen terminated by SIGTERM")

    prev_sigterm = None
    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        pass

    conn = connect(args.db)
    init_db(conn)
    try:
        try:
            res = run_autonomous_replenish(
                conn,
                low_water_mark=args.low_water_mark,
                vac_low_water_mark=args.vacancies_low_water_mark,
                critical_low_mark=args.critical_low_mark,
                stage_timeout=args.timeout,
            )
        except (Exception, _SigtermInterrupt) as exc:
            import traceback

            traceback.print_exc(file=sys.stderr)
            err_payload = [
                {
                    "stage": "vps_autonomous_replenish_crash",
                    "returncode": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                    "at": utc_now(),
                }
            ]
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                now_stamp = utc_now()
                conn.execute(
                    """
                    INSERT INTO kv_settings (key, value, updated_at)
                    VALUES ('leadgen_stage_errors', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (json.dumps(err_payload, ensure_ascii=False), now_stamp),
                )
                conn.commit()
            except Exception:
                pass
            return 1
    finally:
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError):
                pass
        conn.close()

    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
