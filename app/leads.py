from datetime import datetime, timedelta, timezone
import re
import sqlite3

from app.db import STATUSES, as_dict, utc_now
from app.notes import notes_for_card
from app.properties import list_lead_properties
from app.tasks import list_tasks

PROVIDERS = ("telegram", "whatsapp")


def _add_hours_iso(iso_str: str | None, hours: int | None) -> str | None:
    if not iso_str:
        return None
    s = str(iso_str).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except ValueError:
        return None
    target = dt + timedelta(hours=max(0, int(hours or 0)))
    return target.strftime("%Y-%m-%dT%H:%M:%S")


def _enrich_lead(conn, data: dict | None) -> dict | None:
    if data is None:
        return None
    data["provider"] = data.get("provider") or "telegram"
    lid = data.get("id")
    acc_id = data.get("account_id")
    tg_id = data.get("telegram_user_id")
    username = data.get("username")
    phone = data.get("phone")
    dialog_id = data.get("dialog_id")

    # 1. Campaign recipient info
    rec_clauses = []
    rec_params = []
    if lid is not None:
        rec_clauses.append("cr.lead_id = ?")
        rec_params.append(int(lid))
    if tg_id is not None:
        rec_clauses.append("cr.telegram_user_id = ?")
        rec_params.append(int(tg_id))
    if username:
        rec_clauses.append("(cr.username IS NOT NULL AND lower(cr.username) = lower(?))")
        rec_params.append(str(username))
    if phone:
        rec_clauses.append("(cr.phone IS NOT NULL AND cr.phone = ?)")
        rec_params.append(str(phone))

    rec_row = None
    if rec_clauses:
        rec_row = conn.execute(
            f"""
            SELECT cr.*,
                   (
                       SELECT cs.delay_hours
                       FROM campaign_steps cs
                       WHERE cs.campaign_id = cr.campaign_id
                         AND cs.step_order = cr.current_step + 1
                       LIMIT 1
                   ) AS next_delay_hours
            FROM campaign_recipients cr
            WHERE {' OR '.join(rec_clauses)}
            ORDER BY (cr.lead_id = ?) DESC, cr.updated_at DESC, cr.id DESC
            LIMIT 1
            """,
            [*rec_params, int(lid) if lid is not None else -1],
        ).fetchone()

    if rec_row is not None:
        rec = as_dict(rec_row)
        data["campaign_status"] = rec.get("status")
        data["campaign_step"] = rec.get("current_step")
        data["last_sent_at"] = rec.get("last_sent_at")
        if rec.get("assigned_account_id") and not acc_id:
            acc_id = rec["assigned_account_id"]
        if (
            rec.get("status") == "in_progress"
            and rec.get("last_sent_at")
            and rec.get("next_delay_hours") is not None
        ):
            delay_h = int(rec["next_delay_hours"] or 0)
            if delay_h > 0:
                data["next_followup_at"] = _add_hours_iso(rec["last_sent_at"], delay_h)
            else:
                data["next_followup_at"] = rec["last_sent_at"]
        else:
            data["next_followup_at"] = None
    else:
        data["campaign_status"] = None
        data["campaign_step"] = None
        data["last_sent_at"] = None
        data["next_followup_at"] = None

    # 2. Dialog info
    dlg_clauses = []
    dlg_params = []
    if dialog_id is not None:
        dlg_clauses.append("d.id = ?")
        dlg_params.append(int(dialog_id))
    if lid is not None:
        dlg_clauses.append("d.lead_id = ?")
        dlg_params.append(int(lid))
    if tg_id is not None:
        dlg_clauses.append("d.peer_id = ?")
        dlg_params.append(int(tg_id))
    if username:
        dlg_clauses.append("(d.username IS NOT NULL AND lower(d.username) = lower(?))")
        dlg_params.append(str(username))
    if phone:
        dlg_clauses.append("(d.phone IS NOT NULL AND d.phone = ?)")
        dlg_params.append(str(phone))

    dlg_row = None
    if dlg_clauses:
        dlg_row = conn.execute(
            f"""
            SELECT d.id, d.account_id, d.last_message_at, d.last_message_preview, d.last_from_peer
            FROM dialogs d
            WHERE d.peer_type = 'user' AND ({' OR '.join(dlg_clauses)})
            ORDER BY (d.id = ?) DESC, (d.lead_id = ?) DESC, d.last_message_at DESC, d.id DESC
            LIMIT 1
            """,
            [
                *dlg_params,
                int(dialog_id) if dialog_id is not None else -1,
                int(lid) if lid is not None else -1,
            ],
        ).fetchone()

    if dlg_row is not None:
        dlg = as_dict(dlg_row)
        data["last_message_at"] = dlg.get("last_message_at")
        data["last_message_preview"] = dlg.get("last_message_preview")
        data["last_from_peer"] = dlg.get("last_from_peer")
        if dlg.get("account_id") and not acc_id:
            acc_id = dlg["account_id"]
    else:
        data["last_message_at"] = None
        data["last_message_preview"] = None
        data["last_from_peer"] = None

    # 3. Account info
    if acc_id is not None:
        acc_row = conn.execute(
            "SELECT phone, telegram_name, telegram_username FROM accounts WHERE id = ?",
            (int(acc_id),),
        ).fetchone()
        if acc_row is not None:
            acc = as_dict(acc_row)
            data["account_phone"] = acc.get("phone")
            data["account_name"] = (
                acc.get("telegram_name")
                or acc.get("telegram_username")
                or acc.get("phone")
            )
        else:
            data["account_phone"] = None
            data["account_name"] = None
    else:
        data["account_phone"] = None
        data["account_name"] = None

    return data


