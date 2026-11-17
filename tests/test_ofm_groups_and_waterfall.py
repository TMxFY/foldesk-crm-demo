from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from app.db import apply_ofm_and_top_veins_v8, connect, init_db
from app.hygiene import append_ofm_review_lead, read_ofm_review_leads
from app.leadgen_settings import get_leadgen_config, get_leadgen_status, push_discovered_now
from app.main import create_app
from app.mcp_server import call_mcp_tool_handler
from leadgen import high_yield_veins


def test_ofm_review_isolation_api_mcp_and_v8_migration(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")

    db_path = tmp_path / "crm_ofm_test.db"
    conn = connect(db_path)
    init_db(conn)

    # Seed legacy v7 config without new sources, clear v8 marker, and verify v8 idempotent upgrade
    conn.execute("DELETE FROM kv_settings WHERE key = 'ofm_and_top_veins_v8'")
    conn.execute(
        """
        INSERT OR REPLACE INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_config', ?, '2026-11-17T00:00:00')
        """,
        (
            json.dumps(
                {
                    "b2b": {
                        "sources": {
                            "tg_groups": False,
                            "meta_ads": True,
                            "social_x_reddit": True,
                        }
                    }
                }
            ),
        ),
    )
    conn.commit()
    res_v8 = apply_ofm_and_top_veins_v8(conn)
    assert res_v8["already_applied"] is False
    assert apply_ofm_and_top_veins_v8(conn)["already_applied"] is True
    cfg = get_leadgen_config(conn)
    for k in ("tg_groups", "ofm_groups", "facebook_groups", "app_stores", "ats_hiring", "ph_detailed"):
        assert cfg["b2b"]["sources"][k] is True
    assert "FranceOFM" in cfg["b2b"]["ofm_tg_groups"]

    # Rejects bots / contactless OFM entries, deduplicates personal @handles
    assert append_ofm_review_lead(state_dir, {"username": "GHClone4Bot", "name": "Group Help"}) is False
    assert append_ofm_review_lead(state_dir, {"username": "", "name": "NoContact"}) is False
    assert append_ofm_review_lead(
        state_dir,
        {
            "name": "Amelie",
            "username": "@ameliemlh",
            "source": "ofm_tg",
            "source_group": "@FranceOFM",
            "ofm_tier": "traffic_buyer",
            "icp_score": 92,
            "market": "US",
            "channels": ["Instagram"],
            "signal_summary": "RECRUTEMENT VA INSTAGRAM — US market agency hiring",
        },
    ) is True
    # Duplicate handle skipped
    assert append_ofm_review_lead(state_dir, {"username": "ameliemlh", "name": "Amelie Dup"}) is False
    # Also write an OFM-tagged row into a normal .final.jsonl to test defense-in-depth exclusion
    rogue_final = state_dir / "raw_20261117_000001.final.jsonl"
    rogue_final.write_text(
        json.dumps(
            {
                "name": "OFM Spy",
                "username": "rogue_ofm_buyer",
                "source": "ofm_tg",
                "ofm_tier": "traffic_buyer",
                "project": "OFM Agency",
                "project_url": "https://ofm-agency.example",
                "icp_score": 95,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    push_out = push_discovered_now(conn)
    assert push_out["ingested"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients").fetchone()[0] == 0

    status = get_leadgen_status(conn)
    assert status["ofm_review_count"] == 1
    assert status["recent_ofm_review_leads"][0]["username"] == "ameliemlh"

    mcp_res = call_mcp_tool_handler(conn, "list_ofm_review_leads", {"limit": 10, "query": "amelie"})
    assert mcp_res["total"] == 1
    assert mcp_res["items"][0]["tg_url"] == "https://t.me/ameliemlh"

    app = create_app(conn=conn)
    client = TestClient(app)
    resp = client.get("/api/leadgen/ofm-review?query=FranceOFM")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["username"] == "ameliemlh"
    conn.close()


def test_top3_high_yield_veins_app_stores_ats_and_ph_detailed() -> None:
    # 1) App Stores: keeps 300..30000 ratings, rejects too small (<300) and megacorp (>30000)
    store_rows, _ = high_yield_veins.collect_scaling_store_apps(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        apple_search_fn=lambda term: (
            {
                "results": [
                    {"trackViewUrl": "https://apps.apple.com/us/app/tiny-habit/id101", "trackName": "TinyHabit"},
                    {"trackViewUrl": "https://apps.apple.com/us/app/calai-pro/id102", "trackName": "CalAI Pro"},
                    {"trackViewUrl": "https://apps.apple.com/us/app/megacorp-fit/id103", "trackName": "MyFitnessPal"},
                ]
            }
            if term == high_yield_veins.collect_scaling_store_apps.__globals__["APP_STORE_VEIN_QUERIES"][0]
            or True
            else {"results": []}
        ),
        play_search_fn=lambda term: {"results": []},
        store_resolver_fn=lambda u: (
            {"app_name": "TinyHabit", "developer": "TinyCo", "website": "https://tinyhabit.app", "domain": "tinyhabit.app", "rating_count": 85, "is_megacorp": False}
            if "101" in u
            else (
                {"app_name": "CalAI Pro", "developer": "CalLabs Inc", "website": "https://callabs.ai", "domain": "callabs.ai", "email": "founders@callabs.ai", "rating_count": 4200, "is_megacorp": False}
                if "102" in u
                else {"app_name": "BigApp", "developer": "Under Armour", "website": "https://myfitnesspal.com", "domain": "myfitnesspal.com", "rating_count": 85000, "is_megacorp": True}
            )
        ),
        enrich_fn=lambda b, d: {"founder_x": "https://x.com/callabs_ai"},
        site_scrape_fn=lambda u: {},
    )
    assert len(store_rows) == 1
    assert store_rows[0]["source"] == "app_stores"
    assert store_rows[0]["project_url"] == "https://callabs.ai"
    assert store_rows[0]["rating_count"] == 4200

    # 2) ATS Hiring: Ashby/Greenhouse board with UGC/Short-Form role, rejects >150 open roles and CIS
    ats_rows, _ = high_yield_veins.collect_ats_ugc_hiring(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        search_fn=lambda q: (
            {
                "results": [
                    {"url": "https://jobs.ashbyhq.com/pulseapp/123", "title": "PulseApp Hiring"},
                    {"url": "https://jobs.ashbyhq.com/megabank/999", "title": "MegaBank"},
                    {"url": "https://job-boards.greenhouse.io/cissocial/456", "title": "CIS Co"},
                    {"url": "https://pulseapp.ai", "title": "PulseApp Official"},
                ]
            }
            if "ashbyhq" in q
            else {"results": [{"url": "https://pulseapp.ai", "title": "PulseApp Official"}]}
        ),
        ashby_fn=lambda slug: (
            {
                "jobs": [
                    {"title": "Short-Form Content Lead (TikTok/UGC)", "location": "Remote - US", "absolute_url": "https://jobs.ashbyhq.com/pulseapp/123"},
                    {"title": "Backend Engineer", "location": "Remote - US"},
                ]
            }
            if slug == "pulseapp"
            else {"jobs": [{"title": "UGC Lead", "location": "US"}] * 160}
        ),
        greenhouse_fn=lambda slug: {
            "jobs": [{"title": "UGC Creator Manager", "location": "Moscow, Russia"}]
        },
        enrich_fn=lambda b, d: {"work_email": "growth@pulseapp.ai"},
        site_scrape_fn=lambda u: {},
    )
    assert len(ats_rows) == 1
    assert ats_rows[0]["source"] == "ats_hiring"
    assert ats_rows[0]["project_url"] == "https://pulseapp.ai"
    assert ats_rows[0]["work_email"] == "growth@pulseapp.ai"

    # 3) Product Hunt detailed: skips open-source dev CLI, admits consumer AI app with domain + X
    ph_rows, _ = high_yield_veins.collect_producthunt_detailed(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        ph_fetch_fn=lambda: {
            "launches": [
                {
                    "name": "DevGraph CLI",
                    "tagline": "Open-source codebase knowledge graph MIT license CLI",
                    "website": "https://github.com/devgraph/cli",
                },
                {
                    "name": "SleepSync AI",
                    "tagline": "AI sleep & recovery tracker for busy founders",
                    "website": "https://sleepsync.app",
                    "ph_url": "https://www.producthunt.com/posts/sleepsync-ai",
                    "maker_x": "https://x.com/sleepsync_ceo",
                },
            ]
        },
        enrich_fn=lambda b, d: {"work_email": "team@sleepsync.app"},
        site_scrape_fn=lambda u: {},
    )
    assert len(ph_rows) == 1
    assert ph_rows[0]["source"] == "ph_detailed"
    assert ph_rows[0]["project"] == "SleepSync AI"
    assert ph_rows[0]["founder_x"] == "https://x.com/sleepsync_ceo"


def test_contact_waterfall_two_tier_ranking_crawl_and_budget() -> None:
    from types import SimpleNamespace
    from leadgen import contact_waterfall

    # 1) Email ranking: founder/personal on domain > hello@ > external personal > support@; noreply@ dropped
    assert contact_waterfall.is_personal_or_exec_email("noreply@acme.ai", "acme.ai") is False
    assert contact_waterfall.is_personal_or_exec_email("support@acme.ai", "acme.ai") is False
    assert contact_waterfall.is_personal_or_exec_email("alex@acme.ai", "acme.ai") is True
    assert (
        contact_waterfall.rank_and_pick_best_email(
            ["noreply@acme.ai", "support@acme.ai", "hello@acme.ai", "ceo@acme.ai"],
            domain="acme.ai",
        )
        == "ceo@acme.ai"
    )
    assert contact_waterfall.rank_and_pick_best_email(["no-reply@acme.ai"], domain="acme.ai") == ""

    # 2) Tier 1 crawls root -> /contact -> linktr.ee and upgrades support@ to founder@
    pages = {
        "https://calai.app": '<a href="/contact">Contact</a><a href="https://linktr.ee/calai">Bio</a>',
        "https://calai.app/contact": "Email alex [at] calai.app or support@calai.app",
        "https://linktr.ee/calai": '<a href="https://t.me/alex_calai">TG</a> <a href="https://x.com/alex_calai">X</a>',
    }
    tier2_calls: list[str] = []
    out_t1 = contact_waterfall.enrich_lead_contacts(
        {"project": "CalAI", "project_url": "https://calai.app", "work_email": "support@calai.app", "icp_score": 88},
        http_get_fn=lambda url, **kw: SimpleNamespace(status_code=200, text=pages.get(url.rstrip("/"), pages.get(url, ""))),
        treg_fn=lambda ep, **kw: tier2_calls.append(ep) or {},
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )
    assert out_t1["work_email"] == "alex@calai.app"
    assert out_t1["username"] == "alex_calai"
    assert out_t1["founder_x"] == "https://x.com/alex_calai"
    assert out_t1["contact_waterfall_tier"] == "tier1"
    assert tier2_calls == []

    # 3) Tier 2 paid fallback triggers only when lacking TG/WA/X/personal email & respects budget cap
    budget = [0.006]
    out_t2 = contact_waterfall.enrich_lead_contacts(
        {"project": "HabitFlow", "project_url": "https://habitflow.io", "work_email": "support@habitflow.io", "icp_score": 85},
        allow_paid_tier2=True,
        budget_usd=budget,
        http_get_fn=lambda url, **kw: SimpleNamespace(status_code=200, text="<html><body>No personal contact</body></html>"),
        dm_finder_fn=lambda c, d: {},
        treg_fn=lambda ep, **kw: (
            tier2_calls.append(ep)
            or (
                {
                    "employees": [
                        {"name": "Sarah Kim", "title": "Co-Founder & CEO", "linkedin_url": "https://www.linkedin.com/in/sarah-kim-hf"},
                    ]
                }
                if ep == "leadmagic.x.employee-finder"
                else {"email": "sarah@habitflow.io"}
            )
        ),
        tg_bridge_fn=lambda r: {"username": "sarah_hf"} if r.get("founder_linkedin") else r,
    )
    assert "leadmagic.x.employee-finder" in tier2_calls
    assert out_t2["work_email"] == "sarah@habitflow.io"
    assert out_t2["username"] == "sarah_hf"
    assert out_t2["contact_waterfall_tier"] == "tier2"
    assert 0.0 <= budget[0] < 0.006


def test_ofm_france_classifier_tg_groups_and_facebook_groups(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from leadgen import ofm_and_groups

    # 1) Real @FranceOFM buyer messages accepted, job seekers / bots / KYC fraud rejected
    r_amelie = ofm_and_groups.classify_ofm_actor(
        name="Amelie",
        username="ameliemlh",
        bio="Agency Assistant - DM MOI",
        text="RECRUTEMENT VA — INSTAGRAM MARCHÉ AMÉRICAIN (US). Rejoins notre agence!",
    )
    assert r_amelie["is_buyer"] is True
    assert r_amelie["ofm_tier"] == "traffic_buyer"
    assert r_amelie["icp_score"] >= 90

    r_paul = ofm_and_groups.classify_ofm_actor(
        name="Paul",
        username="pashokbiz",
        text="HIRING: Twitter (X) VAs! $0.70 for every subscriber you bring, paid in USDC",
    )
    assert r_paul["is_buyer"] is True
    assert r_paul["ofm_tier"] == "traffic_buyer"

    r_strong = ofm_and_groups.classify_ofm_actor(
        name="Strong",
        username="diudiubo1",
        text="Notre agence est a la recherche de VA pour Instagram et Twitter (x) 0,7 $ pour chaque new abo",
    )
    assert r_strong["is_buyer"] is True
    assert r_strong["ofm_tier"] == "traffic_buyer"

    r_agency = ofm_and_groups.classify_ofm_actor(
        name="M-A Agence",
        username="maagence1",
        text="Notre agence agrandit l'équipe - On recrute 5 chatteurs cette semaine",
    )
    assert r_agency["is_buyer"] is True
    assert r_agency["ofm_tier"] == "agency_owner"

    assert ofm_and_groups.classify_ofm_actor(
        name="JIMMY",
        username="jamb206",
        text="AVAILABLE & EXPERIENCED VIRTUAL ASSISTANT for Reddit, Tinder & Instagram promotion",
    )["is_buyer"] is False

    assert ofm_and_groups.classify_ofm_actor(
        name="Group Help",
        username="GHClone4Bot",
        text="Action: Restreint jusqu'au 17/11/2026",
    )["is_buyer"] is False

    assert ofm_and_groups.classify_ofm_actor(
        name="kane",
        username="kanepay",
        text="Lemon cash Argentina verified account on emulator, withdraw money from OnlyFans",
    )["is_buyer"] is False

    assert ofm_and_groups.classify_ofm_actor(
        name="NoUsername",
        username="",
        text="On recrute des VA Instagram pour agence US",
    )["is_buyer"] is False

    # 2) Mock Telethon client collects OFM buyers exclusively to ofm_review_leads.jsonl
    state_dir = tmp_path / "state"

    class FakeSender(SimpleNamespace):
        pass

    class FakeMsg(SimpleNamespace):
        pass

    class FakeClient:
        def __init__(self, msgs):
            self._msgs = msgs

        async def get_entity(self, group):
            return SimpleNamespace(id=1945621315, username=group, megagroup=True)

        async def get_messages(self, ent, limit=250):
            return self._msgs

        async def __call__(self, req):
            u = getattr(req, "id", None)
            uname = getattr(u, "username", "")
            return SimpleNamespace(full_user=SimpleNamespace(about=f"DM @{uname}" if uname else ""))

    fake_msgs = [
        FakeMsg(
            id=1,
            message="RECRUTEMENT VA INSTAGRAM MARCHÉ AMÉRICAIN (US) $300+/mo",
            sender=FakeSender(id=101, username="ameliemlh", first_name="Amelie", last_name="", bot=False, deleted=False),
        ),
        FakeMsg(
            id=2,
            message="AVAILABLE & EXPERIENCED VIRTUAL ASSISTANT looking for agency",
            sender=FakeSender(id=102, username="jamb206", first_name="Jimmy", last_name="", bot=False, deleted=False),
        ),
        FakeMsg(
            id=3,
            message="L4L SFS group drop your link",
            sender=FakeSender(id=103, username="l4lspam", first_name="Promo", last_name="", bot=False, deleted=False),
        ),
    ]
    res_ofm = ofm_and_groups.collect_ofm_tg_groups(
        groups=["FranceOFM"],
        state_dir=state_dir,
        msg_limit=50,
        delay=0.0,
        client=FakeClient(fake_msgs),
    )
    assert res_ofm["ofm_added"] == 1
    ofm_saved = [json.loads(l) for l in (state_dir / "ofm_review_leads.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(ofm_saved) == 1
    assert ofm_saved[0]["username"] == "ameliemlh"

    # 3) collect_facebook_groups routes OFM post -> ofm_review_leads.jsonl and DTC/SaaS post -> b2b_rows
    fb_calls: list[str] = []

    def fake_fb_treg(ep, method="GET", query=None, data=None, **kw):
        fb_calls.append(ep)
        if ep == "anyapi.facebook.group.posts":
            return None  # force fallback to scrapecreators!
        if ep == "scrapecreators.x.v1-facebook-group-posts":
            return {
                "posts": [
                    {
                        "id": "fb1",
                        "authorName": "Julien OFM",
                        "text": "HIRING Instagram & Threads VAs for US OnlyFans agency! DM @julienofm on TG",
                    },
                    {
                        "id": "fb2",
                        "authorName": "Mia Chen",
                        "text": "Just scaled our Shopify skincare store https://glowdrop.co looking for UGC creators, email mia@glowdrop.co",
                    },
                ]
            }
        return {}

    fb_res = ofm_and_groups.collect_facebook_groups(
        group_urls=["https://www.facebook.com/groups/ofmagencyowners/"],
        state_dir=state_dir,
        treg_fn=fake_fb_treg,
    )
    assert "anyapi.facebook.group.posts" in fb_calls
    assert "scrapecreators.x.v1-facebook-group-posts" in fb_calls
    assert len(fb_res["ofm_rows"]) == 1
    assert fb_res["ofm_rows"][0]["username"] == "julienofm"
    assert len(fb_res["b2b_rows"]) == 1
    assert fb_res["b2b_rows"][0]["project_url"] == "https://glowdrop.co"

    # 4) Audit blocker regressions:
    # - MCP ingest_discovered_leads blocks OFM leads cleanly
    conn = connect(tmp_path / "mcp_ofm_block.db")
    init_db(conn)
    mcp_block = call_mcp_tool_handler(
        conn,
        "ingest_discovered_leads",
        {
            "campaign_name": "SoftUGC — База Все2",
            "leads": [
                {
                    "name": "OFM Spy",
                    "username": "ofmbuyer",
                    "source": "ofm_tg",
                    "ofm_tier": "traffic_buyer",
                    "project": "OnlyFans Agency",
                    "url": "https://ofmagency.example",
                    "their_angle": "OnlyFans management agency hiring VAs",
                }
            ],
        },
    )
    assert mcp_block["created_leads"] == 0
    assert mcp_block["enrolled_in_campaign"] == 0
    assert mcp_block["skipped_non_icp"] == 1
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    conn.close()

    # - @FranceOFM plural VAs / xMaats accepted, while contractor pitches & Channel senders rejected
    assert ofm_and_groups.classify_ofm_actor(
        name="Maats",
        username="xMaats",
        text="ON RECRUTE DES VA RÉSEAUX SOCIAUX! Tu publies nos vidéos.",
    )["is_buyer"] is True
    assert ofm_and_groups.classify_ofm_actor(
        name="AgencyX",
        username="agencyx_ofm",
        text="On recrute des VAs Threads & Instagram pour nos modèles Fanvue",
    )["is_buyer"] is True
    assert ofm_and_groups.classify_ofm_actor(
        name="Contractor",
        username="promo_va",
        text="Experienced VA Instagram/Threads + Mother/Child $1 per subscriber",
    )["is_buyer"] is False

    # - Facebook OFM-bio author cannot leak into B2B even without OFM words in post_text
    fb_leak_res = ofm_and_groups.collect_facebook_groups(
        group_urls=["https://www.facebook.com/groups/genericfounders/"],
        state_dir=state_dir,
        treg_fn=lambda ep, **kw: (
            {
                "posts": [
                    {
                        "id": "fb_leak",
                        "authorName": "Jean OFM Agency",
                        "authorBio": "OnlyFans management agency owner",
                        "text": "Need more traffic reels for our US roster https://myagency.example DM me @jeanofm",
                    }
                ]
            }
            if ep == "anyapi.facebook.group.posts"
            else None
        ),
    )
    assert fb_leak_res["b2b_rows"] == []

    # - Non-human Channel sender & autoreply@ dropped
    assert ofm_and_groups._is_non_human_tg_sender(
        FakeMsg(id=99, post=True, from_id=None),
        FakeSender(username="some_channel", broadcast=True, bot=False, deleted=False),
    ) is True
    from leadgen import contact_waterfall
    assert contact_waterfall.is_personal_or_exec_email("autoreply@acme.ai", "acme.ai") is False



