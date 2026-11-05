from datetime import datetime, timedelta, timezone
import re
import sqlite3

from app.db import as_dict, utc_now
from app.events import record_lead_event
from app.reminders import normalize_due, postpone_reminder

CALENDAR_EVENT_STATUSES = ("scheduled", "completed", "cancelled")

EVENT_TYPE_COLORS = {
    "call": "#a855f7",
    "meeting": "#38bdf8",
    "reminder": "#fbbf24",
    "followup": "#10b981",
    "outreach_step": "#6366f1",
    "deadline": "#f97316",
    "task": "#f97316",
    "project": "#ec4899",
    "airtable_task": "#f97316",
    "airtable_project": "#ec4899",
}


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if len(s) >= 11 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        return None


def _normalize_calendar_datetime(
    value: str | None,
    *,
    all_day: bool = False,
    default_time: str = "10:00:00",
    required: bool = True,
) -> str | None:
    if value is None or not str(value).strip():
        if required:
            raise ValueError("Укажите дату и время события")
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Некорректная дата события") from exc
        if all_day:
            return s
        return f"{s}T{default_time}"

    dt = _parse_iso_dt(s)
    if dt is None:
        raise ValueError("Некорректная дата и время события")
    if all_day:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_item_kind(source: str | None, event_type: str | None) -> str:
    src = str(source or "crm").strip().lower()
    etype = str(event_type or "call").strip().lower()
    if src == "airtable_task" or (src.startswith("airtable_") and "task" in (src + "_" + etype)):
        return "airtable_task"
    if src == "airtable_project" or (
        src.startswith("airtable_") and ("project" in (src + "_" + etype) or etype == "deadline")
    ):
        return "airtable_project"
    if src.startswith("airtable_"):
        return src
    return "calendar_event"


def _resolve_color(event_type: str | None, item_kind: str | None = None) -> str:
    if item_kind and item_kind in EVENT_TYPE_COLORS:
        return EVENT_TYPE_COLORS[item_kind]
    etype = str(event_type or "call").strip().lower()
    return EVENT_TYPE_COLORS.get(etype, "#a855f7")


