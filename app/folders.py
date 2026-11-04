from app.accounts import _load as load_account
from app.db import as_dict, utc_now
from app.leads import add_lead_from_dialog


def list_folders(conn, account_id: int) -> list:
    _require_account(conn, account_id)
    rows = conn.execute(
        """
        SELECT * FROM folders
        WHERE account_id = ?
        ORDER BY title, id
        """,
        (account_id,),
    ).fetchall()
    return [as_dict(row) for row in rows]


async def pull_folders(conn, account_id: int, gateway) -> list:
    account = _connected(conn, account_id)
    remote = await gateway.list_folders(account["session_path"])
    if not isinstance(remote, list):
        raise ValueError("Список папок пустой")
    now = utc_now()
    for item in remote:
        if not isinstance(item, dict) or item.get("telegram_folder_id") is None:
            continue
        _upsert_folder(conn, account_id, item, now)
    conn.commit()
    return list_folders(conn, account_id)


def update_folder(conn, folder_id: int, sync_enabled) -> dict:
    folder = _get(conn, folder_id)
    enabled = 1 if sync_enabled else 0
    if enabled and folder["blocked_reason"]:
        raise ValueError(folder["blocked_reason"])
    conn.execute(
        "UPDATE folders SET sync_enabled = ?, updated_at = ? WHERE id = ?",
        (enabled, utc_now(), folder_id),
    )
    conn.commit()
    return _get(conn, folder_id)


async def sync_folder(conn, folder_id: int, gateway) -> dict:
    folder = _get(conn, folder_id)
    if folder["blocked_reason"]:
        raise ValueError(folder["blocked_reason"])
    account = _connected(conn, folder["account_id"])
    peers = await gateway.folder_peers(account["session_path"], folder["telegram_folder_id"])
    if not isinstance(peers, list):
        raise ValueError("В папке нет списка диалогов")
    added = 0
    lead_ids = []
    seen = set()
    for peer in peers:
        if not _person(peer):
            continue
        telegram_user_id = int(peer["telegram_user_id"])
        if telegram_user_id in seen:
            continue
        seen.add(telegram_user_id)
        existing = conn.execute(
            """
            SELECT id FROM leads
            WHERE account_id = ? AND telegram_user_id = ?
            """,
            (account["id"], telegram_user_id),
        ).fetchone()
        lead = add_lead_from_dialog(
            conn,
            account["id"],
            telegram_user_id,
            peer.get("username"),
            peer.get("name"),
        )
        if existing is None:
            added += 1
        if lead.get("folder_id") != folder["id"]:
            conn.execute(
                "UPDATE leads SET folder_id = ?, updated_at = ? WHERE id = ?",
                (folder["id"], utc_now(), lead["id"]),
            )
            conn.commit()
        lead_ids.append(lead["id"])
    conn.execute(
        "UPDATE folders SET sync_enabled = 1, updated_at = ? WHERE id = ?",
        (utc_now(), folder["id"]),
    )
    conn.commit()
    return {
        "folder_id": folder["id"],
        "added": added,
        "total": len(lead_ids),
        "lead_ids": lead_ids,
    }


def _require_account(conn, account_id: int) -> None:
    row = conn.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise LookupError("account")


def _connected(conn, account_id: int) -> dict:
    account = load_account(conn, account_id)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    return account


def _get(conn, folder_id: int) -> dict:
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise LookupError("folder")
    return as_dict(row)


def _upsert_folder(conn, account_id: int, item: dict, now: str) -> None:
    telegram_folder_id = int(item["telegram_folder_id"])
    title = str(item.get("title") or "").strip() or f"Папка {telegram_folder_id}"
    raw = item.get("blocked_reason")
    reason = str(raw).strip() if raw else None
    if not reason:
        reason = None
    row = conn.execute(
        """
        SELECT id, sync_enabled FROM folders
        WHERE account_id = ? AND telegram_folder_id = ?
        """,
        (account_id, telegram_folder_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO folders (
                account_id, telegram_folder_id, title, sync_enabled,
                blocked_reason, created_at, updated_at
            ) VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (account_id, telegram_folder_id, title, reason, now, now),
        )
        return
    enabled = 0 if reason else int(row["sync_enabled"] or 0)
    conn.execute(
        """
        UPDATE folders
        SET title = ?, blocked_reason = ?, sync_enabled = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, reason, enabled, now, row["id"]),
    )


def _person(peer) -> bool:
    if not isinstance(peer, dict) or peer.get("is_group"):
        return False
    if (peer.get("peer_type") or "user") != "user":
        return False
    return peer.get("telegram_user_id") is not None
