from app.accounts import _load as load_account
from app.db import as_dict, safe_int, utc_now
from app.leads import add_lead_from_dialog


def list_folders(conn, account_id: int) -> list:
    _require_account(conn, account_id)
    rows = conn.execute(
        """
        SELECT f.*,
               (SELECT COUNT(*) FROM leads l WHERE l.folder_id = f.id) AS leads_count
        FROM folders f
        WHERE f.account_id = ?
        ORDER BY f.title, f.id
        """,
        (int(account_id),),
    ).fetchall()
    return [d for d in (as_dict(row) for row in rows) if d]


def _detach_and_delete_folders(conn, folder_ids: list[int]) -> None:
    if not folder_ids:
        return
    sph = ",".join("?" for _ in folder_ids)
    conn.execute(f"UPDATE leads SET folder_id = NULL WHERE folder_id IN ({sph})", folder_ids)
    conn.execute(f"DELETE FROM folders WHERE id IN ({sph})", folder_ids)


async def pull_folders(conn, account_id: int, gateway) -> list:
    account = _connected(conn, account_id)
    try:
        remote = await gateway.list_folders(account["session_path"])
    except Exception:
        existing = list_folders(conn, account_id)
        if existing:
            return existing
        raise
    if not isinstance(remote, list):
        raise ValueError("Список папок пустой")
    now = utc_now()
    remote_tg_ids: list[int] = []
    for item in remote:
        if not isinstance(item, dict):
            continue
        tg_fid = safe_int(item.get("telegram_folder_id"))
        if tg_fid is None:
            continue
        remote_tg_ids.append(tg_fid)
        _upsert_folder(conn, account_id, item, now)
    if remote_tg_ids:
        ph = ",".join("?" for _ in remote_tg_ids)
        stale_rows = conn.execute(
            f"SELECT id FROM folders WHERE account_id = ? AND telegram_folder_id NOT IN ({ph})",
            [account_id, *remote_tg_ids],
        ).fetchall()
        stale_ids = [
            sid for sid in (safe_int(r["id"]) for r in stale_rows if r is not None) if sid is not None
        ]
        _detach_and_delete_folders(conn, stale_ids)
    else:
        stale_rows = conn.execute(
            "SELECT id FROM folders WHERE account_id = ?",
            (account_id,),
        ).fetchall()
        stale_ids = [
            sid for sid in (safe_int(r["id"]) for r in stale_rows if r is not None) if sid is not None
        ]
        _detach_and_delete_folders(conn, stale_ids)
        conn.execute("UPDATE leads SET folder_id = NULL WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM folders WHERE account_id = ?", (account_id,))
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
    try:
        peers = await gateway.folder_peers(account["session_path"], folder["telegram_folder_id"])
    except Exception:
        existing_ids = [
            lid
            for lid in (
                safe_int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM leads WHERE folder_id = ? ORDER BY id",
                    (folder["id"],),
                ).fetchall()
                if r is not None
            )
            if lid is not None
        ]
        if existing_ids:
            conn.execute(
                "UPDATE folders SET sync_enabled = 1, updated_at = ? WHERE id = ?",
                (utc_now(), folder["id"]),
            )
            conn.commit()
            return {
                "folder_id": folder["id"],
                "added": 0,
                "total": len(existing_ids),
                "lead_ids": existing_ids,
            }
        raise
    if not isinstance(peers, list):
        raise ValueError("В папке нет списка диалогов")
    added = 0
    lead_ids = []
    seen = set()
    for peer in peers:
        if not _person(peer):
            continue
        telegram_user_id = safe_int(peer.get("telegram_user_id"))
        if telegram_user_id is None:
            continue
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
        if existing is None and peer.get("username"):
            clean_u = str(peer.get("username") or "").strip().lstrip("@").lower()
            if clean_u:
                unlinked = conn.execute(
                    """
                    SELECT id FROM leads
                    WHERE account_id IS NULL AND telegram_user_id IS NULL AND lower(username) = ?
                    ORDER BY id LIMIT 1
                    """,
                    (clean_u,),
                ).fetchone()
                unlinked_id = safe_int(unlinked["id"]) if unlinked is not None else None
                if unlinked_id is not None:
                    conn.execute(
                        """
                        UPDATE leads
                        SET account_id = ?, telegram_user_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (account["id"], telegram_user_id, utc_now(), unlinked_id),
                    )
                    conn.commit()
                    existing = unlinked
        lead = add_lead_from_dialog(
            conn,
            account["id"],
            telegram_user_id,
            peer.get("username"),
            peer.get("name"),
        )
        lead_id = safe_int(lead.get("id")) if isinstance(lead, dict) else None
        if lead_id is None:
            continue
        if existing is None:
            added += 1
        if lead.get("folder_id") != folder["id"]:
            conn.execute(
                "UPDATE leads SET folder_id = ?, updated_at = ? WHERE id = ?",
                (folder["id"], utc_now(), lead_id),
            )
            conn.commit()
        lead_ids.append(lead_id)
    if lead_ids:
        ph = ",".join("?" for _ in lead_ids)
        conn.execute(
            f"UPDATE leads SET folder_id = NULL, updated_at = ? WHERE folder_id = ? AND id NOT IN ({ph})",
            [utc_now(), folder["id"], *lead_ids],
        )
    else:
        conn.execute(
            "UPDATE leads SET folder_id = NULL, updated_at = ? WHERE folder_id = ?",
            (utc_now(), folder["id"]),
        )
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


async def sync_all_folders(
    conn, account_id: int, gateway, folder_ids: list[int] | None = None
) -> dict:
    if folder_ids is not None:
        target_ids = {
            fid for fid in (safe_int(raw_id) for raw_id in folder_ids) if fid is not None
        }
        folders = (
            [
                f
                for f in list_folders(conn, account_id)
                if safe_int(f.get("id")) in target_ids and not f.get("blocked_reason")
            ]
            if target_ids
            else []
        )
    else:
        pulled = await pull_folders(conn, account_id, gateway)
        folders = [
            f
            for f in pulled
            if isinstance(f, dict)
            and (safe_int(f.get("sync_enabled"), 0) or 0) == 1
            and not f.get("blocked_reason")
        ]
    synced_folders = 0
    total_added = 0
    total_leads = 0
    for f in folders:
        fid = safe_int(f.get("id")) if isinstance(f, dict) else None
        if fid is None or f.get("blocked_reason"):
            continue
        try:
            res = await sync_folder(conn, fid, gateway)
            synced_folders += 1
            total_added += safe_int(res.get("added"), 0) or 0
            total_leads += safe_int(res.get("total"), 0) or 0
        except Exception:
            continue
    return {
        "ok": True,
        "account_id": account_id,
        "synced_folders": synced_folders,
        "added": total_added,
        "total": total_leads,
        "folders": list_folders(conn, account_id),
    }


def _require_account(conn, account_id: int) -> None:
    aid = safe_int(account_id)
    if aid is None:
        raise LookupError("account")
    row = conn.execute("SELECT id FROM accounts WHERE id = ?", (aid,)).fetchone()
    if row is None or safe_int(row["id"]) is None:
        raise LookupError("account")


def _connected(conn, account_id: int) -> dict:
    account = load_account(conn, account_id)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    return account


def _get(conn, folder_id: int) -> dict:
    fid = safe_int(folder_id)
    if fid is None:
        raise LookupError("folder")
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (fid,)).fetchone()
    if row is None or safe_int(row["id"]) is None:
        raise LookupError("folder")
    return as_dict(row)


def _upsert_folder(conn, account_id: int, item: dict, now: str) -> None:
    telegram_folder_id = safe_int(item.get("telegram_folder_id"))
    if telegram_folder_id is None:
        return
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
    row_id = safe_int(row["id"]) if row is not None else None
    if row_id is None:
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
    enabled = 0 if reason else (safe_int(row["sync_enabled"], 0) or 0)
    conn.execute(
        """
        UPDATE folders
        SET title = ?, blocked_reason = ?, sync_enabled = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, reason, enabled, now, row_id),
    )


def _person(peer) -> bool:
    if not isinstance(peer, dict) or peer.get("is_group"):
        return False
    if (peer.get("peer_type") or "user") != "user":
        return False
    return safe_int(peer.get("telegram_user_id")) is not None