def _enrich_calendar_event_row(row) -> dict | None:
    data = as_dict(row)
    if data is None:
        return None
    ev_id = int(data["id"])
    start_at = str(data.get("start_at") or "")
    source = str(data.get("source") or "crm")
    event_type = str(data.get("event_type") or "call")
    item_kind = _resolve_item_kind(source, event_type)
    lead_name = data.get("lead_name") or data.get("lead_username")
    return {
        "id": ev_id,
        "uid": f"ev:{ev_id}",
        "item_kind": item_kind,
        "title": data.get("title") or "",
        "event_type": event_type,
        "start_at": start_at,
        "end_at": data.get("end_at"),
        "date": start_at[:10] if start_at else "",
        "all_day": bool(data.get("all_day")),
        "lead_id": int(data["lead_id"]) if data.get("lead_id") is not None else None,
        "lead_name": lead_name,
        "lead_username": data.get("lead_username"),
        "lead_company": data.get("lead_company"),
        "lead_status": data.get("lead_status"),
        "meet_link": data.get("meet_link"),
        "notes": data.get("notes"),
        "status": data.get("status") or "scheduled",
        "source": source,
        "external_id": data.get("external_id"),
        "color": _resolve_color(event_type, item_kind),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def get_calendar_event(conn: sqlite3.Connection, event_id: int) -> dict:
    row = conn.execute(
        """
        SELECT
            ce.*,
            l.name AS lead_name,
            l.username AS lead_username,
            l.company AS lead_company,
            l.status AS lead_status
        FROM calendar_events ce
        LEFT JOIN leads l ON l.id = ce.lead_id
        WHERE ce.id = ?
        """,
        (int(event_id),),
    ).fetchone()
    if row is None:
        raise LookupError("calendar_event")
    return _enrich_calendar_event_row(row)


def create_calendar_event(
    conn: sqlite3.Connection,
    *,
    title: str,
    start_at: str,
    end_at: str | None = None,
    event_type: str = "call",
    all_day: bool = False,
    lead_id: int | None = None,
    meet_link: str | None = None,
    notes: str | None = None,
    status: str = "scheduled",
    source: str = "crm",
    external_id: str | None = None,
) -> dict:
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Укажите название события")

    is_all_day = bool(all_day)
    norm_start = _normalize_calendar_datetime(
        start_at, all_day=is_all_day, default_time="10:00:00", required=True
    )
    norm_end = _normalize_calendar_datetime(
        end_at, all_day=is_all_day, default_time="11:00:00", required=False
    )

    clean_type = (event_type or "call").strip().lower() or "call"
    clean_status = (status or "scheduled").strip().lower() or "scheduled"
    if clean_status not in CALENDAR_EVENT_STATUSES:
        raise ValueError("Некорректный статус события")

    clean_source = (source or "crm").strip() or "crm"
    clean_ext_id = (external_id or "").strip() or None
    clean_meet = (meet_link or "").strip() or None
    clean_notes = (notes or "").strip() or None

    resolved_lead_id: int | None = None
    if lead_id is not None and str(lead_id).strip() != "":
        resolved_lead_id = int(lead_id)
        lead_row = conn.execute(
            "SELECT id FROM leads WHERE id = ?", (resolved_lead_id,)
        ).fetchone()
        if lead_row is None:
            raise LookupError("lead")

    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO calendar_events (
            title, event_type, start_at, end_at, all_day,
            lead_id, meet_link, notes, status, source, external_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_title,
            clean_type,
            norm_start,
            norm_end,
            1 if is_all_day else 0,
            resolved_lead_id,
            clean_meet,
            clean_notes,
            clean_status,
            clean_source,
            clean_ext_id,
            now,
            now,
        ),
    )
    event_id = int(cur.lastrowid)

    if resolved_lead_id is not None:
        display_when = norm_start[:16].replace("T", " ")
        record_lead_event(
            conn,
            resolved_lead_id,
            "call_scheduled",
            f"Назначено событие: {clean_title} ({display_when})",
            {
                "calendar_event_id": event_id,
                "event_type": clean_type,
                "start_at": norm_start,
                "end_at": norm_end,
                "meet_link": clean_meet,
            },
            created_at=now,
        )

    conn.commit()
    return get_calendar_event(conn, event_id)


