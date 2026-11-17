from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

from app.db import (
    apply_sniper_icp_cleanup_v7,
    bootstrap_prod_snapshot,
    connect,
    init_db,
    utc_now,
)
from app.hygiene import (
    audit_crm_hygiene,
    audit_server_leadgen_readiness,
    clean_crm_lead_names,
    heal_crm_hygiene,
    sniper_lead_violation_reason,
)
from app.leadgen_settings import (
    DEFAULT_B2B_STEPS,
    DEFAULT_LEADGEN_CONFIG,
    get_leadgen_status,
    push_discovered_now,
    save_leadgen_config,
)
from app.mcp_server import call_mcp_tool_handler, handle_jsonrpc_message
from app.outreach import (
    create_campaign,
    get_campaign,
    preview_outreach_batch,
    run_autopilot_tick,
    run_campaign_batch,
    set_autopilot_enabled,
)
from app.telegram_gateway import FakeGateway
from leadgen.outreach_copy import REVSHARE_OFFER_TEXT, SOFTUGC_PITCH_TEXT


def _make_valid_mtproto_session(path: Path, key_byte: int = 0x42) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as sconn:
        sconn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, auth_key BLOB)"
        )
        sconn.execute("DELETE FROM sessions")
        sconn.execute(
            "INSERT INTO sessions (dc_id, server_address, port, auth_key) VALUES (2, '149.154.167.50', 443, ?)",
            (bytes([key_byte]) * 256,),
        )
        sconn.commit()
    return path


