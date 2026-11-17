from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

import app.digest as app_digest
from app.db import connect, init_db, utc_now
from app.leadgen_settings import DEFAULT_B2B_STEPS, save_leadgen_config
from app.outreach import create_campaign
from leadgen import (
    buyer_catalogs,
    creator_external_leads,
    meta_ad_leads,
    supply_side,
    tg_curate,
    tg_enrich_leads,
    tg_llm_qualify,
    vps_autonomous_replenish,
)


def test_sniper_reject_reason_blocks_agencies_pr_dev_oss_hosting_and_forum_builders():
    rejected_samples = [
        "Show HN: Gleanary – free, open-source codebase knowledge graph (MIT license)",
        "FINPR Agency — Web3 & Crypto PR & Growth agency",
        "Marketacross | Trusted by top Web3 names — case studies show how great PR drives growth",
        "6. agentmash — десктоп-приложение, которое синхронизирует AI-кодинг-агентов",
        "Kirito Bot Hosting — managed hosting 24/7 for Telegram bots",
        "[OPEN] MADFRIGHT - Looking for a new experienced 3D modeler to design an asym map",
        "[BUILDER NEEDED] Sci-Fi Roblox lobby map",
        "Boutique talent recruitment for AI startups",
        "Строим отделы продаж B2B под ключ",
        "Taboola expert & Shopify expert | ecommerce helper",
        "IT ментор | сторисмейкер для экспертов",
        "Open-source Mac app under Apache 2.0 | Claude skill | vibecoded ngrok alternative",
        "Lightweight container runtime & движок графов знаний кодовой базы",
        "Account recovery API, ethical hacking & backlink indexing tool",
    ]
    for sample in rejected_samples:
        reason = supply_side.sniper_reject_reason(sample)
        assert reason != "", f"Expected sniper_reject_reason to block: {sample}"

    assert supply_side.sniper_reject_reason("Valid text", category="Инфра") != ""
    assert supply_side.sniper_reject_reason("Valid text", category="Инфраструктура / dev") != ""
    assert supply_side.sniper_reject_reason(
        {"name": "Dev Guy", "project": "SomeTool", "role": "Dev", "category": "SaaS / AI app"}
    ) != ""

    valid_buyers = [
        {
            "name": "Emma Stone",
            "project": "Monetor",
            "bio": "Founder @ Monetor — consumer AI expense tracker & budgeting subscription app ($9.99/mo)",
            "category": "SaaS / AI app",
            "role": "Founder",
        },
        {
            "name": "Liam Vance",
            "project": "FundedPeak",
            "bio": "CEO at FundedPeak — prop trading challenge for futures traders",
            "category": "Проп трейдинг",
            "role": "Founder",
        },
        {
            "name": "Chloe Park",
            "project": "GlowSerum",
            "bio": "Head of Growth at GlowSerum — D2C skincare brand on Shopify",
            "category": "SaaS / AI app",
            "role": "Marketing",
        },
    ]
    for buyer in valid_buyers:
        assert supply_side.sniper_reject_reason(buyer) == ""


def test_sanitize_person_and_company_cleans_hn_numbers_urls_handles_and_human_names():
    _, comp_hn = supply_side.sanitize_person_and_company(
        "Show HN: Monetor – expense tracker for couples",
        "Show HN: Monetor – expense tracker for couples",
        site="https://monetor.app",
    )
    assert comp_hn == "Monetor"

    _, comp_ask = supply_side.sanitize_person_and_company(
        "Ask HN: ClipOrbit - AI reels scheduler",
        "Launch HN: ClipOrbit - AI reels scheduler",
    )
    assert comp_ask == "ClipOrbit"

    _, comp_num = supply_side.sanitize_person_and_company("Alim (@alimwtf)", "6. agentmash")
    assert comp_num == "Agentmash"

    name_open, comp_open = supply_side.sanitize_person_and_company(
        "[OPEN] MADFRIGHT - asym horror",
        "[OPEN] MADFRIGHT",
    )
    assert "OPEN" not in name_open
    assert comp_open == "MADFRIGHT"

    name_oleg, comp_oleg_no_site = supply_side.sanitize_person_and_company(
        "Олег https://freeline.studio (@oleg_fl)",
        "Олег https://freeline.studio",
    )
    assert name_oleg == "Олег"
    assert comp_oleg_no_site == ""

    _, comp_oleg_with_site = supply_side.sanitize_person_and_company(
        "Олег https://freeline.studio",
        "Олег",
        site="https://freeline.studio",
    )
    assert comp_oleg_with_site == "Freeline"

    for human_only in ("Anna Bozina", "Timmy Smith", "Виктор Боргезе"):
        clean_n, clean_c = supply_side.sanitize_person_and_company(
            f"{human_only} (@handle123)",
            human_only,
            site="https://linktr.ee/profile",
        )
        assert clean_n == human_only
        assert clean_c == "", f"Human name leaked as company: {human_only} -> {clean_c}"

        _, clean_c_domain = supply_side.sanitize_person_and_company(
            human_only,
            human_only,
            site="https://pulseboard.io",
        )
        assert clean_c_domain == "Pulseboard"