def update_calendar_event(conn: sqlite3.Connection, event_id: int, **fields) -> dict:
    existing = conn.execute(
        "SELECT * FROM calendar_events WHERE id = ?", (int(event_id),)
    ).fetchone()
    if existing is None:
        raise LookupError("calendar_event")
    curr = as_dict(existing)

    updates: dict = {}
    is_all_day = bool(fields["all_day"]) if "all_day" in fields and fields["all_day"] is not None else bool(curr.get("all_day"))
    if "all_day" in fields and fields["all_day"] is not None:
        updates["all_day"] = 1 if is_all_day else 0

    if "title" in fields and fields["title"] is not None:
        clean_title = str(fields["title"]).strip()
        if not clean_title:
            raise ValueError("Укажите название события")
        updates["title"] = clean_title

    if "event_type" in fields and fields["event_type"] is not None:
        clean_type = str(fields["event_type"]).strip().lower()
        if clean_type:
            updates["event_type"] = clean_type

    if "start_at" in fields and fields["start_at"] is not None:
        old_start = str(curr.get("start_at") or "")
        default_time = old_start[11:19] if len(old_start) >= 19 else "10:00:00"
        updates["start_at"] = _normalize_calendar_datetime(
            fields["start_at"],
            all_day=is_all_day,
            default_time=default_time,
            required=True,
        )

    if "end_at" in fields:
        if fields["end_at"] is None or str(fields["end_at"]).strip() == "":
            updates["end_at"] = None
        else:
            old_end = str(curr.get("end_at") or "")
            default_time = old_end[11:19] if len(old_end) >= 19 else "11:00:00"
            updates["end_at"] = _normalize_calendar_datetime(
                fields["end_at"],
                all_day=is_all_day,
                default_time=default_time,
                required=False,
            )
    elif "start_at" in updates and curr.get("end_at"):
        old_start_dt = _parse_iso_dt(str(curr.get("start_at") or ""))
        old_end_dt = _parse_iso_dt(str(curr.get("end_at") or ""))
        new_start_dt = _parse_iso_dt(updates["start_at"])
        if (
            old_start_dt is not None
            and old_end_dt is not None
            and new_start_dt is not None
        ):
            shifted_end_dt = old_end_dt + (new_start_dt - old_start_dt)
            if shifted_end_dt < new_start_dt:
                shifted_end_dt = new_start_dt
            updates["end_at"] = (
                shifted_end_dt.strftime("%Y-%m-%d")
                if is_all_day
                else shifted_end_dt.strftime("%Y-%m-%dT%H:%M:%S")
            )
        elif str(curr.get("end_at") or "") < updates["start_at"]:
            updates["end_at"] = updates["start_at"]

    eff_start = updates.get("start_at") or str(curr.get("start_at") or "")
    eff_end = updates.get("end_at") if "end_at" in updates else curr.get("end_at")
    if eff_start and eff_end and str(eff_end) < str(eff_start):
        updates["end_at"] = eff_start

    if "lead_id" in fields:
        raw_lid = fields["lead_id"]
        if raw_lid is None or str(raw_lid).strip() == "":
            updates["lead_id"] = None
        else:
            lid = int(raw_lid)
            lead_row = conn.execute("SELECT id FROM leads WHERE id = ?", (lid,)).fetchone()
            if lead_row is None:
                raise LookupError("lead")
            updates["lead_id"] = lid

    if "meet_link" in fields:
        updates["meet_link"] = (str(fields["meet_link"]).strip() or None) if fields["meet_link"] is not None else None

    if "notes" in fields:
        updates["notes"] = (str(fields["notes"]).strip() or None) if fields["notes"] is not None else None

    if "status" in fields and fields["status"] is not None:
        clean_status = str(fields["status"]).strip().lower()
        if clean_status not in CALENDAR_EVENT_STATUSES:
            raise ValueError("Некорректный статус события")
        updates["status"] = clean_status

    if "source" in fields and fields["source"] is not None:
        clean_src = str(fields["source"]).strip()
        if clean_src:
            updates["source"] = clean_src

    if "external_id" in fields:
        updates["external_id"] = (
            str(fields["external_id"]).strip() or None
            if fields["external_id"] is not None
            else None
        )

    if updates:
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(
            f"UPDATE calendar_events SET {assignments} WHERE id = ?",
            [*updates.values(), int(event_id)],
        )
        conn.commit()

    return get_calendar_event(conn, int(event_id))


def delete_calendar_event(conn: sqlite3.Connection, event_id: int) -> dict:
    eid = int(event_id)
    existing = conn.execute(
        "SELECT id FROM calendar_events WHERE id = ?", (eid,)
    ).fetchone()
    if existing is None:
        raise LookupError("calendar_event")
    conn.execute("DELETE FROM calendar_events WHERE id = ?", (eid,))
    conn.commit()
    return {"ok": True, "id": eid, "deleted_id": eid}


def _matches_date_range(date_str: str, start_date: str | None, end_date: str | None) -> bool:
    if not date_str:
        return False
    d = date_str[:10]
    if start_date and d < start_date[:10]:
        return False
    if end_date and d > end_date[:10]:
        return False
    return True


def _matches_event_type_filter(item: dict, event_type_filter: str | None) -> bool:
    if not event_type_filter:
        return True
    flt = str(event_type_filter).strip().lower()
    if not flt or flt == "all":
        return True
    etype = str(item.get("event_type") or "").strip().lower()
    ikind = str(item.get("item_kind") or "").strip().lower()
    if flt in (etype, ikind):
        return True
    if flt in ("call", "calls") and etype in ("call", "meeting"):
        return True
    if flt in ("reminder", "reminders") and (etype == "reminder" or ikind == "reminder"):
        return True
    if flt in ("followup", "followups") and (
        etype in ("followup", "outreach_step") or ikind in ("lead_followup", "campaign_step")
    ):
        return True
    if flt in ("deadline", "deadlines") and (
        etype in ("deadline", "task", "project", "airtable_task", "airtable_project")
        or ikind in ("airtable_task", "airtable_project")
    ):
        return True
    return False


