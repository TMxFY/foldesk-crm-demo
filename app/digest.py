from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import ssl
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.db import (
    ROOT,
    as_dict,
    clean_tg_username_lookup,
    is_recipient_contacted_outreach,
    locked_conn,
    sql_norm_tg_username_expr,
    utc_now,
)
from app.reminders import complete_reminder, list_reminders, normalize_due

logger = logging.getLogger(__name__)

TELEGRAM_SERVICE_PEER_IDS = frozenset({777000, 178220800, 1271266957, 93372553})
TELEGRAM_SERVICE_USERNAMES = frozenset({"telegram", "replies", "spambot", "botfather"})
INTERNAL_TEAM_USERNAMES = frozenset(
    {
        "vladwilhelm",
        "korznikov_timofei",
        "tim_cros",
        "tim_korznikov",
        "realbyme_m",
        "subscale",
        "make1tlo",
        "saburowe",
        "saabath",
        "otv73",
        "bobkovartur",
        "softugccollegabot",
        "soctugcaccbot",
        "spambot",
        "botfather",
        "replies",
        "telegram",
    }
)
CLOSED_LEAD_STATUSES = frozenset({"won", "lost"})
CLOSED_AIRTABLE_STATUSES = frozenset({"signed", "cancelled", "closed won", "closed lost"})
_BOT_USERNAME_SUFFIX_RE = re.compile(
    r"(?:_bot|(?:collega|crm|outreach|notify|alert|helper|support|shop|futurology|acc)bot)$",
    re.IGNORECASE,
)


def extract_bot_user_id_from_token(token: str | None) -> int | None:
    raw = str(token or "").strip()
    if ":" not in raw:
        return None
    prefix = raw.split(":", 1)[0].strip()
    if prefix.isdigit():
        val = int(prefix)
        return val if val > 0 else None
    return None


def is_likely_telegram_bot_username(username: str | None) -> bool:
    clean = str(username or "").strip().lstrip("@").lower()
    if not clean:
        return False
    if clean in {"replies"}:
        return True
    if len(clean) >= 4 and clean.endswith("bot") and re.fullmatch(r"[a-z0-9_]{4,32}", clean):
        return True
    return bool(_BOT_USERNAME_SUFFIX_RE.search(clean))


def is_bot_or_service_telegram_username(username: str | None) -> bool:
    clean = str(username or "").strip().lstrip("@").lower()
    if not clean:
        return False
    return clean in TELEGRAM_SERVICE_USERNAMES or is_likely_telegram_bot_username(clean)


def is_closed_lead_status(
    status: str | None,
    airtable_status: str | None = None,
    *,
    won_only: bool = False,
) -> bool:
    st = str(status or "").strip().lower()
    at = str(airtable_status or "").strip().lower()
    if won_only:
        return st == "won" or at in {"signed", "closed won"}
    return st in CLOSED_LEAD_STATUSES or at in CLOSED_AIRTABLE_STATUSES


def _extract_handle_username_and_phone(
    handle: str | None,
    *,
    allow_bare_username: bool = False,
) -> tuple[str | None, str | None]:
    raw = str(handle or "").strip()
    if not raw or raw.lower() == "unknown" or raw.startswith(("id:", "#")):
        return None, None
    if raw.startswith("@"):
        u = raw[1:].strip()
        if re.fullmatch(r"[A-Za-z0-9_]{3,32}", u):
            return u, None
        return None, None
    digits = re.sub(r"\D", "", raw)
    if (raw.startswith("+") or raw.isdigit()) and len(digits) >= 7:
        return None, digits[-15:]
    if allow_bare_username and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", raw):
        return raw, None
    return None, None


def _table_exists(conn, table: str) -> bool:
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def build_ineligible_peer_context(conn) -> dict:
    token, chat_id = resolve_colleague_bot_credentials(conn)
    bot_uid = extract_bot_user_id_from_token(token)
    operator_chat_id: int | None = None
    if chat_id and str(chat_id).strip().lstrip("-").isdigit():
        try:
            operator_chat_id = int(str(chat_id).strip())
        except ValueError:
            operator_chat_id = None
    acc_uids: set[int] = set()
    acc_unames: set[str] = set()
    acc_phones: set[str] = set()
    if conn is not None and _table_exists(conn, "accounts"):
        for arow in conn.execute(
            "SELECT id, telegram_user_id, telegram_username, phone FROM accounts"
        ).fetchall():
            a_uid = arow["telegram_user_id"]
            if a_uid is not None:
                acc_uids.add(int(a_uid))
            a_uname = clean_tg_username_lookup(arow["telegram_username"])
            if a_uname:
                acc_unames.add(a_uname)
            a_pdigits = re.sub(r"\D", "", str(arow["phone"] or ""))
            if len(a_pdigits) >= 7:
                acc_phones.add(a_pdigits[-15:])
    return {
        "bot_uid": bot_uid,
        "operator_chat_id": operator_chat_id,
        "acc_uids": acc_uids,
        "acc_unames": acc_unames,
        "acc_phones": acc_phones,
    }


