import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now, utc_today
from app.funnel_analytics import (
    apply_read_outbox_update,
    compute_icp_quality,
    ensure_funnel_columns,
    get_outreach_funnel,
    sync_dialog_read_receipts,
)
from app.main import create_app, start_account_watch
from app.telegram_gateway import FakeGateway, TelethonGateway, listening_event


def _make_conn(tmp_path):
    conn = connect(tmp_path / "funnel.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    return conn


def _seed_account(conn, phone="+79990001122", session_path="data/sessions/op1.session") -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id, telegram_username,
            telegram_name, daily_limit, created_at, updated_at
        ) VALUES (?, ?, 'connected', 1001, 'vlad_op', 'Vlad', 40, ?, ?)
        """,
        (phone, session_path, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_campaign(conn, account_id: int, name: str) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
        VALUES (?, ?, ?, 30, 'running', ?, ?)
        """,
        (name, account_id, f"[{account_id}]", now, now),
    )
    cid = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
        VALUES (?, 1, 0, '{step1_text}', ?)
        """,
        (cid, now),
    )
    conn.commit()
    return cid


def test_ensure_funnel_columns_idempotent(tmp_path):
    conn = connect(tmp_path / "cols.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    ensure_funnel_columns(conn)

    dlg_cols = {r[1] for r in conn.execute("PRAGMA table_info(dialogs)").fetchall()}
    rec_cols = {r[1] for r in conn.execute("PRAGMA table_info(campaign_recipients)").fetchall()}

    assert "read_outbox_max_id" in dlg_cols
    for col in ("read_at", "variant_key", "pain_angle", "quality_score", "added_date"):
        assert col in rec_cols


def test_icp_quality_scoring_and_tiers():
    vlad_draft = (
        "Hey! Built SoftUGC as an organic short-form arm — 183.5M views in 90 days "
        "working on revshare or CPA. Want 3 demo reels?"
    )
    score_a, tier_a = compute_icp_quality(
        website="https://acme.ai",
        their_angle="AI copilot for B2B engineering teams automating PR reviews",
        username="@acme_founder",
        category="B2B SaaS / AI",
        step1_text=vlad_draft,
    )
    assert score_a == 100
    assert tier_a == "A"

    score_b, tier_b = compute_icp_quality(
        website="acme.io",
        their_angle="Crypto analytics platform for institutional traders",
        username="crypto_ceo",
        category="Other",
        step1_text="Hello, quick question about your growth",
    )
    assert score_b == 70
    assert tier_b == "B"

    score_c, tier_c = compute_icp_quality(
        website="",
        their_angle="Short",
        username="",
        category="Unknown",
        step1_text="Hi",
    )
    assert score_c == 0
    assert tier_c == "C"


def test_read_outbox_max_id_and_telethon_raw_update(tmp_path):
    conn = _make_conn(tmp_path)
    acc_id = _seed_account(conn)
    camp_id = _seed_campaign(conn, acc_id, "B2B Outreach")
    now = utc_now()

    # Recipient 1: sent msg_id=105, dialog read_outbox_max_id=100 -> sent_unread (sent_not_opened)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 5001, 'unread_founder', 'Alex', 'AlphaSaaS', 'SaaS', 'Hi Alpha', 1, 'in_progress', ?, ?, ?)
        """,
        (camp_id, now, now, now),
    )
    d1 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, read_outbox_max_id, created_at, updated_at
        ) VALUES (?, 'user', 5001, 'unread_founder', 'Alex', 100, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 105, 'out', 'Hi Alpha', ?, ?, ?)
        """,
        (d1, now, now, now),
    )

    # Recipient 2: sent msg_id=210, dialog read_outbox_max_id=215 -> read_no_reply
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 5002, 'read_founder', 'Maria', 'BetaAI', 'AI', 'Hi Beta', 3, 'completed', ?, ?, ?)
        """,
        (camp_id, now, now, now),
    )
    d2 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, read_outbox_max_id, created_at, updated_at
        ) VALUES (?, 'user', 5002, 'read_founder', 'Maria', 215, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 210, 'out', 'Final touch Beta', ?, ?, ?)
        """,
        (d2, now, now, now),
    )
    conn.commit()

    funnel_before = get_outreach_funnel(conn)
    by_user = {row["username"]: row for row in funnel_before["leads"]}
    assert by_user["unread_founder"]["stage"] == "sent_unread"
    assert by_user["unread_founder"]["reason_code"] == "sent_not_opened"
    assert "ещё не открыл" in by_user["unread_founder"]["reason_label"]

    assert by_user["read_founder"]["stage"] == "read_no_reply"
    assert by_user["read_founder"]["reason_code"] == "read_sequence_exhausted"
    assert "3 касания" in by_user["read_founder"]["reason_label"]

    # Now simulate live UpdateReadHistoryOutbox advancing 5001's read_outbox_max_id to 105
    updated = apply_read_outbox_update(conn, acc_id, peer_id=5001, max_id=105)
    assert updated == 1

    funnel_after = get_outreach_funnel(conn)
    by_user_after = {row["username"]: row for row in funnel_after["leads"]}
    assert by_user_after["unread_founder"]["stage"] == "read_no_reply"
    assert by_user_after["unread_founder"]["read_at"] is not None
    assert by_user_after["unread_founder"]["reason_code"] == "read_awaiting_followup"

    # Also verify TelethonGateway.watch_private dispatches UpdateReadHistoryOutbox via events.Raw
    gw = TelethonGateway()
    read_events = []

    class _DummyClient:
        def __init__(self):
            self.handlers = []

        def add_event_handler(self, cb, _ev):
            self.handlers.append(cb)

        def remove_event_handler(self, cb):
            self.handlers.remove(cb)

        async def catch_up(self):
            return None

        async def run_until_disconnected(self):
            await asyncio.Future()

    dummy_client = _DummyClient()

    class UpdateReadHistoryOutbox:
        def __init__(self, user_id: int, max_id: int):
            self.peer = SimpleNamespace(user_id=user_id)
            self.max_id = max_id

    async def _run_watch():
        async def _open(_s):
            return dummy_client, False

        gw._open = _open
        task = asyncio.create_task(
            gw.watch_private("sess", lambda _m: None, on_read=lambda p: read_events.append(p))
        )
        await asyncio.wait_for(listening_event(gw, "sess").wait(), timeout=2)
        assert len(dummy_client.handlers) == 2
        # Trigger raw handler with UpdateReadHistoryOutbox
        from telethon import tl

        real_ev = tl.types.UpdateReadHistoryOutbox(
            peer=tl.types.PeerUser(user_id=5001),
            max_id=120,
            pts=1,
            pts_count=1,
        )
        await dummy_client.handlers[1](real_ev)
        assert read_events == [{"telegram_user_id": 5001, "read_outbox_max_id": 120}]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_watch())


def test_funnel_ssot_variants_diagnoses_and_vacancy_exclusion(tmp_path):
    conn = _make_conn(tmp_path)
    acc_id = _seed_account(conn)
    b2b_cid = _seed_campaign(conn, acc_id, "Leadgen B2B Autopilot")
    vac_cid = _seed_campaign(conn, acc_id, "TG Вакансии")
    today = utc_today()
    now = f"{today}T10:00:00"
    later = f"{today}T11:00:00"

    # Vacancy recipient — MUST be excluded by default (Cyrillic 'Вакансии')!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 8001, 'hr_lead', 'HR', 'JobCorp', 'HR', 'Отклик на вакансию', 0, 'pending', ?, ?)
        """,
        (vac_cid, now, now),
    )

    def _add_b2b_rec(
        tg_id: int,
        uname: str,
        company: str,
        step1: str,
        *,
        step: int = 0,
        status: str = "pending",
        variant_key: str | None = None,
        website: str = "https://product.io",
        their_angle: str = "Automated B2B analytics platform for SaaS revenue teams",
        category: str = "SaaS",
        inbound_text: str | None = None,
        lead_status: str = "new",
        with_calendar: bool = False,
        read_max_id: int = 0,
    ) -> int:
        lead_cur = conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, company, niche, url,
                lead_source, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'TG Ad Spy', ?, ?, ?)
            """,
            (acc_id, tg_id, uname, company, company, category, website, lead_status, now, now),
        )
        lid = int(lead_cur.lastrowid)
        if with_calendar:
            conn.execute(
                """
                INSERT INTO calendar_events (title, event_type, start_at, lead_id, status, created_at, updated_at)
                VALUES ('Demo Call', 'call', ?, ?, 'scheduled', ?, ?)
                """,
                (later, lid, now, now),
            )
        rcur = conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name, company,
                website, category, their_angle, step1_text, variant_key, added_date,
                current_step, status, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                b2b_cid,
                lid,
                tg_id,
                uname,
                company,
                company,
                website,
                category,
                their_angle,
                step1,
                variant_key,
                today,
                step,
                status,
                now if step >= 1 else None,
                now,
                now,
            ),
        )
        if step >= 1 or inbound_text is not None:
            dcur = conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, lead_id,
                    read_outbox_max_id, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
                """,
                (acc_id, tg_id, uname, company, lid, read_max_id, now, now),
            )
            did = int(dcur.lastrowid)
            conn.execute(
                """
                INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
                VALUES (?, 10, 'out', ?, ?, ?, ?)
                """,
                (did, step1, now, now, now),
            )
            if inbound_text is not None:
                conn.execute(
                    """
                    INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
                    VALUES (?, 11, 'in', ?, ?, ?, ?)
                    """,
                    (did, inbound_text, later, later, later),
                )
        return int(rcur.lastrowid)

    # 1. Queued (sonnet5_vlad_v1)
    _add_b2b_rec(
        6001,
        "queued_sonnet",
        "SonnetLabs",
        "Hey! SoftUGC drove 183.5M views on revshare or CPA.",
        step=0,
        status="pending",
        variant_key="sonnet5_vlad_v1",
    )
    # 2. Sent unread with MTProto read_outbox_max_id == 0 (vlad_softugc_v1)
    _add_b2b_rec(
        6002,
        "unread_vlad",
        "VladSoft",
        "Hey! We built SoftUGC and work on revshare or CPA (183.5M views).",
        step=1,
        status="in_progress",
        read_max_id=0,
    )
    # 3. Read no reply (legacy_creatoros)
    _add_b2b_rec(
        6003,
        "ghosted_creator",
        "OldCreator",
        "Hi! CreatorOS helps scale short-form clips.",
        step=2,
        status="in_progress",
        read_max_id=15,
    )
    # 4. Replied — declined ("Not interested" MUST NOT false-trigger positive 'interested'!)
    _add_b2b_rec(
        6004,
        "declined_lead",
        "PassCo",
        "If your user growth has hit a ceiling, let us help.",
        step=1,
        status="replied",
        inbound_text="Not interested right now, no thanks.",
    )
    # 5. Replied — pricing question
    _add_b2b_rec(
        6005,
        "price_lead",
        "BudgetApp",
        "Custom intro about organic videos",
        step=1,
        status="replied",
        inbound_text="Сколько стоит такой пакет и какой прайс?",
    )
    # 6. Replied — awaiting demo / materials
    _add_b2b_rec(
        6006,
        "materials_lead",
        "DeckCo",
        "Custom intro about organic videos",
        step=1,
        status="replied",
        inbound_text="Пришлите портфолио и кейсы почитать",
    )
    # 7. Replied — stalled generic reply
    _add_b2b_rec(
        6007,
        "stalled_lead",
        "NeutralCo",
        "Custom intro about organic videos",
        step=1,
        status="replied",
        inbound_text="Привет, мы сейчас в разъездах до конца месяца",
    )
    # 8. Positive via inbound reply ("Sure, send the demo reels")
    _add_b2b_rec(
        6008,
        "positive_reply_lead",
        "HotAI",
        "Hey! SoftUGC drove 183.5M views on revshare or CPA.",
        step=1,
        status="replied",
        variant_key="sonnet5_vlad_v1",
        inbound_text="Sure, send the 3 demo reels, let's talk!",
    )
    # 9. Positive via calendar call scheduled
    _add_b2b_rec(
        6009,
        "calendar_call_lead",
        "BookedDeFi",
        "Hey! SoftUGC drove 183.5M views on revshare or CPA.",
        step=1,
        status="in_progress",
        variant_key="sonnet5_vlad_v1",
        with_calendar=True,
    )
    conn.commit()

    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        res = client.get("/api/analytics/outreach-funnel")
        assert res.status_code == 200
        payload = res.json()

        summary = payload["summary"]
        # Strict SSOT check: 9 B2B leads (vacancy campaign excluded!)
        assert summary["total_leads"] == 9
        assert sum(summary["stages"].values()) == summary["total_leads"]
        assert summary["stages"] == {
            "queued": 1,
            "sent_unread": 1,
            "read_no_reply": 1,
            "replied_no_call": 4,
            "positive_or_call": 2,
        }
        assert summary["contacted_total"] == 8
        assert summary["read_total"] == 7
        assert summary["replied_total"] == 6
        assert summary["positive_or_call_total"] == 2
        assert summary["added_today"] == 9
        assert sum(summary["quality_tiers"].values()) == 9

        # Check lead-level diagnoses & variants
        by_uname = {r["username"]: r for r in payload["leads"]}
        assert "hr_lead" not in by_uname

        assert by_uname["unread_vlad"]["reason_code"] == "unread_mtproto_pending"
        assert "синхронизации MTProto" in by_uname["unread_vlad"]["reason_label"]
        assert by_uname["unread_vlad"]["variant_key"] == "vlad_softugc_v1"

        assert by_uname["ghosted_creator"]["variant_key"] == "legacy_creatoros"
        assert by_uname["ghosted_creator"]["stage"] == "read_no_reply"

        assert by_uname["declined_lead"]["stage"] == "replied_no_call"
        assert by_uname["declined_lead"]["reason_code"] == "declined"
        assert by_uname["declined_lead"]["variant_key"] == "legacy_ceiling_hook"

        assert by_uname["price_lead"]["reason_code"] == "pricing_question"
        assert by_uname["materials_lead"]["reason_code"] == "awaiting_demo"
        assert by_uname["stalled_lead"]["reason_code"] == "stalled_after_reply"

        assert by_uname["positive_reply_lead"]["stage"] == "positive_or_call"
        assert by_uname["positive_reply_lead"]["dialog_id"] is not None
        assert by_uname["calendar_call_lead"]["stage"] == "positive_or_call"

        # Variant comparison stats
        vstats = {v["variant_key"]: v for v in payload["variant_stats"]}
        assert vstats["sonnet5_vlad_v1"]["label"] == "SoftUGC · Sonnet 5 (новый)"
        assert vstats["sonnet5_vlad_v1"]["total_leads"] == 3
        assert vstats["sonnet5_vlad_v1"]["queued"] == 1
        assert vstats["sonnet5_vlad_v1"]["sent"] == 2
        assert vstats["sonnet5_vlad_v1"]["positive_or_call"] == 2
        assert vstats["sonnet5_vlad_v1"]["positive_rate_pct"] == 100.0

        # Filtering by stage, variant, search q, and explicit vacancy campaign_id
        f_pos = client.get("/api/analytics/outreach-funnel?stage=positive_or_call").json()
        assert len(f_pos["leads"]) == 2
        assert f_pos["summary"]["total_leads"] == 9  # SSOT summary stays consistent

        f_var = client.get("/api/analytics/outreach-funnel?variant=legacy_ceiling_hook").json()
        assert [r["username"] for r in f_var["leads"]] == ["declined_lead"]

        f_q = client.get("/api/analytics/outreach-funnel?q=BudgetApp").json()
        assert [r["username"] for r in f_q["leads"]] == ["price_lead"]

        f_vac = client.get(f"/api/analytics/outreach-funnel?campaign_id={vac_cid}").json()
        assert f_vac["summary"]["total_leads"] == 1
        assert f_vac["leads"][0]["username"] == "hr_lead"

        # UI navigation & assets check
        html = client.get("/").text
        assert 'href="#funnel"' in html
        assert "Воронка и Лидген" in html
        assert 'id="funnel"' in html
        js = client.get("/static/app.js").text
        assert "/api/analytics/outreach-funnel" in js


