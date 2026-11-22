"""Offline integration and regression tests for Outreach v2 replenish wiring."""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from app.airtable_sync import _select_airtable_push_candidates, is_lead_airtable_excluded
from app.db import connect, init_db
from app.leadgen_settings import save_leadgen_config
from app.outreach import create_campaign
from leadgen import budget_guard, high_yield_veins, vps_autonomous_replenish
from leadgen.language_router import (
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
)
from leadgen.tg_group_admins import GroupAdminContact


def _make_sqlite_session(path: Path, *, auth_key: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = auth_key if auth_key is not None else os.urandom(256)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, auth_key BLOB)")
        db.execute("INSERT INTO sessions (dc_id, auth_key) VALUES (2, ?)", (key,))
        db.commit()
    return path


def _setup_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Path, Path]:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    sessions_dir = tmp_path / "scraper_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-replenish-v2")
    conn = connect(tmp_path / "crm_v2.db")
    init_db(conn)
    return conn, state_dir, sessions_dir


def test_high_yield_veins_whale_icp_gaming_ban_tagging_and_tg_links() -> None:
    all_queries = (
        *high_yield_veins.WHOP_QUERIES,
        *high_yield_veins.TIKTOK_AD_QUERIES,
        *high_yield_veins.TIKTOK_SHOP_QUERIES,
        *high_yield_veins.APP_STORE_VEIN_QUERIES,
        *high_yield_veins.ATS_HIRING_QUERIES,
        *high_yield_veins.D2C_WHATSAPP_QUERIES,
    )
    joined = " ".join(all_queries).lower()
    for banned in ("roblox", "fortnite", "fivem", "uefn", "gamedev"):
        assert banned not in joined
    assert high_yield_veins.TIKTOK_AD_QUERIES[0] == "AI app"

    # 1) App Store vein rejects gaming / Roblox / Fortnite and preserves whale tags + TG link
    rotated_app_kw = high_yield_veins._rotate_for_day(
        high_yield_veins.APP_STORE_VEIN_QUERIES, date(2026, 11, 22), 2
    )[0]
    rows_app, _ = high_yield_veins.collect_scaling_store_apps(
        limit=5,
        day=date(2026, 11, 22),
        apple_search_fn=lambda kw: {
            "results": [
                {"url": "https://apps.apple.com/us/app/blox-creator/id111222333", "title": "Blox Creator Mod"},
                {"url": "https://apps.apple.com/us/app/calm-calorie-ai/id444555666", "title": "CalmCalorie AI"},
            ]
            if kw == rotated_app_kw
            else []
        },
        play_search_fn=lambda kw: {"results": []},
        store_resolver_fn=lambda u: (
            {
                "app_name": "Blox Creator Mod",
                "developer": "Blox Studio",
                "website": "https://roblox-creators.example.com",
                "domain": "roblox-creators.example.com",
                "description": "Best Roblox obby and Fortnite UEFN map companion app.",
                "category": "Gaming",
                "rating_count": 1500,
            }
            if "111222333" in u
            else {
                "app_name": "CalmCalorie AI",
                "developer": "CalmCalorie Labs",
                "website": "https://calmcalorie.ai",
                "domain": "calmcalorie.ai",
                "description": "AI meal scanner & calorie subscription. Community: https://t.me/calmcalorie_club",
                "category": "Health & Fitness",
                "rating_count": 2400,
            }
        ),
        enrich_fn=lambda c, d: {"founder_linkedin": "https://linkedin.com/in/calmcalorie"},
        site_scrape_fn=lambda u: {"founder_x": "https://x.com/calmcalorie", "telegram_group": "https://t.me/calmcalorie_club"},
    )
    assert [r["company"] for r in rows_app] == ["CalmCalorie Labs"]
    assert rows_app[0]["segment"] == "whale"
    assert rows_app[0]["language"] == "en"
    assert rows_app[0]["telegram_group"] == "https://t.me/calmcalorie_club"
    assert "https://t.me/calmcalorie_club" in rows_app[0]["links"]

    # 2) ATS hiring vein rejects Fortnite / FiveM gaming companies via whale_icp_reject_reason
    rows_ats, _ = high_yield_veins.collect_ats_ugc_hiring(
        limit=5,
        day=date(2026, 11, 22),
        search_fn=lambda q: {
            "results": [
                {"url": "https://jobs.ashbyhq.com/fiveblox-gaming", "title": "FiveBlox Gaming Jobs"},
                {"url": "https://jobs.ashbyhq.com/lumina-wellness", "title": "Lumina Wellness Jobs"},
            ]
        },
        ashby_fn=lambda slug: (
            {
                "jobs": [
                    {
                        "title": "UGC Creator Lead",
                        "location": "Remote US",
                        "descriptionPlain": "Grow our FiveM RP server and Roblox game clips.",
                        "absolute_url": "https://jobs.ashbyhq.com/fiveblox-gaming/1",
                    }
                ]
            }
            if "fiveblox" in slug
            else {
                "jobs": [
                    {
                        "title": "Short-Form TikTok Growth Lead",
                        "location": "Remote US",
                        "descriptionPlain": "Scale D2C supplement reels. Join https://t.me/luminacommunity",
                        "absolute_url": "https://jobs.ashbyhq.com/lumina-wellness/2",
                    }
                ]
            }
        ),
        enrich_fn=lambda b, d: {"project_url": "https://luminawellness.co", "work_email": "growth@luminawellness.co"},
        site_scrape_fn=lambda u: {"founder_x": "https://x.com/luminawellness"},
    )
    assert [r["company"] for r in rows_ats] == ["Lumina Wellness"]
    assert rows_ats[0]["segment"] == "whale"
    assert rows_ats[0]["language"] == "en"
    assert rows_ats[0]["telegram_group"] == "https://t.me/luminacommunity"