def is_ineligible_reply_notify_peer(
    conn,
    *,
    peer_id: int | None = None,
    username: str | None = None,
    lead_handle: str | None = None,
    phone: str | None = None,
    account_id: int | None = None,
    account_handle: str | None = None,
    is_bot: bool = False,
    preloaded_ctx: dict | None = None,
) -> tuple[bool, str]:
    """Single source of truth for peers that must NEVER trigger lead-reply alerts."""
    if is_bot:
        return True, "telegram_bot"

    clean_peer = int(peer_id) if peer_id is not None else None
    if clean_peer is not None and clean_peer in TELEGRAM_SERVICE_PEER_IDS:
        return True, "telegram_service_peer"

    raw_user, raw_u_phone = _extract_handle_username_and_phone(
        username, allow_bare_username=True
    )
    hdl_user, hdl_phone = _extract_handle_username_and_phone(
        lead_handle, allow_bare_username=False
    )
    candidate_users = [u for u in (raw_user, hdl_user) if u]

    raw_phone_digits = re.sub(r"\D", "", str(phone or ""))
    norm_phone_digits = (
        raw_phone_digits[-15:]
        if len(raw_phone_digits) >= 7
        else (hdl_phone or raw_u_phone or "")
    )

    for cu in candidate_users:
        if is_likely_telegram_bot_username(cu):
            return True, "telegram_bot_username"

    acc_user, acc_phone_digits = _extract_handle_username_and_phone(
        account_handle, allow_bare_username=False
    )
    if acc_user and any(cu.lower() == acc_user.lower() for cu in candidate_users):
        return True, "self_account_echo"
    if norm_phone_digits and acc_phone_digits and norm_phone_digits == acc_phone_digits:
        return True, "self_account_echo"

    if any(cu.lower() in INTERNAL_TEAM_USERNAMES for cu in candidate_users):
        return True, "internal_team_member"

    if preloaded_ctx is not None:
        bot_uid = preloaded_ctx.get("bot_uid")
        if bot_uid is not None and clean_peer is not None and clean_peer == bot_uid:
            return True, "colleague_bot_self_echo"
        op_chat_id = preloaded_ctx.get("operator_chat_id")
        if op_chat_id is not None and clean_peer is not None and clean_peer == op_chat_id:
            return True, "operator_chat_self_echo"
        acc_uids = preloaded_ctx.get("acc_uids") or ()
        if clean_peer is not None and clean_peer in acc_uids:
            return True, "crm_connected_account"
        acc_unames = preloaded_ctx.get("acc_unames") or ()
        if any(clean_tg_username_lookup(cu) in acc_unames for cu in candidate_users):
            return True, "crm_connected_account"
        acc_phones = preloaded_ctx.get("acc_phones") or ()
        if norm_phone_digits and norm_phone_digits in acc_phones:
            return True, "crm_connected_account"
        return False, ""

    token, chat_id = resolve_colleague_bot_credentials(conn)
    bot_uid = extract_bot_user_id_from_token(token)
    if bot_uid is not None and clean_peer is not None and clean_peer == bot_uid:
        return True, "colleague_bot_self_echo"
    if chat_id and str(chat_id).strip().lstrip("-").isdigit() and clean_peer is not None:
        try:
            if clean_peer == int(str(chat_id).strip()):
                return True, "operator_chat_self_echo"
        except ValueError:
            pass

    if conn is not None and _table_exists(conn, "accounts"):
        acc_rows = conn.execute(
            "SELECT id, telegram_user_id, telegram_username, phone FROM accounts"
        ).fetchall()
        cand_lower = {cu.lower() for cu in candidate_users}
        for arow in acc_rows:
            a_uid = arow["telegram_user_id"]
            if clean_peer is not None and a_uid is not None and int(a_uid) == clean_peer:
                return True, "crm_connected_account"
            a_uname = str(arow["telegram_username"] or "").strip().lstrip("@").lower()
            if a_uname and a_uname in cand_lower:
                return True, "crm_connected_account"
            a_pdigits = re.sub(r"\D", "", str(arow["phone"] or ""))
            a_norm = a_pdigits[-15:] if len(a_pdigits) >= 7 else ""
            if norm_phone_digits and a_norm and norm_phone_digits == a_norm:
                return True, "crm_connected_account"

    return False, ""


def _check_lead_or_recipient_backing(
    conn,
    *,
    lead_id: int | None,
    peer_id: int | None,
    username: str | None = None,
    lead_handle: str | None = None,
    phone: str | None = None,
    require_backing: bool,
) -> tuple[bool, str]:
    """Verify lead status is open and (when peer/dialog-backed) peer is an actual lead or recipient."""
    if conn is None:
        return True, ""

    clean_peer = int(peer_id) if peer_id is not None else None
    raw_user, raw_u_phone = _extract_handle_username_and_phone(
        username, allow_bare_username=True
    )
    hdl_user, hdl_phone = _extract_handle_username_and_phone(
        lead_handle, allow_bare_username=False
    )
    clean_user = clean_tg_username_lookup(raw_user or hdl_user) or None
    raw_phone_digits = re.sub(r"\D", "", str(phone or ""))
    norm_phone_digits = (
        raw_phone_digits[-15:]
        if len(raw_phone_digits) >= 7
        else (hdl_phone or raw_u_phone or "")
    )
    sql_phone_expr = (
        "substr(replace(replace(replace(replace(replace(phone, '+', ''), ' ', ''), '-', ''), '(', ''), ')', ''), -15)"
    )
    sql_uname_expr = sql_norm_tg_username_expr("username")

    matched_leads = []
    if _table_exists(conn, "leads"):
        if lead_id is not None:
            lrow = conn.execute(
                "SELECT id, status, airtable_status FROM leads WHERE id = ?",
                (int(lead_id),),
            ).fetchone()
            if lrow is not None:
                matched_leads.append(lrow)
        clauses = []
        params: list = []
        if clean_peer is not None:
            clauses.append("telegram_user_id = ?")
            params.append(clean_peer)
        if clean_user:
            clauses.append(f"(username IS NOT NULL AND {sql_uname_expr} = ?)")
            params.append(clean_user)
        if norm_phone_digits:
            clauses.append(f"(phone IS NOT NULL AND {sql_phone_expr} = ?)")
            params.append(norm_phone_digits)
        if clauses:
            seen_ids = {int(r["id"]) for r in matched_leads}
            for r in conn.execute(
                f"SELECT id, status, airtable_status FROM leads WHERE {' OR '.join(clauses)}",
                params,
            ).fetchall():
                if int(r["id"]) not in seen_ids:
                    matched_leads.append(r)
                    seen_ids.add(int(r["id"]))

    if matched_leads:
        if any(is_closed_lead_status(r["status"], r["airtable_status"], won_only=True) for r in matched_leads):
            return False, "closed_won_client"
        if all(is_closed_lead_status(r["status"], r["airtable_status"]) for r in matched_leads):
            return False, "closed_lost_lead"
        return True, "open_lead"

    if not require_backing:
        return True, ""

    if _table_exists(conn, "campaign_recipients"):
        rc_clauses = []
        rc_params: list = []
        if clean_peer is not None:
            rc_clauses.append("telegram_user_id = ?")
            rc_params.append(clean_peer)
        if clean_user:
            rc_clauses.append(f"(username IS NOT NULL AND {sql_uname_expr} = ?)")
            rc_params.append(clean_user)
        if norm_phone_digits:
            rc_clauses.append(f"(phone IS NOT NULL AND {sql_phone_expr} = ?)")
            rc_params.append(norm_phone_digits)
        if rc_clauses:
            rec_rows = conn.execute(
                f"""
                SELECT status, current_step, last_sent_at, error_text
                FROM campaign_recipients
                WHERE ({' OR '.join(rc_clauses)})
                """,
                rc_params,
            ).fetchall()
            if any(
                is_recipient_contacted_outreach(
                    r["status"],
                    r["current_step"],
                    r["last_sent_at"],
                    r["error_text"],
                )
                for r in rec_rows
            ):
                return True, "campaign_recipient"

    return False, "non_lead_dialog"

_NOTIFY_INFLIGHT_GUARD = threading.Lock()
_NOTIFY_INFLIGHT_LOCKS: dict[tuple, threading.Lock] = {}


