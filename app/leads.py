from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

from app.db import (
    STATUSES,
    as_dict,
    create_pipeline_stage,
    list_pipeline_stages,
    normalize_status,
    status_to_airtable,
    update_pipeline_stage,
    utc_now,
    valid_statuses,
)
from app.events import add_lead_event, list_lead_events, record_lead_event
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


def _parse_offer_type(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [part.strip() for part in s.split(",") if part.strip()]


def _serialize_offer_type(raw) -> str | None:
    items = _parse_offer_type(raw)
    if not items:
        return None
    return json.dumps(items, ensure_ascii=False)


def _public(row, conn=None) -> dict | None:
    data = as_dict(row)
    if data is None:
        return None
    data["provider"] = data.get("provider") or "telegram"
    data["amount"] = int(data.get("amount") or 0)
    data["monthly_value"] = int(data.get("monthly_value") or 0)
    data["currency"] = data.get("currency") or "USD"
    data["offer_type"] = _parse_offer_type(data.get("offer_type"))
    st = data.get("status") or "new"
    data["airtable_status"] = data.get("airtable_status") or status_to_airtable(st)
    if conn is not None:
        return _enrich_lead(conn, data)
    return data


def _clean_amount(value) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("Некорректный бюджет")
    if isinstance(value, (int, float)):
        amt = int(round(value))
        if amt < 0:
            raise ValueError("Бюджет не может быть отрицательным")
        return amt
    raw = str(value).strip().replace(",", "").replace(" ", "").lstrip("$€₽")
    if not raw:
        return 0
    try:
        amt = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ValueError("Некорректный бюджет") from None
    if amt < 0:
        raise ValueError("Бюджет не может быть отрицательным")
    return amt


def _clean_currency(value) -> str:
    if value is None:
        return "USD"
    cleaned = str(value).strip().upper()
    return cleaned or "USD"


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
    amount: int | float | str | None = 0,
    currency: str | None = "USD",
    company: str | None = None,
    niche: str | None = None,
    offer_type=None,
    offer_details: str | None = None,
    monthly_value: int | float | str | None = 0,
    lead_source: str | None = None,
    next_follow_up: str | None = None,
    last_contacted: str | None = None,
    airtable_record_id: str | None = None,
) -> dict:
    norm_status = normalize_status(status, conn)
    if norm_status not in valid_statuses(conn):
        raise ValueError("Неизвестный статус")
    status = norm_status
    airtable_st = status_to_airtable(status)
    resolved_provider = _resolve_provider(conn, provider, account_id)
    username = _clean_username(username)
    name = (name or "").strip() or None
    phone = (phone or "").strip() or None
    note = _blank(note)
    next_step = None if next_step is None else str(next_step)
    description = _blank(description)
    email = _blank(email)
    url = _blank(url)
    company = _blank(company)
    niche = _blank(niche)
    clean_offer_type = _serialize_offer_type(offer_type)
    offer_details = _blank(offer_details)
    clean_monthly = _clean_amount(monthly_value)
    lead_source = _blank(lead_source)
    next_follow_up = _blank(next_follow_up)
    last_contacted = _blank(last_contacted)
    airtable_record_id = _blank(airtable_record_id)
    clean_amt = _clean_amount(amount)
    clean_cur = _clean_currency(currency)
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
                description, email, url, provider, amount, currency,
                company, niche, offer_type, offer_details, monthly_value,
                lead_source, next_follow_up, last_contacted, airtable_record_id, airtable_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                clean_amt,
                clean_cur,
                company,
                niche,
                clean_offer_type,
                offer_details,
                clean_monthly,
                lead_source,
                next_follow_up,
                last_contacted,
                airtable_record_id,
                airtable_st,
            ),
        )
        new_id = int(cur.lastrowid)
        label = name or (f"@{username}" if username else (phone or str(telegram_user_id or new_id)))
        record_lead_event(
            conn,
            new_id,
            "created",
            f"Создан лид {label}",
            {
                "status": status,
                "provider": resolved_provider,
                "amount": clean_amt,
                "currency": clean_cur,
            },
            created_at=now,
        )
        if clean_amt > 0:
            record_lead_event(
                conn,
                new_id,
                "amount_changed",
                f"Установлен бюджет: {clean_amt} {clean_cur}",
                {"old_amount": 0, "new_amount": clean_amt, "currency": clean_cur},
                created_at=now,
            )
        if note:
            record_lead_event(
                conn,
                new_id,
                "note_added",
                "Добавлена заметка при создании лида",
                {"body": str(note)[:200], "source": "lead_create"},
                created_at=now,
            )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        message = str(exc).upper()
        if "UNIQUE" in message:
            raise ValueError("Лид с таким telegram id уже есть") from exc
        raise ValueError("Некорректные данные лида") from exc
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (cur.lastrowid,)).fetchone()
    out = _public(row, conn)
    out["events"] = list_lead_events(conn, int(cur.lastrowid))
    return out