def test_budget_guard_low_balance_skips_paid_tier2_while_free_layers_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    scraper_sess = _make_sqlite_session(sessions_dir / "signals_ro.session")
    jobs_sess = _make_sqlite_session(sessions_dir / "jobs_reader.session")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    monkeypatch.setenv("HH_API_TOKEN", "hh-test-token")

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "Whales EN — TG",
                "sources": {
                    "tg_groups": False,
                    "buyer_catalogs": False,
                    "meta_ads": True,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "high_yield_veins": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "facebook_groups": False,
                    "linkedin_jobs": False,
                    "tg_identity_bridge": False,
                },
            },
            "vacancies": {
                "enabled": True,
                "daily_limit": 10,
                "vps_scraper_session": str(jobs_sess),
                "sources": {
                    "tg_chats": True,
                    "hh_ru": True,
                    "profi_ru": True,
                    "kwork": False,
                    "upwork": False,
                    "linkedin_jobs": False,
                },
            },
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )

    paid_treg_calls: list[str] = []
    paid_monid_calls: list[str] = []
    alerts: list[str] = []

    def _out_flag(cmd: list[str]) -> Path | None:
        return Path(cmd[cmd.index("--output") + 1]) if "--output" in cmd else None

    def fake_runner(cmd, **kwargs):
        script = Path(cmd[1]).name
        out_p = _out_flag(cmd)
        if script == "meta_ad_leads.py" and out_p is not None:
            out_p.write_text(
                json.dumps(
                    {
                        "id": "meta_ads:hydrapulse",
                        "name": "HydraPulse",
                        "company": "HydraPulse",
                        "project": "HydraPulse",
                        "project_url": "https://hydrapulse.co",
                        "links": ["https://t.me/hydrapulse_club"],
                        "telegram_group": "https://t.me/hydrapulse_club",
                        "source": "meta_ads",
                        "bio": "Active US Meta video ads for hydration electrolytes ($49/mo)",
                        "icp_score": 90,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script in ("tg_enrich_leads.py", "tg_llm_qualify.py", "tg_curate.py") and out_p is not None:
            in_p = Path(cmd[cmd.index("--input") + 1])
            rows = [json.loads(ln) for ln in in_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r.update(
                    {
                        "qualified": True,
                        "llm_qualified": True,
                        "icp_score": 90,
                        "category": "SaaS / AI app",
                        "role": "Founder",
                        "roles": ["Founder"],
                        "what_they_do": "Hydration consumer brand scaling Meta ads",
                    }
                )
            out_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        elif script == "job_monitor.py":
            out_arg = Path(cmd[cmd.index("--out") + 1])
            out_arg.write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        budget_treg_balance_fn=lambda: 0.25,
        budget_monid_balance_fn=lambda: 10.0,
        budget_notify_fn=lambda msg: alerts.append(msg) or True,
        waterfall_http_get_fn=lambda u, **kw: SimpleNamespace(
            status_code=200,
            text='<a href="mailto:founders@hydrapulse.co">Email</a>',
            url=u,
        ),
        waterfall_treg_fn=lambda *a, **kw: paid_treg_calls.append("treg") or {},
        waterfall_monid_fn=lambda *a, **kw: paid_monid_calls.append("monid") or {},
        tg_admin_extractor_fn=lambda _cli, handle: [
            GroupAdminContact(
                username="hydra_founder",
                user_id=777101,
                first_name="Liam",
                last_name="",
                admin_rank="Founder & CEO",
                role_guess="Founder / CEO / Owner",
                group_handle=handle,
            )
        ],
        hh_collector_fn=lambda **kw: {
            "status": "ok",
            "rows": [
                {
                    "id": "hh_free_1",
                    "source": "hh_ru",
                    "lead_source": "hh_ru",
                    "segment": "vacancy",
                    "language": "ru",
                    "provider": "telegram",
                    "title": "Продюсер Reels / Shorts AI",
                    "name": "Ирина",
                    "company": "РоликПро",
                    "username": "irina_rolikpro",
                    "step1_text": "Добрый день! Сделаем серию AI Reels для РоликПро.",
                    "step2_text": "Подскажите, актуален ли тестовый ролик?",
                    "step3_text": "Присылаю кейсы: https://tmxfy.github.io/",
                }
            ],
            "stats": {},
        },
        freelance_monitor_fn=lambda **kw: {
            "status": "ok",
            "rows": [
                {
                    "id": "profi_free_1",
                    "source": "profi_ru",
                    "lead_source": "profi_ru",
                    "segment": "vacancy_assisted",
                    "language": "ru",
                    "provider": "manual",
                    "title": "Нужен AI UGC монтаж для Reels",
                    "company": "Profi Order",
                    "manual_action_url": "https://profi.ru/order/998811",
                    "manual_draft_text": "Добрый день! Соберём AI UGC серию Reels под ключ.",
                }
            ],
            "sources": {"profi_ru": {"status": "ok"}},
        },
        scraper_sessions_dir=sessions_dir,
    )

    assert res["ok"] is True
    assert res["budget_state"]["paid_allowed"] is False
    assert res["budget_state"]["treg_balance_usd"] == 0.25
    assert paid_treg_calls == []
    assert paid_monid_calls == []
    assert budget_guard.get_daily_enrichment_spend(conn) == 0.0
    assert len(alerts) == 1
    assert "job_monitor" in res["stages_run"]
    assert res["tg_group_admins"]["leads_promoted"] == 1
    assert res["wave2_vacancies"]["hh_count"] == 1
    assert res["wave2_vacancies"]["freelance_count"] == 1

    # Free Layer 0 TG group admin promoted Whale lead into Whales EN — TG,
    # while free hh.ru & Profi.ru kept flowing cleanly into CRM.
    whale_row = conn.execute("SELECT segment, language FROM leads WHERE username = 'hydra_founder'").fetchone()
    assert whale_row is not None
    assert whale_row["segment"] == "whale"
    assert whale_row["language"] == "en"

    hh_row = conn.execute("SELECT segment, language FROM leads WHERE username = 'irina_rolikpro'").fetchone()
    assert hh_row is not None
    assert hh_row["segment"] == "vacancy"
    assert hh_row["language"] == "ru"

    profi_row = conn.execute(
        "SELECT segment, language, manual_action_url FROM leads WHERE manual_action_url = 'https://profi.ru/order/998811'"
    ).fetchone()
    assert profi_row is not None
    assert profi_row["segment"] == "vacancy_assisted"

    # Also verify $5.00 daily cap stops paid Tier 2 calls
    for idx in range(5):
        budget_guard.record_enrichment_spend(
            conn, provider="treg", endpoint="phone", lead_key=f"cap_{idx}", cost_usd=1.0, hit=True
        )
    cap_calls: list[str] = []
    wf_out, cap_state = vps_autonomous_replenish.run_contact_waterfall_with_budget(
        conn,
        [{"id": "cap_lead", "company": "Acme", "project_url": "https://acme.ai"}],
        treg_balance_fn=lambda: 20.0,
        monid_balance_fn=lambda: 20.0,
        treg_fn=lambda *a, **kw: cap_calls.append("treg") or {},
        http_get_fn=lambda *a, **kw: SimpleNamespace(status_code=200, text="", url="https://acme.ai"),
    )
    assert cap_state.paid_allowed is False
    assert cap_calls == []
    assert len(wf_out) == 1

    # Also verify positive balance records enrichment spend into `enrichment_ledger`
    conn_ok = connect(tmp_path / "crm_v2_paid.db")
    init_db(conn_ok)
    paid_calls: list[str] = []

    def fake_paid_treg(endpoint: str, **kwargs):
        paid_calls.append(endpoint)
        return {"email": "alex@paidacme.io", "cost_usd": 0.01}

    wf_paid, ok_state = vps_autonomous_replenish.run_contact_waterfall_with_budget(
        conn_ok,
        [
            {
                "id": "paid_lead_1",
                "company": "PaidAcme",
                "project_url": "https://paidacme.io",
                "founder_linkedin": "https://www.linkedin.com/in/alex-paidacme",
                "icp_score": 88,
            }
        ],
        treg_balance_fn=lambda: 12.0,
        monid_balance_fn=lambda: 12.0,
        treg_fn=fake_paid_treg,
        http_get_fn=lambda *a, **kw: SimpleNamespace(status_code=200, text="", url="https://paidacme.io"),
        tg_bridge_fn=lambda r: {},
    )
    assert ok_state.paid_allowed is True
    assert paid_calls
    assert budget_guard.get_daily_enrichment_spend(conn_ok) > 0.0
    assert wf_paid[0]["work_email"] == "alex@paidacme.io"
    conn_ok.close()
    conn.close()


def test_layer2_phone_resolver_checker_session_safety_and_social_manual_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)

    # Create a connected WA account and one active TG outreach account
    outreach_auth_key = b"\x33" * 256
    outreach_sess = _make_sqlite_session(tmp_path / "sessions" / "outreach_main.session", auth_key=outreach_auth_key)
    now_iso = "2026-11-22T12:00:00Z"
    conn.execute(
        "INSERT INTO accounts (phone, session_path, status, health, provider, created_at) VALUES (?, ?, 'connected', 'active', 'telegram', ?)",
        ("+14158210101", str(outreach_sess), now_iso),
    )
    conn.execute(
        "INSERT INTO accounts (phone, session_path, status, health, provider, created_at) VALUES (?, ?, 'connected', 'active', 'whatsapp', ?)",
        ("+14158210102", str(tmp_path / "sessions" / "wa_main.db"), now_iso),
    )
    conn.commit()

    # Non-checker session in scraper_sessions MUST NOT trigger resolve_phones_telegram
    _make_sqlite_session(sessions_dir / "signals_ro.session")
    assert vps_autonomous_replenish.find_phone_checker_session(conn, sessions_dir) is None

    class FakeWAGateway:
        async def check_numbers(self, session_path: str, phones: list[str]) -> list[dict[str, object]]:
            return [
                {"phone": p, "exists": p == "+14158210171", "jid": f"{p.lstrip('+')}@s.whatsapp.net"}
                for p in phones
            ]

    sample_rows = [
        {
            "id": "lead_phone_tg_or_wa",
            "name": "Maya Patel",
            "company": "VeloFit",
            "phone": "+14158210171",
            "icp_score": 88,
            "source": "d2c_whatsapp",
            "language": "en",
        },
        {
            "id": "lead_social_manual",
            "name": "Noah Vance",
            "company": "ClipCraft",
            "founder_x": "https://x.com/noahvance_cc",
            "icp_score": 87,
            "source": "meta_ads",
            "language": "en",
        },
    ]

    routed_no_checker, stats_no_checker = vps_autonomous_replenish.resolve_and_route_phone_leads(
        conn,
        sample_rows,
        wa_gateway=FakeWAGateway(),
        scraper_sessions_dir=sessions_dir,
    )
    assert stats_no_checker["wa_verified"] == 1
    assert stats_no_checker["tg_verified"] == 0
    assert routed_no_checker[0]["provider"] == "whatsapp"
    assert routed_no_checker[0]["whatsapp"] == "https://wa.me/14158210171"
    assert routed_no_checker[1]["segment"] == "social_manual"
    assert routed_no_checker[1]["manual_action_url"] == "https://x.com/noahvance_cc"
    assert routed_no_checker[1]["manual_draft_text"]

    # If a phone_checker_*.session collides with an outreach session auth_key, reject it!
    _make_sqlite_session(sessions_dir / "phone_checker_00.session", auth_key=outreach_auth_key)
    assert vps_autonomous_replenish.find_phone_checker_session(conn, sessions_dir) is None
    (sessions_dir / "phone_checker_00.session").unlink()

    # Dedicated isolated phone_checker_01.session resolves TG and upgrades TG > WA priority
    _make_sqlite_session(sessions_dir / "phone_checker_01.session")

    class FakeTGCheckerClient:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def __call__(self, req: object) -> object:
            self.calls.append(req)
            if type(req).__name__ == "ImportContactsRequest":
                return SimpleNamespace(
                    users=[SimpleNamespace(id=990011, username="velofit_maya", phone="14158210171", bot=False)]
                )
            return SimpleNamespace(users=[])

    tg_cli = FakeTGCheckerClient()
    routed_with_checker, stats_with_checker = vps_autonomous_replenish.resolve_and_route_phone_leads(
        conn,
        sample_rows,
        wa_gateway=FakeWAGateway(),
        tg_checker_client=tg_cli,
        scraper_sessions_dir=sessions_dir,
    )
    assert stats_with_checker["tg_verified"] == 1
    assert routed_with_checker[0]["provider"] == "telegram"
    assert routed_with_checker[0]["username"] == "velofit_maya"
    assert routed_with_checker[0]["telegram_user_id"] == 990011

    # Verify social_manual lead persists to leads table WITHOUT auto-send campaign_recipients enrollment
    ingest_res = vps_autonomous_replenish.ingest_v2_routed_leads(conn, [routed_with_checker[1]])
    assert ingest_res["social_manual_ingested"] == 1
    sm_lead = conn.execute("SELECT segment, manual_action_url FROM leads WHERE company = 'ClipCraft'").fetchone()
    assert sm_lead is not None
    assert sm_lead["segment"] == "social_manual"
    assert sm_lead["manual_action_url"] == "https://x.com/noahvance_cc"
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients").fetchone()[0] == 0
    conn.close()


def test_hlr_pending_wa_routes_without_fake_auto_send_into_social_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    _make_sqlite_session(sessions_dir / "phone_checker_01.session")

    class NotFoundTGChecker:
        async def __call__(self, req: object) -> object:
            return SimpleNamespace(users=[], imported=[])

    # No connected WA account in DB + TG checker returns not_found -> must mark wa_check_pending=True
    rows = [
        {
            "id": "hlr_only_1",
            "name": "Mark Founder",
            "company": "Mark Builds Brands",
            "phone": "+14158210199",
            "icp_score": 90,
            "source": "meta_ads",
            "language": "en",
        },
        {
            "id": "cform_dx_1",
            "name": "Small Newsletter",
            "company": "0dtenews",
            "project_url": "https://0dtenews.com",
            "contact_form_url": "https://0dtenews.com/advertise",
            "icp_score": 82,
            "source": "meta_ads",
            "language": "en",
        },
    ]

    routed, stats = vps_autonomous_replenish.resolve_and_route_phone_leads(
        conn,
        rows,
        wa_gateway=None,
        tg_checker_client=NotFoundTGChecker(),
        scraper_sessions_dir=sessions_dir,
    )
    assert stats["routed_wa_pending"] == 1
    assert routed[0]["wa_check_pending"] is True
    assert routed[0].get("provider") != "whatsapp"

    final_jsonl = state_dir / "final.jsonl"
    dx_jsonl = state_dir / "direct_x_leads.jsonl"
    ms_jsonl = state_dir / "manual_social.jsonl"
    final_jsonl.write_text("\n".join(json.dumps(r) for r in routed) + "\n", encoding="utf-8")

    split_res = vps_autonomous_replenish.split_tg_and_direct_x_leads(
        final_jsonl,
        dx_jsonl,
        manual_social_jsonl=ms_jsonl,
    )
    # Never pushed into tg_wa_kept auto-send; HLR WA lead enters manual_social_jsonl; low-ICP contact form goes to direct_x
    assert split_res["tg_wa_kept"] == 0
    assert split_res["social_manual_added"] == 1
    assert split_res["direct_x_added"] == 1

    ms_rows = [json.loads(ln) for ln in ms_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert ms_rows[0]["segment"] == "social_manual"
    assert ms_rows[0]["manual_action_url"] == "https://wa.me/14158210199"
    assert ms_rows[0]["manual_channel"] == "whatsapp_hlr"
    assert ms_rows[0]["phone"] == "+14158210199"

    dx_rows = [json.loads(ln) for ln in dx_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert dx_rows[0]["contact_form_url"] == "https://0dtenews.com/advertise"

    ingest_res = vps_autonomous_replenish.ingest_v2_routed_leads(conn, ms_rows)
    assert ingest_res["social_manual_ingested"] == 1
    lead_db = conn.execute(
        "SELECT phone, segment, manual_action_url FROM leads WHERE company = 'Mark Builds Brands'"
    ).fetchone()
    assert lead_db is not None
    assert lead_db["phone"] == "+14158210199"
    assert lead_db["segment"] == "social_manual"
    assert lead_db["manual_action_url"] == "https://wa.me/14158210199"
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients").fetchone()[0] == 0

    # Duplicate manual_action_url insert must preserve original draft while backfilling missing email
    orig_draft = ms_rows[0]["manual_draft_text"]
    dup_row = dict(ms_rows[0])
    dup_row["manual_draft_text"] = "OVERWRITTEN DRAFT SHOULD NOT REPLACE"
    dup_row["work_email"] = "mark@ecommercescalingsecrets.com"
    dup_res = vps_autonomous_replenish.ingest_v2_routed_leads(conn, [dup_row])
    assert dup_res["social_manual_ingested"] == 0
    lead_after = conn.execute(
        "SELECT email, manual_draft_text FROM leads WHERE company = 'Mark Builds Brands'"
    ).fetchone()
    assert lead_after["email"] == "mark@ecommercescalingsecrets.com"
    assert lead_after["manual_draft_text"] == orig_draft

    # Even if stale row has wa_check_pending=True AND leftover whatsapp url, split_tg_and_direct_x_leads never emits tg_wa_kept
    stale_pending = {
        "id": "stale_pend_1",
        "name": "Stale Pending",
        "company": "PendingBrand",
        "phone": "+14158210888",
        "whatsapp": "https://wa.me/14158210888",
        "provider": "whatsapp",
        "wa_check_pending": True,
        "icp_score": 89,
        "language": "en",
    }
    routed_stale = vps_autonomous_replenish.route_resolved_lead(stale_pending, {})
    assert not routed_stale.get("whatsapp")
    assert routed_stale.get("provider") != "whatsapp"
    stale_final = state_dir / "stale_final.jsonl"
    stale_ms = state_dir / "stale_ms.jsonl"
    stale_final.write_text(json.dumps(stale_pending) + "\n", encoding="utf-8")
    stale_split = vps_autonomous_replenish.split_tg_and_direct_x_leads(
        stale_final,
        state_dir / "stale_dx.jsonl",
        manual_social_jsonl=stale_ms,
    )
    assert stale_split["tg_wa_kept"] == 0
    assert stale_split["social_manual_added"] == 1
    conn.close()


def test_wave2_vacancies_and_wave3_ru_experts_orchestration_and_airtable_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    _make_sqlite_session(sessions_dir / "jobs_reader.session")

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {"enabled": False},
            "vacancies": {
                "enabled": True,
                "daily_limit": 20,
                "vps_scraper_session": str(sessions_dir / "jobs_reader.session"),
                "sources": {
                    "tg_chats": True,
                    "hh_ru": True,
                    "profi_ru": True,
                    "kwork": True,
                    "upwork": True,
                    "linkedin_jobs": True,
                },
            },
            "ru_experts": {
                "enabled": True,
                "target_campaign": CAMPAIGN_RU_EXPERTS_TG,
                "exclude_from_airtable": True,
                "daily_limit": 15,
            },
        },
        sync_cases_file=False,
    )

    def fake_runner(cmd, **kwargs):
        if Path(cmd[1]).name == "job_monitor.py":
            Path(cmd[cmd.index("--out") + 1]).write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_waterfall(row, **kwargs):
        out = dict(row)
        if out.get("company_domain") == "lumiohealth.io":
            out["username"] = "lumio_growth"
            out["provider"] = "telegram"
        return out

    async def fake_ru_experts(**kwargs):
        return {
            "status": "ok",
            "rows": [
                {
                    "id": "ru_exp_1",
                    "name": "Анна Климова",
                    "username": "anna_klimova_prod",
                    "company": "Климова Психология",
                    "niche": "психология",
                    "what_they_do": "Онлайн-школа и консультации",
                    "their_angle": "Наставничество по самооценке",
                    "segment": "ru_expert",
                    "language": "ru",
                    "exclude_from_airtable": 1,
                    "provider": "telegram",
                }
            ],
            "stats": {"channels_found": 1, "chats_found": 0, "ig_found": 0},
        }

    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        budget_treg_balance_fn=lambda: 15.0,
        budget_monid_balance_fn=lambda: 15.0,
        waterfall_enrich_fn=fake_waterfall,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=lambda **kw: {
            "status": "ok",
            "rows": [
                {
                    "id": "hh_direct_1",
                    "source": "hh_ru",
                    "lead_source": "hh_ru",
                    "segment": "vacancy",
                    "language": "ru",
                    "provider": "telegram",
                    "title": "AI-контент продюсер Reels",
                    "name": "Дмитрий",
                    "company": "БрендАгент",
                    "username": "dmitry_brend",
                },
                {
                    "id": "hh_assisted_1",
                    "source": "hh_ru",
                    "lead_source": "hh_ru",
                    "segment": "vacancy_assisted",
                    "language": "ru",
                    "provider": "manual",
                    "title": "Монтажёр Reels/TikTok удалённо",
                    "company": "МедиаДом",
                    "manual_action_url": "https://hh.ru/vacancy/123456",
                    "manual_draft_text": "Добрый день! Откликаюсь по вакансии Reels.",
                },
            ],
            "stats": {},
        },
        freelance_monitor_fn=lambda **kw: {
            "status": "partial",
            "rows": [
                {
                    "id": "profi_1",
                    "source": "profi_ru",
                    "lead_source": "profi_ru",
                    "segment": "vacancy_assisted",
                    "language": "ru",
                    "provider": "manual",
                    "title": "Снять и смонтировать 15 Reels",
                    "company": "ProfiClient",
                    "manual_action_url": "https://profi.ru/order/771122",
                    "manual_draft_text": "Добрый день! Можем закрыть серией AI Reels.",
                },
                {
                    "id": "upwork_1",
                    "source": "upwork",
                    "lead_source": "upwork",
                    "segment": "vacancy_assisted",
                    "language": "en",
                    "provider": "manual",
                    "title": "Need Short-Form TikTok UGC Editor",
                    "company": "PeakSkin US",
                    "manual_action_url": "https://www.upwork.com/jobs/~01abcdef",
                    "manual_draft_text": "Hi! At SoftUGC we scale short-form UGC for D2C brands.",
                },
            ],
            "sources": {
                "profi_ru": {"status": "ok"},
                "kwork": {"status": "blocked_by_antibot"},
                "upwork": {"status": "ok"},
            },
        },
        linkedin_collector_fn=lambda: [
            {
                "id": "li_1",
                "name": "Lumio Health",
                "company": "Lumio Health",
                "project": "Lumio Health",
                "what_they_do": "Short-form UGC Creator Remote US",
                "job_url": "https://linkedin.com/jobs/view/1001",
                "company_domain": "lumiohealth.io",
                "project_url": "https://lumiohealth.io",
                "source": "linkedin_jobs",
                "segment": "vacancy",
                "language": "en",
            },
            {
                "id": "li_2",
                "name": "Apex Supplements",
                "company": "Apex Supplements",
                "project": "Apex Supplements",
                "what_they_do": "TikTok Creative Producer Remote",
                "job_url": "https://linkedin.com/jobs/view/1002",
                "company_domain": "apexsupps.com",
                "project_url": "https://apexsupps.com",
                "source": "linkedin_jobs",
                "segment": "vacancy",
                "language": "en",
            },
        ],
        ru_experts_fn=fake_ru_experts,
    )

    assert res["ok"] is True
    assert "job_monitor" in res["stages_run"]
    assert "kwork:blocked_by_antibot" in res["reasons"]
    assert res["wave2_vacancies"]["routed_vacancy"] == 2
    assert res["wave2_vacancies"]["routed_vacancy_assisted"] == 4
    assert res["wave3_ru_experts"]["enabled"] is True
    assert res["wave3_ru_experts"]["collected"] == 1

    # Direct RU + EN vacancies enrolled in correct language-split campaigns
    ru_vac_camp = conn.execute(
        "SELECT id, segment, language, copy_profile FROM campaigns WHERE name = ?",
        (CAMPAIGN_VACANCY_RU_TG,),
    ).fetchone()
    en_vac_camp = conn.execute(
        "SELECT id, segment, language, copy_profile FROM campaigns WHERE name = ?",
        (CAMPAIGN_VACANCY_EN_TG,),
    ).fetchone()
    assert ru_vac_camp is not None and ru_vac_camp["copy_profile"] == "vacancy_ru"
    assert en_vac_camp is not None and en_vac_camp["copy_profile"] == "vacancy_en"
    assert (
        conn.execute(
            "SELECT username FROM campaign_recipients WHERE campaign_id = ?",
            (ru_vac_camp["id"],),
        ).fetchone()["username"]
        == "dmitry_brend"
    )
    assert (
        conn.execute(
            "SELECT username FROM campaign_recipients WHERE campaign_id = ?",
            (en_vac_camp["id"],),
        ).fetchone()["username"]
        == "lumio_growth"
    )

    # Assisted jobs stored cleanly in `leads` ONLY, NEVER enrolled into campaign_recipients
    assisted_urls = {
        r["manual_action_url"]
        for r in conn.execute("SELECT manual_action_url FROM leads WHERE segment = 'vacancy_assisted'").fetchall()
    }
    assert assisted_urls == {
        "https://hh.ru/vacancy/123456",
        "https://profi.ru/order/771122",
        "https://www.upwork.com/jobs/~01abcdef",
        "https://linkedin.com/jobs/view/1002",
    }
    for url_u in assisted_urls:
        enrolled_cnt = conn.execute(
            """
            SELECT COUNT(*) FROM campaign_recipients cr
            JOIN leads l ON l.id = cr.lead_id
            WHERE l.manual_action_url = ?
            """,
            (url_u,),
        ).fetchone()[0]
        assert enrolled_cnt == 0

    # Wave 3 RU Experts: draft campaign + exclude_from_airtable=1 on both campaign & lead,
    # and strictly excluded by Airtable candidate selector!
    ru_camp = conn.execute(
        "SELECT id, status, segment, language, exclude_from_airtable FROM campaigns WHERE name = ?",
        (CAMPAIGN_RU_EXPERTS_TG,),
    ).fetchone()
    assert ru_camp is not None
    assert ru_camp["status"] == "draft"
    assert ru_camp["segment"] == "ru_expert"
    assert ru_camp["language"] == "ru"
    assert int(ru_camp["exclude_from_airtable"]) == 1

    ru_lead = dict(
        conn.execute("SELECT * FROM leads WHERE username = 'anna_klimova_prod'").fetchone()
    )
    assert ru_lead["segment"] == "ru_expert"
    assert ru_lead["language"] == "ru"
    assert int(ru_lead["exclude_from_airtable"]) == 1
    assert is_lead_airtable_excluded(ru_lead, dict(ru_camp)) is True
    push_cands = _select_airtable_push_candidates(conn)
    assert all(c.get("username") != "anna_klimova_prod" for c in push_cands)

    # Also verify auth_required on hh_vacancies degrades cleanly without crash
    res_auth = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        budget_treg_balance_fn=lambda: 15.0,
        budget_monid_balance_fn=lambda: 15.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=lambda **kw: {"status": "auth_required", "rows": [], "error": "missing token", "stats": {}},
        freelance_monitor_fn=lambda **kw: {"status": "blocked_by_antibot", "rows": [], "sources": {"upwork": {"status": "blocked_by_antibot"}}},
        linkedin_collector_fn=lambda: [],
        ru_experts_fn=lambda **kw: {"status": "ok", "rows": [], "stats": {}},
    )
    assert res_auth["ok"] is True
    assert "hh_ru:auth_required" in res_auth["reasons"]
    conn.close()


