import json
from pathlib import Path
import sys

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import as_dict, connect, init_db
from app.hygiene import append_direct_x_lead, read_direct_x_leads
from app.leadgen_settings import (
    DEFAULT_LEADGEN_CONFIG,
    _normalize_config,
    get_leadgen_config,
    get_leadgen_status,
    push_discovered_now,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.outreach import create_campaign, get_campaign
from leadgen.language_router import (
    CAMPAIGN_OFM_TG,
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
    CAMPAIGN_VACANCY_RU_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
)
from leadgen.outreach_copy import (
    REVSHARE_OFFER_TEXT,
    build_outreach_sequence,
    build_reverse_bridge_draft,
    check_copy_language,
    render_whale_offer,
)

LEGACY_FORBIDDEN = "Creator" + "OS"


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "wave_b_copy_settings.db"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"

    monkeypatch.setenv("CRMTG_DB_PATH", str(db_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))

    conn = connect(db_path)
    init_db(conn)
    try:
        yield conn, state_dir, cfg_path
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("lead", "expected_tokens"),
    [
        (
            {
                "company": "StarsPay",
                "category": "tma",
                "their_angle": "Telegram Mini App wallet on Telegram Stars",
                "source": "tma_apps",
                "segment": "whale",
            },
            ("telegram ads", "catalog", "cpm", "mini app", "organic users"),
        ),
        (
            {
                "company": "MiniTutor",
                "category": "Education",
                "their_angle": "TG mini app flashcard trainer",
                "source": "tg_catalog_discovery",
                "segment": "whale",
            },
            ("telegram ads", "mini app", "organic users"),
        ),
        (
            {
                "company": "SignalDesk",
                "category": "SaaS",
                "their_angle": "AI trading alerts channel",
                "source": "tg_ad_spy",
                "segment": "whale",
            },
            ("paid channel seeding", "telegram ads", "one-day spike", "cpm", "compound daily user acquisition"),
        ),
        (
            {
                "company": "PulseHabit",
                "category": "Health",
                "their_angle": "iOS/Android app for habit coaching",
                "source": "app_stores",
                "segment": "whale",
            },
            ("mobile ua creative fatigue", "5-7 days", "cpi", "cac", "ai ugc", "install costs"),
        ),
        (
            {
                "company": "FocusTimer",
                "category": "mobile_ua",
                "their_angle": "Subscription iOS app with $12/mo plan",
                "source": "high_yield_veins",
                "segment": "whale",
            },
            ("mobile ua creative fatigue", "5-7 days", "drop install costs"),
        ),
        (
            {
                "company": "VelvetGlow",
                "category": "Beauty",
                "their_angle": "Skincare gift card and checkout bundle",
                "source": "tiktok_shop",
                "segment": "whale",
            },
            ("product seeding delays", "zero-shoot daily ai ugc output"),
        ),
        (
            {
                "company": "PeakFuel",
                "category": "D2C",
                "their_angle": "Shopify physical products for hydration",
                "source": "d2c_whatsapp",
                "segment": "whale",
            },
            ("product seeding delays", "zero-shoot daily ai ugc output"),
        ),
    ],
)
def test_wave_b_segment_hooks_in_whale_mass_sequence(lead: dict, expected_tokens: tuple[str, ...]) -> None:
    seq = build_outreach_sequence(lead, copy_profile="whale_mass")
    s1_low = seq.step1_text.lower()
    for token in expected_tokens:
        assert token in s1_low, f"Missing {token!r} in step1_text: {seq.step1_text}"

    expected_offer = render_whale_offer(lead["company"])
    assert expected_offer in seq.step1_text
    assert LEGACY_FORBIDDEN.lower() not in s1_low
    assert "revshare" not in s1_low
    assert "cpa" not in s1_low
    assert REVSHARE_OFFER_TEXT not in seq.step1_text
    assert "—" not in seq.step1_text and "–" not in seq.step1_text
    assert check_copy_language(seq.step1_text, "en")[0] is True