def _public(row, conn=None) -> dict | None:
    data = as_dict(row)
    if data is None:
        return None
    data["provider"] = data.get("provider") or "telegram"
    if conn is not None:
        return _enrich_lead(conn, data)
    return data


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


def _account_provider(conn, account_id) -> str:
    if account_id is None:
        return "telegram"
    try:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
    except Exception:
        row = None
    if row is None:
        return "telegram"
    data = as_dict(row)
    prov = (data.get("provider") or "telegram").strip().lower()
    return prov if prov in PROVIDERS else "telegram"


def _resolve_provider(conn, provider: str | None, account_id=None) -> str:
    if provider is not None and str(provider).strip():
        prov = str(provider).strip().lower()
        if prov not in PROVIDERS:
            raise ValueError("Неизвестный провайдер")
        return prov
    return _account_provider(conn, account_id)


def _phone_digits_id(phone: str | None) -> int | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) >= 7:
        return int(digits[-12:])
    return None


def _find_dialog_lead(conn, account_id, telegram_user_id=None, phone=None, provider=None):
    if telegram_user_id is not None:
        row = conn.execute(
            """
            SELECT * FROM leads
            WHERE account_id IS ? AND telegram_user_id = ?
            ORDER BY id LIMIT 1
            """,
            (account_id, telegram_user_id),
        ).fetchone()
        if row is not None:
            return _public(row, conn)
    if phone and (provider == "whatsapp" or telegram_user_id is None):
        row = conn.execute(
            """
            SELECT * FROM leads
            WHERE account_id IS ? AND phone = ?
            ORDER BY id LIMIT 1
            """,
            (account_id, phone),
        ).fetchone()
        if row is not None:
            return _public(row, conn)
    return None


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
    provider: str | None = None,
) -> dict:
    if status not in STATUSES:
        raise ValueError("Неизвестный статус")
    resolved_provider = _resolve_provider(conn, provider, account_id)
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

    if resolved_provider == "whatsapp":
        if telegram_user_id is None and phone:
            telegram_user_id = _phone_digits_id(phone)
        if phone:
            dup = conn.execute(
                """
                SELECT id FROM leads
                WHERE account_id IS ? AND phone = ? AND COALESCE(provider, 'telegram') = 'whatsapp'
                LIMIT 1
                """,
                (account_id, phone),
            ).fetchone()
            if dup is not None:
                raise ValueError("Лид с таким телефоном уже есть")

    now = utc_now()
    try:
        cur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, phone,
                status, note, next_step, created_at, updated_at,
                description, email, url, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                resolved_provider,
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
    return _public(row, conn)


