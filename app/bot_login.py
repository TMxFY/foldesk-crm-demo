import json
import os
import urllib.error
import urllib.request

from app.db import as_dict, utc_now
from app.leads import create_lead
from app.notes import add_note

GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
NO_TOKEN = "Нет BOT_TOKEN"
NO_CHAT = "Нет BOT_CHAT_ID"
BAD_UPDATE = "Не апдейт"
REJECTED = "Telegram не принял запрос"


def _token() -> str:
    return os.environ.get("BOT_TOKEN", "").strip()


def _expected_chat() -> str:
    return os.environ.get("BOT_CHAT_ID", "").strip()


def operator_status(conn) -> dict:
    row = conn.execute(
        "SELECT logged_in, chat_id, logged_in_at FROM operator_session WHERE id = 1"
    ).fetchone()
    if row is None or not row["logged_in"]:
        return {"logged_in": False, "chat_id": None, "logged_in_at": None}
    return {
        "logged_in": True,
        "chat_id": row["chat_id"],
        "logged_in_at": row["logged_in_at"],
    }


def _is_start(text) -> bool:
    if not isinstance(text, str):
        return False
    token = text.strip().split()[0] if text.strip() else ""
    command = token.split("@", 1)[0].lower()
    return command == "/start"


def _chat_id(message: dict):
    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat or chat["id"] is None:
        return None
    return str(chat["id"]).strip()


def _open(conn, chat_id: str) -> dict:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO operator_session (id, logged_in, chat_id, logged_in_at)
        VALUES (1, 1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            logged_in = 1,
            chat_id = excluded.chat_id,
            logged_in_at = excluded.logged_in_at
        """,
        (chat_id, now),
    )
    conn.commit()
    return {"logged_in": True, "opened": True, "chat_id": chat_id, "logged_in_at": now}


def _no_lead() -> dict:
    return {"created": False, "lead_id": None, "note_id": None}


def _positive_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        number = int(value.strip())
    else:
        return None
    if number <= 0:
        return None
    return number


def _join_name(user: dict):
    parts = []
    for key in ("first_name", "last_name"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts:
        return None
    return " ".join(parts)


def _username(value):
    if not isinstance(value, str):
        return None
    return value


def _person_from_user(user: dict):
    tg_id = _positive_id(user.get("id"))
    if tg_id is None:
        return None
    return {
        "telegram_user_id": tg_id,
        "name": _join_name(user),
        "username": _username(user.get("username")),
    }


def _hidden_person(name):
    if not isinstance(name, str) or not name.strip():
        return None
    return {"telegram_user_id": None, "name": name.strip(), "username": None}


def _forward_person(message: dict):
    # Имя автора, не оператора. Скрытый профиль без id — пустой username.
    sender = message.get("forward_from")
    if isinstance(sender, dict):
        person = _person_from_user(sender)
        if person is not None:
            return person
    hidden = _hidden_person(message.get("forward_sender_name"))
    if hidden is not None:
        return hidden
    origin = message.get("forward_origin")
    if not isinstance(origin, dict):
        return None
    kind = origin.get("type")
    if kind == "user":
        user = origin.get("sender_user")
        if isinstance(user, dict):
            return _person_from_user(user)
        return None
    if kind == "hidden_user":
        return _hidden_person(origin.get("sender_user_name"))
    return None


def _forward_text(message: dict) -> str:
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _update_id(update: dict):
    value = update.get("update_id")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _lead_for_telegram(conn, telegram_user_id: int):
    row = conn.execute(
        """
        SELECT * FROM leads
        WHERE telegram_user_id = ?
        ORDER BY (account_id IS NOT NULL), id
        LIMIT 1
        """,
        (telegram_user_id,),
    ).fetchone()
    return as_dict(row)


def _remember_update(conn, update_id, lead_id, note_id) -> None:
    if update_id is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO bot_seen_updates (update_id, lead_id, note_id)
        VALUES (?, ?, ?)
        """,
        (update_id, lead_id, note_id),
    )
    conn.commit()


