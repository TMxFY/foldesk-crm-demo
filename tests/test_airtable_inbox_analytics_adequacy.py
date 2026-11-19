"""Track 2 Adequacy Tests: Airtable idempotency, Inbox SSOT waiting/search/drift,
Analytics <=60KB payload + drilldown, and Security / dead /a/ proxy removal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.airtable_sync import (
    prune_redundant_airtable_synced_events,
    pull_from_airtable,
    sync_airtable,
)
from app.analytics import get_analytics_summary, get_deep_outreach_bucket
from app.db import (
    connect,
    create_pipeline_stage,
    init_db,
    is_waiting_our_answer,
    sql_is_waiting_our_answer_expr,
    utc_now,
    utc_today,
)
from app.events import record_lead_event
from app.funnel_analytics import ensure_funnel_columns
from app.inbox import list_inbox, repair_dialog_last_from_peer_drift
from app.leads import create_lead, update_lead
from app.main import create_app


class _FakeAirtable:
    def __init__(self, leads: list[dict[str, Any]]):
        self.table_name = "Leads"
        self.tables: dict[str, list[dict[str, Any]]] = {
            "Leads": [dict(r, fields=dict(r.get("fields") or {})) for r in leads],
            "Tasks": [],
            "Projects": [],
        }
        self.calls: list[dict[str, Any]] = []
        self._seq = 1

    def list_records(self, table: str | None = None, **_kw):
        t = table or self.table_name
        self.calls.append({"op": "list", "table": t})
        return [dict(r, fields=dict(r.get("fields") or {})) for r in self.tables.get(t, [])]

    get_records = list_records

    def create_record(self, table: str, fields: dict[str, Any], *, typecast: bool = True):
        self._seq += 1
        rec_id = f"recNew{self._seq}"
        rec = {"id": rec_id, "fields": dict(fields)}
        self.tables.setdefault(table, []).append(rec)
        self.calls.append({"op": "create", "table": table, "id": rec_id})
        return dict(rec, fields=dict(rec["fields"]))

    def update_record(
        self, table: str, record_id: str, fields: dict[str, Any], *, typecast: bool = True
    ):
        self.calls.append({"op": "update", "table": table, "id": record_id})
        for r in self.tables.setdefault(table, []):
            if r["id"] == record_id:
                r["fields"].update(fields)
                return dict(r, fields=dict(r["fields"]))
        rec = {"id": record_id, "fields": dict(fields)}
        self.tables[table].append(rec)
        return dict(rec, fields=dict(rec["fields"]))


def test_airtable_sync_idempotent_zero_write_storm_and_event_compaction(tmp_path: Path):
    conn = connect(tmp_path / "at_idem.db")
    init_db(conn)

    client = _FakeAirtable(
        leads=[
            {
                "id": "recAlpha01",
                "fields": {
                    "Lead Name": "Alpha Corp",
                    "Company": "Alpha Corp",
                    "Status": "In conversation",
                    "Value": 3000,
                    "Monthly Value": 1500,
                    "Contact": "@alpha_ceo",
                    "Offer Type": ["Performance"],
                    "Lead Source": "Cold outreach",
                    "Notes": "Initial sync note",
                },
            },
            {
                "id": "recBeta02",
                "fields": {
                    "Lead Name": "Beta Labs",
                    "Company": "Beta Labs",
                    "Status": "New",
                    "Contact": "@beta_founder",
                    "Lead Source": "Cold outreach",
                },
            },
        ]
    )

    first = sync_airtable(conn, direction="both", client=client)
    assert first["created_in_crm"] == 2
    assert first["updated_in_crm"] == 0
    assert first["pushed_to_airtable"] == 0
    ev_after_first = conn.execute(
        "SELECT COUNT(*) FROM lead_events WHERE event_type = 'airtable_synced'"
    ).fetchone()[0]
    assert ev_after_first == 2

    # 2 consecutive pulls/syncs within the exact same second must produce 0 updates, 0 pushes, 0 new events
    second = sync_airtable(conn, direction="both", client=client)
    assert second["created_in_crm"] == 0
    assert second["updated_in_crm"] == 0
    assert second["pushed_to_airtable"] == 0

    third = pull_from_airtable(conn, client=client, preserve_dirty=True)
    assert third["created_in_crm"] == 0
    assert third["updated_in_crm"] == 0

    ev_after_third = conn.execute(
        "SELECT COUNT(*) FROM lead_events WHERE event_type = 'airtable_synced'"
    ).fetchone()[0]
    assert ev_after_third == 2

    # Local edit in same second pushes once, then next sync is completely quiet (no .1 dirty feedback loop!)
    alpha_id = int(
        conn.execute("SELECT id FROM leads WHERE airtable_record_id = 'recAlpha01'").fetchone()["id"]
    )
    update_lead(conn, alpha_id, next_step="Send pilot proposal")
    fourth = sync_airtable(conn, direction="both", client=client)
    assert fourth["pushed_to_airtable"] == 1
    assert fourth["updated_in_crm"] == 0

    fifth = sync_airtable(conn, direction="both", client=client)
    assert fifth["pushed_to_airtable"] == 0
    assert fifth["updated_in_crm"] == 0

    # Compaction: prune_redundant_airtable_synced_events keeps latest 1 per lead_id & preserves other events
    record_lead_event(conn, alpha_id, "airtable_synced", "dup 1")
    record_lead_event(conn, alpha_id, "airtable_synced", "dup 2")
    record_lead_event(conn, alpha_id, "reply_received", "Client replied")
    conn.commit()

    prune_res = prune_redundant_airtable_synced_events(conn)
    assert prune_res["deleted"] >= 2
    assert prune_res["remaining"] == 2
    reply_cnt = conn.execute(
        "SELECT COUNT(*) FROM lead_events WHERE event_type = 'reply_received'"
    ).fetchone()[0]
    assert reply_cnt == 1
    conn.close()


def test_ssot_is_waiting_our_answer_parity_across_inbox_leads_and_analytics(tmp_path: Path):
    assert is_waiting_our_answer(1, "talking") is True
    assert is_waiting_our_answer(1, "new") is True
    assert is_waiting_our_answer(1, None) is True
    assert is_waiting_our_answer(0, "talking") is False
    for closed in ("lost", "won", "on_hold", "archived", "Cancelled", "Signed", "On hold"):
        assert is_waiting_our_answer(1, closed) is False

    conn = connect(tmp_path / "ssot_waiting.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    create_pipeline_stage(conn, key="archived", label="Архив")
    now = utc_now()
    today = utc_today()

    acc_id = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, telegram_user_id, telegram_username, telegram_name,
                session_path, status, health, daily_limit, sent_today, sent_date,
                created_at, updated_at
            ) VALUES ('+79990001100', 1001, 'sender_one', 'Sender One', 's1.session', 'connected', 'active', 20, 1, ?, ?, ?)
            """,
            (today, now, now),
        ).lastrowid
    )

    lead_specs = [
        ("Active Talking", "act_talk", 2001, "talking", 1, "Interested!"),
        ("Active New", "act_new", 2002, "new", 1, "Tell me more"),
        ("Closed Lost", "closed_lost", 2003, "lost", 1, "No thanks"),
        ("Closed Won", "closed_won", 2004, "won", 1, "Thanks!"),
        ("Paused Hold", "paused_hold", 2005, "on_hold", 1, "Maybe next quarter"),
        ("Archived Deal", "arch_deal", 2006, "archived", 1, "Old thread"),
        ("Our Turn Active", "our_turn", 2007, "talking", 0, "Sent deck"),
    ]
    for name, uname, tg_id, status, lfp, preview in lead_specs:
        lead = create_lead(
            conn,
            name=name,
            username=uname,
            telegram_user_id=tg_id,
            status=status,
            account_id=acc_id,
        )
        dlg_id = int(
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, is_group,
                    unread_count, last_message_at, last_message_preview, last_from_peer,
                    lead_id, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?)
                """,
                (acc_id, tg_id, uname, name, now, preview, lfp, lead["id"], now, now),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (dlg_id, tg_id * 10, "in" if lfp == 1 else "out", preview, now, now, now),
        )
    conn.commit()

    # SQL predicate agrees with Python predicate
    sql_expr = sql_is_waiting_our_answer_expr("d.last_from_peer", "l.status")
    sql_waiting_cnt = int(
        conn.execute(
            f"SELECT COUNT(*) FROM dialogs d JOIN leads l ON l.id = d.lead_id WHERE {sql_expr}"
        ).fetchone()[0]
    )
    assert sql_waiting_cnt == 2

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        inbox_all = client.get("/api/inbox", params={"account_id": 0, "with_counts": 1, "crm_only": 1}).json()
        inbox_wait = client.get(
            "/api/inbox", params={"account_id": 0, "chip": "waiting", "with_counts": 1, "crm_only": 1}
        ).json()
        leads_resp = client.get("/api/leads").json()
        analytics = client.get("/api/analytics/summary").json()

        inbox_waiting_count = inbox_all["counts"]["waiting"]
        leads_waiting_count = sum(1 for ld in leads_resp if ld.get("is_waiting_our_answer"))
        analytics_waiting_count = analytics["deep_outreach"]["summary"]["waiting_our_answer"]

        assert inbox_waiting_count == 2
        assert len(inbox_wait["dialogs"]) == 2
        assert {d["username"] for d in inbox_wait["dialogs"]} == {"act_talk", "act_new"}
        assert leads_waiting_count == inbox_waiting_count
        assert analytics_waiting_count == inbox_waiting_count
    conn.close()


def test_repair_dialog_last_from_peer_drift_and_preserve_unbacked_snapshot(tmp_path: Path):
    conn = connect(tmp_path / "drift.db")
    init_db(conn)
    now = utc_now()
    acc_id = int(
        conn.execute(
            "INSERT INTO accounts (phone, session_path, status, health, created_at, updated_at) "
            "VALUES ('+79991110000', 's.session', 'connected', 'active', ?, ?)",
            (now, now),
        ).lastrowid
    )

    # Dialog 1: drifted lfp=0, latest cached message is 'in' at same/newer ts -> repair to 1
    d1 = int(
        conn.execute(
            "INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, last_message_at, last_message_preview, last_from_peer, created_at, updated_at) "
            "VALUES (?, 'user', 3001, 'drift_in', 'Drift In', '2026-11-10T10:00:00', 'Hello', 0, ?, ?)",
            (acc_id, now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
        "VALUES (?, 101, 'in', 'Hello', '2026-11-10T10:05:00', ?, ?)",
        (d1, now, now),
    )

    # Dialog 2: drifted lfp=1, latest cached message is 'out' at same ts -> repair to 0
    d2 = int(
        conn.execute(
            "INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, last_message_at, last_message_preview, last_from_peer, created_at, updated_at) "
            "VALUES (?, 'user', 3002, 'drift_out', 'Drift Out', '2026-11-10T10:00:00', 'We replied', 1, ?, ?)",
            (acc_id, now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
        "VALUES (?, 102, 'out', 'We replied', '2026-11-10T10:00:00', ?, ?)",
        (d2, now, now),
    )

    # Dialog 3: genuinely newer unbacked live snapshot (snapshot 60s newer than latest cached out msg) -> keep lfp=1!
    d3 = int(
        conn.execute(
            "INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, last_message_at, last_message_preview, last_from_peer, created_at, updated_at) "
            "VALUES (?, 'user', 3003, 'fresh_snap', 'Fresh Snap', '2026-11-10T10:02:00', 'Brand new incoming', 1, ?, ?)",
            (acc_id, now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
        "VALUES (?, 103, 'out', 'Old pitch', '2026-11-10T10:00:00', ?, ?)",
        (d3, now, now),
    )
    conn.commit()

    rep = repair_dialog_last_from_peer_drift(conn)
    assert rep["repaired"] == 2
    assert rep["repaired_to_in"] == 1
    assert rep["repaired_to_out"] == 1
    assert conn.execute("SELECT last_from_peer FROM dialogs WHERE id = ?", (d1,)).fetchone()[0] == 1
    assert conn.execute("SELECT last_from_peer FROM dialogs WHERE id = ?", (d2,)).fetchone()[0] == 0
    assert conn.execute("SELECT last_from_peer FROM dialogs WHERE id = ?", (d3,)).fetchone()[0] == 1
    conn.close()


def test_workspace_total_dialogs_stability_crm_search_and_multi_account_tg_search(tmp_path: Path):
    conn = connect(tmp_path / "inbox_search.db")
    init_db(conn)
    now = utc_now()

    acc1 = int(
        conn.execute(
            "INSERT INTO accounts (phone, session_path, status, health, telegram_user_id, telegram_username, created_at, updated_at) "
            "VALUES ('+79991111111', 's1.session', 'connected', 'active', 101, 'acc_one', ?, ?)",
            (now, now),
        ).lastrowid
    )
    acc2 = int(
        conn.execute(
            "INSERT INTO accounts (phone, session_path, status, health, telegram_user_id, telegram_username, created_at, updated_at) "
            "VALUES ('+79992222222', 's2.session', 'connected', 'active', 102, 'acc_two', ?, ?)",
            (now, now),
        ).lastrowid
    )

    l1 = create_lead(
        conn,
        name="Alice Founder",
        company="HyperScale AI",
        note="Asked about TikTok revshare",
        username="alice_h",
        telegram_user_id=5001,
        status="talking",
        account_id=acc1,
    )
    l2 = create_lead(
        conn,
        name="Bob Builder",
        company="GrowthForge",
        note="Waiting for case study",
        username="bob_gf",
        telegram_user_id=5002,
        status="waiting",
        account_id=acc2,
    )
    l3 = create_lead(
        conn,
        name="Carol Ops",
        company="PixelLabs",
        username="carol_px",
        telegram_user_id=5003,
        status="talking",
        account_id=acc2,
    )

    for aid, lead, lfp, preview in (
        (acc1, l1, 1, "Can you share demo reels?"),
        (acc2, l2, 0, "Sent you the deck"),
        (acc2, l3, 1, "Sounds great, let us talk"),
    ):
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, created_at, updated_at
            ) VALUES (?, 'user', ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
            """,
            (aid, lead["telegram_user_id"], lead["username"], lead["name"], now, preview, lfp, lead["id"], now, now),
        )
    conn.commit()

    class _MultiSearchGateway:
        def __init__(self):
            self.called_sessions: list[str] = []

        async def search_dialogs(self, session_path: str, q: str, limit: int = 30):
            self.called_sessions.append(session_path)
            if session_path == "s1.session":
                return [
                    {
                        "telegram_user_id": 5001,
                        "username": "alice_h",
                        "name": "Alice Founder",
                        "last_message_at": "2026-11-19T09:00:00",
                        "last_message_preview": "Can you share demo reels?",
                        "last_from_peer": 1,
                    }
                ]
            if session_path == "s2.session":
                return [
                    {
                        "telegram_user_id": 5003,
                        "username": "carol_px",
                        "name": "Carol Ops",
                        "last_message_at": "2026-11-19T10:00:00",
                        "last_message_preview": "Sounds great, let us talk",
                        "last_from_peer": 1,
                    }
                ]
            return []

    gw = _MultiSearchGateway()
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        # workspace_total_dialogs stays 3 regardless of chip, single account, or search query
        all_res = client.get("/api/inbox", params={"account_id": 0, "with_counts": 1, "crm_only": 1}).json()
        assert all_res["workspace_total_dialogs"] == 3
        assert len(all_res["dialogs"]) == 3

        chip_res = client.get(
            "/api/inbox", params={"account_id": 0, "chip": "waiting", "with_counts": 1, "crm_only": 1}
        ).json()
        assert len(chip_res["dialogs"]) == 2
        assert chip_res["workspace_total_dialogs"] == 3

        acc1_res = client.get(
            "/api/inbox", params={"account_id": acc1, "with_counts": 1, "crm_only": 1}
        ).json()
        assert len(acc1_res["dialogs"]) == 1
        assert acc1_res["workspace_total_dialogs"] == 3

        counts_res = client.get("/api/inbox/counts", params={"account_id": acc1, "crm_only": 1}).json()
        assert counts_res["workspace_total_dialogs"] == 3

        # Server-side CRM search matches company, note, and message snippet without mutating leads
        by_company = client.get(
            "/api/inbox", params={"account_id": 0, "q": "HyperScale", "with_counts": 1, "crm_only": 1}
        ).json()
        assert [d["username"] for d in by_company["dialogs"]] == ["alice_h"]
        assert by_company["workspace_total_dialogs"] == 3

        by_note = client.get(
            "/api/inbox", params={"account_id": 0, "q": "TikTok revshare", "crm_only": 1}
        ).json()
        assert [d["username"] for d in by_note] == ["alice_h"]

        by_snippet = client.get(
            "/api/inbox", params={"account_id": 0, "q": "demo reels", "crm_only": 1}
        ).json()
        assert [d["username"] for d in by_snippet] == ["alice_h"]

        # Multi-account Telegram search via account_id=0 and /api/inbox/telegram-search
        tg_multi = client.get("/api/accounts/0/dialogs", params={"q": "demo", "crm_only": 1})
        assert tg_multi.status_code == 200
        assert {d["username"] for d in tg_multi.json()} == {"alice_h", "carol_px"}
        assert set(gw.called_sessions) == {"s1.session", "s2.session"}

        tg_alias = client.get("/api/inbox/telegram-search", params={"account_id": 0, "q": "demo", "crm_only": 1})
        assert tg_alias.status_code == 200
        assert len(tg_alias.json()) == 2
    conn.close()


