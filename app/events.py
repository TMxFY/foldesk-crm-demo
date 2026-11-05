from datetime import datetime, timedelta, timezone
import json
import sqlite3

from app.db import as_dict, utc_now

EVENT_TYPES = (
    "created",
    "status_changed",
    "amount_changed",
    "outreach_sent",
    "reply_received",
    "note_added",
    "reminder_set",
    "merged",
)

STATUS_LABELS_RU = {
    "new": "Лид",
    "talking": "Разговор",
    "waiting": "Ждём",
    "won": "Выигран",
    "lost": "Проигран",
}


def ensure_events_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            meta_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS lead_events_lead_created
            ON lead_events(lead_id, created_at);
        """
    )


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 11 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _due_key(value: str | None) -> str:
    if not value:
        return ""
    try:
        from app.reminders import normalize_due

        return normalize_due(value)
    except ValueError:
        dt = _parse_iso_utc(value)
        if dt is not None:
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        return str(value).strip()


def _public_event(row) -> dict | None:
    data = as_dict(row)
    if data is None:
        return None
    raw_meta = data.get("meta_json")
    meta_obj = {}
    if isinstance(raw_meta, str) and raw_meta.strip():
        try:
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict):
                meta_obj = parsed
        except Exception:
            meta_obj = {}
    elif isinstance(raw_meta, dict):
        meta_obj = raw_meta
        data["meta_json"] = json.dumps(raw_meta, ensure_ascii=False)
    data["meta"] = meta_obj
    return data


def record_lead_event(
    conn: sqlite3.Connection,
    lead_id: int | None,
    event_type: str,
    title: str,
    meta: dict | str | None = None,
    created_at: str | None = None,
    *,
    commit: bool = False,
) -> dict | None:
    if lead_id is None:
        return None
    try:
        lid = int(lead_id)
    except (TypeError, ValueError):
        return None

    ensure_events_table(conn)
    lead_row = conn.execute("SELECT id FROM leads WHERE id = ?", (lid,)).fetchone()
    if lead_row is None:
        return None

    clean_type = (event_type or "note_added").strip() or "note_added"
    clean_title = (title or clean_type).strip() or clean_type
    stamp = (created_at or "").strip() or utc_now()

    if meta is None:
        meta_json = "{}"
    elif isinstance(meta, str):
        meta_json = meta
    else:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except Exception:
            meta_json = "{}"

    cur = conn.execute(
        """
        INSERT INTO lead_events (lead_id, event_type, title, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (lid, clean_type, clean_title, meta_json, stamp),
    )
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM lead_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _public_event(row)


add_lead_event = record_lead_event
log_lead_event = record_lead_event


def list_lead_events(conn: sqlite3.Connection, lead_id: int, limit: int = 200) -> list[dict]:
    ensure_events_table(conn)
    lid = int(lead_id)
    cap = max(1, int(limit or 200))
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT * FROM lead_events
            WHERE lead_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        ) sub
        ORDER BY created_at ASC, id ASC
        """,
        (lid, cap),
    ).fetchall()
    if not rows:
        lead_row = conn.execute("SELECT * FROM leads WHERE id = ?", (lid,)).fetchone()
        if lead_row is not None:
            ld = as_dict(lead_row)
            label = ld.get("name") or ld.get("username") or ld.get("phone") or f"Лид #{lid}"
            record_lead_event(
                conn,
                lid,
                "created",
                f"Лид создан: {label}",
                {"status": ld.get("status") or "new", "provider": ld.get("provider") or "telegram"},
                created_at=ld.get("created_at") or utc_now(),
                commit=True,
            )
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM lead_events
                    WHERE lead_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                ) sub
                ORDER BY created_at ASC, id ASC
                """,
                (lid, cap),
            ).fetchall()
    return [_public_event(r) for r in rows if r is not None]


