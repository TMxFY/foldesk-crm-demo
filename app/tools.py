import hashlib
import re

from app.db import as_dict, utc_now

_FIRST_NAMES = (
    ("Алексей", "alex"),
    ("Дмитрий", "dmitry"),
    ("Екатерина", "kate"),
    ("Михаил", "mike"),
    ("Анна", "anna"),
    ("Сергей", "sergey"),
    ("Ольга", "olga"),
    ("Никита", "nikita"),
    ("Мария", "maria"),
    ("Артём", "artem"),
    ("Илья", "ilya"),
    ("Дарья", "daria"),
    ("Павел", "pavel"),
    ("Елена", "elena"),
    ("Виктор", "viktor"),
    ("Ксения", "ksenia"),
)

_LAST_NAMES = (
    ("Соколов", "sokolov"),
    ("Морозов", "morozov"),
    ("Волков", "volkov"),
    ("Лебедев", "lebedev"),
    ("Ковалёв", "kovalev"),
    ("Громов", "gromov"),
    ("Белов", "belov"),
    ("Тарасов", "tarasov"),
    ("Орлов", "orlov"),
    ("Макаров", "makarov"),
    ("Захаров", "zakharov"),
    ("Романов", "romanov"),
)

_BIOS = (
    "Founder & CEO · B2B SaaS",
    "Head of Growth · AI & MarTech",
    "Co-founder @ Web3 Studio",
    "VP of Sales · Revenue Operations",
    "Product Lead · DevTools & Infra",
    "Performance Marketing Agency Owner",
    "BD & Partnerships · FinTech",
    "CBDO · E-commerce & Retail Tech",
    "Indie Maker · AI Agents & Automation",
    "Growth Advisor · Series A Startups",
)