def test_creator_external_buyer_catalogs_and_meta_ads_sniper_defaults(monkeypatch, tmp_path):
    called_sources: list[str] = []

    monkeypatch.setattr(
        creator_external_leads,
        "github_candidates",
        lambda limit=20: called_sources.append("github") or [
            {"name": "gh-cli", "project": "gh-cli", "project_url": "https://github.com/u/gh-cli", "source": "github"}
        ],
    )
    monkeypatch.setattr(
        creator_external_leads,
        "hn_candidates",
        lambda limit=20: called_sources.append("show_hn") or [
            {"name": "Show HN: Tool", "project": "Tool", "project_url": "https://tool.dev", "source": "show_hn"}
        ],
    )
    monkeypatch.setattr(
        creator_external_leads,
        "hf_candidates",
        lambda limit=20: called_sources.append("huggingface") or [
            {"name": "org/space", "project": "org/space", "project_url": "https://huggingface.co/spaces/org/space", "source": "huggingface_space"}
        ],
    )
    monkeypatch.setattr(
        creator_external_leads,
        "app_stores",
        lambda limit=20: called_sources.append("app_stores") or [
            {
                "name": "AstraFit",
                "project": "AstraFit",
                "project_url": "https://astrafit.app",
                "source_url": "https://apps.apple.com/us/app/astrafit/id1",
                "bio": "Consumer AI fitness & calorie subscription app",
                "links": ["https://astrafit.app"],
                "source": "app_store",
                "icp_match": True,
            }
        ],
    )
    monkeypatch.setattr(
        creator_external_leads,
        "launch_signals",
        lambda limit=20: called_sources.append("launch_signals") or [],
    )

    # 1) Default discover_all() skips github / show_hn / huggingface
    default_rows = creator_external_leads.discover_all(limit=5)
    assert called_sources == ["app_stores", "launch_signals"]
    assert len(default_rows) == 1
    assert default_rows[0]["project"] == "AstraFit"

    # 2) Explicit include_dev_sources=True enables dev sources; direct candidate functions remain callable
    called_sources.clear()
    dev_rows = creator_external_leads.discover_all(limit=5, include_dev_sources=True)
    assert {"github", "show_hn", "huggingface", "app_stores", "launch_signals"}.issubset(set(called_sources))
    assert len(dev_rows) >= 2

    out_cli = tmp_path / "ext_cli.jsonl"
    called_sources.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        ["creator_external_leads.py", "--output", str(out_cli), "--no-qualify"],
    )
    creator_external_leads.main()
    assert "github" not in called_sources
    assert "show_hn" not in called_sources
    assert "huggingface" not in called_sources

    # 3) buyer_catalogs removes devforum from default SOURCES and blocks modeler/builder threads
    source_names = [name for name, _ in buyer_catalogs.SOURCES]
    assert "devforum" not in source_names
    devforum_json = json.dumps(
        {
            "topic_list": {
                "topics": [
                    {
                        "id": 10,
                        "slug": "madfright",
                        "title": "[OPEN] MADFRIGHT - Looking for a new experienced 3D modeler to design an asym map",
                        "excerpt": "Contact @maddev https://madfright.gg",
                    },
                    {
                        "id": 11,
                        "slug": "builder-needed",
                        "title": "[BUILDER NEEDED] Looking for scripter and builder for simulator",
                        "excerpt": "https://simstudio.gg",
                    },
                ]
            }
        }
    )
    assert buyer_catalogs.parse_catalog("devforum", devforum_json, "https://devforum.roblox.com/c/82.json") == []

    # 4) meta_ad_leads covers all 4 golden clusters
    joined_queries = " | ".join(meta_ad_leads.SEARCH_QUERIES).lower()
    assert "roblox" in joined_queries
    assert any(k in joined_queries for k in ("astrology", "coaching", "language", "fitness"))
    assert any(k in joined_queries for k in ("skincare", "supplement", "cosmetic", "household", "detergent", "shopify"))
    assert any(k in joined_queries for k in ("crypto wallet", "prop trading", "portfolio"))


