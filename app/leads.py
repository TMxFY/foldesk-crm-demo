import sqlite3

from app.db import STATUSES, as_dict, utc_now
from app.notes import notes_for_card
from app.properties import list_lead_properties
from app.tasks import list_tasks


def _clean_username(username):
    if username is None:
        return None
    value = str(username).strip()
    if value.startswith("@"):
        value = value[1:]
    return value or None


def _blank(value):
    if value is None:
        return None
    return str(value).strip() or None


def _find_dialog_lead(conn, account_id, telegram_user_id):
    row = conn.execute(
        """
        SELECT * FROM leads
        WHERE account_id = ? AND telegram_user_id = ?
        """,
        (account_id, telegram_user_id),
    ).fetchone()
    return as_dict(row)


def create_lead(
    conn,
    *,
    account_id=None,
    telegram_user_id=None,
    username=None,
    name=None,
    phone=None,
    status="new",
    note=None,
    next_step=None,
    description=None,
    email=None,
    url=None,
) -> dict:
    if status not in STATUSES:
        raise ValueError("Неизвестный статус")
    username = _clean_username(username)
    name = (name or "").strip() or None
    phone = (phone or "").strip() or None
    note = _blank(note)
    next_step = None if next_step is None else str(next_step)
    description = _blank(description)
    email = _blank(email)
    url = _blank(url)
    if not any((name, username, phone, telegram_user_id)):
        raise ValueError("Укажите имя, username, телефон или telegram id")
    now = utc_now()
    try:
        cur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, phone,
                status, note, next_step, created_at, updated_at,
                description, email, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                telegram_user_id,
                username,
                name,
                phone,
                status,
                note,
                next_step,
                now,
                now,
                description,
                email,
                url,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        message = str(exc).upper()
        if "UNIQUE" in message:
            raise ValueError("Лид с таким telegram id уже есть") from exc
        raise ValueError("Некорректные данные лида") from exc
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


def get_lead(conn, lead_id: int) -> dict:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    return as_dict(row)


def _folder_title(conn, folder_id):
    if not folder_id:
        return None
    found = conn.execute(
        "SELECT title FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    if found is None:
        return None
    return found["title"]


def _own_folder(conn, lead_row, folder_id):
    if folder_id is None or folder_id == "":
        return None
    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        raise ValueError("Некорректная папка") from None
    folder = conn.execute(
        "SELECT id, account_id FROM folders WHERE id = ?",
        (folder_id,),
    ).fetchone()
    if folder is None:
        raise ValueError("Папка не найдена")
    account_id = lead_row["account_id"]
    if account_id is None:
        raise ValueError("У лида нет аккаунта")
    if int(folder["account_id"]) != int(account_id):
        raise ValueError("Папка другого аккаунта")
    return int(folder["id"])


def lead_card(conn, lead_id: int) -> dict:
    lead = get_lead(conn, lead_id)
    card = dict(lead)
    card["folder_name"] = _folder_title(conn, lead.get("folder_id"))
    card["notes"] = notes_for_card(conn, lead)
    card["tasks"] = list_tasks(conn, lead_id)
    card["properties"] = list_lead_properties(conn, lead_id)
    return card


def update_lead(conn, lead_id: int, **fields) -> dict:
    allowed = {
        "status", "note", "next_step", "name", "description", "email", "url", "folder_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("Нельзя менять: " + ", ".join(sorted(unknown)))
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    if not fields:
        return as_dict(row)
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError("Неизвестный статус")
    if "folder_id" in fields:
        fields["folder_id"] = _own_folder(conn, row, fields["folder_id"])
    if "name" in fields and fields["name"] is not None:
        fields["name"] = str(fields["name"]).strip() or None
    for key in ("description", "email", "url"):
        if key in fields and fields[key] is not None:
            fields[key] = str(fields[key]).strip() or None
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(utc_now())
    values.append(lead_id)
    conn.execute(
        f"UPDATE leads SET {assignments}, updated_at = ? WHERE id = ?",
        values,
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return as_dict(updated)


def search_leads(
    conn, q: str = "", status: str | None = None, folder_id: int | None = None
) -> list:
    if status is not None and status not in STATUSES:
        raise ValueError("Неизвестный статус")
    if folder_id is not None and folder_id != "":
        try:
            folder_id = int(folder_id)
        except (TypeError, ValueError):
            raise ValueError("Некорректная папка") from None
    else:
        folder_id = None
    sql = "SELECT * FROM leads"
    params = []
    clauses = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if folder_id is not None:
        clauses.append("folder_id = ?")
        params.append(folder_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = [as_dict(row) for row in conn.execute(sql, params)]
    needle = (q or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if not needle:
        return rows
    folded = needle.casefold()

    def matches(lead: dict) -> bool:
        blob = " ".join(
            (
                lead.get("name") or "",
                lead.get("username") or "",
                lead.get("phone") or "",
                str(lead.get("telegram_user_id") or ""),
                lead.get("email") or "",
                lead.get("url") or "",
            )
        )
        return folded in blob.casefold()

    return [lead for lead in rows if matches(lead)]


def add_lead_from_dialog(
    conn, account_id, telegram_user_id, username=None, name=None
) -> dict:
    if account_id is None or telegram_user_id is None:
        raise ValueError("Нужны account_id и telegram_user_id")
    existing = _find_dialog_lead(conn, account_id, telegram_user_id)
    if existing:
        return existing
    try:
        return create_lead(
            conn,
            account_id=account_id,
            telegram_user_id=telegram_user_id,
            username=username,
            name=name,
            status="new",
        )
    except ValueError:
        existing = _find_dialog_lead(conn, account_id, telegram_user_id)
        if existing:
            return existing
        raise
