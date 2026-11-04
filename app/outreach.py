import csv
import io
import json
import re
import sqlite3

from app.accounts import _load as load_account
from app.accounts import _public as public_account
from app.accounts import increment_daily_sent
from app.db import as_dict, utc_now, utc_today

CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed")
RECIPIENT_STATUSES = ("pending", "in_progress", "completed", "replied", "failed")

DEFAULT_SCHEDULE_DAYS = {
    "mon": {"enabled": True, "start": "09:00", "end": "18:00"},
    "tue": {"enabled": True, "start": "09:00", "end": "18:00"},
    "wed": {"enabled": True, "start": "09:00", "end": "18:00"},
    "thu": {"enabled": True, "start": "09:00", "end": "18:00"},
    "fri": {"enabled": True, "start": "09:00", "end": "18:00"},
    "sat": {"enabled": False, "start": "10:00", "end": "16:00"},
    "sun": {"enabled": False, "start": "10:00", "end": "16:00"},
}

TARGET_CSV_FIELDS = (
    "username",
    "phone",
    "name",
    "company",
    "step1_text",
    "step2_text",
    "step3_text",
    "their_angle",
    "our_angle",
    "category",
    "website",
    "hook",
    "note",
    "telegram_user_id",
)

HEADER_ALIASES = {
    "username": "username",
    "user": "username",
    "tg": "username",
    "telegram": "username",
    "ник": "username",
    "юзернейм": "username",
    "контакт": "username",
    "contact": "username",
    "tg_link": "username",
    "ссылка": "username",
    "name": "name",
    "full_name": "name",
    "fullname": "name",
    "имя": "name",
    "фио": "name",
    "phone": "phone",
    "tel": "phone",
    "телефон": "phone",
    "номер": "phone",
    "whatsapp": "phone",
    "wa": "phone",
    "wa_phone": "phone",
    "company": "company",
    "org": "company",
    "компания": "company",
    "проект": "company",
    "название": "company",
    "сайт": "website",
    "website": "website",
    "url": "website",
    "домен": "website",
    "категория": "category",
    "category": "category",
    "ниша": "category",
    "их угол": "their_angle",
    "their_angle": "their_angle",
    "описание": "their_angle",
    "наш угол": "our_angle",
    "our_angle": "our_angle",
    "оффер": "our_angle",
    "хук": "hook",
    "hook": "hook",
    "первое касание": "step1_text",
    "first_touch": "step1_text",
    "шаг 1": "step1_text",
    "step1": "step1_text",
    "фоллоуап 1": "step2_text",
    "followup 1": "step2_text",
    "followup_1": "step2_text",
    "шаг 2": "step2_text",
    "step2": "step2_text",
    "фоллоуап 2": "step3_text",
    "followup 2": "step3_text",
    "followup_2": "step3_text",
    "шаг 3": "step3_text",
    "step3": "step3_text",
    "note": "note",
    "notes": "note",
    "comment": "note",
    "заметка": "note",
    "комментарий": "note",
    "telegram_user_id": "telegram_user_id",
    "tg_id": "telegram_user_id",
    "user_id": "telegram_user_id",
}


def _parse_account_ids(account_id=None, account_ids=None) -> list[int]:
    raw_list = []
    if isinstance(account_ids, str):
        try:
            parsed = json.loads(account_ids)
            if isinstance(parsed, list):
                raw_list.extend(parsed)
        except Exception:
            pass
    elif isinstance(account_ids, (list, tuple, set)):
        raw_list.extend(account_ids)
    if account_id is not None and not isinstance(account_id, bool):
        raw_list.append(account_id)
    out = []
    seen = set()
    for item in raw_list:
        if isinstance(item, bool) or item is None:
            continue
        try:
            val = int(item)
        except (TypeError, ValueError):
            continue
        if val > 0 and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _clean_handle(raw: str | None) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    val = re.sub(r"^https?://(?:www\.)?t\.me/", "", val, flags=re.IGNORECASE)
    val = re.sub(r"^t\.me/", "", val, flags=re.IGNORECASE)
    val = val.strip().lstrip("@").strip("/")
    return val or None


def _clean_company_title(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"^\d+\.\s+", "", s).strip()
    return s or None


def _clean_step1_text(raw: str | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    marker = "Actually, here's the rewrite based on what you gave me:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    low = text.lower()
    if low.startswith("i can't write this one") or low.startswith("i cannot write"):
        return None, "Пропущен: в CSV ошибка генерации текста (Cloudflare/пустое описание)"
    return (text or None), None


def render_template(template_text: str, recipient: dict) -> str:
    text = str(template_text or "")
    name_val = (
        (recipient.get("name") or "").strip()
        or (recipient.get("company") or "").strip()
        or (recipient.get("username") or "").strip()
        or "коллега"
    )
    username_val = (recipient.get("username") or "").strip()
    company_val = (recipient.get("company") or "").strip()
    note_val = (recipient.get("note") or recipient.get("their_angle") or "").strip()
    phone_val = (recipient.get("phone") or "").strip()
    website_val = (recipient.get("website") or "").strip()
    category_val = (recipient.get("category") or "").strip()
    their_angle_val = (recipient.get("their_angle") or "").strip()
    our_angle_val = (recipient.get("our_angle") or "").strip()
    hook_val = (recipient.get("hook") or "").strip()
    step1_val = (recipient.get("step1_text") or "").strip()
    step2_val = (recipient.get("step2_text") or "").strip()
    step3_val = (recipient.get("step3_text") or "").strip()

    replacements = {
        "{name}": name_val,
        "{username}": username_val,
        "{company}": company_val,
        "{note}": note_val,
        "{phone}": phone_val,
        "{website}": website_val,
        "{category}": category_val,
        "{their_angle}": their_angle_val,
        "{our_angle}": our_angle_val,
        "{hook}": hook_val,
        "{first_touch}": step1_val,
        "{followup_1}": step2_val,
        "{followup_2}": step3_val,
        "{step1_text}": step1_val,
        "{step2_text}": step2_val,
        "{step3_text}": step3_val,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)

    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t]{2,}", " ", line).strip()
        cleaned = re.sub(r"\s+([,!?.:;])", r"\1", cleaned)
        lines.append(cleaned)
    return "\n".join(lines).strip()