def test_analytics_summary_payload_under_60kb_and_deep_outreach_drilldown(tmp_path: Path):
    conn = connect(tmp_path / "analytics_budget.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    now = utc_now()
    today = utc_today()

    acc_id = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, telegram_user_id, telegram_username, telegram_name,
                session_path, status, health, daily_limit, sent_today, sent_date,
                created_at, updated_at
            ) VALUES ('+79995550000', 1001, 'big_sender', 'Big Sender', 's1.session', 'connected', 'active', 20, 5, ?, ?, ?)
            """,
            (today, now, now),
        ).lastrowid
    )
    camp_id = int(
        conn.execute(
            "INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at) "
            "VALUES ('SoftUGC — Stress Scale', ?, ?, 140, 'running', ?, ?)",
            (acc_id, f"[{acc_id}]", now, now),
        ).lastrowid
    )

    long_pitch = "Hey, saw your app. " + ("We run SoftUGC organic short-form AI UGC distribution. " * 6)
    long_reply = "Super interested! Can you send examples and pricing? " + ("More details here. " * 10)

    # Seed 160 replied recipients across 35 distinct hooks (60 waiting, 100 stalled, all positive)
    for i in range(160):
        tg_id = 90000 + i
        uname = f"scale_user_{i}"
        is_wait = i < 60
        extra = json.dumps(
            {
                "niche": f"Niche {i % 20}",
                "hook_format": f"Hook Variant #{i % 35}: Strong Founder Pain Angle",
                "reply_intent": "positive_interested",
                "replied_to_step": "step1_personal_pitch",
                "fine_status": "replied_waiting_our_answer" if is_wait else "replied_we_answered_seen_stalled",
            }
        )
        lead = create_lead(
            conn,
            name=f"Scale Brand {i}",
            company=f"Scale Co {i}",
            username=uname,
            telegram_user_id=tg_id,
            status="talking",
            account_id=acc_id,
        )
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, assigned_account_id, sent_account_id,
                telegram_user_id, username, name, company, category,
                step1_text, current_step, status, last_sent_at, extra_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'replied', ?, ?, ?, ?)
            """,
            (
                camp_id,
                lead["id"],
                acc_id,
                acc_id,
                tg_id,
                uname,
                f"Scale Brand {i}",
                f"Scale Co {i}",
                f"Category {i % 15}",
                long_pitch,
                now,
                extra,
                now,
                now,
            ),
        )
        dlg_id = int(
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, is_group,
                    unread_count, last_message_at, last_message_preview, last_from_peer,
                    lead_id, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acc_id, tg_id, uname, f"Scale Brand {i}", 1 if is_wait else 0, now, long_reply[:120], 1 if is_wait else 0, lead["id"], now, now),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
            "VALUES (?, ?, 'in', ?, ?, ?, ?)",
            (dlg_id, tg_id * 10 + 1, long_reply, now, now, now),
        )
        if not is_wait:
            conn.execute(
                "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) "
                "VALUES (?, ?, 'out', ?, ?, ?, ?)",
                (dlg_id, tg_id * 10 + 2, "Sent examples!", now, now, now),
            )
    conn.commit()

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        resp = client.get("/api/analytics/summary")
        assert resp.status_code == 200
        raw_bytes = resp.content
        assert len(raw_bytes) <= 60 * 1024, f"/api/analytics/summary exceeded 60KB budget: {len(raw_bytes)}B"

        data = resp.json()
        deep = data["deep_outreach"]
        assert deep["summary"]["waiting_our_answer"] == 60
        assert deep["summary"]["positive_count"] == 160
        assert deep["waiting_items_total"] == 60
        assert deep["positive_items_total"] == 160
        assert deep["stalled_items_total"] == 100
        assert len(deep["waiting_items"]) == 15
        assert len(deep["positive_items"]) == 10
        assert len(deep["stalled_items"]) == 10
        assert len(deep["hooks_breakdown"]) == 12

        # Drilldown endpoint returns full paginated records
        page1 = client.get(
            "/api/analytics/deep-outreach",
            params={"bucket": "waiting", "limit": 25, "offset": 0},
        ).json()
        assert page1["total"] == 60
        assert len(page1["items"]) == 25
        assert page1["has_more"] is True

        page3 = client.get(
            "/api/analytics/deep-outreach",
            params={"bucket": "waiting", "limit": 25, "offset": 50},
        ).json()
        assert len(page3["items"]) == 10
        assert page3["has_more"] is False

        hooks_full = get_deep_outreach_bucket(conn, bucket="hooks", limit=50, offset=0)
        assert hooks_full["total"] == 35
        assert len(hooks_full["items"]) == 35
    conn.close()


