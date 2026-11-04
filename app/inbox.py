import sqlite3

from app.db import as_dict, utc_now
from app.telegram_gateway import GatewayError


def _public(row) -> dict:
    data = as_dict(row)
    return {
        "id": data["id"],
        "account_id": data["account_id"],
        "telegram_user_id": data["peer_id"],
        "username": data["username"],
        "name": data["title"] or "",
        "peer_type": data["peer_type"],
        "unread_count": data["unread_count"],
        "muted": data["muted"],
        "is_group": data["is_group"],
        "last_message_at": data["last_message_at"],
        "last_message_preview": data["last_message_preview"],
        "last_from_peer": data["last_from_peer"],
        "lead_id": data["lead_id"],
    }


def remember_dialogs(conn, account_id: int, people: list) -> list:
    now = utc_now()
    saved = []
    for person in people:
        peer_id = person.get("telegram_user_id")
        if peer_id is None:
            continue
        title = (person.get("name") or "").strip()
        username = person.get("username")
        if username is not None:
            username = str(username).strip().lstrip("@") or None
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title,
                is_group, unread_count, last_from_peer, muted,
                created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, 0, 0, 0, 0, ?, ?)
            ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                username = excluded.username,
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (account_id, int(peer_id), username, title, now, now),
        )
        row = conn.execute(
            """
            SELECT * FROM dialogs
            WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
            """,
            (account_id, int(peer_id)),
        ).fetchone()
        saved.append(_public(row))
    conn.commit()
    return saved


def list_inbox(conn, account_id: int, q: str = "") -> list:
    account = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError("account")
    rows = conn.execute(
        """
        SELECT * FROM dialogs
        WHERE account_id = ? AND peer_type = 'user'
        ORDER BY (last_message_at IS NULL), last_message_at DESC, updated_at DESC, id DESC
        LIMIT 30
        """,
        (account_id,),
    ).fetchall()
    found = [_public(row) for row in rows]
    needle = (q or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if not needle:
        return found
    folded = needle.casefold()
    return [
        row for row in found
        if folded in " ".join((
            row.get("name") or "",
            row.get("username") or "",
            str(row.get("telegram_user_id") or ""),
        )).casefold()
    ]


def list_messages(conn, dialog_id: int) -> list:
    dialog = conn.execute("SELECT id FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    rows = conn.execute(
        """
        SELECT * FROM messages
        WHERE dialog_id = ?
        ORDER BY sent_at, id
        """,
        (dialog_id,),
    ).fetchall()
    return [as_dict(row) for row in rows]


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
    sent = await gateway.send_message(
        account["session_path"], dialog["peer_id"], body, reply_to
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
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)
