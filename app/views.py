import sqlite3

from app.db import STATUSES, as_dict, normalize_status, utc_now, valid_statuses


def list_views(conn) -> list:
    rows = conn.execute(
        """
        SELECT id, name, status, folder_id, created_at
        FROM saved_views
        ORDER BY name, id
        """
    ).fetchall()
    return [as_dict(row) for row in rows]


def save_view(conn, name, status=None, folder_id=None) -> dict:
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("Укажите имя вида")
    if status is None or str(status).strip() == "":
        status = None
    else:
        status = normalize_status(str(status).strip(), conn)
        if status not in valid_statuses(conn):
            raise ValueError("Неизвестный статус")
    if folder_id is None or folder_id == "":
        folder_id = None
    else:
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            raise ValueError("Некорректная папка") from None
        found = conn.execute(
            "SELECT id FROM folders WHERE id = ?",
            (folder_id,),
        ).fetchone()
        if found is None:
            raise ValueError("Папка не найдена")
    existing = conn.execute(
        "SELECT id FROM saved_views WHERE name = ?",
        (cleaned,),
    ).fetchone()
    if existing is None:
        try:
            conn.execute(
                """
                INSERT INTO saved_views (name, status, folder_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cleaned, status, folder_id, utc_now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = conn.execute(
                "SELECT id FROM saved_views WHERE name = ?",
                (cleaned,),
            ).fetchone()
            if existing is None:
                raise ValueError("Не удалось сохранить вид") from None
            _replace_filter(conn, existing["id"], status, folder_id)
    else:
        _replace_filter(conn, existing["id"], status, folder_id)
    row = conn.execute(
        """
        SELECT id, name, status, folder_id, created_at
        FROM saved_views
        WHERE name = ?
        """,
        (cleaned,),
    ).fetchone()
    return as_dict(row)


def _replace_filter(conn, view_id, status, folder_id) -> None:
    conn.execute(
        "UPDATE saved_views SET status = ?, folder_id = ? WHERE id = ?",
        (status, folder_id, view_id),
    )
    conn.commit()