def _get_notify_inflight_lock(
    chat_id: str,
    dialog_id: int | None,
    account_id: int | None,
    peer_id: int | None,
    lead_id: int | None,
) -> threading.Lock:
    if dialog_id is not None:
        key: tuple = ("dlg", str(chat_id), int(dialog_id))
    elif account_id is not None and peer_id is not None:
        key = ("peer", str(chat_id), int(account_id), int(peer_id))
    elif lead_id is not None:
        key = ("lead", str(chat_id), int(lead_id))
    else:
        key = ("global", str(chat_id))
    with _NOTIFY_INFLIGHT_GUARD:
        lock = _NOTIFY_INFLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _NOTIFY_INFLIGHT_LOCKS[key] = lock
        return lock

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
EMPTY_TEXT = "На сегодня задач нет."
REJECTED = "Telegram не принял сообщение"
NO_OPERATOR = "Нет BOT_TOKEN или BOT_CHAT_ID"


def _default_urlopen(req, timeout: float = 15):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if not isinstance(getattr(exc, "reason", None), ssl.SSLError):
            raise
    cafile: str | None = None
    try:
        import certifi
        cafile = certifi.where()
    except Exception:
        if os.path.exists("/etc/ssl/cert.pem"):
            cafile = "/etc/ssl/cert.pem"
    ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _day(value: str) -> str:
    try:
        return normalize_due(value)[:10]
    except ValueError:
        return (value or "")[:10]


def list_today_schedule_for_digest(conn, now: str | None = None) -> list[dict]:
    if now:
        day = _day(now)
    else:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = conn.execute(
            """
            SELECT
                ce.*,
                l.name AS lead_name,
                l.username AS lead_username
            FROM calendar_events ce
            LEFT JOIN leads l ON l.id = ce.lead_id
            WHERE ce.status = 'scheduled'
            ORDER BY ce.start_at ASC, ce.id ASC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    items: list[dict] = []
    for raw in rows:
        ce = as_dict(raw)
        start_at = str(ce.get("start_at") or "")
        if _day(start_at) != day:
            continue
        title = str(ce.get("title") or "").strip() or "Созвон"
        items.append(
            {
                "id": int(ce["id"]),
                "calendar_event_id": int(ce["id"]),
                "lead_id": ce.get("lead_id"),
                "lead_name": ce.get("lead_name"),
                "lead_username": ce.get("lead_username"),
                "due_at": start_at,
                "start_at": start_at,
                "end_at": ce.get("end_at"),
                "title": title,
                "text": f"📅 {title}",
                "event_type": ce.get("event_type") or "call",
                "meet_link": ce.get("meet_link"),
                "status": ce.get("status") or "scheduled",
                "kind": "calendar_event",
            }
        )
    return items


def open_today(conn, now: str | None = None) -> list:
    if now:
        day = _day(now)
    else:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = list_reminders(conn, scope="open")
    today_reminders = [row for row in rows if _day(row["due_at"]) == day]
    cal_items = list_today_schedule_for_digest(conn, now=now)
    if not cal_items:
        return today_reminders
    return today_reminders + cal_items


def build_digest(rows: list) -> tuple[str, dict | None]:
    if not rows:
        return EMPTY_TEXT, None
    lines = ["Задачи на сегодня:"]
    keyboard = []
    for index, row in enumerate(rows, start=1):
        who = row.get("lead_name") or row.get("lead_username") or "лид"
        clock = ""
        due = row.get("due_at") or ""
        if len(due) >= 16:
            clock = due[11:16]
        line = f"{index}. {row['text']} — {who}"
        if clock:
            line = f"{line} ({clock})"
        lines.append(line)
        if len(keyboard) < 45:
            if row.get("kind") == "calendar_event":
                btns = [{"text": "Сделано", "callback_data": f"cal_done:{row['id']}"}]
                if row.get("lead_id") is not None:
                    btns.append({"text": "К лиду", "callback_data": f"lead:{row['lead_id']}"})
                keyboard.append(btns)
            else:
                keyboard.append(
                    [
                        {"text": "Сделано", "callback_data": f"done:{row['id']}"},
                        {"text": "К лиду", "callback_data": f"lead:{row['lead_id']}"},
                    ]
                )
    return "\n".join(lines), {"inline_keyboard": keyboard}


def resolve_colleague_bot_credentials(conn=None, *, default_chat_id: bool = False) -> tuple[str, str]:
    from app.runtime_config import is_e2e_sandbox

    if is_e2e_sandbox():
        return "", ""
    token = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("BOT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if (not token or not chat_id) and conn is not None:
        try:
            with locked_conn(conn):
                rows = dict(
                    conn.execute(
                        "SELECT key, value FROM kv_settings WHERE key IN ('bot_token', 'bot_chat_id')"
                    ).fetchall()
                )
            if not token:
                token = str(rows.get("bot_token") or "").strip()
            if not chat_id:
                chat_id = str(rows.get("bot_chat_id") or "").strip()
        except Exception:
            pass
    return token, chat_id


def _operator(conn=None) -> tuple[str, str]:
    token, chat_id = resolve_colleague_bot_credentials(conn, default_chat_id=False)
    if not token or not chat_id:
        raise ValueError(NO_OPERATOR)
    return token, chat_id


def _accepted(status: int, raw: bytes) -> dict:
    if status != 200:
        raise ValueError(REJECTED)
    try:
        parsed = json.loads((raw or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(REJECTED) from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise ValueError(REJECTED)
    return parsed


def _post(url: str, payload: dict, opener) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_fn = opener or _default_urlopen
    try:
        with open_fn(req, timeout=15) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = getattr(resp, "code", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return _accepted(exc.code, raw)
    except urllib.error.URLError as exc:
        raise ValueError(REJECTED) from exc
    return _accepted(status, raw)


def _norm_ts(value: str | None) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        iso_candidate = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        if len(s) >= 13 and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        for suffix in ("Z", "z", "+00:00", "-00:00"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
                break
        if "." in s and len(s) >= 19:
            head, _, _ = s.partition(".")
            if len(head) == 19:
                s = head
        return s


def _ensure_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return False
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    return False


def ensure_dialog_notify_columns(conn) -> None:
    """Idempotently add read/turn watermark columns to dialogs."""
    if conn is None:
        return
    c1 = _ensure_column_if_missing(conn, "dialogs", "last_read_at", "TEXT")
    c2 = _ensure_column_if_missing(conn, "dialogs", "read_inbox_max_id", "INTEGER DEFAULT 0")
    c3 = _ensure_column_if_missing(conn, "dialogs", "reply_turn_seq", "INTEGER DEFAULT 0")
    c4 = _ensure_column_if_missing(conn, "dialogs", "is_bot", "INTEGER DEFAULT 0")
    if c1 or c2 or c3 or c4:
        conn.commit()


def ensure_bot_reply_notifications_table(conn) -> None:
    if conn is None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_reply_notifications (
            bot_message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            dialog_id INTEGER,
            lead_id INTEGER,
            account_id INTEGER,
            peer_id INTEGER,
            lead_handle TEXT,
            account_handle TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, bot_message_id)
        )
        """
    )
    _ensure_column_if_missing(conn, "bot_reply_notifications", "incoming_message_id", "INTEGER")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "incoming_sent_at", "TEXT")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "turn_key", "TEXT")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "turn_seq", "INTEGER")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "last_out_msg_row_id", "INTEGER")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "dialog_last_read_at", "TEXT")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "dialog_read_inbox_max_id", "INTEGER")
    _ensure_column_if_missing(conn, "bot_reply_notifications", "preview_hash", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_notif_chat_dlg ON bot_reply_notifications(chat_id, dialog_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_notif_chat_acc_peer ON bot_reply_notifications(chat_id, account_id, peer_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_notif_turn ON bot_reply_notifications(chat_id, turn_key)"
    )


def _resolve_notify_dialog(
    conn,
    *,
    dialog_id: int | None,
    account_id: int | None,
    peer_id: int | None,
    lead_id: int | None,
):
    if conn is None:
        return None
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dialogs'"
    ).fetchone()
    if exists is None:
        return None
    if dialog_id is not None:
        row = conn.execute(
            "SELECT * FROM dialogs WHERE id = ? AND peer_type = 'user' LIMIT 1",
            (int(dialog_id),),
        ).fetchone()
        if row is not None:
            return row
    if account_id is not None and peer_id is not None:
        row = conn.execute(
            "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ? LIMIT 1",
            (int(account_id), int(peer_id)),
        ).fetchone()
        if row is not None:
            return row
    if account_id is None and peer_id is not None:
        row = conn.execute(
            "SELECT * FROM dialogs WHERE peer_type = 'user' AND peer_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (int(peer_id),),
        ).fetchone()
        if row is not None:
            return row
    if lead_id is not None:
        if account_id is not None:
            row = conn.execute(
                "SELECT * FROM dialogs WHERE lead_id = ? AND account_id = ? AND peer_type = 'user' ORDER BY last_message_at DESC, id DESC LIMIT 1",
                (int(lead_id), int(account_id)),
            ).fetchone()
            if row is not None:
                return row
        else:
            row = conn.execute(
                "SELECT * FROM dialogs WHERE lead_id = ? AND peer_type = 'user' ORDER BY last_message_at DESC, id DESC LIMIT 1",
                (int(lead_id),),
            ).fetchone()
            if row is not None:
                return row
    return None


def _dialog_message_state(conn, dialog_id: int | None) -> tuple[object | None, int, str]:
    if conn is None or dialog_id is None:
        return None, 0, ""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
    ).fetchone()
    if exists is None:
        return None, 0, ""
    latest_msg = conn.execute(
        """
        SELECT id, telegram_message_id, direction, sent_at, created_at
        FROM messages
        WHERE dialog_id = ?
        ORDER BY REPLACE(COALESCE(sent_at, ''), ' ', 'T') DESC, COALESCE(telegram_message_id, 0) DESC, id DESC
        LIMIT 1
        """,
        (int(dialog_id),),
    ).fetchone()
    latest_out = conn.execute(
        """
        SELECT id, telegram_message_id, sent_at, created_at
        FROM messages
        WHERE dialog_id = ? AND direction = 'out'
        ORDER BY REPLACE(COALESCE(sent_at, ''), ' ', 'T') DESC, COALESCE(telegram_message_id, 0) DESC, id DESC
        LIMIT 1
        """,
        (int(dialog_id),),
    ).fetchone()
    out_row_id = int(latest_out["id"]) if latest_out is not None else 0
    out_sent_at = _norm_ts(latest_out["sent_at"]) if latest_out is not None else ""
    return latest_msg, out_row_id, out_sent_at