def transfer_lead_events_on_merge(
    conn: sqlite3.Connection,
    source_lead_id: int,
    target_lead_id: int,
    source_name: str | None = None,
    *,
    commit: bool = False,
) -> dict | None:
    ensure_events_table(conn)
    src_id = int(source_lead_id)
    tgt_id = int(target_lead_id)
    conn.execute(
        "UPDATE lead_events SET lead_id = ? WHERE lead_id = ?",
        (tgt_id, src_id),
    )
    label = (source_name or "").strip() or f"#{src_id}"
    ev = record_lead_event(
        conn,
        tgt_id,
        "merged",
        f"Слит дубль лида {label} (#{src_id} → #{tgt_id})",
        {"source_lead_id": src_id, "target_lead_id": tgt_id, "source_name": source_name},
        commit=commit,
    )
    return ev


def get_today_focus(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    stale_days: int = 3,
) -> dict:
    now_dt = _parse_iso_utc(now) if now else datetime.now(timezone.utc)
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    today_date = now_iso[:10]
    today_end_iso = f"{today_date}T23:59:59"

    # 1. waiting_reply: dialogs where client wrote last (last_from_peer = 1)
    dlg_rows = conn.execute(
        """
        SELECT
            d.*,
            COALESCE(d.provider, a.provider, 'telegram') AS resolved_provider,
            a.phone AS account_phone,
            COALESCE(a.telegram_name, a.telegram_username, a.phone) AS account_name,
            a.telegram_username AS account_username,
            l.id AS joined_lead_id,
            l.name AS lead_name,
            l.username AS lead_username,
            l.status AS lead_status
        FROM dialogs d
        LEFT JOIN accounts a ON a.id = d.account_id
        LEFT JOIN leads l ON l.id = d.lead_id
        WHERE d.peer_type = 'user'
          AND d.peer_id != 777000
          AND COALESCE(d.is_group, 0) = 0
          AND d.last_from_peer = 1
        ORDER BY (d.last_message_at IS NULL), d.last_message_at DESC, d.updated_at DESC, d.id DESC
        """
    ).fetchall()

    waiting_reply = []
    for row in dlg_rows:
        d = as_dict(row)
        lead_id = d.get("lead_id") or d.get("joined_lead_id")
        lead_name = d.get("lead_name")
        lead_status = d.get("lead_status")
        if lead_id is None:
            match_clauses = []
            match_params = []
            if d.get("peer_id") is not None:
                match_clauses.append("telegram_user_id = ?")
                match_params.append(int(d["peer_id"]))
            if d.get("username"):
                match_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
                match_params.append(str(d["username"]))
            if d.get("phone"):
                match_clauses.append("(phone IS NOT NULL AND phone = ?)")
                match_params.append(str(d["phone"]))
            if match_clauses:
                l_row = conn.execute(
                    f"SELECT id, name, status FROM leads WHERE {' OR '.join(match_clauses)} ORDER BY id LIMIT 1",
                    match_params,
                ).fetchone()
                if l_row is not None:
                    lead_id = int(l_row["id"])
                    lead_name = l_row["name"]
                    lead_status = l_row["status"]

        provider = (d.get("resolved_provider") or d.get("provider") or "telegram").strip().lower()
        display_name = (d.get("title") or "").strip() or (lead_name or "").strip() or d.get("username") or d.get("phone") or f"Диалог #{d['id']}"
        waiting_reply.append(
            {
                "id": int(d["id"]),
                "dialog_id": int(d["id"]),
                "account_id": d.get("account_id"),
                "lead_id": int(lead_id) if lead_id is not None else None,
                "telegram_user_id": d.get("peer_id"),
                "username": d.get("username"),
                "name": display_name,
                "title": d.get("title") or display_name,
                "phone": d.get("phone"),
                "provider": provider,
                "unread_count": int(d.get("unread_count") or 0),
                "last_message_at": d.get("last_message_at"),
                "last_message_preview": d.get("last_message_preview"),
                "last_from_peer": int(d.get("last_from_peer") or 1),
                "account_name": d.get("account_name"),
                "account_phone": d.get("account_phone"),
                "lead_status": lead_status,
            }
        )

    # 2. due_reminders: open reminders due today or overdue + open tasks
    rem_rows = conn.execute(
        """
        SELECT r.*, l.name AS lead_name, l.username AS lead_username, l.status AS lead_status, l.provider AS lead_provider
        FROM reminders r
        JOIN leads l ON l.id = r.lead_id
        WHERE r.done_at IS NULL
        ORDER BY r.due_at ASC, r.id ASC
        """
    ).fetchall()

    due_reminders = []
    for row in rem_rows:
        r = as_dict(row)
        due_norm = _due_key(r.get("due_at"))
        if not due_norm or due_norm <= today_end_iso:
            is_overdue = bool(due_norm and due_norm <= now_iso)
            due_reminders.append(
                {
                    "id": int(r["id"]),
                    "reminder_id": int(r["id"]),
                    "kind": "reminder",
                    "lead_id": int(r["lead_id"]),
                    "lead_name": r.get("lead_name"),
                    "lead_username": r.get("lead_username"),
                    "lead_status": r.get("lead_status"),
                    "provider": r.get("lead_provider") or "telegram",
                    "due_at": r.get("due_at"),
                    "text": r.get("text") or "",
                    "title": r.get("text") or "",
                    "done_at": None,
                    "is_overdue": is_overdue,
                    "overdue": is_overdue,
                    "created_at": r.get("created_at"),
                }
            )

    task_rows = conn.execute(
        """
        SELECT t.*, l.name AS lead_name, l.username AS lead_username, l.status AS lead_status, l.provider AS lead_provider
        FROM tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.done_at IS NULL
        ORDER BY t.created_at ASC, t.id ASC
        """
    ).fetchall()
    open_tasks = []
    for row in task_rows:
        t = as_dict(row)
        task_item = {
            "id": int(t["id"]),
            "task_id": int(t["id"]),
            "kind": "task",
            "lead_id": int(t["lead_id"]),
            "lead_name": t.get("lead_name"),
            "lead_username": t.get("lead_username"),
            "lead_status": t.get("lead_status"),
            "provider": t.get("lead_provider") or "telegram",
            "due_at": t.get("created_at") or now_iso,
            "text": t.get("title") or "",
            "title": t.get("title") or "",
            "done_at": None,
            "is_overdue": False,
            "overdue": False,
            "created_at": t.get("created_at"),
        }
        open_tasks.append(task_item)
    if not due_reminders and open_tasks:
        due_reminders = list(open_tasks)

    # 3. stale_leads: leads in talking / waiting without updates
    cutoff_days = max(0, int(stale_days if stale_days is not None else 3))
    cutoff_dt = now_dt - timedelta(days=cutoff_days)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")

    candidate_rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE status IN ('talking', 'waiting')
        ORDER BY updated_at ASC, id ASC
        """
    ).fetchall()

    stale_leads = []
    for row in candidate_rows:
        ld = as_dict(row)
        upd_str = ld.get("updated_at") or ld.get("created_at") or ""
        upd_dt = _parse_iso_utc(upd_str)
        upd_iso = upd_dt.strftime("%Y-%m-%dT%H:%M:%S") if upd_dt else upd_str
        if upd_iso and upd_iso <= cutoff_iso:
            days_old = max(0, int((now_dt - upd_dt).total_seconds() // 86400)) if upd_dt else cutoff_days
            ld["days_stale"] = days_old
            ld["provider"] = ld.get("provider") or "telegram"
            ld["amount"] = int(ld.get("amount") or 0)
            ld["currency"] = ld.get("currency") or "USD"
            stale_leads.append(ld)

    return {
        "waiting_reply": waiting_reply,
        "due_reminders": due_reminders,
        "open_tasks": open_tasks,
        "stale_leads": stale_leads,
        "counts": {
            "waiting_reply": len(waiting_reply),
            "due_reminders": len(due_reminders),
            "open_tasks": len(open_tasks),
            "stale_leads": len(stale_leads),
            "total": len(waiting_reply) + len(due_reminders) + len(stale_leads),
            "total_focus": len(waiting_reply) + len(due_reminders) + len(stale_leads),
        },
        "generated_at": now_iso,
    }
