import sqlite3

from app.db import as_dict, utc_now
from app.events import record_lead_event

SOURCES = ("manual", "forwarded")


def add_note(conn, lead_id: int, body: str, source: str = "manual") -> dict:
    lead = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        raise LookupError("lead")
    text = (body or "").strip()
    if not text:
        raise ValueError("Напишите текст")
    if source not in SOURCES:
        raise ValueError("Неизвестный источник заметки")
    now = utc_now()
    try:
        cur = conn.execute(
            """
            INSERT INTO notes (lead_id, body, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead_id, text, source, now, now),
        )
        record_lead_event(
            conn,
            lead_id,
            "note_added",
            "Добавлена заметка в карточку",
            {"note_id": int(cur.lastrowid), "source": source, "body": text[:200]},
            created_at=now,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Некорректная заметка") from exc
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


def list_notes(conn, lead_id: int) -> list:
    rows = conn.execute(
        """
        SELECT * FROM notes
        WHERE lead_id = ?
        ORDER BY created_at, id
        """,
        (lead_id,),
    )
    return [as_dict(row) for row in rows]


def notes_for_card(conn, lead: dict) -> list:
    rows = list_notes(conn, lead["id"])
    if rows:
        return rows
    legacy = (lead.get("note") or "").strip()
    if not legacy:
        return []
    when = lead.get("updated_at") or lead.get("created_at")
    return [{
        "id": None,
        "lead_id": lead["id"],
        "body": legacy,
        "source": "legacy",
        "created_at": when,
        "updated_at": when,
        "stored": False,
    }]