def _find_prev_reply_notification(
    conn,
    *,
    chat_id: str,
    dialog_id: int | None,
    account_id: int | None,
    peer_id: int | None,
    lead_id: int | None,
    has_dialog: bool,
):
    if dialog_id is not None or (account_id is not None and peer_id is not None):
        return conn.execute(
            """
            SELECT * FROM bot_reply_notifications
            WHERE chat_id = ?
              AND (
                  (? IS NOT NULL AND dialog_id = ?)
                  OR (? IS NOT NULL AND ? IS NOT NULL AND account_id = ? AND peer_id = ?)
              )
            ORDER BY created_at DESC, bot_message_id DESC
            LIMIT 1
            """,
            (
                chat_id,
                dialog_id,
                dialog_id,
                account_id,
                peer_id,
                account_id,
                peer_id,
            ),
        ).fetchone()
    if not has_dialog and lead_id is not None:
        return conn.execute(
            """
            SELECT * FROM bot_reply_notifications
            WHERE chat_id = ? AND dialog_id IS NULL AND lead_id = ?
            ORDER BY created_at DESC, bot_message_id DESC
            LIMIT 1
            """,
            (chat_id, int(lead_id)),
        ).fetchone()
    return None


def _should_skip_reply_notification(
    conn,
    *,
    chat_id: str,
    dlg_row,
    resolved_dialog_id: int | None,
    resolved_lead_id: int | None,
    resolved_account_id: int | None,
    resolved_peer_id: int | None,
    incoming_sent_at: str | None,
    telegram_message_id: int | None,
    allow_read: bool,
    latest_msg,
    latest_out_row_id: int,
    latest_out_sent_at: str,
) -> bool:
    if conn is None:
        return False
    in_ts = _norm_ts(incoming_sent_at)
    turn_seq = int(dlg_row["reply_turn_seq"] or 0) if dlg_row is not None else 0

    if dlg_row is not None:
        # 1) Guard: triggering message & latest dialog state must be incoming (never outgoing)
        if telegram_message_id is not None and resolved_dialog_id is not None and _table_exists(conn, "messages"):
            trig_row = conn.execute(
                "SELECT direction FROM messages WHERE dialog_id = ? AND telegram_message_id = ? LIMIT 1",
                (int(resolved_dialog_id), int(telegram_message_id)),
            ).fetchone()
            if trig_row is not None and str(trig_row["direction"] or "").strip().lower() != "in":
                return True

        # 2) Guard: manager already replied
        if int(dlg_row["last_from_peer"] or 0) == 0:
            return True
        if latest_msg is not None and str(latest_msg["direction"]) == "out":
            out_mid = latest_msg["telegram_message_id"]
            if telegram_message_id is not None and out_mid is not None:
                if int(telegram_message_id) <= int(out_mid):
                    return True
            else:
                out_ts = _norm_ts(latest_msg["sent_at"])
                if not (in_ts and out_ts and in_ts > out_ts and int(dlg_row["last_from_peer"] or 0) == 1):
                    return True

        # 3) Guard: dialog is already read
        if not allow_read and int(dlg_row["unread_count"] or 0) == 0:
            return True
        read_inbox_max_id = int(dlg_row["read_inbox_max_id"] or 0)
        if telegram_message_id is not None and read_inbox_max_id > 0 and int(telegram_message_id) <= read_inbox_max_id:
            return True
        last_read_at = _norm_ts(dlg_row["last_read_at"])
        if last_read_at and not allow_read:
            if telegram_message_id is not None and read_inbox_max_id > 0:
                pass
            elif telegram_message_id is not None:
                if in_ts and in_ts <= last_read_at:
                    return True
            else:
                eff_in_ts = in_ts or _norm_ts(dlg_row["last_message_at"])
                if not eff_in_ts or eff_in_ts <= last_read_at:
                    return True

    # 3) Guard: already notified during this active unreplied turn / for same message
    prev_notif = _find_prev_reply_notification(
        conn,
        chat_id=chat_id,
        dialog_id=resolved_dialog_id,
        account_id=resolved_account_id,
        peer_id=resolved_peer_id,
        lead_id=resolved_lead_id,
        has_dialog=dlg_row is not None,
    )
    if prev_notif is None:
        return False

    if (
        telegram_message_id is not None
        and prev_notif["incoming_message_id"] is not None
        and int(prev_notif["incoming_message_id"]) == int(telegram_message_id)
    ):
        return True

    if dlg_row is None:
        return True

    prev_out_row_id = int(prev_notif["last_out_msg_row_id"] or 0)
    prev_created_ts = _norm_ts(prev_notif["created_at"])
    out_advanced = latest_out_row_id > prev_out_row_id or (
        bool(latest_out_sent_at) and bool(prev_created_ts) and latest_out_sent_at > prev_created_ts
    )
    seq_advanced = prev_notif["turn_seq"] is not None and turn_seq > int(prev_notif["turn_seq"])

    return not (out_advanced or seq_advanced)