def test_paid_discovery_stages_skipped_when_budget_low_in_real_runner_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When budget_guard reports paid_allowed=False in real subprocess mode, paid CLI stages & LinkedIn are skipped."""
    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    scraper_sess = _make_sqlite_session(sessions_dir / "signals_ro.session")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    monkeypatch.setenv("HH_API_TOKEN", "hh-test-token")
    monkeypatch.setattr(vps_autonomous_replenish, "check_treg_ready", lambda: True)

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "Whales EN — TG",
                "sources": {
                    "tg_groups": False,
                    "buyer_catalogs": True,
                    "high_yield_veins": True,
                    "facebook_groups": True,
                    "meta_ads": True,
                    "social_x_reddit": True,
                    "yc_ph": True,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                },
            },
            "whales": {"enabled": True, "veins": ["whop"], "geos": ["US"]},
            "vacancies": {
                "enabled": True,
                "sources": {
                    "tg_chats": False,
                    "hh_ru": True,
                    "kwork": False,
                    "profi_ru": False,
                    "upwork": False,
                    "linkedin_jobs": True,
                },
            },
            "ru_experts": {"enabled": False},
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )

    def _boom_linkedin(*_a, **_kw):
        raise AssertionError("collect_linkedin_jobs must be skipped when paid_allowed=False")

    monkeypatch.setattr(vps_autonomous_replenish, "collect_linkedin_jobs", _boom_linkedin)

    invoked: list[tuple[list[str], dict[str, str]]] = []

    def spy_subprocess_run(cmd, **kwargs):
        cmd_strs = [str(c) for c in cmd]
        invoked.append((cmd_strs, dict(kwargs.get("env") or {})))
        if "--output" in cmd_strs:
            out_p = Path(cmd_strs[cmd_strs.index("--output") + 1])
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"ok": True, "new_leads": 0, "skipped": 0}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", spy_subprocess_run)

    hh_called = {"n": 0}

    def fake_hh(**_kw):
        hh_called["n"] += 1
        return {"status": "ok", "rows": [], "stats": {}}

    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=None,
        budget_treg_balance_fn=lambda: 0.12,
        budget_monid_balance_fn=lambda: 12.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=fake_hh,
        freelance_monitor_fn=lambda **kw: {"status": "ok", "rows": [], "sources": {}},
    )

    assert res["ok"] is True
    assert hh_called["n"] == 1

    called_scripts = [Path(cmd[1]).name for cmd, _ in invoked if len(cmd) > 1]
    for blocked_script in (
        "buyer_public_leads.py",
        "high_yield_veins.py",
        "facebook_group_leads.py",
        "meta_ad_leads.py",
        "treg_social_leads.py",
    ):
        assert blocked_script not in called_scripts, f"Paid stage {blocked_script} leaked under low budget!"

    for expected_reason in (
        "budget_guard_skipped:buyer_public",
        "budget_guard_skipped:high_yield_veins",
        "budget_guard_skipped:facebook_groups",
        "budget_guard_skipped:meta_ad_leads",
        "budget_guard_skipped:treg_social_leads",
        "budget_guard_skipped:linkedin_jobs",
    ):
        assert expected_reason in res["reasons"], f"Missing {expected_reason} in {res['reasons']}"

    assert len(invoked) >= 1
    for _cmd, env in invoked:
        assert env.get("BUDGET_PAID_ALLOWED") == "0"
    conn.close()


def test_budget_guard_fails_closed_when_probes_unavailable_in_real_runner_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real runner mode never sets allow_unavailable=True; failed balance probe must block paid stages."""
    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    scraper_sess = _make_sqlite_session(sessions_dir / "signals_ro.session")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "Whales EN — TG",
                "sources": {
                    "buyer_catalogs": True,
                    "high_yield_veins": True,
                    "meta_ads": True,
                    "yc_ph": True,
                },
            },
            "whales": {"enabled": True},
            "ru_experts": {"enabled": False},
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )

    invoked_scripts: list[str] = []

    def spy_subprocess_run(cmd, **kwargs):
        cmd_strs = [str(c) for c in cmd]
        if len(cmd_strs) > 1:
            invoked_scripts.append(Path(cmd_strs[1]).name)
        if "--output" in cmd_strs:
            out_p = Path(cmd_strs[cmd_strs.index("--output") + 1])
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"ok": True, "new_leads": 0}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", spy_subprocess_run)

    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=None,
        budget_treg_balance_fn=lambda: None,
        budget_monid_balance_fn=lambda: 15.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=lambda **kw: {"status": "ok", "rows": [], "stats": {}},
        freelance_monitor_fn=lambda **kw: {"status": "ok", "rows": [], "sources": {}},
    )

    assert res["ok"] is True
    assert res["budget_state"]["paid_allowed"] is False
    assert "buyer_public_leads.py" not in invoked_scripts
    assert "high_yield_veins.py" not in invoked_scripts
    assert "meta_ad_leads.py" not in invoked_scripts
    assert "budget_guard_skipped:high_yield_veins" in res["reasons"]
    conn.close()


