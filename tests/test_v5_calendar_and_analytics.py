from datetime import datetime, timedelta, timezone

import pytest

from app.analytics import get_analytics_summary
from app.calendar_events import (
    create_calendar_event,
    delete_calendar_event,
    get_calendar_event,
    list_calendar_items,
    reschedule_calendar_item,
    update_calendar_event,
)
from app.db import connect, ensure_airtable_pipeline_stages, init_db
from app.digest import (
    apply_digest_callback,
    build_digest,
    list_today_schedule_for_digest,
    open_today,
)
from app.events import get_today_focus, list_lead_events
from app.leads import create_lead
from app.reminders import create_reminder


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def test_calendar_events_crud_and_lead_timeline(tmp_path):
    conn = connect(tmp_path / "crm_cal.db")
    init_db(conn)

    lead = create_lead(
        conn,
        name="Артём Соколов",
        username="artem_sokolov",
        phone="+79991112233",
        status="talking",
    )
    conn.execute(
        "UPDATE leads SET company = 'FinTech Corp' WHERE id = ?",
        (lead["id"],),
    )
    conn.commit()

    # 1. Create timed event with date-only start_at -> normalized to T10:00:00
    ev1 = create_calendar_event(
        conn,
        title="Демо-созвон по интеграции",
        start_at="2026-11-05",
        event_type="call",
        all_day=False,
        lead_id=lead["id"],
        meet_link="https://meet.google.com/abc-defg-hij",
        notes="Показать аналитику и календарь",
    )
    assert ev1["id"] > 0
    assert ev1["uid"] == f"ev:{ev1['id']}"
    assert ev1["item_kind"] == "calendar_event"
    assert ev1["start_at"] == "2026-11-05T10:00:00"
    assert ev1["date"] == "2026-11-05"
    assert ev1["all_day"] is False
    assert ev1["lead_id"] == lead["id"]
    assert ev1["lead_name"] == "Артём Соколов"
    assert ev1["lead_username"] == "artem_sokolov"
    assert ev1["lead_company"] == "FinTech Corp"
    assert ev1["lead_status"] == "talking"
    assert ev1["status"] == "scheduled"

    # Verify lead_events recorded call_scheduled
    events = list_lead_events(conn, lead["id"])
    call_evs = [e for e in events if e["event_type"] == "call_scheduled"]
    assert len(call_evs) == 1
    assert "Назначено событие: Демо-созвон по интеграции (2026-11-05 10:00)" in call_evs[0]["title"]

    # 2. Create all_day event
    ev2 = create_calendar_event(
        conn,
        title="Дедлайн сдачи КП",
        start_at="2026-11-06T15:30:00",
        all_day=True,
        event_type="deadline",
        source="airtable_project",
        external_id="recProj001",
    )
    assert ev2["start_at"] == "2026-11-06"
    assert ev2["all_day"] is True
    assert ev2["item_kind"] == "airtable_project"

    # 3. Update event
    updated = update_calendar_event(
        conn,
        ev1["id"],
        title="Финальный созвон по договору",
        start_at="2026-11-05T14:30:00",
        end_at="2026-11-05T15:30:00",
        status="completed",
    )
    assert updated["title"] == "Финальный созвон по договору"
    assert updated["start_at"] == "2026-11-05T14:30:00"
    assert updated["end_at"] == "2026-11-05T15:30:00"
    assert updated["status"] == "completed"

    # 4. Delete event
    deleted = delete_calendar_event(conn, ev2["id"])
    assert deleted["ok"] is True
    assert deleted["deleted_id"] == ev2["id"]
    with pytest.raises(LookupError):
        get_calendar_event(conn, ev2["id"])

    # Validation errors
    with pytest.raises(ValueError):
        create_calendar_event(conn, title="   ", start_at="2026-11-05")
    with pytest.raises(LookupError):
        create_calendar_event(conn, title="Call", start_at="2026-11-05", lead_id=999999)