def _sync_campaign_replies(conn, campaign_id: int, account_id: int | None) -> int:
    if not account_id:
        return 0
    rows = conn.execute(
        """
        SELECT * FROM campaign_recipients
        WHERE campaign_id = ? AND status IN ('in_progress', 'completed')
        """,
        (campaign_id,),
    ).fetchall()
    replied_now = 0
    now = utc_now()
    for row in rows:
        rec = as_dict(row)
        dialog = None
        if rec.get("telegram_user_id"):
            dialog = conn.execute(
                """
                SELECT last_from_peer, last_message_at FROM dialogs
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (account_id, int(rec["telegram_user_id"])),
            ).fetchone()
        if dialog is None and rec.get("lead_id"):
            dialog = conn.execute(
                """
                SELECT last_from_peer, last_message_at FROM dialogs
                WHERE account_id = ? AND lead_id = ?
                """,
                (account_id, int(rec["lead_id"])),
            ).fetchone()
        if dialog is None and rec.get("phone"):
            phone_digits = re.sub(r"\D", "", str(rec["phone"]))
            peer_cand = int(phone_digits[-12:]) if phone_digits and len(phone_digits) >= 7 else -1
            dialog = conn.execute(
                """
                SELECT last_from_peer, last_message_at FROM dialogs
                WHERE account_id = ? AND peer_type = 'user' AND (phone = ? OR peer_id = ?)
                """,
                (account_id, rec["phone"], peer_cand),
            ).fetchone()
        if dialog and int(dialog["last_from_peer"] or 0) == 1:
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'replied', updated_at = ?
                WHERE id = ?
                """,
                (now, rec["id"]),
            )
            replied_now += 1
    if replied_now:
        conn.commit()
    return replied_now


def get_campaign(conn, campaign_id: int) -> dict:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        raise LookupError("campaign")
    camp = as_dict(row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    for aid in account_ids:
        _sync_campaign_replies(conn, campaign_id, aid)

    accounts_list = []
    acc_name_map = {}
    for aid in account_ids:
        try:
            acc_pub = public_account(load_account(conn, aid))
            accounts_list.append(acc_pub)
            acc_name_map[int(aid)] = (
                acc_pub.get("telegram_name")
                or acc_pub.get("telegram_username")
                or acc_pub.get("phone")
                or f"Аккаунт #{aid}"
            )
        except LookupError:
            continue

    steps_rows = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order",
        (campaign_id,),
    ).fetchall()
    steps = [as_dict(s) for s in steps_rows]

    rec_rows = conn.execute(
        "SELECT * FROM campaign_recipients WHERE campaign_id = ? ORDER BY id",
        (campaign_id,),
    ).fetchall()
    recipients = []
    for r in rec_rows:
        rd = as_dict(r)
        assigned_id = rd.get("assigned_account_id")
        if assigned_id is not None:
            assigned_id_int = int(assigned_id)
            if assigned_id_int not in acc_name_map:
                try:
                    ap = public_account(load_account(conn, assigned_id_int))
                    acc_name_map[assigned_id_int] = (
                        ap.get("telegram_name")
                        or ap.get("telegram_username")
                        or ap.get("phone")
                        or f"Аккаунт #{assigned_id_int}"
                    )
                except LookupError:
                    acc_name_map[assigned_id_int] = None
            rd["assigned_account_name"] = acc_name_map.get(assigned_id_int)
        else:
            rd["assigned_account_name"] = None
        recipients.append(rd)

    today = utc_today()
    pending_count = sum(1 for r in recipients if r["status"] == "pending")
    in_progress_count = sum(1 for r in recipients if r["status"] == "in_progress")
    completed_count = sum(1 for r in recipients if r["status"] == "completed")
    replied_count = sum(1 for r in recipients if r["status"] == "replied")
    failed_count = sum(1 for r in recipients if r["status"] == "failed")
    sent_today = sum(
        1
        for r in recipients
        if (r.get("last_sent_at") or "").startswith(today)
    )

    camp["account_ids"] = account_ids
    camp["accounts"] = accounts_list
    camp["steps"] = steps
    camp["recipients"] = recipients
    camp["total_recipients"] = len(recipients)
    camp["pending_count"] = pending_count
    camp["in_progress_count"] = in_progress_count
    camp["completed_count"] = completed_count
    camp["replied_count"] = replied_count
    camp["failed_count"] = failed_count
    camp["sent_today"] = sent_today
    return camp


def list_campaigns(conn) -> list:
    rows = conn.execute("SELECT id FROM campaigns ORDER BY id DESC").fetchall()
    return [get_campaign(conn, row["id"]) for row in rows]


