import asyncio
import base64
import gzip
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.accounts import check_all_accounts, purge_dead_accounts
import app.cloud_persistence as cp
from app.db import (
    CANONICAL_STEP3_IMAGE_URL,
    apply_canonical_vlad_outreach_v4,
    connect,
    init_db,
    resolve_outreach_media,
    utc_now,
)
from app.main import create_app
from app.mcp_server import handle_jsonrpc_message
from app.outreach import (
    create_campaign,
    heal_transient_failed_recipients,
    is_offline_non_icp_contact,
    render_template,
    run_campaign_batch,
)
from leadgen.outreach_copy import (
    REVSHARE_OFFER_TEXT,
    SOFTUGC_PITCH_TEXT,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_TEXT,
    clean_brand_name,
    first_touch,
    followup_one,
    followup_two,
    infer_lead_audience,
    upgrade_to_vlad_step1,
)


def test_vlad_canonical_sequence_and_all_five_audiences():
    # 1. Agency -> Vlad's exact agency pain + SoftUGC + revshare/CPA 3 demo reels
    s1_agency = first_touch(
        "Crypto Executives",
        "Executive search and BD advisory for Web3 teams",
        category="Агентство",
    )
    assert s1_agency.startswith("Hey,\n\nSaw Crypto Executives. You're probably running into the same thing most agencies hit:")
    assert SOFTUGC_PITCH_TEXT in s1_agency
    assert s1_agency.endswith(REVSHARE_OFFER_TEXT)
    assert "(generated)" not in s1_agency
    assert "—" not in s1_agency and "–" not in s1_agency

    # 2. All 5 {audience} buckets resolve identically in leadgen and app/outreach
    cases = [
        ({"category": "Агентство", "company": "Crypto Executives", "their_angle": "Web3 BD agency"}, "buyers"),
        ({"category": "EdTech", "company": "CohortLab", "their_angle": "Online coding bootcamp for engineers"}, "students"),
        ({"category": "SaaS", "company": "PulseNote AI", "their_angle": "AI meeting copilot for B2B teams"}, "users"),
        ({"category": "Gaming", "company": "Gielinor Survivors", "their_angle": "Browser roguelike multiplayer game"}, "players"),
        ({"category": "Podcast", "company": "Founders FM", "their_angle": "Weekly startup podcast on Spotify"}, "listeners"),
    ]
    for rec, expected_aud in cases:
        assert infer_lead_audience(
            category=rec["category"],
            company=rec["company"],
            their_angle=rec["their_angle"],
        ) == expected_aud
        s2_leadgen = followup_one(rec["company"], rec["their_angle"], category=rec["category"])
        s2_rendered = render_template(VLAD_STEP2_TEMPLATE, rec)
        assert s2_leadgen == s2_rendered
        assert f"(which equals highly qualified {expected_aud})" in s2_rendered
        assert "So if you’re down just lmk if you wanna see the demo reels" in s2_rendered

    # 3. Step 3 is identical for every lead
    assert followup_two("Crypto Executives") == VLAD_STEP3_TEXT
    assert VLAD_STEP3_TEXT == "You’re doing me dirty mate... :((("