def format_lead_reply_notification(
    lead_handle: str,
    account_handle: str,
    preview: str | None = None,
    lead_name: str | None = None,
) -> str:
    clean_lead = str(lead_handle or "").strip() or "unknown"
    clean_acc = str(account_handle or "").strip() or "unknown"
    base = f"You received a response from {clean_lead}\n\nAccount: {clean_acc}"
    clean_preview = str(preview or "").strip()
    if not clean_preview:
        return base
    return (
        f"{base}"
        f"\n💬 {clean_preview}\n\n"
        "↩️ Сделайте Reply на это сообщение, чтобы ответить клиенту прямо из бота."
    )


def _prepare_reply_notification_state(
    conn,
    *,
    chat_id: str,
    resolved_dialog_id: int | None,
    resolved_lead_id: int | None,
    resolved_account_id: int | None,
    resolved_peer_id: int | None,
    incoming_sent_at: str | None,
    telegram_message_id: int | None,
    allow_read: bool,
) -> tuple[bool, dict]:
    if conn is None:
        return True, {
            "dialog_id": resolved_dialog_id,
            "lead_id": resolved_lead_id,
            "account_id": resolved_account_id,
            "peer_id": resolved_peer_id,
            "turn_seq": None,
            "last_out_msg_row_id": None,
            "eff_in_sent": _norm_ts(incoming_sent_at),
            "dialog_last_read_at": None,
            "dialog_read_inbox_max_id": None,
        }
    with locked_conn(conn):
        ensure_bot_reply_notifications_table(conn)
        ensure_dialog_notify_columns(conn)
        dlg_row = _resolve_notify_dialog(
            conn,
            dialog_id=resolved_dialog_id,
            account_id=resolved_account_id,
            peer_id=resolved_peer_id,
            lead_id=resolved_lead_id,
        )
        if dlg_row is not None:
            resolved_dialog_id = int(dlg_row["id"])
            if resolved_account_id is None and dlg_row["account_id"] is not None:
                resolved_account_id = int(dlg_row["account_id"])
            if resolved_peer_id is None and dlg_row["peer_id"] is not None:
                resolved_peer_id = int(dlg_row["peer_id"])
            if resolved_lead_id is None and dlg_row["lead_id"] is not None:
                resolved_lead_id = int(dlg_row["lead_id"])
        latest_msg, latest_out_row_id, latest_out_sent_at = _dialog_message_state(conn, resolved_dialog_id)
        if _should_skip_reply_notification(
            conn,
            chat_id=chat_id,
            dlg_row=dlg_row,
            resolved_dialog_id=resolved_dialog_id,
            resolved_lead_id=resolved_lead_id,
            resolved_account_id=resolved_account_id,
            resolved_peer_id=resolved_peer_id,
            incoming_sent_at=incoming_sent_at,
            telegram_message_id=telegram_message_id,
            allow_read=allow_read,
            latest_msg=latest_msg,
            latest_out_row_id=latest_out_row_id,
            latest_out_sent_at=latest_out_sent_at,
        ):
            return False, {}
        return True, {
            "dialog_id": resolved_dialog_id,
            "lead_id": resolved_lead_id,
            "account_id": resolved_account_id,
            "peer_id": resolved_peer_id,
            "turn_seq": int(dlg_row["reply_turn_seq"] or 0) if dlg_row is not None else None,
            "last_out_msg_row_id": latest_out_row_id if dlg_row is not None else None,
            "eff_in_sent": (
                _norm_ts(incoming_sent_at)
                or (_norm_ts(dlg_row["last_message_at"]) if dlg_row is not None else "")
            ),
            "dialog_last_read_at": (
                (_norm_ts(dlg_row["last_read_at"]) or None) if dlg_row is not None else None
            ),
            "dialog_read_inbox_max_id": (
                int(dlg_row["read_inbox_max_id"] or 0) if dlg_row is not None else None
            ),
        }


