from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from app.db import connect, init_db, utc_now
from app.leadgen_settings import push_discovered_now
from app.mcp_server import _find_lead_by_lookup, handle_jsonrpc_message, register_mcp_routes
from app.outreach import create_campaign, get_campaign
from leadgen import (
    buyer_catalogs,
    creator_external_leads,
    crm_mcp_push,
    identity_enrichment,
    job_monitor,
    supply_side,
    tg_curate,
    tg_enrich_leads,
    tg_llm_qualify,
    tg_pipeline_locked,
    tg_scrape_posters,
    typesafe_leads,
    yc_ph_leads,
)
from leadgen.outreach_copy import REVSHARE_OFFER_TEXT, SOFTUGC_PITCH_TEXT


def _mcp_call(conn, tool_name: str, arguments: dict) -> dict:
    resp = handle_jsonrpc_message(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    assert isinstance(resp, dict) and "result" in resp, f"Unexpected MCP error: {resp}"
    res = resp["result"]
    assert res["isError"] is False, f"Tool {tool_name} failed: {res['content'][0]['text']}"
    return res["structuredContent"]


# 1. Supply-side hard gate
def test_supply_side_unconditionally_blocks_agencies_freelancers_and_vendors_even_with_founder_hiring():
    banned_bios = [
        "UGC Creator | Ambassador | Video creator | Freelancer",
        "Web3 Shiller|Helping projects trend & gain real traction| Hype Builder",
        "Team: || BD || KOL manager ||Community moderator|| community manager|| Marketer",
        "Shopify merchant Help and developer",
        "Helping online stores get consistent sales with smart strategies",
        "Сервис информационной безопасности + разработка ботов с ИИ",
        "Social media account recovery APIs and ethical hacking tools",
        "Telegram bot setup & managed hosting · music, autofilter, files · 24/7",
        "Co-Founder, CEO @ ana.fi | GTM Consultancy for AI and Blockchain",
    ]
    for bio in banned_bios:
        assert supply_side.is_supply_side(bio) is True, f"Missed bare supply bio: {bio}"
        poisoned = f"{bio} | Founder & CEO | hiring | my community"
        assert supply_side.is_supply_side(poisoned) is True, f"Bypassed via founder/hiring/community: {poisoned}"


# 2. TypeSafe thresholds, SELL_ROLES, and curate category gate
def test_typesafe_creator_filter_and_curate_drop_non_buyer_roles_and_agency_categories(monkeypatch):
    monkeypatch.delenv("TYPESAFE_CREATOR_KEEP", raising=False)
    monkeypatch.delenv("TYPESAFE_CREATOR_DROP", raising=False)

    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.42, "typesafe_role": "Creator/KOL", "typesafe_category": "SaaS / AI app"}
    ) != "keep"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.45, "typesafe_role": "Community", "typesafe_category": "DeFi / DEX"}
    ) != "keep"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.65, "typesafe_role": "Dev", "typesafe_category": "SaaS / AI app"}
    ) != "keep"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.75, "typesafe_role": "Founder", "typesafe_category": "Сервис / агентство"}
    ) == "drop"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.35, "typesafe_role": "Founder", "typesafe_category": "SaaS / AI app"}
    ) != "keep"

    assert tg_scrape_posters.SELL_ROLES == {"Founder", "Marketing"}

    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    curated = tg_curate.curate_rows(
        [
            {"username": "vishaljaiswalofficial", "name": "Vishal", "role": "Founder"},
            {"username": "agency_lead", "project": "AgencyX", "category": "Агентство", "role": "Founder"},
            {"username": "service_lead", "project": "SvcX", "category": "Сервис / агентство", "role": "Founder"},
            {"username": "partner_lead", "project": "PartX", "category": "Партнёр, не ЦА", "role": "Founder"},
            {"username": "low_prio", "project": "LowX", "category": "Низкий приоритет", "role": "Founder"},
            {
                "username": "real_saas_buyer",
                "project": "MetricPulse",
                "project_url": "https://metricpulse.io",
                "category": "SaaS / AI app",
                "role": "Founder",
                "what_they_do": "Product analytics for mobile apps",
            },
        ]
    )
    assert [r["username"] for r in curated] == ["real_saas_buyer"]