def test_clean_brand_name_and_upgrade_to_vlad_step1_guards():
    assert clean_brand_name("Show HN: LoopTranscribe", "https://looptranscribe.com") == "LoopTranscribe"
    assert clean_brand_name("7. Calendarly (YC W26)", "https://calendarly.ai") == "Calendarly"
    assert clean_brand_name("dontgetrekt", "", "DontGetRekt protects DeFi wallets") == "DontGetRekt"
    assert clean_brand_name("311wrapped", "") == "311 Wrapped"
    assert clean_brand_name("Jeremy | OpenEden", "https://openeden.com") == "OpenEden"
    # Person-only Cyrillic or Latin names without a brand do NOT produce "Saw Валерия"
    assert clean_brand_name("Валерия", "", "") == ""
    assert clean_brand_name("Olya M", "", "") == ""
    assert clean_brand_name("Clayton V Block", "") == ""
    assert clean_brand_name("[Passion Project] Sports Game!", "") == "Sports Game"
    msg_sports = first_touch("[Passion Project] Sports Game!", "", category="Gaming")
    assert "Saw Sports Game." in msg_sports
    assert "!." not in msg_sports
    msg_person_only = first_touch("Валерия", "", category="SaaS")
    assert "Валерия" not in msg_person_only
    assert msg_person_only.startswith("Hey,\n\nCame across what you're building.")

    # Genuine custom LLM draft keeps individualized pain & pitch, upgrades CTA to REVSHARE_OFFER_TEXT
    custom_sheet_draft = (
        "Hey,\n\n"
        "Saw Floopi. Getting consistent merchant signups in crypto payments usually hits a wall "
        "once warm intros run dry and paid ads get restricted.\n\n"
        "We run CreatorOS — an AI short-form video engine and owned account network that drove "
        "183.5M views in 90 days with zero shoots, 13-40x cheaper than clippers. "
        "We work on revshare/CPA, so there's no upfront risk. Want me to walk you through how this could work for you?"
    )
    upgraded = upgrade_to_vlad_step1(custom_sheet_draft, company="Floopi", category="Финтех")
    assert "Saw Floopi. Getting consistent merchant signups" in upgraded
    assert "CreatorOS" not in upgraded
    assert "SoftUGC" in upgraded
    assert "—" not in upgraded
    assert "no upfront risk" not in upgraded
    assert upgraded.endswith(REVSHARE_OFFER_TEXT)

    # Compound ", and we work on revshare..." trims cleanly with a period (no dangling ", and")
    compound_draft = (
        "Hey Maxim,\n\n"
        "Saw Axonnic. Getting manufacturers to adopt robotics for dexterous tasks is tough when you have to prove ROI.\n\n"
        "We run SoftUGC. Organic distribution using AI video production and a network of owned accounts. "
        "We pushed 183.5M views in 90 days with zero live shoots, traffic runs 13 to 40x cheaper than standard clippers, "
        "and we work on revshare so you only pay for what converts. Want me to show how this would work for you?"
    )
    upgraded_compound = upgrade_to_vlad_step1(compound_draft, company="Axonnic", category="SaaS")
    assert upgraded_compound.startswith("Hey,\n\nSaw Axonnic.")
    assert "standard clippers." in upgraded_compound
    assert "clippers, and" not in upgraded_compound
    assert upgraded_compound.endswith(REVSHARE_OFFER_TEXT)

    pawl_draft = (
        "Hey,\n\nSaw pawl on your site. Small Mac utilities pick up a loyal user base fast.\n\n"
        "We run SoftUGC. Pushed 183.5M views in 90 days, no shoots, traffic running 13 to 40x cheaper than standard clipper services. "
        "We do revshare so there's no upfront risk on your end."
    )
    upgraded_pawl = upgrade_to_vlad_step1(pawl_draft, company="7. pawl", category="SaaS")
    assert "We do revshare" not in upgraded_pawl
    assert upgraded_pawl.endswith(REVSHARE_OFFER_TEXT)
    assert "(which equals highly qualified users)" in render_template(
        VLAD_STEP2_TEMPLATE,
        {"company": "7. pawl", "category": "SaaS", "their_angle": "Мини-таймер для фокуса с игровой механикой"},
    )

    # Robot training noise ("обучения роботов") stays 'users', not 'students'
    rec_robot = {
        "company": "Axonnic",
        "category": "SaaS",
        "their_angle": "Записывает движения рабочих и использует эти данные для обучения роботов.",
    }
    assert infer_lead_audience(**rec_robot) == "users"
    assert "(which equals highly qualified users)" in render_template(VLAD_STEP2_TEMPLATE, rec_robot)

    # Puzzle game gets game studio pain in Step 1 AND 'players' in Step 2
    s1_puzzle = first_touch("crux", "A free daily colour sort puzzle with endless levels", category="AI", site="crux.quest")
    assert "most game studios hit" in s1_puzzle
    assert infer_lead_audience(category="AI", company="crux", their_angle="A free daily colour sort puzzle") == "players"

    # Leaked LLM refusal or raw-bio template gets completely replaced
    refusal = 'I can\'t write this one. The "what they do" field is just "Just a moment..." which is a Cloudflare interstitial.'
    fixed_refusal = upgrade_to_vlad_step1(refusal, company="Monetor", category="SaaS")
    assert "can't write" not in fixed_refusal
    assert "Cloudflare" not in fixed_refusal
    assert "Saw Monetor." in fixed_refusal
    assert fixed_refusal.endswith(REVSHARE_OFFER_TEXT)

    assert is_offline_non_icp_contact(username="janmichalek", their_angle=refusal) is True
    assert is_offline_non_icp_contact(username="tgsoft_oleg", website="https://tgcenterapp.ru") is True


