from __future__ import annotations

from datetime import date
import inspect
import json
from pathlib import Path
import sys
import threading
import time

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from leadgen import (
    high_yield_veins,
    meta_ad_leads,
    tg_curate,
    tg_llm_qualify,
    treg_social_leads,
    typesafe_leads,
    yc_ph_leads,
)


def test_wave_a_icp_70_threshold_and_prompt_gate_v1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert tg_llm_qualify.MIN_ICP_SCORE == 70
    assert tg_curate.MIN_ICP_SCORE == 70

    prompt = tg_llm_qualify.SYSTEM_PROMPT
    assert "Golden ICP clusters" in prompt
    assert "REJECT" in prompt
    assert "Sniper ICP Gate >= 70" in prompt
    assert "icp_score < 70" in prompt
    golden_head = prompt.split("REJECT", 1)[0].lower()
    for banned in ("roblox", "fortnite", "fivem"):
        assert banned not in golden_head

    monkeypatch.setenv("TYPESAFE_ENABLED", "0")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-key")

    rows = [
        {
            "name": "LowApp",
            "username": "low_65",
            "bio": "Founder @ LowApp https://lowapp.io",
            "project": "LowApp",
            "project_url": "https://lowapp.io",
            "has_monetization_signal": True,
        },
        {
            "name": "FitSeventy",
            "username": "fit_70",
            "bio": "Founder @ FitSeventy — fitness subscription ($12/mo) https://fitseventy.app",
            "project": "FitSeventy",
            "project_url": "https://fitseventy.app",
            "has_monetization_signal": True,
        },
    ]

    monkeypatch.setattr(
        tg_llm_qualify,
        "call_llm_retry",
        lambda batch: [
            {
                "is_lead": True,
                "icp_score": 65,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "LowApp",
                "project_url": "https://lowapp.io",
                "what_they_do": "Small utility app",
            },
            {
                "is_lead": True,
                "icp_score": 70,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "FitSeventy",
                "project_url": "https://fitseventy.app",
                "what_they_do": "Consumer fitness tracker subscription app",
            },
        ],
    )

    kept, dropped = tg_llm_qualify.qualify_loaded(rows, batch_size=10)
    assert dropped == 1
    assert [r["username"] for r in kept] == ["fit_70"]
    assert kept[0]["icp_score"] == 70
    assert kept[0]["segment"] == "whale"

    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    curated = tg_curate.curate_rows(
        [
            {
                "username": "low_65",
                "project": "LowApp",
                "project_url": "https://lowapp.io",
                "category": "SaaS / AI app",
                "role": "Founder",
                "icp_score": 65,
                "monetization": "subscription",
                "what_they_do": "Subscription tool",
            },
            {
                "username": "fit_70",
                "project": "FitSeventy",
                "project_url": "https://fitseventy.app",
                "category": "SaaS / AI app",
                "role": "Founder",
                "icp_score": 70,
                "monetization": "subscription",
                "what_they_do": "Consumer fitness tracker subscription app",
            },
        ]
    )
    assert [r["username"] for r in curated] == ["fit_70"]


