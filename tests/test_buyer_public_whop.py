"""Comprehensive tests for Whop / Content Rewards discovery in leadgen/buyer_public.py."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from leadgen import buyer_public
from leadgen import contact_waterfall


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$5K", 5000),
        ("$5k", 5000),
        ("$10,000", 10000),
        ("$55k", 55000),
        ("$101,000", 101000),
        ("$2.5K", 2500),
        ("$1.5M", 1500000),
        ("$5,000+", 5000),
        ("USD 25,000", 25000),
        ("15k USD", 15000),
        ("$4,200 / $10,000 paid out", 10000),
        ("$2.00 per 1,000 views · $25,000 budget", 25000),
        ("Earn $1.50 / 1K views · Pool: $55k · Min payout $25", 55000),
        ("$29/mo subscription · $12,000 prize pool", 12000),
        ("$1.50 per 1,000 views", None),
        ("$2.00 / 1k views", None),
        ("$3.00 CPM", None),
        ("183.5M views in 90 days", None),
        ("", None),
    ],
)
def test_parse_pool_usd_variants(text: str, expected: int | None):
    assert buyer_public.parse_pool_usd(text) == expected


@pytest.mark.parametrize(
    ("text", "item", "expected"),
    [
        ("Status: Active · $15,000 pool", None, "active"),
        ("Live now · accepting submissions", None, "active"),
        ("$4,200 / $10,000 paid out", None, "active"),
        ("Recently finished · $25,000 pool", None, "finished"),
        ("Campaign ended · 100% paid", None, "finished"),
        ("Status: Closed · Budget depleted", None, "finished"),
        ("$10,000 / $10,000 paid out", None, "finished"),
        ("Any text", {"status": "completed"}, "finished"),
        ("Any text", {"is_active": False}, "finished"),
        ("Any text", {"paid_out": 15000, "total_budget": 15000}, "finished"),
        ("Any text", {"paid_out": 5000, "total_budget": 15000, "status": "active"}, "active"),
    ],
)
def test_detect_campaign_status(text: str, item: dict | None, expected: str):
    assert buyer_public.detect_campaign_status(text, item=item) == expected


def test_resolve_whop_min_pool_usd_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "leadgen_config.json"
    cfg_file.write_text(
        json.dumps({"whales": {"whop_min_pool_usd": 8000}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.delenv("WHOP_MIN_POOL_USD", raising=False)

    assert buyer_public.resolve_whop_min_pool_usd() == 8000
    assert buyer_public.resolve_whop_min_pool_usd({"whales": {"whop_min_pool_usd": 12000}}) == 12000

    monkeypatch.setenv("WHOP_MIN_POOL_USD", "7500")
    assert buyer_public.resolve_whop_min_pool_usd() == 7500
    assert buyer_public.resolve_whop_min_pool_usd(min_pool_usd=5000) == 5000

    monkeypatch.delenv("WHOP_MIN_POOL_USD", raising=False)
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "missing.json"))
    assert buyer_public.resolve_whop_min_pool_usd() == 5000


def test_extract_active_and_finished_html_cards_filters_pool_and_extracts_contacts():
    html_page = """
    <html><body>
      <div class="campaigns-grid">
        <article class="campaign-card">
          <img src="https://cdn.whop.com/logo1.png"><br>
          <h3>PulseFit Rewards — Content Rewards | Whop</h3>
          <p>Status: Active · $25,000 total pool · $2.00 per 1,000 views</p>
          <a href="https://whop.com/discover/pulsefit/?utm_source=dir">View on Whop</a>
          <a href="https://pulsefit.ai/?utm_source=whop">Website</a>
          <a href="https://x.com/pulsefit_app">X</a>
          <a href="https://t.me/pulsefit_founder">Telegram</a>
          <a href="https://discord.gg/pulsefit">Discord</a>
          <span>Discord Owner: @alex_pulse</span>
        </article>
        <article class="campaign-card">
          <h3>VaultProp — Content Rewards</h3>
          <p>Recently finished · $10,000 / $10,000 paid out · Budget: $10,000</p>
          <a href="https://whop.com/discover/vaultprop/">Whop</a>
          <a href="https://vaultprop.com">Site</a>
          <a href="https://discord.com/invite/vaultprop">Join Discord</a>
        </article>
        <article class="campaign-card">
          <h3>MidScale AI</h3>
          <p>Active · $7,500 pool · https://midscale.io</p>
          <a href="https://x.com/midscaleai">X</a>
        </article>
        <article class="campaign-card">
          <h3>TinyBudget App</h3>
          <p>Active · $1,500 pool</p>
          <a href="https://tinybudget.io">Site</a>
          <a href="https://x.com/tinybudget">X</a>
        </article>
        <article class="campaign-card">
          <h3>BloxWorld Roblox Server</h3>
          <p>Active · $20,000 pool · Roblox clipping</p>
          <a href="https://bloxworld.gg">Site</a>
          <a href="https://x.com/bloxworld">X</a>
        </article>
        <article class="campaign-card">
          <h3>Clipper Brag</h3>
          <p>Got accepted into Whop Content Rewards! My last payout was $5,500</p>
          <a href="https://x.com/clipper_brag/status/123456">Post</a>
        </article>
      </div>
    </body></html>
    """
    rows = buyer_public.whop_rows(fetch_fn=lambda _url: html_page)
    by_proj = {r["project"]: r for r in rows}

    assert set(by_proj) == {"PulseFit Rewards", "VaultProp", "MidScale AI"}
    assert "TinyBudget App" not in by_proj
    assert "BloxWorld Roblox Server" not in by_proj
    assert "Clipper Brag" not in by_proj

    pulse = by_proj["PulseFit Rewards"]
    assert pulse["source"] == "whop_content_rewards"
    assert pulse["segment"] == "whale"
    assert pulse["pool_usd"] == 25000
    assert pulse["campaign_status"] == "active"
    assert pulse["icp_score"] == 92
    assert pulse["project_url"] == "https://pulsefit.ai"
    assert pulse["domain"] == "pulsefit.ai"
    assert pulse["founder_x"] == "https://x.com/pulsefit_app"
    assert pulse["username"] == "pulsefit_founder"
    assert pulse["telegram_verified"] == [{"username": "pulsefit_founder", "url": "https://t.me/pulsefit_founder"}]
    assert pulse["discord_url"] == "https://discord.gg/pulsefit"
    assert pulse["discord_invite"] == "pulsefit"
    assert pulse["discord_owner"] == "alex_pulse"
    assert pulse["source_url"] == "https://whop.com/discover/pulsefit/"

    vault = by_proj["VaultProp"]
    assert vault["source"] == "whop_content_rewards"
    assert vault["segment"] == "whale"
    assert vault["pool_usd"] == 10000
    assert vault["campaign_status"] == "finished"
    assert vault["icp_score"] == 92
    assert vault["project_url"] == "https://vaultprop.com"
    assert vault["domain"] == "vaultprop.com"
    assert vault["discord_url"] == "https://discord.com/invite/vaultprop"
    assert vault["discord_invite"] == "vaultprop"

    mid = by_proj["MidScale AI"]
    assert mid["pool_usd"] == 7500
    assert mid["campaign_status"] == "active"
    assert mid["icp_score"] == 85
    assert mid["project_url"] == "https://midscale.io"

    # Lowering whop_min_pool_usd via config includes TinyBudget App ($1,500)
    low_cap_rows = buyer_public.whop_rows(
        fetch_fn=lambda _url: html_page,
        cfg={"whales": {"whop_min_pool_usd": 1000}},
    )
    assert any(r["project"] == "TinyBudget App" and r["pool_usd"] == 1500 for r in low_cap_rows)


def test_extract_next_data_json_campaigns_and_verified_budget_signal():
    next_payload = {
        "props": {
            "pageProps": {
                "campaigns": [
                    {
                        "title": "AstraCoach AI",
                        "total_budget": "$55k",
                        "status": "finished",
                        "whop_url": "https://whop.com/discover/astracoach/",
                        "website": "https://astracoach.ai",
                        "discord_url": "https://discord.gg/astracoach",
                        "discord_owner": "coach_founder",
                    },
                    {
                        "title": "SignalBudget Brand",
                        "cpm": "$2.50 per 1,000 views",
                        "status": "active",
                        "website": "https://signalbudget.io",
                        "whop_url": "https://whop.com/discover/signalbudget/",
                        "description": "Content Rewards clipping campaign for creators",
                    },
                    {
                        "title": "BelowThreshold",
                        "total_budget": 4999,
                        "status": "active",
                        "website": "https://belowthreshold.com",
                    },
                ]
            }
        }
    }
    html_with_next = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_payload)}"
        "</script></head><body></body></html>"
    )
    rows = buyer_public.extract_whop_campaigns(html_with_next)
    by_proj = {r["project"]: r for r in rows}

    assert set(by_proj) == {"AstraCoach AI", "SignalBudget Brand"}
    astra = by_proj["AstraCoach AI"]
    assert astra["pool_usd"] == 55000
    assert astra["campaign_status"] == "finished"
    assert astra["icp_score"] == 92
    assert astra["discord_owner"] == "coach_founder"
    assert astra["project_url"] == "https://astracoach.ai"

    sig = by_proj["SignalBudget Brand"]
    assert sig["pool_usd"] is None
    assert sig["campaign_status"] == "active"
    assert sig["icp_score"] == 85
    assert sig["project_url"] == "https://signalbudget.io"


def test_owner_contacts_backward_compatibility_and_noise_filtering():
    html = """
    <div>
      <a href="https://x.com/whop">Whop Platform</a>
      <a href="https://x.com/clipper99/status/18471928374">Clipper Submission</a>
      <a href="https://t.me/contentrewardsbot">Bot</a>
      <a href="https://t.me/joinchat/AAAAAE">Invite</a>
      <a href="https://x.com/brand_owner_x">Owner X</a>
      <a href="https://t.me/brand_owner_tg">Owner TG</a>
    </div>
    """
    rows = buyer_public.owner_contacts(html, source_url=buyer_public.DISCOVER_URL)
    assert len(rows) == 2
    handles = {r.get("founder_x") or r.get("username") for r in rows}
    assert handles == {"https://x.com/brand_owner_x", "brand_owner_tg"}
    for row in rows:
        assert row["source"] == "whop_content_rewards"
        assert row["segment"] == "whale"
        assert row["campaign_status"] == "active"
        assert row["icp_score"] == 85
        assert row["icp_match"] is True


def test_treg_search_rows_filters_pools_extracts_finished_and_enriches_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(buyer_public, "_curl", lambda _u: "")
    monkeypatch.setattr("leadgen.treg_client.check_treg_ready", lambda: True)
    monkeypatch.setattr(
        "leadgen.treg_client.call",
        lambda ep, **kw: {
            "results": [
                {
                    "title": "TradePulse — $101,000 Content Rewards | Whop",
                    "url": "https://whop.com/discover/tradepulse/",
                    "snippet": "Campaign ended · 100% paid · Site: tradepulse.io · Discord: https://discord.gg/tradepulse",
                },
                {
                    "title": "TooSmall — $2,500 Pool",
                    "url": "https://whop.com/discover/toosmall/",
                    "snippet": "https://x.com/toosmall_owner",
                },
                {
                    "title": "Terms of Service",
                    "url": "https://whop.com/terms/",
                    "snippet": "https://x.com/legal_noise",
                },
            ]
        },
    )

    def _fake_discord(api_url: str):
        assert "tradepulse" in api_url
        return {
            "guild": {"name": "TradePulse", "description": "Official https://tradepulse.io X: https://x.com/tradepulse_hq"},
            "inviter": {"username": "tp_ceo"},
            "approximate_member_count": 4200,
        }

    rows = buyer_public.whop_rows(discord_fetch_fn=_fake_discord)
    # _curl fails in offline unit env without mock -> only treg results returned
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "TradePulse"
    assert row["pool_usd"] == 101000
    assert row["campaign_status"] == "finished"
    assert row["icp_score"] == 92
    assert row["project_url"] == "https://tradepulse.io"
    assert row["domain"] == "tradepulse.io"
    assert row["discord_invite"] == "tradepulse"
    assert row["discord_owner"] == "tp_ceo"
    assert row["founder_x"] == "https://x.com/tradepulse_hq"


def test_whop_rows_flow_cleanly_into_contact_waterfall():
    html = """
    <article class="campaign-card">
      <h3>KetoBoost Labs</h3>
      <p>Recently finished · $15,000 budget · Content Rewards</p>
      <a href="https://whop.com/discover/ketoboost/">Whop</a>
      <a href="https://ketoboost.com">Website</a>
      <a href="https://x.com/ketoboost">X</a>
      <a href="https://discord.gg/ketoboost">Discord</a>
      <span>Discord Owner: @keto_founder</span>
    </article>
    """
    rows = buyer_public.whop_rows(fetch_fn=lambda _u: html)
    assert len(rows) == 1
    lead = rows[0]
    assert lead["icp_score"] == 92
    assert lead["pool_usd"] == 15000

    treg_calls: list[tuple[str, dict]] = []

    def _fake_treg(ep: str, **kw):
        treg_calls.append((ep, kw))
        if ep == "contactout.people.phone.available":
            return {"available": True}
        if ep == "crawl4ai.people.phone.find":
            return {"phone": "+14155550199"}
        if ep == "hlrlookup.people.phone.validate":
            return {"line_type": "MOBILE"}
        return {}

    enriched = contact_waterfall.enrich_lead_contacts(
        lead,
        allow_paid_tier2=True,
        http_get_fn=lambda u: "<html><a href='mailto:ceo@ketoboost.com'>Email</a></html>",
        dm_finder_fn=lambda company, domain: {
            "founder_name": "Samir Patel",
            "founder_linkedin": "https://www.linkedin.com/in/samirpatel",
            "work_email": "samir@ketoboost.com",
        },
        tg_bridge_fn=lambda _r: {},
        treg_fn=_fake_treg,
    )

    assert enriched["source"] == "whop_content_rewards"
    assert enriched["segment"] == "whale"
    assert enriched["pool_usd"] == 15000
    assert enriched["campaign_status"] == "finished"
    assert enriched["discord_url"] == "https://discord.gg/ketoboost"
    assert enriched["discord_invite"] == "ketoboost"
    assert enriched["discord_owner"] == "keto_founder"
    assert enriched["project_url"] == "https://ketoboost.com"
    assert enriched["founder_linkedin"] == "https://www.linkedin.com/in/samirpatel"
    assert enriched["work_email"] in {"ceo@ketoboost.com", "samir@ketoboost.com"}
    assert enriched["phone"] == "+14155550199"
    assert any(ep == "crawl4ai.people.phone.find" for ep, _ in treg_calls)


def test_buyer_public_cli_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out_file = tmp_path / "buyer_out.jsonl"
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        buyer_public,
        "_curl",
        lambda _u: '<div class="campaign-card"><h3>ClipPro</h3> $10,000 pool <a href="https://clippro.io">Site</a> <a href="https://x.com/clippro">X</a></div>',
    )
    monkeypatch.setattr("leadgen.treg_client.check_treg_ready", lambda: False)
    monkeypatch.setattr(sys, "argv", ["buyer_public.py", "--output", str(out_file), "--min-pool-usd", "5000"])

    buyer_public.main()
    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["project"] == "ClipPro"
    assert lines[0]["pool_usd"] == 10000
    assert lines[0]["source"] == "whop_content_rewards"
    assert lines[0]["segment"] == "whale"


def test_anchor_card_bare_domain_boundary_pool_and_super_whale_cap():
    html = """
    <div class="cards-grid">
      <a class="campaign-card" href="https://whop.com/discover/fundedfox/">
        <h3>FundedFox</h3>
        <span>$5,000 pool · Website: fundedfox.io · Ended</span>
      </a>
      <a class="campaign-card" href="https://whop.com/discover/justunder/">
        <h3>JustUnder</h3>
        <span>$4,999 pool · Website: justunder.io</span>
      </a>
    </div>
    """
    rows = buyer_public.whop_rows(fetch_fn=lambda _u: html)
    assert len(rows) == 1
    r = rows[0]
    assert r["project"] == "FundedFox"
    assert r["pool_usd"] == 5000
    assert r["campaign_status"] == "finished"
    assert r["icp_score"] == 85
    assert r["project_url"] == "https://fundedfox.io"
    assert r["domain"] == "fundedfox.io"
    assert contact_waterfall.resolve_per_lead_cap_usd(r) == pytest.approx(0.05)

    big_rows = buyer_public.whop_rows(
        fetch_fn=lambda _u: '<div class="campaign-card"><h3>MegaApp</h3> $10,000 pool <a href="https://megaapp.ai">Site</a></div>'
    )
    assert len(big_rows) == 1
    assert big_rows[0]["icp_score"] == 92
    assert contact_waterfall.resolve_per_lead_cap_usd(big_rows[0]) == pytest.approx(0.15)


def test_flat_same_handle_merges_and_non_join_loaders_stay_safe():
    same_html = '<a href="https://x.com/same_owner">X</a> <a href="https://t.me/same_owner">TG</a>'
    rows = buyer_public.owner_contacts(same_html, source_url=buyer_public.DISCOVER_URL)
    assert len(rows) == 1
    assert rows[0]["founder_x"] == "https://x.com/same_owner"
    assert rows[0]["username"] == "same_owner"
    assert buyer_public.bloss_rows() == []
    assert buyer_public.propinfa_rows() == []