def get_lead(conn, lead_id: int) -> dict:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    lead = _public(row, conn)
    lead["events"] = list_lead_events(conn, lead_id)
    return lead


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
    card["events"] = list_lead_events(conn, lead_id)
    return card


def update_lead(conn, lead_id: int, **fields) -> dict:
    allowed = {
        "status", "note", "next_step", "name", "description", "email", "url", "folder_id",
        "phone", "provider", "amount", "currency", "company", "niche", "offer_type",
        "offer_details", "monthly_value", "lead_source", "next_follow_up", "last_contacted",
        "airtable_record_id", "airtable_status", "airtable_synced_at", "username",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("Нельзя менять: " + ", ".join(sorted(unknown)))
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if row is None:
        raise LookupError("lead")
    if not fields:
        out = _public(row, conn)
        out["events"] = list_lead_events(conn, lead_id)
        return out
    if "status" in fields:
        norm_st = normalize_status(fields["status"], conn)
        if norm_st not in valid_statuses(conn):
            raise ValueError("Неизвестный статус")
        fields["status"] = norm_st
        if "airtable_status" not in fields:
            fields["airtable_status"] = status_to_airtable(norm_st)
    if "provider" in fields and fields["provider"] is not None:
        fields["provider"] = _resolve_provider(conn, fields["provider"], row["account_id"])
    if "folder_id" in fields:
        fields["folder_id"] = _own_folder(conn, row, fields["folder_id"])
    if "amount" in fields:
        fields["amount"] = _clean_amount(fields["amount"])
    if "monthly_value" in fields:
        fields["monthly_value"] = _clean_amount(fields["monthly_value"])
    if "currency" in fields:
        fields["currency"] = _clean_currency(fields["currency"])
    if "offer_type" in fields:
        fields["offer_type"] = _serialize_offer_type(fields["offer_type"])
    if "username" in fields:
        fields["username"] = _clean_username(fields["username"])
    if "name" in fields and fields["name"] is not None:
        fields["name"] = str(fields["name"]).strip() or None
    for key in (
        "description", "email", "url", "phone", "company", "niche",
        "offer_details", "lead_source", "next_follow_up", "last_contacted",
        "airtable_record_id", "airtable_status", "airtable_synced_at",
    ):
        if key in fields and fields[key] is not None:
            fields[key] = str(fields[key]).strip() or None

    now = utc_now()
    old_status = row["status"]
    old_amount = int(row["amount"] or 0) if "amount" in row.keys() else 0
    old_currency = (row["currency"] if "currency" in row.keys() else None) or "USD"
    old_note = (row["note"] or "").strip()

    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(now)
    values.append(lead_id)
    conn.execute(
        f"UPDATE leads SET {assignments}, updated_at = ? WHERE id = ?",
        values,
    )

    if "status" in fields and fields["status"] != old_status:
        record_lead_event(
            conn,
            lead_id,
            "status_changed",
            f"Стадия изменена: {old_status} → {fields['status']}",
            {"old_status": old_status, "new_status": fields["status"]},
            created_at=now,
        )
    if "amount" in fields and int(fields["amount"] or 0) != old_amount:
        new_cur = fields.get("currency") or old_currency
        record_lead_event(
            conn,
            lead_id,
            "amount_changed",
            f"Бюджет изменён: {old_amount} → {int(fields['amount'] or 0)} {new_cur}",
            {
                "old_amount": old_amount,
                "new_amount": int(fields["amount"] or 0),
                "currency": new_cur,
            },
            created_at=now,
        )
    if "note" in fields:
        new_note = (fields["note"] or "").strip()
        if new_note and new_note != old_note:
            record_lead_event(
                conn,
                lead_id,
                "note_added",
                "Обновлена заметка в карточке",
                {"body": new_note[:200], "source": "lead_field"},
                created_at=now,
            )

    conn.commit()
    updated = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    out = _public(updated, conn)
    out["events"] = list_lead_events(conn, lead_id)
    return out


def search_leads(
    conn,
    q: str = "",
    status: str | None = None,
    folder_id: int | None = None,
    provider: str | None = None,
) -> list:
    if status is not None and str(status).strip() != "":
        norm_st = normalize_status(status, conn)
        if norm_st not in valid_statuses(conn):
            raise ValueError("Неизвестный статус")
        status = norm_st
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
                lead.get("company") or "",
                lead.get("niche") or "",
                lead.get("note") or "",
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
    amount=None,
    currency=None,
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
        if amount is not None and int(_clean_amount(amount)) > 0:
            updates["amount"] = _clean_amount(amount)
        if currency is not None:
            updates["currency"] = _clean_currency(currency)
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
            amount=amount if amount is not None else 0,
            currency=currency if currency is not None else "USD",
        )
    except ValueError:
        existing = _find_dialog_lead(
            conn, account_id, telegram_user_id, phone=phone, provider=resolved_provider
        )
        if existing:
            return existing
        raise