def test_kermit_media_static_fallback_and_cloud_roundtrip(tmp_path, monkeypatch):
    empty_media_dir = tmp_path / "empty_outreach_media"
    monkeypatch.setattr("app.db.OUTREACH_MEDIA_DIR", empty_media_dir)
    monkeypatch.setattr(cp, "OUTREACH_MEDIA_DIR", empty_media_dir)

    # 1. Even when data/outreach_media does NOT exist (Render container restart), Kermit resolves & serves 200 JPEG
    resolved = resolve_outreach_media(CANONICAL_STEP3_IMAGE_URL)
    assert isinstance(resolved, Path)
    assert resolved.exists()
    assert resolved.read_bytes()[:3] == b"\xff\xd8\xff"

    conn = connect(tmp_path / "media_test.db")
    init_db(conn)
    client = TestClient(create_app(conn=conn))
    resp = client.get(CANONICAL_STEP3_IMAGE_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content[:3] == b"\xff\xd8\xff"

    # 2. Custom uploaded outreach media backs up to R2 bundle and restores on fresh disk
    empty_media_dir.mkdir(parents=True, exist_ok=True)
    sample_bytes = resolved.read_bytes()
    (empty_media_dir / "custom123.jpg").write_bytes(sample_bytes)

    stored_r2: dict[str, bytes] = {}
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda _conn=None: {"account_id": "acc", "access_key": "k", "secret_key": "s", "bucket": "b"},
    )

    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def put(self, url, content=b"", headers=None):
            stored_r2["payload"] = content
            return type("R", (), {"status_code": 200, "text": ""})()

        def get(self, url, headers=None):
            payload = stored_r2.get("payload", b"")
            return type("R", (), {"status_code": 200 if payload else 404, "content": payload})()

    monkeypatch.setattr(cp.httpx, "Client", _FakeHttpxClient)
    b_res = cp.backup_to_cloud(conn, tmp_path / "media_test.db")
    assert b_res["ok"] is True
    raw_bundle = json.loads(gzip.decompress(stored_r2["payload"]).decode("utf-8"))
    assert "custom123.jpg" in raw_bundle["outreach_media"]

    # Wipe media dir and restore
    (empty_media_dir / "custom123.jpg").unlink()
    assert not (empty_media_dir / "custom123.jpg").exists()
    r_res = cp.restore_from_cloud(tmp_path / "restored.db", force=True)
    assert r_res["ok"] is True
    assert r_res["restored_media"] == 1
    assert (empty_media_dir / "custom123.jpg").read_bytes() == sample_bytes


