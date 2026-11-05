from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now, utc_today
from app.main import create_app


def test_analytics_summary_empty_db_edge_case(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        resp = client.get("/api/analytics/summary")
        assert resp.status_code == 200
        data = resp.json()

        assert data["pipeline_value"] == 0
        assert data["won_value"] == 0
        assert data["lost_value"] == 0
        assert data["total_value"] == 0
        assert data["win_rate_pct"] == 0.0
        assert data["reply_rate_pct"] == 0.0

        funnel = data["funnel"]
        assert funnel["total_leads"] == 0
        assert funnel["active_leads"] == 0
        assert funnel["won_leads"] == 0
        assert funnel["lost_leads"] == 0
        assert funnel["closed_win_rate_pct"] == 0.0
        assert len(funnel["stages"]) >= 5
        assert all(s["count"] == 0 and s["amount"] == 0 for s in funnel["stages"])
        assert all(c["rate_pct"] == 0.0 for c in funnel["conversions"])

        outreach = data["outreach"]
        assert outreach["total_campaigns"] == 0
        assert outreach["total_recipients"] == 0
        assert outreach["sent"] == 0
        assert outreach["replied"] == 0
        assert outreach["reply_rate_pct"] == 0.0
        assert outreach["campaigns"] == []

        accounts = data["accounts"]
        assert accounts["total_accounts"] == 0
        assert accounts["connected_accounts"] == 0
        assert accounts["sent_today"] == 0
        assert accounts["daily_limit"] == 0
        assert accounts["utilization_pct"] == 0.0
        assert accounts["items"] == []


def test_analytics_summary_with_demo_seed(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        seed_resp = client.post("/api/demo/seed")
        assert seed_resp.status_code == 200

        resp = client.get("/api/analytics/summary")
        assert resp.status_code == 200
        data = resp.json()

        # 1. Funnel & Money
        funnel = data["funnel"]
        assert funnel["total_leads"] == 11
        assert funnel["active_leads"] == 9
        assert funnel["won_leads"] == 1
        assert funnel["lost_leads"] == 1
        assert funnel["pipeline_value"] == 21850
        assert funnel["won_value"] == 4500
        assert funnel["lost_value"] == 1500
        assert funnel["total_value"] == 27850
        assert funnel["win_rate_pct"] == 9.1
        assert funnel["closed_win_rate_pct"] == 50.0

        by_stage = funnel["by_stage"]
        assert by_stage["new"]["count"] == 3
        assert by_stage["new"]["amount"] == 6800
        assert by_stage["talking"]["count"] == 3
        assert by_stage["talking"]["amount"] == 9200
        assert by_stage["waiting"]["count"] == 3
        assert by_stage["waiting"]["amount"] == 5850
        assert by_stage["won"]["count"] == 1
        assert by_stage["won"]["amount"] == 4500
        assert by_stage["lost"]["count"] == 1
        assert by_stage["lost"]["amount"] == 1500

        conv_rates = funnel["conversion_rates"]
        assert conv_rates["new_to_talking_pct"] == 63.6
        assert conv_rates["talking_to_waiting_pct"] == 57.1
        assert conv_rates["waiting_to_won_pct"] == 25.0

        # 2. Outreach KPIs
        outreach = data["outreach"]
        assert outreach["total_campaigns"] == 2
        assert outreach["running_campaigns"] == 1
        assert outreach["total_recipients"] >= 6
        assert outreach["sent"] == (
            outreach["in_progress"] + outreach["completed"] + outreach["replied"]
        )
        assert outreach["replied"] >= 2
        assert outreach["reply_rate_pct"] > 0
        assert len(outreach["campaigns"]) == 2
        for camp in outreach["campaigns"]:
            assert camp["sent"] == camp["in_progress"] + camp["completed"] + camp["replied"]
            expected_rate = (
                round((camp["replied"] / camp["sent"]) * 100, 1)
                if camp["sent"] > 0
                else 0.0
            )
            assert camp["reply_rate_pct"] == expected_rate

        # 3. Accounts Efficiency & Health
        accounts = data["accounts"]
        assert accounts["total_accounts"] == 4
        assert accounts["connected_accounts"] == 4
        assert accounts["sent_today"] == 19  # 7 + 4 + 3 + 5
        assert accounts["daily_limit"] == 70  # 20 + 10 + 15 + 25
        assert accounts["utilization_pct"] == 27.1
        assert accounts["by_health"]["active"] == 4
        assert accounts["total_dialogs"] == 9
        assert accounts["total_replies"] >= 9

        items_by_phone = {a["phone"]: a for a in accounts["items"]}
        tim_acc = items_by_phone["+79995550101"]
        assert tim_acc["sent_today"] == 7
        assert tim_acc["daily_limit"] == 20
        assert tim_acc["dialogs_count"] == 6
        assert tim_acc["replies_count"] >= 6
        assert tim_acc["health"] == "active"

        wa_acc = items_by_phone["+79990007788"]
        assert wa_acc["provider"] == "whatsapp"
        assert wa_acc["sent_today"] == 5
        assert wa_acc["daily_limit"] == 25
        assert wa_acc["dialogs_count"] == 3
        assert wa_acc["replies_count"] >= 2


def test_analytics_custom_health_stale_sent_date_and_ui_elements(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    today = utc_today()

    # Ensure leads.amount column works if present or added
    have_cols = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    if "amount" not in have_cols:
        conn.execute("ALTER TABLE leads ADD COLUMN amount INTEGER NOT NULL DEFAULT 0")

    # Insert accounts with varied health and stale vs today sent_date
    conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_username, telegram_name,
            created_at, health, daily_limit, sent_today, sent_date
        ) VALUES
            ('+79001110001', 'data/sessions/a1.session', 'connected', 'acc_active', 'Active Acc', ?, 'active', 50, 25, ?),
            ('+79001110002', 'data/sessions/a2.session', 'connected', 'acc_stale', 'Stale Date Acc', ?, 'offline', 30, 19, '2020-01-01'),
            ('+79001110003', 'data/sessions/a3.session', 'error', 'acc_banned', 'Banned Acc', ?, 'banned', 20, 5, ?)
        """,
        (now, today, now, now, today),
    )
    conn.execute(
        """
        INSERT INTO leads (name, username, status, amount, created_at, updated_at)
        VALUES
            ('Deal A', 'deal_a', 'new', 1000, ?, ?),
            ('Deal B', 'deal_b', 'talking', 3000, ?, ?),
            ('Deal C', 'deal_c', 'won', 6000, ?, ?)
        """,
        (now, now, now, now, now, now),
    )
    conn.commit()

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        summary = client.get("/api/analytics/summary").json()
        assert summary["pipeline_value"] == 4000
        assert summary["won_value"] == 6000
        assert summary["total_value"] == 10000
        assert summary["win_rate_pct"] == 33.3
        assert summary["funnel"]["closed_win_rate_pct"] == 100.0

        accs = summary["accounts"]
        assert accs["total_accounts"] == 3
        assert accs["connected_accounts"] == 2
        # Stale sent_date ('2020-01-01') resets sent_today to 0
        items_map = {a["phone"]: a for a in accs["items"]}
        assert items_map["+79001110001"]["sent_today"] == 25
        assert items_map["+79001110001"]["utilization_pct"] == 50.0
        assert items_map["+79001110002"]["sent_today"] == 0
        assert items_map["+79001110003"]["health"] == "banned"
        assert accs["by_health"]["active"] == 1
        assert accs["by_health"]["offline"] == 1
        assert accs["by_health"]["banned"] == 1

        index_html = client.get("/").text
        assert 'id="analytics"' in index_html
        assert 'id="pipeline-analytics-bar"' in index_html
        assert 'id="stat-pipeline-value"' in index_html
        assert 'id="stat-won-value"' in index_html