def _save_forward_lead(conn, person: dict) -> tuple:
    tg_id = person["telegram_user_id"]
    if tg_id is not None:
        existing = _lead_for_telegram(conn, tg_id)
        if existing:
            return existing, False
    try:
        lead = create_lead(
            conn,
            telegram_user_id=tg_id,
            username=person["username"],
            name=person["name"],
        )
        return lead, True
    except ValueError as exc:
        if tg_id is None or "уже есть" not in str(exc):
            raise
        existing = _lead_for_telegram(conn, tg_id)
        if existing is None:
            raise
        return existing, False


def apply_forward_update(conn, update: dict) -> dict:
    # Не отвечает пересланному человеку и не шлёт сообщений.
    if not _token():
        raise ValueError(NO_TOKEN)
    expected = _expected_chat()
    if not expected:
        raise ValueError(NO_CHAT)
    if not isinstance(update, dict):
        raise ValueError(BAD_UPDATE)
    message = update.get("message")
    if not isinstance(message, dict):
        return _no_lead()
    if _chat_id(message) != expected:
        return _no_lead()
    person = _forward_person(message)
    if person is None:
        return _no_lead()
    update_id = _update_id(update)
    if update_id is not None:
        seen = conn.execute(
            "SELECT lead_id FROM bot_seen_updates WHERE update_id = ?",
            (update_id,),
        ).fetchone()
        if seen is not None:
            return {"created": False, "lead_id": seen["lead_id"], "note_id": None}
    lead, created = _save_forward_lead(conn, person)
    text = _forward_text(message)
    note_id = None
    if text:
        note = add_note(conn, lead["id"], text, source="forwarded")
        note_id = note["id"]
    _remember_update(conn, update_id, lead["id"], note_id)
    return {"created": created, "lead_id": lead["id"], "note_id": note_id}


def apply_bot_update(conn, update: dict) -> dict:
    status = dict(apply_start_update(conn, update))
    captured = apply_forward_update(conn, update)
    status["lead_id"] = captured["lead_id"]
    status["note_id"] = captured["note_id"]
    status["lead_created"] = captured["created"]
    return status


def apply_start_update(conn, update: dict) -> dict:
    # Пустой токен не пишет сессию и не считается входом.
    if not _token():
        raise ValueError(NO_TOKEN)
    expected = _expected_chat()
    if not expected:
        raise ValueError(NO_CHAT)
    if not isinstance(update, dict):
        raise ValueError(BAD_UPDATE)
    message = update.get("message")
    if not isinstance(message, dict) or not _is_start(message.get("text")):
        status = operator_status(conn)
        status["opened"] = False
        return status
    chat_id = _chat_id(message)
    if chat_id != expected:
        status = operator_status(conn)
        status["opened"] = False
        return status
    return _open(conn, chat_id)


def _read(url: str, payload: dict, opener) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=15) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = getattr(resp, "code", 200)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(REJECTED) from exc
    except urllib.error.URLError as exc:
        raise ValueError(REJECTED) from exc
    if status != 200:
        raise ValueError(REJECTED)
    try:
        parsed = json.loads((raw or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(REJECTED) from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise ValueError(REJECTED)
    return parsed


def poll_bot_login(conn, opener=None) -> dict:
    # Кнопка тянет getUpdates. Не шлёт сообщений и не подтверждает offset:
    # чужие апдейты, включая callback дайджеста, остаются в очереди.
    token = _token()
    if not token:
        raise ValueError(NO_TOKEN)
    if not _expected_chat():
        raise ValueError(NO_CHAT)
    parsed = _read(
        GET_UPDATES.format(token=token),
        {"timeout": 0, "allowed_updates": ["message"]},
        opener,
    )
    result = parsed.get("result")
    if not isinstance(result, list):
        raise ValueError(REJECTED)
    opened = False
    for update in result:
        if not isinstance(update, dict):
            continue
        if apply_bot_update(conn, update).get("opened"):
            opened = True
    status = operator_status(conn)
    status["opened"] = opened
    return status