def _normalize_steps(steps: list | None) -> list[dict]:
    if not steps:
        return [
            {
                "step_order": 1,
                "delay_hours": 0,
                "template_text": "Привет, {name}! Пишу по поводу сотрудничества.",
            }
        ]
    normalized = []
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError("Некорректный формат шага цепочки")
        text = str(step.get("template_text") or "").strip()
        if not text:
            raise ValueError("Укажите текст сообщения для каждого шага")
        delay_raw = step.get("delay_hours", 0 if idx == 1 else 24)
        try:
            delay_hours = max(0, int(delay_raw or 0))
        except (TypeError, ValueError):
            raise ValueError("Задержка шага должна быть числом часов") from None
        normalized.append(
            {
                "step_order": idx,
                "delay_hours": delay_hours,
                "template_text": text,
            }
        )
    return normalized


def create_campaign(
    conn,
    name: str,
    account_id: int | None = None,
    daily_limit: int = 20,
    steps: list | None = None,
    account_ids: list[int] | None = None,
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Укажите название рассылки")
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not (1 <= int(daily_limit) <= 500):
        raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")
    resolved_ids = _parse_account_ids(account_id, account_ids)
    for aid in resolved_ids:
        acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (aid,)).fetchone()
        if acc is None:
            raise ValueError("Аккаунт не найден")
    primary_account_id = resolved_ids[0] if resolved_ids else None
    account_ids_json = json.dumps(resolved_ids)
    norm_steps = _normalize_steps(steps)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'draft', ?, ?)
        """,
        (clean_name, primary_account_id, account_ids_json, int(daily_limit), now, now),
    )
    campaign_id = cur.lastrowid
    for step in norm_steps:
        conn.execute(
            """
            INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                step["step_order"],
                step["delay_hours"],
                step["template_text"],
                now,
            ),
        )
    conn.commit()
    return get_campaign(conn, campaign_id)


def update_campaign(
    conn,
    campaign_id: int,
    name: str | None = None,
    account_id: int | None = None,
    daily_limit: int | None = None,
    status: str | None = None,
    steps: list | None = None,
    account_ids: list[int] | None = None,
) -> dict:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        raise LookupError("campaign")
    current = as_dict(row)
    new_name = current["name"] if name is None else (name or "").strip()
    if not new_name:
        raise ValueError("Укажите название рассылки")
    new_limit = current["daily_limit"] if daily_limit is None else daily_limit
    if isinstance(new_limit, bool) or not isinstance(new_limit, int) or not (1 <= int(new_limit) <= 500):
        raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")

    if account_ids is not None:
        resolved_ids = _parse_account_ids(account_id, account_ids)
    elif account_id is not None:
        resolved_ids = _parse_account_ids(account_id, None)
    else:
        resolved_ids = _parse_account_ids(current.get("account_id"), current.get("account_ids_json"))

    for aid in resolved_ids:
        acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (aid,)).fetchone()
        if acc is None:
            raise ValueError("Аккаунт не найден")
    primary_account_id = resolved_ids[0] if resolved_ids else None
    account_ids_json = json.dumps(resolved_ids)

    new_status = current["status"] if status is None else status
    if new_status not in CAMPAIGN_STATUSES:
        raise ValueError("Неизвестный статус рассылки")

    now = utc_now()
    conn.execute(
        """
        UPDATE campaigns
        SET name = ?, account_id = ?, account_ids_json = ?, daily_limit = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_name, primary_account_id, account_ids_json, int(new_limit), new_status, now, campaign_id),
    )
    if steps is not None:
        norm_steps = _normalize_steps(steps)
        conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ?", (campaign_id,))
        for step in norm_steps:
            conn.execute(
                """
                INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    step["step_order"],
                    step["delay_hours"],
                    step["template_text"],
                    now,
                ),
            )
    conn.commit()
    return get_campaign(conn, campaign_id)


def get_outreach_schedule(conn) -> dict:
    row = conn.execute("SELECT * FROM outreach_schedule WHERE id = 1").fetchone()
    if row is None:
        now = utc_now()
        days_copy = {k: dict(v) for k, v in DEFAULT_SCHEDULE_DAYS.items()}
        conn.execute(
            """
            INSERT INTO outreach_schedule (id, timezone, days_json, updated_at)
            VALUES (1, 'UTC', ?, ?)
            """,
            (json.dumps(days_copy), now),
        )
        conn.commit()
        return {"timezone": "UTC", "days": days_copy, "updated_at": now}
    d = as_dict(row)
    try:
        parsed_days = json.loads(d.get("days_json") or "{}")
    except Exception:
        parsed_days = {}
    days_out = {}
    for day_key, default_cfg in DEFAULT_SCHEDULE_DAYS.items():
        raw_day = parsed_days.get(day_key) if isinstance(parsed_days, dict) else None
        if isinstance(raw_day, dict):
            days_out[day_key] = {
                "enabled": bool(raw_day.get("enabled", default_cfg["enabled"])),
                "start": str(raw_day.get("start") or default_cfg["start"]).strip(),
                "end": str(raw_day.get("end") or default_cfg["end"]).strip(),
            }
        else:
            days_out[day_key] = dict(default_cfg)
    return {
        "timezone": d.get("timezone") or "UTC",
        "days": days_out,
        "updated_at": d.get("updated_at") or utc_now(),
    }