def test_live_watch_and_sync_dialog_read_receipts(tmp_path):
    conn = _make_conn(tmp_path)
    sess = "data/sessions/live_read.session"
    acc_id = _seed_account(conn, session_path=sess)
    camp_id = _seed_campaign(conn, acc_id, "Live Read Test")
    now = utc_now()

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 7701, 'live_peer', 'Pavel', 'LiveCo', 'Ping', 1, 'in_progress', ?, ?, ?)
        """,
        (camp_id, now, now, now),
    )
    did = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, created_at, updated_at
        ) VALUES (?, 'user', 7701, 'live_peer', 'Pavel', ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 55, 'out', 'Ping', ?, ?, ?)
        """,
        (did, now, now, now),
    )
    conn.commit()

    gw = FakeGateway(
        dialogs=[
            {
                "telegram_user_id": 7701,
                "username": "live_peer",
                "name": "Pavel",
                "read_outbox_max_id": 40,
            }
        ]
    )
    app = create_app(conn=conn, gateway=gw)

    async def _exercise_live_read():
        await start_account_watch(app, acc_id)
        try:
            # Before read receipt >= 55 -> sent_unread
            assert get_outreach_funnel(conn)["leads"][0]["stage"] == "sent_unread"
            gw.push_read_outbox(sess, peer_id=7701, max_id=55)
            await asyncio.sleep(0.05)
            lead_row = get_outreach_funnel(conn)["leads"][0]
            assert lead_row["stage"] == "read_no_reply"
            assert lead_row["read_at"] is not None
        finally:
            for t in list(app.state.inbox_tasks.values()):
                t.cancel()
            await asyncio.gather(*app.state.inbox_tasks.values(), return_exceptions=True)

    asyncio.run(_exercise_live_read())

    # Also verify sync_dialog_read_receipts helper directly
    sync_cnt = sync_dialog_read_receipts(
        conn, acc_id, [{"telegram_user_id": 7701, "read_outbox_max_id": 60}]
    )
    assert sync_cnt == 0  # already stamped read_at above
    d_row = conn.execute("SELECT read_outbox_max_id FROM dialogs WHERE id = ?", (did,)).fetchone()
    assert int(d_row["read_outbox_max_id"]) == 60