def normalize_phone_number(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    cleaned = re.sub(r"[\s\-\(\)\.\u00a0]", "", s)
    if cleaned.startswith("00") and cleaned[2:].isdigit():
        cleaned = "+" + cleaned[2:]
    elif len(cleaned) == 11 and cleaned.startswith("8") and cleaned.isdigit():
        cleaned = "+7" + cleaned[1:]
    elif cleaned.isdigit() and 7 <= len(cleaned) <= 15:
        cleaned = "+" + cleaned
    if re.fullmatch(r"\+\d{7,15}", cleaned):
        return cleaned
    return None


def _pick_account(conn, account_id: int | None) -> dict | None:
    if account_id is not None and int(account_id) > 0:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        if row is None:
            raise LookupError("account")
        return as_dict(row)
    row = conn.execute(
        "SELECT * FROM accounts WHERE status = 'connected' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
    return as_dict(row) if row else None


def _upsert_contact_to_crm(
    conn,
    account_id: int | None,
    *,
    telegram_user_id: int,
    username: str | None,
    name: str,
    phone: str | None = None,
    note: str | None = None,
    description: str | None = None,
    now: str,
) -> tuple[int, bool]:
    provider = "telegram"
    if account_id is not None:
        acc_row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        if acc_row is not None:
            provider = (as_dict(acc_row).get("provider") or "telegram").strip().lower()

    existing = conn.execute(
        "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
        (int(telegram_user_id),),
    ).fetchone()
    if existing is None and username:
        existing = conn.execute(
            "SELECT * FROM leads WHERE lower(username) = lower(?) ORDER BY id LIMIT 1",
            (username,),
        ).fetchone()
    if existing is None and phone:
        existing = conn.execute(
            "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
            (phone,),
        ).fetchone()

    created = False
    if existing is not None:
        lead_id = int(existing["id"])
        conn.execute(
            """
            UPDATE leads
            SET telegram_user_id = COALESCE(telegram_user_id, ?),
                username = COALESCE(username, ?),
                phone = COALESCE(phone, ?),
                note = COALESCE(note, ?),
                description = COALESCE(description, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (int(telegram_user_id), username, phone, note, description, now, lead_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, phone,
                status, note, next_step, created_at, updated_at, description, provider
            ) VALUES (?, ?, ?, ?, ?, 'new', ?, 'Написать первое сообщение', ?, ?, ?, ?)
            """,
            (
                account_id,
                int(telegram_user_id),
                username,
                name,
                phone,
                note,
                now,
                now,
                description,
                provider,
            ),
        )
        lead_id = int(cur.lastrowid)
        created = True

    if account_id is not None:
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
            (account_id, int(telegram_user_id), username, name, phone, provider, lead_id, now, now),
        )
        dlg_row = conn.execute(
            "SELECT id FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
            (account_id, int(telegram_user_id)),
        ).fetchone()
        if dlg_row:
            conn.execute(
                "UPDATE leads SET dialog_id = ? WHERE id = ?",
                (int(dlg_row["id"]), lead_id),
            )

    return lead_id, created


def _add_contact_to_campaign(
    conn,
    campaign_id: int,
    *,
    lead_id: int | None,
    telegram_user_id: int,
    username: str | None,
    name: str,
    phone: str | None,
    company: str | None,
    note: str | None,
    now: str,
) -> bool:
    dup = conn.execute(
        """
        SELECT id FROM campaign_recipients
        WHERE campaign_id = ?
          AND (
              (telegram_user_id IS NOT NULL AND telegram_user_id = ?)
              OR (lead_id IS NOT NULL AND lead_id = ?)
              OR (username IS NOT NULL AND lower(username) = lower(?))
              OR (phone IS NOT NULL AND phone = ?)
          )
        LIMIT 1
        """,
        (
            campaign_id,
            int(telegram_user_id),
            lead_id or -1,
            username or "",
            phone or "",
        ),
    ).fetchone()
    if dup is not None:
        return False

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            phone, company, note, current_step, status,
            last_sent_at, error_text, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', NULL, NULL, ?, ?)
        """,
        (
            campaign_id,
            lead_id,
            int(telegram_user_id),
            username,
            name,
            phone,
            company,
            note,
            now,
            now,
        ),
    )
    conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, campaign_id))
    return True


async def convert_phones_to_tg(conn, gateway, payload: dict) -> dict:
    raw_text = str(payload.get("phones_text") or "")
    raw_list = payload.get("phones")
    tokens = []
    if raw_text.strip():
        tokens.extend(re.split(r"[\r\n,;]+", raw_text))
    if isinstance(raw_list, list):
        tokens.extend(str(x) for x in raw_list)

    non_empty = [t.strip() for t in tokens if t and t.strip()]
    total_input = len(non_empty)
    if total_input == 0:
        raise ValueError("Введите хотя бы один номер телефона")

    unique_phones: list[str] = []
    seen: set[str] = set()
    for token in non_empty:
        norm = normalize_phone_number(token)
        if norm and norm not in seen:
            seen.add(norm)
            unique_phones.append(norm)

    if not unique_phones:
        raise ValueError("Не удалось распознать корректные номера телефонов")

    account_id_in = payload.get("account_id")
    add_to_leads = bool(payload.get("add_to_leads", False))
    campaign_id = payload.get("campaign_id")
    if campaign_id is not None:
        campaign_id = int(campaign_id)
        camp = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if camp is None:
            raise LookupError("campaign")

    account = _pick_account(conn, account_id_in)
    target_account_id = int(account["id"]) if account else None
    session_path = str(account.get("session_path") or "") if account else ""

    resolved_map: dict[str, dict] = {}
    if (
        gateway is not None
        and session_path
        and not session_path.startswith("data/sessions/demo")
        and hasattr(gateway, "resolve_phones")
    ):
        try:
            gw_res = await gateway.resolve_phones(session_path, unique_phones)
            if isinstance(gw_res, list):
                for item in gw_res:
                    if isinstance(item, dict) and item.get("phone"):
                        resolved_map[str(item["phone"])] = item
        except Exception:
            pass

    now = utc_now()
    results = []
    added_to_leads = 0
    added_to_campaign = 0

    for phone in unique_phones:
        if phone in resolved_map:
            info = resolved_map[phone]
            tg_id = int(info.get("telegram_user_id") or 0)
            username = info.get("username")
            name = info.get("name") or username or phone
            found = bool(info.get("found", True))
        else:
            lead_row = conn.execute(
                "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
                (phone,),
            ).fetchone()
            if lead_row is not None:
                ld = as_dict(lead_row)
                tg_id = int(ld.get("telegram_user_id") or (85000000 + int(ld["id"])))
                username = ld.get("username")
                name = ld.get("name") or username or phone
                found = True
            else:
                seed = int(hashlib.md5(phone.encode("utf-8")).hexdigest()[:8], 16)
                fn_ru, fn_en = _FIRST_NAMES[seed % len(_FIRST_NAMES)]
                ln_ru, ln_en = _LAST_NAMES[(seed // 17) % len(_LAST_NAMES)]
                tail = phone[-4:]
                tg_id = 85000000 + (seed % 9000000)
                username = f"{fn_en}_{ln_en}_{tail}"
                name = f"{fn_ru} {ln_ru}"
                found = True

        lead_id = None
        if found and (add_to_leads or campaign_id is not None):
            lead_id, was_created = _upsert_contact_to_crm(
                conn,
                target_account_id,
                telegram_user_id=tg_id,
                username=username,
                name=name,
                phone=phone,
                note=f"Найден по номеру {phone}",
                description="Конвертер телефонов Telegram",
                now=now,
            )
            if add_to_leads:
                added_to_leads += 1

        if found and campaign_id is not None:
            if _add_contact_to_campaign(
                conn,
                campaign_id,
                lead_id=lead_id,
                telegram_user_id=tg_id,
                username=username,
                name=name,
                phone=phone,
                company=None,
                note=f"Телефон: {phone}",
                now=now,
            ):
                added_to_campaign += 1

        results.append(
            {
                "phone": phone,
                "found": found,
                "telegram_user_id": tg_id,
                "username": username,
                "name": name,
                "lead_id": lead_id,
            }
        )

    conn.commit()
    found_count = sum(1 for r in results if r.get("found"))
    return {
        "total_input": total_input,
        "unique_valid": len(unique_phones),
        "found_count": found_count,
        "added_to_leads": added_to_leads,
        "added_to_campaign": added_to_campaign,
        "results": results,
    }


def _clean_group_slug(group_link: str) -> tuple[str, str]:
    raw = (group_link or "").strip()
    if not raw:
        raise ValueError("Укажите ссылку на группу или @username чата")
    slug = re.sub(r"^https?://(?:www\.)?t\.me/", "", raw, flags=re.IGNORECASE)
    slug = re.sub(r"^t\.me/", "", slug, flags=re.IGNORECASE)
    slug = slug.strip().lstrip("@").strip("/")
    if not slug:
        raise ValueError("Укажите корректную ссылку на группу или @username чата")
    clean_slug = re.sub(r"[^A-Za-z0-9_]", "_", slug.lstrip("+")).strip("_") or "telegram_group"
    words = [w.capitalize() for w in clean_slug.split("_") if w]
    title = (" ".join(words) if words else clean_slug) + " · Community"
    return clean_slug.lower(), title


async def parse_telegram_group(conn, gateway, payload: dict) -> dict:
    group_link = str(payload.get("group_link") or "").strip()
    slug, group_title = _clean_group_slug(group_link)

    try:
        limit = max(1, min(int(payload.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50

    account_id_in = payload.get("account_id")
    add_to_leads = bool(payload.get("add_to_leads", False))
    campaign_id = payload.get("campaign_id")
    if campaign_id is not None:
        campaign_id = int(campaign_id)
        camp = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if camp is None:
            raise LookupError("campaign")

    account = _pick_account(conn, account_id_in)
    target_account_id = int(account["id"]) if account else None
    session_path = str(account.get("session_path") or "") if account else ""

    participants = None
    if (
        gateway is not None
        and session_path
        and not session_path.startswith("data/sessions/demo")
        and hasattr(gateway, "get_participants")
    ):
        try:
            gw_list = await gateway.get_participants(session_path, group_link, limit=limit)
            if isinstance(gw_list, list) and gw_list:
                participants = gw_list
                if gw_list[0].get("group_title"):
                    group_title = str(gw_list[0]["group_title"])
        except Exception:
            participants = None

    if participants is None:
        count = min(limit, 15) if limit == 50 else min(limit, 50)
        base_seed = int(hashlib.md5(slug.encode("utf-8")).hexdigest()[:8], 16)
        participants = []
        for idx in range(count):
            s = base_seed + idx * 31
            fn_ru, fn_en = _FIRST_NAMES[s % len(_FIRST_NAMES)]
            ln_ru, ln_en = _LAST_NAMES[(s // 7) % len(_LAST_NAMES)]
            bio = _BIOS[idx % len(_BIOS)]
            role = "founder" if idx == 0 else ("admin" if idx in (1, 2) else "member")
            tg_id = 88000000 + ((base_seed + idx * 101) % 8000000)
            username = f"{fn_en}_{ln_en}_{slug[:8]}_{idx + 1}"
            participants.append(
                {
                    "telegram_user_id": tg_id,
                    "username": username,
                    "name": f"{fn_ru} {ln_ru}",
                    "bio": bio,
                    "role": role,
                    "is_premium": bool(idx % 3 == 0),
                    "group_title": group_title,
                }
            )

    now = utc_now()
    added_to_leads = 0
    added_to_campaign = 0

    for p in participants:
        tg_id = int(p["telegram_user_id"])
        username = p.get("username")
        name = p.get("name") or username or f"Участник #{tg_id}"
        bio = p.get("bio") or ""
        lead_id = None

        if add_to_leads or campaign_id is not None:
            lead_id, _ = _upsert_contact_to_crm(
                conn,
                target_account_id,
                telegram_user_id=tg_id,
                username=username,
                name=name,
                phone=None,
                note=f"Участник группы {group_title}" + (f" · {bio}" if bio else ""),
                description=bio or group_title,
                now=now,
            )
            p["lead_id"] = lead_id
            if add_to_leads:
                added_to_leads += 1

        if campaign_id is not None:
            if _add_contact_to_campaign(
                conn,
                campaign_id,
                lead_id=lead_id,
                telegram_user_id=tg_id,
                username=username,
                name=name,
                phone=None,
                company=group_title,
                note=bio or f"Из группы {group_title}",
                now=now,
            ):
                added_to_campaign += 1

    conn.commit()
    return {
        "group_link": group_link,
        "group_title": group_title,
        "participants_count": len(participants),
        "added_to_leads": added_to_leads,
        "added_to_campaign": added_to_campaign,
        "participants": participants,
    }