def test_spambot_freezes_account_and_retries_step1_on_next_healthy_account(tmp_path):
    conn = connect(tmp_path / "spambot_test.db")
    init_db(conn)
    now = utc_now()

    acc1 = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, updated_at, daily_limit, sent_today, provider
            ) VALUES ('+10000000001', 'data/sessions/s1.session', 'connected', 1001, 'spam_acc', 'SpamAcc', ?, 'active', ?, 20, 0, 'telegram')
            """,
            (now, now),
        ).lastrowid
    )
    acc2 = int(
        conn.execute(
            """
            INSERT INTO accounts (
                phone, session_path, status, telegram_user_id,
                telegram_username, telegram_name, created_at,
                health, updated_at, daily_limit, sent_today, provider
            ) VALUES ('+10000000002', 'data/sessions/s2.session', 'connected', 1002, 'healthy_acc', 'HealthyAcc', ?, 'active', ?, 20, 0, 'telegram')
            """,
            (now, now),
        ).lastrowid
    )
    conn.commit()

    camp = create_campaign(
        conn,
        name="B2B SoftUGC Test",
        account_ids=[acc1, acc2],
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
            {"step_order": 3, "delay_hours": 24, "template_text": VLAD_STEP3_TEXT, "image_url": CANONICAL_STEP3_IMAGE_URL},
        ],
    )
    cid = int(camp["id"])
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, error_text, created_at, updated_at
        ) VALUES (?, 55501, 'target_founder', 'Target Founder', 'TargetAI', 'SaaS', ?, 0, 'failed',
                  'You cannot message non-contacts (see @SpamBot)', ?, ?)
        """,
        (cid, first_touch("TargetAI", "AI SaaS tool", category="SaaS"), now, now),
    )
    conn.commit()

    # 1. Previously burned @SpamBot lead is healed back to 'pending'
    healed = heal_transient_failed_recipients(conn, cid)
    assert healed == 1

    calls = []
    closed_sessions = []
    searched_sessions = []

    class _SpamThenHealthyGateway:
        async def _close(self, session_path):
            closed_sessions.append(str(session_path))

        async def search_dialogs(self, session_path, query, limit=1):
            searched_sessions.append((str(session_path), query))
            return [{"telegram_user_id": 55501, "username": "target_founder"}]

        async def session_health(self, session_path):
            return {"health": "active", "last_error": None}

        async def send_message(self, session_path, peer_target, text, reply_to=None, image_path=None):
            calls.append((str(session_path), peer_target, text, image_path))
            if "s1.session" in str(session_path):
                raise RuntimeError("PeerFloodError: You cannot message non-contacts, check @SpamBot for details")
            return {"telegram_message_id": 9101, "sent_at": now}

    gw = _SpamThenHealthyGateway()
    res = asyncio.run(run_campaign_batch(conn, cid, gateway=gw, max_send=1))
    assert res["sent"] == 1
    assert [c[0] for c in calls] == ["data/sessions/s1.session", "data/sessions/s2.session"]
    # @SpamBot account must NOT have its live Telethon listener closed, and acc2 resolves @username on rotation
    assert closed_sessions == []
    assert ("data/sessions/s2.session", "@target_founder") in searched_sessions
    # Empty orphan dialog on acc1 was cleaned up
    assert conn.execute("SELECT COUNT(*) FROM dialogs WHERE account_id = ?", (acc1,)).fetchone()[0] == 0

    acc1_health = conn.execute("SELECT health FROM accounts WHERE id = ?", (acc1,)).fetchone()["health"]
    acc2_health = conn.execute("SELECT health FROM accounts WHERE id = ?", (acc2,)).fetchone()["health"]
    rec_row = conn.execute("SELECT status, current_step, assigned_account_id, error_text FROM campaign_recipients WHERE campaign_id = ?", (cid,)).fetchone()

    assert acc1_health == "frozen"
    assert acc2_health == "active"
    assert rec_row["status"] == "in_progress"
    assert rec_row["current_step"] == 1
    assert rec_row["assigned_account_id"] == acc2
    assert rec_row["error_text"] is None

    # Bulk check_all_accounts and purge_dead_accounts preserve @SpamBot-restricted account
    asyncio.run(check_all_accounts(conn, gateway=gw))
    assert conn.execute("SELECT health FROM accounts WHERE id = ?", (acc1,)).fetchone()["health"] == "frozen"
    purge_res = purge_dead_accounts(conn, gateway=gw)
    assert acc1 not in purge_res["deleted_ids"]