def _seed_connected_account(conn: sqlite3.Connection, acc_id: int = 41, username: str = "vlad_sender") -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, provider,
            telegram_user_id, telegram_username, telegram_name,
            daily_limit, sent_today, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', 'active', 'telegram', ?, ?, 'Vlad Sender', 40, 0, ?, ?)
        """,
        (acc_id, f"+1555999{acc_id:04d}", f"data/sessions/acc_{acc_id}.session", 8100000 + acc_id, username, now, now),
    )
    conn.commit()
    return acc_id


def test_sniper_lead_violation_reason_catches_all_agencies_oss_devtools_and_micro_vacancies():
    # 1. Non-ICP categories / niches
    for bad_niche in (
        "Агентство",
        "Сервис / агентство",
        "Инфра",
        "Инфраструктура / dev",
        "Низкий приоритет",
        "Партнёр, не ЦА",
    ):
        assert sniper_lead_violation_reason(company="SomeCo", niche=bad_niche) != ""

    # 2. Agencies, helpers, marketers, bot hostings, PR, mentors, dev teams
    junk_profiles = [
        {"company": "FINPR Agency", "description": "Crypto PR & marketing"},
        {"company": "Marketacross", "description": "Blockchain PR"},
        {"company": "You Agency", "note": "Growth agency"},
        {"name": "Alex | Taboola expert", "note": "Media buying"},
        {"name": "Sam — Shopify expert", "note": "Store setup"},
        {"company": "Shopify merchant Help", "note": "Store audits"},
        {"company": "Kirito Bot Hosting", "description": "24/7 bot hosting"},
        {"company": "CloudBot", "description": "Telegram managed hosting for bots"},
        {"company": "SalesPro", "note": "Отделы продаж B2B под ключ"},
        {"name": "Aimrktops", "company": "Aimrktops"},
        {"name": "Itmentor", "note": "IT ментор до оффера"},
        {"company": "DevSquad", "description": "Full Stack Developers Team"},
        {"company": "EcomBoost", "note": "Helping online stores get consistent sales"},
        {"company": "ChainBuzz", "description": "PR & Growth for Crypto"},
        {"name": "sparks_merch", "company": "Sparks Merch", "url": "https://sparks-merch.com"},
    ]
    for prof in junk_profiles:
        reason = sniper_lead_violation_reason(**prof)
        assert reason != "", f"Expected violation reason for {prof}"

    # 3. Free / open-source / dev-only utilities & ethical hacking & backlink indexing
    oss_and_dev_profiles = [
        {"company": "ClipMenu", "description": "A free, open-source Mac app for clipboard"},
        {"company": "OpenLogger", "note": "100% open-source telemetry SDK"},
        {"company": "SecRecover", "description": "Account recovery APIs and ethical hacking"},
        {"company": "CodeGraph", "description": "движок графов знаний кодовой базы для разработчиков"},
        {"company": "agentmash", "note": "синхронизирует AI-кодинг-агентов (Claude Code, Codex, Cursor)"},
        {"company": "IndexFast", "description": "Backlink indexing service for SEO"},
    ]
    for prof in oss_and_dev_profiles:
        reason = sniper_lead_violation_reason(**prof)
        assert reason != "", f"Expected OSS/dev violation for {prof}"

    # 4. Micro-vacancies when vacancies_enabled=False
    assert sniper_lead_violation_reason(name="Сторисмейкер", vacancies_enabled=False) != ""
    assert sniper_lead_violation_reason(lead_source="mari_vakansii", vacancies_enabled=False) != ""
    assert sniper_lead_violation_reason(note="Ищу человека, подработка за 10-15 тыс в месяц", vacancies_enabled=False) != ""

    # 5. Valid B2B SaaS / Consumer App / Web3 product buyers return ""
    assert (
        sniper_lead_violation_reason(
            name="Alice Smith",
            company="Monetor",
            niche="Fintech SaaS",
            description="Paid subscription personal finance & expense tracker app ($12/mo)",
            url="https://monetor.app",
            lead_source="Cold outreach",
        )
        == ""
    )


def test_clean_crm_lead_names_normalizes_hn_numbers_urls_and_human_fio_idempotently():
    assert clean_crm_lead_names("Show HN: Monetor – expense tracker for iOS", None) == (
        "Monetor",
        "Monetor",
    )
    assert clean_crm_lead_names("Show HN: Gleanary", "Show HN: Gleanary") == (
        "Gleanary",
        "Gleanary",
    )
    assert clean_crm_lead_names("Ask HN: PulseDeck - analytics", None) == (
        "PulseDeck",
        "PulseDeck",
    )
    assert clean_crm_lead_names("Launch HN: Veloce (YC W26) — AI billing", None) == (
        "Veloce",
        "Veloce",
    )
    assert clean_crm_lead_names("6. agentmash", "6. agentmash") == (
        "Agentmash",
        "Agentmash",
    )
    assert clean_crm_lead_names("Олег https://freeline.studio", None) == (
        "Олег",
        None,
    )
    assert clean_crm_lead_names("Олег https://freeline.studio", "Олег", "https://freeline.studio") == (
        "Олег",
        "Freeline",
    )

    for fio in ("Anna Bozina", "Veronika Panova", "Виктор Боргезе", "Timmy Smith"):
        c_name, c_comp = clean_crm_lead_names(fio, fio, None)
        assert c_name == fio
        assert c_comp is None
        # Idempotence check
        assert clean_crm_lead_names(c_name, c_comp, None) == (c_name, c_comp)

    assert clean_crm_lead_names("Anna Bozina", "Anna Bozina", "https://payflow.io") == (
        "Anna Bozina",
        "Payflow",
    )
    # Real company names stay untouched
    assert clean_crm_lead_names("Роблокс", "Indigo VC", "https://www.indg.vc/") == (
        "Роблокс",
        "Indigo VC",
    )
    assert clean_crm_lead_names("Romain", "T3tris", "https://t3tris.finance") == (
        "Romain",
        "T3tris",
    )


def test_self_healing_cleans_prod_junk_and_strictly_protects_indigo_vc_and_t3tris(tmp_path: Path):
    conn = connect(tmp_path / "hygiene_self_heal.db")
    init_db(conn)
    now = utc_now()

    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=40,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    # Protected deals: Indigo VC ($4394, won, Signed) and T3tris ($1000, on_hold, On hold) — both contactless in DB!
    conn.execute(
        """
        INSERT INTO leads (
            name, company, status, amount, monthly_value, currency,
            url, niche, lead_source, airtable_status, created_at, updated_at
        ) VALUES
            ('Роблокс', 'Indigo VC', 'won', 4394, 4090, 'USD', 'https://www.indg.vc/', 'Gaming', 'Cold outreach', 'Signed', ?, ?),
            ('Romain', 'T3tris', 'on_hold', 1000, 600, 'USD', 'https://t3tris.finance', 'Crypto', 'Cold outreach', 'On hold', ?, ?),
            ('Call Lead Agency', 'FINPR Call', 'call_scheduled', 0, 0, 'USD', 'https://finpr.io', 'Агентство', 'Cold outreach', 'Call scheduled', ?, ?)
        """,
        (now, now, now, now, now, now),
    )

    # Prod junk & dirty leads:
    # 1) rea-video contactless
    # 2) FINPR Agency
    # 3) Kirito Bot Hosting
    # 4) 6. agentmash (both dirty numbered title AND AI coding agents sync dev tool)
    # 5) Show HN: Gleanary (dirty HN title on valid SaaS buyer)
    # 6) Anna Bozina (company == human ФИО on valid buyer)
    conn.execute(
        """
        INSERT INTO leads (
            username, name, company, status, amount, monthly_value,
            url, niche, note, description, lead_source, created_at, updated_at
        ) VALUES
            (NULL, 'rea-video', 'rea-video', 'new', 0, 0, 'https://rea-video.com', 'AI', 'Scraped site without TG', 'rea-video', 'rea-video', ?, ?),
            ('finpr_bd', 'Alex FINPR', 'FINPR Agency', 'new', 0, 0, 'https://finpr.agency', 'Сервис / агентство', 'PR & Growth for Crypto', 'FINPR Agency', 'Cold outreach', ?, ?),
            ('kirito_host', 'Kirito', 'Kirito Bot Hosting', 'talking', 0, 0, 'https://kiritohost.xyz', 'Инфра', 'Telegram bot setup & managed hosting 24/7', 'Kirito Bot Hosting', 'Leadgen Auto', ?, ?),
            ('alimwtf', '6. agentmash', '6. agentmash', 'new', 0, 0, 'https://agentmash.dev', 'SaaS', 'синхронизирует AI-кодинг-агентов (Claude Code, Codex, Cursor)', '6. agentmash', 'Google Sheet', ?, ?),
            ('gleanary_ceo', 'Show HN: Gleanary', 'Show HN: Gleanary', 'new', 0, 0, 'https://gleanary.com', 'SaaS', 'Paid knowledge workspace for research teams', 'Show HN: Gleanary', 'Leadgen HN', ?, ?),
            ('anna_bozina_tg', 'Anna Bozina', 'Anna Bozina', 'new', 0, 0, NULL, 'EdTech', 'Paid language learning mobile app ($19/mo)', 'Anna Bozina', 'Cold outreach', ?, ?)
        """,
        (now, now, now, now, now, now, now, now, now, now, now, now),
    )

    # Prod junk recipients in campaign_recipients: sparks_merch + contactless recipient
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, name, company, category, website, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES
            (?, 'sparks_merch', 'Sparks Merch', 'Sparks Merch', 'Сервис / агентство', 'https://sparks-merch.com', 'Крипто-мерч и сувениры', 'Hey Sparks', 0, 'pending', ?, ?),
            (?, NULL, 'Phantom NoHandle', 'PhantomCo', 'SaaS', 'https://phantomco.io', 'B2B SaaS', 'Hey Phantom', 0, 'pending', ?, ?),
            (?, 'clean_saas_founder', 'Clean Founder', 'CleanMetrics', 'SaaS', 'https://cleanmetrics.io', 'Paid subscription analytics SaaS', 'Hey CleanMetrics', 0, 'pending', ?, ?)
        """,
        (b2b_camp["id"], now, now, b2b_camp["id"], now, now, b2b_camp["id"], now, now),
    )
    conn.commit()

    audit_before = audit_crm_hygiene(conn)
    assert audit_before["ok"] is False
    assert audit_before["active_junk_recipient_count"] == 2
    assert audit_before["contactless_lead_count"] == 1
    assert audit_before["active_junk_lead_count"] == 3
    assert audit_before["dirty_title_lead_count"] >= 2
    assert len(audit_before["samples"]) >= 1

    # Run migration v7 via init_db after clearing flag to verify startup self-healing
    conn.execute("DELETE FROM kv_settings WHERE key = 'sniper_icp_cleanup_v7'")
    conn.commit()
    init_db(conn)

    v7_flag = conn.execute("SELECT value FROM kv_settings WHERE key = 'sniper_icp_cleanup_v7'").fetchone()
    assert v7_flag is not None and v7_flag["value"] == "1"
    assert apply_sniper_icp_cleanup_v7(conn) == {"already_applied": True}

    audit_after = audit_crm_hygiene(conn)
    assert audit_after["ok"] is True
    assert audit_after["active_junk_recipient_count"] == 0
    assert audit_after["active_junk_lead_count"] == 0
    assert audit_after["contactless_lead_count"] == 0
    assert audit_after["dirty_title_lead_count"] == 0

    # Verify Indigo VC, T3tris, and call_scheduled are 100% untouched
    indigo = conn.execute("SELECT * FROM leads WHERE company = 'Indigo VC'").fetchone()
    assert indigo["status"] == "won"
    assert indigo["amount"] == 4394
    assert indigo["airtable_status"] == "Signed"

    t3tris = conn.execute("SELECT * FROM leads WHERE company = 'T3tris'").fetchone()
    assert t3tris["status"] == "on_hold"
    assert t3tris["amount"] == 1000
    assert t3tris["airtable_status"] == "On hold"

    call_lead = conn.execute("SELECT * FROM leads WHERE company = 'FINPR Call'").fetchone()
    assert call_lead["status"] == "call_scheduled"
    assert call_lead["airtable_status"] == "Call scheduled"

    # Verify quarantined junk leads
    for q_uname in ("finpr_bd", "kirito_host", "alimwtf"):
        q_row = conn.execute("SELECT * FROM leads WHERE username = ?", (q_uname,)).fetchone()
        assert q_row["status"] == "lost"
        assert q_row["airtable_status"] == "Cancelled"
        assert str(q_row["next_step"] or "").startswith("Авто-карантин Sniper ICP: ")

    rea_row = conn.execute("SELECT * FROM leads WHERE lead_source = 'rea-video'").fetchone()
    assert rea_row["status"] == "lost"
    assert rea_row["airtable_status"] == "Cancelled"
    assert str(rea_row["next_step"] or "").startswith("Авто-карантин Sniper ICP: ")

    # Verify sanitized cold leads remained 'new' with clean titles
    gleanary = conn.execute("SELECT * FROM leads WHERE username = 'gleanary_ceo'").fetchone()
    assert gleanary["status"] == "new"
    assert gleanary["name"] == "Gleanary"
    assert gleanary["company"] == "Gleanary"

    anna = conn.execute("SELECT * FROM leads WHERE username = 'anna_bozina_tg'").fetchone()
    assert anna["status"] == "new"
    assert anna["name"] == "Anna Bozina"
    assert anna["company"] is None

    # Verify sparks_merch & contactless recipient stopped, while clean_saas_founder stayed pending
    sparks_rec = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'sparks_merch'"
    ).fetchone()
    assert sparks_rec["status"] == "completed"
    assert str(sparks_rec["error_text"] or "").startswith("Остановлено Sniper ICP-фильтром: ")

    clean_rec = conn.execute(
        "SELECT status FROM campaign_recipients WHERE username = 'clean_saas_founder'"
    ).fetchone()
    assert clean_rec["status"] == "pending"
    conn.close()