BULK_ACTIONS = ("set_status", "set_folder", "set_amount", "enroll_campaign", "delete")


def _ensure_lead_amount_columns(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "amount" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN amount INTEGER NOT NULL DEFAULT 0")
    if "currency" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")


def _valid_statuses(conn) -> set[str]:
    return valid_statuses(conn)


def _has_table(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _record_lead_event_safe(
    conn, lead_id: int, event_type: str, title: str, meta: dict | None = None
) -> None:
    if not _has_table(conn, "lead_events"):
        return
    import json

    try:
        conn.execute(
            """
            INSERT INTO lead_events (lead_id, event_type, title, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(lead_id),
                event_type,
                title,
                json.dumps(meta or {}, ensure_ascii=False),
                utc_now(),
            ),
        )
    except Exception:
        pass


def _iter_chunks(items: list[int], size: int = 900):
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _is_synthetic_tg_id(lead_id: int, tg_id) -> bool:
    if tg_id is None:
        return False
    try:
        return int(tg_id) == 900000 + int(lead_id)
    except (TypeError, ValueError):
        return False


def bulk_leads_action(
    conn,
    lead_ids: list[int],
    action: str,
    *,
    status: str | None = None,
    folder_id: int | str | None = None,
    amount: int | float | str | None = None,
    currency: str | None = None,
    campaign_id: int | None = None,
    value=None,
) -> dict:
    _ensure_lead_amount_columns(conn)
    clean_ids: list[int] = []
    seen: set[int] = set()
    for item in lead_ids or []:
        if item is None or isinstance(item, bool):
            continue
        try:
            lid = int(item)
        except (TypeError, ValueError):
            continue
        if lid > 0 and lid not in seen:
            seen.add(lid)
            clean_ids.append(lid)

    if not clean_ids:
        raise ValueError("Выберите хотя бы одного лида")

    act = (action or "").strip().lower()
    if act not in BULK_ACTIONS:
        raise ValueError("Неизвестное массовое действие")

    rows = []
    for chunk in _iter_chunks(clean_ids):
        ph = ",".join("?" for _ in chunk)
        rows.extend(conn.execute(f"SELECT * FROM leads WHERE id IN ({ph})", chunk).fetchall())
    existing_by_id = {int(r["id"]): as_dict(r) for r in rows}
    existing_ids = [lid for lid in clean_ids if lid in existing_by_id]
    if not existing_ids:
        raise LookupError("lead")

    now = utc_now()

    if act == "set_status":
        target_status = status if status is not None else value
        target_status = normalize_status(str(target_status or "").strip(), conn)
        if not target_status or target_status not in _valid_statuses(conn):
            raise ValueError("Неизвестный статус")
        airtable_st = status_to_airtable(target_status)
        for chunk in _iter_chunks(existing_ids):
            ep = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE leads SET status = ?, airtable_status = ?, updated_at = ? WHERE id IN ({ep})",
                [target_status, airtable_st, now, *chunk],
            )
        for lid in existing_ids:
            old_status = existing_by_id[lid].get("status") or "new"
            record_lead_event(
                conn,
                lid,
                "status_changed",
                f"Стадия изменена (массово): {old_status} → {target_status}",
                {
                    "old_status": old_status,
                    "new_status": target_status,
                    "status": target_status,
                    "bulk": True,
                },
                created_at=now,
            )
        conn.commit()
        return {
            "ok": True,
            "action": "set_status",
            "updated": len(existing_ids),
            "lead_ids": existing_ids,
            "status": target_status,
        }

    if act == "set_folder":
        raw_folder = folder_id if folder_id is not None else value
        if raw_folder is None or str(raw_folder).strip().lower() in ("", "0", "none", "null"):
            resolved_folder = None
        else:
            try:
                resolved_folder = int(raw_folder)
            except (TypeError, ValueError) as exc:
                raise ValueError("Некорректная папка") from exc
            f_row = conn.execute(
                "SELECT id, account_id FROM folders WHERE id = ?",
                (resolved_folder,),
            ).fetchone()
            if f_row is None:
                raise ValueError("Папка не найдена")
        for chunk in _iter_chunks(existing_ids):
            ep = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE leads SET folder_id = ?, updated_at = ? WHERE id IN ({ep})",
                [resolved_folder, now, *chunk],
            )
        conn.commit()
        return {
            "ok": True,
            "action": "set_folder",
            "updated": len(existing_ids),
            "lead_ids": existing_ids,
            "folder_id": resolved_folder,
        }

    if act == "set_amount":
        raw_amount = amount if amount is not None else value
        if raw_amount is None or isinstance(raw_amount, bool) or str(raw_amount).strip() == "":
            raise ValueError("Укажите сумму")
        parsed_amount = _clean_amount(raw_amount)
        clean_currency = str(currency).strip().upper() if currency else None
        for chunk in _iter_chunks(existing_ids):
            ep = ",".join("?" for _ in chunk)
            if clean_currency:
                conn.execute(
                    f"UPDATE leads SET amount = ?, currency = ?, updated_at = ? WHERE id IN ({ep})",
                    [parsed_amount, clean_currency, now, *chunk],
                )
            else:
                conn.execute(
                    f"UPDATE leads SET amount = ?, updated_at = ? WHERE id IN ({ep})",
                    [parsed_amount, now, *chunk],
                )
        for lid in existing_ids:
            old_amount = int(existing_by_id[lid].get("amount") or 0)
            resolved_cur = clean_currency or existing_by_id[lid].get("currency") or "USD"
            record_lead_event(
                conn,
                lid,
                "amount_changed",
                f"Бюджет обновлён (массово): {old_amount} → {parsed_amount} {resolved_cur}",
                {
                    "old_amount": old_amount,
                    "new_amount": parsed_amount,
                    "amount": parsed_amount,
                    "currency": resolved_cur,
                    "bulk": True,
                },
                created_at=now,
            )
        conn.commit()
        return {
            "ok": True,
            "action": "set_amount",
            "updated": len(existing_ids),
            "lead_ids": existing_ids,
            "amount": parsed_amount,
            "currency": clean_currency or "USD",
        }

    if act == "enroll_campaign":
        raw_cid = campaign_id if campaign_id is not None else value
        if raw_cid is None or str(raw_cid).strip() == "":
            raise ValueError("Выберите кампанию")
        try:
            cid = int(raw_cid)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректная кампания") from exc
        from app.outreach import enroll_leads_from_crm

        res = enroll_leads_from_crm(conn, cid, lead_ids=existing_ids)
        return {
            "ok": True,
            "action": "enroll_campaign",
            "updated": res.get("imported", 0),
            "imported": res.get("imported", 0),
            "skipped": res.get("skipped", 0),
            "campaign_id": cid,
            "lead_ids": existing_ids,
        }

    if act == "delete":
        for chunk in _iter_chunks(existing_ids):
            ep = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM reminders WHERE lead_id IN ({ep})", chunk)
            conn.execute(f"DELETE FROM notes WHERE lead_id IN ({ep})", chunk)
            conn.execute(f"DELETE FROM tasks WHERE lead_id IN ({ep})", chunk)
            conn.execute(f"DELETE FROM property_values WHERE lead_id IN ({ep})", chunk)
            conn.execute(
                f"UPDATE dialogs SET lead_id = NULL, updated_at = ? WHERE lead_id IN ({ep})",
                [now, *chunk],
            )
            conn.execute(
                f"UPDATE campaign_recipients SET lead_id = NULL, updated_at = ? WHERE lead_id IN ({ep})",
                [now, *chunk],
            )
            conn.execute(
                f"UPDATE bot_seen_updates SET lead_id = NULL WHERE lead_id IN ({ep})",
                chunk,
            )
            if _has_table(conn, "lead_events"):
                conn.execute(f"DELETE FROM lead_events WHERE lead_id IN ({ep})", chunk)
            conn.execute(f"DELETE FROM leads WHERE id IN ({ep})", chunk)
        conn.commit()
        return {
            "ok": True,
            "action": "delete",
            "deleted": len(existing_ids),
            "updated": len(existing_ids),
            "lead_ids": existing_ids,
        }

    raise ValueError("Неизвестное массовое действие")


def _norm_dup_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    raw = str(phone).strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) >= 7:
        return digits
    return raw


def find_duplicate_leads(conn) -> list[dict]:
    _ensure_lead_amount_columns(conn)
    rows = [
        _public(row, conn)
        for row in conn.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()
    ]
    by_id: dict[int, dict] = {int(r["id"]): r for r in rows if r}
    key_to_ids: dict[tuple[str, str], list[int]] = {}
    key_display: dict[tuple[str, str], str] = {}

    for lead in rows:
        if not lead:
            continue
        lid = int(lead["id"])
        uname = _clean_username(lead.get("username"))
        if uname:
            k = ("username", uname.casefold())
            key_to_ids.setdefault(k, []).append(lid)
            key_display.setdefault(k, f"@{uname}")
        norm_p = _norm_dup_phone(lead.get("phone"))
        if norm_p:
            k = ("phone", norm_p)
            key_to_ids.setdefault(k, []).append(lid)
            key_display.setdefault(k, str(lead.get("phone")).strip())
        tg_id = lead.get("telegram_user_id")
        if tg_id is not None and not _is_synthetic_tg_id(lid, tg_id):
            k = ("telegram_user_id", str(int(tg_id)))
            key_to_ids.setdefault(k, []).append(lid)
            key_display.setdefault(k, str(int(tg_id)))

    parent: dict[int, int] = {lid: lid for lid in by_id}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    dup_keys_per_pair: list[tuple[tuple[str, str], list[int]]] = []
    for k, ids in key_to_ids.items():
        unique_ids = sorted(set(ids))
        if len(unique_ids) >= 2:
            dup_keys_per_pair.append((k, unique_ids))
            first = unique_ids[0]
            for other in unique_ids[1:]:
                union(first, other)

    groups_map: dict[int, list[int]] = {}
    for lid in sorted(by_id):
        root = find(lid)
        groups_map.setdefault(root, []).append(lid)

    result: list[dict] = []
    for root, member_ids in sorted(groups_map.items()):
        if len(member_ids) < 2:
            continue
        member_set = set(member_ids)
        matched_fields: list[str] = []
        match_values: dict[str, str] = {}
        for (field, _norm_val), ids in dup_keys_per_pair:
            if len(member_set.intersection(ids)) >= 2:
                if field not in matched_fields:
                    matched_fields.append(field)
                match_values.setdefault(field, key_display[(field, _norm_val)])

        if not matched_fields:
            continue
        primary_field = matched_fields[0]
        primary_val = match_values.get(primary_field, "")
        group_leads = [by_id[lid] for lid in member_ids]
        result.append(
            {
                "field": primary_field,
                "fields": matched_fields,
                "value": primary_val,
                "match_values": match_values,
                "target_lead_id": member_ids[0],
                "source_lead_id": member_ids[1],
                "source_lead_ids": member_ids[1:],
                "lead_ids": member_ids,
                "count": len(member_ids),
                "leads": group_leads,
            }
        )
    return result


_RECIPIENT_STATUS_PRIORITY = {
    "replied": 5,
    "completed": 4,
    "in_progress": 3,
    "pending": 2,
    "failed": 1,
}


def _merge_campaign_recipients_for_leads(
    conn, source_id: int, target_id: int, now: str
) -> None:
    src_rows = [
        as_dict(r)
        for r in conn.execute(
            "SELECT * FROM campaign_recipients WHERE lead_id = ? ORDER BY id ASC",
            (source_id,),
        ).fetchall()
    ]
    for src_rec in src_rows:
        cid = int(src_rec["campaign_id"])
        tgt_row = conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND lead_id = ? ORDER BY id ASC LIMIT 1",
            (cid, target_id),
        ).fetchone()
        if tgt_row is None:
            conn.execute(
                "UPDATE campaign_recipients SET lead_id = ?, updated_at = ? WHERE id = ?",
                (target_id, now, int(src_rec["id"])),
            )
            continue

        tgt_rec = as_dict(tgt_row)
        tgt_prio = _RECIPIENT_STATUS_PRIORITY.get(str(tgt_rec.get("status") or "pending"), 0)
        src_prio = _RECIPIENT_STATUS_PRIORITY.get(str(src_rec.get("status") or "pending"), 0)
        merged_status = (
            src_rec["status"] if src_prio > tgt_prio else tgt_rec["status"]
        )
        merged_step = max(
            int(tgt_rec.get("current_step") or 0),
            int(src_rec.get("current_step") or 0),
        )
        rec_updates: dict[str, object] = {
            "status": merged_status,
            "current_step": merged_step,
        }
        for col in (
            "assigned_account_id",
            "telegram_user_id",
            "username",
            "name",
            "phone",
            "company",
            "note",
            "website",
            "category",
            "their_angle",
            "our_angle",
            "hook",
            "step1_text",
            "step2_text",
            "step3_text",
            "extra_json",
            "last_sent_at",
            "error_text",
        ):
            t_val = tgt_rec.get(col)
            s_val = src_rec.get(col)
            t_empty = t_val is None or (isinstance(t_val, str) and not t_val.strip())
            s_empty = s_val is None or (isinstance(s_val, str) and not s_val.strip())
            if t_empty and not s_empty:
                rec_updates[col] = s_val
            elif col == "last_sent_at" and not t_empty and not s_empty:
                if str(s_val) > str(t_val):
                    rec_updates[col] = s_val

        assigns = ", ".join(f"{k} = ?" for k in rec_updates)
        conn.execute(
            f"UPDATE campaign_recipients SET {assigns}, updated_at = ? WHERE id = ?",
            [*rec_updates.values(), now, int(tgt_rec["id"])],
        )
        conn.execute(
            "DELETE FROM campaign_recipients WHERE id = ?",
            (int(src_rec["id"]),),
        )


def merge_leads(conn, source_lead_id: int, target_lead_id: int) -> dict:
    _ensure_lead_amount_columns(conn)
    source_id = int(source_lead_id)
    target_id = int(target_lead_id)
    if source_id == target_id:
        raise ValueError("Нельзя слить карточку саму с собой")

    source_row = conn.execute("SELECT * FROM leads WHERE id = ?", (source_id,)).fetchone()
    target_row = conn.execute("SELECT * FROM leads WHERE id = ?", (target_id,)).fetchone()
    if source_row is None or target_row is None:
        raise LookupError("lead")

    source = as_dict(source_row)
    target = as_dict(target_row)
    now = utc_now()

    # 1. Transfer notes, tasks, reminders, dialogs, campaign_recipients, bot_seen_updates
    conn.execute(
        "UPDATE notes SET lead_id = ?, updated_at = ? WHERE lead_id = ?",
        (target_id, now, source_id),
    )
    # Preserve inline leads.note if both leads have notes or if stored notes exist on target
    src_inline_note = (source.get("note") or "").strip()
    tgt_inline_note = (target.get("note") or "").strip()
    existing_note_bodies = {
        str(r["body"]).strip()
        for r in conn.execute(
            "SELECT body FROM notes WHERE lead_id = ?", (target_id,)
        ).fetchall()
        if r["body"]
    }
    if existing_note_bodies:
        for inline_body in (tgt_inline_note, src_inline_note):
            if inline_body and inline_body not in existing_note_bodies:
                conn.execute(
                    """
                    INSERT INTO notes (lead_id, body, source, created_at, updated_at)
                    VALUES (?, ?, 'manual', ?, ?)
                    """,
                    (target_id, inline_body, now, now),
                )
                existing_note_bodies.add(inline_body)
    elif tgt_inline_note and src_inline_note and tgt_inline_note != src_inline_note:
        for inline_body in (tgt_inline_note, src_inline_note):
            if inline_body not in existing_note_bodies:
                conn.execute(
                    """
                    INSERT INTO notes (lead_id, body, source, created_at, updated_at)
                    VALUES (?, ?, 'manual', ?, ?)
                    """,
                    (target_id, inline_body, now, now),
                )
                existing_note_bodies.add(inline_body)

    conn.execute(
        "UPDATE tasks SET lead_id = ?, updated_at = ? WHERE lead_id = ?",
        (target_id, now, source_id),
    )
    conn.execute(
        "UPDATE reminders SET lead_id = ? WHERE lead_id = ?",
        (target_id, source_id),
    )
    conn.execute(
        "UPDATE dialogs SET lead_id = ?, updated_at = ? WHERE lead_id = ?",
        (target_id, now, source_id),
    )
    if source.get("dialog_id"):
        conn.execute(
            "UPDATE dialogs SET lead_id = ?, updated_at = ? WHERE id = ? AND (lead_id IS NULL OR lead_id = ?)",
            (target_id, now, int(source["dialog_id"]), source_id),
        )
    _merge_campaign_recipients_for_leads(conn, source_id, target_id, now)
    conn.execute(
        "UPDATE bot_seen_updates SET lead_id = ? WHERE lead_id = ?",
        (target_id, source_id),
    )
    if _has_table(conn, "lead_events"):
        conn.execute(
            "UPDATE lead_events SET lead_id = ? WHERE lead_id = ?",
            (target_id, source_id),
        )

    # 2. Transfer property_values (respecting UNIQUE(property_id, lead_id))
    src_props = conn.execute(
        "SELECT * FROM property_values WHERE lead_id = ?",
        (source_id,),
    ).fetchall()
    for sp in src_props:
        prop_id = int(sp["property_id"])
        tgt_prop = conn.execute(
            "SELECT * FROM property_values WHERE property_id = ? AND lead_id = ?",
            (prop_id, target_id),
        ).fetchone()
        if tgt_prop is None:
            conn.execute(
                "UPDATE property_values SET lead_id = ?, updated_at = ? WHERE id = ?",
                (target_id, now, int(sp["id"])),
            )
        else:
            tgt_has_val = (
                tgt_prop["value_text"] is not None
                and str(tgt_prop["value_text"]).strip() != ""
            ) or (tgt_prop["value_int"] is not None)
            src_has_val = (
                sp["value_text"] is not None and str(sp["value_text"]).strip() != ""
            ) or (sp["value_int"] is not None)
            if not tgt_has_val and src_has_val:
                conn.execute(
                    """
                    UPDATE property_values
                    SET value_text = ?, value_int = ?, currency = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        sp["value_text"],
                        sp["value_int"],
                        sp["currency"],
                        now,
                        int(tgt_prop["id"]),
                    ),
                )
            conn.execute("DELETE FROM property_values WHERE id = ?", (int(sp["id"]),))

    # 3. Compute fields to backfill on target from source
    updates: dict = {}
    for col in (
        "name",
        "username",
        "phone",
        "telegram_user_id",
        "account_id",
        "email",
        "url",
        "description",
        "note",
        "next_step",
        "folder_id",
        "dialog_id",
    ):
        t_val = target.get(col)
        s_val = source.get(col)
        t_empty = t_val is None or (isinstance(t_val, str) and not t_val.strip())
        s_empty = s_val is None or (isinstance(s_val, str) and not s_val.strip())
        if col == "telegram_user_id":
            if _is_synthetic_tg_id(target_id, t_val):
                t_empty = True
            if _is_synthetic_tg_id(source_id, s_val):
                s_empty = True
        if t_empty and not s_empty:
            updates[col] = s_val

    t_amt = int(target.get("amount") or 0)
    s_amt = int(source.get("amount") or 0)
    if t_amt == 0 and s_amt != 0:
        updates["amount"] = s_amt
        if source.get("currency") and (
            not target.get("currency") or target.get("currency") == "USD"
        ):
            updates["currency"] = source["currency"]

    if not target.get("dialog_id") and "dialog_id" not in updates:
        dlg_row = conn.execute(
            "SELECT id FROM dialogs WHERE lead_id = ? ORDER BY last_message_at DESC, id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        if dlg_row is not None:
            updates["dialog_id"] = int(dlg_row["id"])

    # 4. Delete source lead BEFORE updating target to avoid UNIQUE constraint on telegram_user_id
    conn.execute("DELETE FROM leads WHERE id = ?", (source_id,))

    # 5. Update target lead safely without violating UNIQUE(account_id, telegram_user_id)
    if updates:
        new_acc = updates.get("account_id", target.get("account_id"))
        new_tg = updates.get("telegram_user_id", target.get("telegram_user_id"))
        if new_tg is not None:
            conflict = conn.execute(
                "SELECT id FROM leads WHERE id != ? AND account_id IS ? AND telegram_user_id = ?",
                (target_id, new_acc, new_tg),
            ).fetchone()
            if conflict is not None:
                if "telegram_user_id" in updates and "account_id" not in updates:
                    updates.pop("telegram_user_id", None)
                elif "account_id" in updates and "telegram_user_id" not in updates:
                    updates.pop("account_id", None)
                else:
                    # Both were in updates; check if keeping target's original account_id avoids conflict
                    alt_conflict = conn.execute(
                        "SELECT id FROM leads WHERE id != ? AND account_id IS ? AND telegram_user_id = ?",
                        (target_id, target.get("account_id"), new_tg),
                    ).fetchone()
                    if alt_conflict is None:
                        updates.pop("account_id", None)
                    else:
                        updates.pop("telegram_user_id", None)

    if updates:
        assigns = ", ".join(f"{k} = ?" for k in updates)
        vals = [*updates.values(), now, target_id]
        conn.execute(f"UPDATE leads SET {assigns}, updated_at = ? WHERE id = ?", vals)
    else:
        conn.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now, target_id))

    source_label = (
        source.get("name")
        or (f"@{source['username']}" if source.get("username") else None)
        or source.get("phone")
    )
    event_title = (
        f"Объединён дубль #{source_id} ({source_label})"
        if source_label
        else f"Объединён дубль #{source_id}"
    )
    record_lead_event(
        conn,
        target_id,
        "merged",
        event_title,
        {"source_lead_id": source_id, "target_lead_id": target_id},
        created_at=now,
    )
    conn.commit()

    card = lead_card(conn, target_id)
    card["ok"] = True
    card["merged_from_id"] = source_id
    card["source_lead_id"] = source_id
    card["target_lead_id"] = target_id
    return card


