from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import leadgen_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.leads import create_lead  # noqa: E402
from app.outreach import create_campaign as real_create_campaign  # noqa: E402
from leadgen import (  # noqa: E402
    contact_waterfall,
    tg_catalog_discovery,
    treg_enrich,
    vps_autonomous_replenish as repl,
)


def _setup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Path]:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-wave2")
    conn = connect(tmp_path / "wave2.db")
    init_db(conn)
    return conn, state_dir


def test_treg_enrich_glued_url_cleaning() -> None:
    assert treg_enrich.domain_of("https://globalexpat.nethttps") == "globalexpat.net"
    assert (
        treg_enrich.domain_of("https://globalexpat.nethttps://www.linkedin.com/in/moaboshanab")
        == "globalexpat.net"
    )
    assert treg_enrich.domain_of("https://www.globalexpat.nethttp") == "globalexpat.net"
    assert treg_enrich.domain_of("https://adamhealth.sahttps") == "adamhealth.sa"
    assert treg_enrich.domain_of("https://myhttps.com") == "myhttps.com"

    glued_payload = {
        "organic": [
            {
                "url": "https://globalexpat.nethttps://www.linkedin.com/in/moaboshanab",
                "snippet": "Mo Aboshanab founder",
            }
        ]
    }
    assert (
        treg_enrich._matching_project_url(glued_payload, ["globalexpat"])
        == "https://globalexpat.net"
    )
    assert (
        treg_enrich._matching_project_url(
            {"url": "https://globalexpat.nethttps"}, ["globalexpat"]
        )
        == "https://globalexpat.net"
    )


def test_contact_waterfall_sanitizes_glued_project_urls() -> None:
    assert (
        contact_waterfall._sanitize_project_url(
            "https://globalexpat.nethttps://www.linkedin.com/in/moaboshanab"
        )
        == "https://globalexpat.net"
    )
    assert (
        contact_waterfall._sanitize_project_url("https://globalexpat.nethttps")
        == "https://globalexpat.net"
    )
    assert (
        contact_waterfall._sanitize_project_url("https://lp.acme-scale.io/promo")
        == "https://lp.acme-scale.io/promo"
    )
    assert contact_waterfall._sanitize_project_url("https://t.me/some_channel") == ""

    # 1) Sanitize monid discovered_site (FB website and IG externalUrl)
    out_monid = contact_waterfall.enrich_lead_contacts(
        {
            "id": "cw_monid_glued",
            "company": "GlobalExpat",
            "fb_page_id": "123456",
            "icp_score": 85,
        },
        http_get_fn=lambda *_a, **_kw: SimpleNamespace(status_code=404, text="", url=""),
        monid_fn=lambda *_a, **_kw: {
            "website": "https://globalexpat.nethttps://www.linkedin.com/in/moaboshanab"
        },
        treg_fn=lambda *_a, **_kw: {},
        dm_finder_fn=lambda *_a, **_kw: [],
        allow_paid_tier2=True,
    )
    assert out_monid["project_url"] == "https://globalexpat.net"
    assert out_monid["company_domain"] == "globalexpat.net"

    out_monid_ig = contact_waterfall.enrich_lead_contacts(
        {
            "id": "cw_monid_ig_glued",
            "company": "GlobalExpat",
            "instagram_handle": "globalexpat",
            "icp_score": 85,
        },
        http_get_fn=lambda *_a, **_kw: SimpleNamespace(status_code=404, text="", url=""),
        monid_fn=lambda *_a, **_kw: {
            "externalUrl": "https://globalexpat.nethttps"
        },
        treg_fn=lambda *_a, **_kw: {},
        dm_finder_fn=lambda *_a, **_kw: [],
        allow_paid_tier2=True,
    )
    assert out_monid_ig["project_url"] == "https://globalexpat.net"
    assert out_monid_ig["company_domain"] == "globalexpat.net"

    # 2) Sanitize dm_site from Tier-2 DM discovery
    out_dm = contact_waterfall.enrich_lead_contacts(
        {
            "id": "cw_dm_glued",
            "company": "GlobalExpat",
            "icp_score": 88,
        },
        http_get_fn=lambda *_a, **_kw: SimpleNamespace(status_code=404, text="", url=""),
        monid_fn=lambda *_a, **_kw: {},
        treg_fn=lambda *_a, **_kw: {},
        dm_finder_fn=lambda *_a, **_kw: [
            {
                "founder_name": "Mo Aboshanab",
                "founder_linkedin": "https://www.linkedin.com/in/moaboshanab",
                "project_url": "https://globalexpat.nethttps",
            }
        ],
        allow_paid_tier2=True,
    )
    assert out_dm["project_url"] == "https://globalexpat.net"
    assert out_dm["company_domain"] == "globalexpat.net"

    # 3) Sanitize pre-existing glued project_url on input row
    out_pre = contact_waterfall.enrich_lead_contacts(
        {
            "id": "cw_pre_glued",
            "company": "GlobalExpat",
            "project_url": "https://globalexpat.nethttps://www.linkedin.com/in/moaboshanab",
            "icp_score": 70,
        },
        http_get_fn=lambda *_a, **_kw: SimpleNamespace(status_code=404, text="", url=""),
        allow_paid_tier2=False,
    )
    assert out_pre["project_url"] == "https://globalexpat.net"
    assert out_pre["company_domain"] == "globalexpat.net"


