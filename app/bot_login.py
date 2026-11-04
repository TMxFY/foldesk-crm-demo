import json
import os
import urllib.error
import urllib.request

from app.db import utc_now

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
        if apply_start_update(conn, update).get("opened"):
            opened = True
    status = operator_status(conn)
    status["opened"] = opened
    return status