def test_build_reverse_bridge_draft_handles_links_fallbacks_and_scrubbing() -> None:
    lead = {
        "project": f"StarsWallet {LEGACY_FORBIDDEN}",
        "name": "Alex Rivera",
        "category": "tma",
        "their_angle": "Telegram Mini App for Stars subscriptions",
        "lead_source": "tma_apps",
    }

    with_handle = build_reverse_bridge_draft(lead, tg_handle="@Vlad_SoftUGC")
    assert set(with_handle.keys()) == {"whatsapp_draft", "email_subject", "email_body", "tg_link"}
    assert with_handle["tg_link"] == "https://t.me/Vlad_SoftUGC"
    assert "https://t.me/Vlad_SoftUGC" in with_handle["whatsapp_draft"]
    assert "https://t.me/Vlad_SoftUGC" in with_handle["email_body"]
    assert "Hey Alex," in with_handle["whatsapp_draft"]
    assert "Hi Alex," in with_handle["email_body"]
    assert "telegram mini app" in with_handle["whatsapp_draft"].lower()
    assert "StarsWallet" in with_handle["email_subject"]
    for field_val in with_handle.values():
        assert LEGACY_FORBIDDEN.lower() not in field_val.lower()
        assert "—" not in field_val and "–" not in field_val
        assert "revshare" not in field_val.lower()

    from_url = build_reverse_bridge_draft(
        {"company": "GlowBar", "source": "tiktok_shop", "what_they_do": "D2C collagen bars"},
        tg_handle="https://t.me/softugc_showcase/?start=1",
    )
    assert from_url["tg_link"] == "https://t.me/softugc_showcase"
    assert "product seeding delays" in from_url["whatsapp_draft"].lower()
    assert "https://t.me/softugc_showcase" in from_url["email_body"]

    no_handle = build_reverse_bridge_draft(
        {"company": "PulseApp", "source": "app_stores", "what_they_do": "iOS fitness tracker"},
        tg_handle="",
    )
    assert no_handle["tg_link"] == ""
    assert "https://t.me/" not in no_handle["whatsapp_draft"]
    assert "https://t.me/" not in no_handle["email_body"]
    assert "SoftUGC Telegram" in no_handle["whatsapp_draft"]
    assert "SoftUGC Telegram" in no_handle["email_body"]
    assert "hi there" not in no_handle["email_body"].lower()
    assert "[" not in no_handle["whatsapp_draft"] and "{" not in no_handle["whatsapp_draft"]
    assert "mobile ua creative fatigue" in no_handle["email_body"].lower()

    bad_handle = build_reverse_bridge_draft({"company": "PulseApp"}, tg_handle="bad handle!!")
    assert bad_handle["tg_link"] == ""
    assert "https://t.me/" not in bad_handle["whatsapp_draft"]
    assert "SoftUGC Telegram" in bad_handle["whatsapp_draft"]