def test_security_removes_e2e_backdoor_guards_deep_health_and_unmounts_tg_web_proxy(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    monkeypatch.setenv("BOT_TOKEN", "e2e-not-a-secret")
    monkeypatch.setenv("MCP_API_KEY", "mcp-super-secret-2026")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongTimPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "StrongVladPass#2026")
    monkeypatch.setenv("CRM_OPERATOR_PASSWORD", "StrongOpPass#2026")

    conn = connect(tmp_path / "sec_hardened.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    # 1. Default TestClient (non-loopback host 'testclient'): BOT_TOKEN=e2e-not-a-secret MUST NOT bypass auth!
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/api/leads").status_code == 401
        r_root = client.get("/")
        assert r_root.status_code == 302
        assert r_root.headers["location"].startswith("/login")

        # 2. Shallow /api/health stays public (200), external /api/health?deep=1 returns 401
        r_shallow = client.get("/api/health")
        assert r_shallow.status_code == 200
        assert r_shallow.json()["ok"] is True
        assert "active_accounts" not in r_shallow.json()

        assert client.get("/api/health?deep=1").status_code == 401
        assert (
            client.get(
                "/api/health?deep=1",
                headers={"X-Forwarded-For": "203.0.113.10", "User-Agent": "Mozilla/5.0"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/health?deep=1",
                headers={"CF-Connecting-IP": "198.51.100.5", "User-Agent": "Mozilla/5.0"},
            ).status_code
            == 401
        )

        # 3. Authenticated /api/health?deep=1 works via Bearer MCP_API_KEY, X-API-Key, and CRM session cookie
        r_bearer = client.get(
            "/api/health?deep=1",
            headers={"Authorization": "Bearer mcp-super-secret-2026"},
        )
        assert r_bearer.status_code == 200
        assert "active_accounts" in r_bearer.json()

        r_xapi = client.get(
            "/api/health?deep=1",
            headers={"X-API-Key": "mcp-super-secret-2026"},
        )
        assert r_xapi.status_code == 200
        assert "leadgen_ready" in r_xapi.json()

        r_login = client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "StrongTimPass#2026"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        assert r_login.status_code == 200
        r_cookie_deep = client.get("/api/health?deep=1", headers={"User-Agent": "Mozilla/5.0"})
        assert r_cookie_deep.status_code == 200
        assert "db_ok" in r_cookie_deep.json()

        # 4. Dead /a/* proxy is completely unmounted -> 404 (even when authenticated!)
        for dead_path in ("/a", "/a/", "/a/foldesk-session-init.js", "/a/index.html"):
            r_dead = client.get(dead_path, headers={"User-Agent": "Mozilla/5.0"})
            assert r_dead.status_code == 404, f"Expected 404 on {dead_path}, got {r_dead.status_code}"

    # 5. Direct loopback (127.0.0.1) WITHOUT proxy headers can query /api/health?deep=1,
    #    but loopback WITH X-Forwarded-For or CF-Connecting-IP is still rejected with 401!
    class _LoopbackScopeApp:
        def __init__(self, inner):
            self._inner = inner

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope, client=("127.0.0.1", 50000))
            await self._inner(scope, receive, send)

    with TestClient(_LoopbackScopeApp(app)) as loop_client:
        assert loop_client.get("/api/health?deep=1").status_code == 200
        assert (
            loop_client.get(
                "/api/health?deep=1",
                headers={"X-Forwarded-For": "203.0.113.10", "User-Agent": "Mozilla/5.0"},
            ).status_code
            == 401
        )
        assert (
            loop_client.get(
                "/api/health?deep=1",
                headers={"CF-Connecting-IP": "198.51.100.5", "User-Agent": "Mozilla/5.0"},
            ).status_code
            == 401
        )
    conn.close()