def test_wave_a_typesafe_drop_rescue_and_creator_filter_backward_compat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TYPESAFE_CREATOR_DROP", raising=False)
    monkeypatch.delenv("TYPESAFE_CREATOR_KEEP", raising=False)

    # 1) Backward-compatible 1-arg creator_filter behavior
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.25, "typesafe_role": "Dev", "typesafe_category": "SaaS / AI app"}
    ) == "drop"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.35, "typesafe_role": "Other", "typesafe_category": "SaaS / AI app"}
    ) == "drop"

    # 2) has_product_url=True rescues non-rejected Dev/Other when fit > 0.15, still drops <= 0.15 or rejected cat
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.25, "typesafe_role": "Dev", "typesafe_category": "SaaS / AI app"},
        has_product_url=True,
    ) == "uncertain"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.16, "typesafe_role": "Other", "typesafe_category": "E-com / D2C"},
        has_product_url=True,
    ) == "uncertain"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.15, "typesafe_role": "Founder", "typesafe_category": "SaaS / AI app"},
        has_product_url=True,
    ) == "drop"
    assert typesafe_leads.creator_filter(
        {"typesafe_fit": 0.85, "typesafe_role": "Founder", "typesafe_category": "Сервис / агентство"},
        has_product_url=True,
    ) == "drop"

    # 3) qualify_loaded rescue across external product URLs vs platform URLs / low fit / rejected category / non-rescue source
    monkeypatch.setenv("TYPESAFE_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_FAST_PATH", "0")
    monkeypatch.setenv("TYPESAFE_IN_SUBPROCESS", "0")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-key")

    candidates = [
        # Rescued: yc + external project_url + Dev role + fit=0.22
        {
            "name": "DevRescued",
            "username": "yc_rescued",
            "source": "yc",
            "project": "HabitAI",
            "project_url": "https://habitai.app",
            "bio": "Founder @ HabitAI consumer habit tracker ($12/mo)",
            "has_monetization_signal": True,
        },
        # Rescued: meta_ads + website fallback + Other role + fit=0.28
        {
            "name": "MetaRescued",
            "username": "meta_rescued",
            "source": "meta_ads",
            "project": "GlowSerum",
            "website": "https://glowserum.shop",
            "bio": "D2C peptide skincare brand",
            "has_monetization_signal": True,
        },
        # Dropped: fit <= 0.15 even with external URL
        {
            "name": "TooLowFit",
            "username": "low_fit_drop",
            "source": "tg_ad_spy",
            "project": "LowFitApp",
            "project_url": "https://lowfitapp.io",
            "bio": "Consumer app",
        },
        # Dropped: only social/platform URL (t.me), not external product URL
        {
            "name": "SocialOnly",
            "username": "social_only_drop",
            "source": "tg_channel_graph",
            "project": "SocialChan",
            "project_url": "https://t.me/socialchan",
            "bio": "Channel owner",
        },
        # Dropped: rejected category even with external URL
        {
            "name": "AgencyDrop",
            "username": "agency_ts_drop",
            "source": "app_stores",
            "project": "AgencyCo",
            "project_url": "https://agencyco.io",
            "bio": "Growth team",
        },
        # Dropped: source not in rescue allowlist
        {
            "name": "OtherSourceDrop",
            "username": "other_src_drop",
            "source": "random_forum",
            "project": "RandomApp",
            "project_url": "https://randomapp.io",
            "bio": "App maker",
        },
    ]

    ts_map = {
        "yc_rescued": {"typesafe_fit": 0.22, "typesafe_category": "SaaS / AI app", "typesafe_role": "Dev"},
        "meta_rescued": {"typesafe_fit": 0.28, "typesafe_category": "E-com / D2C", "typesafe_role": "Other"},
        "low_fit_drop": {"typesafe_fit": 0.15, "typesafe_category": "SaaS / AI app", "typesafe_role": "Founder"},
        "social_only_drop": {"typesafe_fit": 0.25, "typesafe_category": "SaaS / AI app", "typesafe_role": "Dev"},
        "agency_ts_drop": {"typesafe_fit": 0.65, "typesafe_category": "Сервис / агентство", "typesafe_role": "Founder"},
        "other_src_drop": {"typesafe_fit": 0.25, "typesafe_category": "SaaS / AI app", "typesafe_role": "Dev"},
    }

    async def _fake_typesafe(rows):
        return [ts_map[r["username"]] for r in rows]

    monkeypatch.setattr(tg_llm_qualify, "qualify_creator_rows", _fake_typesafe)
    monkeypatch.setattr(tg_llm_qualify, "creator_filter", typesafe_leads.creator_filter)

    llm_seen_users: list[str] = []

    def _fake_llm(batch):
        llm_seen_users.extend(r["username"] for r in batch)
        return [
            {
                "is_lead": True,
                "icp_score": 78,
                "monetization": "subscription" if r["username"] == "yc_rescued" else "ecom",
                "short_form_fit": True,
                "category": "SaaS / AI app" if r["username"] == "yc_rescued" else "E-com / D2C",
                "role": "Founder",
                "project": r["project"],
                "project_url": r.get("project_url") or r.get("website") or "",
                "what_they_do": r["bio"],
            }
            for r in batch
        ]

    monkeypatch.setattr(tg_llm_qualify, "call_llm_retry", _fake_llm)

    kept, dropped = tg_llm_qualify.qualify_loaded(candidates, batch_size=10)
    assert llm_seen_users == ["yc_rescued", "meta_rescued"]
    assert [r["username"] for r in kept] == ["yc_rescued", "meta_rescued"]
    assert dropped == 4

    q_rows = [
        json.loads(ln)
        for ln in (tmp_path / "quarantined_unqualified.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert {r["username"]: r["quarantine_reason"] for r in q_rows} == {
        "low_fit_drop": "typesafe_drop",
        "social_only_drop": "typesafe_drop",
        "agency_ts_drop": "typesafe_drop",
        "other_src_drop": "typesafe_drop",
    }


def test_wave_a_meta_ad_leads_throughput_defaults_and_main_query_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sig_countries = inspect.signature(meta_ad_leads.countries_for_day)
    assert sig_countries.parameters["count"].default == 6
    assert len(meta_ad_leads.countries_for_day(date(2026, 11, 23))) == 6

    if not meta_ad_leads.os.getenv("META_ADS_CAP"):
        assert meta_ad_leads.CAP == 80

    # Gate V1 preservation
    sq_low = " | ".join(meta_ad_leads.SEARCH_QUERIES).lower()
    for banned in ("roblox", "fortnite", "fivem"):
        assert banned not in sq_low

    captured: dict = {}

    def _fake_collect(**kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(meta_ad_leads, "collect", _fake_collect)
    out_file = tmp_path / "meta.jsonl"
    seen_file = tmp_path / "meta_seen.json"
    meta_ad_leads.main(["--output", str(out_file), "--seen", str(seen_file)])

    assert captured["query_count"] == 12
    assert captured["cap"] == meta_ad_leads.CAP


def test_wave_a_high_yield_veins_parallel_budgets_lock_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sig = inspect.signature(high_yield_veins.collect_all_veins)
    assert sig.parameters["limit"].default == 80

    budgets_seen: dict[str, list[int]] = {}
    barrier = threading.Barrier(4, timeout=2.0)
    thread_ids: set[int] = set()

    def _make_mock(name: str, delay_sec: float):
        def _runner(*, limit: int, day: date, seen: dict, call_budget: list[int], **kwargs):
            budgets_seen[name] = call_budget
            thread_ids.add(threading.get_ident())
            barrier.wait()
            time.sleep(delay_sec)
            row = {
                "id": f"{name}:item1",
                "source": name,
                "project": f"Proj_{name}",
                "project_url": f"https://{name}.example.com",
            }
            new_seen = dict(seen)
            new_seen[f"{name}:item1"] = day.isoformat()
            return [row], new_seen

        return _runner

    # Staggered delays: whop finishes slowest, d2c fastest -> deterministic order must still follow cursor-rotated veins!
    monkeypatch.setattr(high_yield_veins, "collect_whop_content_rewards", _make_mock("whop", 0.06))
    monkeypatch.setattr(high_yield_veins, "collect_tiktok_creative_ads", _make_mock("tiktok_ads", 0.04))
    monkeypatch.setattr(high_yield_veins, "collect_tiktok_shop_brands", _make_mock("tiktok_shop", 0.02))
    monkeypatch.setattr(high_yield_veins, "collect_d2c_whatsapp_brands", _make_mock("d2c_whatsapp", 0.00))

    rows, updated_seen = high_yield_veins.collect_all_veins(
        day=date(2026, 11, 23),
        seen={"preexisting:k": "2026-11-22"},
        veins=("whop", "tiktok_ads", "tiktok_shop", "d2c_whatsapp"),
        cursor=1,  # rotates to: tiktok_ads, tiktok_shop, d2c_whatsapp, whop
    )

    # All 4 ran concurrently (barrier succeeded + multiple worker thread ids)
    assert len(thread_ids) == 4
    # Independent [20] budgets (distinct list objects)
    assert set(budgets_seen) == {"whop", "tiktok_ads", "tiktok_shop", "d2c_whatsapp"}
    assert all(b == [20] for b in budgets_seen.values())
    assert len({id(b) for b in budgets_seen.values()}) == 4
    # Deterministic order matches rotated ordered_veins regardless of completion latency
    assert [r["source"] for r in rows] == ["tiktok_ads", "tiktok_shop", "d2c_whatsapp", "whop"]
    # merged_seen retained preexisting + all 4 veins
    for key in ("preexisting:k", "whop:item1", "tiktok_ads:item1", "tiktok_shop:item1", "d2c_whatsapp:item1"):
        assert key in updated_seen


def test_wave_a_treg_social_leads_exclusions_and_noise_prefilter() -> None:
    required_excl = ('-"for hire"', '-"available for"', '-"dm for"', '-"airdrop"', '-"giveaway"', '-"jam track"')
    for q in (*treg_social_leads._BUYER_CORE, *treg_social_leads._CLASSIC_CORE):
        for tok in required_excl:
            assert tok in q, f"Missing {tok} in query: {q}"

    noisy_samples = [
        ("[FOR HIRE] Virtual assistant ready to help", ""),
        ("Available for hire! DM me for rates and portfolio https://va.example.com", ""),
        ("New Fortnite Festival jam track drop, looking for a duo https://x.com/jam", ""),
        ("Massive $500 USDT giveaway & airdrop! Like & follow + tag 3 friends https://drop.io", ""),
        ("Scaling content today https://neutral.example.com", "Experienced virtual assistant available for hire, DM for rates"),
    ]
    for text, bio in noisy_samples:
        assert treg_social_leads.is_social_noise_post(text, bio) is True, (text, bio)

    valid_samples = [
        ("Looking for UGC creators for our skincare brand https://glowserum.shop", ""),
        ("We are hiring clippers for our Whop content rewards campaign https://clipapp.io", ""),
        ("Need AI UGC videos for our fitness app https://fitapp.io", ""),
        ("Hiring an experienced VA for our Shopify brand https://d2cshop.com", ""),
    ]
    for text, bio in valid_samples:
        assert treg_social_leads.is_social_noise_post(text, bio) is False, (text, bio)

    # discover() drops spam posts before emitting JSONL rows
    rows, seen_out, _stats = treg_social_leads.discover(
        day=date(2026, 11, 23),
        seen={},
        cap=10,
        search_x_fn=lambda q: {
            "posts": [
                {
                    "authorUsername": "va_spammer",
                    "authorName": "VA Spammer",
                    "text": "Virtual assistant for hire! DM me for rates https://vaspam.io",
                    "url": "https://x.com/va_spammer/status/1",
                    "isReply": False,
                },
                {
                    "authorUsername": "airdrop_bot",
                    "authorName": "Airdrop Bot",
                    "text": "Huge token airdrop & giveaway! Drop your SOL wallet https://airdrop.io",
                    "url": "https://x.com/airdrop_bot/status/2",
                    "isReply": False,
                },
                {
                    "authorUsername": "real_founder",
                    "authorName": "Real Founder",
                    "text": "We need AI UGC videos for our subscription app https://realsaas.io",
                    "url": "https://x.com/real_founder/status/3",
                    "isReply": False,
                },
            ]
        }
        if "need AI UGC" in q
        else {"posts": []},
        search_reddit_fn=lambda s, t: {"posts": []},
        search_comments_fn=lambda u: {"posts": []},
    )
    assert [r["founder_x"] for r in rows] == ["https://x.com/real_founder"]
    assert "x:va_spammer" in seen_out
    assert "x:airdrop_bot" in seen_out


def test_wave_a_yc_ph_leads_non_consumer_industry_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    non_consumer_cases = [
        {"name": "BioFold", "industries": ["Healthcare", "Therapeutics"], "one_liner": "AI therapeutics for oncology"},
        {"name": "GeneEdit", "tags": ["Biotech", "Synthetic Biology"], "one_liner": "CRISPR platform for wet labs"},
        {"name": "AeroStrike", "industries": ["Industrials", "Defense"], "one_liner": "Autonomous defense drones for tactical ISR"},
        {"name": "OrbitSat", "industries": ["Industrials", "Aviation and Space"], "one_liner": "Orbital satellite propulsion systems"},
        {"name": "RoboForge", "industries": ["Industrials", "Manufacturing and Robotics"], "one_liner": "Humanoid robots for industrial manufacturing"},
        {"name": "ChipFab", "tags": ["Semiconductors"], "one_liner": "Photonic ASIC chip design for datacenters"},
        {"name": "AirCapture", "industries": ["Industrials", "Climate"], "one_liner": "Direct air capture climate hardware"},
        {"name": "KubeTrace", "tags": ["DevOps", "Developer Tools"], "one_liner": "Kubernetes observability and eBPF OpenTelemetry SDK"},
    ]
    for co in non_consumer_cases:
        assert yc_ph_leads.is_non_consumer_yc_company(co) is True, co

    consumer_cases = [
        {"name": "AstroCoach", "industries": ["Consumer", "AI"], "one_liner": "AI astrology and coaching subscription app"},
        {"name": "GlowSerum", "industries": ["Consumer", "E-Commerce"], "one_liner": "D2C peptide skincare and collagen brand"},
        {"name": "FundedPeak", "industries": ["Fintech"], "one_liner": "Prop trading challenge platform for futures traders"},
        {"name": "VitalSleep", "industries": ["Healthcare", "Consumer Health and Wellness"], "one_liner": "Consumer sleep and longevity fitness tracker"},
        {"name": "ClipMagic", "industries": ["Consumer", "Content"], "one_liner": "AI short-form video editor for creators"},
        {"name": "MetricFlow", "industries": ["B2B", "SaaS"], "one_liner": "AI SaaS marketing analytics for D2C brands"},
    ]
    for co in consumer_cases:
        assert yc_ph_leads.is_non_consumer_yc_company(co) is False, co

    # scrape_yc drops non-consumer Algolia hits BEFORE fetching yc_company_details
    details_called: list[str] = []
    monkeypatch.setattr(yc_ph_leads, "yc_algolia_key", lambda: ("app", "key"))
    monkeypatch.setattr(
        yc_ph_leads,
        "yc_fetch_batch",
        lambda app, key, batch, limit, page=0: [
            {
                "slug": "bio-therapeutics",
                "name": "BioTherapeutics",
                "one_liner": "AI drug discovery for oncology therapeutics",
                "industries": ["Healthcare", "Therapeutics"],
                "website": "https://biotherapeutics.io",
            },
            {
                "slug": "kube-obs",
                "name": "KubeObs",
                "one_liner": "Kubernetes observability SDK for devops",
                "tags": ["DevOps", "Developer Tools"],
                "website": "https://kubeobs.dev",
            },
            {
                "slug": "pulse-fit",
                "name": "PulseFit",
                "one_liner": "Consumer AI fitness and nutrition coaching subscription app",
                "industries": ["Consumer", "Health"],
                "website": "https://pulsefit.app",
            },
        ]
        if batch == yc_ph_leads.YC_BATCHES[0]
        else [],
    )
    monkeypatch.setattr(
        yc_ph_leads,
        "yc_company_details",
        lambda slug: (
            details_called.append(slug),
            {
                "founders": [{"full_name": "Alex Rivera", "twitter_url": "https://x.com/alex_pulse"}],
                "twitter": "pulsefit",
                "one_liner": "Consumer AI fitness and nutrition coaching subscription app",
                "website": "https://pulsefit.app",
                "batch": "Winter 2027",
                "industries": ["Consumer"],
            },
        )[1],
    )
    monkeypatch.setattr(yc_ph_leads.time, "sleep", lambda _: None)

    yc_rows = yc_ph_leads.scrape_yc(limit=5)
    assert details_called == ["pulse-fit"]
    assert [r["name"] for r in yc_rows] == ["PulseFit"]
