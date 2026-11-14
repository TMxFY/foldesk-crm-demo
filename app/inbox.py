import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from app.accounts import check_daily_limit, increment_daily_sent
from app.db import INBOX_MEDIA_DIR, ROOT, as_dict, normalize_status, utc_now
from app.telegram_gateway import GatewayError, render_formatted_html


def _media_preview_label(
    media_type: str | None,
    text: str | None,
    filename: str | None = None,
) -> str:
    if text is not None and str(text).strip():
        return str(text)[:120]
    mt = str(media_type or "").strip().lower()
    if mt == "voice":
        return "🎤 Голосовое сообщение"
    if mt == "video_note":
        return "🔵 Видеосообщение"
    if mt == "photo":
        return "📷 Фото"
    if mt == "video":
        return "🎬 Видео"
    if mt == "audio":
        return "🎵 Аудио"
    if mt == "document":
        return f"📎 {filename}"[:120] if filename else "📎 Файл"
    return "" if text is None else str(text)[:120]


def _is_synthetic_peer_id(peer_id) -> bool:
    try:
        return 900000 <= int(peer_id or 0) < 1000000
    except (TypeError, ValueError):
        return False


def _ensure_lead_events_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


def _record_lead_event_safe(
    conn,
    lead_id: int,
    event_type: str,
    title: str,
    meta: dict | None = None,
    now: str | None = None,
) -> None:
    if not lead_id:
        return
    ts = now or utc_now()
    meta_str = json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else None
    try:
        _ensure_lead_events_table(conn)
        conn.execute(
            """
            INSERT INTO lead_events (lead_id, event_type, title, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(lead_id), str(event_type), str(title), meta_str, ts),
        )
    except Exception:
        pass


def _account_provider(conn, account_id: int, *, require_exists: bool = False) -> str:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        if require_exists:
            raise LookupError("account")
        return "telegram"
    data = as_dict(row)
    prov = (data.get("provider") or "telegram").strip().lower()
    return prov if prov in ("telegram", "whatsapp") else "telegram"


def _extract_dialog_phone(item: dict, peer_id: int | None, provider: str) -> str | None:
    raw = (item.get("phone") or "").strip()
    if raw:
        digits = re.sub(r"\D", "", raw)
        if digits and len(digits) >= 7:
            return raw if raw.startswith("+") else f"+{digits}"
        return raw
    if provider == "whatsapp" and peer_id is not None:
        peer_str = str(peer_id).strip()
        if peer_str.isdigit() and 7 <= len(peer_str) <= 15 and not peer_str.startswith("900"):
            return f"+{peer_str}"
    return None


def _public(row) -> dict:
    data = as_dict(row)
    provider = data.get("provider") or "telegram"
    phone = data.get("phone")
    if not phone and provider == "whatsapp":
        peer_str = str(data.get("peer_id") or "").strip()
        if peer_str.isdigit() and 7 <= len(peer_str) <= 15 and not peer_str.startswith("900"):
            phone = f"+{peer_str}"
    resolved_lead_id = (
        data["lead_id"]
        if data.get("lead_id") is not None
        else data.get("resolved_lead_id")
    )
    out = {
        "id": data["id"],
        "dialog_id": data["id"],
        "account_id": data["account_id"],
        "telegram_user_id": data["peer_id"],
        "username": data["username"],
        "name": data["title"] or "",
        "phone": phone,
        "provider": provider,
        "peer_type": data["peer_type"],
        "unread_count": data["unread_count"],
        "muted": data["muted"],
        "is_group": data["is_group"],
        "last_message_at": data["last_message_at"],
        "last_message_preview": data["last_message_preview"],
        "last_from_peer": data["last_from_peer"],
        "lead_id": resolved_lead_id,
    }
    if "account_phone" in data:
        out["account_phone"] = data["account_phone"]
    if "account_name" in data:
        out["account_name"] = data["account_name"]
    if "account_username" in data:
        out["account_username"] = data["account_username"]
    if "lead_status" in data and data["lead_status"] is not None:
        out["lead_status"] = data["lead_status"]
    return out


def stop_outreach_on_reply(
    conn,
    account_id: int | None = None,
    peer_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
    lead_id: int | None = None,
    preview: str | None = None,
    force_event: bool = False,
    min_sent_at: str | None = None,
    opener=None,
) -> int:
    now = utc_now()
    clean_min_sent = str(min_sent_at).strip() if min_sent_at else None
    clean_user = str(username).strip().lstrip("@") if username else None
    clean_phone = str(phone).strip() if phone else None
    phone_digits = re.sub(r"\D", "", clean_phone or "")
    phone_variants = set()
    if clean_phone:
        phone_variants.add(clean_phone)
    if phone_digits and len(phone_digits) >= 7:
        phone_variants.add(phone_digits)
        phone_variants.add(f"+{phone_digits}")
    phone_peer_id = int(phone_digits[-12:]) if (phone_digits and len(phone_digits) >= 7) else None
    clean_peer_id = int(peer_id) if peer_id is not None else phone_peer_id
    clean_lead_id = int(lead_id) if lead_id is not None else None

    if clean_peer_id in (777000, 178220800):
        return 0

    if not any((clean_lead_id, clean_peer_id, clean_user, phone_variants)):
        return 0

    # Find matching leads first so we can also match campaign_recipients by lead_id
    lead_clauses = []
    lead_params: list = []
    if clean_lead_id is not None:
        lead_clauses.append("id = ?")
        lead_params.append(clean_lead_id)
    if clean_peer_id is not None:
        lead_clauses.append("telegram_user_id = ?")
        lead_params.append(clean_peer_id)
    if clean_user:
        lead_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
        lead_params.append(clean_user)
    if phone_variants:
        ph_v = ",".join("?" for _ in phone_variants)
        lead_clauses.append(f"(phone IS NOT NULL AND phone IN ({ph_v}))")
        lead_params.extend(sorted(phone_variants))

    matched_lead_ids: set[int] = set()
    prev_lead_statuses: dict[int, str] = {}
    if clean_lead_id is not None:
        matched_lead_ids.add(clean_lead_id)

    if lead_clauses:
        lead_rows = conn.execute(
            f"SELECT id, status FROM leads WHERE {' OR '.join(lead_clauses)}",
            lead_params,
        ).fetchall()
        for lr in lead_rows:
            lid = int(lr["id"])
            matched_lead_ids.add(lid)
            prev_lead_statuses[lid] = str(lr["status"] or "")

    # Also find matching campaign_recipients
    rec_clauses = []
    rec_params: list = []
    if matched_lead_ids:
        ph = ",".join("?" for _ in matched_lead_ids)
        rec_clauses.append(f"lead_id IN ({ph})")
        rec_params.extend(sorted(matched_lead_ids))
    if clean_peer_id is not None:
        rec_clauses.append("telegram_user_id = ?")
        rec_params.append(clean_peer_id)
    if clean_user:
        rec_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
        rec_params.append(clean_user)
    if phone_variants:
        ph_v = ",".join("?" for _ in phone_variants)
        rec_clauses.append(f"(phone IS NOT NULL AND phone IN ({ph_v}))")
        rec_params.extend(sorted(phone_variants))

    stopped_count = 0
    stopped_campaign_ids: set[int] = set()
    candidate_account_ids: list[int] = []
    candidate_usernames: list[str] = []
    candidate_names: list[str] = []
    candidate_phones: list[str] = []
    all_rec_rows = []
    had_active_recs = False
    has_any_recipient = False

    if rec_clauses:
        all_rec_rows = conn.execute(
            f"""
            SELECT id, campaign_id, lead_id, status, last_sent_at,
                   sent_account_id, assigned_account_id, telegram_user_id,
                   username, name, phone, company, note, website, category, their_angle
            FROM campaign_recipients
            WHERE {' OR '.join(rec_clauses)}
            """,
            rec_params,
        ).fetchall()
        has_any_recipient = len(all_rec_rows) > 0
        for rr in all_rec_rows:
            if rr["lead_id"]:
                lid_existing = int(rr["lead_id"])
                matched_lead_ids.add(lid_existing)
                if lid_existing not in prev_lead_statuses:
                    lrow_ex = conn.execute("SELECT status FROM leads WHERE id = ?", (lid_existing,)).fetchone()
                    if lrow_ex is not None:
                        prev_lead_statuses[lid_existing] = str(lrow_ex["status"] or "")
            if rr["username"]:
                u = str(rr["username"]).strip().lstrip("@")
                if u and u not in candidate_usernames:
                    candidate_usernames.append(u)
            if rr["name"]:
                n = str(rr["name"]).strip()
                if n and n not in candidate_names:
                    candidate_names.append(n)
            if rr["phone"]:
                p = str(rr["phone"]).strip()
                if p and p not in candidate_phones:
                    candidate_phones.append(p)
            for cand_acc in (rr["sent_account_id"], rr["assigned_account_id"]):
                if cand_acc is not None:
                    aid = int(cand_acc)
                    if aid not in candidate_account_ids:
                        candidate_account_ids.append(aid)

        active_rec_rows = [
            rr for rr in all_rec_rows if rr["status"] in ("pending", "in_progress", "completed")
        ]
        had_active_recs = len(active_rec_rows) > 0
        if clean_min_sent:
            active_rec_rows = [
                rr
                for rr in active_rec_rows
                if not rr["last_sent_at"] or str(rr["last_sent_at"]) <= clean_min_sent
            ]
        for rr in active_rec_rows:
            if rr["lead_id"]:
                lid = int(rr["lead_id"])
                matched_lead_ids.add(lid)
                if lid not in prev_lead_statuses:
                    lrow = conn.execute("SELECT status FROM leads WHERE id = ?", (lid,)).fetchone()
                    if lrow is not None:
                        prev_lead_statuses[lid] = str(lrow["status"] or "")
            if rr["campaign_id"]:
                stopped_campaign_ids.add(int(rr["campaign_id"]))
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'replied', updated_at = ?
                WHERE id = ?
                """,
                (now, int(rr["id"])),
            )
            stopped_count += 1

    dlg_row = None
    if account_id is not None and clean_peer_id is not None:
        dlg_row = conn.execute(
            "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
            (int(account_id), int(clean_peer_id)),
        ).fetchone()
    elif clean_peer_id is not None:
        dlg_row = conn.execute(
            "SELECT * FROM dialogs WHERE peer_type = 'user' AND peer_id = ? ORDER BY last_message_at DESC LIMIT 1",
            (int(clean_peer_id),),
        ).fetchone()
    elif clean_user:
        dlg_row = conn.execute(
            "SELECT * FROM dialogs WHERE lower(username) = lower(?) ORDER BY last_message_at DESC LIMIT 1",
            (clean_user,),
        ).fetchone()
    elif matched_lead_ids:
        ph_lids = ",".join("?" for _ in matched_lead_ids)
        dlg_row = conn.execute(
            f"SELECT * FROM dialogs WHERE lead_id IN ({ph_lids}) ORDER BY last_message_at DESC LIMIT 1",
            sorted(matched_lead_ids),
        ).fetchone()

    if clean_min_sent and had_active_recs and stopped_count == 0 and not force_event:
        return 0

    if has_any_recipient and not matched_lead_ids and all_rec_rows:
        first_rec = all_rec_rows[0]
        auto_acc_id = (
            int(account_id)
            if account_id is not None
            else (
                candidate_account_ids[0]
                if candidate_account_ids
                else (int(dlg_row["account_id"]) if dlg_row and dlg_row["account_id"] is not None else None)
            )
        )
        auto_tg_id = (
            clean_peer_id
            if clean_peer_id is not None
            else (int(first_rec["telegram_user_id"]) if first_rec["telegram_user_id"] is not None else None)
        )
        auto_user = clean_user or (candidate_usernames[0] if candidate_usernames else None)
        if not auto_user and dlg_row and dlg_row["username"]:
            auto_user = str(dlg_row["username"]).strip().lstrip("@") or None
        auto_phone = clean_phone or (candidate_phones[0] if candidate_phones else None)
        if not auto_phone and dlg_row and dlg_row["phone"]:
            auto_phone = str(dlg_row["phone"]).strip() or None
        auto_company = str(first_rec["company"] or "").strip() or None
        auto_note = (
            str(first_rec["note"] or "").strip()
            or str(first_rec["their_angle"] or "").strip()
            or None
        )
        auto_url = str(first_rec["website"] or "").strip() or None
        if auto_url and not re.match(r"^https?://", auto_url, flags=re.IGNORECASE):
            auto_url = f"https://{auto_url}"
        auto_niche = str(first_rec["category"] or "").strip() or None
        auto_name = candidate_names[0] if candidate_names else None
        if not auto_name and dlg_row and dlg_row["title"]:
            auto_name = str(dlg_row["title"]).strip() or None
        if not auto_name:
            auto_name = auto_company or auto_user or auto_phone or (f"id:{auto_tg_id}" if auto_tg_id else "Контакт")
        auto_dlg_id = int(dlg_row["id"]) if dlg_row is not None else None
        auto_next_step = "Клиент ответил — цепочка остановлена, ответьте вручную"
        try:
            cur_lead = conn.execute(
                """
                INSERT INTO leads (
                    account_id, telegram_user_id, username, name, phone,
                    status, airtable_status, company, description, niche, url,
                    note, next_step, dialog_id, provider, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'talking', 'In conversation', ?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?)
                """,
                (
                    auto_acc_id,
                    auto_tg_id,
                    auto_user,
                    auto_name,
                    auto_phone,
                    auto_company,
                    auto_company,
                    auto_niche,
                    auto_url,
                    auto_note,
                    auto_next_step,
                    auto_dlg_id,
                    now,
                    now,
                ),
            )
            new_lid = int(cur_lead.lastrowid)
            matched_lead_ids.add(new_lid)
            prev_lead_statuses[new_lid] = "talking"
        except sqlite3.IntegrityError:
            if auto_tg_id is not None:
                ex_row = conn.execute(
                    "SELECT id, status FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
                    (auto_tg_id,),
                ).fetchone()
                if ex_row is not None:
                    ex_lid = int(ex_row["id"])
                    matched_lead_ids.add(ex_lid)
                    prev_lead_statuses[ex_lid] = str(ex_row["status"] or "")

    any_lead_status_moved = False
    if matched_lead_ids:
        primary_lid = sorted(matched_lead_ids)[0]
        if has_any_recipient and all_rec_rows:
            rec_ids = [int(r["id"]) for r in all_rec_rows]
            ph_recs = ",".join("?" for _ in rec_ids)
            conn.execute(
                f"""
                UPDATE campaign_recipients
                SET lead_id = COALESCE(lead_id, ?),
                    updated_at = ?
                WHERE id IN ({ph_recs})
                """,
                [primary_lid, now, *rec_ids],
            )
        if dlg_row is not None:
            conn.execute(
                """
                UPDATE dialogs
                SET lead_id = COALESCE(lead_id, ?)
                WHERE id = ?
                """,
                (primary_lid, int(dlg_row["id"])),
            )
        if account_id is not None and clean_peer_id is not None:
            conn.execute(
                """
                UPDATE dialogs
                SET lead_id = COALESCE(lead_id, ?)
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (primary_lid, int(account_id), int(clean_peer_id)),
            )

        ph = ",".join("?" for _ in matched_lead_ids)
        conn.execute(
            f"""
            UPDATE leads
            SET status = CASE WHEN status IN ('new', 'waiting') THEN 'talking' ELSE status END,
                airtable_status = CASE WHEN status IN ('new', 'waiting') THEN 'In conversation' ELSE airtable_status END,
                next_step = CASE
                    WHEN status IN ('new', 'waiting')
                    THEN 'Клиент ответил — цепочка остановлена, ответьте вручную'
                    WHEN status NOT IN ('won', 'lost')
                    THEN COALESCE(next_step, 'Клиент ответил — цепочка остановлена, ответьте вручную')
                    ELSE next_step
                END,
                updated_at = ?
            WHERE id IN ({ph})
            """,
            [now, *sorted(matched_lead_ids)],
        )

        for lid in sorted(matched_lead_ids):
            old_status = prev_lead_statuses.get(lid)
            status_moved = old_status in ("new", "waiting")
            if status_moved:
                any_lead_status_moved = True
            if stopped_count > 0 or status_moved or force_event:
                snippet = f": «{str(preview).strip()[:80]}»" if preview and str(preview).strip() else ""
                if stopped_count > 0:
                    title = f"Получен ответ от клиента{snippet} — рассылка остановлена, стадия «Разговор»"
                elif status_moved:
                    title = f"Получен ответ от клиента{snippet} — сделка переведена в «Разговор»"
                else:
                    title = f"Входящее сообщение от клиента{snippet}"
                _record_lead_event_safe(
                    conn,
                    lead_id=lid,
                    event_type="reply_received",
                    title=title,
                    meta={
                        "account_id": account_id,
                        "peer_id": clean_peer_id,
                        "username": clean_user,
                        "stopped_recipients": stopped_count,
                        "campaign_ids": sorted(stopped_campaign_ids),
                        "old_status": old_status,
                        "new_status": "talking" if status_moved else old_status,
                        "preview": str(preview)[:120] if preview else None,
                    },
                    now=now,
                )

    should_notify_colleague = (
        stopped_count > 0
        or any_lead_status_moved
        or (force_event and (bool(matched_lead_ids) or has_any_recipient or (dlg_row is not None)))
    )
    if should_notify_colleague:
        try:
            from app.digest import send_lead_reply_notification

            lrow_disp = None
            if matched_lead_ids:
                lrow_disp = conn.execute(
                    "SELECT username, name, phone, account_id FROM leads WHERE id = ?",
                    (sorted(matched_lead_ids)[0],),
                ).fetchone()
                if lrow_disp is not None and account_id is None and lrow_disp["account_id"] is not None:
                    aid = int(lrow_disp["account_id"])
                    if aid not in candidate_account_ids:
                        candidate_account_ids.append(aid)

            lead_username = clean_user
            if not lead_username and candidate_usernames:
                lead_username = candidate_usernames[0]
            if not lead_username and dlg_row and dlg_row["username"]:
                lead_username = str(dlg_row["username"]).strip().lstrip("@")
            if not lead_username and lrow_disp is not None:
                lu = str(lrow_disp["username"] or "").strip().lstrip("@")
                if lu:
                    lead_username = lu

            if lead_username:
                lead_display = f"@{lead_username}"
            else:
                lead_name = ""
                if candidate_names:
                    lead_name = candidate_names[0]
                elif dlg_row and dlg_row["title"]:
                    lead_name = str(dlg_row["title"]).strip()
                elif lrow_disp and lrow_disp["name"]:
                    lead_name = str(lrow_disp["name"]).strip()

                if lead_name:
                    lead_display = lead_name
                else:
                    lead_phone = clean_phone
                    if not lead_phone and candidate_phones:
                        lead_phone = candidate_phones[0]
                    elif not lead_phone and dlg_row and dlg_row["phone"]:
                        lead_phone = str(dlg_row["phone"]).strip()
                    elif not lead_phone and lrow_disp and lrow_disp["phone"]:
                        lead_phone = str(lrow_disp["phone"]).strip()

                    lead_display = (
                        lead_phone
                        or (f"id:{clean_peer_id}" if clean_peer_id else "unknown")
                    )

            resolved_acc_id = (
                int(account_id)
                if account_id is not None
                else (
                    candidate_account_ids[0]
                    if candidate_account_ids
                    else (int(dlg_row["account_id"]) if dlg_row and dlg_row["account_id"] is not None else None)
                )
            )
            acc_display = "unknown"
            if resolved_acc_id is not None:
                arow = conn.execute(
                    "SELECT telegram_username, telegram_name, phone FROM accounts WHERE id = ?",
                    (resolved_acc_id,),
                ).fetchone()
                if arow is not None:
                    au = str(arow["telegram_username"] or "").strip().lstrip("@")
                    if au:
                        acc_display = f"@{au}"
                    elif arow["telegram_name"]:
                        acc_display = str(arow["telegram_name"]).strip()
                    elif arow["phone"]:
                        acc_display = str(arow["phone"]).strip()
                    else:
                        acc_display = f"#{resolved_acc_id}"
            send_lead_reply_notification(
                conn,
                lead_handle=lead_display,
                account_handle=acc_display,
                preview=preview if os.getenv("BOT_INCLUDE_PREVIEW") == "1" else None,
                dialog_id=int(dlg_row["id"]) if dlg_row and dlg_row["id"] is not None else None,
                lead_id=sorted(matched_lead_ids)[0] if matched_lead_ids else lead_id,
                account_id=resolved_acc_id,
                peer_id=clean_peer_id,
                opener=opener,
            )
        except Exception:
            pass

    return stopped_count



def remember_dialogs(conn, account_id: int, people: list, opener=None) -> list:
    acc_provider = _account_provider(conn, account_id)
    now = utc_now()
    saved = []
    for person in people:
        peer_id = person.get("telegram_user_id")
        if peer_id is None and person.get("phone"):
            digits = re.sub(r"\D", "", str(person["phone"]))
            if len(digits) >= 7:
                peer_id = int(digits[-12:])
        if peer_id is None:
            continue
        title = (person.get("name") or "").strip()
        username = person.get("username")
        if username is not None:
            username = str(username).strip().lstrip("@") or None
        provider = str(person.get("provider") or acc_provider or "telegram").strip().lower()
        phone = _extract_dialog_phone(person, int(peer_id), provider)
        unread = person.get("unread_count") if "unread_count" in person else None
        if unread is not None:
            unread = max(0, int(unread))
        preview = person.get("last_message_preview")
        if preview is not None:
            preview = str(preview)[:120]
        sent_at = person.get("last_message_at")
        if sent_at is not None:
            sent_at = str(sent_at)
        last_from = person.get("last_from_peer") if "last_from_peer" in person else None
        if last_from is not None:
            last_from = 1 if last_from else 0
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, phone, provider,
                is_group, unread_count, last_message_at, last_message_preview,
                last_from_peer, muted, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                username = COALESCE(excluded.username, dialogs.username),
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
                phone = COALESCE(excluded.phone, dialogs.phone),
                provider = COALESCE(excluded.provider, dialogs.provider),
                unread_count = COALESCE(?, dialogs.unread_count),
                last_message_at = COALESCE(excluded.last_message_at, dialogs.last_message_at),
                last_message_preview = COALESCE(excluded.last_message_preview, dialogs.last_message_preview),
                last_from_peer = COALESCE(?, dialogs.last_from_peer),
                updated_at = excluded.updated_at
            """,
            (
                account_id,
                int(peer_id),
                username,
                title,
                phone,
                provider,
                0 if unread is None else unread,
                sent_at,
                preview,
                0 if last_from is None else last_from,
                now,
                now,
                unread,
                last_from,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM dialogs
            WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
            """,
            (account_id, int(peer_id)),
        ).fetchone()
        if row and int(row["last_from_peer"] or 0) == 1:
            stop_outreach_on_reply(
                conn,
                account_id=account_id,
                peer_id=int(row["peer_id"]),
                username=row["username"],
                phone=row["phone"],
                lead_id=row["lead_id"],
                preview=row["last_message_preview"],
                opener=opener,
            )
            row = conn.execute(
                "SELECT * FROM dialogs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        saved.append(_public(row))
    conn.commit()
    return saved


def apply_incoming(conn, account_id: int, message: dict, opener=None) -> dict:
    if not isinstance(message, dict) or message.get("is_group"):
        raise ValueError("Это не личный диалог")
    if (message.get("peer_type") or "user") != "user":
        raise ValueError("Это не личный диалог")
    if (message.get("direction") or "in") != "in":
        raise ValueError("Нужно входящее")
    peer_id = message.get("telegram_user_id")
    if peer_id is None and message.get("phone"):
        digits = re.sub(r"\D", "", str(message["phone"]))
        if len(digits) >= 7:
            peer_id = int(digits[-12:])
    if peer_id is None:
        raise ValueError("Это не личный диалог")
    acc_provider = _account_provider(conn, account_id, require_exists=True)
    provider = str(message.get("provider") or acc_provider or "telegram").strip().lower()
    phone = _extract_dialog_phone(message, int(peer_id), provider)
    now = utc_now()
    title = (message.get("name") or "").strip()
    username = message.get("username")
    if username is not None:
        username = str(username).strip().lstrip("@") or None
    text = message.get("text")
    media_url = message.get("media_url")
    media_type = message.get("media_type")
    media_mime = message.get("media_mime")
    media_filename = message.get("media_filename")
    media_duration = (
        int(message["media_duration"])
        if message.get("media_duration") is not None
        else None
    )
    raw_entities = (
        message.get("entities")
        if "entities" in message
        else message.get("entities_json")
    )
    if isinstance(raw_entities, list):
        entities_json = json.dumps(raw_entities, ensure_ascii=False) if raw_entities else None
    elif isinstance(raw_entities, str):
        entities_json = raw_entities.strip() or None
    else:
        entities_json = None
    formatted_html = message.get("formatted_html")
    if not formatted_html:
        formatted_html = render_formatted_html(
            text,
            raw_entities if isinstance(raw_entities, (list, str)) else entities_json,
        )
    edited_at = message.get("edited_at")
    is_edit = bool(message.get("is_edit"))
    reply_to_mid = (
        int(message["reply_to_telegram_message_id"])
        if message.get("reply_to_telegram_message_id")
        else None
    )
    preview = _media_preview_label(media_type, text, media_filename)
    sent_at = str(message.get("sent_at") or now)
    prev_dlg = conn.execute(
        """
        SELECT last_from_peer FROM dialogs
        WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
        """,
        (account_id, int(peer_id)),
    ).fetchone()
    was_already_from_peer = is_edit or bool(prev_dlg and int(prev_dlg["last_from_peer"] or 0) == 1)
    unread_inc = 0 if is_edit else 1
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, 0, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            unread_count = dialogs.unread_count + ?,
            last_message_at = excluded.last_message_at,
            last_message_preview = excluded.last_message_preview,
            last_from_peer = 1,
            updated_at = excluded.updated_at
        """,
        (
            account_id,
            int(peer_id),
            username,
            title,
            phone,
            provider,
            unread_inc,
            sent_at,
            preview,
            now,
            now,
            unread_inc,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM dialogs
        WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
        """,
        (account_id, int(peer_id)),
    ).fetchone()
    mid = message.get("telegram_message_id")
    if mid is not None:
        body = None if text is None else str(text)
        try:
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, media_url,
                    media_type, media_mime, media_filename, media_duration,
                    entities_json, formatted_html, edited_at,
                    sent_at, reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dialog_id, telegram_message_id) DO UPDATE SET
                    text = excluded.text,
                    formatted_html = excluded.formatted_html,
                    entities_json = excluded.entities_json,
                    edited_at = COALESCE(excluded.edited_at, messages.edited_at),
                    media_type = COALESCE(excluded.media_type, messages.media_type),
                    media_url = COALESCE(excluded.media_url, messages.media_url),
                    media_mime = COALESCE(excluded.media_mime, messages.media_mime),
                    media_duration = COALESCE(excluded.media_duration, messages.media_duration),
                    media_filename = COALESCE(excluded.media_filename, messages.media_filename),
                    sent_at = excluded.sent_at,
                    updated_at = excluded.updated_at
                """,
                (
                    row["id"],
                    int(mid),
                    body,
                    media_url,
                    media_type,
                    media_mime,
                    media_filename,
                    media_duration,
                    entities_json,
                    formatted_html,
                    edited_at,
                    sent_at,
                    reply_to_mid,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("Не удалось записать входящее") from exc
    stop_outreach_on_reply(
        conn,
        account_id=account_id,
        peer_id=int(row["peer_id"]),
        username=row["username"],
        phone=row["phone"],
        lead_id=row["lead_id"],
        preview=preview,
        force_event=not was_already_from_peer,
        opener=opener,
    )
    row = conn.execute(
        "SELECT * FROM dialogs WHERE id = ?",
        (row["id"],),
    ).fetchone()
    conn.commit()
    return _public(row)


def _person_is_crm_relevant(
    conn,
    account_id: int,
    peer_id: int,
    username: str | None,
    phone: str | None,
) -> bool:
    clean_u = (username or "").strip().lstrip("@").lower()
    clean_p = (phone or "").strip()
    p_digits = re.sub(r"\D", "", clean_p)

    dlg_existing = conn.execute(
        """
        SELECT lead_id FROM dialogs
        WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
        LIMIT 1
        """,
        (int(account_id), int(peer_id)),
    ).fetchone()
    if dlg_existing is not None and dlg_existing["lead_id"] is not None:
        return True

    lead_clauses = ["telegram_user_id = ?"]
    lead_params: list = [int(peer_id)]
    if clean_u:
        lead_clauses.append("(username IS NOT NULL AND ltrim(lower(username), '@') = ?)")
        lead_params.append(clean_u)
    if clean_p:
        lead_clauses.append("phone = ?")
        lead_params.append(clean_p)
    if len(p_digits) >= 7:
        lead_clauses.append("phone = ?")
        lead_params.append(p_digits)
        lead_clauses.append("phone = ?")
        lead_params.append(f"+{p_digits}")

    if conn.execute(
        f"SELECT 1 FROM leads WHERE {' OR '.join(lead_clauses)} LIMIT 1",
        lead_params,
    ).fetchone() is not None:
        return True

    rec_clauses = ["telegram_user_id = ?"]
    rec_params: list = [int(peer_id)]
    if clean_u:
        rec_clauses.append("(username IS NOT NULL AND ltrim(lower(username), '@') = ?)")
        rec_params.append(clean_u)
    if clean_p:
        rec_clauses.append("phone = ?")
        rec_params.append(clean_p)
    if len(p_digits) >= 7:
        rec_clauses.append("phone = ?")
        rec_params.append(p_digits)
        rec_clauses.append("phone = ?")
        rec_params.append(f"+{p_digits}")

    if conn.execute(
        f"SELECT 1 FROM campaign_recipients WHERE {' OR '.join(rec_clauses)} LIMIT 1",
        rec_params,
    ).fetchone() is not None:
        return True

    return False


def sync_inbox(
    conn,
    account_id: int,
    people: list,
    opener=None,
    crm_only: bool = False,
) -> list:
    acc_provider = _account_provider(conn, account_id, require_exists=True)
    now = utc_now()
    saved = []
    try:
        for person in people or []:
            if not isinstance(person, dict) or person.get("is_group"):
                continue
            if (person.get("peer_type") or "user") != "user":
                continue
            peer_id = person.get("telegram_user_id")
            if peer_id is None and person.get("phone"):
                digits = re.sub(r"\D", "", str(person["phone"]))
                if len(digits) >= 7:
                    peer_id = int(digits[-12:])
            if peer_id is None:
                continue
            title = (person.get("name") or "").strip()
            username = person.get("username")
            if username is not None:
                username = str(username).strip().lstrip("@") or None
            provider = str(person.get("provider") or acc_provider or "telegram").strip().lower()
            phone = _extract_dialog_phone(person, int(peer_id), provider)
            if crm_only and not _person_is_crm_relevant(
                conn, account_id, int(peer_id), username, phone
            ):
                continue
            unread = person.get("unread_count") if "unread_count" in person else None
            if unread is not None:
                unread = max(0, int(unread))
            preview = person.get("last_message_preview")
            if preview is not None:
                preview = str(preview)[:120]
            sent_at = person.get("last_message_at")
            if sent_at is not None:
                sent_at = str(sent_at)
            last_from = person.get("last_from_peer") if "last_from_peer" in person else None
            if last_from is not None:
                last_from = 1 if last_from else 0
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, phone, provider,
                    is_group, unread_count, last_message_at, last_message_preview,
                    last_from_peer, muted, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
                    username = COALESCE(excluded.username, dialogs.username),
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE dialogs.title END,
                    phone = COALESCE(excluded.phone, dialogs.phone),
                    provider = COALESCE(excluded.provider, dialogs.provider),
                    unread_count = COALESCE(?, dialogs.unread_count),
                    last_message_at = COALESCE(excluded.last_message_at, dialogs.last_message_at),
                    last_message_preview = COALESCE(excluded.last_message_preview, dialogs.last_message_preview),
                    last_from_peer = COALESCE(?, dialogs.last_from_peer),
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    int(peer_id),
                    username,
                    title,
                    phone,
                    provider,
                    0 if unread is None else unread,
                    sent_at,
                    preview,
                    0 if last_from is None else last_from,
                    now,
                    now,
                    unread,
                    last_from,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM dialogs
                WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?
                """,
                (account_id, int(peer_id)),
            ).fetchone()
            if row and int(row["last_from_peer"] or 0) == 1:
                stop_outreach_on_reply(
                    conn,
                    account_id=account_id,
                    peer_id=int(row["peer_id"]),
                    username=row["username"],
                    phone=row["phone"],
                    lead_id=row["lead_id"],
                    preview=row["last_message_preview"],
                    opener=opener,
                )
                row = conn.execute(
                    "SELECT * FROM dialogs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
            saved.append(_public(row))
        conn.commit()
    except (sqlite3.IntegrityError, TypeError, ValueError) as exc:
        conn.rollback()
        raise ValueError("Не удалось записать входящие") from exc
    return saved


class InboxResultList(list):
    def __init__(self, items=(), counts: dict | None = None):
        super().__init__(items)
        self.counts = counts or {
            "waiting": 0,
            "our_turn": 0,
            "unread": 0,
            "groups": 0,
            "muted": 0,
        }


_INBOX_SELECT_SQL = """
    SELECT
        d.*,
        COALESCE(d.provider, a.provider, 'telegram') AS provider,
        d.phone AS phone,
        a.phone AS account_phone,
        COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
        a.telegram_username AS account_username,
        COALESCE(
            d.lead_id,
            (SELECT l.id FROM leads l WHERE l.telegram_user_id = d.peer_id AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.id FROM leads l WHERE l.telegram_user_id = d.peer_id ORDER BY l.id LIMIT 1),
            (SELECT l.id FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.id FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') ORDER BY l.id LIMIT 1)
        ) AS resolved_lead_id,
        COALESCE(
            (SELECT l.status FROM leads l WHERE l.id = d.lead_id LIMIT 1),
            (SELECT l.status FROM leads l WHERE l.telegram_user_id = d.peer_id AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE l.telegram_user_id = d.peer_id ORDER BY l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') AND (l.account_id = d.account_id OR l.account_id IS NULL) ORDER BY l.account_id DESC, l.id LIMIT 1),
            (SELECT l.status FROM leads l WHERE d.username IS NOT NULL AND l.username IS NOT NULL AND ltrim(lower(l.username), '@') = ltrim(lower(d.username), '@') ORDER BY l.id LIMIT 1)
        ) AS lead_status
    FROM dialogs d
    JOIN accounts a ON a.id = d.account_id
"""


def _resolve_missing_lead_status_by_phone(conn, dialogs: list[dict]) -> None:
    need_phone = [
        d for d in dialogs
        if not d.get("lead_status") and d.get("phone")
    ]
    if not need_phone:
        return
    lead_rows = conn.execute(
        "SELECT id, account_id, phone, status FROM leads WHERE phone IS NOT NULL AND phone != '' ORDER BY id ASC"
    ).fetchall()
    if not lead_rows:
        return
    by_digits: dict[str, list[dict]] = {}
    for lr in lead_rows:
        ld = as_dict(lr)
        digits = re.sub(r"\D", "", str(ld.get("phone") or ""))
        if len(digits) >= 7:
            by_digits.setdefault(digits, []).append(ld)
            if len(digits) > 10:
                by_digits.setdefault(digits[-10:], []).append(ld)
    for d in need_phone:
        digits = re.sub(r"\D", "", str(d.get("phone") or ""))
        if len(digits) < 7:
            continue
        candidates = by_digits.get(digits) or (by_digits.get(digits[-10:]) if len(digits) > 10 else None)
        if not candidates:
            continue
        acc_id = d.get("account_id")
        best = sorted(
            candidates,
            key=lambda c: (
                0 if c.get("account_id") == acc_id else (1 if c.get("account_id") is None else 2),
                int(c.get("id") or 0),
            ),
        )[0]
        if best.get("status"):
            d["lead_status"] = best["status"]
        if not d.get("lead_id") and best.get("id"):
            d["lead_id"] = best["id"]


def _filter_crm_only_rows(conn, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    rec_rows = conn.execute(
        """
        SELECT lead_id, telegram_user_id, username, phone, status
        FROM campaign_recipients
        """
    ).fetchall()
    recs_by_lid: dict[int, list[str]] = {}
    recs_by_tgid: dict[int, list[str]] = {}
    recs_by_uname: dict[str, list[str]] = {}
    recs_by_phone: dict[str, list[str]] = {}
    for rr in rec_rows:
        st = str(rr["status"] or "").strip().lower()
        if rr["lead_id"] is not None:
            recs_by_lid.setdefault(int(rr["lead_id"]), []).append(st)
        if rr["telegram_user_id"] is not None:
            recs_by_tgid.setdefault(int(rr["telegram_user_id"]), []).append(st)
        if rr["username"]:
            u = str(rr["username"]).strip().lstrip("@").lower()
            if u:
                recs_by_uname.setdefault(u, []).append(st)
        if rr["phone"]:
            p_dig = re.sub(r"\D", "", str(rr["phone"]))
            if len(p_dig) >= 7:
                recs_by_phone.setdefault(p_dig, []).append(st)

    in_msg_dialog_ids: set[int] = set()
    dialog_ids = [int(r["id"]) for r in rows if r.get("id") is not None]
    if dialog_ids:
        ph_dids = ",".join("?" for _ in dialog_ids)
        for mrow in conn.execute(
            f"SELECT DISTINCT dialog_id FROM messages WHERE dialog_id IN ({ph_dids}) AND direction = 'in'",
            dialog_ids,
        ).fetchall():
            in_msg_dialog_ids.add(int(mrow["dialog_id"]))

    active_deal_statuses = {
        "talking",
        "call_scheduled",
        "won",
        "in conversation",
        "call scheduled",
        "signed",
    }
    filtered = []
    for row in rows:
        if int(row.get("is_group") or 0) != 0:
            continue
        if (row.get("peer_type") or "user") != "user":
            continue

        matched_rec_statuses: list[str] = []
        lid = row.get("lead_id")
        if lid is not None and int(lid) in recs_by_lid:
            matched_rec_statuses.extend(recs_by_lid[int(lid)])
        tg_id = row.get("telegram_user_id")
        if tg_id is not None and int(tg_id) in recs_by_tgid:
            matched_rec_statuses.extend(recs_by_tgid[int(tg_id)])
        uname = str(row.get("username") or "").strip().lstrip("@").lower()
        if uname and uname in recs_by_uname:
            matched_rec_statuses.extend(recs_by_uname[uname])
        p_dig = re.sub(r"\D", "", str(row.get("phone") or ""))
        if len(p_dig) >= 7 and p_dig in recs_by_phone:
            matched_rec_statuses.extend(recs_by_phone[p_dig])

        has_any_rec = len(matched_rec_statuses) > 0
        has_replied_rec = "replied" in matched_rec_statuses
        has_lead = bool(row.get("lead_id") is not None or row.get("lead_status"))

        if not has_lead and not has_replied_rec:
            continue

        if has_any_rec and not has_replied_rec:
            last_from_peer = int(row.get("last_from_peer") or 0)
            has_in_msg = int(row["id"]) in in_msg_dialog_ids
            raw_lstatus = str(row.get("lead_status") or "").strip().lower()
            try:
                norm_lstatus = normalize_status(raw_lstatus, conn).strip().lower() if raw_lstatus else ""
            except ValueError:
                norm_lstatus = raw_lstatus
            is_active_deal = (
                raw_lstatus in active_deal_statuses
                or norm_lstatus in ("talking", "call_scheduled", "won")
            )
            if last_from_peer == 0 and not has_in_msg and not is_active_deal:
                continue

        filtered.append(row)
    return filtered


def _query_inbox_rows(
    conn,
    account_id: int | None = None,
    q: str = "",
    provider: str | None = None,
    status: str | None = None,
    crm_only: bool = False,
) -> list[dict]:
    where_clauses = [
        "(d.peer_type = 'user' OR d.is_group = 1)",
        "COALESCE(d.peer_id, 0) != 178220800",
        "lower(COALESCE(d.username, '')) != 'spambot'",
    ]
    if crm_only:
        where_clauses.append("d.peer_type = 'user'")
        where_clauses.append("COALESCE(d.is_group, 0) = 0")
    params: list = []

    if account_id is not None and int(account_id) != 0:
        acc_id = int(account_id)
        account = conn.execute("SELECT id FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        if account is None:
            raise LookupError("account")
        where_clauses.append("d.account_id = ?")
        params.append(acc_id)

    clean_provider = (provider or "").strip().lower()
    if clean_provider in ("telegram", "whatsapp"):
        where_clauses.append("lower(COALESCE(d.provider, a.provider, 'telegram')) = ?")
        params.append(clean_provider)

    sql = (
        f"{_INBOX_SELECT_SQL} "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY (d.last_message_at IS NULL), d.last_message_at DESC, d.updated_at DESC, d.id DESC"
    )
    rows = conn.execute(sql, params).fetchall()
    found = [_public(row) for row in rows]
    _resolve_missing_lead_status_by_phone(conn, found)

    if crm_only:
        found = _filter_crm_only_rows(conn, found)

    raw_status = (status or "").strip()
    if raw_status:
        try:
            clean_status = normalize_status(raw_status, conn).strip().lower()
        except ValueError:
            clean_status = raw_status.lower()
        found = [
            row
            for row in found
            if row.get("lead_status")
            and (
                str(row["lead_status"]).strip().lower() == clean_status
                or normalize_status(str(row["lead_status"]), conn).strip().lower() == clean_status
            )
        ]

    needle = (q or "").strip()
    if needle.startswith("@"):
        needle = needle[1:]
    if needle:
        folded = needle.casefold()
        needle_digits = re.sub(r"\D", "", needle)
        matched = []
        for row in found:
            phone_str = row.get("phone") or ""
            phone_digits = re.sub(r"\D", "", phone_str)
            blob = " ".join((
                row.get("name") or "",
                row.get("username") or "",
                phone_str,
                phone_digits,
                str(row.get("telegram_user_id") or ""),
                row.get("account_name") or "",
                row.get("account_phone") or "",
                row.get("provider") or "",
            )).casefold()
            if folded in blob or (needle_digits and len(needle_digits) >= 3 and needle_digits in phone_digits):
                matched.append(row)
        found = matched

    return found


def compute_inbox_counts(dialogs: list[dict]) -> dict:
    return {
        "waiting": sum(1 for d in dialogs if int(d.get("last_from_peer") or 0) == 1),
        "our_turn": sum(1 for d in dialogs if int(d.get("last_from_peer") or 0) == 0),
        "unread": sum(1 for d in dialogs if int(d.get("unread_count") or 0) > 0),
        "groups": sum(1 for d in dialogs if int(d.get("is_group") or 0) == 1),
        "muted": sum(1 for d in dialogs if int(d.get("muted") or 0) == 1),
    }


def get_inbox_counts(
    conn,
    account_id: int | None = None,
    q: str = "",
    provider: str | None = None,
    status: str | None = None,
    crm_only: bool = False,
) -> dict:
    base_rows = _query_inbox_rows(
        conn,
        account_id=account_id,
        q=q,
        provider=provider,
        status=status,
        crm_only=crm_only,
    )
    return compute_inbox_counts(base_rows)


def _matches_chips(row: dict, chips: set[str]) -> bool:
    if "waiting" in chips and int(row.get("last_from_peer") or 0) != 1:
        return False
    if "our_turn" in chips and int(row.get("last_from_peer") or 0) != 0:
        return False
    if "unread" in chips and int(row.get("unread_count") or 0) <= 0:
        return False
    if "groups" in chips and int(row.get("is_group") or 0) != 1:
        return False
    if "muted" in chips and int(row.get("muted") or 0) != 1:
        return False
    return True


def list_inbox(
    conn,
    account_id: int | None = None,
    q: str = "",
    chip: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    with_counts: bool = False,
    crm_only: bool = False,
) -> list | dict:
    base_rows = _query_inbox_rows(
        conn,
        account_id=account_id,
        q=q,
        provider=provider,
        status=status,
        crm_only=crm_only,
    )
    counts = compute_inbox_counts(base_rows)

    chips = {c.strip().lower() for c in (chip or "").split(",") if c.strip()}
    if chips:
        filtered = [row for row in base_rows if _matches_chips(row, chips)]
    else:
        filtered = base_rows

    has_explicit_filter = bool(
        chips
        or (q or "").strip()
        or (provider or "").strip()
        or (status or "").strip()
        or crm_only
    )
    if has_explicit_filter:
        cap = 500
    else:
        cap = 60 if (account_id is None or int(account_id) == 0) else 30
    capped = filtered[:cap]

    if with_counts:
        return {"dialogs": capped, "counts": counts}
    return InboxResultList(capped, counts=counts)


def _load_dialog_public(conn, dialog_id: int) -> dict:
    row = conn.execute(
        f"{_INBOX_SELECT_SQL} WHERE d.id = ?",
        (int(dialog_id),),
    ).fetchone()
    if row is None:
        raise LookupError("dialog")
    return _public(row)


def mark_dialog_unread(conn, dialog_id: int) -> dict:
    did = int(dialog_id)
    existing = conn.execute("SELECT id, account_id FROM dialogs WHERE id = ?", (did,)).fetchone()
    if existing is None:
        raise LookupError("dialog")
    now = utc_now()
    conn.execute(
        """
        UPDATE dialogs
        SET unread_count = MAX(1, COALESCE(unread_count, 0)),
            last_from_peer = 1,
            updated_at = ?
        WHERE id = ?
        """,
        (now, did),
    )
    conn.commit()
    out = _load_dialog_public(conn, did)
    out["counts"] = get_inbox_counts(conn, account_id=out["account_id"])
    out["all_counts"] = get_inbox_counts(conn, account_id=0)
    return out


def mark_dialog_read(conn, dialog_id: int) -> dict:
    did = int(dialog_id)
    existing = conn.execute("SELECT id, account_id FROM dialogs WHERE id = ?", (did,)).fetchone()
    if existing is None:
        raise LookupError("dialog")
    now = utc_now()
    conn.execute(
        """
        UPDATE dialogs
        SET unread_count = 0,
            updated_at = ?
        WHERE id = ?
        """,
        (now, did),
    )
    conn.commit()
    out = _load_dialog_public(conn, did)
    out["counts"] = get_inbox_counts(conn, account_id=out["account_id"])
    out["all_counts"] = get_inbox_counts(conn, account_id=0)
    return out


def open_dialog_by_lead(
    conn,
    lead_id: int | None = None,
    account_id: int | None = None,
    telegram_user_id: int | None = None,
    username: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    now = utc_now()
    explicit_acc_id = int(account_id) if (account_id is not None and int(account_id) > 0) else None
    explicit_acc = None
    if explicit_acc_id is not None:
        acc_check = conn.execute("SELECT * FROM accounts WHERE id = ?", (explicit_acc_id,)).fetchone()
        if acc_check is None:
            raise LookupError("account")
        explicit_acc = as_dict(acc_check)

    clean_user = str(username).strip().lstrip("@") if username and str(username).strip() else None
    clean_phone = str(phone).strip() if phone and str(phone).strip() else None
    clean_tg_id = (
        int(telegram_user_id)
        if telegram_user_id is not None and str(telegram_user_id).strip() != ""
        else None
    )
    clean_name = str(name).strip() if name and str(name).strip() else None

    lead_row = None
    if lead_id is not None and int(lead_id) > 0:
        lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (int(lead_id),)).fetchone()
    if lead_row is None and clean_tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (clean_tg_id,),
        ).fetchone()
    if lead_row is None and clean_user:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE username IS NOT NULL AND ltrim(lower(username), '@') = lower(?) ORDER BY id LIMIT 1",
            (clean_user,),
        ).fetchone()
    if lead_row is None and clean_phone:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
            (clean_phone,),
        ).fetchone()
        if lead_row is None:
            p_digits = re.sub(r"\D", "", clean_phone)
            if len(p_digits) >= 7:
                p_peer_id = int(p_digits[-12:])
                lead_row = conn.execute(
                    "SELECT * FROM leads WHERE telegram_user_id = ? OR phone = ? OR phone = ? ORDER BY id LIMIT 1",
                    (p_peer_id, p_digits, f"+{p_digits}"),
                ).fetchone()

    if lead_row is not None:
        lead = as_dict(lead_row)
        lid = int(lead["id"])
        if clean_tg_id is not None and not lead.get("telegram_user_id"):
            lead["telegram_user_id"] = clean_tg_id
        if clean_user and not lead.get("username"):
            lead["username"] = clean_user
        if clean_phone and not lead.get("phone"):
            lead["phone"] = clean_phone
        if clean_name and not lead.get("name"):
            lead["name"] = clean_name
    elif any((clean_tg_id is not None, clean_user, clean_phone, clean_name)):
        lead = {
            "id": None,
            "account_id": explicit_acc_id,
            "telegram_user_id": clean_tg_id,
            "username": clean_user,
            "phone": clean_phone,
            "name": clean_name,
            "provider": "telegram",
            "dialog_id": None,
            "note": None,
            "description": None,
        }
        lid = None
    else:
        raise LookupError("lead")

    lead_provider = (lead.get("provider") or "telegram").strip().lower()
    lead_phone = (lead.get("phone") or "").strip() or None
    phone_digits = re.sub(r"\D", "", lead_phone or "")
    wa_peer_id = int(phone_digits[-12:]) if (phone_digits and len(phone_digits) >= 7) else None

    if explicit_acc is not None:
        wanted_provider = (explicit_acc.get("provider") or "telegram").strip().lower()
    else:
        wanted_provider = lead_provider

    # 1. Try existing dialog on requested account (or any account if not specified)
    existing_dlg = None
    match_lid = lid if lid is not None else -1
    if explicit_acc_id is not None:
        existing_dlg = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.account_id = ?
              AND d.peer_type = 'user'
              AND (
                  d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.phone IS NOT NULL AND d.phone = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY d.id LIMIT 1
            """,
            (
                explicit_acc_id,
                match_lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                wa_peer_id,
                wa_peer_id,
                lead_phone,
                lead_phone,
                lead.get("username"),
                lead.get("username"),
            ),
        ).fetchone()
    else:
        existing_dlg = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.peer_type = 'user'
              AND COALESCE(d.provider, a.provider, 'telegram') = ?
              AND (
                  d.id = ?
                  OR d.lead_id = ?
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.peer_id = ?)
                  OR (? IS NOT NULL AND d.phone IS NOT NULL AND d.phone = ?)
                  OR (? IS NOT NULL AND d.username IS NOT NULL AND lower(d.username) = lower(?))
              )
            ORDER BY (d.lead_id = ?) DESC, d.id LIMIT 1
            """,
            (
                wanted_provider,
                lead.get("dialog_id") or -1,
                match_lid,
                lead.get("telegram_user_id"),
                lead.get("telegram_user_id"),
                wa_peer_id,
                wa_peer_id,
                lead_phone,
                lead_phone,
                lead.get("username"),
                lead.get("username"),
                match_lid,
            ),
        ).fetchone()

    if existing_dlg is not None:
        dlg_id = int(existing_dlg["id"])
        conn.execute(
            """
            UPDATE dialogs
            SET lead_id = COALESCE(?, lead_id),
                phone = COALESCE(phone, ?),
                provider = COALESCE(provider, ?)
            WHERE id = ?
            """,
            (lid, lead_phone, wanted_provider, dlg_id),
        )
        if lid is not None:
            conn.execute("UPDATE leads SET dialog_id = ? WHERE id = ?", (dlg_id, lid))
        conn.commit()
        refreshed = conn.execute(
            """
            SELECT d.*,
                   COALESCE(d.provider, a.provider, 'telegram') AS provider,
                   d.phone AS phone,
                   a.phone AS account_phone,
                   COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
                   a.telegram_username AS account_username
            FROM dialogs d
            JOIN accounts a ON a.id = d.account_id
            WHERE d.id = ?
            """,
            (dlg_id,),
        ).fetchone()
        return _public(refreshed)

    # 2. Determine target account for creating a new dialog
    target_acc_id = explicit_acc_id
    if target_acc_id is None and lead.get("account_id"):
        acc_row = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND status = 'connected'",
            (int(lead["account_id"]),),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE status = 'connected' AND COALESCE(provider, 'telegram') = ? ORDER BY id LIMIT 1",
            (wanted_provider,),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE COALESCE(provider, 'telegram') = ? ORDER BY id LIMIT 1",
            (wanted_provider,),
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute(
            "SELECT id FROM accounts WHERE status = 'connected' ORDER BY id LIMIT 1"
        ).fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        acc_row = conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
        if acc_row:
            target_acc_id = int(acc_row["id"])
    if target_acc_id is None:
        raise ValueError("Подключите хотя бы один Telegram-аккаунт для открытия чата")

    acc_provider = _account_provider(conn, target_acc_id)
    is_whatsapp = (wanted_provider == "whatsapp" or acc_provider == "whatsapp")
    dialog_provider = "whatsapp" if is_whatsapp else "telegram"

    fallback_synth = 900000 + (lid if lid is not None else (abs(hash(clean_user or clean_phone or clean_name or "0")) % 99999 + 1))
    if dialog_provider == "whatsapp":
        if wa_peer_id is not None:
            peer_id = wa_peer_id
        elif lead.get("telegram_user_id"):
            peer_id = int(lead["telegram_user_id"])
        else:
            peer_id = fallback_synth
    else:
        peer_id = int(lead.get("telegram_user_id") or wa_peer_id or fallback_synth)

    username = (lead.get("username") or "").strip().lstrip("@") or None
    title = (lead.get("name") or "").strip() or username or lead_phone or (f"Лид #{lid}" if lid is not None else "Контакт")
    preview = (lead.get("note") or lead.get("description") or "Контакт из CRM · готов к диалогу")[:120]

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, 0, ?, ?, 0, 0, ?, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN dialogs.title IS NULL OR dialogs.title = '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            lead_id = COALESCE(excluded.lead_id, dialogs.lead_id),
            updated_at = excluded.updated_at
        """,
        (target_acc_id, peer_id, username, title, lead_phone, dialog_provider, now, preview, lid, now, now),
    )
    created_row = conn.execute(
        """
        SELECT d.*,
               COALESCE(d.provider, a.provider, 'telegram') AS provider,
               d.phone AS phone,
               a.phone AS account_phone,
               COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
               a.telegram_username AS account_username
        FROM dialogs d
        JOIN accounts a ON a.id = d.account_id
        WHERE d.account_id = ? AND d.peer_type = 'user' AND d.peer_id = ?
        """,
        (target_acc_id, peer_id),
    ).fetchone()
    dlg_id = int(created_row["id"])
    if lid is not None:
        try:
            conn.execute(
                """
                UPDATE leads
                SET dialog_id = ?,
                    account_id = COALESCE(account_id, ?),
                    telegram_user_id = COALESCE(telegram_user_id, ?),
                    provider = COALESCE(provider, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (dlg_id, target_acc_id, peer_id, dialog_provider, now, lid),
            )
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE leads SET dialog_id = ?, updated_at = ? WHERE id = ?",
                (dlg_id, now, lid),
            )
    conn.commit()
    return _public(created_row)


def list_messages(conn, dialog_id: int, limit: int | None = None) -> list:
    dialog = conn.execute("SELECT id FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    if limit is None:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE dialog_id = ?
            ORDER BY sent_at, id
            """,
            (dialog_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE dialog_id = ?
                ORDER BY sent_at DESC, id DESC
                LIMIT ?
            ) ORDER BY sent_at, id
            """,
            (dialog_id, int(limit)),
        ).fetchall()
    out = []
    for row in rows:
        d = as_dict(row)
        if not d.get("formatted_html") and d.get("text"):
            d["formatted_html"] = render_formatted_html(d["text"], d.get("entities_json"))
        elif d.get("formatted_html") is None:
            d["formatted_html"] = ""
        out.append(d)
    return out


def _cache_messages(conn, dialog_id: int, items: list) -> None:
    now = utc_now()
    normalized = [_normalize_message(item, now) for item in items]
    try:
        for (
            message_id,
            direction,
            text,
            sent_at,
            reply_to,
            media_url,
            media_type,
            media_mime,
            media_filename,
            media_duration,
            entities_json,
            formatted_html,
            edited_at,
        ) in normalized:
            conn.execute(
                """
                INSERT INTO messages (
                    dialog_id, telegram_message_id, direction, text, media_url,
                    media_type, media_mime, media_filename, media_duration,
                    entities_json, formatted_html, edited_at,
                    sent_at, reply_to_telegram_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dialog_id, telegram_message_id) DO UPDATE SET
                    direction = excluded.direction,
                    text = excluded.text,
                    formatted_html = excluded.formatted_html,
                    entities_json = excluded.entities_json,
                    edited_at = COALESCE(excluded.edited_at, messages.edited_at),
                    media_type = COALESCE(excluded.media_type, messages.media_type),
                    media_url = COALESCE(excluded.media_url, messages.media_url),
                    media_mime = COALESCE(excluded.media_mime, messages.media_mime),
                    media_duration = COALESCE(excluded.media_duration, messages.media_duration),
                    media_filename = COALESCE(excluded.media_filename, messages.media_filename),
                    sent_at = excluded.sent_at,
                    reply_to_telegram_message_id = excluded.reply_to_telegram_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    dialog_id,
                    message_id,
                    direction,
                    text,
                    media_url,
                    media_type,
                    media_mime,
                    media_filename,
                    media_duration,
                    entities_json,
                    formatted_html,
                    edited_at,
                    sent_at,
                    reply_to,
                    now,
                    now,
                ),
            )
        if normalized:
            latest = max(normalized, key=lambda m: (m[3], m[0]))
            (
                _,
                last_dir,
                last_text,
                last_at,
                _,
                _,
                last_media_type,
                _,
                last_media_filename,
                _,
                _,
                _,
                _,
            ) = latest
            last_preview = _media_preview_label(
                last_media_type, last_text, last_media_filename
            )
            if last_dir == "in":
                conn.execute(
                    """
                    UPDATE dialogs
                    SET last_from_peer = 1,
                        last_message_at = COALESCE(?, last_message_at),
                        last_message_preview = CASE WHEN ? != '' THEN ? ELSE last_message_preview END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (last_at, last_preview, last_preview, now, dialog_id),
                )
                dlg_row = conn.execute(
                    "SELECT * FROM dialogs WHERE id = ?", (dialog_id,)
                ).fetchone()
                if dlg_row is not None:
                    stop_outreach_on_reply(
                        conn,
                        account_id=int(dlg_row["account_id"]),
                        peer_id=int(dlg_row["peer_id"]),
                        username=dlg_row["username"],
                        phone=dlg_row["phone"],
                        lead_id=dlg_row["lead_id"],
                        preview=last_preview,
                        min_sent_at=last_at,
                    )
            else:
                conn.execute(
                    """
                    UPDATE dialogs
                    SET last_from_peer = 0,
                        last_message_at = CASE WHEN last_message_at IS NULL OR last_message_at <= ? THEN ? ELSE last_message_at END,
                        last_message_preview = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (last_at, last_at, last_preview, now, dialog_id),
                )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Не удалось записать историю") from exc


def _normalize_message(item, now: str):
    if not isinstance(item, dict) or item.get("telegram_message_id") is None:
        raise GatewayError("История без номера сообщения")
    direction = item.get("direction") or "in"
    if direction not in ("in", "out"):
        raise GatewayError("Непонятное направление сообщения")
    text = item.get("text")
    if text is not None:
        text = str(text)
    sent_at = item.get("sent_at") or now
    reply = item.get("reply_to_telegram_message_id")
    media_url = item.get("media_url")
    media_type = item.get("media_type")
    media_mime = item.get("media_mime")
    media_filename = item.get("media_filename")
    media_duration = (
        int(item["media_duration"])
        if item.get("media_duration") is not None
        else None
    )
    raw_entities = (
        item.get("entities")
        if "entities" in item
        else item.get("entities_json")
    )
    if isinstance(raw_entities, list):
        entities_json = json.dumps(raw_entities, ensure_ascii=False) if raw_entities else None
    elif isinstance(raw_entities, str):
        entities_json = raw_entities.strip() or None
    else:
        entities_json = None
    formatted_html = item.get("formatted_html")
    if not formatted_html:
        formatted_html = render_formatted_html(
            text,
            raw_entities if isinstance(raw_entities, (list, str)) else entities_json,
        )
    edited_at = item.get("edited_at")
    return (
        int(item["telegram_message_id"]),
        direction,
        text,
        str(sent_at),
        int(reply) if reply else None,
        media_url,
        media_type,
        media_mime,
        media_filename,
        media_duration,
        entities_json,
        formatted_html,
        edited_at,
    )


async def get_thread(conn, dialog_id: int, gateway, limit: int = 50) -> list:
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    if dialog["peer_type"] != "user" or int(dialog["is_group"] or 0):
        raise ValueError("Это не личный диалог")
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
    ).fetchone()
    if account is None:
        raise LookupError("account")
    account = as_dict(account)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    cap = 50 if limit is None else max(1, min(int(limit), 50))
    peer_id = int(dialog["peer_id"])
    if _is_synthetic_peer_id(peer_id) and dialog.get("username") and hasattr(gateway, "search_dialogs"):
        try:
            found = await gateway.search_dialogs(
                account["session_path"], f"@{dialog['username']}", limit=1
            )
            if isinstance(found, list) and found and found[0].get("telegram_user_id"):
                real_id = int(found[0]["telegram_user_id"])
                if real_id != peer_id:
                    try:
                        conn.execute(
                            "UPDATE dialogs SET peer_id = ?, updated_at = ? WHERE id = ?",
                            (real_id, utc_now(), dialog_id),
                        )
                        if dialog.get("lead_id"):
                            conn.execute(
                                "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                (real_id, utc_now(), int(dialog["lead_id"])),
                            )
                        conn.commit()
                        peer_id = real_id
                    except sqlite3.IntegrityError:
                        conn.rollback()
        except Exception:
            pass
    if _is_synthetic_peer_id(peer_id) or gateway is None:
        return list_messages(conn, dialog_id, cap)
    try:
        try:
            fetched = await gateway.fetch_messages(account["session_path"], peer_id, cap)
        except Exception as exc:
            uname = str(dialog.get("username") or "").strip().lstrip("@")
            if uname and "could not find the input entity" in str(exc).casefold():
                fetched = await gateway.fetch_messages(
                    account["session_path"], f"@{uname}", cap
                )
            else:
                raise
    except Exception:
        cached = list_messages(conn, dialog_id, cap)
        if cached:
            return cached
        raise
    if not isinstance(fetched, list):
        cached = list_messages(conn, dialog_id, cap)
        if cached:
            return cached
        raise GatewayError("История без списка")
    _cache_messages(conn, dialog_id, fetched[-cap:])
    return list_messages(conn, dialog_id, cap)


def _dialog_has_peer_reply(conn, dialog: dict) -> bool:
    """Return True if the peer has replied or initiated the conversation."""
    if int(dialog.get("last_from_peer") or 0) == 1:
        return True
    did = int(dialog["id"])
    has_in_msg = conn.execute(
        "SELECT 1 FROM messages WHERE dialog_id = ? AND direction = 'in' LIMIT 1",
        (did,),
    ).fetchone()
    if has_in_msg is not None:
        return True
    lead_id = dialog.get("lead_id")
    if lead_id is not None:
        has_replied_lead = conn.execute(
            "SELECT 1 FROM campaign_recipients WHERE lead_id = ? AND status = 'replied' LIMIT 1",
            (int(lead_id),),
        ).fetchone()
        if has_replied_lead is not None:
            return True
    peer_id = dialog.get("peer_id")
    if peer_id is not None and int(peer_id) > 0:
        has_replied_peer = conn.execute(
            "SELECT 1 FROM campaign_recipients WHERE telegram_user_id = ? AND status = 'replied' LIMIT 1",
            (int(peer_id),),
        ).fetchone()
        if has_replied_peer is not None:
            return True
    return False


async def send_reply(conn, dialog_id: int, text: str, gateway, reply_to=None) -> dict:
    body = (text or "").strip()
    if not body:
        raise ValueError("Напишите сообщение")
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (dialog_id,)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
    ).fetchone()
    if account is None:
        raise LookupError("account")
    account = as_dict(account)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")
    is_reply_to_peer = _dialog_has_peer_reply(conn, dialog)
    if not is_reply_to_peer:
        check_daily_limit(conn, account["id"])
    if gateway is None:
        next_row = conn.execute(
            "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone()
        sent = {
            "telegram_message_id": int(next_row["next_id"]),
            "text": body,
            "sent_at": utc_now(),
        }
    else:
        peer_target = int(dialog["peer_id"])
        if _is_synthetic_peer_id(peer_target) and dialog.get("username"):
            if hasattr(gateway, "search_dialogs"):
                try:
                    found = await gateway.search_dialogs(
                        account["session_path"], f"@{dialog['username']}", limit=1
                    )
                    if isinstance(found, list) and found and found[0].get("telegram_user_id"):
                        real_id = int(found[0]["telegram_user_id"])
                        peer_target = real_id
                        try:
                            conn.execute(
                                "UPDATE dialogs SET peer_id = ?, updated_at = ? WHERE id = ?",
                                (real_id, utc_now(), dialog_id),
                            )
                            if dialog.get("lead_id"):
                                conn.execute(
                                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                    (real_id, utc_now(), int(dialog["lead_id"])),
                                )
                            conn.commit()
                        except sqlite3.IntegrityError:
                            conn.rollback()
                except Exception:
                    pass
            if isinstance(peer_target, int) and _is_synthetic_peer_id(peer_target):
                peer_target = f"@{dialog['username']}"
        try:
            sent = await gateway.send_message(
                account["session_path"], peer_target, body, reply_to
            )
        except Exception as exc:
            uname = str(dialog.get("username") or "").strip().lstrip("@")
            if (
                uname
                and peer_target != f"@{uname}"
                and "could not find the input entity" in str(exc).casefold()
            ):
                sent = await gateway.send_message(
                    account["session_path"], f"@{uname}", body, reply_to
                )
            else:
                raise
    if not isinstance(sent, dict) or sent.get("telegram_message_id") is None:
        raise GatewayError("Ответ без номера сообщения")
    now = utc_now()
    sent_at = sent.get("sent_at") or now
    message_text = (sent.get("text") or body).strip() or body
    formatted_html = sent.get("formatted_html") or render_formatted_html(message_text, None)
    try:
        cur = conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, formatted_html,
                sent_at, reply_to_telegram_message_id, created_at, updated_at
            ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?)
            """,
            (
                dialog_id,
                int(sent["telegram_message_id"]),
                message_text,
                formatted_html,
                sent_at,
                int(reply_to) if reply_to else None,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Такое сообщение уже есть") from exc
    preview = message_text[:120]
    conn.execute(
        """
        UPDATE dialogs
        SET last_message_at = ?, last_message_preview = ?, last_from_peer = 0, updated_at = ?
        WHERE id = ?
        """,
        (sent_at, preview, now, dialog_id),
    )
    if not is_reply_to_peer:
        increment_daily_sent(conn, account["id"])
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


async def edit_dialog_message(
    conn,
    dialog_id: int,
    telegram_message_id: int,
    text: str,
    gateway=None,
) -> dict:
    body = (text or "").strip()
    if not body:
        raise ValueError("Напишите сообщение")
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (int(dialog_id),)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    msg_row = conn.execute(
        """
        SELECT * FROM messages
        WHERE dialog_id = ? AND telegram_message_id = ?
        LIMIT 1
        """,
        (int(dialog_id), int(telegram_message_id)),
    ).fetchone()
    if msg_row is None:
        raise LookupError("message")
    msg = as_dict(msg_row)
    if msg.get("direction") != "out":
        raise ValueError("Можно редактировать только исходящие сообщения")

    now = utc_now()
    edited_at = now
    new_text = body

    if gateway is not None:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
        ).fetchone()
        if account is None:
            raise LookupError("account")
        account = as_dict(account)
        if account["status"] != "connected":
            raise ValueError("Аккаунт не подключён")
        peer_target = int(dialog["peer_id"])
        if _is_synthetic_peer_id(peer_target) and dialog.get("username"):
            peer_target = f"@{dialog['username']}"
        try:
            res = await gateway.edit_message(
                account["session_path"],
                peer_target,
                int(telegram_message_id),
                body,
            )
        except Exception as exc:
            uname = str(dialog.get("username") or "").strip().lstrip("@")
            if (
                uname
                and peer_target != f"@{uname}"
                and "could not find the input entity" in str(exc).casefold()
            ):
                res = await gateway.edit_message(
                    account["session_path"],
                    f"@{uname}",
                    int(telegram_message_id),
                    body,
                )
            else:
                raise
        if isinstance(res, dict):
            if res.get("text"):
                new_text = str(res["text"]).strip() or body
            if res.get("edited_at"):
                edited_at = str(res["edited_at"])

    formatted_html = render_formatted_html(new_text, None)
    conn.execute(
        """
        UPDATE messages
        SET text = ?, formatted_html = ?, edited_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_text, formatted_html, edited_at, now, int(msg["id"])),
    )
    latest_row = conn.execute(
        """
        SELECT telegram_message_id FROM messages
        WHERE dialog_id = ?
        ORDER BY sent_at DESC, id DESC
        LIMIT 1
        """,
        (int(dialog_id),),
    ).fetchone()
    if latest_row is not None and int(latest_row["telegram_message_id"]) == int(telegram_message_id):
        preview = _media_preview_label(
            msg.get("media_type"), new_text, msg.get("media_filename")
        )
        conn.execute(
            """
            UPDATE dialogs
            SET last_message_preview = ?, updated_at = ?
            WHERE id = ?
            """,
            (preview, now, int(dialog_id)),
        )
    conn.commit()
    updated = conn.execute("SELECT * FROM messages WHERE id = ?", (int(msg["id"]),)).fetchone()
    return as_dict(updated)