def test_sync_recipient_drafts_requires_auth_and_protects_contacted_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", "test-mcp-secret-999")
    conn = connect(tmp_path / "draft_sync.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    now = utc_now()

    acc_id = _seed_account(conn)
    cid = _seed_campaign(conn, acc_id, name="SoftUGC — База Все2")

    conn.execute(
        """
        INSERT INTO campaign_recipients (campaign_id, username, company, current_step, status, created_at, updated_at)
        VALUES (?, 'fresh_founder', 'FreshCo', 0, 'pending', ?, ?)
        """,
        (cid, now, now),
    )
    fresh_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute(
        """
        INSERT INTO campaign_recipients (campaign_id, username, company, step1_text, current_step, status, last_sent_at, created_at, updated_at)
        VALUES (?, 'already_replied', 'OldCo', 'Original sent text', 1, 'replied', ?, ?, ?)
        """,
        (cid, now, now, now),
    )
    old_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()

    app = create_app(conn=conn)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        unauth = client.post(
            "/api/campaigns/sync-recipient-drafts",
            json={"items": [{"recipient_id": fresh_id, "step1_text": "Hey,\n\nTest"}]},
        )
        assert unauth.status_code == 401

        auth_res = client.post(
            "/api/campaigns/sync-recipient-drafts",
            headers={"Authorization": "Bearer test-mcp-secret-999"},
            json={
                "items": [
                    {
                        "recipient_id": fresh_id,
                        "username": "fresh_founder",
                        "company": "FreshCo",
                        "step1_text": "Hey,\n\nSaw FreshCo.\n\nWe run SoftUGC: 183.5M views.\n\nWe work on revshare or CPA and you only cover the production costs. Want to see 3 example reels for your brand, to check how it could work for you?",
                        "audience": "buyers",
                        "variant_key": "sonnet5_vlad_v1",
                        "pain_angle": "ecom_creative_fatigue",
                    },
                    {
                        "recipient_id": old_id,
                        "username": "already_replied",
                        "step1_text": "SHOULD NOT OVERWRITE",
                    },
                ]
            },
        )
        assert auth_res.status_code == 200
        body = auth_res.json()
        assert body["updated"] == 1
        assert body["skipped"] == 1

    fresh_row = conn.execute(
        "SELECT step1_text, step2_text, variant_key, pain_angle, status FROM campaign_recipients WHERE id = ?",
        (fresh_id,),
    ).fetchone()
    assert "SoftUGC" in fresh_row["step1_text"]
    assert fresh_row["step2_text"] is None
    assert fresh_row["variant_key"] == "sonnet5_vlad_v1"
    assert fresh_row["pain_angle"] == "ecom_creative_fatigue"

    old_row = conn.execute(
        "SELECT step1_text, status FROM campaign_recipients WHERE id = ?",
        (old_id,),
    ).fetchone()
    assert old_row["step1_text"] == "Original sent text"
    assert old_row["status"] == "replied"
