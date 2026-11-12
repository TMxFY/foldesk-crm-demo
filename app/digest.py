import json
import os
from pathlib import Path
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

from app.db import ROOT, as_dict, utc_now
from app.reminders import complete_reminder, list_reminders, normalize_due

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
EMPTY_TEXT = "На сегодня задач нет."
REJECTED = "Telegram не принял сообщение"
NO_OPERATOR = "Нет BOT_TOKEN или BOT_CHAT_ID"
SOFTUGCBOT_ENV = Path("/Users/timofei/dev/Python/new era/softugcbot/.env")
DEFAULT_COLLEAGUE_CHAT_ID = "764309033"


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
            for env_path in (ROOT / ".env", SOFTUGCBOT_ENV):
                vals = _read_env_file(env_path)
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
    if token and not chat_id and default_chat_id:
        chat_id = DEFAULT_COLLEAGUE_CHAT_ID
    return token, chat_id


def _operator(conn=None) -> tuple[str, str]:
    token, chat_id = resolve_colleague_bot_credentials(conn, default_chat_id=False)
    if not token or not chat_id:
        raise ValueError(NO_OPERATOR)
    return token, chat_id


def _accepted(status: int, raw: bytes) -> None:
    if status != 200:
        raise ValueError(REJECTED)
    try:
        parsed = json.loads((raw or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(REJECTED) from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise ValueError(REJECTED)


def _post(url: str, payload: dict, opener) -> None:
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
        _accepted(exc.code, raw)
        return
    except urllib.error.URLError as exc:
        raise ValueError(REJECTED) from exc
    _accepted(status, raw)


def format_lead_reply_notification(
    lead_handle: str,
    account_handle: str,
) -> str:
    clean_lead = str(lead_handle or "").strip() or "unknown"
    clean_acc = str(account_handle or "").strip() or "unknown"
    return f"You received a response from {clean_lead}\n\nAccount: {clean_acc}"


def send_lead_reply_notification(
    conn,
    *,
    lead_handle: str,
    account_handle: str,
    opener=None,
) -> bool:
    try:
        token, chat_id = resolve_colleague_bot_credentials(conn)
        if not token or not chat_id:
            return False
        text = format_lead_reply_notification(lead_handle, account_handle)
        _post(SEND_URL.format(token=token), {"chat_id": chat_id, "text": text}, opener)
        return True
    except Exception:
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


def apply_digest_callback(conn, data: str) -> dict:
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