async def send_dialog_media(
    conn,
    dialog_id: int,
    file_bytes: bytes,
    filename: str,
    mime_type: str = "",
    media_type: str = "document",
    caption: str = "",
    duration: int | None = None,
    reply_to=None,
    gateway=None,
) -> dict:
    if not file_bytes:
        raise ValueError("Пустой файл")
    dialog = conn.execute("SELECT * FROM dialogs WHERE id = ?", (int(dialog_id),)).fetchone()
    if dialog is None:
        raise LookupError("dialog")
    dialog = as_dict(dialog)
    account = conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (dialog["account_id"],)
    ).fetchone()
    if account is None:
        raise LookupError("account")
    account = as_dict(account)
    if account["status"] != "connected":
        raise ValueError("Аккаунт не подключён")

    is_reply_to_peer = _dialog_has_peer_reply(conn, dialog)
    if not is_reply_to_peer:
        check_daily_limit(conn, account["id"])

    clean_mime = (mime_type or "").strip()
    clean_mtype = (media_type or "").strip().lower()
    if clean_mtype not in ("voice", "video_note", "photo", "video", "audio", "document"):
        if clean_mime.startswith("image/"):
            clean_mtype = "photo"
        elif clean_mime.startswith("audio/ogg"):
            clean_mtype = "voice"
        elif clean_mime.startswith("audio/"):
            clean_mtype = "audio"
        elif clean_mime.startswith("video/"):
            clean_mtype = "video"
        else:
            clean_mtype = "document"

    safe_orig_name = Path(filename or "file").name
    ext = Path(safe_orig_name).suffix.lower()
    allowed_exts = {
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".ogg", ".mp3", ".wav", ".mp4",
        ".pdf", ".txt", ".csv", ".zip", ".bin",
    }
    if not ext or ext not in allowed_exts:
        if clean_mtype == "voice":
            ext = ".ogg"
        elif clean_mtype == "photo":
            ext = ".jpg"
        elif clean_mtype in ("video", "video_note"):
            ext = ".mp4"
        else:
            ext = ".bin"

    INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(file_bytes).hexdigest()[:12]
    stored_filename = f"dlg{int(dialog_id)}_{digest}{ext}"
    dest_path = INBOX_MEDIA_DIR / stored_filename
    dest_path.write_bytes(file_bytes)
    media_url = f"/media/inbox/{stored_filename}"

    clean_caption = (caption or "").strip()
    clean_duration = int(duration) if duration is not None else None

    if gateway is None:
        next_row = conn.execute(
            "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
            (int(dialog_id),),
        ).fetchone()
        sent = {
            "telegram_message_id": int(next_row["next_id"]),
            "text": clean_caption,
            "media_type": clean_mtype,
            "sent_at": utc_now(),
        }
    else:
        peer_target = int(dialog["peer_id"])
        if _is_synthetic_peer_id(peer_target) and dialog.get("username"):
            peer_target = f"@{dialog['username']}"
        try:
            sent = await gateway.send_media_message(
                account["session_path"],
                peer_target,
                str(dest_path),
                media_type=clean_mtype,
                caption=clean_caption,
                reply_to=reply_to,
                duration=clean_duration,
            )
        except Exception as exc:
            uname = str(dialog.get("username") or "").strip().lstrip("@")
            if (
                uname
                and peer_target != f"@{uname}"
                and "could not find the input entity" in str(exc).casefold()
            ):
                sent = await gateway.send_media_message(
                    account["session_path"],
                    f"@{uname}",
                    str(dest_path),
                    media_type=clean_mtype,
                    caption=clean_caption,
                    reply_to=reply_to,
                    duration=clean_duration,
                )
            else:
                raise

    if not isinstance(sent, dict) or sent.get("telegram_message_id") is None:
        raise GatewayError("Ответ без номера сообщения")

    now = utc_now()
    sent_at = str(sent.get("sent_at") or now)
    msg_text = str(sent.get("text") if sent.get("text") is not None else clean_caption)
    formatted_html = sent.get("formatted_html") or render_formatted_html(msg_text, None)
    cur = conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, media_url,
            media_type, media_mime, media_filename, media_duration,
            formatted_html, sent_at, reply_to_telegram_message_id,
            created_at, updated_at
        ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(dialog_id),
            int(sent["telegram_message_id"]),
            msg_text,
            media_url,
            clean_mtype,
            clean_mime or None,
            safe_orig_name,
            clean_duration,
            formatted_html,
            sent_at,
            int(reply_to) if reply_to else None,
            now,
            now,
        ),
    )
    preview = _media_preview_label(clean_mtype, msg_text, safe_orig_name)
    conn.execute(
        """
        UPDATE dialogs
        SET last_message_at = ?, last_message_preview = ?, last_from_peer = 0, updated_at = ?
        WHERE id = ?
        """,
        (sent_at, preview, now, int(dialog_id)),
    )
    if not is_reply_to_peer:
        increment_daily_sent(conn, account["id"])
    conn.commit()
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return as_dict(row)