def test_tg_enrich_leads_parallel_threadpool_5s_timeout_and_monetization_xray(monkeypatch):
    assert tg_enrich_leads.TIMEOUT == 5

    def _fake_get(url, headers=None, timeout=5, allow_redirects=True):
        assert timeout == 5
        time.sleep(0.06)
        if "agencyx.io" in url:
            html_body = (
                "<html><head><title>AgencyX — PR Agency</title></head>"
                "<body>Our clients love our case studies. Book a discovery call with our marketing agency.</body></html>"
            )
        elif url.rstrip("/").endswith("/pricing"):
            html_body = "<html><body><h2>Pro Plan $29 /mo</h2><button>Subscribe</button></body></html>"
        elif "indirectprice.io" in url:
            html_body = (
                "<html><head><meta name='description' content='AI voice coaching app'></head>"
                "<body><a href='/pricing'>Pricing</a></body></html>"
            )
        else:
            html_body = (
                "<html><head><meta name='description' content='Consumer AI habit tracker'></head>"
                "<body>Start your free trial — $9.99 per month or download on apps.apple.com/app/id123</body></html>"
            )
        return SimpleNamespace(
            status_code=200,
            ok=True,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html_body,
        )

    monkeypatch.setattr(tg_enrich_leads.requests, "get", _fake_get)
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    raw_leads = []
    for idx in range(10):
        raw_leads.append(
            {
                "name": f"Олег https://app{idx}.io (@oleg_{idx})",
                "username": f"oleg_{idx}",
                "bio": f"Founder building habit app https://app{idx}.io",
                "links": [f"https://app{idx}.io"],
                "project": f"{idx}. habitflow",
                "icp_match": True,
            }
        )
    raw_leads.append(
        {
            "name": "Timmy Smith (@timmys)",
            "username": "timmys",
            "bio": "Founder https://agencyx.io",
            "links": ["https://agencyx.io"],
            "project": "Timmy Smith",
            "icp_match": True,
        }
    )
    raw_leads.append(
        {
            "name": "Show HN: IndirectPrice – AI coach",
            "username": "ind_founder",
            "bio": "Founder https://indirectprice.io",
            "links": ["https://indirectprice.io"],
            "project": "Show HN: IndirectPrice – AI coach",
            "icp_match": True,
        }
    )

    t0 = time.monotonic()
    enriched = tg_enrich_leads.enrich_rows(raw_leads, fetch=True)
    elapsed = time.monotonic() - t0

    # 12 HTTP fetches at 0.06s each sequential would take >= 0.72s; parallel 12 workers finishes way faster
    assert elapsed < 0.55, f"Expected parallel ThreadPoolExecutor enrichment, took {elapsed:.2f}s"
    assert len(enriched) == 12

    first = enriched[0]
    assert first["name"] == "Олег"
    assert first["has_monetization_signal"] is True
    assert first["has_agency_signal"] is False

    agency_row = next(r for r in enriched if r["username"] == "timmys")
    assert agency_row["name"] == "Timmy Smith"
    assert agency_row["project"] == "Agencyx"
    assert agency_row["has_agency_signal"] is True
    assert agency_row["has_monetization_signal"] is False

    indirect_row = next(r for r in enriched if r["username"] == "ind_founder")
    assert indirect_row["project"] == "IndirectPrice"
    assert indirect_row["has_monetization_signal"] is True


