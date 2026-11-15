import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import ssl
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from app.db import ROOT, as_dict, locked_conn, utc_now
from app.reminders import complete_reminder, list_reminders, normalize_due

logger = logging.getLogger(__name__)

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
VPS_SOFTUGCBOT_ENV = Path("/opt/softugcbot/.env")
SOFTUGCBOT_ENV = Path("/Users/timofei/dev/Python/new era/softugcbot/.env")
DEFAULT_COLLEAGUE_CHAT_ID = "978069541"


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


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return out


def resolve_colleague_bot_credentials(conn=None, *, default_chat_id: bool = True) -> tuple[str, str]:
    token = (os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("BOT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if token == "e2e-not-a-secret":
        return "", ""
    in_pytest = "PYTEST_CURRENT_TEST" in os.environ and os.environ.get("ALLOW_FILE_BOT_CREDS_IN_TEST") != "1"
    if not token or not chat_id:
        if not in_pytest:
            vals = _read_env_file(ROOT / ".env")
            if not token:
                token = (vals.get("BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN") or "").strip()
            if not chat_id:
                chat_id = (vals.get("BOT_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID") or "").strip()
    if (not token or not chat_id) and conn is not None:
        if not in_pytest or default_chat_id:
            try:
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
    if not token or not chat_id:
        if not in_pytest:
            for env_path in (VPS_SOFTUGCBOT_ENV, SOFTUGCBOT_ENV):
                vals = _read_env_file(env_path)
                if not token:
                    token = (vals.get("BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN") or "").strip()
                if not chat_id:
                    chat_id = (vals.get("BOT_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and not chat_id and default_chat_id:
        chat_id = DEFAULT_COLLEAGUE_CHAT_ID
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
    if c1 or c2 or c3:
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
        # 1) Guard: manager already replied
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

        # 2) Guard: dialog is already read
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
                if in_ts and in_ts < last_read_at:
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
    crm_url: str | None = None,
    opener=None,
    incoming_sent_at: str | None = None,
    telegram_message_id: int | None = None,
    allow_read: bool = False,
) -> bool:
    try:
        resolved_dialog_id = int(dialog_id) if dialog_id is not None else None
        resolved_lead_id = int(lead_id) if lead_id is not None else None
        resolved_account_id = int(account_id) if account_id is not None else None
        resolved_peer_id = int(peer_id) if peer_id is not None else None
        clean_mid = int(telegram_message_id) if telegram_message_id is not None else None

        with locked_conn(conn):
            token, chat_id = resolve_colleague_bot_credentials(conn)
            if not token or not chat_id:
                return False
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
    rows = open_today(conn, now)
    text, markup = build_digest(rows)
    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    _post(SEND_URL.format(token=token), payload, opener)
    return {"ok": True, "chat_id": chat_id, "text": text, "count": len(rows)}


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