# 3. tg_pipeline.sh removes blind members lurker pass & exports TG_SESSION
def test_tg_pipeline_sh_removes_blind_members_lurker_pass_and_exports_tg_session():
    script_text = (WORKTREE_ROOT / "leadgen" / "tg_pipeline.sh").read_text(encoding="utf-8")
    assert "TG_SCRAPE_MODE=members" not in script_text
    assert "export TG_SESSION=" in script_text
    assert "creatoros" not in script_text.lower()


# 4. tg_scrape_posters strict is_match & Rule 5 direct --send guard
def test_tg_scrape_posters_strict_is_match_and_blocks_direct_send_rule5(tmp_path):
    assert tg_scrape_posters.is_match("Just hanging out https://x.com/random_user", "Other", "") is False
    assert tg_scrape_posters.is_match("Follow my channel t.me/daily_thoughts", "Other", "") is False
    assert tg_scrape_posters.is_match("Living in Lisbon and loving specialty coffee every morning", "Other", "") is False
    assert tg_scrape_posters.is_match("", "Other", "Haha yeah totally agree with everyone in this thread today!") is False
    assert tg_scrape_posters.is_match(
        "Founder @ MetricPulse — B2B analytics https://metricpulse.io",
        "Founder",
        "",
    ) is True

    out_file = tmp_path / "scraped.jsonl"
    with out_file.open("w", encoding="utf-8") as fh:
        with pytest.raises(RuntimeError, match="Rule 5"):
            asyncio.run(
                tg_scrape_posters.scrape_group(
                    client=None,
                    group="solofounders",
                    msg_limit=10,
                    delay=0.0,
                    fh=fh,
                    exclude_paths=[],
                    send=True,
                )
            )