def test_prod_snapshot_has_zero_false_positives_under_hygiene_audit(tmp_path: Path):
    conn = connect(tmp_path / "prod_snap_check.db")
    init_db(conn)
    bootstrap_prod_snapshot(conn)
    audit = audit_crm_hygiene(conn)
    assert audit["ok"] is True, f"Unexpected false positives on prod_snapshot: {audit}"
    heal_res = heal_crm_hygiene(conn)
    assert heal_res == {"quarantined_recipients": 0, "quarantined_leads": 0, "sanitized_leads": 0}
    conn.close()


def test_mcp_ingest_gate_direct_x_leads_and_outreach_step1_step2_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "leadgen_state"
    cfg_path = tmp_path / "leadgen_config.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))

    # Check DEFAULT_LEADGEN_CONFIG defaults
    assert "shopifyeasy" not in DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_new"]
    assert "solofounders" not in DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_old"]
    assert DEFAULT_LEADGEN_CONFIG["b2b"]["limits"]["external_limit"] == 0

    conn = connect(tmp_path / "mcp_outreach_gate.db")
    init_db(conn)
    acc_id = _seed_connected_account(conn, 51, "vlad_outreach")

    valid_step1 = (
        f"Hey,\n\nSaw Monetor. Paid acquisition gets expensive fast.\n\n"
        f"{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    )

    # 1. MCP ingest_discovered_leads blocks contactless (saves to direct_x_leads.jsonl),
    # blocks icp_score < 80, blocks agencies/OSS, and cleans dirty HN title on valid lead!
    ingest_res = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_name": "SoftUGC — База Все2",
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Direct X Founder",
                    "company": "PayOrbit",
                    "email": "ceo@payorbit.io",
                    "x_url": "https://x.com/payorbit_ceo",
                    "url": "https://payorbit.io",
                    "category": "Fintech SaaS",
                    "their_angle": "Cross-border B2B billing platform",
                    "icp_score": 88,
                },
                {
                    "username": "@low_score_dev",
                    "name": "Low Score",
                    "company": "TinyUtil",
                    "url": "https://tinyutil.dev",
                    "category": "SaaS",
                    "their_angle": "Tiny CLI helper",
                    "icp_score": 65,
                    "step1_text": valid_step1,
                },
                {
                    "username": "@finpr_sales",
                    "name": "FINPR",
                    "company": "FINPR Agency",
                    "url": "https://finpr.agency",
                    "category": "Агентство",
                    "their_angle": "PR & Growth for Crypto",
                    "icp_score": 95,
                    "step1_text": valid_step1,
                },
                {
                    "username": "@monetor_founder",
                    "name": "Show HN: Monetor – expense tracker for iOS",
                    "company": None,
                    "url": "https://monetor.app",
                    "category": "Fintech",
                    "their_angle": "Subscription iOS expense tracker ($9.99/mo)",
                    "icp_score": 92,
                    "step1_text": valid_step1,
                },
            ],
        },
    )
    assert ingest_res["created_leads"] == 1
    assert ingest_res["enrolled_in_campaign"] == 1
    assert ingest_res["skipped_non_icp"] == 3

    all_db_leads = conn.execute("SELECT username, name, company FROM leads").fetchall()
    assert len(all_db_leads) == 1
    assert all_db_leads[0]["username"] == "monetor_founder"
    assert all_db_leads[0]["name"] == "Monetor"
    assert all_db_leads[0]["company"] == "Monetor"

    # Verify list_direct_x_leads MCP tool & get_leadgen_status.direct_x_leads_count
    tools_list = handle_jsonrpc_message(conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "list_direct_x_leads" in {t["name"] for t in tools_list["result"]["tools"]}

    dx_res = call_mcp_tool_handler(conn, "list_direct_x_leads", {"limit": 10})
    assert dx_res["ok"] is True
    assert dx_res["total"] == 1
    assert dx_res["leads"][0]["company"] == "PayOrbit"
    assert dx_res["leads"][0]["email"] == "ceo@payorbit.io"

    lg_status = get_leadgen_status(conn)
    assert lg_status["direct_x_leads_count"] == 1

    # 2. push_discovered_now also blocks contactless & non-ICP rows from JSONL
    (state_dir / "batch.final.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "name": "NoTG SaaS",
                        "company": "NoTGCloud",
                        "email": "hi@notgcloud.com",
                        "website": "https://notgcloud.com",
                        "category": "SaaS",
                        "their_angle": "Subscription cloud backup SaaS",
                    }
                ),
                json.dumps(
                    {
                        "username": "@oss_mac_dev",
                        "name": "MacDev",
                        "company": "FreeMacBar",
                        "website": "https://freemacbar.app",
                        "category": "SaaS",
                        "their_angle": "A free, open-source Mac app for menu bar",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    push_res = push_discovered_now(conn)
    assert push_res["ingested"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1

    # 3. Outreach Step 1 AND Step 2/3 block agencies, open-source/dev tools, and contactless rows
    camp = create_campaign(
        conn,
        name="SoftUGC — Outbound Guard Test",
        account_ids=[acc_id],
        daily_limit=30,
        status="running",
        steps=DEFAULT_B2B_STEPS,
    )
    now = utc_now()
    # Seed prior outgoing message for Step 2 agency recipient so only Sniper guard stops Step 2!
    cur_dlg = conn.execute(
        """
        INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, is_group, unread_count, created_at, updated_at)
        VALUES (?, 'user', 777001, 'marketacross_bd', 'Marketacross', 0, 0, ?, ?)
        """,
        (acc_id, now, now),
    )
    dlg_id = cur_dlg.lastrowid
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 1, 'out', 'Hey Marketacross, initial touch', '2026-11-15T10:00:00Z', ?, ?)
        """,
        (dlg_id, now, now),
    )

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, assigned_account_id, sent_account_id, telegram_user_id, username,
            name, company, category, their_angle, step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES
            (?, ?, NULL, 777002, 'oss_tool_founder', 'OSS Founder', 'OpenClip', 'SaaS', 'free, open-source Mac app', ?, 0, 'pending', NULL, ?, ?),
            (?, ?, NULL, NULL, NULL, 'Contactless Row', 'GhostApp', 'SaaS', 'Paid SaaS app', ?, 0, 'pending', NULL, ?, ?),
            (?, ?, ?, 777001, 'marketacross_bd', 'Marketacross BD', 'Marketacross', 'Агентство', 'PR & Growth for Crypto', ?, 1, 'in_progress', '2026-11-15T10:00:00Z', ?, ?)
        """,
        (
            camp["id"], acc_id, valid_step1, now, now,
            camp["id"], acc_id, valid_step1, now, now,
            camp["id"], acc_id, acc_id, valid_step1, now, now,
        ),
    )
    conn.commit()

    preview = preview_outreach_batch(conn, camp["id"], strict=False)
    assert preview["ok"] is False
    viol_codes = {v["code"] for v in preview["violations"]}
    assert "sniper_icp_violation" in viol_codes
    assert "contactless_recipient" in viol_codes

    gw = FakeGateway()
    batch_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10))
    assert batch_res["sent"] == 0
    assert gw.sent == []

    statuses = [
        r["status"]
        for r in conn.execute(
            "SELECT status FROM campaign_recipients WHERE campaign_id = ?", (camp["id"],)
        ).fetchall()
    ]
    assert statuses == ["completed", "completed", "completed"]

    # Autopilot tick also runs heal_crm_hygiene(conn) automatically
    conn.execute(
        """
        INSERT INTO leads (username, name, company, status, amount, monthly_value, niche, lead_source, created_at, updated_at)
        VALUES ('you_agency_ceo', 'You Agency', 'You Agency', 'new', 0, 0, 'Агентство', 'Cold outreach', ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    set_autopilot_enabled(conn, True)
    asyncio.run(run_autopilot_tick(conn, gateway=gw, force=True))
    healed_lead = conn.execute("SELECT status, airtable_status FROM leads WHERE username = 'you_agency_ceo'").fetchone()
    assert healed_lead["status"] == "lost"
    assert healed_lead["airtable_status"] == "Cancelled"
    conn.close()


def test_audit_server_leadgen_readiness_enforced_vs_non_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg_path = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "leadgen_state"
    scraper_dir = tmp_path / "scraper_sessions"
    state_dir.mkdir(parents=True, exist_ok=True)
    scraper_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(scraper_dir))
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TREG_API_KEY", raising=False)
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)

    conn = connect(tmp_path / "readiness.db")
    init_db(conn)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "tg_groups": True,
                    "meta_ads": True,
                    "social_x_reddit": True,
                },
            },
        },
    )

    # 1. Non-enforced (local/dev): ok=True, but observability flags accurately show missing pieces
    local_audit = audit_server_leadgen_readiness(conn, enforce_vps_checks=False)
    assert local_audit["ok"] is True
    assert local_audit["enforced"] is False
    assert local_audit["leadgen_enabled"] is True
    assert local_audit["llm_key_ready"] is False
    assert local_audit["scraper_session_required"] is True
    assert local_audit["scraper_session_ready"] is False
    assert local_audit["treg_required"] is True
    assert local_audit["typesafe_ready"] is False
    assert "llm_api_key" in local_audit["missing_components"]
    assert "isolated_scraper_tg_session" in local_audit["missing_components"]

    # 2. Enforced (VPS): fails when missing LLM key / valid MTProto scraper session / treg / or stage errors exist
    vps_bad = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert vps_bad["ok"] is False
    assert vps_bad["enforced"] is True

    # Invalid stub session file (not a 256-byte MTProto SQLite session) must still fail scraper_session_ready!
    bad_sess = scraper_dir / "broken_stub.session"
    bad_sess.write_text("not-sqlite", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(bad_sess))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-live-valid")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-live-valid")
    assert audit_server_leadgen_readiness(conn, enforce_vps_checks=True)["scraper_session_ready"] is False

    # Valid isolated 256-byte MTProto session + fake treg binary & config -> ok=True!
    good_sess = _make_valid_mtproto_session(scraper_dir / "isolated_valid.session", key_byte=0x7A)
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(good_sess))
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir(parents=True, exist_ok=True)
    fake_treg = fake_bin_dir / "treg"
    fake_treg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_treg.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TREG_API_KEY", "treg-token-ok")

    vps_ok = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert vps_ok["ok"] is True
    assert vps_ok["llm_key_ready"] is True
    assert vps_ok["scraper_session_ready"] is True
    assert vps_ok["treg_ready"] is True
    assert vps_ok["typesafe_ready"] is True
    assert vps_ok["stage_timeout_count"] == 0
    assert vps_ok["missing_components"] == []

    # Stage timeout error in kv_settings['leadgen_stage_errors'] flips enforced ok -> False
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('leadgen_stage_errors', ?, ?)",
        (json.dumps([{"stage": "yc_ph_leads", "returncode": -1, "timeout": 120}]), utc_now()),
    )
    conn.commit()
    vps_timeout = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert vps_timeout["ok"] is False
    assert vps_timeout["stage_timeout_count"] == 1

    # Disabled leadgen -> ok=True even on enforced VPS
    save_leadgen_config(conn, {"enabled": False})
    vps_disabled = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert vps_disabled["ok"] is True
    assert vps_disabled["leadgen_enabled"] is False
    conn.close()