async def start_direct_dialog(
    conn,
    handle: str,
    account_id: int | None = None,
    name: str | None = None,
    gateway=None,
) -> dict:
    raw = (handle or "").strip()
    if not raw:
        raise ValueError("Введите @username или номер телефона")
    clean_raw = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "@", raw, flags=re.IGNORECASE).strip()

    target_acc = None
    if account_id is not None and int(account_id) > 0:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        row = conn.execute(
            "SELECT * FROM accounts WHERE status = 'connected' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        row = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            target_acc = as_dict(row)
    if target_acc is None:
        raise ValueError("Подключите хотя бы один аккаунт Telegram или WhatsApp")

    acc_id = int(target_acc["id"])
    provider = str(target_acc.get("provider") or "telegram").strip().lower()
    if provider not in ("telegram", "whatsapp"):
        provider = "telegram"

    phone = None
    username = None
    resolved_tg_id = None
    resolved_name = (name or "").strip() or None

    digits = re.sub(r"\D", "", clean_raw)
    if clean_raw.startswith("+") or (digits and len(digits) >= 7 and not re.search(r"[A-Za-zА-Яа-я]", clean_raw)):
        phone = f"+{digits}"
    else:
        username = clean_raw.lstrip("@").split("/")[0].split("?")[0].strip() or None

    session_path = str(target_acc.get("session_path") or "")
    if (
        gateway is not None
        and target_acc.get("status") == "connected"
        and not session_path.startswith("data/sessions/demo")
        and hasattr(gateway, "search_dialogs")
    ):
        search_q = f"@{username}" if username else (phone or clean_raw)
        try:
            found = await gateway.search_dialogs(session_path, search_q, limit=1)
            if isinstance(found, list) and found:
                first = found[0]
                if first.get("telegram_user_id"):
                    resolved_tg_id = int(first["telegram_user_id"])
                if first.get("username"):
                    username = str(first["username"]).strip().lstrip("@")
                if first.get("name") and not resolved_name:
                    resolved_name = str(first["name"]).strip()
        except Exception:
            pass

    now = utc_now()
    lead_row = None
    if resolved_tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (resolved_tg_id,),
        ).fetchone()
    if lead_row is None and username:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE lower(username) = lower(?) ORDER BY id LIMIT 1",
            (username,),
        ).fetchone()
    if lead_row is None and phone:
        phone_peer_id = int(digits[-12:]) if (digits and len(digits) >= 7) else None
        lead_row = conn.execute(
            """
            SELECT * FROM leads
            WHERE phone = ?
               OR phone = ?
               OR (? IS NOT NULL AND telegram_user_id = ?)
            ORDER BY id LIMIT 1
            """,
            (phone, digits, phone_peer_id, phone_peer_id),
        ).fetchone()
        if lead_row is None and digits:
            for cand in conn.execute(
                "SELECT * FROM leads WHERE phone IS NOT NULL AND phone != '' ORDER BY id"
            ).fetchall():
                if re.sub(r"\D", "", str(cand["phone"] or "")) == digits:
                    lead_row = cand
                    break

    display_name = resolved_name or (f"@{username}" if username else phone) or clean_raw
    if lead_row is not None:
        lead_id = int(lead_row["id"])
        if resolved_tg_id is not None and not lead_row["telegram_user_id"]:
            try:
                conn.execute(
                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                    (resolved_tg_id, now, lead_id),
                )
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        dialog = open_dialog_by_lead(conn, lead_id, acc_id)
        updated_lead = as_dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone())
    else:
        dialog = open_dialog_by_lead(
            conn,
            None,
            acc_id,
            telegram_user_id=resolved_tg_id,
            username=username,
            phone=phone,
            name=display_name,
        )
        updated_lead = {
            "id": None,
            "account_id": acc_id,
            "telegram_user_id": dialog.get("telegram_user_id"),
            "username": username,
            "name": display_name,
            "phone": phone,
            "status": "talking",
            "provider": provider,
            "dialog_id": dialog.get("id"),
        }
    return {"dialog": dialog, "lead": updated_lead}