# 5. tg_channel_graph and tg_ad_spy exit 0 cleanly when seed/channel env vars are empty
def test_channel_graph_and_ad_spy_exit_zero_with_empty_channels_and_create_empty_jsonl(tmp_path):
    env = dict(os.environ)
    env["TG_GRAPH_SEED_CHANNELS"] = ""
    env["TG_AD_SPY_CHANNELS"] = ""
    env.pop("TG_API_ID", None)
    env.pop("TG_API_HASH", None)

    graph_out = tmp_path / "empty_graph.jsonl"
    spy_out = tmp_path / "empty_ad_spy.jsonl"

    r_graph = subprocess.run(
        [sys.executable, str(WORKTREE_ROOT / "leadgen" / "tg_channel_graph.py"), "--output", str(graph_out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r_graph.returncode == 0, f"tg_channel_graph failed: {r_graph.stderr}"
    assert graph_out.exists() and graph_out.read_text(encoding="utf-8") == ""

    r_spy = subprocess.run(
        [sys.executable, str(WORKTREE_ROOT / "leadgen" / "tg_ad_spy.py"), "--output", str(spy_out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r_spy.returncode == 0, f"tg_ad_spy failed: {r_spy.stderr}"
    assert spy_out.exists() and spy_out.read_text(encoding="utf-8") == ""


# 6. Shared atomic flock per session file across tg_pipeline_locked and job_monitor
def test_shared_atomic_session_flock_across_tg_pipeline_locked_and_job_monitor(tmp_path, monkeypatch):
    tim2 = tmp_path / "tim2.session"
    tim3 = tmp_path / "tim3.session"
    tim2.write_text("", encoding="utf-8")
    tim3.write_text("", encoding="utf-8")

    assert hasattr(tg_pipeline_locked, "acquire_session_locks")
    assert hasattr(tg_pipeline_locked, "session_lock_path")

    # Locking tim2 in job_monitor must block tg_pipeline_locked on tim2
    monkeypatch.setattr(job_monitor, "SESSION", str(tim2))
    monkeypatch.setattr(job_monitor, "OUTDIR", str(tmp_path))
    held = job_monitor.lock()
    try:
        with tg_pipeline_locked.acquire_session_locks([str(tim2), str(tim3)]) as acquired:
            assert acquired is False
    finally:
        if hasattr(held, "close"):
            held.close()

    # Once released, tg_pipeline_locked acquires both tim2 and tim3 atomically
    with tg_pipeline_locked.acquire_session_locks([str(tim2), str(tim3)]) as acquired2:
        assert acquired2 is True
        # And a concurrent job_monitor.lock() sees tim2 is busy and exits cleanly
        with pytest.raises(SystemExit):
            job_monitor.lock()


# 7. Sacred Rule 1: zero CreatorOS & plug-and-play organic distribution arm in leadgen
def test_sacred_rule1_zero_creatoros_and_plug_and_play_templates_in_leadgen():
    assert "creatoros" not in tg_llm_qualify.SYSTEM_PROMPT.lower()
    assert "SoftUGC" in tg_llm_qualify.SYSTEM_PROMPT
    assert "creatoros" not in creator_external_leads.UA["User-Agent"].lower()

    for rel in (
        "leadgen/meta_ad_leads.py",
        "leadgen/tg_to_gsheet.py",
        "leadgen/tg_pipeline_locked.py",
        "leadgen/tg_scrape_members.py",
        "leadgen/treg_enrich.py",
    ):
        txt = (WORKTREE_ROOT / rel).read_text(encoding="utf-8")
        assert "creatoros" not in txt.lower(), f"Found legacy CreatorOS in {rel}"

    bad_copy = (
        "Hey,\n\nSaw Acme. We built SoftUGC as a plug-and-play organic distribution arm: "
        f"183.5M views in 90 days.\n\n{REVSHARE_OFFER_TEXT}"
    )
    assert crm_mcp_push._has_canonical_vlad_copy(bad_copy) is False


# 8. Fail-closed qualification + catalog/volunteer/for-hire/product reality filters
def test_tg_llm_qualify_fail_closed_and_filters_catalogs_agencies_and_bad_products(monkeypatch):
    monkeypatch.setenv("TYPESAFE_ENABLED", "0")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "")

    rows = [
        {
            "name": "[FOR HIRE] Roblox Scripter & Video Creator",
            "project": "[FOR HIRE] Scripter",
            "project_url": "https://example.com",
            "source": "catalog_devforum",
        },
        {
            "name": "Co-Founder @ ana.fi",
            "bio": "Co-Founder, CEO @ ana.fi | GTM Consultancy for AI and Blockchain",
            "project": "ana.fi",
            "project_url": "https://ana.fi",
            "source": "meta_ads",
        },
        {
            "name": "Uncertain Startup Founder",
            "username": "uncertain_ceo",
            "bio": "Founder building stealth tool https://stealthx.io",
            "project": "StealthX",
            "project_url": "https://stealthx.io",
            "source": "tg",
        },
    ]
    # Without API key -> strict Fail-Closed (0 kept!)
    kept_no_key, dropped_no_key = tg_llm_qualify.qualify_loaded(rows, batch_size=10)
    assert kept_no_key == []
    assert dropped_no_key == 3

    # With API key, if LLM throws or returns broken JSON -> strict Fail-Closed (0 kept!)
    monkeypatch.setenv("RELAYFAST_API_KEY", "dummy-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "dummy-key")
    monkeypatch.setattr(tg_llm_qualify, "call_llm_retry", lambda batch: (_ for _ in ()).throw(RuntimeError("502 gateway")))
    kept_err, _ = tg_llm_qualify.qualify_loaded([rows[2]], batch_size=10)
    assert kept_err == []

    # Valid LLM call still drops empty product, cohort tags, insufficient product info, or agency
    def _fake_llm(batch):
        return [
            {"is_lead": True, "category": "SaaS / AI app", "role": "Founder", "project": "YC S26", "project_url": "", "what_they_do": "AI tool"},
            {"is_lead": True, "category": "SaaS / AI app", "role": "Founder", "project": "", "project_url": "", "what_they_do": "Some tool"},
            {"is_lead": True, "category": "SaaS / AI app", "role": "Founder", "project": " mystery ", "project_url": "https://m.io", "what_they_do": "Недостаточно информации о продукте"},
            {"is_lead": True, "category": "SaaS / AI app", "role": "Founder", "project": "ValidApp", "project_url": "https://validapp.io", "what_they_do": "AI note-taking app for B2B sales teams"},
        ]

    monkeypatch.setattr(tg_llm_qualify, "call_llm_retry", _fake_llm)
    test_batch = [
        {"name": "A", "username": "u1", "bio": "Founder YC S26", "first_touch": ""},
        {"name": "B", "username": "u2", "bio": "Founder", "first_touch": ""},
        {"name": "C", "username": "u3", "bio": "Founder", "first_touch": ""},
        {"name": "D", "username": "u4", "bio": "Founder ValidApp https://validapp.io", "first_touch": ""},
    ]
    kept_valid, _ = tg_llm_qualify.qualify_loaded(test_batch, batch_size=10)
    assert [r["username"] for r in kept_valid] == ["u4"]

    # buyer_catalogs drops [FOR HIRE] and plural volunteers
    discourse_payload = json.dumps(
        {
            "topic_list": {
                "topics": [
                    {"id": 1, "slug": "for-hire-dev", "title": "[FOR HIRE] Builder & Animator", "excerpt": "Portfolio https://mywork.dev"},
                    {"id": 2, "slug": "need-volunteers", "title": "Looking for volunteers for RPG", "excerpt": "Join https://rpgworld.io"},
                    {"id": 3, "slug": "paid-studio", "title": "Hiring trailer creator for our game ($1500)", "excerpt": "Game site https://realgame.io contact @realstudio"},
                ]
            }
        }
    )
    parsed_cat = buyer_catalogs.parse_catalog("devforum", discourse_payload, "https://devforum.roblox.com/c/82.json")
    assert len(parsed_cat) == 1
    assert parsed_cat[0]["project_url"] == "https://realgame.io"


# 9. Aggregator/platform domains never turn into company names ("lu", "news", "huggingface", "itunes")
def test_tg_enrich_leads_blocks_aggregator_domains_from_becoming_company_names():
    platform_urls = [
        "https://lu.ma/ai-founders-dinner",
        "https://news.ycombinator.com/item?id=42000",
        "https://huggingface.co/spaces/org/demo",
        "https://itunes.apple.com/us/app/cool/id12345",
        "https://apps.apple.com/us/app/cool/id12345",
        "https://play.google.com/store/apps/details?id=com.cool",
        "https://linktr.ee/founder",
        "https://notion.site/my-doc",
        "https://notion.so/my-doc",
        "https://calendly.com/founder/15min",
        "https://medium.com/@founder/post",
        "https://substack.com/@founder",
        "https://docs.google.com/document/d/123",
        "https://drive.google.com/file/d/123",
    ]
    assert tg_enrich_leads.pick_project_url(platform_urls) == ""
    for url in platform_urls:
        assert tg_enrich_leads.project_name_from(url, "") == "", f"Extracted garbage project name from {url}"


# 10. job_monitor full-text RESUME_RX + fail-closed triage + job_reply clean imports
def test_job_monitor_full_text_resume_filter_fail_closed_triage_and_job_reply_no_broken_import(monkeypatch):
    long_intro = "Привет всем участникам чата! Делаю крутые ролики на заказ в разных нишах для бизнеса и блогеров. " * 5
    assert len(long_intro) > 420
    resume_post = {"text": f"{long_intro}\n#резюме ищу работу моё портфолио — пишите @editor_resume"}
    real_job_post = {
        "text": "Ищем AI-креатора для генерации 30 вертикальных роликов в месяц под SaaS-продукт. Оплата $1500/мес, удалённо. Писать в личку @saas_hiring_lead"
    }
    filtered = job_monitor.prefilter([resume_post, real_job_post], min_len=80)
    assert filtered == [real_job_post]

    monkeypatch.setattr(job_monitor, "ask_llm", lambda prompt, timeout=240: (_ for _ in ()).throw(RuntimeError("LLM down")))
    assert job_monitor.triage([real_job_post]) == []

    monkeypatch.setattr(job_monitor, "ask_llm", lambda prompt, timeout=240: "not valid json at all")
    assert job_monitor.triage([real_job_post]) == []

    job_reply_src = (WORKTREE_ROOT / "leadgen" / "job_reply.py").read_text(encoding="utf-8")
    assert "app.runtime_context" not in job_reply_src


# 11. Block guessed namesake handles in creator_external_leads, identity_enrichment, and yc_ph_leads
def test_identity_and_external_and_yc_ph_block_guessed_namesake_handles(monkeypatch):
    # A) creator_external_leads HTTP t.me verification
    pages = {
        "https://t.me/nonexistent_nick": (
            200,
            '<div class="tgme_page"><div class="tgme_page_description">If you have <strong>Telegram</strong>, you can contact @nonexistent_nick right away.</div></div>',
        ),
        "https://t.me/some_channel": (
            200,
            '<div class="tgme_page"><div class="tgme_page_title">Crypto Daily</div><div class="tgme_page_extra">18 400 subscribers</div></div>',
        ),
        "https://t.me/some_helper_bot": (
            200,
            '<div class="tgme_page"><div class="tgme_page_title">Helper Bot</div><div class="tgme_page_extra">@some_helper_bot</div><a class="tgme_action_button_new">Start Bot</a></div>',
        ),
        "https://t.me/redirected_user": (302, ""),
        "https://t.me/real_alice": (
            200,
            '<div class="tgme_page"><div class="tgme_page_title"><span>Alice Chen</span></div><div class="tgme_page_extra">@real_alice</div><div class="tgme_page_description">Founder @ ClipPulse</div></div>',
        ),
    }

    def _fake_get(url, headers=None, params=None, timeout=20, allow_redirects=False):
        status, body = pages[url]
        return SimpleNamespace(
            status_code=status,
            is_redirect=(300 <= status < 400),
            headers={"Location": "https://telegram.org/"} if status == 302 else {},
            text=body,
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(creator_external_leads.requests, "get", _fake_get)
    monkeypatch.setattr(creator_external_leads.time, "sleep", lambda _: None)
    cands = [{"username": u.rsplit("/", 1)[-1], "url": u, "match": "explicit"} for u in pages]
    verified_http = creator_external_leads.verify_telegram_candidates(cands, max_checks=10)
    assert [v["username"] for v in verified_http] == ["real_alice"]

    # B) identity_enrichment must NEVER treat its own telegram_candidates field as explicit_telegram
    poisoned_row = {
        "name": "David Scott",
        "founder_name": "David Scott",
        "project": "HyperFlow",
        "project_url": "https://hyperflow.ai",
        "telegram_candidates": [
            {"username": "davidscott", "url": "https://t.me/davidscott", "match": "same_handle_candidate"}
        ],
    }
    extracted = identity_enrichment.extract_public_handles(poisoned_row)
    assert all(not h.explicit_telegram for h in extracted)
    gen_cands = identity_enrichment.generate_telegram_candidates(poisoned_row)
    assert all(c.match_type != "explicit_telegram" for c in gen_cands)

    # C) yc_ph_leads rejects namesake @davidscott without company/domain in TG bio/name, bots, and channels,
    #    and validate_lead rejects raw cohort tags, empty product, insufficient info, and <40 char pitches
    from telethon.tl.types import Channel, User

    lead_david = {
        "name": "HyperFlow",
        "founder_name": "David Scott",
        "site": "https://hyperflow.ai",
    }
    namesake_user = User(id=101, is_self=False, contact=False, mutual_contact=False, deleted=False, bot=False,
                         bot_chat_history=False, bot_nochats=False, verified=False, restricted=False, min=False,
                         bot_inline_geo=False, support=False, scam=False, apply_min_photo=False, fake=False,
                         bot_attach_menu=False, premium=False, attach_menu_enabled=False, access_hash=1,
                         first_name="David", last_name="Scott", username="davidscott")
    bot_entity = User(id=102, is_self=False, contact=False, mutual_contact=False, deleted=False, bot=True,
                      bot_chat_history=False, bot_nochats=False, verified=False, restricted=False, min=False,
                      bot_inline_geo=False, support=False, scam=False, apply_min_photo=False, fake=False,
                      bot_attach_menu=False, premium=False, attach_menu_enabled=False, access_hash=2,
                      first_name="HyperFlow", last_name="Bot", username="davidscott")
    chan_entity = Channel(id=103, title="HyperFlow Channel", photo=None, date=None, access_hash=3, username="davidscott")

    assert hasattr(yc_ph_leads, "verify_yc_tg_candidate")
    assert yc_ph_leads.verify_yc_tg_candidate(namesake_user, "Photographer based in Austin", lead_david, "davidscott") is False
    assert yc_ph_leads.verify_yc_tg_candidate(bot_entity, "Building HyperFlow https://hyperflow.ai", lead_david, "davidscott") is False
    assert yc_ph_leads.verify_yc_tg_candidate(chan_entity, "Building HyperFlow https://hyperflow.ai", lead_david, "davidscott") is False
    assert yc_ph_leads.verify_yc_tg_candidate(namesake_user, "Co-founder @ HyperFlow (hyperflow.ai)", lead_david, "davidscott") is True

    assert yc_ph_leads.validate_lead(
        {"name": "YC S26", "tg_username": "u1", "their_angle": "A" * 45, "our_angle": "B", "first_touch": "C" * 50},
        require_tg=False,
    )
    assert yc_ph_leads.validate_lead(
        {"name": "RealApp", "tg_username": "u1", "their_angle": "Недостаточно информации о продукте для питча", "our_angle": "B", "first_touch": "C" * 50},
        require_tg=False,
    )
    assert yc_ph_leads.validate_lead(
        {"name": "RealApp", "tg_username": "u1", "their_angle": "A" * 45, "our_angle": "B", "first_touch": "Hi,"},
        require_tg=False,
    )


# 12. Exact watermark advancement (start_idx + len(batch)) & flock in crm_mcp_push
def test_crm_mcp_push_exact_quota_watermark_and_state_flock(tmp_path, monkeypatch):
    state_file = tmp_path / "crm_mcp_push_state.json"
    cfg_file = tmp_path / "leadgen_config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "enabled": True,
                "auto_push_to_crm": True,
                "total_daily_quota": 3,
                "b2b": {"enabled": True, "touch_mode": "direct", "campaign_daily_limit": 3},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(crm_mcp_push, "STATE_PATH", str(state_file))
    monkeypatch.setattr(crm_mcp_push, "LEGACY_CSV_STATE_PATH", str(tmp_path / "none.json"))

    header = ["Канал", "Приор", "Фит", "Категория", "Название", "Сайт", "Контакт", "Их угол", "Наш угол", "Хук", "Первое касание"]
    fake_rows = [header]
    for idx in range(1, 11):
        fake_rows.append(
            [
                "TG",
                "🟢",
                "🟢",
                "SaaS / AI app",
                f"Startup{idx}",
                f"https://startup{idx}.io",
                f"https://t.me/founder_{idx}",
                f"B2B SaaS workflow automation platform number {idx} for sales teams",
                "SoftUGC angle",
                "",
                f"Hey,\n\nSaw Startup{idx}. Running paid ads gets pricey fast.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}",
            ]
        )

    pushed_usernames: list[str] = []

    def _fake_push(leads, **kwargs):
        pushed_usernames.extend(l["username"] for l in leads)
        return {"ok": True, "processed": len(leads), "created_leads": len(leads), "updated_leads": 0, "enrolled_in_campaign": len(leads)}

    monkeypatch.setattr(crm_mcp_push, "sheet_rows", lambda tab="Все2": fake_rows)
    monkeypatch.setattr(crm_mcp_push, "push_leads_via_mcp", _fake_push)
    monkeypatch.setattr(sys, "argv", ["crm_mcp_push.py", "--tab", "Все2", "--force"])

    assert crm_mcp_push.main() == 0
    assert pushed_usernames == ["founder_1", "founder_2", "founder_3"]
    saved_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_state["last_sent_row"] == 4  # 1 (header) + 3 pushed, NOT 11!

    # Second run picks up founder_4..founder_6 without losing leads!
    assert crm_mcp_push.main() == 0
    assert pushed_usernames == ["founder_1", "founder_2", "founder_3", "founder_4", "founder_5", "founder_6"]
    saved_state2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_state2["last_sent_row"] == 7


# 13. push_discovered_now enforces strict Product-Buyer gate (keep_for_sheet + supply_side)
def test_push_discovered_now_applies_strict_product_buyer_gate(tmp_path, monkeypatch):
    cfg_file = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))

    jsonl_path = state_dir / "batch.final.jsonl"
    records = [
        {"username": "@pure_dev", "name": "Dev Dan", "role": "Dev", "roles": ["Dev"], "company": "SomeRepo", "category": "SaaS / AI app", "their_angle": "Solidity engineer and Rust developer"},
        {"username": "@pure_trader", "name": "Trader Tom", "role": "Trader", "roles": ["Trader"], "company": "AlphaTrades", "category": "Проп трейдинг", "their_angle": "Retail crypto futures trader"},
        {"username": "@agency_owner", "name": "Agency Ann", "role": "Founder", "roles": ["Founder"], "company": "ICODA", "category": "Сервис / агентство", "their_angle": "Web3 marketing agency"},
        {"username": "@gtm_consultant", "name": "Ana CEO", "role": "Founder", "roles": ["Founder"], "company": "ana.fi", "category": "SaaS / AI app", "bio": "Co-Founder, CEO @ ana.fi | GTM Consultancy for AI and Blockchain", "their_angle": "GTM Consultancy for AI and Blockchain"},
        {"username": "@real_saas_ceo", "name": "Sarah Jenkins", "role": "Founder", "roles": ["Founder"], "company": "MetricPulse", "website": "https://metricpulse.io", "category": "SaaS / AI app", "their_angle": "Self-serve product analytics platform for mobile apps"},
    ]
    jsonl_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    conn = connect(tmp_path / "push_now.db")
    init_db(conn)
    res = push_discovered_now(conn)
    assert res["ok"] is True
    assert res["ingested"] == 1
    db_usernames = [r["username"] for r in conn.execute("SELECT username FROM leads").fetchall()]
    assert db_usernames == ["real_saas_ceo"]
    conn.close()


# 14. MCP global cross-campaign dedup, competitor denylist, 1300+ archive, and pre-lead ICP/supply filter
def test_mcp_ingest_global_cross_campaign_dedup_denylist_archive_and_pre_lead_filter(tmp_path):
    conn = connect(tmp_path / "mcp_dedup.db")
    init_db(conn)
    now = utc_now()

    camp1 = create_campaign(
        conn,
        name="Campaign One",
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    # Seed George Levin (@goshalevin) in Campaign One
    _mcp_call(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_id": camp1["id"],
            "leads": [
                {
                    "username": "@goshalevin",
                    "name": "George Levin",
                    "company": "WaitlistHQ",
                    "website": "https://waitlisthq.com",
                    "category": "SaaS",
                    "their_angle": "Referral waitlist builder for SaaS launches",
                    "step1_text": f"Hey,\n\nSaw WaitlistHQ. Paid acquisition gets expensive.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}",
                }
            ],
        },
    )

    # Seed an outbound message in dialogs/messages to @already_contacted
    cur_acc = conn.execute(
        "INSERT INTO accounts (phone, session_path, status, created_at, provider) VALUES ('+15550001', 's.session', 'connected', ?, 'telegram')",
        (now,),
    )
    acc_id = int(cur_acc.lastrowid)
    cur_dlg = conn.execute(
        """
        INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, provider, is_group, unread_count, last_message_at, created_at, updated_at)
        VALUES (?, 'user', 888001, 'already_contacted', 'Contacted User', 'telegram', 0, 0, ?, ?, ?)
        """,
        (acc_id, now, now, now),
    )
    dlg_id = int(cur_dlg.lastrowid)
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 1, 'out', 'Sent pitch', ?, ?, ?)",
        (dlg_id, now, now, now),
    )

    # Seed an archived 1300+ lead
    conn.execute(
        """
        INSERT INTO leads (telegram_user_id, username, name, company, status, note, created_at, updated_at, provider)
        VALUES (888002, 'legacy_1300_founder', 'Legacy Guy', 'OldSaaS', 'new', 'Импорт old_base_1300', ?, ?, 'telegram')
        """,
        (now, now),
    )
    conn.commit()

    leads_count_before = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    valid_pitch = f"Hey,\n\nSaw Product. Paid ads are expensive.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"

    res2 = _mcp_call(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_name": "Campaign Two",
            "enroll_in_campaign": True,
            "leads": [
                # 1) Same person + company on a SECOND Telegram handle (@gosha_lev1n)!
                {"username": "@gosha_lev1n", "name": "George Levin", "company": "WaitlistHQ", "category": "SaaS", "their_angle": "Waitlist SaaS", "step1_text": valid_pitch},
                # 2) Same product domain in another campaign!
                {"username": "@waitlist_cofounder", "name": "Sam Co", "company": "WaitlistHQ App", "website": "https://www.waitlisthq.com/pricing", "category": "SaaS", "their_angle": "Waitlist SaaS", "step1_text": valid_pitch},
                # 3) Already has messages(direction='out')
                {"username": "@already_contacted", "name": "Contacted User", "company": "NewApp", "website": "https://newapp.io", "category": "SaaS", "their_angle": "AI SaaS", "step1_text": valid_pitch},
                # 4) In 1300+ archive
                {"username": "@legacy_1300_founder", "name": "Legacy Guy", "company": "OldSaaS", "category": "SaaS", "their_angle": "AI SaaS", "step1_text": valid_pitch},
                # 5-7) Competitor denylist (crmchat, firsay, matrixreach)
                {"username": "@crmchat_ceo", "name": "Alex", "company": "CRMChat", "website": "https://crmchat.ai", "category": "SaaS", "their_angle": "Telegram CRM", "step1_text": valid_pitch},
                {"username": "@floopi_stg", "name": "Firsay Founder", "company": "firsay", "website": "https://firsay.io", "category": "SaaS", "their_angle": "Web3 leads", "step1_text": valid_pitch},
                {"username": "@matrix_ceo", "name": "Matrix Founder", "company": "MatrixReach", "website": "https://matrixreach.com", "category": "SaaS", "their_angle": "Outreach tool", "step1_text": valid_pitch},
                # 8) Offline non-ICP
                {"username": "@arch_bureau", "name": "Иван", "company": "ArchStudio", "their_angle": "Архитектурное бюро жилых комплексов", "step1_text": valid_pitch},
                # 9) Supply-side freelancer
                {"username": "@ugc_max", "name": "Max", "company": "MaxClips", "their_angle": "UGC Creator | Ambassador | Video creator | Freelancer", "step1_text": valid_pitch},
                # 10) Truncated pitch (<40 chars) & raw cohort tag
                {"username": "@truncated_hi", "name": "Tom", "company": "TomSaaS", "their_angle": "B2B SaaS tool", "step1_text": "Hi,"},
                {"username": "@raw_cohort_tag", "name": "YC W27", "company": "YC W27", "their_angle": "B2B SaaS tool", "step1_text": valid_pitch},
            ],
        },
    )
    assert res2["enrolled_in_campaign"] == 0
    assert res2["created_leads"] == 0
    leads_count_after = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    assert leads_count_after == leads_count_before
    conn.close()


# 15. No false company merge for vacancies/short names, auto-reopen completed campaign, and close local HTTP conn
def test_mcp_no_false_company_merge_for_vacancies_or_short_names_and_reopens_completed_campaign_and_closes_http_conn(tmp_path, monkeypatch):
    db_file = tmp_path / "mcp_vac_http.db"
    conn = connect(db_file)
    init_db(conn)

    # A) Two different employers hiring "Рилсмейкер" must NOT merge by lower(company)
    vac_camp = create_campaign(
        conn,
        name="TG Вакансии",
        status="completed",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    vac_res = _mcp_call(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_id": vac_camp["id"],
            "enroll_in_campaign": True,
            "leads": [
                {
                    "username": "@employer_alpha",
                    "name": "Рилсмейкер (@employer_alpha)",
                    "company": "Рилсмейкер",
                    "category": "AI-контент",
                    "lead_source": "TG Вакансии (Chat 1)",
                    "step1_text": "Добрый день! По вакансии рилсмейкера. Какой объём роликов в месяц планируете?",
                },
                {
                    "username": "@employer_beta",
                    "name": "Рилсмейкер (@employer_beta)",
                    "company": "Рилсмейкер",
                    "category": "AI-контент",
                    "lead_source": "TG Вакансии (Chat 2)",
                    "step1_text": "Добрый день! По вакансии рилсмейкера. В какой нише проект и какой хронометраж?",
                },
            ],
        },
    )
    assert vac_res["created_leads"] == 2
    assert vac_res["enrolled_in_campaign"] == 2

    # C) Campaign in status 'completed' automatically returns to 'running' when valid pending recipients are added
    updated_vac_camp = get_campaign(conn, vac_camp["id"])
    assert updated_vac_camp["status"] == "running"

    # B) Two B2B founders with generic company="SaaS" do not merge, and _find_lead_by_lookup ignores LIKE on <4 chars
    valid_pitch_1 = f"Hey,\n\nSaw AliceApp. Paid acquisition gets expensive.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    valid_pitch_2 = f"Hey,\n\nSaw BobApp. Paid acquisition gets expensive.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    b2b_res = _mcp_call(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_name": "B2B Generic Co Test",
            "enroll_in_campaign": True,
            "leads": [
                {"username": "@alice_saas_1", "name": "Alice Carter", "company": "SaaS", "website": "https://aliceapp.io", "category": "SaaS", "their_angle": "Workflow SaaS for designers", "step1_text": valid_pitch_1},
                {"username": "@bob_saas_2", "name": "Bob Miller", "company": "SaaS", "website": "https://bobapp.io", "category": "SaaS", "their_angle": "Analytics SaaS for podcasters", "step1_text": valid_pitch_2},
            ],
        },
    )
    assert b2b_res["created_leads"] == 2
    assert b2b_res["enrolled_in_campaign"] == 2
    assert _find_lead_by_lookup(conn, name="Ali") is None
    conn.close()

    # D) mcp_http_endpoint closes locally opened connect() in finally
    closed_flags: list[bool] = []
    import app.mcp_server as mcp_mod

    real_connect = connect

    class _TrackedConnWrapper:
        def __init__(self, inner):
            self._inner = inner

        def close(self):
            closed_flags.append(True)
            return self._inner.close()

        def __getattr__(self, item):
            return getattr(self._inner, item)

    monkeypatch.setattr(mcp_mod, "connect", lambda *a, **kw: _TrackedConnWrapper(real_connect(db_file)))
    app = FastAPI()
    register_mcp_routes(app)
    client = TestClient(app)
    http_resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert http_resp.status_code == 200
    assert closed_flags == [True]