def list_calendar_items(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    event_type: str | None = None,
    lead_id: int | None = None,
    include_completed: bool = True,
) -> dict:
    start_prefix = start_date[:10] if start_date and str(start_date).strip() else None
    end_prefix = end_date[:10] if end_date and str(end_date).strip() else None
    target_lead_id = int(lead_id) if lead_id is not None and str(lead_id).strip() != "" else None

    items: list[dict] = []

    # 1. calendar_events
    ev_rows = conn.execute(
        """
        SELECT
            ce.*,
            l.name AS lead_name,
            l.username AS lead_username,
            l.company AS lead_company,
            l.status AS lead_status
        FROM calendar_events ce
        LEFT JOIN leads l ON l.id = ce.lead_id
        ORDER BY ce.start_at ASC, ce.id ASC
        """
    ).fetchall()
    for row in ev_rows:
        item = _enrich_calendar_event_row(row)
        if item is None:
            continue
        items.append(item)

    # 2. reminders
    rem_rows = conn.execute(
        """
        SELECT
            r.*,
            l.name AS lead_name,
            l.username AS lead_username,
            l.company AS lead_company,
            l.status AS lead_status
        FROM reminders r
        LEFT JOIN leads l ON l.id = r.lead_id
        ORDER BY r.due_at ASC, r.id ASC
        """
    ).fetchall()
    for row in rem_rows:
        r = as_dict(row)
        rid = int(r["id"])
        due_at = str(r.get("due_at") or "")
        rem_status = "completed" if r.get("done_at") else "scheduled"
        lead_name = r.get("lead_name") or r.get("lead_username")
        items.append(
            {
                "id": rid,
                "uid": f"rem:{rid}",
                "item_kind": "reminder",
                "title": r.get("text") or "",
                "event_type": "reminder",
                "start_at": due_at,
                "end_at": None,
                "date": due_at[:10] if due_at else "",
                "all_day": False,
                "lead_id": int(r["lead_id"]) if r.get("lead_id") is not None else None,
                "lead_name": lead_name,
                "lead_username": r.get("lead_username"),
                "lead_company": r.get("lead_company"),
                "lead_status": r.get("lead_status"),
                "meet_link": None,
                "notes": None,
                "status": rem_status,
                "source": "crm",
                "external_id": None,
                "color": EVENT_TYPE_COLORS["reminder"],
                "created_at": r.get("created_at"),
                "updated_at": r.get("done_at") or r.get("created_at"),
            }
        )

    # 3. leads.next_follow_up
    fu_rows = conn.execute(
        """
        SELECT *
        FROM leads
        WHERE next_follow_up IS NOT NULL AND trim(next_follow_up) != ''
        ORDER BY next_follow_up ASC, id ASC
        """
    ).fetchall()
    for row in fu_rows:
        ld = as_dict(row)
        lid = int(ld["id"])
        raw_fu = str(ld["next_follow_up"]).strip()
        fu_date = raw_fu[:10]
        fu_start = f"{fu_date}T11:00:00" if len(raw_fu) <= 10 else raw_fu
        lead_name = (
            ld.get("name")
            or ld.get("username")
            or ld.get("phone")
            or f"Лид #{lid}"
        )
        next_step = (ld.get("next_step") or "").strip()
        title = f"Follow-up: {lead_name}" + (f" · {next_step}" if next_step else "")
        ld_status = str(ld.get("status") or "new").strip().lower()
        fu_status = "completed" if ld_status in ("won", "lost") else "scheduled"
        fu_source = "airtable_lead" if ld.get("airtable_record_id") else "crm"
        items.append(
            {
                "id": lid,
                "uid": f"followup:{lid}",
                "item_kind": "lead_followup",
                "title": title,
                "event_type": "followup",
                "start_at": fu_start,
                "end_at": None,
                "date": fu_date,
                "all_day": False,
                "lead_id": lid,
                "lead_name": lead_name,
                "lead_username": ld.get("username"),
                "lead_company": ld.get("company"),
                "lead_status": ld.get("status"),
                "meet_link": None,
                "notes": next_step or ld.get("note"),
                "status": fu_status,
                "source": fu_source,
                "external_id": ld.get("airtable_record_id"),
                "color": EVENT_TYPE_COLORS["followup"],
                "created_at": ld.get("created_at"),
                "updated_at": ld.get("updated_at"),
            }
        )

    # 4. campaign_recipients in 'in_progress' with next scheduled step
    camp_steps_by_cid: dict[int, list[dict]] = {}
    for s_row in conn.execute(
        "SELECT * FROM campaign_steps ORDER BY campaign_id ASC, step_order ASC"
    ).fetchall():
        sd = as_dict(s_row)
        camp_steps_by_cid.setdefault(int(sd["campaign_id"]), []).append(sd)

    rec_rows = conn.execute(
        """
        SELECT
            cr.*,
            c.name AS campaign_name,
            l.name AS joined_lead_name,
            l.username AS joined_lead_username,
            l.company AS joined_lead_company,
            l.status AS joined_lead_status
        FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        LEFT JOIN leads l ON l.id = cr.lead_id
        WHERE cr.status = 'in_progress'
        ORDER BY cr.id ASC
        """
    ).fetchall()
    for row in rec_rows:
        cr = as_dict(row)
        rec_id = int(cr["id"])
        cid = int(cr["campaign_id"])
        curr_step = int(cr.get("current_step") or 0)
        steps = camp_steps_by_cid.get(cid, [])
        next_step_obj = None
        if curr_step < len(steps):
            next_step_obj = steps[curr_step]
        else:
            for st_obj in steps:
                if int(st_obj.get("step_order") or 0) > curr_step:
                    next_step_obj = st_obj
                    break

        if next_step_obj is None:
            continue
        last_sent_str = cr.get("last_sent_at") or cr.get("updated_at")
        base_dt = _parse_iso_dt(last_sent_str)
        if base_dt is None:
            continue
        if next_step_obj.get("delay_hours") is not None:
            delay_hours = max(0, int(next_step_obj["delay_hours"]))
        else:
            delay_hours = 24
        next_step_num = int(next_step_obj.get("step_order") or (curr_step + 1))

        due_dt = base_dt + timedelta(hours=delay_hours)
        due_iso = due_dt.strftime("%Y-%m-%dT%H:%M:%S")
        rec_name = (
            cr.get("name")
            or cr.get("joined_lead_name")
            or cr.get("username")
            or cr.get("joined_lead_username")
            or cr.get("phone")
            or f"Получатель #{rec_id}"
        )
        camp_name = cr.get("campaign_name") or f"Кампания #{cid}"
        items.append(
            {
                "id": rec_id,
                "uid": f"camp:{rec_id}",
                "item_kind": "campaign_step",
                "title": f"Шаг {next_step_num} ({camp_name}): {rec_name}",
                "event_type": "outreach_step",
                "start_at": due_iso,
                "end_at": None,
                "date": due_iso[:10],
                "all_day": False,
                "lead_id": int(cr["lead_id"]) if cr.get("lead_id") is not None else None,
                "lead_name": rec_name,
                "lead_username": cr.get("username") or cr.get("joined_lead_username"),
                "lead_company": cr.get("company") or cr.get("joined_lead_company"),
                "lead_status": cr.get("joined_lead_status"),
                "meet_link": None,
                "notes": None,
                "status": "scheduled",
                "source": "crm",
                "external_id": None,
                "color": EVENT_TYPE_COLORS["outreach_step"],
                "campaign_id": cid,
                "step_order": next_step_num,
                "created_at": cr.get("created_at"),
                "updated_at": cr.get("updated_at"),
            }
        )

    # Filter items
    filtered: list[dict] = []
    for item in items:
        if not include_completed and item.get("status") in ("completed", "cancelled"):
            continue
        if target_lead_id is not None and item.get("lead_id") != target_lead_id:
            continue
        if not _matches_date_range(item.get("date") or "", start_prefix, end_prefix):
            continue
        if not _matches_event_type_filter(item, event_type):
            continue
        filtered.append(item)

    sorted_items = sorted(
        filtered,
        key=lambda x: (
            x.get("date") or "",
            x.get("start_at") or "",
            x.get("uid") or "",
        ),
    )

    calls_count = sum(
        1 for x in sorted_items if x.get("event_type") in ("call", "meeting")
    )
    reminders_count = sum(
        1
        for x in sorted_items
        if x.get("event_type") == "reminder" or x.get("item_kind") == "reminder"
    )
    followups_count = sum(
        1
        for x in sorted_items
        if x.get("event_type") in ("followup", "outreach_step")
        or x.get("item_kind") in ("lead_followup", "campaign_step")
    )
    deadlines_count = sum(
        1
        for x in sorted_items
        if x.get("event_type")
        in ("deadline", "task", "project", "airtable_task", "airtable_project")
        or x.get("item_kind") in ("airtable_task", "airtable_project")
    )

    return {
        "items": sorted_items,
        "count": len(sorted_items),
        "counts": {
            "total": len(sorted_items),
            "calls": calls_count,
            "reminders": reminders_count,
            "followups": followups_count,
            "deadlines": deadlines_count,
        },
    }


