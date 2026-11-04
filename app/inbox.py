import sqlite3

from app.accounts import check_daily_limit, increment_daily_sent
from app.db import as_dict, utc_now
from app.telegram_gateway import GatewayError


def _public(row) -> dict:
    data = as_dict(row)
    out = {
        "id": data["id"],
        "dialog_id": data["id"],
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
    if "account_phone" in data:
        out["account_phone"] = data["account_phone"]
    if "account_name" in data:
        out["account_name"] = data["account_name"]
    if "account_username" in data:
        out["account_username"] = data["account_username"]
    return out


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


def apply_incoming(conn, account_id: int, message: dict) -> dict:
    if not isinstance(message, dict) or message.get("is_group"):
        raise ValueError("Это не личный диалог")
    if (message.get("peer_type") or "user") != "user":
        raise ValueError("Это не личный диалог")
    if (message.get("direction") or "in") != "in":
        raise ValueError("Нужно входящее")
    peer_id = message.get("telegram_user_id")
    if peer_id is None:
        raise ValueError("Это не личный диалог")
    account = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError("account")
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
            account_id, peer_type, peer_id, username, title,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, 0, 1, ?, ?, 1, 0, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
            unread_count = dialogs.unread_count + 1,
            last_message_at = excluded.last_message_at,
            last_message_preview = excluded.last_message_preview,
            last_from_peer = 1,
            updated_at = excluded.updated_at
        """,
        (account_id, int(peer_id), username, title, sent_at, preview, now, now),
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
    conn.commit()
    return _public(row)


def sync_inbox(conn, account_id: int, people: list) -> list:
    account = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise LookupError("account")
    now = utc_now()
    saved = []
    try:
        for person in people or []:
            if not isinstance(person, dict) or person.get("is_group"):
                continue
            if (person.get("peer_type") or "user") != "user":
                continue
            peer_id = person.get("telegram_user_id")
            if peer_id is None:
                continue
            title = (person.get("name") or "").strip()
            username = person.get("username")
            if username is not None:
                username = str(username).strip().lstrip("@") or None
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
                    account_id, peer_type, peer_id, username, title,
                    is_group, unread_count, last_message_at, last_message_preview,
                    last_from_peer, muted, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                    username = COALESCE(excluded.username, dialogs.username),
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
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
            saved.append(_public(row))
        conn.commit()
    except (sqlite3.IntegrityError, TypeError, ValueError) as exc:
        conn.rollback()
        raise ValueError("Не удалось записать входящие") from exc
    return saved


def list_inbox(conn, account_id: int | None = None, q: str = "") -> list:
    if account_id is None or int(account_id) == 0:
        rows = conn.execute(
            """
            SELECT
                d.*,
                a.phone AS account_phone,
                COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.peer_type = 'user'
            ORDER BY (d.last_message_at IS NULL), d.last_message_at DESC, d.updated_at DESC, d.id DESC
            LIMIT 60
            """
        ).fetchall()
    else:
        acc_id = int(account_id)
        account = conn.execute("SELECT id FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        if account is None:
            raise LookupError("account")
        rows = conn.execute(
            """
            SELECT
                d.*,
                a.phone AS account_phone,
                COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.account_id = ? AND d.peer_type = 'user'
            ORDER BY (d.last_message_at IS NULL), d.last_message_at DESC, d.updated_at DESC, d.id DESC
            LIMIT 30
            """,
            (acc_id,),
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
            row.get("account_name") or "",
            row.get("account_phone") or "",
        )).casefold()
    ]


def open_dialog_by_lead(conn, lead_id: int, account_id: int | None = None) -> dict:
    lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if lead_row is None:
        raise LookupError("lead")
    lead = as_dict(lead_row)
    lid = int(lead["id"])
    now = utc_now()

    explicit_acc_id = int(account_id) if (account_id is not None and int(account_id) > 0) else None
    if explicit_acc_id is not None:
        acc_check = conn.execute("SELECT * FROM accounts WHERE id = ?", (explicit_acc_id,)).fetchone()
        if acc_check is None:
            raise LookupError("account")

    # 1. Try existing dialog on requested account (or any account if not specified)
    existing_dlg = None
    if explicit_acc_id is not None:
        existing_dlg = conn.execute(
            """
            SELECT d.*, a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.account_id = ?
              AND d.peer_type = 'user'
              AND (
                  d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY d.id LIMIT 1
            """,
            (
                explicit_acc_id,
                lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                lead.get("username"),
                lead.get("username"),
            ),
        ).fetchone()
    else:
        existing_dlg = conn.execute(
            """
            SELECT d.*, a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.peer_type = 'user'
              AND (
                  d.id = ?
                  OR d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY (d.lead_id = ?) DESC, d.id LIMIT 1
            """,
            (
                lead.get("dialog_id") or -1,
                lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                lead.get("username"),
                lead.get("username"),
                lid,
            ),
        ).fetchone()

    if existing_dlg is not None:
        dlg_id = int(existing_dlg["id"])
        conn.execute("UPDATE dialogs SET lead_id = ? WHERE id = ?", (lid, dlg_id))
        conn.execute("UPDATE leads SET dialog_id = ? WHERE id = ?", (dlg_id, lid))
        conn.commit()
        refreshed = conn.execute(
            """
            SELECT d.*, a.phone AS account_phone,
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
            "SELECT id FROM accounts WHERE id = ? AND status = 'connected'",
            (int(lead["account_id"]),),
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

    peer_id = int(lead.get("telegram_user_id") or (900000 + lid))
    username = (lead.get("username") or "").strip().lstrip("@") or None
    title = (lead.get("name") or "").strip() or username or lead.get("phone") or f"Лид #{lid}"
    preview = (lead.get("note") or lead.get("description") or "Контакт из CRM · готов к диалогу")[:120]

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, 0, 0, ?, ?, 0, 0, ?, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN dialogs.title IS NULL OR dialogs.title = '' THEN excluded.title ELSE dialogs.title END,
            lead_id = excluded.lead_id,
            updated_at = excluded.updated_at
        """,
        (target_acc_id, peer_id, username, title, now, preview, lid, now, now),
    )
    created_row = conn.execute(
        """
        SELECT d.*, a.phone AS account_phone,
               COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
               a.telegram_username AS account_username
        FROM dialogs d
        JOIN accounts a ON a.id = d.account_id
        WHERE d.account_id = ? AND d.peer_type = 'user' AND d.peer_id = ?
        """,
        (target_acc_id, peer_id),
    ).fetchone()
    dlg_id = int(created_row["id"])
    conn.execute(
        """
        UPDATE leads
        SET dialog_id = ?,
            account_id = COALESCE(account_id, ?),
            telegram_user_id = COALESCE(telegram_user_id, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (dlg_id, target_acc_id, peer_id, now, lid),
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
    fetched = await gateway.fetch_messages(account["session_path"], dialog["peer_id"], cap)
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
    increment_daily_sent(conn, account["id"])
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)