def test_wave_b_config_defaults_normalization_and_legacy_new_vein_keys(isolated_env) -> None:
    conn, _state_dir, cfg_path = isolated_env

    assert DEFAULT_LEADGEN_CONFIG["b2b"]["sources"]["tma_apps"] is True
    assert DEFAULT_LEADGEN_CONFIG["b2b"]["sources"]["tg_catalog_discovery"] is True
    assert DEFAULT_LEADGEN_CONFIG["b2b"]["reverse_bridge_tg_handle"] == ""

    norm_at = _normalize_config({"b2b": {"reverse_bridge_tg_handle": "@SoftUGC_Team"}})
    assert norm_at["b2b"]["reverse_bridge_tg_handle"] == "SoftUGC_Team"

    norm_url = _normalize_config({"b2b": {"reverse_bridge_tg_handle": "https://t.me/vlad_demo/"}})
    assert norm_url["b2b"]["reverse_bridge_tg_handle"] == "vlad_demo"

    norm_empty = _normalize_config({"b2b": {"reverse_bridge_tg_handle": "  @  "}})
    assert norm_empty["b2b"]["reverse_bridge_tg_handle"] == ""

    for bad in ("bad handle!", "https://example.com/abc", "ab", "123user", 42):
        with pytest.raises(ValueError):
            _normalize_config({"b2b": {"reverse_bridge_tg_handle": bad}})

    # Restricted legacy config (meta_ads=False, social_x_reddit=False without new_vein_keys) disables Wave B keys too
    restricted = save_leadgen_config(
        conn,
        {
            "b2b": {
                "sources": {
                    "tg_groups": True,
                    "meta_ads": False,
                    "social_x_reddit": False,
                },
                "reverse_bridge_tg_handle": "@founder_bridge",
            }
        },
        sync_cases_file=False,
    )
    assert restricted["b2b"]["sources"]["tma_apps"] is False
    assert restricted["b2b"]["sources"]["tg_catalog_discovery"] is False
    assert restricted["b2b"]["reverse_bridge_tg_handle"] == "founder_bridge"

    # Explicit opt-in enables them and round-trips through DB & JSON
    updated = save_leadgen_config(
        conn,
        {
            "b2b": {
                "sources": {
                    "tma_apps": True,
                    "tg_catalog_discovery": True,
                },
                "reverse_bridge_tg_handle": "https://t.me/softugc_live",
            }
        },
        sync_cases_file=False,
    )
    assert updated["b2b"]["sources"]["tma_apps"] is True
    assert updated["b2b"]["sources"]["tg_catalog_discovery"] is True
    assert updated["b2b"]["reverse_bridge_tg_handle"] == "softugc_live"

    loaded = get_leadgen_config(conn)
    assert loaded["b2b"]["sources"]["tma_apps"] is True
    assert loaded["b2b"]["sources"]["tg_catalog_discovery"] is True
    assert loaded["b2b"]["reverse_bridge_tg_handle"] == "softugc_live"
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["b2b"]["reverse_bridge_tg_handle"] == "softugc_live"


def test_sync_leadgen_campaigns_keeps_new_segments_draft_and_preserves_running(isolated_env) -> None:
    conn, _state_dir, _cfg_path = isolated_env

    # Seed healthy TG accounts & autopilot ON + pre-existing running Whales EN — TG and SoftUGC — OFM (TG)
    now = "2026-11-24T10:00:00"
    conn.execute(
        "INSERT INTO accounts (phone, session_path, status, created_at, health, provider) VALUES ('+15550001001', 'tg1.session', 'connected', ?, 'active', 'telegram')",
        (now,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '1', ?)",
        (now,),
    )
    conn.commit()

    whales_running = create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        status="running",
        daily_limit=30,
        leadgen_enabled=True,
    )
    ofm_running = create_campaign(
        conn,
        name=CAMPAIGN_OFM_TG,
        status="running",
        daily_limit=25,
        leadgen_enabled=True,
    )

    cfg = save_leadgen_config(
        conn,
        {
            "b2b": {
                "enabled": True,
                "ofm_outreach_enabled": True,
                "target_campaign_tg": CAMPAIGN_WHALES_EN_TG,
                "target_campaign_ofm": CAMPAIGN_OFM_TG,
            },
            "vacancies": {"enabled": True},
            "ru_experts": {"enabled": True},
        },
        sync_cases_file=False,
    )
    res = sync_leadgen_campaigns(conn, cfg)
    assert res["ok"] is True

    by_name = {c["name"]: c for c in res["campaigns"]}
    assert by_name[CAMPAIGN_WHALES_EN_TG]["status"] == "running"
    assert by_name[CAMPAIGN_OFM_TG]["status"] == "running"
    assert get_campaign(conn, whales_running["id"])["status"] == "running"
    assert get_campaign(conn, ofm_running["id"])["status"] == "running"

    for new_name in (
        CAMPAIGN_WHALES_EN_WA,
        CAMPAIGN_VACANCY_RU_TG,
        CAMPAIGN_VACANCY_RU_WA,
        CAMPAIGN_VACANCY_EN_TG,
        CAMPAIGN_RU_EXPERTS_TG,
    ):
        assert by_name[new_name]["status"] == "draft"

    # Even a brand-new custom segment target_campaign_tg starts strictly in draft
    custom_cfg = save_leadgen_config(
        conn,
        {"b2b": {"target_campaign_tg": "TMA Apps — TG", "ofm_outreach_enabled": False}},
        sync_cases_file=False,
    )
    res_custom = sync_leadgen_campaigns(conn, custom_cfg)
    custom_by_name = {c["name"]: c for c in res_custom["campaigns"]}
    assert custom_by_name["TMA Apps — TG"]["status"] == "draft"
    assert get_campaign(conn, whales_running["id"])["status"] == "running"
    assert get_campaign(conn, ofm_running["id"])["status"] == "running"