def test_tier1_tg_identity_bridge_respects_budget_guard_via_replenish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_contact_waterfall_with_budget wraps real bridge_lead_to_telegram to enforce paid_allowed & spend ledger."""
    import leadgen.tg_identity_bridge as tg_bridge_mod

    conn, _state_dir, _sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tg_bridge_mod, "_default_site_fetch", lambda url: "")
    monkeypatch.setattr(tg_bridge_mod, "_default_tme_fetch", lambda nick: None)

    paid_treg_calls: list[str] = []
    paid_monid_calls: list[str] = []

    def spy_treg(endpoint, params=None):
        paid_treg_calls.append(str(endpoint))
        return {
            "ok": True,
            "results": [
                {
                    "url": "https://t.me/acme_founder_dm",
                    "title": "Acme AI Founder",
                    "snippet": "acme-ai.io",
                }
            ],
        }

    def spy_monid(endpoint, params=None):
        paid_monid_calls.append(str(endpoint))
        return None

    row_no_tg = {
        "company": "Acme AI Labs",
        "name": "Jane Doe",
        "project_url": "https://acme-ai.io",
        "x_url": "https://x.com/acme_ai_founder",
        "founder_x": "https://x.com/acme_ai_founder",
        "icp_score": 85,
    }

    def offline_http_get(url, **_kw):
        return SimpleNamespace(status_code=200, text="<html></html>", url=url)

    low_budget = budget_guard.check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 0.20,
        monid_balance_fn=lambda: 10.0,
    )
    assert low_budget.paid_allowed is False
    out_blocked, _st_low = vps_autonomous_replenish.run_contact_waterfall_with_budget(
        conn,
        [dict(row_no_tg)],
        budget_state=low_budget,
        http_get_fn=offline_http_get,
        treg_fn=spy_treg,
        monid_fn=spy_monid,
        tg_bridge_fn=None,
    )
    assert len(out_blocked) == 1
    assert paid_treg_calls == []
    assert paid_monid_calls == []
    assert budget_guard.get_daily_enrichment_spend(conn) == 0.0

    ok_budget = budget_guard.check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.0,
        monid_balance_fn=lambda: 10.0,
    )
    assert ok_budget.paid_allowed is True
    vps_autonomous_replenish.run_contact_waterfall_with_budget(
        conn,
        [dict(row_no_tg)],
        budget_state=ok_budget,
        http_get_fn=offline_http_get,
        treg_fn=spy_treg,
        monid_fn=spy_monid,
        tg_bridge_fn=None,
    )
    assert len(paid_treg_calls) + len(paid_monid_calls) >= 1
    assert budget_guard.get_daily_enrichment_spend(conn) > 0.0
    conn.close()


def test_wave2_triggers_on_v2_vacancy_campaign_when_legacy_completed_and_stops_when_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave 2 checks both legacy TG Вакансии AND v2 vacancy campaigns for trigger & low-water stop."""
    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    scraper_sess = _make_sqlite_session(sessions_dir / "signals_ro.session")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    monkeypatch.setenv("HH_API_TOKEN", "hh-test-token")

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {"enabled": True, "target_campaign_tg": "Whales EN — TG"},
            "whales": {"enabled": False},
            "ru_experts": {"enabled": False},
            "vacancies": {
                "enabled": True,
                "sources": {
                    "tg_chats": False,
                    "hh_ru": True,
                    "kwork": False,
                    "profi_ru": False,
                    "upwork": False,
                    "linkedin_jobs": False,
                },
            },
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )
    # Fill Whales EN — TG above low_water_mark=20 so B2B stage does not trigger
    vps_autonomous_replenish.ingest_v2_routed_leads(
        conn,
        [
            {
                "campaign_name": "Whales EN — TG",
                "name": f"Whale Seed {idx}",
                "username": f"whale_seed_{idx}",
                "company": f"WhaleCo {idx}",
                "segment": "whale",
                "language": "en",
            }
            for idx in range(22)
        ],
    )

    # Legacy TG Вакансии is completed, while v2 CAMPAIGN_VACANCY_RU_TG is running & empty
    create_campaign(
        conn,
        name="TG Вакансии",
        daily_limit=20,
        status="completed",
        language="ru",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! По вакансии для {company}."}],
    )
    create_campaign(
        conn,
        name=CAMPAIGN_VACANCY_RU_TG,
        daily_limit=20,
        status="running",
        segment="vacancy",
        language="ru",
        copy_profile="vacancy_ru",
        leadgen_enabled=True,
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Здравствуйте, {name}! Делаем AI Reels для {company}."}],
    )

    hh_calls = {"n": 0}

    def hh_collector(**_kw):
        hh_calls["n"] += 1
        return {
            "status": "ok",
            "rows": [
                {
                    "id": "hh_wave2_trigger_1",
                    "name": "Мария Вакансия",
                    "company": "Studio HH",
                    "username": "maria_hh_studio",
                    "source": "hh_ru",
                    "lead_source": "hh_ru",
                    "title": "Ищем UGC продюсера",
                    "description": "Поток Reels на заказ",
                    "url": "https://hh.ru/vacancy/9901",
                    "segment": "vacancy",
                    "language": "ru",
                    "provider": "telegram",
                    "icp_score": 82,
                }
            ],
            "stats": {},
        }

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"ok": True, "new_leads": 0}), stderr=""
        )

    res1 = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        low_water_mark=20,
        vac_low_water_mark=20,
        runner=fake_runner,
        budget_treg_balance_fn=lambda: 15.0,
        budget_monid_balance_fn=lambda: 15.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=hh_collector,
        freelance_monitor_fn=lambda **kw: {"status": "ok", "rows": [], "sources": {}},
    )
    assert res1["ok"] is True
    assert hh_calls["n"] == 1
    assert res1["wave2_vacancies"]["hh_count"] == 1

    ingested_row = conn.execute(
        """
        SELECT c.name FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        JOIN leads l ON l.id = cr.lead_id
        WHERE l.username = 'maria_hh_studio'
        """
    ).fetchone()
    assert ingested_row is not None
    assert ingested_row["name"] == CAMPAIGN_VACANCY_RU_TG

    # Phase 2: Top up CAMPAIGN_VACANCY_RU_TG past vac_low_water_mark=20 -> Wave 2 stops re-triggering
    vps_autonomous_replenish.ingest_v2_routed_leads(
        conn,
        [
            {
                "campaign_name": CAMPAIGN_VACANCY_RU_TG,
                "name": f"Вакансия Сид {idx}",
                "username": f"vac_seed_ru_{idx}",
                "company": f"ВакансияКо {idx}",
                "source": "hh_ru",
                "segment": "vacancy",
                "language": "ru",
            }
            for idx in range(21)
        ],
    )

    res2 = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        low_water_mark=20,
        vac_low_water_mark=20,
        runner=fake_runner,
        budget_treg_balance_fn=lambda: 15.0,
        budget_monid_balance_fn=lambda: 15.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=hh_collector,
        freelance_monitor_fn=lambda **kw: {"status": "ok", "rows": [], "sources": {}},
    )
    assert res2["ok"] is True
    assert hh_calls["n"] == 1
    assert int(res2["pending_vacancies"]) >= 20
    assert not res2["wave2_vacancies"]
    conn.close()


