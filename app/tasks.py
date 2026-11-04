import sqlite3

from app.db import as_dict, utc_now


def create_task(conn, lead_id: int, title: str) -> dict:
    # schema.sql tasks has no due_at; the date stays on reminders.
    lead = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        raise LookupError("lead")
    text = (title or "").strip()
    if not text:
        raise ValueError("Напишите задачу")
    now = utc_now()
    try:
        cur = conn.execute(
            """
            INSERT INTO tasks (lead_id, title, done_at, created_at, updated_at)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (lead_id, text, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Некорректная задача") from exc
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


def list_tasks(conn, lead_id: int) -> list:
    rows = conn.execute(
        """
        SELECT * FROM tasks
        WHERE lead_id = ?
        ORDER BY created_at, id
        """,
        (lead_id,),
    )
    return [as_dict(row) for row in rows]


def set_task_done(conn, task_id: int, done: bool = True) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise LookupError("task")
    now = utc_now()
    done_at = now if done else None
    conn.execute(
        "UPDATE tasks SET done_at = ?, updated_at = ? WHERE id = ?",
        (done_at, now, task_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return as_dict(updated)