def send_lead_reply_notification(
    conn,
    *,
    lead_handle: str,
    account_handle: str,
    preview: str | None = None,
    lead_name: str | None = None,
    dialog_id: int | None = None,
    lead_id: int | None = None,
    account_id: int | None = None,
    peer_id: int | None = None,
    phone: str | None = None,
    direction: str = "in",
    crm_url: str | None = None,
    opener=None,
    incoming_sent_at: str | None = None,
    telegram_message_id: int | None = None,
    allow_read: bool = False,
) -> bool:
    try:
        if os.getenv("DISABLE_BOT_ALERTS", "").strip() == "1":
            return False
        if str(direction or "").strip().lower() != "in":
            return False
        resolved_dialog_id = int(dialog_id) if dialog_id is not None else None
        resolved_lead_id = int(lead_id) if lead_id is not None else None
        resolved_account_id = int(account_id) if account_id is not None else None
        resolved_peer_id = int(peer_id) if peer_id is not None else None
        clean_mid = int(telegram_message_id) if telegram_message_id is not None else None

        with locked_conn(conn):
            token, chat_id = resolve_colleague_bot_credentials(conn)
            if not token or not chat_id:
                return False
            peek_dlg = None
            dlg_is_bot = False
            dlg_username = None
            dlg_phone = str(phone).strip() if phone else None
            if conn is not None:
                peek_dlg = _resolve_notify_dialog(
                    conn,
                    dialog_id=resolved_dialog_id,
                    account_id=resolved_account_id,
                    peer_id=resolved_peer_id,
                    lead_id=resolved_lead_id,
                )
                if peek_dlg is not None:
                    resolved_dialog_id = int(peek_dlg["id"])
                    if resolved_account_id is None and peek_dlg["account_id"] is not None:
                        resolved_account_id = int(peek_dlg["account_id"])
                    if resolved_peer_id is None and peek_dlg["peer_id"] is not None:
                        resolved_peer_id = int(peek_dlg["peer_id"])
                    if resolved_lead_id is None and peek_dlg["lead_id"] is not None:
                        resolved_lead_id = int(peek_dlg["lead_id"])
                    dlg_username = peek_dlg["username"]
                    dlg_phone = peek_dlg["phone"] or dlg_phone
                    if "is_bot" in peek_dlg.keys():
                        dlg_is_bot = bool(peek_dlg["is_bot"])
                if not dlg_phone and resolved_lead_id is not None and _table_exists(conn, "leads"):
                    l_ph = conn.execute(
                        "SELECT phone FROM leads WHERE id = ?",
                        (resolved_lead_id,),
                    ).fetchone()
                    if l_ph is not None and l_ph["phone"]:
                        dlg_phone = str(l_ph["phone"]).strip() or None
                if (
                    resolved_dialog_id is not None
                    and clean_mid is not None
                    and _table_exists(conn, "messages")
                ):
                    msg_row = conn.execute(
                        "SELECT direction FROM messages WHERE dialog_id = ? AND telegram_message_id = ? LIMIT 1",
                        (resolved_dialog_id, clean_mid),
                    ).fetchone()
                    if msg_row is not None and str(msg_row["direction"] or "").strip().lower() != "in":
                        return False

            inelig, _ = is_ineligible_reply_notify_peer(
                conn,
                peer_id=resolved_peer_id,
                username=dlg_username,
                lead_handle=lead_handle,
                phone=dlg_phone,
                account_id=resolved_account_id,
                account_handle=account_handle,
                is_bot=dlg_is_bot,
            )
            if inelig:
                return False

            backed_ok, _ = _check_lead_or_recipient_backing(
                conn,
                lead_id=resolved_lead_id,
                peer_id=resolved_peer_id,
                username=dlg_username,
                lead_handle=lead_handle,
                phone=dlg_phone,
                require_backing=(conn is not None),
            )
            if not backed_ok:
                return False

        inflight_lock = _get_notify_inflight_lock(
            str(chat_id),
            resolved_dialog_id,
            resolved_account_id,
            resolved_peer_id,
            resolved_lead_id,
        )
        with inflight_lock:
            should_send, state = _prepare_reply_notification_state(
                conn,
                chat_id=str(chat_id),
                resolved_dialog_id=resolved_dialog_id,
                resolved_lead_id=resolved_lead_id,
                resolved_account_id=resolved_account_id,
                resolved_peer_id=resolved_peer_id,
                incoming_sent_at=incoming_sent_at,
                telegram_message_id=clean_mid,
                allow_read=bool(allow_read),
            )
            if not should_send:
                return False

            final_dialog_id = state["dialog_id"]
            final_lead_id = state["lead_id"]
            final_account_id = state["account_id"]
            final_peer_id = state["peer_id"]

            text = format_lead_reply_notification(
                lead_handle,
                account_handle,
                preview=preview,
                lead_name=lead_name,
            )
            payload: dict = {"chat_id": chat_id, "text": text}
            buttons: list[dict] = []
            if final_dialog_id is not None:
                buttons.append({"text": "✍️ Ответить", "callback_data": f"reply_dlg:{final_dialog_id}"})
            elif final_lead_id is not None:
                buttons.append({"text": "✍️ Ответить", "callback_data": f"reply_lead:{final_lead_id}"})
            base_url = (crm_url or os.getenv("CRM_PUBLIC_URL") or "").strip().rstrip("/")
            if base_url.startswith("https://"):
                buttons.append(
                    {
                        "text": "📱 Открыть CRM",
                        "web_app": {"url": f"{base_url}/?tg_miniapp=1#inbox"},
                    }
                )
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": [buttons]}

            # HTTP call runs outside SQLite connection lock to avoid blocking DB readers/writers
            parsed = _post(SEND_URL.format(token=token), payload, opener)
            result_obj = parsed.get("result") if isinstance(parsed, dict) else None
            bot_msg_id = result_obj.get("message_id") if isinstance(result_obj, dict) else None
            if conn is not None and bot_msg_id is not None:
                turn_seq = state["turn_seq"]
                last_out_row_id = state["last_out_msg_row_id"]
                eff_in_sent = state["eff_in_sent"]
                if final_dialog_id is not None:
                    turn_key = f"dlg:{final_dialog_id}:seq:{turn_seq or 0}:out:{last_out_row_id or 0}"
                elif final_account_id is not None and final_peer_id is not None:
                    turn_key = f"peer:{final_account_id}:{final_peer_id}"
                elif final_lead_id is not None:
                    turn_key = f"lead:{final_lead_id}"
                else:
                    turn_key = None
                clean_preview = str(preview or "").strip()
                preview_hash = (
                    hashlib.sha256(clean_preview.encode("utf-8")).hexdigest()[:16]
                    if clean_preview
                    else None
                )
                with locked_conn(conn):
                    ensure_bot_reply_notifications_table(conn)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO bot_reply_notifications (
                            bot_message_id, chat_id, dialog_id, lead_id, account_id, peer_id,
                            lead_handle, account_handle, incoming_message_id, incoming_sent_at,
                            turn_key, turn_seq, last_out_msg_row_id, dialog_last_read_at,
                            dialog_read_inbox_max_id, preview_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(bot_msg_id),
                            str(chat_id),
                            final_dialog_id,
                            final_lead_id,
                            final_account_id,
                            final_peer_id,
                            str(lead_handle or "").strip() or None,
                            str(account_handle or "").strip() or None,
                            clean_mid,
                            eff_in_sent or None,
                            turn_key,
                            turn_seq,
                            last_out_row_id,
                            state["dialog_last_read_at"],
                            state["dialog_read_inbox_max_id"],
                            preview_hash,
                            utc_now(),
                        ),
                    )
                    conn.commit()
        return True
    except Exception as exc:
        logger.warning("send_lead_reply_notification failed: %s", exc, exc_info=True)
        return False