def test_config_wiring_whales_geos_whop_min_pool_ig_handles_and_prompt_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies all 4 Blocker 3b config keys propagate cleanly end-to-end without hollow pipes."""
    def fake_whop_search(query, num=10):
        return {
            "results": [
                {
                    "url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/discover/fffe2df1",
                    "title": "Small Brand — $2,500 budget Content Rewards",
                    "snippet": "Budget: $2,500. CPM: $1 per 1,000 views. DM https://t.me/small_founder",
                },
                {
                    "url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/discover/fffe2df2",
                    "title": "Earn $1 per 1000 Views Clipping MegaWhale by Plox Media",
                    "snippet": "Budget: $7,500. CPM: $1 per 1,000 views. DM https://t.me/megawhale_boss",
                },
            ]
        }

    whop_rows, _whop_stats = high_yield_veins.collect_whop_content_rewards(
        limit=10,
        min_pool_usd=5000,
        search_fn=fake_whop_search,
        enrich_fn=lambda comp, dom: {
            "project_url": "https://megawhale.ai",
            "founder_linkedin": "https://www.linkedin.com/in/megawhale-ceo",
            "work_email": "team@megawhale.ai",
        },
        site_scrape_fn=lambda url: {"founder_x": "https://x.com/megawhaleapp"},
    )
    assert len(whop_rows) == 1
    assert whop_rows[0]["pool_usd"] == 7500
    assert "MegaWhale" in whop_rows[0]["project"]

    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    scraper_sess = _make_sqlite_session(sessions_dir / "signals_ro.session")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(scraper_sess))
    monkeypatch.setenv("HH_API_TOKEN", "hh-test-token")

    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": "Whales EN — TG",
                "sources": {
                    "meta_ads": True,
                    "high_yield_veins": True,
                    "whop_content_rewards": True,
                    "tg_groups": False,
                    "buyer_catalogs": False,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "facebook_groups": False,
                },
            },
            "whales": {
                "enabled": True,
                "veins": ["whop"],
                "geos": ["GB", "CA"],
                "whop_min_pool_usd": 6500,
            },
            "vacancies": {
                "enabled": True,
                "editable_prompt_override": "CUSTOM OVERRIDE FOR {role} AT {company} :: {showcase_url}",
                "sources": {
                    "tg_chats": False,
                    "hh_ru": True,
                    "kwork": False,
                    "profi_ru": False,
                    "upwork": True,
                    "linkedin_jobs": False,
                },
            },
            "ru_experts": {
                "enabled": True,
                "min_subscribers": 5000,
                "ig_handles": ["@expert_one", "https://instagram.com/expert_two/"],
            },
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )

    seen_cmds: list[list[str]] = []

    def spy_runner(cmd, **kwargs):
        cmd_list = [str(c) for c in cmd]
        seen_cmds.append(cmd_list)
        if "--output" in cmd_list:
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"ok": True, "new_leads": 0}), stderr=""
        )

    captured: dict[str, object] = {}

    def spy_hh(**kw):
        captured["hh_template"] = kw.get("custom_draft_template")
        return {"status": "ok", "rows": [], "stats": {}}

    def spy_freelance(**kw):
        captured["freelance_templates"] = kw.get("custom_template_by_source")
        return {
            "status": "ok",
            "rows": [
                {
                    "id": "upw_cfg_1",
                    "name": "Liam Upwork",
                    "company": "Acme SaaS",
                    "source": "upwork",
                    "lead_source": "upwork",
                    "role": "UGC Producer",
                    "title": "UGC Producer Needed",
                    "url": "https://www.upwork.com/jobs/~01xyz",
                    "manual_action_url": "https://www.upwork.com/jobs/~01xyz",
                    "segment": "vacancy_assisted",
                    "language": "en",
                    "provider": "manual",
                    "icp_score": 80,
                }
            ],
            "sources": {"upwork": {"status": "ok"}},
        }

    def spy_ru_experts(**kw):
        captured["ru_ig_handles"] = kw.get("ig_handles")
        return {"status": "ok", "rows": [], "stats": {}}

    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=spy_runner,
        budget_treg_balance_fn=lambda: 20.0,
        budget_monid_balance_fn=lambda: 20.0,
        scraper_sessions_dir=sessions_dir,
        hh_collector_fn=spy_hh,
        freelance_monitor_fn=spy_freelance,
        ru_experts_fn=spy_ru_experts,
    )
    assert res["ok"] is True

    hyv_cmd = next((c for c in seen_cmds if len(c) > 1 and c[1].endswith("high_yield_veins.py")), None)
    assert hyv_cmd is not None and "--whop-min-pool-usd" in hyv_cmd
    assert hyv_cmd[hyv_cmd.index("--whop-min-pool-usd") + 1] == "6500"

    meta_cmd = next((c for c in seen_cmds if len(c) > 1 and c[1].endswith("meta_ad_leads.py")), None)
    assert meta_cmd is not None and "--country" in meta_cmd
    assert meta_cmd[meta_cmd.index("--country") + 1] == "GB,CA"

    assert captured["ru_ig_handles"] == ["expert_one", "expert_two"]
    assert captured["hh_template"] == "CUSTOM OVERRIDE FOR {role} AT {company} :: {showcase_url}"
    assert "upwork" in (captured["freelance_templates"] or {})

    upwork_row = conn.execute(
        "SELECT manual_draft_text FROM leads WHERE manual_action_url = 'https://www.upwork.com/jobs/~01xyz'"
    ).fetchone()
    assert upwork_row is not None
    assert "CUSTOM OVERRIDE FOR UGC Producer Needed AT Acme SaaS" in upwork_row["manual_draft_text"]
    conn.close()


def test_ingest_v2_routed_leads_handles_middle_duplicate_via_lead_id_by_index_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensures middle skipped duplicate never shifts v2 metadata onto adjacent leads (both with & without lead_id_by_index)."""
    import app.mcp_server as app_mcp_server

    conn, _state_dir, _sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    camp = create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=30,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        leadgen_enabled=True,
    )
    cid = int(camp["id"])

    # Pre-seed an existing duplicate lead row with distinct segment/pool_usd
    conn.execute(
        """
        INSERT INTO leads(
            name, username, company, segment, language, status, provider, created_at, updated_at
        ) VALUES ('Existing Dup', 'dup_mid_user', 'Dup Corp', 'b2b', 'en', 'new', 'telegram', datetime('now'), datetime('now'))
        """
    )
    dup_lid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO campaign_recipients(campaign_id, lead_id, current_step, status, created_at, updated_at) VALUES (?, ?, 0, 'pending', datetime('now'), datetime('now'))",
        (cid, dup_lid),
    )
    conn.commit()

    def _insert_lead_row(conn_arg, item):
        cur = conn_arg.execute(
            """
            INSERT INTO leads(name, username, company, status, provider, created_at, updated_at)
            VALUES (?, ?, ?, 'new', 'telegram', datetime('now'), datetime('now'))
            """,
            (item["name"], item["username"], item.get("company")),
        )
        new_id = int(cur.lastrowid)
        conn_arg.execute(
            "INSERT INTO campaign_recipients(campaign_id, lead_id, current_step, status, created_at, updated_at) VALUES (?, ?, 0, 'pending', datetime('now'), datetime('now'))",
            (cid, new_id),
        )
        return new_id

    # Path A: MCP returns explicit lead_id_by_index where middle item is None (skipped duplicate)
    def fake_mcp_with_index(conn_arg, _tool_name, payload):
        ids_by_idx: list[int | None] = []
        created_ids: list[int] = []
        for item in payload["leads"]:
            if item.get("username") == "dup_mid_user":
                ids_by_idx.append(None)
                continue
            new_id = _insert_lead_row(conn_arg, item)
            ids_by_idx.append(new_id)
            created_ids.append(new_id)
        conn_arg.commit()
        return {
            "ok": True,
            "enrolled_in_campaign": len(created_ids),
            "lead_ids": created_ids,
            "lead_id_by_index": ids_by_idx,
        }

    monkeypatch.setattr(app_mcp_server, "call_mcp_tool_handler", fake_mcp_with_index)

    batch_a = [
        {
            "name": "Lead Zero",
            "username": "lead_zero_a",
            "company": "Zero Co",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 8000,
            "manual_draft_text": "DRAFT_ZERO",
        },
        {
            "name": "Dup Mid",
            "username": "dup_mid_user",
            "company": "Dup Corp",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 99999,
            "manual_draft_text": "SHOULD_NOT_OVERWRITE",
        },
        {
            "name": "Lead Two",
            "username": "lead_two_a",
            "company": "Two Co",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 12000,
            "manual_draft_text": "DRAFT_TWO",
        },
    ]
    res_a = vps_autonomous_replenish.ingest_v2_routed_leads(conn, batch_a)
    assert res_a["whales_ingested"] == 2

    row0 = conn.execute("SELECT segment, pool_usd, manual_draft_text FROM leads WHERE username = 'lead_zero_a'").fetchone()
    row_dup = conn.execute("SELECT segment, pool_usd, manual_draft_text FROM leads WHERE username = 'dup_mid_user'").fetchone()
    row2 = conn.execute("SELECT segment, pool_usd, manual_draft_text FROM leads WHERE username = 'lead_two_a'").fetchone()
    assert row0["segment"] == "whale" and int(row0["pool_usd"]) == 8000 and row0["manual_draft_text"] == "DRAFT_ZERO"
    assert row_dup["segment"] == "b2b" and row_dup["pool_usd"] is None and row_dup["manual_draft_text"] is None
    assert row2["segment"] == "whale" and int(row2["pool_usd"]) == 12000 and row2["manual_draft_text"] == "DRAFT_TWO"

    # Path B: Legacy MCP WITHOUT lead_id_by_index (only short lead_ids=[id0, id2]) -> uses identity lookup fallback!
    def fake_mcp_legacy_short_ids(conn_arg, _tool_name, payload):
        created_ids: list[int] = []
        for item in payload["leads"]:
            if item.get("username") == "skipped_mid_ghost":
                continue
            created_ids.append(_insert_lead_row(conn_arg, item))
        conn_arg.commit()
        return {"ok": True, "enrolled_in_campaign": len(created_ids), "lead_ids": created_ids}

    monkeypatch.setattr(app_mcp_server, "call_mcp_tool_handler", fake_mcp_legacy_short_ids)

    batch_b = [
        {
            "name": "Lead Zero B",
            "username": "lead_zero_b",
            "company": "Zero B",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 9000,
            "manual_draft_text": "DRAFT_ZERO_B",
        },
        {
            "name": "Skipped Ghost Mid",
            "username": "skipped_mid_ghost",
            "company": "Ghost Corp",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 77777,
            "manual_draft_text": "GHOST_MUST_NOT_SHIFT",
        },
        {
            "name": "Lead Two B",
            "username": "lead_two_b",
            "company": "Two B",
            "source": "whop_content_rewards",
            "segment": "whale",
            "language": "en",
            "pool_usd": 11000,
            "manual_draft_text": "DRAFT_TWO_B",
        },
    ]
    res_b = vps_autonomous_replenish.ingest_v2_routed_leads(conn, batch_b)
    assert res_b["whales_ingested"] == 2

    row0b = conn.execute("SELECT segment, pool_usd, manual_draft_text FROM leads WHERE username = 'lead_zero_b'").fetchone()
    row2b = conn.execute("SELECT segment, pool_usd, manual_draft_text FROM leads WHERE username = 'lead_two_b'").fetchone()
    assert row0b["segment"] == "whale" and int(row0b["pool_usd"]) == 9000 and row0b["manual_draft_text"] == "DRAFT_ZERO_B"
    assert row2b["segment"] == "whale" and int(row2b["pool_usd"]) == 11000 and row2b["manual_draft_text"] == "DRAFT_TWO_B"
    conn.close()