def test_list_calendar_items_four_sources_and_drag_drop_reschedule(tmp_path):
    conn = connect(tmp_path / "crm_cal_items.db")
    init_db(conn)
    now_iso = "2026-11-04T12:00:00"

    # Create lead with next_follow_up and airtable_record_id
    lead1 = create_lead(
        conn,
        name="Мария Климова",
        username="maria_klim",
        phone="+79990001111",
        status="call_scheduled",
    )
    conn.execute(
        """
        UPDATE leads
        SET next_follow_up = '2026-11-06',
            next_step = 'Обсудить условия пилота',
            airtable_record_id = 'recLeadMaria1'
        WHERE id = ?
        """,
        (lead1["id"],),
    )

    # Source 1: calendar_events (crm call + airtable_task)
    cal_call = create_calendar_event(
        conn,
        title="Созвон с Марией",
        start_at="2026-11-05T13:00:00",
        end_at="2026-11-05T14:00:00",
        event_type="call",
        lead_id=lead1["id"],
    )
    air_task = create_calendar_event(
        conn,
        title="Подготовить презентацию",
        start_at="2026-11-07",
        all_day=True,
        event_type="task",
        source="airtable_task",
        external_id="recTask777",
        lead_id=lead1["id"],
    )

    # Source 2: reminders
    rem = create_reminder(
        conn,
        lead1["id"],
        "2026-11-05T16:00:00",
        "Проверить оплату счёта",
    )

    # Source 4: campaign_recipients in 'in_progress'
    c_cur = conn.execute(
        """
        INSERT INTO campaigns (name, daily_limit, status, created_at, updated_at)
        VALUES ('Ноябрьский аутрич', 20, 'running', ?, ?)
        """,
        (now_iso, now_iso),
    )
    camp_id = int(c_cur.lastrowid)
    conn.execute(
        """
        INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
        VALUES
            (?, 1, 0, 'Привет, {name}!', ?),
            (?, 2, 48, 'Фоллоу-ап по предложению', ?)
        """,
        (camp_id, now_iso, camp_id, now_iso),
    )
    rec_cur = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, username, name, current_step,
            status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, 'maria_klim', 'Мария Климова', 1, 'in_progress', '2026-11-04T10:00:00', ?, ?)
        """,
        (camp_id, lead1["id"], now_iso, now_iso),
    )
    rec_id = int(rec_cur.lastrowid)
    conn.commit()

    # List all items in range [2026-11-05, 2026-11-08]
    feed = list_calendar_items(
        conn,
        start_date="2026-11-05",
        end_date="2026-11-08",
    )
    items = feed["items"]
    counts = feed["counts"]

    assert counts["total"] == 5
    assert counts["calls"] == 1
    assert counts["reminders"] == 1
    assert counts["followups"] == 2  # lead_followup + campaign_step
    assert counts["deadlines"] == 1  # airtable_task

    uids = {it["uid"] for it in items}
    assert f"ev:{cal_call['id']}" in uids
    assert f"ev:{air_task['id']}" in uids
    assert f"rem:{rem['id']}" in uids
    assert f"followup:{lead1['id']}" in uids
    assert f"camp:{rec_id}" in uids

    # Verify Drag-and-Drop reschedule for calendar_event (preserves time when given YYYY-MM-DD)
    res_ev = reschedule_calendar_item(
        conn,
        uid=f"ev:{cal_call['id']}",
        new_start_at="2026-11-09",
    )
    assert res_ev["ok"] is True
    assert res_ev["item_kind"] == "calendar_event"
    assert res_ev["new_date"] == "2026-11-09"
    assert res_ev["start_at"] == "2026-11-09T13:00:00"
    assert res_ev["needs_airtable_push"] is False

    # Verify Drag-and-Drop reschedule for airtable_task (sets needs_airtable_push=True)
    res_air = reschedule_calendar_item(
        conn,
        item_kind="airtable_task",
        item_id=air_task["id"],
        new_start_at="2026-11-10",
    )
    assert res_air["ok"] is True
    assert res_air["item_kind"] == "airtable_task"
    assert res_air["new_date"] == "2026-11-10"
    assert res_air["needs_airtable_push"] is True

    # Verify Drag-and-Drop reschedule for reminder
    res_rem = reschedule_calendar_item(
        conn,
        uid=f"rem:{rem['id']}",
        new_start_at="2026-11-11T17:30:00",
    )
    assert res_rem["ok"] is True
    assert res_rem["item_kind"] == "reminder"
    assert res_rem["new_date"] == "2026-11-11"
    assert res_rem["start_at"] == "2026-11-11T17:30:00"

    # Verify Drag-and-Drop reschedule for lead_followup
    res_fu = reschedule_calendar_item(
        conn,
        uid=f"followup:{lead1['id']}",
        new_start_at="2026-11-12",
    )
    assert res_fu["ok"] is True
    assert res_fu["item_kind"] == "lead_followup"
    assert res_fu["new_date"] == "2026-11-12"
    assert res_fu["needs_airtable_push"] is True
    row_lead = conn.execute(
        "SELECT next_follow_up FROM leads WHERE id = ?", (lead1["id"],)
    ).fetchone()
    assert row_lead["next_follow_up"] == "2026-11-12"


def test_today_focus_and_digest_with_calendar_events(tmp_path):
    conn = connect(tmp_path / "crm_focus_digest.db")
    init_db(conn)

    lead = create_lead(
        conn,
        name="Денис Орлов",
        username="denis_orlov",
        phone="+79995554433",
        status="talking",
    )
    create_reminder(conn, lead["id"], "2026-11-05T11:00:00", "Отправить смету")
    cal_ev = create_calendar_event(
        conn,
        title="Зум с Денисом",
        start_at="2026-11-05T15:00:00",
        lead_id=lead["id"],
        event_type="call",
    )
    # Future call (should not appear in today's digest or today_calls)
    create_calendar_event(
        conn,
        title="Будущий звонок",
        start_at="2026-11-08T15:00:00",
        lead_id=lead["id"],
    )

    focus = get_today_focus(conn, now="2026-11-05T12:00:00")
    assert "today_calls" in focus
    assert len(focus["today_calls"]) == 1
    assert focus["today_calls"][0]["title"] == "Зум с Денисом"
    assert focus["counts"]["today_calls"] == 1

    cal_digest = list_today_schedule_for_digest(conn, now="2026-11-05T09:00:00")
    assert len(cal_digest) == 1
    assert cal_digest[0]["text"] == "📅 Зум с Денисом"
    assert cal_digest[0]["kind"] == "calendar_event"

    rows = open_today(conn, now="2026-11-05T09:00:00")
    assert len(rows) == 2
    text, markup = build_digest(rows)
    assert "Отправить смету" in text
    assert "📅 Зум с Денисом" in text
    assert "15:00" in text
    assert markup is not None
    assert len(markup["inline_keyboard"]) == 2

    cb_res = apply_digest_callback(conn, f"cal_done:{cal_ev['id']}")
    assert cb_res["ok"] is True
    assert cb_res["action"] == "cal_done"
    assert get_calendar_event(conn, cal_ev["id"])["status"] == "completed"


def test_analytics_five_charts_and_periods(tmp_path):
    conn = connect(tmp_path / "crm_analytics_v5.db")
    init_db(conn)
    ensure_airtable_pipeline_stages(conn)

    now_dt = datetime.now(timezone.utc)
    today_str = now_dt.strftime("%Y-%m-%d")
    now_iso = _iso(now_dt)
    d_2_iso = _iso(now_dt - timedelta(days=2))
    d_15_iso = _iso(now_dt - timedelta(days=15))
    d_45_iso = _iso(now_dt - timedelta(days=45))

    # Accounts (Telegram & WhatsApp)
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, provider, telegram_username, telegram_name,
            created_at, health, daily_limit, sent_today, sent_date
        ) VALUES
            (1, '+79990000001', 'data/sessions/tg1.session', 'connected', 'telegram', 'tg_sales', 'TG Sales', ?, 'active', 30, 12, ?),
            (2, '+79990000002', 'data/sessions/wa1.session', 'connected', 'whatsapp', 'wa_sales', 'WA Sales', ?, 'active', 25, 8, ?)
        """,
        (now_iso, today_str, now_iso, today_str),
    )

    # Leads across all 7 stages with amounts, monthly_value, and lead_source
    leads_seed = [
        ("Lead New", "new", 1000, 200, "Cold outreach", "telegram", now_iso),
        ("Lead Talking", "talking", 2500, 500, "Cold outreach", "telegram", d_2_iso),
        ("Lead Call", "call_scheduled", 4000, 800, "Website", "telegram", d_2_iso),
        ("Lead Waiting", "waiting", 3500, 700, "Referral", "whatsapp", d_15_iso),
        ("Lead Hold", "on_hold", 1500, 300, "Website", "telegram", d_15_iso),
        ("Lead Won", "won", 8000, 1500, "Referral", "whatsapp", d_2_iso),
        ("Lead Lost", "lost", 2000, 400, "Cold outreach", "telegram", d_45_iso),
    ]
    for idx, (name, st, amt, mrr, src, prov, c_at) in enumerate(leads_seed, start=1):
        conn.execute(
            """
            INSERT INTO leads (
                id, account_id, name, username, status, amount, monthly_value,
                lead_source, provider, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idx,
                1 if prov == "telegram" else 2,
                name,
                f"user_{idx}",
                st,
                amt,
                mrr,
                src,
                prov,
                c_at,
                c_at,
            ),
        )

    # Dialogs & messages (inbound + outbound across hours for heatmap & daily_flow)
    conn.execute(
        """
        INSERT INTO dialogs (
            id, account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES
            (1, 1, 'user', 5001, 'user_2', 'Lead Talking', 'telegram', 0, 1, 1, 0, 2, ?, ?),
            (2, 2, 'user', 5006, 'user_6', 'Lead Won', 'whatsapp', 0, 0, 1, 0, 6, ?, ?)
        """,
        (d_2_iso, now_iso, d_2_iso, now_iso),
    )
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES
            (1, 101, 'out', 'Привет!', ?, ?, ?),
            (1, 102, 'in', 'Добрый день, интересно', ?, ?, ?),
            (2, 201, 'out', 'Отправляю договор', ?, ?, ?),
            (2, 202, 'in', 'Подписали!', ?, ?, ?)
        """,
        (
            d_2_iso,
            d_2_iso,
            d_2_iso,
            now_iso,
            now_iso,
            now_iso,
            d_2_iso,
            d_2_iso,
            d_2_iso,
            now_iso,
            now_iso,
            now_iso,
        ),
    )
    conn.commit()

    # Check all 4 periods: 7d, 30d, 90d, all
    for period, expected_min_len in (("7d", 7), ("30d", 30), ("90d", 90), ("all", 14)):
        summary = get_analytics_summary(conn, period=period)
        assert summary["period"] == period
        assert summary["pipeline_value"] == 12500  # 1000+2500+4000+3500+1500
        assert summary["won_value"] == 8000
        assert summary["lost_value"] == 2000
        assert summary["total_value"] == 22500
        assert summary["monthly_value"] == 4000  # active (2500) + won (1500)

        funnel = summary["funnel"]
        assert len(funnel["stages"]) == 7
        for st_key in ("new", "talking", "call_scheduled", "waiting", "on_hold", "won", "lost"):
            assert st_key in funnel["by_stage"]
            assert funnel["by_stage"][st_key]["count"] == 1

        charts = summary["charts"]
        assert set(charts.keys()) == {
            "daily_flow",
            "revenue_timeline",
            "funnel_dropoff",
            "channels_and_sources",
            "reply_heatmap",
        }

        # Chart 1: daily_flow
        df = charts["daily_flow"]
        assert len(df) >= expected_min_len
        assert all(
            {"date", "label", "new_leads", "messages_sent", "replies_received"} <= set(pt.keys())
            for pt in df
        )
        assert sum(pt["messages_sent"] for pt in df) >= 2
        assert sum(pt["replies_received"] for pt in df) >= 2

        # Chart 2: revenue_timeline
        rt = charts["revenue_timeline"]
        assert len(rt) == len(df)
        assert rt[-1]["cumulative_won"] == 8000
        assert rt[-1]["pipeline_amount"] == 12500
        assert rt[-1]["monthly_value"] == 4000

        # Chart 3: funnel_dropoff
        fd = charts["funnel_dropoff"]
        assert len(fd) == 7
        assert [s["key"] for s in fd] == [
            "new",
            "talking",
            "call_scheduled",
            "waiting",
            "on_hold",
            "won",
            "lost",
        ]
        for s in fd:
            assert {
                "key",
                "label",
                "airtable_label",
                "count",
                "amount",
                "monthly_value",
                "reached_count",
                "reached_amount",
                "conversion_from_prev_pct",
                "dropoff_count",
                "dropoff_amount",
                "color",
            } <= set(s.keys())

        # Chart 4: channels_and_sources
        cs = charts["channels_and_sources"]
        assert len(cs["by_channel"]) == 2
        assert {c["provider"] for c in cs["by_channel"]} == {"telegram", "whatsapp"}
        assert len(cs["by_source"]) >= 2
        assert len(cs["accounts_ranking"]) == 2

        # Chart 5: reply_heatmap
        hm = charts["reply_heatmap"]
        assert hm["days"] == ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        assert hm["slots"] == ["00–04", "04–08", "08–12", "12–16", "16–20", "20–24"]
        assert len(hm["matrix"]) == 7
        assert all(len(row) == 6 for row in hm["matrix"])
        assert hm["total_inbound"] == 2
        assert hm["max_value"] >= 1
        assert hm["peak_day"] in hm["days"]
        assert hm["peak_slot"] in hm["slots"]