def send_morning_digest(conn, opener=None, now: str | None = None) -> dict:
    token, chat_id = _operator(conn)
    clean_chat = str(chat_id or "").strip()
    if not clean_chat:
        raise ValueError(NO_OPERATOR)
    rows = open_today(conn, now)
    text, markup = build_digest(rows)
    payload = {"chat_id": clean_chat, "text": text}
    if markup:
        payload["reply_markup"] = markup
    _post(SEND_URL.format(token=token), payload, opener)
    return {"ok": True, "chat_id": clean_chat, "text": text, "count": len(rows)}


def _store_active_reply_dialog(conn, dialog_id: int, chat_id: str | None = None) -> None:
    now_stamp = utc_now()
    clean_chat = str(chat_id or "").strip()
    keys = ["bot_active_reply_dialog_default"]
    if clean_chat:
        keys.append(f"bot_active_reply_dialog_{clean_chat}")
    else:
        _, resolved_chat = resolve_colleague_bot_credentials(conn)
        if resolved_chat:
            keys.append(f"bot_active_reply_dialog_{resolved_chat}")
    for key in set(keys):
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(int(dialog_id)), now_stamp),
        )
    conn.commit()


def apply_digest_callback(conn, data: str, chat_id: str | None = None) -> dict:
    # Не шлёт сообщение: ни оператору, ни лиду.
    raw = (data or "").strip()
    action, sep, rest = raw.partition(":")
    if sep != ":" or not rest:
        raise ValueError("Неизвестное действие")
    if action == "done":
        try:
            reminder_id = int(rest)
        except ValueError as exc:
            raise ValueError("Неизвестное действие") from exc
        row = complete_reminder(conn, reminder_id)
        return {
            "ok": True,
            "action": "done",
            "reminder_id": row["id"],
            "lead_id": row["lead_id"],
            "done_at": row["done_at"],
        }
    if action == "cal_done":
        try:
            event_id = int(rest)
        except ValueError as exc:
            raise ValueError("Неизвестное действие") from exc
        found = conn.execute(
            "SELECT * FROM calendar_events WHERE id = ?", (event_id,)
        ).fetchone()
        if found is None:
            raise LookupError("calendar_event")
        now_stamp = utc_now()
        conn.execute(
            "UPDATE calendar_events SET status = 'completed', updated_at = ? WHERE id = ?",
            (now_stamp, event_id),
        )
        conn.commit()
        return {
            "ok": True,
            "action": "cal_done",
            "calendar_event_id": event_id,
            "lead_id": found["lead_id"],
            "done_at": now_stamp,
        }
    if action == "lead":
        try:
            lead_id = int(rest)
        except ValueError as exc:
            raise ValueError("Неизвестное действие") from exc
        found = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if found is None:
            raise LookupError("lead")
        return {"ok": True, "action": "open", "lead_id": lead_id}
    if action == "reply_dlg":
        try:
            dialog_id = int(rest)
        except ValueError as exc:
            raise ValueError("Неизвестное действие") from exc
        found = conn.execute("SELECT id FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
        if found is None:
            raise LookupError("dialog")
        _store_active_reply_dialog(conn, dialog_id, chat_id=chat_id)
        return {"ok": True, "action": "reply_prompt", "dialog_id": dialog_id}
    if action == "reply_lead":
        try:
            lead_id = int(rest)
        except ValueError as exc:
            raise ValueError("Неизвестное действие") from exc
        dlg_row = conn.execute(
            "SELECT id FROM dialogs WHERE lead_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        if dlg_row is not None:
            dialog_id = int(dlg_row["id"])
        else:
            from app.inbox import open_dialog_by_lead

            opened = open_dialog_by_lead(conn, lead_id=lead_id)
            dialog_id = int(opened["id"])
        _store_active_reply_dialog(conn, dialog_id, chat_id=chat_id)
        return {"ok": True, "action": "reply_prompt", "dialog_id": dialog_id}
    raise ValueError("Неизвестное действие")


def callback_data_from_update(update: dict) -> str:
    if not isinstance(update, dict):
        raise ValueError("Неизвестное действие")
    query = update.get("callback_query")
    if not isinstance(query, dict):
        raise ValueError("Неизвестное действие")
    data = query.get("data")
    if not isinstance(data, str) or not data.strip():
        raise ValueError("Неизвестное действие")
    return data


def audit_reply_notifications(conn, *, since_hours: int | None = None) -> dict:
    """Audit persisted `bot_reply_notifications` and `leads` for echo/bot/non-lead/closed-client leaks."""
    if conn is None:
        return {
            "ok": True,
            "false_notification_count": 0,
            "bot_username_lead_count": 0,
            "false_notifications": [],
            "bot_username_leads": [],
        }

    cutoff = ""
    if since_hours is not None and since_hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(since_hours))).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

    false_notifications: list[dict] = []
    bot_username_leads: list[dict] = []

    with locked_conn(conn):
        has_dialogs = _table_exists(conn, "dialogs")
        if _table_exists(conn, "bot_reply_notifications"):
            rows = conn.execute(
                "SELECT * FROM bot_reply_notifications ORDER BY created_at DESC"
            ).fetchall()
            for r in rows:
                created_norm = _norm_ts(r["created_at"])
                if cutoff and created_norm and created_norm < cutoff:
                    continue
                dlg_row = None
                if has_dialogs and r["dialog_id"] is not None:
                    dlg_row = conn.execute(
                        "SELECT * FROM dialogs WHERE id = ?",
                        (int(r["dialog_id"]),),
                    ).fetchone()
                elif has_dialogs and r["account_id"] is not None and r["peer_id"] is not None:
                    dlg_row = conn.execute(
                        "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
                        (int(r["account_id"]), int(r["peer_id"])),
                    ).fetchone()

                dlg_user = dlg_row["username"] if dlg_row is not None else None
                dlg_phone = dlg_row["phone"] if dlg_row is not None else None
                dlg_lead_id = dlg_row["lead_id"] if dlg_row is not None else None
                dlg_is_bot = (
                    bool(dlg_row["is_bot"])
                    if (dlg_row is not None and "is_bot" in dlg_row.keys())
                    else False
                )

                inelig, reason = is_ineligible_reply_notify_peer(
                    conn,
                    peer_id=r["peer_id"],
                    username=dlg_user,
                    lead_handle=r["lead_handle"],
                    phone=dlg_phone,
                    account_id=r["account_id"],
                    account_handle=r["account_handle"],
                    is_bot=dlg_is_bot,
                )
                if not inelig:
                    eff_lead_id = (
                        int(r["lead_id"])
                        if r["lead_id"] is not None
                        else (int(dlg_lead_id) if dlg_lead_id is not None else None)
                    )
                    eff_peer_id = (
                        int(r["peer_id"])
                        if r["peer_id"] is not None
                        else (int(dlg_row["peer_id"]) if dlg_row is not None and dlg_row["peer_id"] is not None else None)
                    )
                    backed, back_reason = _check_lead_or_recipient_backing(
                        conn,
                        lead_id=eff_lead_id,
                        peer_id=eff_peer_id,
                        username=dlg_user,
                        lead_handle=r["lead_handle"],
                        phone=dlg_phone,
                        require_backing=True,
                    )
                    if not backed:
                        inelig = True
                        reason = back_reason

                if (
                    not inelig
                    and r["dialog_id"] is not None
                    and r["incoming_message_id"] is not None
                    and _table_exists(conn, "messages")
                ):
                    mdir = conn.execute(
                        "SELECT direction FROM messages WHERE dialog_id = ? AND telegram_message_id = ? LIMIT 1",
                        (int(r["dialog_id"]), int(r["incoming_message_id"])),
                    ).fetchone()
                    if mdir is not None and str(mdir["direction"] or "").strip().lower() != "in":
                        inelig = True
                        reason = "outgoing_message_alert"

                if inelig:
                    false_notifications.append(
                        {
                            "chat_id": str(r["chat_id"]),
                            "bot_message_id": int(r["bot_message_id"]),
                            "lead_handle": r["lead_handle"],
                            "account_handle": r["account_handle"],
                            "peer_id": r["peer_id"],
                            "lead_id": r["lead_id"],
                            "created_at": r["created_at"],
                            "reason": reason,
                        }
                    )

            seen_msg_keys: set[tuple[str, int, int]] = set()
            for r in rows:
                created_norm = _norm_ts(r["created_at"])
                if cutoff and created_norm and created_norm < cutoff:
                    continue
                if r["dialog_id"] is None or r["incoming_message_id"] is None:
                    continue
                d_key = (str(r["chat_id"]), int(r["dialog_id"]), int(r["incoming_message_id"]))
                if d_key in seen_msg_keys:
                    false_notifications.append(
                        {
                            "chat_id": str(r["chat_id"]),
                            "bot_message_id": int(r["bot_message_id"]),
                            "lead_handle": r["lead_handle"],
                            "account_handle": r["account_handle"],
                            "peer_id": r["peer_id"],
                            "lead_id": r["lead_id"],
                            "created_at": r["created_at"],
                            "reason": "duplicate_incoming_message_alert",
                        }
                    )
                else:
                    seen_msg_keys.add(d_key)

        if _table_exists(conn, "leads"):
            for lr in conn.execute(
                "SELECT id, name, username, status FROM leads WHERE username IS NOT NULL AND trim(username) != ''"
            ).fetchall():
                if is_bot_or_service_telegram_username(lr["username"]):
                    bot_username_leads.append(
                        {
                            "id": int(lr["id"]),
                            "name": lr["name"],
                            "username": lr["username"],
                            "status": lr["status"],
                        }
                    )

    ok = len(false_notifications) == 0 and len(bot_username_leads) == 0
    return {
        "ok": ok,
        "false_notification_count": len(false_notifications),
        "bot_username_lead_count": len(bot_username_leads),
        "false_notifications": false_notifications,
        "bot_username_leads": bot_username_leads,
    }