def update_outreach_schedule(conn, timezone: str, days: dict) -> dict:
    clean_tz = (timezone or "UTC").strip() or "UTC"
    if not isinstance(days, dict):
        raise ValueError("Некорректный формат дней расписания")
    current = get_outreach_schedule(conn)["days"]
    merged = {}
    for day_key, default_cfg in DEFAULT_SCHEDULE_DAYS.items():
        base_cfg = current.get(day_key, default_cfg)
        incoming = days.get(day_key)
        if isinstance(incoming, dict):
            merged[day_key] = {
                "enabled": bool(incoming.get("enabled", base_cfg["enabled"])),
                "start": str(incoming.get("start") or base_cfg["start"]).strip(),
                "end": str(incoming.get("end") or base_cfg["end"]).strip(),
            }
        else:
            merged[day_key] = dict(base_cfg)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO outreach_schedule (id, timezone, days_json, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            timezone = excluded.timezone,
            days_json = excluded.days_json,
            updated_at = excluded.updated_at
        """,
        (clean_tz, json.dumps(merged), now),
    )
    conn.commit()
    return get_outreach_schedule(conn)


def _detect_delimiter(sample: str) -> str:
    first_line = ""
    for line in sample.splitlines():
        if line.strip():
            first_line = line
            break
    if "\t" in first_line:
        return "\t"
    if ";" in first_line and first_line.count(";") >= first_line.count(","):
        return ";"
    return ","


def _build_col_map(
    headers: list[str], column_mapping: dict | None = None
) -> dict[int, str]:
    header_lower = [c.lower().strip().lstrip("\ufeff") for c in headers]
    col_map: dict[int, str] = {}
    for idx, col in enumerate(header_lower):
        alias = HEADER_ALIASES.get(col)
        if alias:
            col_map[idx] = alias

    if isinstance(column_mapping, dict) and column_mapping:
        header_by_lower = {c.lower().strip().lstrip("\ufeff"): idx for idx, c in enumerate(headers)}
        for k, v in column_mapping.items():
            k_str = str(k or "").strip()
            v_str = str(v or "").strip()
            if not k_str or not v_str:
                continue
            if k_str in TARGET_CSV_FIELDS:
                target_field = k_str
                col_ref = v_str
            elif v_str in TARGET_CSV_FIELDS:
                target_field = v_str
                col_ref = k_str
            else:
                continue

            matched_idx = header_by_lower.get(col_ref.lower().lstrip("\ufeff"))
            if matched_idx is None and col_ref.isdigit():
                idx_candidate = int(col_ref)
                if 0 <= idx_candidate < len(headers):
                    matched_idx = idx_candidate
            if matched_idx is not None:
                for old_idx, old_field in list(col_map.items()):
                    if old_field == target_field:
                        col_map.pop(old_idx, None)
                col_map[matched_idx] = target_field
    return col_map


def preview_campaign_csv(csv_text: str) -> dict:
    raw = (csv_text or "").strip()
    if not raw:
        raise ValueError("Вставьте строки CSV или выберите файл с контактами")
    delimiter = _detect_delimiter(raw)
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("В таблице CSV нет строк с контактами")

    first_clean = [c.strip().lstrip("\ufeff") for c in rows[0]]
    first_lower = [c.lower() for c in first_clean]
    is_header = any(col in HEADER_ALIASES for col in first_lower)

    if is_header:
        headers = first_clean
        data_rows = rows[1:]
        col_map = _build_col_map(headers)
    else:
        max_cols = max(len(r) for r in rows)
        headers = [f"Колонка {i + 1}" for i in range(max_cols)]
        data_rows = rows
        default_fields = ["name", "username", "phone", "company", "note"]
        col_map = {i: default_fields[i] for i in range(min(len(headers), len(default_fields)))}

    detected_mapping: dict[str, str] = {}
    for idx, field in col_map.items():
        if idx < len(headers) and field not in detected_mapping:
            detected_mapping[field] = headers[idx]

    sample_rows = []
    for r in data_rows[:5]:
        row_dict = {}
        for idx, h in enumerate(headers):
            row_dict[h] = r[idx] if idx < len(r) else ""
        sample_rows.append(row_dict)

    return {
        "headers": headers,
        "sample_rows": sample_rows,
        "detected_mapping": detected_mapping,
        "total_rows": len(data_rows),
    }


def _parse_csv_rows(csv_text: str, column_mapping: dict | None = None) -> list[dict]:
    raw = (csv_text or "").strip()
    if not raw:
        raise ValueError("Вставьте строки CSV или выберите файл с контактами")
    delimiter = _detect_delimiter(raw)
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("В таблице CSV нет строк с контактами")

    first_lower = [c.lower().strip().lstrip("\ufeff") for c in rows[0]]
    has_explicit_mapping = bool(isinstance(column_mapping, dict) and column_mapping)
    is_header = any(col in HEADER_ALIASES for col in first_lower) or has_explicit_mapping

    parsed = []
    if is_header:
        col_map = _build_col_map(rows[0], column_mapping=column_mapping)
        data_rows = rows[1:]
        for r in data_rows:
            item = {}
            for idx, val in enumerate(r):
                field = col_map.get(idx)
                if field and val:
                    item[field] = val
            if item:
                parsed.append(item)
    else:
        for r in rows:
            c0 = r[0] if len(r) > 0 else ""
            c1 = r[1] if len(r) > 1 else ""
            c2 = r[2] if len(r) > 2 else ""
            c3 = r[3] if len(r) > 3 else ""
            c4 = r[4] if len(r) > 4 else ""
            if c0.startswith("@") or "t.me/" in c0.lower() or (
                re.fullmatch(r"[A-Za-z0-9_]{3,32}", c0) and (" " in c1 or re.search(r"[А-Яа-яЁё]", c1))
            ):
                username, name = c0, c1
            else:
                name, username = c0, c1
            parsed.append(
                {
                    "name": name,
                    "username": username,
                    "phone": c2,
                    "company": c3,
                    "note": c4,
                }
            )
    return parsed


def _account_provider(conn, account_id: int | None) -> str:
    if account_id is None:
        return "telegram"
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
    if row is None:
        return "telegram"
    prov = (as_dict(row).get("provider") or "telegram").strip().lower()
    return prov if prov in ("telegram", "whatsapp") else "telegram"


def _resolve_or_create_lead(conn, account_id: int | None, item: dict, now: str) -> tuple[int, int]:
    provider = str(item.get("provider") or _account_provider(conn, account_id) or "telegram").strip().lower()
    if provider not in ("telegram", "whatsapp"):
        provider = "telegram"
    username = _clean_handle(item.get("username"))
    company = _clean_company_title(item.get("company"))
    name = (item.get("name") or "").strip() or None
    phone = (item.get("phone") or "").strip() or None
    note = (item.get("note") or "").strip() or None
    their_angle = (item.get("their_angle") or "").strip() or None
    website_raw = (item.get("website") or "").strip()
    url_val = None
    if website_raw:
        if re.match(r"^https?://", website_raw, flags=re.IGNORECASE):
            url_val = website_raw
        else:
            url_val = f"https://{website_raw}"

    tg_id_raw = item.get("telegram_user_id")
    tg_id = None
    if tg_id_raw is not None and str(tg_id_raw).strip():
        try:
            tg_id = int(str(tg_id_raw).strip())
        except ValueError:
            tg_id = None
    if tg_id is None and provider == "whatsapp" and phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 7:
            tg_id = int(digits[-12:])

    lead_row = None
    if tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (tg_id,),
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

    combined_note = note or their_angle or (f"Компания: {company}" if company else None)
    display_name = name or company or (f"{username} · {company}" if username and company else username) or phone

    if lead_row is not None:
        lead_id = int(lead_row["id"])
        existing_tg_id = lead_row["telegram_user_id"]
        if existing_tg_id is None:
            existing_tg_id = tg_id if tg_id is not None else (900000 + lead_id)
            conn.execute(
                "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                (existing_tg_id, now, lead_id),
            )
        if phone and not lead_row["phone"]:
            conn.execute("UPDATE leads SET phone = ?, updated_at = ? WHERE id = ?", (phone, now, lead_id))
        if url_val and not lead_row["url"]:
            conn.execute("UPDATE leads SET url = ?, updated_at = ? WHERE id = ?", (url_val, now, lead_id))
        if combined_note and not lead_row["note"]:
            conn.execute("UPDATE leads SET note = ?, updated_at = ? WHERE id = ?", (combined_note, now, lead_id))
        return lead_id, int(existing_tg_id)

    cur = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, phone,
            status, note, next_step, created_at, updated_at, description, url, provider
        ) VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            tg_id,
            username,
            display_name,
            phone,
            combined_note,
            "Отправить цепочку рассылки",
            now,
            now,
            company,
            url_val,
            provider,
        ),
    )
    lead_id = int(cur.lastrowid)
    if tg_id is None:
        tg_id = 900000 + lead_id
        conn.execute(
            "UPDATE leads SET telegram_user_id = ? WHERE id = ?",
            (tg_id, lead_id),
        )
    return lead_id, int(tg_id)


def import_campaign_csv(
    conn,
    campaign_id: int,
    csv_text: str,
    column_mapping: dict | None = None,
) -> dict:
    camp_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if camp_row is None:
        raise LookupError("campaign")
    camp = as_dict(camp_row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    account_id = account_ids[0] if account_ids else camp.get("account_id")

    contacts = _parse_csv_rows(csv_text, column_mapping=column_mapping)
    existing_rows = conn.execute(
        "SELECT username, phone, lead_id, telegram_user_id FROM campaign_recipients WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    seen_usernames = {
        str(r["username"]).lower() for r in existing_rows if r["username"]
    }
    seen_phones = {str(r["phone"]) for r in existing_rows if r["phone"]}
    seen_leads = {int(r["lead_id"]) for r in existing_rows if r["lead_id"]}

    now = utc_now()
    imported = 0
    has_step3 = False
    for item in contacts:
        username = _clean_handle(item.get("username"))
        company = _clean_company_title(item.get("company"))
        explicit_name = (item.get("name") or "").strip() or None
        name = explicit_name or company or username
        phone = (item.get("phone") or "").strip() or None
        website = (item.get("website") or "").strip() or None
        category = (item.get("category") or "").strip() or None
        their_angle = (item.get("their_angle") or "").strip() or None
        our_angle = (item.get("our_angle") or "").strip() or None
        hook = (item.get("hook") or "").strip() or None
        note = (item.get("note") or "").strip() or their_angle
        step1_text, error_reason = _clean_step1_text(item.get("step1_text"))
        step2_text = (item.get("step2_text") or "").strip() or None
        step3_text = (item.get("step3_text") or "").strip() or None
        if step3_text:
            has_step3 = True

        if not any((username, name, phone, item.get("telegram_user_id"))):
            continue
        if username and username.lower() in seen_usernames:
            continue
        if phone and phone in seen_phones:
            continue

        lead_item = dict(item)
        lead_item["company"] = company
        lead_item["name"] = name
        lead_id, tg_id = _resolve_or_create_lead(conn, account_id, lead_item, now)
        if lead_id in seen_leads:
            continue

        rec_status = "failed" if error_reason else "pending"
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                phone, company, note, website, category, their_angle,
                our_angle, hook, step1_text, step2_text, step3_text,
                current_step, status, last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
            """,
            (
                campaign_id,
                lead_id,
                tg_id,
                username,
                name or username or phone,
                phone,
                company,
                note,
                website,
                category,
                their_angle,
                our_angle,
                hook,
                step1_text,
                step2_text,
                step3_text,
                rec_status,
                error_reason,
                now,
                now,
            ),
        )
        if username:
            seen_usernames.add(username.lower())
        if phone:
            seen_phones.add(phone)
        seen_leads.add(lead_id)
        imported += 1

    if has_step3:
        existing_steps = conn.execute(
            "SELECT step_order FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order",
            (campaign_id,),
        ).fetchall()
        existing_orders = {int(s["step_order"]) for s in existing_steps}
        if existing_orders and 3 not in existing_orders:
            if 2 not in existing_orders:
                conn.execute(
                    """
                    INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
                    VALUES (?, 2, 24, '{followup_1}', ?)
                    """,
                    (campaign_id, now),
                )
            conn.execute(
                """
                INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
                VALUES (?, 3, 72, '{followup_2}', ?)
                """,
                (campaign_id, now),
            )

    conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, campaign_id))
    conn.commit()
    updated_campaign = get_campaign(conn, campaign_id)
    return {
        "imported": imported,
        "total_recipients": updated_campaign["total_recipients"],
        "campaign": updated_campaign,
    }


