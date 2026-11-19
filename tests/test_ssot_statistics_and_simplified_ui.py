"""Tests for SSOT executive statistics and the simplified 4-tab CRM UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.analytics import get_analytics_summary
from app.db import connect, init_db, utc_now, utc_today
from app.funnel_analytics import ensure_funnel_columns
from app.leads import create_lead
from app.main import create_app


def test_ssot_analytics_running_campaigns_dropped_zero_send_accounts_and_clean_daily_flow(tmp_path):
    conn = connect(tmp_path / "ssot_stats.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    now = utc_now()
    today = utc_today()

    # 1. Healthy connected account + dropped 0-send/0-dialog account (@realbyme_m)
    acc_live = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, telegram_user_id, telegram_username, telegram_name,
                session_path, status, health, daily_limit, sent_today, sent_date,
                created_at, updated_at
            ) VALUES ('+79991112233', 1001, 'saabath', 'Saabath', 's1.session', 'connected', 'active', 20, 4, ?, ?, ?)
            """,
            (today, now, now),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_user_id, telegram_username, telegram_name,
            session_path, status, health, last_error, daily_limit, sent_today, sent_date,
            created_at, updated_at
        ) VALUES ('+79994445566', 1002, 'realbyme_m', 'Dropped Zero', 's2.session', 'error', 'unauthorized', 'AuthKeyDuplicatedError (2 IPs)', 20, 0, NULL, ?, ?)
        """,
        (now, now),
    )

    # 2. Running campaign + Draft campaign
    camp_run = int(
        conn.execute(
            """
            INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
            VALUES ('SoftUGC — Running Alpha', ?, ?, 20, 'running', ?, ?)
            """,
            (acc_live, f"[{acc_live}]", now, now),
        ).lastrowid
    )
    camp_draft = int(
        conn.execute(
            """
            INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
            VALUES ('Draft Queue Beta', ?, ?, 20, 'draft', ?, ?)
            """,
            (acc_live, f"[{acc_live}]", now, now),
        ).lastrowid
    )

    lead_read = create_lead(
        conn, name="Lead Read", username="lead_read", telegram_user_id=2001, status="waiting", account_id=acc_live
    )
    lead_wait = create_lead(
        conn, name="Lead Reply Wait", username="lead_wait", telegram_user_id=2002, status="talking", account_id=acc_live
    )

    # Recipients: 1 in_progress (read via outbox watermark), 1 replied (waiting answer), 1 pending in running, 2 pending in draft
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, step1_text, current_step, status, last_sent_at, assigned_account_id, created_at, updated_at
        ) VALUES
        (?, ?, 2001, 'lead_read', 'Lead Read', 'Hi 1', 1, 'in_progress', ?, ?, ?, ?),
        (?, ?, 2002, 'lead_wait', 'Lead Reply Wait', 'Hi 2', 1, 'replied', ?, ?, ?, ?),
        (?, NULL, 2003, 'lead_pending', 'Lead Pending', 'Hi 3', 0, 'pending', NULL, ?, ?, ?),
        (?, NULL, 2004, 'draft_1', 'Draft 1', 'D1', 0, 'pending', NULL, ?, ?, ?),
        (?, NULL, 2005, 'draft_2', 'Draft 2', 'D2', 0, 'pending', NULL, ?, ?, ?)
        """,
        (
            camp_run, lead_read["id"], now, acc_live, now, now,
            camp_run, lead_wait["id"], now, acc_live, now, now,
            camp_run, acc_live, now, now,
            camp_draft, acc_live, now, now,
            camp_draft, acc_live, now, now,
        ),
    )

    # Dialogs: lead_read (unread=0, read_outbox_max_id=10 >= msg 10), lead_wait (last_from_peer=1), AND a personal non-CRM chat
    dlg_read = int(
        conn.execute(
            """
            INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, lead_id, read_outbox_max_id, last_from_peer, created_at, updated_at)
            VALUES (?, 'user', 2001, 'lead_read', 'Lead Read', ?, 10, 0, ?, ?)
            """,
            (acc_live, lead_read["id"], now, now),
        ).lastrowid
    )
    dlg_wait = int(
        conn.execute(
            """
            INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, lead_id, read_outbox_max_id, last_from_peer, last_message_preview, created_at, updated_at)
            VALUES (?, 'user', 2002, 'lead_wait', 'Lead Reply Wait', ?, 20, 1, 'Интересно, пришлите прайс!', ?, ?)
            """,
            (acc_live, lead_wait["id"], now, now),
        ).lastrowid
    )
    dlg_personal = int(
        conn.execute(
            """
            INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, lead_id, last_from_peer, created_at, updated_at)
            VALUES (?, 'user', 9999, 'miroslava_personal', 'Miroslava Personal', NULL, 1, ?, ?)
            """,
            (acc_live, now, now),
        ).lastrowid
    )

    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES
        (?, 10, 'out', 'Hi 1', ?, ?, ?),
        (?, 20, 'out', 'Hi 2', ?, ?, ?),
        (?, 21, 'in', 'Интересно, пришлите прайс!', ?, ?, ?),
        (?, 901, 'out', 'Личное сообщение', ?, ?, ?),
        (?, 902, 'in', 'Личный ответ 1', ?, ?, ?),
        (?, 903, 'in', 'Личный ответ 2', ?, ?, ?)
        """,
        (
            dlg_read, now, now, now,
            dlg_wait, now, now, now,
            dlg_wait, now, now, now,
            dlg_personal, now, now, now,
            dlg_personal, now, now, now,
            dlg_personal, now, now, now,
        ),
    )
    conn.commit()

    stats = get_analytics_summary(conn, period="7d")

    # Outreach running/draft separation + strict SSOT read+unseen==contacted
    outreach = stats["outreach"]
    assert outreach["pending_running_total"] == 1
    assert outreach["pending_draft_total"] == 2
    assert len(outreach["active_campaigns"]) == 1
    assert outreach["active_campaigns"][0]["name"] == "SoftUGC — Running Alpha"
    assert outreach["active_campaigns"][0]["read"] == 2

    deep_sum = stats["deep_outreach"]["summary"]
    assert deep_sum["contacted"] == 2
    assert deep_sum["read"] == 2
    assert deep_sum["unseen_total"] == 0
    assert deep_sum["read"] + deep_sum["unseen_total"] == deep_sum["contacted"]
    assert deep_sum["pending_queue"] == 1
    assert deep_sum["waiting_our_answer"] == 1
    assert deep_sum["waiting_on_healthy_accounts"] == 1
    assert deep_sum["waiting_on_dropped_accounts"] == 0

    # Dropped 0-send/0-dialog account MUST be surfaced in dropped_accounts & excluded from healthy daily limit
    accs = stats["accounts"]
    assert accs["healthy_accounts"] == 1
    assert accs["dropped_accounts_count"] == 1
    assert accs["daily_limit"] == 20
    dropped_tags = [a["telegram_username"] for a in accs["dropped_accounts"]]
    assert dropped_tags == ["realbyme_m"]
    assert "AuthKeyDuplicatedError" in (accs["dropped_accounts"][0].get("last_error") or "")

    # Personal non-CRM dialog messages MUST NOT pollute daily_flow
    flow_sent = sum(d["messages_sent"] for d in stats["charts"]["daily_flow"])
    flow_rep = sum(d["replies_received"] for d in stats["charts"]["daily_flow"])
    assert flow_sent == 2
    assert flow_rep == 1


def test_simplified_4_tab_ui_routes_and_zero_hardcoded_stats(tmp_path):
    conn = connect(tmp_path / "ui.db")
    init_db(conn)
    app = create_app(conn=conn)
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        html = res.text

        # Strictly 4 sidebar nav links in Russian
        for sheet_key, label in (
            ("inbox", "Входящие"),
            ("leads", "Лиды"),
            ("outreach", "Рассылки"),
            ("analytics", "Статистика"),
        ):
            assert f'data-sheet-link="{sheet_key}"' in html
            assert label in html

        # Removed tabs must not appear in sidebar nav
        nav_section = html.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
        assert nav_section.count("<a ") == 4
        for removed in ("calendar", "funnel", "s05", "s06", "s07", "accounts", "digest", "settings"):
            assert f'data-sheet-link="{removed}"' not in nav_section

        assert 'id="leads-view-toggle"' in html
        assert 'id="outreach-subtabs"' in html
        assert 'id="stats-campaigns-grid"' in html
        assert 'id="stats-accounts-health"' in html
        assert "2 174" not in html
        assert "129 ждущих" not in html

        js = client.get("/static/app.js").text
        assert "setActiveSheet(" not in js
        assert "2 174" not in js
        assert "129 ждущих" not in js
        assert "moveDialogToStage" in js
        assert "renderStatsExecutiveBlocks" in js