def export_leads_csv(
    conn,
    q: str = "",
    status: str | None = None,
    folder_id: int | None = None,
    provider: str | None = None,
) -> str:
    import csv
    import io
    from app.properties import list_property_defs

    _ensure_lead_amount_columns(conn)
    leads = search_leads(
        conn,
        q=q,
        status=status or None,
        folder_id=folder_id,
        provider=provider or None,
    )
    prop_defs = list_property_defs(conn, "lead")

    prop_map: dict[tuple[int, int], dict] = {}
    if leads and prop_defs:
        lids = [int(l["id"]) for l in leads]
        for chunk in _iter_chunks(lids):
            ph = ",".join("?" for _ in chunk)
            for pv in conn.execute(
                f"SELECT * FROM property_values WHERE lead_id IN ({ph})",
                chunk,
            ).fetchall():
                prop_map[(int(pv["lead_id"]), int(pv["property_id"]))] = as_dict(pv)

    base_columns = [
        "id",
        "name",
        "username",
        "phone",
        "telegram_user_id",
        "provider",
        "status",
        "amount",
        "currency",
        "email",
        "url",
        "description",
        "note",
        "next_step",
        "folder_id",
        "folder_name",
        "account_id",
        "account_phone",
        "account_name",
        "campaign_status",
        "campaign_step",
        "last_sent_at",
        "last_message_at",
        "created_at",
        "updated_at",
    ]

    custom_cols: list[tuple[int, str]] = []
    used_headers = set(base_columns)
    for pdef in prop_defs:
        col_name = str(pdef["name"]).strip()
        if not col_name or col_name in used_headers:
            col_name = f"prop_{pdef['key']}"
        used_headers.add(col_name)
        custom_cols.append((int(pdef["id"]), col_name))

    headers = [*base_columns, *(col_name for _, col_name in custom_cols)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, lineterminator="\n")
    writer.writeheader()

    for lead in leads:
        lid = int(lead["id"])
        row_dict: dict[str, object] = {}
        for col in base_columns:
            if col == "folder_name":
                val = _folder_title(conn, lead.get("folder_id"))
            elif col == "amount":
                val = int(lead.get("amount") or 0)
            elif col == "currency":
                val = lead.get("currency") or "USD"
            else:
                val = lead.get(col)
            row_dict[col] = "" if val is None else val

        for prop_id, col_name in custom_cols:
            pv = prop_map.get((lid, prop_id))
            if pv is None:
                row_dict[col_name] = ""
            elif pv.get("value_text") is not None:
                row_dict[col_name] = pv["value_text"]
            elif pv.get("value_int") is not None:
                row_dict[col_name] = pv["value_int"]
            else:
                row_dict[col_name] = ""

        writer.writerow(row_dict)

    return buf.getvalue()