def test_tg_llm_qualify_and_curate_icp_score_80_gate_and_quarantine_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_ENABLED", "0")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-fake-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-fake-key")

    # Backward compatibility when icp_score is omitted in mock response
    norm_compat_true = tg_llm_qualify.norm_result(
        {"is_lead": True, "category": "SaaS / AI app", "role": "Founder", "project": "LegacyMockApp"}
    )
    norm_compat_false = tg_llm_qualify.norm_result(
        {"is_lead": False, "category": "SaaS / AI app", "role": "Founder", "project": "NoApp"}
    )
    assert norm_compat_true["icp_score"] == 85
    assert norm_compat_false["icp_score"] == 0

    candidates = [
        {
            "name": "FINPR Agency",
            "username": "finpr_ceo",
            "bio": "FINPR — Web3 PR & Growth agency",
            "project": "FINPR",
            "project_url": "https://finpr.agency",
        },
        {
            "name": "Mid SaaS",
            "username": "mid_saas",
            "bio": "Founder at MidB2B https://midb2b.io",
            "project": "MidB2B",
            "project_url": "https://midb2b.io",
        },
        {
            "name": "Free Hobby",
            "username": "free_dev",
            "bio": "Built a free browser puzzle https://puzzlefree.io",
            "project": "PuzzleFree",
            "project_url": "https://puzzlefree.io",
        },
        {
            "name": "Disguised Shop",
            "username": "agency_site_guy",
            "bio": "CEO @ DevCraft https://devcraft.io",
            "project": "DevCraft",
            "project_url": "https://devcraft.io",
            "has_agency_signal": True,
            "has_monetization_signal": False,
        },
        {
            "name": "Sniper Winner",
            "username": "sniper_winner",
            "bio": "Founder @ AstroCoach — AI astrology subscription app ($14.99/mo) https://astrocoach.app",
            "project": "AstroCoach",
            "project_url": "https://astrocoach.app",
            "has_monetization_signal": True,
            "has_agency_signal": False,
        },
    ]

    # Note: candidate[0] (FINPR) is caught in gate_before_model, so batch has 4 items
    def _mock_llm(batch):
        assert len(batch) == 4
        return [
            {
                "is_lead": True,
                "icp_score": 74,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "MidB2B",
                "project_url": "https://midb2b.io",
                "what_they_do": "Niche enterprise ERP connector",
            },
            {
                "is_lead": True,
                "icp_score": 86,
                "monetization": "none",
                "short_form_fit": True,
                "category": "Gaming / AI",
                "role": "Founder",
                "project": "PuzzleFree",
                "project_url": "https://puzzlefree.io",
                "what_they_do": "Free browser puzzle with zero monetization",
            },
            {
                "is_lead": True,
                "icp_score": 88,
                "monetization": "paid_product",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "DevCraft",
                "project_url": "https://devcraft.io",
                "what_they_do": "Software solutions for clients",
            },
            {
                "is_lead": True,
                "icp_score": 92,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "AstroCoach",
                "project_url": "https://astrocoach.app",
                "what_they_do": "Consumer AI astrology & coaching subscription app",
            },
        ]

    monkeypatch.setattr(tg_llm_qualify, "call_llm_retry", _mock_llm)
    kept, dropped = tg_llm_qualify.qualify_loaded(candidates, batch_size=10)
    assert dropped == 4
    assert [r["username"] for r in kept] == ["sniper_winner"]
    assert kept[0]["icp_score"] == 92
    assert kept[0]["monetization"] == "subscription"

    q_file = tmp_path / "quarantined_unqualified.jsonl"
    assert q_file.is_file()
    q_rows = [json.loads(line) for line in q_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(q_rows) == 4
    q_by_user = {r["username"]: r["quarantine_reason"] for r in q_rows}
    assert "pre_gate" in q_by_user["finpr_ceo"]
    assert "74" in q_by_user["mid_saas"] or "icp_score" in q_by_user["mid_saas"]
    assert "monetization" in q_by_user["free_dev"]
    assert "agency" in q_by_user["agency_site_guy"]

    # Curate re-checks icp_score >= 80, sniper_reject_reason, and sanitize_person_and_company
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    curated = tg_curate.curate_rows(
        [
            {
                "username": "below_threshold",
                "name": "Low Founder",
                "project": "LowApp",
                "project_url": "https://lowapp.io",
                "category": "SaaS / AI app",
                "role": "Founder",
                "icp_score": 79,
                "what_they_do": "Subscription tool",
            },
            {
                "username": "kirito_host",
                "name": "Kirito",
                "project": "Kirito Bot Hosting",
                "project_url": "https://kiritohost.io",
                "category": "SaaS / AI app",
                "role": "Founder",
                "icp_score": 90,
                "what_they_do": "Telegram bot hosting 24/7",
            },
            {
                "username": "monetor_ceo",
                "name": "Олег https://monetor.app (@monetor_ceo)",
                "project": "Show HN: Monetor – expense tracker for couples",
                "project_url": "https://monetor.app",
                "category": "SaaS / AI app",
                "role": "Founder",
                "icp_score": 91,
                "monetization": "subscription",
                "what_they_do": "Consumer AI expense tracker subscription app",
            },
        ]
    )
    assert [r["username"] for r in curated] == ["monetor_ceo"]
    assert curated[0]["name"] == "Олег"
    assert curated[0]["project"] == "Monetor"


def test_vps_autonomous_replenish_check_treg_ready_full_sources_and_direct_x_split(
    tmp_path, monkeypatch
):
    cfg_file = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    scraper_dir = tmp_path / "scraper_sessions"
    scraper_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(scraper_dir))
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    monkeypatch.delenv("TREG_API_KEY", raising=False)
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("MONID_ENRICH", "0")
    monkeypatch.setenv("TREG_CONFIG_PATH", str(tmp_path / "missing_treg_config.json"))

    # 1) check_treg_ready() unit verification
    monkeypatch.setattr(vps_autonomous_replenish, "treg_bin", lambda: None)
    assert vps_autonomous_replenish.check_treg_ready() is False

    fake_treg_bin = tmp_path / "treg"
    fake_treg_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(vps_autonomous_replenish, "treg_bin", lambda: str(fake_treg_bin))
    assert vps_autonomous_replenish.check_treg_ready() is False

    treg_cfg = tmp_path / "treg_config.json"
    treg_cfg.write_text('{"api_key": "team-token"}', encoding="utf-8")
    monkeypatch.setenv("TREG_CONFIG_PATH", str(treg_cfg))
    assert vps_autonomous_replenish.check_treg_ready() is True

    # 2) runner is subprocess.run & not check_treg_ready() -> records missing_treg_cli without crashing
    monkeypatch.setenv("TREG_CONFIG_PATH", str(tmp_path / "none.json"))
    assert vps_autonomous_replenish.check_treg_ready() is False

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    conn.executemany(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
        [("bot_token", "123:TEST", now), ("bot_chat_id", "-1001", now)],
    )
    conn.commit()
    monkeypatch.setattr(app_digest, "_post", lambda url, payload, opener=None: {"ok": True})

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "meta_ads": True,
                    "social_x_reddit": True,
                    "buyer_catalogs": False,
                    "yc_ph": False,
                    "tg_groups": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                },
                "limits": {"external_limit": 0},
            },
            "vacancies": {"enabled": False},
        },
    )
    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=50,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    res_missing_treg = vps_autonomous_replenish.run_autonomous_replenish(conn)
    assert res_missing_treg["ok"] is True
    assert "missing_treg_cli" in (res_missing_treg.get("reasons") or [])
    assert "missing_treg_cli" in (res_missing_treg["low_water_alert"]["reasons"])

    # 3) Full sources + TG vs Direct/X Split (3-A) with treg_ready=True and isolated_session
    monkeypatch.setenv("TREG_CONFIG_PATH", str(treg_cfg))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-fake-key")
    isolated_sess = scraper_dir / "sniper_scraper.session"
    isolated_sess.write_text("isolated-bytes", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(isolated_sess))

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "meta_ads": True,
                    "social_x_reddit": True,
                    "buyer_catalogs": True,
                    "yc_ph": False,
                    "tg_groups": False,
                    "channel_graph": True,
                    "tg_ad_spy": True,
                },
                "limits": {"external_limit": 5},
            },
            "vacancies": {"enabled": False},
        },
    )

    stages_seen: list[str] = []

    def _sniper_runner(cmd, **kwargs):
        cmd_list = [str(x) for x in cmd]
        cmd_str = " ".join(cmd_list)
        stages_seen.append(cmd_str)
        if "meta_ad_leads.py" in cmd_str:
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            out_p.write_text(
                json.dumps(
                    {
                        "id": "meta_ads:rea-video",
                        "name": "rea-video",
                        "username": "",
                        "project": "ReaVideo",
                        "project_url": "https://reavideo.ai",
                        "work_email": "founder@reavideo.ai",
                        "founder_x": "https://x.com/reavideo_ai",
                        "category": "SaaS / AI app",
                        "role": "Founder",
                        "roles": ["Founder"],
                        "what_they_do": "Subscription AI video studio ($29/mo)",
                        "icp_match": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif "tg_llm_qualify.py" in cmd_str:
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            out_p.write_text(
                json.dumps({"username": "@astro_founder", "icp_score": 91}) + "\n",
                encoding="utf-8",
            )
        elif "tg_curate.py" in cmd_str:
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            curated_batch = [
                # Valid TG buyer -> stays in final_jsonl -> goes to CRM leads
                {
                    "username": "@astro_founder",
                    "name": "Sarah Jenkins",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "project": "AstroPulse",
                    "company": "AstroPulse",
                    "project_url": "https://astropulse.app",
                    "website": "https://astropulse.app",
                    "category": "SaaS / AI app",
                    "their_angle": "Consumer AI astrology subscription app ($14.99/mo)",
                    "what_they_do": "Consumer AI astrology subscription app ($14.99/mo)",
                    "icp_score": 91,
                    "monetization": "subscription",
                },
                # Non-TG buyer (like rea-video) -> moves to direct_x_leads.jsonl, REMOVED from final_jsonl & CRM!
                {
                    "username": "",
                    "name": "ReaVideo",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "project": "ReaVideo",
                    "company": "ReaVideo",
                    "project_url": "https://reavideo.ai",
                    "website": "https://reavideo.ai",
                    "work_email": "founder@reavideo.ai",
                    "founder_x": "https://x.com/reavideo_ai",
                    "category": "SaaS / AI app",
                    "their_angle": "Subscription AI video studio ($29/mo)",
                    "what_they_do": "Subscription AI video studio ($29/mo)",
                    "icp_score": 89,
                    "monetization": "subscription",
                },
                # Bot TG handle + LinkedIn -> treated as non-TG, routed to direct_x_leads.jsonl!
                {
                    "username": "@reavideo_support_bot",
                    "name": "Alex Rivera",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "project": "SkinGlow",
                    "company": "SkinGlow",
                    "project_url": "https://skinglow.shop",
                    "founder_linkedin": "https://www.linkedin.com/in/alex-rivera-glow",
                    "category": "SaaS / AI app",
                    "their_angle": "D2C collagen skincare store on Shopify",
                    "what_they_do": "D2C collagen skincare store on Shopify",
                    "icp_score": 86,
                    "monetization": "ecom",
                },
                # Below icp_score 80 -> dropped from both!
                {
                    "username": "",
                    "name": "LowScoreCo",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "project": "LowScoreCo",
                    "project_url": "https://lowscore.io",
                    "work_email": "ceo@lowscore.io",
                    "category": "SaaS / AI app",
                    "what_they_do": "Unclear enterprise tool",
                    "icp_score": 72,
                    "monetization": "paid_product",
                },
            ]
            out_p.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in curated_batch) + "\n",
                encoding="utf-8",
            )
        elif "treg_enrich.py" in cmd_str:
            inp_p = Path(cmd_list[cmd_list.index("--input") + 1])
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            out_p.write_text(inp_p.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res_full = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=_sniper_runner)
    assert res_full["ok"] is True
    for expected_script in (
        "meta_ad_leads.py",
        "treg_social_leads.py",
        "buyer_public.py",
        "buyer_catalogs.py",
        "tg_channel_graph.py",
        "tg_ad_spy.py",
        "treg_enrich.py",
    ):
        assert any(expected_script in s for s in stages_seen), f"Missing stage: {expected_script}"

    # Verify CRM leads table ONLY got @astro_founder, NEVER ReaVideo or SkinGlow without personal TG!
    crm_leads = [dict(r) for r in conn.execute("SELECT username, name, company FROM leads").fetchall()]
    assert len(crm_leads) == 1
    assert crm_leads[0]["username"] == "astro_founder"

    # Verify direct_x_leads.jsonl received ReaVideo and SkinGlow, deduplicated on repeat!
    direct_x_file = state_dir / "direct_x_leads.jsonl"
    assert direct_x_file.is_file()
    dx_rows = [json.loads(line) for line in direct_x_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    dx_projects = [r.get("project") for r in dx_rows]
    assert dx_projects == ["ReaVideo", "SkinGlow"]

    # Re-running split with same rows does not create duplicates in direct_x_leads.jsonl
    dup_final = state_dir / "dup_check.final.jsonl"
    dup_final.write_text(direct_x_file.read_text(encoding="utf-8"), encoding="utf-8")
    vps_autonomous_replenish.split_tg_and_direct_x_leads(dup_final, direct_x_file)
    dx_rows_after = [json.loads(line) for line in direct_x_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(dx_rows_after) == 2
    assert not dup_final.exists()

    conn.close()