def reschedule_calendar_item(
    conn: sqlite3.Connection,
    *,
    uid: str | None = None,
    item_kind: str | None = None,
    item_id: int | None = None,
    new_start_at: str | None = None,
    new_end_at: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
) -> dict:
    effective_start = new_start_at if new_start_at is not None else start_at
    effective_end = new_end_at if new_end_at is not None else end_at
    if not effective_start or not str(effective_start).strip():
        raise ValueError("Укажите новую дату или время")
    new_start_at = str(effective_start)
    new_end_at = effective_end

    resolved_kind = (item_kind or "").strip().lower()
    resolved_id = int(item_id) if item_id is not None and str(item_id).strip() != "" else None

    if uid and str(uid).strip():
        raw_uid = str(uid).strip()
        prefix, sep, rest = raw_uid.partition(":")
        if sep == ":" and rest.strip():
            resolved_id = int(rest.strip())
            p = prefix.strip().lower()
            if p in ("ev", "event"):
                resolved_kind = "calendar_event"
            elif p in ("rem", "reminder"):
                resolved_kind = "reminder"
            elif p in ("followup", "lead_followup"):
                resolved_kind = "lead_followup"
            elif p in ("camp", "campaign_step", "outreach_step"):
                resolved_kind = "campaign_step"
            else:
                resolved_kind = p

    if resolved_id is None or not resolved_kind:
        raise ValueError("Укажите uid или пару (item_kind, item_id)")

    # 1. Calendar event / Airtable task / Airtable project
    if resolved_kind in (
        "ev",
        "event",
        "calendar_event",
        "airtable_task",
        "airtable_project",
        "call",
        "meeting",
        "deadline",
    ):
        update_kwargs: dict = {"start_at": new_start_at}
        if new_end_at is not None:
            update_kwargs["end_at"] = new_end_at
        updated_ev = update_calendar_event(conn, resolved_id, **update_kwargs)
        src = str(updated_ev.get("source") or "crm")
        ext_id = updated_ev.get("external_id")
        needs_airtable = bool(src.startswith("airtable_") and ext_id)
        return {
            **updated_ev,
            "ok": True,
            "item": updated_ev,
            "item_kind": updated_ev["item_kind"],
            "item_id": resolved_id,
            "new_date": updated_ev["date"],
            "new_start_at": updated_ev["start_at"],
            "needs_airtable_push": needs_airtable,
        }

    # 2. Reminder
    if resolved_kind in ("rem", "reminder"):
        rem_row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (resolved_id,)
        ).fetchone()
        if rem_row is None:
            raise LookupError("reminder")
        rem_dict = as_dict(rem_row)
        old_due = str(rem_dict.get("due_at") or "")
        default_time = old_due[11:19] if len(old_due) >= 19 else "10:00:00"
        s_new = str(new_start_at).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s_new):
            s_new = f"{s_new}T{default_time}"
        norm_due = normalize_due(s_new)

        if rem_dict.get("done_at") is None:
            postpone_reminder(conn, resolved_id, norm_due)
        else:
            conn.execute(
                "UPDATE reminders SET due_at = ? WHERE id = ?",
                (norm_due, resolved_id),
            )
            conn.commit()

        joined = conn.execute(
            """
            SELECT r.*, l.name AS lead_name, l.username AS lead_username, l.company AS lead_company, l.status AS lead_status
            FROM reminders r
            LEFT JOIN leads l ON l.id = r.lead_id
            WHERE r.id = ?
            """,
            (resolved_id,),
        ).fetchone()
        r = as_dict(joined)
        rem_item = {
            "id": resolved_id,
            "uid": f"rem:{resolved_id}",
            "item_kind": "reminder",
            "title": r.get("text") or "",
            "event_type": "reminder",
            "start_at": norm_due,
            "end_at": None,
            "date": norm_due[:10],
            "all_day": False,
            "lead_id": int(r["lead_id"]) if r.get("lead_id") is not None else None,
            "lead_name": r.get("lead_name") or r.get("lead_username"),
            "lead_username": r.get("lead_username"),
            "lead_company": r.get("lead_company"),
            "lead_status": r.get("lead_status"),
            "status": "completed" if r.get("done_at") else "scheduled",
            "source": "crm",
            "external_id": None,
            "color": EVENT_TYPE_COLORS["reminder"],
        }
        return {
            **rem_item,
            "ok": True,
            "item": rem_item,
            "item_kind": "reminder",
            "item_id": resolved_id,
            "new_date": norm_due[:10],
            "new_start_at": norm_due,
            "needs_airtable_push": False,
        }

    # 3. Lead follow-up (leads.next_follow_up)
    if resolved_kind in ("followup", "lead_followup"):
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE id = ?", (resolved_id,)
        ).fetchone()
        if lead_row is None:
            raise LookupError("lead")
        ld = as_dict(lead_row)
        new_date = _normalize_calendar_datetime(
            new_start_at, all_day=True, required=True
        )[:10]
        now = utc_now()
        conn.execute(
            "UPDATE leads SET next_follow_up = ?, updated_at = ? WHERE id = ?",
            (new_date, now, resolved_id),
        )
        record_lead_event(
            conn,
            resolved_id,
            "followup_rescheduled",
            f"Следующее касание перенесено на {new_date}",
            {"next_follow_up": new_date, "previous_follow_up": ld.get("next_follow_up")},
            created_at=now,
        )
        conn.commit()

        lead_name = (
            ld.get("name")
            or ld.get("username")
            or ld.get("phone")
            or f"Лид #{resolved_id}"
        )
        next_step = (ld.get("next_step") or "").strip()
        title = f"Follow-up: {lead_name}" + (f" · {next_step}" if next_step else "")
        needs_airtable = bool(ld.get("airtable_record_id"))
        fu_start = f"{new_date}T11:00:00"
        fu_item = {
            "id": resolved_id,
            "uid": f"followup:{resolved_id}",
            "item_kind": "lead_followup",
            "title": title,
            "event_type": "followup",
            "start_at": fu_start,
            "end_at": None,
            "date": new_date,
            "all_day": False,
            "lead_id": resolved_id,
            "lead_name": lead_name,
            "lead_username": ld.get("username"),
            "lead_company": ld.get("company"),
            "lead_status": ld.get("status"),
            "next_follow_up": new_date,
            "status": "completed" if ld.get("status") in ("won", "lost") else "scheduled",
            "source": "airtable_lead" if needs_airtable else "crm",
            "external_id": ld.get("airtable_record_id"),
            "color": EVENT_TYPE_COLORS["followup"],
        }
        return {
            **fu_item,
            "ok": True,
            "item": fu_item,
            "item_kind": "lead_followup",
            "item_id": resolved_id,
            "new_date": new_date,
            "new_start_at": fu_start,
            "needs_airtable_push": needs_airtable,
        }

    # 4. Campaign recipient outreach step (campaign_step)
    elif resolved_kind in ("camp", "campaign_step", "outreach_step"):
        rec_row = conn.execute(
            "SELECT * FROM campaign_recipients WHERE id = ?", (resolved_id,)
        ).fetchone()
        if rec_row is None:
            raise LookupError("campaign_recipient")
        rec = as_dict(rec_row)
        cid = int(rec["campaign_id"])
        curr_step = int(rec.get("current_step") or 0)
        step_number = curr_step + 1
        step_row = conn.execute(
            "SELECT * FROM campaign_steps WHERE campaign_id = ? AND step_order = ? ORDER BY id ASC LIMIT 1",
            (cid, step_number),
        ).fetchone()
        if step_row is None:
            step_row = conn.execute(
                "SELECT * FROM campaign_steps WHERE campaign_id = ? AND step_order > ? ORDER BY step_order ASC LIMIT 1",
                (cid, curr_step),
            ).fetchone()
        if step_row is not None:
            sd = as_dict(step_row)
            step_number = int(sd.get("step_order") or step_number)
            delay_hours = (
                max(0, int(sd["delay_hours"]))
                if sd.get("delay_hours") is not None
                else 24
            )
        else:
            delay_hours = 24

        old_base_dt = _parse_iso_dt(
            rec.get("last_sent_at") or rec.get("updated_at") or rec.get("created_at")
        )
        if old_base_dt is not None:
            old_due_dt = old_base_dt + timedelta(hours=delay_hours)
            default_time = old_due_dt.strftime("%H:%M:%S")
        else:
            default_time = "10:00:00"

        new_target_iso = _normalize_calendar_datetime(
            new_start_at,
            all_day=False,
            default_time=default_time,
            required=True,
        )
        new_target_dt = _parse_iso_dt(new_target_iso)
        new_last_sent_iso = (
            new_target_dt - timedelta(hours=delay_hours)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        now = utc_now()
        conn.execute(
            "UPDATE campaign_recipients SET last_sent_at = ?, updated_at = ? WHERE id = ?",
            (new_last_sent_iso, now, resolved_id),
        )

        lead_id_val = int(rec["lead_id"]) if rec.get("lead_id") is not None else None
        ld_info: dict = {}
        if lead_id_val is not None:
            conn.execute(
                "UPDATE leads SET next_follow_up = ?, updated_at = ? WHERE id = ?",
                (new_target_iso[:10], now, lead_id_val),
            )
            l_row = conn.execute(
                "SELECT name, username, company, status FROM leads WHERE id = ?",
                (lead_id_val,),
            ).fetchone()
            if l_row is not None:
                ld_info = as_dict(l_row)

        conn.commit()
        camp_row = conn.execute(
            "SELECT name FROM campaigns WHERE id = ?", (cid,)
        ).fetchone()
        camp_name = (
            (camp_row["name"] if camp_row and camp_row["name"] else None)
            or f"Кампания #{cid}"
        )
        rec_name = (
            rec.get("name")
            or ld_info.get("name")
            or rec.get("username")
            or ld_info.get("username")
            or rec.get("phone")
            or f"Получатель #{resolved_id}"
        )
        camp_item = {
            "id": resolved_id,
            "uid": f"camp:{resolved_id}",
            "item_kind": "campaign_step",
            "title": f"Шаг {step_number} ({camp_name}): {rec_name}",
            "event_type": "outreach_step",
            "start_at": new_target_iso,
            "end_at": None,
            "date": new_target_iso[:10],
            "all_day": False,
            "lead_id": lead_id_val,
            "lead_name": rec_name,
            "lead_username": rec.get("username") or ld_info.get("username"),
            "lead_company": rec.get("company") or ld_info.get("company"),
            "lead_status": ld_info.get("status"),
            "meet_link": None,
            "notes": None,
            "status": "scheduled",
            "source": "crm",
            "external_id": None,
            "color": EVENT_TYPE_COLORS["outreach_step"],
            "campaign_id": cid,
            "step_order": step_number,
            "last_sent_at": new_last_sent_iso,
        }
        return {
            **camp_item,
            "ok": True,
            "item": camp_item,
            "item_kind": "campaign_step",
            "item_id": resolved_id,
            "new_date": new_target_iso[:10],
            "new_start_at": new_target_iso,
            "needs_airtable_push": False,
        }

    raise ValueError(f"Неподдерживаемый тип элемента календаря: {resolved_kind}")
