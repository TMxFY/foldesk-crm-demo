import re
import sqlite3

from app.accounts import check_daily_limit, increment_daily_sent
from app.db import as_dict, utc_now
from app.telegram_gateway import GatewayError


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
    return out


def stop_outreach_on_reply(
    conn,
    account_id: int | None = None,
    peer_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
    lead_id: int | None = None,
) -> int:
    now = utc_now()
    clean_user = str(username).strip().lstrip("@") if username else None
    clean_phone = str(phone).strip() if phone else None
    phone_digits = re.sub(r"\D", "", clean_phone or "")
    phone_peer_id = int(phone_digits[-12:]) if (phone_digits and len(phone_digits) >= 7) else None
    clean_peer_id = int(peer_id) if peer_id is not None else phone_peer_id
    clean_lead_id = int(lead_id) if lead_id is not None else None

    if not any((clean_lead_id, clean_peer_id, clean_user, clean_phone)):
        return 0

    # Find matching leads first so we can also match campaign_recipients by lead_id
    lead_clauses = []
    lead_params = []
    if clean_lead_id is not None:
        lead_clauses.append("id = ?")
        lead_params.append(clean_lead_id)
    if clean_peer_id is not None:
        lead_clauses.append("telegram_user_id = ?")
        lead_params.append(clean_peer_id)
    if clean_user:
        lead_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
        lead_params.append(clean_user)
    if clean_phone:
        lead_clauses.append("(phone IS NOT NULL AND phone = ?)")
        lead_params.append(clean_phone)

    matched_lead_ids = set()
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

    # Also find matching campaign_recipients
    rec_clauses = []
    rec_params = []
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
    if clean_phone:
        rec_clauses.append("(phone IS NOT NULL AND phone = ?)")
        rec_params.append(clean_phone)

    stopped_count = 0
    if rec_clauses:
        rec_rows = conn.execute(
            f"""
            SELECT id, lead_id, status FROM campaign_recipients
            WHERE status IN ('pending', 'in_progress', 'completed')
              AND ({' OR '.join(rec_clauses)})
            """,
            rec_params,
        ).fetchall()
        for rr in rec_rows:
            if rr["lead_id"]:
                matched_lead_ids.add(int(rr["lead_id"]))
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'replied', updated_at = ?
                WHERE id = ?
                """,
                (now, int(rr["id"])),
            )
            stopped_count += 1

    if matched_lead_ids:
        ph = ",".join("?" for _ in matched_lead_ids)
        conn.execute(
            f"""
            UPDATE leads
            SET status = CASE WHEN status IN ('new', 'waiting') THEN 'talking' ELSE status END,
                next_step = CASE
                    WHEN status IN ('new', 'waiting')
                    THEN 'Клиент ответил — цепочка остановлена, ответьте вручную'
                    ELSE COALESCE(next_step, 'Клиент ответил — цепочка остановлена, ответьте вручную')
                END,
                updated_at = ?
            WHERE id IN ({ph})
            """,
            [now, *sorted(matched_lead_ids)],
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
            )
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
    )
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
                )
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
                COALESCE(d.provider, a.provider, 'telegram') AS provider,
                d.phone AS phone,
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
                COALESCE(d.provider, a.provider, 'telegram') AS provider,
                d.phone AS phone,
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
    return matched


def open_dialog_by_lead(conn, lead_id: int, account_id: int | None = None) -> dict:
    lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if lead_row is None:
        raise LookupError("lead")
    lead = as_dict(lead_row)
    lid = int(lead["id"])
    now = utc_now()

    explicit_acc_id = int(account_id) if (account_id is not None and int(account_id) > 0) else None
    explicit_acc = None
    if explicit_acc_id is not None:
        acc_check = conn.execute("SELECT * FROM accounts WHERE id = ?", (explicit_acc_id,)).fetchone()
        if acc_check is None:
            raise LookupError("account")
        explicit_acc = as_dict(acc_check)

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
                lid,
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
                lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                wa_peer_id,
                wa_peer_id,
                lead_phone,
                lead_phone,
                lead.get("username"),
                lead.get("username"),
                lid,
            ),
        ).fetchone()

    if existing_dlg is not None:
        dlg_id = int(existing_dlg["id"])
        conn.execute(
            """
            UPDATE dialogs
            SET lead_id = ?,
                phone = COALESCE(phone, ?),
                provider = COALESCE(provider, ?)
            WHERE id = ?
            """,
            (lid, lead_phone, wanted_provider, dlg_id),
        )
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

    if dialog_provider == "whatsapp":
        if wa_peer_id is not None:
            peer_id = wa_peer_id
        elif lead.get("telegram_user_id"):
            peer_id = int(lead["telegram_user_id"])
        else:
            peer_id = 900000 + lid
    else:
        peer_id = int(lead.get("telegram_user_id") or (900000 + lid))

    username = (lead.get("username") or "").strip().lstrip("@") or None
    title = (lead.get("name") or "").strip() or username or lead_phone or f"Лид #{lid}"
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
            lead_id = excluded.lead_id,
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
            _, last_dir, last_text, last_sent_at, _ = latest
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
                    (last_sent_at, last_preview, last_preview, now, dialog_id),
                )
            if any(m[1] == "in" for m in normalized):
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
    if peer_id >= 900000 and dialog.get("username") and hasattr(gateway, "search_dialogs"):
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
    if peer_id >= 900000:
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
        if peer_target >= 900000 and dialog.get("username"):
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
            if isinstance(peer_target, int) and peer_target >= 900000:
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
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
            (phone,),
        ).fetchone()

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
            conn.execute(
                "UPDATE leads SET telegram_user_id = ? WHERE id = ?",
                (resolved_tg_id, lead_id),
            )
        conn.commit()

    dialog = open_dialog_by_lead(conn, lead_id, acc_id)
    updated_lead = as_dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
    return {"dialog": dialog, "lead": updated_lead}