def test_apply_canonical_vlad_outreach_v4_migrates_existing_campaigns_without_unpausing(tmp_path):
    conn = connect(tmp_path / "v4_migration.db")
    init_db(conn)
    now = utc_now()

    # Simulate pre-existing legacy Campaign #4 (72h + {followup_2}), paused Campaign #5, and TG Вакансии
    c4 = create_campaign(
        conn,
        name="Leadgen Auto (TG)",
        status="draft",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
            {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
        ],
    )["id"]
    c5 = create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 050 лидов)",
        status="paused",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE.replace("\n\n", "\u2028\n\n")},
            {"step_order": 3, "delay_hours": 24, "template_text": VLAD_STEP3_TEXT, "image_url": ""},
        ],
    )["id"]
    cvac = create_campaign(
        conn,
        name="TG Вакансии",
        status="draft",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{first_touch}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
            {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
        ],
    )["id"]

    # Insert: 1 phantom row in C4, 1 real TG row in C4, 1 @SpamBot failed row in C5, 1 junk row in C5, 1 contacted row in C5
    conn.execute(
        """
        INSERT INTO campaign_recipients (campaign_id, telegram_user_id, username, company, category, step1_text, step2_text, current_step, status, created_at, updated_at)
        VALUES (?, 900372, NULL, 'Reific', 'AI', 'Hey, saw Reific — running reels?', 'Old step2', 0, 'pending', ?, ?),
               (?, 777001, 'adalat_ceo', 'Adalat AI', 'SaaS', 'Hey, saw Adalat AI — running reels?', 'Old step2', 0, 'pending', ?, ?),
               (?, 777002, 'openeden_j', 'Jeremy | OpenEden', 'DeFi', 'Scaling reach for RWA protocol is hard.', NULL, 0, 'failed', ?, ?),
               (?, 777003, 'tgsoft_oleg', 'Павел Орлов', 'SaaS', 'Пишу про софт tgcenterapp.ru', NULL, 0, 'pending', ?, ?),
               (?, 777004, 'already_sent', 'OldCorp', 'SaaS', 'Legacy CreatorOS text already sent', NULL, 1, 'completed', ?, ?)
        """,
        (c4, now, now, c4, now, now, c5, now, now, c5, now, now, c5, now, now),
    )
    conn.execute(
        "UPDATE campaign_recipients SET error_text = 'You cannot message non-contacts (@SpamBot)' WHERE username = 'openeden_j'"
    )
    conn.execute("DELETE FROM kv_settings WHERE key = 'canonical_vlad_outreach_v4'")
    conn.commit()

    stats = apply_canonical_vlad_outreach_v4(conn)
    assert stats["updated_steps_campaigns"] >= 2
    assert stats["healed_spambot"] >= 1
    assert stats["deleted_non_icp_or_phantom"] >= 2
    assert stats["upgraded_drafts"] >= 2

    # Paused Campaign #5 MUST stay paused, and already-contacted completed row MUST NOT be touched
    assert conn.execute("SELECT status FROM campaigns WHERE id = ?", (c5,)).fetchone()["status"] == "paused"
    sent_row = conn.execute("SELECT status, current_step, step1_text FROM campaign_recipients WHERE username = 'already_sent'").fetchone()
    assert sent_row["status"] == "completed"
    assert sent_row["step1_text"] == "Legacy CreatorOS text already sent"

    # Healed OpenEden row has canonical Vlad Step 1 & cleaned brand
    oe = conn.execute("SELECT status, company, step1_text FROM campaign_recipients WHERE username = 'openeden_j'").fetchone()
    assert oe["status"] == "pending"
    assert oe["company"] == "OpenEden"
    assert oe["step1_text"].startswith("Hey,\n\nSaw OpenEden.")
    assert oe["step1_text"].endswith(REVSHARE_OFFER_TEXT)

    # Campaign #4 steps upgraded to 24h + Kermit image, phantom Reific deleted, Adalat upgraded
    c4_steps = conn.execute("SELECT step_order, delay_hours, image_url FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order", (c4,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in c4_steps] == [(1, 0, None), (2, 24, None), (3, 24, CANONICAL_STEP3_IMAGE_URL)]
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ?", (c4,)).fetchone()[0] == 1

    # TG Вакансии remains completely untouched on 72h {followup_2}
    vac_steps = conn.execute("SELECT delay_hours, template_text FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order", (cvac,)).fetchall()
    assert [(r[0], r[1]) for r in vac_steps] == [(0, "{first_touch}"), (24, "{followup_1}"), (72, "{followup_2}")]

    # Greeting "Hey Alex," normalizes to "Hey," even when REVSHARE_OFFER_TEXT is already present
    already_offered = (
        "Hey Alex,\n\nSawAcme. Scaling outbound is tough.\n\n"
        f"{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    )
    assert upgrade_to_vlad_step1(already_offered, company="Acme", category="SaaS").startswith("Hey,\n\n")


def test_ingest_discovered_leads_skips_phantom_email_only_from_campaign_recipients(tmp_path):
    conn = connect(tmp_path / "ingest_phantom_guard.db")
    init_db(conn)

    resp = handle_jsonrpc_message(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ingest_discovered_leads",
                "arguments": {
                    "campaign_name": "Leadgen Auto (TG)",
                    "enroll_in_campaign": True,
                    "leads": [
                        {
                            "company": "Reific",
                            "email": "founder@reific.co",
                            "website": "https://reific.co",
                            "category": "AI",
                            "lead_source": "Direct",
                        }
                    ],
                },
            },
        },
    )
    res = resp["result"]["structuredContent"]
    assert res["created_leads"] == 1
    assert res["enrolled_in_campaign"] == 0
    rec_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM campaign_recipients").fetchone()["cnt"]
    assert rec_cnt == 0

