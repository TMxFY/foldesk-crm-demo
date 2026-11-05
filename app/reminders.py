from datetime import datetime, timezone

from app.db import as_dict, utc_now

SCOPES = ("open", "overdue", "all")


def normalize_due(value: str) -> str:
    # Наивная строка — UTC: страница переводит datetime-local до отправки.
    text = (value or "").strip()
    if len(text) >= 11 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if len(text) < 16:
        raise ValueError("Укажите срок")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Укажите срок") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _due_key(value: str) -> str:
    try:
        return normalize_due(value)
    except ValueError:
        return value or ""


from app.events import record_lead_event  # noqa: E402


def create_reminder(conn, lead_id: int, due_at: str, text: str) -> dict:
    lead = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        raise LookupError("lead")
    body = (text or "").strip()
    if not body:
        raise ValueError("Укажите текст напоминания")
    due = normalize_due(due_at)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO reminders (lead_id, due_at, text, done_at, created_at)
        VALUES (?, ?, ?, NULL, ?)
        """,
        (lead_id, due, body, now),
    )
    record_lead_event(
        conn,
        lead_id,
        "reminder_set",
        f"Поставлено напоминание на {due[:16].replace('T', ' ')}",
        {"reminder_id": int(cur.lastrowid), "due_at": due, "text": body},
        created_at=now,
    )
    conn.commit()
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


def list_reminders(conn, scope: str = "open", now: str | None = None) -> list:
    if scope not in SCOPES:
        raise ValueError("Неизвестный scope")
    moment = _due_key(now) if now else utc_now()
    sql = """
        SELECT r.*, l.name AS lead_name, l.username AS lead_username
        FROM reminders r
        JOIN leads l ON l.id = r.lead_id
    """
    if scope in ("open", "overdue"):
        sql += " WHERE r.done_at IS NULL"
    sql += " ORDER BY r.due_at, r.id"
    rows = [as_dict(row) for row in conn.execute(sql)]
    if scope != "overdue":
        return rows
    overdue = [row for row in rows if _due_key(row["due_at"]) <= moment]
    overdue.sort(key=lambda row: (_due_key(row["due_at"]), row["id"]))
    return overdue


def postpone_reminder(conn, reminder_id: int, due_at: str) -> dict:
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        raise LookupError("reminder")
    if row["done_at"] is not None:
        raise ValueError("Напоминание уже закрыто")
    due = normalize_due(due_at)
    conn.execute(
        "UPDATE reminders SET due_at = ? WHERE id = ?",
        (due, reminder_id),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    return as_dict(updated)


def complete_reminder(conn, reminder_id: int) -> dict:
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        raise LookupError("reminder")
    if row["done_at"] is None:
        conn.execute(
            "UPDATE reminders SET done_at = ? WHERE id = ?",
            (utc_now(), reminder_id),
        )
        conn.commit()
    updated = conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()
    return as_dict(updated)