def test_push_discovered_now_tma_apps_and_direct_x_reverse_bridge_compat(isolated_env) -> None:
    conn, state_dir, _cfg_path = isolated_env
    save_leadgen_config(
        conn,
        {"b2b": {"reverse_bridge_tg_handle": "@SoftUGC_Bridge"}},
        sync_cases_file=False,
    )
    sync_leadgen_campaigns(conn)

    # 1) One TG-verified tma_apps whale + one contactless tma_apps whale -> direct_x_leads.jsonl
    (state_dir / "discovered_leads.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "name": "Mark Vance",
                        "company": "FocusTMA",
                        "project": "FocusTMA",
                        "username": "mark_focustma",
                        "tma_bot_handle": "focustma_bot",
                        "website": "https://focustma.app",
                        "category": "tma",
                        "their_angle": "Telegram Mini App Pomodoro productivity tool on Stars",
                        "source": "tma_apps",
                        "lead_source": "tma_apps",
                        "segment": "whale",
                        "icp_score": 84,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "name": "Elena Park",
                        "company": "StarsFlow",
                        "project": "StarsFlow",
                        "username": "",
                        "tma_bot_handle": "starsflow_bot",
                        "email": "elena@starsflow.app",
                        "website": "https://starsflow.app",
                        "contact_form_url": "https://starsflow.app/contact",
                        "category": "tma",
                        "their_angle": "Subscription Telegram Mini App analytics",
                        "source": "tma_apps",
                        "lead_source": "tma_apps",
                        "segment": "whale",
                        "icp_score": 82,
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    push_res = push_discovered_now(conn)
    assert push_res["ok"] is True
    assert push_res["ingested"] == 1

    rec = as_dict(conn.execute("SELECT * FROM campaign_recipients WHERE username = 'mark_focustma'").fetchone())
    assert "telegram ads" in rec["step1_text"].lower()
    assert "mini app" in rec["step1_text"].lower()
    assert LEGACY_FORBIDDEN.lower() not in rec["step1_text"].lower()
    assert "revshare" not in rec["step1_text"].lower()

    status = get_leadgen_status(conn)
    assert status["crm_leadgen_counts"]["b2b_leads"] == 1
    assert status["direct_x_leads_count"] == 1

    # Also test read_direct_x_leads (which fills missing fields with None) -> build_reverse_bridge_draft
    append_direct_x_lead(state_dir, {"url": "https://onlydomain.io", "email": "hi@onlydomain.io"})
    dx_items = read_direct_x_leads(state_dir, limit=10)["items"]
    assert len(dx_items) == 2

    stars_item = next(it for it in dx_items if it.get("company") == "StarsFlow")
    assert stars_item.get("contact_form_url") == "https://starsflow.app/contact"
    assert stars_item.get("tma_bot_handle") == "starsflow_bot"

    cfg = get_leadgen_config(conn)
    for item in dx_items:
        draft = build_reverse_bridge_draft(item, tg_handle=cfg["b2b"]["reverse_bridge_tg_handle"])
        assert draft["tg_link"] == "https://t.me/SoftUGC_Bridge"
        assert "https://t.me/SoftUGC_Bridge" in draft["whatsapp_draft"]
        assert "https://t.me/SoftUGC_Bridge" in draft["email_body"]
        assert "None" not in draft["whatsapp_draft"]
        assert "None" not in draft["email_body"]
        assert LEGACY_FORBIDDEN.lower() not in draft["whatsapp_draft"].lower()
        assert LEGACY_FORBIDDEN.lower() not in draft["email_body"].lower()