def heal_reply_notifications(conn) -> dict:
    """Self-heal persisted false reply notifications and bot-username leads."""
    if conn is None:
        return {
            "purged_notifications": 0,
            "scrubbed_bot_leads": 0,
            "detached_ineligible_dialogs": 0,
        }

    with locked_conn(conn):
        report = audit_reply_notifications(conn)
        purged = 0
        for item in report["false_notifications"]:
            cur = conn.execute(
                "DELETE FROM bot_reply_notifications WHERE chat_id = ? AND bot_message_id = ?",
                (item["chat_id"], int(item["bot_message_id"])),
            )
            purged += max(0, int(cur.rowcount or 0))

        scrubbed = 0
        now_ts = utc_now()
        for bl in report["bot_username_leads"]:
            row = conn.execute(
                "SELECT note, username FROM leads WHERE id = ?",
                (int(bl["id"]),),
            ).fetchone()
            if row is None:
                continue
            old_u = str(row["username"] or "").strip().lstrip("@")
            old_note = str(row["note"] or "").strip()
            prefix = "service-handle" if old_u.lower() == "telegram" else "bot-handle"
            marker = f"[{prefix}: @{old_u}]"
            new_note = old_note
            if old_u and marker.lower() not in old_note.lower():
                new_note = f"{old_note}\n{marker}".strip() if old_note else marker
            conn.execute(
                "UPDATE leads SET username = NULL, note = ?, updated_at = ? WHERE id = ?",
                (new_note or None, now_ts, int(bl["id"])),
            )
            scrubbed += 1

        detached = 0
        if _table_exists(conn, "dialogs"):
            dlg_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(dialogs)").fetchall()
            }
            has_is_bot = "is_bot" in dlg_cols
            has_phone = "phone" in dlg_cols
            sel_cols = "id, account_id, peer_id, username"
            if has_phone:
                sel_cols += ", phone"
            if has_is_bot:
                sel_cols += ", is_bot"
            for drow in conn.execute(
                f"SELECT {sel_cols} FROM dialogs WHERE lead_id IS NOT NULL"
            ).fetchall():
                inelig, _ = is_ineligible_reply_notify_peer(
                    conn,
                    peer_id=drow["peer_id"],
                    username=drow["username"],
                    phone=drow["phone"] if has_phone else None,
                    account_id=drow["account_id"],
                    is_bot=bool(drow["is_bot"]) if has_is_bot else False,
                )
                if inelig:
                    conn.execute(
                        "UPDATE dialogs SET lead_id = NULL, updated_at = ? WHERE id = ?",
                        (now_ts, int(drow["id"])),
                    )
                    detached += 1

        if purged or scrubbed or detached:
            conn.commit()
    return {
        "purged_notifications": purged,
        "scrubbed_bot_leads": scrubbed,
        "detached_ineligible_dialogs": detached,
    }