def get_lead(conn, lead_id: int) -> dict:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    return _public(row, conn)


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
        "phone", "provider",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("Нельзя менять: " + ", ".join(sorted(unknown)))
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    if not fields:
        return _public(row, conn)
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError("Неизвестный статус")
    if "provider" in fields and fields["provider"] is not None:
        fields["provider"] = _resolve_provider(conn, fields["provider"], row["account_id"])
    if "folder_id" in fields:
        fields["folder_id"] = _own_folder(conn, row, fields["folder_id"])
    if "name" in fields and fields["name"] is not None:
        fields["name"] = str(fields["name"]).strip() or None
    for key in ("description", "email", "url", "phone"):
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
    return _public(updated, conn)


def search_leads(
    conn,
    q: str = "",
    status: str | None = None,
    folder_id: int | None = None,
    provider: str | None = None,
) -> list:
    if status is not None and status not in STATUSES:
        raise ValueError("Неизвестный статус")
    clean_provider = None
    if provider is not None and str(provider).strip() != "":
        clean_provider = str(provider).strip().lower()
        if clean_provider not in PROVIDERS:
            raise ValueError("Неизвестный провайдер")
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
    if clean_provider is not None:
        clauses.append("COALESCE(provider, 'telegram') = ?")
        params.append(clean_provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = [_public(row, conn) for row in conn.execute(sql, params)]
    needle = (q or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if not needle:
        return rows
    folded = needle.casefold()
    needle_digits = re.sub(r"\D", "", needle)

    def matches(lead: dict) -> bool:
        phone_raw = lead.get("phone") or ""
        phone_digits = re.sub(r"\D", "", phone_raw)
        blob = " ".join(
            (
                lead.get("name") or "",
                lead.get("username") or "",
                phone_raw,
                phone_digits,
                str(lead.get("telegram_user_id") or ""),
                lead.get("email") or "",
                lead.get("url") or "",
                lead.get("provider") or "",
            )
        )
        if folded in blob.casefold():
            return True
        if needle_digits and len(needle_digits) >= 3 and needle_digits in phone_digits:
            return True
        return False

    return [lead for lead in rows if matches(lead)]


def add_lead_from_dialog(
    conn,
    account_id,
    telegram_user_id,
    username=None,
    name=None,
    phone=None,
    provider=None,
) -> dict:
    if telegram_user_id is None and phone:
        telegram_user_id = _phone_digits_id(phone)
    if account_id is None or telegram_user_id is None:
        raise ValueError("Нужны account_id и telegram_user_id")

    dlg_row = conn.execute(
        """
        SELECT * FROM dialogs
        WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
        ORDER BY id LIMIT 1
        """,
        (account_id, int(telegram_user_id)),
    ).fetchone()
    if dlg_row is not None:
        dlg = as_dict(dlg_row)
        if not provider and dlg.get("provider"):
            provider = dlg.get("provider")
        if not phone and dlg.get("phone"):
            phone = dlg.get("phone")
        if not username and dlg.get("username"):
            username = dlg.get("username")
        if not name and dlg.get("title"):
            name = dlg.get("title")

    resolved_provider = _resolve_provider(conn, provider, account_id)
    phone = (phone or "").strip() or None
    if resolved_provider == "whatsapp" and not phone:
        peer_str = str(telegram_user_id).strip()
        if peer_str.isdigit() and 7 <= len(peer_str) <= 15 and not peer_str.startswith("900"):
            phone = f"+{peer_str}"

    existing = _find_dialog_lead(
        conn, account_id, telegram_user_id, phone=phone, provider=resolved_provider
    )
    if existing:
        updates = {}
        if phone and not existing.get("phone"):
            updates["phone"] = phone
        if resolved_provider and existing.get("provider") != resolved_provider:
            updates["provider"] = resolved_provider
        if updates:
            existing = update_lead(conn, existing["id"], **updates)
        return existing
    try:
        return create_lead(
            conn,
            account_id=account_id,
            telegram_user_id=telegram_user_id,
            username=username,
            name=name,
            phone=phone,
            provider=resolved_provider,
            status="new",
        )
    except ValueError:
        existing = _find_dialog_lead(
            conn, account_id, telegram_user_id, phone=phone, provider=resolved_provider
        )
        if existing:
            return existing
        raise