def test_tg_catalog_discovery_gcc_december_vs_global(tmp_path: Path) -> None:
    mena_pool = tg_catalog_discovery.CURATED_MENA_CHANNEL_POOL
    assert len(mena_pool) >= 15
    assert len({h.lower() for h in mena_pool}) == len(mena_pool)
    for handle in mena_pool:
        assert not tg_catalog_discovery._is_bot_handle(handle)
        assert not tg_catalog_discovery._is_junk_channel(handle)

    expected_queries = (
        "site:t.me/ dubai founders",
        "site:t.me/ riyadh startups",
        "site:t.me/ gcc ecommerce",
        "site:t.me/ saudi entrepreneurs",
        "site:t.me/ دبي مشاريع",
        "site:t.me/ السعودية تجارة الكترونية",
    )
    for q in expected_queries:
        assert q in tg_catalog_discovery.MENA_PAID_SEARCH_QUERIES

    proc = subprocess.run(
        [sys.executable, "-m", "leadgen.tg_catalog_discovery", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--market-profile" in proc.stdout

    def fake_http_get(_url: str, **_kw: Any) -> Any:
        return SimpleNamespace(status_code=200, text="", headers={}, is_redirect=False)

    def fake_probe(handle: str, **_kw: Any) -> dict[str, Any]:
        return {
            "handle": handle,
            "kind": "channel",
            "title": f"{handle} Community",
            "bio": "Founders and business operators chat",
            "subscribers": 1500,
        }

    global_queries_seen: list[str] = []
    gcc_queries_seen: list[str] = []

    def fake_treg_global(endpoint: str, query: dict[str, Any] | None = None, **_kw: Any) -> Any:
        if endpoint == "crawl4ai.web.search" and query:
            global_queries_seen.append(str(query.get("q") or ""))
        return {"results": [{"url": "https://t.me/global_search_saas"}]}

    def fake_treg_gcc(endpoint: str, query: dict[str, Any] | None = None, **_kw: Any) -> Any:
        if endpoint == "crawl4ai.web.search" and query:
            gcc_queries_seen.append(str(query.get("q") or ""))
        return {"results": [{"url": "https://t.me/dubai_search_founders"}]}

    res_global = tg_catalog_discovery.discover_and_replenish_catalogs(
        tmp_path / "state_global",
        limit=5,
        allow_paid=True,
        market_profile="global",
        http_get_fn=fake_http_get,
        treg_fn=fake_treg_global,
        probe_fn=fake_probe,
    )
    res_gcc = tg_catalog_discovery.discover_and_replenish_catalogs(
        tmp_path / "state_gcc",
        limit=5,
        allow_paid=True,
        market_profile="gcc_december",
        http_get_fn=fake_http_get,
        treg_fn=fake_treg_gcc,
        probe_fn=fake_probe,
    )

    assert global_queries_seen
    assert all(q in tg_catalog_discovery.PAID_SEARCH_QUERIES for q in global_queries_seen)
    assert "global_search_saas" in res_global["discovered"]
    assert any(h in tg_catalog_discovery.CURATED_CHANNEL_POOL for h in res_global["discovered"])

    assert gcc_queries_seen
    assert all(q in tg_catalog_discovery.MENA_PAID_SEARCH_QUERIES for q in gcc_queries_seen)
    assert "dubai_search_founders" in res_gcc["discovered"]
    assert any(h in tg_catalog_discovery.CURATED_MENA_CHANNEL_POOL for h in res_gcc["discovered"])


def test_resolve_effective_market_config_activates_follower_graph_cleanly() -> None:
    expected_enr_keys = {
        "daily_budget_cap_usd",
        "per_lead_cap_usd",
        "super_whale_cap_usd",
        "contactout_verified_cap_usd",
        "super_whale_icp_min",
        "super_whale_pool_min_usd",
        "low_balance_warn_usd",
        "low_balance_stop_usd",
        "alert_chat_id",
    }
    assert set(leadgen_settings.DEFAULT_LEADGEN_CONFIG["enrichment"].keys()) == expected_enr_keys
    assert leadgen_settings.DEFAULT_LEADGEN_CONFIG["b2b"]["sources"]["follower_graph"] is False
    assert leadgen_settings.DEFAULT_LEADGEN_CONFIG["follower_graph"]["enabled"] is False

    cfg = copy.deepcopy(leadgen_settings.DEFAULT_LEADGEN_CONFIG)
    cfg["active_market_profile"] = "gcc_december"
    cfg_snapshot = copy.deepcopy(cfg)

    eff = leadgen_settings.resolve_effective_market_config(cfg)
    assert cfg == cfg_snapshot
    assert leadgen_settings.DEFAULT_LEADGEN_CONFIG["b2b"]["sources"]["follower_graph"] is False
    assert leadgen_settings.DEFAULT_LEADGEN_CONFIG["follower_graph"]["enabled"] is False
    assert eff["active_market_profile"] == "gcc_december"
    assert eff["b2b"]["sources"]["follower_graph"] is True
    assert eff["follower_graph"]["enabled"] is True

    # Explicit override keeps follower_graph disabled even under gcc_december
    cfg_explicit_off = copy.deepcopy(cfg)
    cfg_explicit_off["_explicit_follower_graph_off"] = True
    eff_off = leadgen_settings.resolve_effective_market_config(cfg_explicit_off)
    assert eff_off["b2b"]["sources"]["follower_graph"] is False
    assert eff_off["follower_graph"]["enabled"] is False

    # Switching back to global restores saved global state
    cfg["active_market_profile"] = "global"
    restored = leadgen_settings.resolve_effective_market_config(cfg)
    assert restored["active_market_profile"] == "global"
    assert restored["b2b"]["sources"]["follower_graph"] is False
    assert restored["follower_graph"]["enabled"] is False


def test_persist_lead_v2_columns_ar_and_ensure_gcc_draft_campaigns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_db(tmp_path, monkeypatch)
    try:
        # 1) _persist_lead_v2_columns preserves language='ar' (via language or language_hint)
        lead_a = create_lead(conn, name="Tariq", username="tariq_gcc", provider="telegram")
        repl._persist_lead_v2_columns(
            conn,
            int(lead_a["id"]),
            {"segment": "gcc_mena", "language": "ar"},
        )
        lead_b = create_lead(conn, name="Noura", username="noura_gcc", provider="telegram")
        repl._persist_lead_v2_columns(
            conn,
            int(lead_b["id"]),
            {"segment": "gcc_mena", "language_hint": "ar"},
        )
        conn.commit()
        row_a = conn.execute("SELECT segment, language FROM leads WHERE id = ?", (lead_a["id"],)).fetchone()
        row_b = conn.execute("SELECT segment, language FROM leads WHERE id = ?", (lead_b["id"],)).fetchone()
        assert row_a["segment"] == "gcc_mena"
        assert row_a["language"] == "ar"
        assert row_b["language"] == "ar"

        # 2) Global profile creates 0 GCC draft campaigns
        assert repl.ensure_gcc_draft_campaigns(conn, {"active_market_profile": "global"}) == []
        assert conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0

        # 3) gcc_december creates GCC / MENA — TG & WA in draft status with ar/gcc_mena_ar/limit=30
        create_calls: list[dict[str, Any]] = []

        def spy_create_campaign(c, **kwargs: Any):
            create_calls.append(dict(kwargs))
            return real_create_campaign(c, **kwargs)

        monkeypatch.setattr(repl, "create_campaign", spy_create_campaign)

        ensured = repl.ensure_gcc_draft_campaigns(conn, {"active_market_profile": "gcc_december"})
        assert len(ensured) == 2
        assert any(call.get("language") == "ar" for call in create_calls)
        assert all(
            call.get("status") == "draft"
            and call.get("segment") == "gcc_mena"
            and call.get("copy_profile") == "gcc_mena_ar"
            and call.get("daily_limit") == 30
            and call.get("leadgen_enabled") is True
            for call in create_calls
        )

        db_rows = conn.execute(
            "SELECT name, status, segment, language, copy_profile, daily_limit, leadgen_enabled "
            "FROM campaigns ORDER BY id ASC"
        ).fetchall()
        assert [(r["name"], r["status"], r["segment"], r["language"], r["copy_profile"], r["daily_limit"], r["leadgen_enabled"]) for r in db_rows] == [
            ("GCC / MENA — TG", "draft", "gcc_mena", "ar", "gcc_mena_ar", 30, 1),
            ("GCC / MENA — WA", "draft", "gcc_mena", "ar", "gcc_mena_ar", 30, 1),
        ]

        # 4) Idempotent & never modifies existing campaign status
        conn.execute("UPDATE campaigns SET status = 'paused' WHERE name = 'GCC / MENA — TG'")
        conn.commit()
        create_calls.clear()
        ensured_again = repl.ensure_gcc_draft_campaigns(conn, {"active_market_profile": "gcc_december"})
        assert len(ensured_again) == 2
        assert create_calls == []
        tg_row = conn.execute(
            "SELECT status FROM campaigns WHERE name = 'GCC / MENA — TG'"
        ).fetchone()
        wa_row = conn.execute(
            "SELECT status FROM campaigns WHERE name = 'GCC / MENA — WA'"
        ).fetchone()
        assert tg_row["status"] == "paused"
        assert wa_row["status"] == "draft"

        # 5) run_autonomous_replenish passes --market-profile gcc_december & creates/keeps draft campaigns
        leadgen_settings.save_leadgen_config(
            conn,
            {
                "active_market_profile": "gcc_december",
                "b2b": {
                    "sources": {
                        "tg_catalog_discovery": True,
                        "meta_ads": False,
                        "social_x_reddit": False,
                        "follower_graph": False,
                        "yc_ph": False,
                        "tma_apps": False,
                        "buyer_catalogs": False,
                        "tg_groups": False,
                        "ofm_groups": False,
                        "facebook_groups": False,
                        "channel_graph": False,
                        "tg_ad_spy": False,
                        "whop_content_rewards": False,
                        "tiktok_ads": False,
                        "tiktok_shop": False,
                        "app_stores": False,
                        "ats_hiring": False,
                        "ph_detailed": False,
                        "d2c_whatsapp": False,
                    },
                    "limits": {"external_limit": 0},
                },
            },
        )
        seen_cmds: list[list[str]] = []

        def fake_runner(cmd: list[str] | str, **_kw: Any) -> subprocess.CompletedProcess[str]:
            parts = [str(x) for x in cmd] if isinstance(cmd, list) else [str(cmd)]
            seen_cmds.append(parts)
            if any("tg_catalog_discovery.py" in p for p in parts):
                (state_dir / "tg_catalog_state.json").write_text(
                    json.dumps({"last_run_at": datetime.now(timezone.utc).isoformat()}),
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(
            repl, "emit_leadgen_daily_digest", lambda *_a, **_kw: {"daily_sent": False, "error": None}
        )
        res = repl.run_autonomous_replenish(
            conn,
            runner=fake_runner,
            max_waves=1,
            budget_treg_balance_fn=lambda: 5.0,
            budget_monid_balance_fn=lambda: 5.0,
        )
        assert res["ok"] is True
        cat_cmds = [c for c in seen_cmds if any("tg_catalog_discovery.py" in p for p in c)]
        assert len(cat_cmds) == 1
        mp_idx = cat_cmds[0].index("--market-profile")
        assert cat_cmds[0][mp_idx + 1] == "gcc_december"
    finally:
        conn.close()