def enroll_leads_from_crm(
    conn,
    campaign_id: int,
    lead_ids: list[int] | None = None,
    status: str | None = None,
    folder_id: int | None = None,
) -> dict:
    camp_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if camp_row is None:
        raise LookupError("campaign")
    camp = as_dict(camp_row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    camp_providers = {_account_provider(conn, aid) for aid in account_ids if aid}
    primary_provider = next(iter(camp_providers)) if len(camp_providers) == 1 else None

    sql = "SELECT * FROM leads"
    clauses = []
    params: list = []
    if lead_ids is not None and len(lead_ids) > 0:
        clean_ids = [int(x) for x in lead_ids if x is not None and not isinstance(x, bool)]
        if not clean_ids:
            raise ValueError("Выберите хотя бы одного лида")
        ph = ",".join("?" for _ in clean_ids)
        clauses.append(f"id IN ({ph})")
        params.extend(clean_ids)
    else:
        if status and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip())
        if folder_id is not None:
            clauses.append("folder_id = ?")
            params.append(int(folder_id))
        if primary_provider is not None:
            clauses.append("COALESCE(provider, 'telegram') = ?")
            params.append(primary_provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"

    lead_rows = [as_dict(r) for r in conn.execute(sql, params).fetchall()]

    existing_rows = conn.execute(
        "SELECT username, phone, lead_id, telegram_user_id FROM campaign_recipients WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    seen_usernames = {str(r["username"]).lower() for r in existing_rows if r["username"]}
    seen_phones = {str(r["phone"]) for r in existing_rows if r["phone"]}
    seen_leads = {int(r["lead_id"]) for r in existing_rows if r["lead_id"]}
    seen_tg_ids = {int(r["telegram_user_id"]) for r in existing_rows if r["telegram_user_id"]}

    now = utc_now()
    imported_count = 0
    skipped_count = 0

    for lead in lead_rows:
        lid = int(lead["id"])
        lead_provider = (lead.get("provider") or primary_provider or "telegram").strip().lower()
        username = _clean_handle(lead.get("username"))
        phone = (lead.get("phone") or "").strip() or None
        tg_id = lead.get("telegram_user_id")
        name = (lead.get("name") or "").strip() or None

        if not username or not tg_id or not phone:
            dlg = conn.execute(
                "SELECT * FROM dialogs WHERE lead_id = ? ORDER BY id LIMIT 1",
                (lid,),
            ).fetchone()
            if dlg is not None:
                dlg_dict = as_dict(dlg)
                if not username and dlg_dict.get("username"):
                    username = _clean_handle(dlg_dict["username"])
                if not tg_id and dlg_dict.get("peer_id"):
                    tg_id = int(dlg_dict["peer_id"])
                if not phone and dlg_dict.get("phone"):
                    phone = str(dlg_dict["phone"]).strip()
                if not name and dlg_dict.get("title"):
                    name = str(dlg_dict["title"]).strip()

        if tg_id is None and lead_provider == "whatsapp" and phone:
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 7:
                tg_id = int(digits[-12:])

        if tg_id is None:
            tg_id = 900000 + lid
            conn.execute(
                "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                (tg_id, now, lid),
            )

        if (
            lid in seen_leads
            or (tg_id and int(tg_id) in seen_tg_ids)
            or (username and username.lower() in seen_usernames)
            or (phone and phone in seen_phones)
        ):
            skipped_count += 1
            continue

        company = (lead.get("description") or "").strip() or None
        if name and "·" in name and not company:
            parts = [p.strip() for p in name.split("·", 1)]
            if len(parts) == 2 and parts[1]:
                company = parts[1]
        note = (lead.get("note") or "").strip() or None
        website = (lead.get("url") or "").strip() or None

        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                phone, company, note, website, current_step, status,
                last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', NULL, NULL, ?, ?)
            """,
            (
                campaign_id,
                lid,
                int(tg_id),
                username,
                name or username or phone or f"Лид #{lid}",
                phone,
                company,
                note,
                website,
                now,
                now,
            ),
        )
        seen_leads.add(lid)
        if tg_id:
            seen_tg_ids.add(int(tg_id))
        if username:
            seen_usernames.add(username.lower())
        if phone:
            seen_phones.add(phone)
        imported_count += 1

    conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, campaign_id))
    conn.commit()
    updated_campaign = get_campaign(conn, campaign_id)
    return {
        "imported_count": imported_count,
        "imported": imported_count,
        "skipped_count": skipped_count,
        "total_recipients": updated_campaign["total_recipients"],
        "campaign": updated_campaign,
    }


def _ensure_recipient_dialog(
    conn, account_id: int, rec: dict, now: str, provider: str | None = None
) -> dict:
    resolved_provider = (provider or _account_provider(conn, account_id) or "telegram").strip().lower()
    phone = (rec.get("phone") or "").strip() or None
    phone_digits = re.sub(r"\D", "", phone or "")
    if resolved_provider == "whatsapp":
        if phone_digits and len(phone_digits) >= 7:
            peer_id = int(phone_digits[-12:])
            if not phone.startswith("+"):
                phone = f"+{phone_digits}"
        elif rec.get("telegram_user_id"):
            peer_id = int(rec["telegram_user_id"])
        else:
            peer_id = 900000 + int(rec.get("lead_id") or rec["id"])
    else:
        peer_id = int(rec.get("telegram_user_id") or (900000 + int(rec.get("lead_id") or rec["id"])))

    username = _clean_handle(rec.get("username"))
    title = (rec.get("name") or "").strip()
    if rec.get("company") and rec["company"] not in title:
        title = f"{title} · {rec['company']}" if title else rec["company"]
    if not title:
        title = username or phone or str(peer_id)
    lead_id = rec.get("lead_id")

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_from_peer, muted, lead_id,
            created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN dialogs.title IS NULL OR dialogs.title = '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            lead_id = COALESCE(dialogs.lead_id, excluded.lead_id),
            updated_at = excluded.updated_at
        """,
        (account_id, peer_id, username, title, phone, resolved_provider, lead_id, now, now),
    )
    row = conn.execute(
        "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
        (account_id, peer_id),
    ).fetchone()
    dlg = as_dict(row)
    if lead_id and dlg:
        conn.execute(
            "UPDATE leads SET dialog_id = ? WHERE id = ? AND dialog_id IS NULL",
            (dlg["id"], lead_id),
        )
    return dlg


async def run_campaign_batch(
    conn,
    campaign_id: int,
    gateway=None,
    max_send: int | None = None,
) -> dict:
    camp = get_campaign(conn, campaign_id)
    account_ids = camp.get("account_ids") or []
    if not account_ids:
        raise ValueError("Выберите хотя бы один подключённый аккаунт для рассылки")

    connected_states = []
    for order_idx, aid in enumerate(account_ids):
        try:
            acc_raw = load_account(conn, int(aid))
        except LookupError:
            continue
        if acc_raw.get("status") != "connected":
            continue
        acc_pub = public_account(acc_raw)
        connected_states.append(
            {
                "id": int(acc_raw["id"]),
                "order": order_idx,
                "raw": acc_raw,
                "daily_limit": int(acc_pub["daily_limit"]),
                "sent_today": int(acc_pub["sent_today"]),
                "sent_in_batch": 0,
            }
        )

    if not connected_states:
        raise ValueError("Выберите хотя бы один подключённый аккаунт для рассылки")

    steps = camp.get("steps") or []
    if not steps:
        raise ValueError("Добавьте хотя бы один шаг сообщения в рассылку")

    total_account_remaining = sum(
        max(0, st["daily_limit"] - st["sent_today"]) for st in connected_states
    )
    if total_account_remaining <= 0:
        first_limit = connected_states[0]["daily_limit"]
        raise ValueError(
            f"Достигнут суточный лимит аккаунта ({first_limit} сообщ./сутки)"
        )

    campaign_daily_limit = int(camp.get("daily_limit") or 20)
    campaign_sent_today = int(camp.get("sent_today") or 0)
    campaign_remaining = max(0, campaign_daily_limit - campaign_sent_today)
    if campaign_remaining <= 0:
        raise ValueError(
            f"Достигнут суточный лимит рассылки ({campaign_daily_limit} сообщ./сутки)"
        )

    cap = int(max_send) if max_send is not None and int(max_send) > 0 else 500
    effective_quota = min(total_account_remaining, campaign_remaining, cap)

    eligible_rows = conn.execute(
        """
        SELECT * FROM campaign_recipients
        WHERE campaign_id = ?
          AND status IN ('pending', 'in_progress')
          AND current_step < ?
        ORDER BY id
        """,
        (campaign_id, len(steps)),
    ).fetchall()

    sent_count = 0
    sent_by_account: dict[str, int] = {}
    state_by_id = {st["id"]: st for st in connected_states}

    for row in eligible_rows:
        if sent_count >= effective_quota:
            break
        rec = as_dict(row)
        step_idx = int(rec.get("current_step") or 0)
        if step_idx >= len(steps):
            continue

        available = [
            st for st in connected_states if st["sent_today"] < st["daily_limit"]
        ]
        if not available:
            break

        assigned_id = rec.get("assigned_account_id")
        if (
            assigned_id is not None
            and int(assigned_id) in state_by_id
            and state_by_id[int(assigned_id)]["sent_today"] < state_by_id[int(assigned_id)]["daily_limit"]
        ):
            chosen_state = state_by_id[int(assigned_id)]
        else:
            chosen_state = min(
                available,
                key=lambda st: (st["sent_in_batch"], st["sent_today"], st["order"]),
            )

        chosen_account_id = chosen_state["id"]
        account_raw = chosen_state["raw"]
        acc_provider = (account_raw.get("provider") or "telegram").strip().lower()
        session_path = str(account_raw.get("session_path") or "")
        use_gateway = gateway is not None and not session_path.startswith("data/sessions/demo")

        step = steps[step_idx]
        if step_idx == 0 and (rec.get("step1_text") or "").strip():
            rendered_text = render_template(rec["step1_text"], rec)
        elif step_idx == 1 and (rec.get("step2_text") or "").strip():
            rendered_text = render_template(rec["step2_text"], rec)
        elif step_idx == 2 and (rec.get("step3_text") or "").strip():
            rendered_text = render_template(rec["step3_text"], rec)
        else:
            rendered_text = render_template(step["template_text"], rec)

        now = utc_now()
        try:
            if acc_provider == "whatsapp":
                dialog = _ensure_recipient_dialog(
                    conn, chosen_account_id, rec, now, provider="whatsapp"
                )
                phone_digits = re.sub(
                    r"\D", "", str(rec.get("phone") or dialog.get("phone") or "")
                )
                peer_target = phone_digits or rec.get("telegram_user_id") or dialog["peer_id"]
                if use_gateway:
                    sent = await gateway.send_message(
                        session_path, peer_target, rendered_text
                    )
                    tg_mid = int(sent["telegram_message_id"])
                    sent_at = str(sent.get("sent_at") or now)
                else:
                    next_row = conn.execute(
                        "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
                        (dialog["id"],),
                    ).fetchone()
                    tg_mid = int(next_row["next_id"])
                    sent_at = now
            else:
                if use_gateway and rec.get("username") and (not rec.get("telegram_user_id") or int(rec["telegram_user_id"]) >= 900000):
                    found = await gateway.search_dialogs(session_path, f"@{rec['username']}", limit=1)
                    if found and found[0].get("telegram_user_id"):
                        real_tg_id = int(found[0]["telegram_user_id"])
                        rec["telegram_user_id"] = real_tg_id
                        conn.execute(
                            "UPDATE campaign_recipients SET telegram_user_id = ? WHERE id = ?",
                            (real_tg_id, rec["id"]),
                        )
                        if rec.get("lead_id"):
                            try:
                                conn.execute(
                                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                    (real_tg_id, now, rec["lead_id"]),
                                )
                            except sqlite3.IntegrityError:
                                pass

                dialog = _ensure_recipient_dialog(
                    conn, chosen_account_id, rec, now, provider="telegram"
                )

                if use_gateway:
                    peer_target = (
                        f"@{rec['username']}"
                        if (int(dialog["peer_id"]) >= 900000 and rec.get("username"))
                        else dialog["peer_id"]
                    )
                    sent = await gateway.send_message(
                        session_path, peer_target, rendered_text, None
                    )
                    tg_mid = int(sent["telegram_message_id"])
                    sent_at = str(sent.get("sent_at") or now)
                else:
                    next_row = conn.execute(
                        "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
                        (dialog["id"],),
                    ).fetchone()
                    tg_mid = int(next_row["next_id"])
                    sent_at = now
        except Exception as exc:
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'failed', error_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(exc)[:300], now, rec["id"]),
            )
            continue

        conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, sent_at,
                reply_to_telegram_message_id, created_at, updated_at
            ) VALUES (?, ?, 'out', ?, ?, NULL, ?, ?)
            """,
            (dialog["id"], tg_mid, rendered_text, sent_at, now, now),
        )
        conn.execute(
            """
            UPDATE dialogs
            SET last_message_at = ?, last_message_preview = ?, last_from_peer = 0, updated_at = ?
            WHERE id = ?
            """,
            (sent_at, rendered_text[:120], now, dialog["id"]),
        )
        increment_daily_sent(conn, chosen_account_id)
        chosen_state["sent_today"] += 1
        chosen_state["sent_in_batch"] += 1
        sent_by_account[str(chosen_account_id)] = sent_by_account.get(str(chosen_account_id), 0) + 1

        next_step = step_idx + 1
        next_status = "completed" if next_step >= len(steps) else "in_progress"
        conn.execute(
            """
            UPDATE campaign_recipients
            SET current_step = ?, status = ?, assigned_account_id = ?, last_sent_at = ?, error_text = NULL, updated_at = ?
            WHERE id = ?
            """,
            (next_step, next_status, chosen_account_id, sent_at, now, rec["id"]),
        )
        sent_count += 1

    remaining_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM campaign_recipients
        WHERE campaign_id = ?
          AND status IN ('pending', 'in_progress')
          AND current_step < ?
        """,
        (campaign_id, len(steps)),
    ).fetchone()
    remaining_active = int(remaining_row["cnt"] or 0)
    new_camp_status = "completed" if remaining_active == 0 and camp["total_recipients"] > 0 else "running"
    conn.execute(
        "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
        (new_camp_status, utc_now(), campaign_id),
    )
    conn.commit()

    primary_account_id = connected_states[0]["id"]
    updated_campaign = get_campaign(conn, campaign_id)
    updated_account = public_account(load_account(conn, primary_account_id))
    return {
        "sent": sent_count,
        "sent_by_account": sent_by_account,
        "limit_reached": sent_count > 0 and sent_count == effective_quota,
        "remaining": remaining_active,
        "campaign": updated_campaign,
        "account": updated_account,
    }