def test_multi_phone_checker_failover_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.errors import FloodWaitError

    conn, _state_dir, sessions_dir = _setup_isolated_env(tmp_path, monkeypatch)
    outreach_auth_key = os.urandom(256)
    outreach_sess = _make_sqlite_session(tmp_path / "sessions" / "outreach_main.session", auth_key=outreach_auth_key)
    conn.execute(
        "INSERT INTO accounts (phone, session_path, status, health, provider, created_at) VALUES (?, ?, 'connected', 'active', 'telegram', ?)",
        ("+15550009911", str(outreach_sess), "2026-11-22T12:00:00Z"),
    )
    conn.commit()

    # Colliding phone_checker_0 must be skipped, while distinct phone_checker_1 & phone_checker_2 stay active
    _make_sqlite_session(sessions_dir / "phone_checker_0.session", auth_key=outreach_auth_key)
    sess1 = sessions_dir / "phone_checker_1.session"
    sess2 = sessions_dir / "phone_checker_2.session"
    _make_sqlite_session(sess1, auth_key=os.urandom(256))
    _make_sqlite_session(sess2, auth_key=os.urandom(256))

    found_sessions = vps_autonomous_replenish.find_phone_checker_sessions(conn, sessions_dir)
    assert found_sessions == [sess1, sess2]
    assert vps_autonomous_replenish.find_phone_checker_session(conn, sessions_dir) == sess1

    class CheckerOneFloodOnSecond:
        def __init__(self) -> None:
            self.imported_batches: list[list[str]] = []

        async def __call__(self, req: object) -> object:
            if type(req).__name__ == "ImportContactsRequest":
                phones = [str(getattr(c, "phone", "")) for c in getattr(req, "contacts", [])]
                self.imported_batches.append(phones)
                if phones == ["+14158210171"]:
                    return SimpleNamespace(
                        users=[SimpleNamespace(id=990171, username="founder_one", phone="14158210171", bot=False)]
                    )
                raise FloodWaitError(request=req, capture=120)
            return SimpleNamespace(users=[])

    class CheckerTwoFallback:
        def __init__(self) -> None:
            self.imported_batches: list[list[str]] = []

        async def __call__(self, req: object) -> object:
            if type(req).__name__ == "ImportContactsRequest":
                phones = [str(getattr(c, "phone", "")) for c in getattr(req, "contacts", [])]
                self.imported_batches.append(phones)
                if phones == ["+14158210172"]:
                    return SimpleNamespace(
                        users=[SimpleNamespace(id=990172, username="founder_two", phone="14158210172", bot=False)]
                    )
            return SimpleNamespace(users=[])

    cli1 = CheckerOneFloodOnSecond()
    cli2 = CheckerTwoFallback()
    rows = [
        {
            "id": "rot_1",
            "name": "Founder One",
            "company": "Brand One",
            "phone": "+14158210171",
            "icp_score": 91,
            "source": "meta_ads",
            "language": "en",
        },
        {
            "id": "rot_2",
            "name": "Founder Two",
            "company": "Brand Two",
            "phone": "+14158210172",
            "icp_score": 92,
            "source": "meta_ads",
            "language": "en",
        },
    ]

    routed, stats = vps_autonomous_replenish.resolve_and_route_phone_leads(
        conn,
        rows,
        cfg={"whales": {"phone_tg_checker_batch_size": 1}},
        wa_gateway=None,
        tg_checker_clients={
            "phone_checker_1.session": cli1,
            "phone_checker_2.session": cli2,
        },
        scraper_sessions_dir=sessions_dir,
    )

    assert cli1.imported_batches == [["+14158210171"], ["+14158210172"]]
    assert cli2.imported_batches == [["+14158210172"]]
    assert stats["phones_checked"] == 2
    assert stats["tg_verified"] == 2
    assert stats["routed_telegram"] == 2
    assert [r["provider"] for r in routed] == ["telegram", "telegram"]
    assert [r["username"] for r in routed] == ["founder_one", "founder_two"]
    conn.close()


