import json
import os
import re
import urllib.error
import urllib.request

from app.db import as_dict, utc_now
from app.digest import (
    SEND_URL,
    _post as _digest_post,
    ensure_bot_reply_notifications_table,
    resolve_colleague_bot_credentials,
)
from app.inbox import open_dialog_by_lead, send_reply
from app.leads import create_lead
from app.notes import add_note

GET_UPDATES = "https://api.telegram.org/bot{token}/getUpdates"
NO_TOKEN = "Нет BOT_TOKEN"
NO_CHAT = "Нет BOT_CHAT_ID"
BAD_UPDATE = "Не апдейт"
REJECTED = "Telegram не принял запрос"


def _token(conn=None) -> str:
    token, _ = resolve_colleague_bot_credentials(conn, default_chat_id=False)
    return token


def _expected_chat(conn=None) -> str:
    _, chat_id = resolve_colleague_bot_credentials(conn, default_chat_id=False)
    return chat_id


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
    else:
        uname = (person.get("username") or "").strip().lstrip("@")
        if uname:
            row = conn.execute(
                "SELECT * FROM leads WHERE lower(username) = lower(?) ORDER BY id LIMIT 1",
                (uname,),
            ).fetchone()
            if row is not None:
                return as_dict(row), False
        name = (person.get("name") or "").strip()
        if name:
            row = conn.execute(
                "SELECT * FROM leads WHERE telegram_user_id IS NULL AND name = ? ORDER BY id LIMIT 1",
                (name,),
            ).fetchone()
            if row is not None:
                return as_dict(row), False
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
    if not _token(conn):
        raise ValueError(NO_TOKEN)
    expected = _expected_chat(conn)
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
    if not _token(conn):
        raise ValueError(NO_TOKEN)
    expected = _expected_chat(conn)
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
    token = _token(conn)
    if not token:
        raise ValueError(NO_TOKEN)
    if not _expected_chat(conn):
        raise ValueError(NO_CHAT)
    params: dict = {"timeout": 0, "allowed_updates": ["message"]}
    row = conn.execute("SELECT MAX(update_id) FROM bot_seen_updates").fetchone()
    if row and row[0] is not None:
        params["offset"] = int(row[0]) + 1
    parsed = _read(
        GET_UPDATES.format(token=token),
        params,
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


def _resolve_account_id_from_handle(conn, acc_handle: str | None) -> int | None:
    clean = str(acc_handle or "").strip()
    if not clean or clean.lower() == "unknown":
        return None
    if clean.startswith("#") and clean[1:].isdigit():
        return int(clean[1:])
    if clean.startswith("@"):
        uname = clean[1:].strip()
        row = conn.execute(
            "SELECT id FROM accounts WHERE telegram_username IS NOT NULL AND lower(ltrim(telegram_username, '@')) = lower(?) ORDER BY id LIMIT 1",
            (uname,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    row = conn.execute(
        """
        SELECT id FROM accounts
        WHERE phone = ?
           OR telegram_name = ?
           OR (telegram_username IS NOT NULL AND lower(ltrim(telegram_username, '@')) = lower(?))
        ORDER BY id LIMIT 1
        """,
        (clean, clean, clean.lstrip("@")),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _resolve_dialog_id_for_bot_reply(
    conn,
    *,
    dialog_id: int | None = None,
    lead_id: int | None = None,
    account_id: int | None = None,
    peer_id: int | None = None,
    lead_handle: str | None = None,
    account_handle: str | None = None,
) -> int | None:
    if dialog_id is not None:
        drow = conn.execute("SELECT id FROM dialogs WHERE id = ?", (int(dialog_id),)).fetchone()
        if drow is not None:
            return int(drow["id"])

    resolved_acc_id = account_id if account_id is not None else _resolve_account_id_from_handle(conn, account_handle)
    clean_lead = str(lead_handle or "").strip()
    uname: str | None = None
    phone: str | None = None
    name: str | None = None
    resolved_peer_id = peer_id

    if clean_lead and clean_lead.lower() != "unknown":
        if clean_lead.startswith("@"):
            uname = clean_lead[1:].strip() or None
        elif clean_lead.startswith("id:") and clean_lead[3:].strip().isdigit():
            resolved_peer_id = int(clean_lead[3:].strip())
        elif clean_lead.startswith("+") or (clean_lead.isdigit() and len(clean_lead) >= 7):
            phone = clean_lead
        else:
            name = clean_lead

    if resolved_peer_id is not None:
        if resolved_acc_id is not None:
            row = conn.execute(
                "SELECT id FROM dialogs WHERE account_id = ? AND peer_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
                (int(resolved_acc_id), int(resolved_peer_id)),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        row = conn.execute(
            "SELECT id FROM dialogs WHERE peer_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (int(resolved_peer_id),),
        ).fetchone()
        if row is not None:
            return int(row["id"])

    if uname:
        if resolved_acc_id is not None:
            row = conn.execute(
                "SELECT id FROM dialogs WHERE account_id = ? AND username IS NOT NULL AND lower(ltrim(username, '@')) = lower(?) ORDER BY last_message_at DESC, id DESC LIMIT 1",
                (int(resolved_acc_id), uname),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        row = conn.execute(
            "SELECT id FROM dialogs WHERE username IS NOT NULL AND lower(ltrim(username, '@')) = lower(?) ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (uname,),
        ).fetchone()
        if row is not None:
            return int(row["id"])

    if lead_id is not None:
        row = conn.execute(
            "SELECT id FROM dialogs WHERE lead_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (int(lead_id),),
        ).fetchone()
        if row is not None:
            return int(row["id"])

    if phone:
        row = conn.execute(
            "SELECT id FROM dialogs WHERE phone = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (phone,),
        ).fetchone()
        if row is not None:
            return int(row["id"])

    if name:
        row = conn.execute(
            "SELECT id FROM dialogs WHERE title = ? OR (username IS NOT NULL AND lower(ltrim(username, '@')) = lower(?)) ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (name, name),
        ).fetchone()
        if row is not None:
            return int(row["id"])

    try:
        opened = open_dialog_by_lead(
            conn,
            lead_id=lead_id,
            account_id=resolved_acc_id,
            telegram_user_id=resolved_peer_id,
            username=uname,
            phone=phone,
            name=name,
        )
        if isinstance(opened, dict) and opened.get("id"):
            return int(opened["id"])
    except Exception:
        return None
    return None


async def handle_bot_reply_to_lead(conn, update: dict, gateway=None, opener=None) -> dict:
    if not isinstance(update, dict):
        return {"ok": True, "replied": False, "dialog_id": None}
    message = update.get("message")
    if not isinstance(message, dict):
        return {"ok": True, "replied": False, "dialog_id": None}
    reply_text = str(message.get("text") or "").strip()
    if not reply_text or _is_start(reply_text) or _forward_person(message) is not None:
        return {"ok": True, "replied": False, "dialog_id": None}

    chat_id = _chat_id(message) or ""
    dialog_id: int | None = None
    used_active_key = False

    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict):
        ensure_bot_reply_notifications_table(conn)
        reply_msg_id = _positive_id(reply_to.get("message_id"))
        notif_row = None
        if reply_msg_id is not None:
            if chat_id:
                notif_row = conn.execute(
                    "SELECT * FROM bot_reply_notifications WHERE chat_id = ? AND bot_message_id = ?",
                    (chat_id, reply_msg_id),
                ).fetchone()
            if notif_row is None:
                notif_row = conn.execute(
                    "SELECT * FROM bot_reply_notifications WHERE bot_message_id = ? ORDER BY created_at DESC LIMIT 1",
                    (reply_msg_id,),
                ).fetchone()
        if notif_row is not None:
            dialog_id = _resolve_dialog_id_for_bot_reply(
                conn,
                dialog_id=notif_row["dialog_id"],
                lead_id=notif_row["lead_id"],
                account_id=notif_row["account_id"],
                peer_id=notif_row["peer_id"],
                lead_handle=notif_row["lead_handle"],
                account_handle=notif_row["account_handle"],
            )
        if dialog_id is None:
            orig_text = str(reply_to.get("text") or "")
            m_lead = re.search(r"You received a response from\s+(?P<lead>[^\r\n]+)", orig_text)
            m_acc = re.search(r"Account:\s*(?P<acc>[^\r\n]+)", orig_text)
            if m_lead:
                dialog_id = _resolve_dialog_id_for_bot_reply(
                    conn,
                    lead_handle=m_lead.group("lead").strip(),
                    account_handle=m_acc.group("acc").strip() if m_acc else None,
                )

    if dialog_id is None:
        candidate_keys = []
        if chat_id:
            candidate_keys.append(f"bot_active_reply_dialog_{chat_id}")
        candidate_keys.append("bot_active_reply_dialog_default")
        for key in candidate_keys:
            row = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                (key,),
            ).fetchone()
            if row and str(row["value"] or "").strip().isdigit():
                dialog_id = int(str(row["value"]).strip())
                used_active_key = True
                break

    if dialog_id is None:
        return {"ok": True, "replied": False, "dialog_id": None}

    sent_msg = await send_reply(conn, int(dialog_id), reply_text, gateway)

    if used_active_key:
        keys_to_clear = ["bot_active_reply_dialog_default"]
        if chat_id:
            keys_to_clear.append(f"bot_active_reply_dialog_{chat_id}")
        for k in set(keys_to_clear):
            conn.execute("DELETE FROM kv_settings WHERE key = ?", (k,))
        conn.commit()

    dlg_row = conn.execute(
        "SELECT username, title, phone FROM dialogs WHERE id = ?",
        (int(dialog_id),),
    ).fetchone()
    lead_label = f"#{dialog_id}"
    if dlg_row is not None:
        if dlg_row["username"]:
            lead_label = f"@{str(dlg_row['username']).strip().lstrip('@')}"
        elif dlg_row["title"]:
            lead_label = str(dlg_row["title"]).strip()
        elif dlg_row["phone"]:
            lead_label = str(dlg_row["phone"]).strip()

    token, cfg_chat_id = resolve_colleague_bot_credentials(conn)
    target_chat = chat_id or cfg_chat_id
    if token and target_chat:
        try:
            _digest_post(
                SEND_URL.format(token=token),
                {"chat_id": target_chat, "text": f"✅ Ответ отправлен для {lead_label}"},
                opener,
            )
        except Exception:
            pass

    return {
        "ok": True,
        "replied": True,
        "dialog_id": int(dialog_id),
        "message": sent_msg,
    }
