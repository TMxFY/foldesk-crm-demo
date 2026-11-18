import json
import sqlite3
from pathlib import Path

import pytest

from app.db import CANONICAL_STEP3_IMAGE_URL, connect, init_db
from app.hygiene import audit_crm_hygiene, append_ofm_review_lead
from app.leadgen_settings import (
    DEFAULT_LEADGEN_CONFIG,
    push_discovered_now,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.mcp_server import call_mcp_tool_handler
from app.outreach import preview_outreach_batch
from leadgen import tg_channel_graph, tg_identity_bridge
from leadgen.outreach_copy import REVSHARE_OFFER_TEXT


def _setup_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "crm_test.db"
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"

    monkeypatch.setenv("CRMTG_DB", str(db_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CRMTG_LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    conn = connect(str(db_path))
    init_db(conn)
    return conn, state_dir


def test_ofm_outreach_isolated_until_enabled_and_passes_hygiene_and_rule5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_test_env(tmp_path, monkeypatch)

    # Queue 3 valid OFM buyers (same group, no websites, emoji name) + 1 spam/casino lead
    ofm_rows = [
        {
            "username": "ameliemlh",
            "name": "Amelie",
            "source": "ofm_tg",
            "source_group": "FranceOFM",
            "ofm_tier": "traffic_buyer",
            "market": "US",
            "channels": ["Instagram"],
            "signal_summary": "RECRUTEMENT VA INSTAGRAM — US market agency hiring",
            "sample_message": "RECRUTEMENT VA Instagram US market scaling OF/Fanvue creators",
            "icp_score": 92,
        },
        {
            "username": "noa_vbe",
            "name": "Noa 🌸",
            "source": "ofm_tg",
            "source_group": "FranceOFM",
            "ofm_tier": "traffic_buyer",
            "market": "FR",
            "channels": ["Instagram"],
            "signal_summary": "RECRUTEMENT VA INSTA",
            "sample_message": "Recrutement VA Insta quotidien 7j/7 agence OFM",
            "icp_score": 92,
        },
        {
            "username": "mathislrq",
            "name": "Mathis lrq",
            "source": "ofm_tg",
            "source_group": "FranceOFM",
            "ofm_tier": "agency_owner",
            "market": "FR",
            "channels": ["OnlyFans/Fanvue"],
            "signal_summary": "Scaling MYM/OF creator",
            "sample_message": "Accompagner scaling d'une creatrice MYM/OnlyFans",
            "icp_score": 94,
        },
    ]
    for r in ofm_rows:
        assert append_ofm_review_lead(state_dir, r) is True

    # Also append a 1win spam line directly to test MCP OFM spam filter
    ofm_file = state_dir / "ofm_review_leads.jsonl"
    ofm_file.write_text(
        ofm_file.read_text(encoding="utf-8")
        + json.dumps(
            {
                "username": "bad_1win_promo",
                "name": "1win partners",
                "source": "ofm_tg",
                "source_group": "FranceOFM",
                "ofm_tier": "traffic_buyer",
                "sample_message": "1win partners cpa casino promo",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # 1) When ofm_outreach_enabled is False (default), push_discovered_now MUST NOT push OFM leads
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "total_daily_quota": 1,
            "b2b": {
                "enabled": True,
                "ofm_outreach_enabled": False,
                "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
                "campaign_daily_limit": 20,
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )
    assert push_discovered_now(conn)["ingested"] == 0

    # 2) Attempting to inject OFM lead directly into B2B campaign MUST STILL BE REJECTED
    b2b_reject = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_name": "SoftUGC — База Все2 (2 045 лидов)",
            "leads": [
                {
                    "username": "rogue_ofm_b2b_attempt",
                    "name": "OFM Agency",
                    "source": "ofm_tg",
                    "ofm_tier": "traffic_buyer",
                    "step1_text": f"Hey, scaling creators.\n\nWe run SoftUGC 183.5M views in 90 days no shoots 13 to 40x less than standard clippers.\n\n{REVSHARE_OFFER_TEXT}",
                    "icp_score": 95,
                }
            ],
        },
    )
    assert b2b_reject["enrolled_in_campaign"] == 0

    # 3) Enable dedicated OFM campaign outreach -> all 3 distinct OFM contacts ingest, 1win spam blocked
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "total_daily_quota": 1,
            "b2b": {
                "enabled": True,
                "ofm_outreach_enabled": True,
                "target_campaign_ofm": "SoftUGC — OFM (TG)",
                "ofm_daily_limit": 25,
                "target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)",
                "campaign_daily_limit": 20,
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )
    sync_leadgen_campaigns(conn, sync_campaign_limits=True)
    res_ofm = push_discovered_now(conn)
    assert res_ofm["ingested"] == 3

    ofm_camp = conn.execute(
        "SELECT * FROM campaigns WHERE name = 'SoftUGC — OFM (TG)'"
    ).fetchone()
    assert ofm_camp is not None
    assert int(ofm_camp["daily_limit"]) == 25

    ofm_steps = conn.execute(
        "SELECT step_order, delay_hours, image_url FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order",
        (ofm_camp["id"],),
    ).fetchall()
    assert [(s["step_order"], s["delay_hours"]) for s in ofm_steps] == [(1, 0), (2, 24), (3, 24)]
    assert ofm_steps[2]["image_url"] == CANONICAL_STEP3_IMAGE_URL

    ofm_recs = conn.execute(
        "SELECT username, status, current_step, step1_text FROM campaign_recipients WHERE campaign_id = ? ORDER BY id",
        (ofm_camp["id"],),
    ).fetchall()
    assert [r["username"] for r in ofm_recs] == ["ameliemlh", "noa_vbe", "mathislrq"]
    assert all(r["status"] == "pending" and int(r["current_step"]) == 0 for r in ofm_recs)
    assert all("SoftUGC" in r["step1_text"] and REVSHARE_OFFER_TEXT in r["step1_text"] for r in ofm_recs)

    # 4) B2B daily quota (total_daily_quota=1) MUST NOT be starved by OFM backlog ingest
    b2b_final = state_dir / "raw_20261118_999999.final.jsonl"
    b2b_final.write_text(
        json.dumps(
            {
                "username": "fresh_saas_founder",
                "name": "Alex Founder",
                "project": "FocusPulse",
                "company": "FocusPulse",
                "project_url": "https://focuspulse.app",
                "category": "SaaS / AI app",
                "what_they_do": "AI productivity app ($19/mo subscription)",
                "icp_score": 90,
                "first_touch": (
                    "Hey,\n\nSaw FocusPulse. Getting users without expensive paid UA slows growth.\n\n"
                    "We run SoftUGC. 183.5M views in 90 days, no shoots, 13 to 40x less than standard clippers.\n\n"
                    f"{REVSHARE_OFFER_TEXT}"
                ),
                "source": "tg_groups",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    res_b2b = push_discovered_now(conn)
    assert res_b2b["ingested"] == 1

    # 5) CRM hygiene & Rule 5 strict Dry-Run MUST pass with 0 violations
    hyg = audit_crm_hygiene(conn)
    assert hyg["ok"] is True, hyg
    assert hyg["active_junk_recipient_count"] == 0
    assert hyg["active_junk_lead_count"] == 0
    assert hyg["dirty_title_lead_count"] == 0

    prev = preview_outreach_batch(conn, int(ofm_camp["id"]), strict=True)
    assert prev["ok"] is True, prev
    assert prev["forbidden_pattern_count"] == 0
    assert prev["fresh_untouched_count"] == 3
    conn.close()


def test_tg_identity_bridge_website_linkedin_email_and_namesake_guard() -> None:
    def ok_tme_html(nick: str, title: str, bio: str) -> str:
        return (
            "<html><body>"
            f'<div class="tgme_page_title"><span>{title}</span></div>'
            f'<div class="tgme_page_extra">@{nick}</div>'
            f'<div class="tgme_page_description">{bio}</div>'
            '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Send Message</a></div>'
            "</body></html>"
        )

    # 1) Direct website HTML t.me extraction
    res_site = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Sarah Founder",
            "company": "CoolFit",
            "project_url": "https://coolfit.app",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: '<a href="https://t.me/coolfit_sarah">Telegram</a>',
        tme_fetch_fn=lambda nick: ok_tme_html(nick, "Sarah Founder", "Building CoolFit") if nick == "coolfit_sarah" else "",
    )
    assert res_site.get("username") == "coolfit_sarah"
    assert res_site.get("tg_bridge_match") == "website_direct"

    # 2) LinkedIn slug confirmed by bio / brand token
    res_li = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Ivan Petrov",
            "company": "AcmeNotes",
            "project_url": "https://acmenotes.io",
            "founder_linkedin": "https://www.linkedin.com/in/ivan-petrov-889123a1/",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: ok_tme_html(nick, "Ivan Petrov", "Founder @ acmenotes.io") if nick == "ivan_petrov" else "",
    )
    assert res_li.get("username") == "ivan_petrov"
    assert res_li.get("tg_bridge_match") == "linkedin_handle_confirmed"

    # 3) Personal non-role email local part works; role emails (info/support) skipped; namesakes rejected
    res_namesake = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Ivan Petrov",
            "company": "AcmeNotes",
            "project_url": "https://acmenotes.io",
            "work_email": "info@acmenotes.io",
            "founder_linkedin": "https://www.linkedin.com/in/ivan-petrov-889123a1/",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: ok_tme_html(nick, "Some Other Ivan", "Crypto trader unrelated"),
    )
    assert not res_namesake.get("username")

    res_email = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Olga Smirnova",
            "company": "VibeDeck",
            "project_url": "https://vibedeck.ai",
            "work_email": "olga.smirnova@vibedeck.ai",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: ok_tme_html(nick, "Olga", "Building VibeDeck") if nick == "olga_smirnova" else "",
    )
    assert res_email.get("username") == "olga_smirnova"
    assert res_email.get("tg_bridge_match") == "email_handle_confirmed"


def test_default_tg_groups_and_graph_seeds_expanded() -> None:
    for grp in ("indiehackers", "founders_chat", "startupchat", "buildinpublic"):
        assert grp in DEFAULT_LEADGEN_CONFIG["b2b"]["tg_groups_new"]
    for seed in ("ai_founders", "microconf", "startups"):
        assert seed in tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS
