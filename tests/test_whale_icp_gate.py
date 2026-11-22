from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from leadgen import supply_side, tg_llm_qualify


def test_tg_llm_qualify_system_prompt_bans_gaming_solo_cheap_ecom_and_requires_ltv():
    prompt = tg_llm_qualify.SYSTEM_PROMPT
    prompt_low = prompt.lower()

    assert "creatoros" not in prompt_low
    assert "SoftUGC" in prompt
    assert "E-com / D2C" in prompt
    assert "revshare" not in prompt_low

    golden_match = re.search(
        r"Golden ICP clusters.*?\n(.*?)\n\s*REJECT\b",
        prompt,
        flags=re.S,
    )
    assert golden_match is not None, "Missing Golden ICP clusters section"
    golden_block = golden_match.group(1).lower()
    for banned_in_golden in ("gaming", "roblox", "fortnite", "fivem", "mobile & pc games"):
        assert banned_in_golden not in golden_block, (
            f"Found banned cluster term {banned_in_golden!r} in Golden ICP clusters"
        )

    reject_match = re.search(r"REJECT\b.*?\n(.*?)\n\s*For EACH lead", prompt, flags=re.S)
    assert reject_match is not None, "Missing REJECT section"
    reject_block = reject_match.group(1).lower()
    for required_ban in (
        "gaming",
        "roblox",
        "fortnite",
        "fivem",
        "low-cpm",
        "solopreneurs",
        "indie hackers without revenue",
        "micro-arbitrage",
        "low-margin e-com",
    ):
        assert required_ban in reject_block, f"Missing {required_ban!r} in REJECT section"

    assert "$50-$100+" in prompt
    assert "ltv" in prompt_low
    assert "icp_score" in prompt_low
    assert "Gaming / AI" not in prompt


def test_supply_side_deterministic_rejects_gaming_solopreneurs_and_cheap_ecom():
    # Hard platform-gaming bans stay rejected EVERYWHERE (generic + whale)
    for hard_cat in (
        "Gaming / Roblox",
        "Gaming / Fortnite",
        "Gaming / FiveM",
        "Roblox",
        "Fortnite",
        "FiveM",
        "UEFN",
    ):
        assert supply_side.is_rejected_buyer_category(hard_cat) is True, hard_cat
        assert supply_side.is_whale_rejected_category(hard_cat) is True, hard_cat

    # Generic gaming & Whale-only exclusions stay rejected by whale_icp_reject_reason / is_whale_rejected_category,
    # while NOT blocking legacy manual CSV campaigns in generic is_rejected_buyer_category
    for whale_only_cat in (
        "Gaming",
        "GameDev",
        "Low-CPM Gaming",
        "Mobile Games",
        "PC Games",
        "Solopreneur",
        "Indie Hacker",
        "Micro-Arbitrage",
        "Low-Margin E-com",
        "Cheap E-com",
        "Dropshipping",
        "Дропшиппинг на Shopify",
    ):
        assert supply_side.is_rejected_buyer_category(whale_only_cat) is False, whale_only_cat
        assert supply_side.is_whale_rejected_category(whale_only_cat) is True, whale_only_cat

    for allowed_cat in (
        "SaaS / AI app",
        "E-com / D2C",
        "Проп трейдинг",
        "CEX / биржа",
        "DeFi / DEX",
        "Платёжки / финтех",
        "Медиа / контент",
        "Gaming / AI",
    ):
        assert supply_side.is_rejected_buyer_category(allowed_cat) is False, allowed_cat
        assert supply_side.is_whale_rejected_category(allowed_cat) is False, allowed_cat

    hard_platform_samples = [
        {"project": "BloxTycoon", "bio": "Building Roblox tycoon experiences with gamepasses"},
        {"project": "BloxMaps", "project_url": "https://devforum.roblox.com/t/123", "bio": "Game launch"},
        {"project": "FNMaps", "bio": "Fortnite UEFN creative map code studio"},
        {"project": "LosSantosRP", "bio": "FiveM GTA RP roleplay server owner", "project_url": "https://cfx.re/join/abc"},
    ]
    for sample in hard_platform_samples:
        assert supply_side.sniper_reject_reason(sample) != "", sample
        assert supply_side.whale_icp_reject_reason(sample) != "", sample

    # Legacy manual CSV campaigns with generic gaming or dropshipping pass generic sniper_reject_reason,
    # while whale_icp_reject_reason blocks all of them deterministically
    legacy_manual_allowed = [
        {"name": "Darius", "category": "Gaming", "what_they_do": "Initial hello — saw your game"},
        {"name": "PewPew", "category": "AI", "their_angle": "Free multiplayer space shooter .io game in your browser."},
        {"name": "Андрей", "niche": "Дропшиппинг на Shopify"},
    ]
    for legacy_row in legacy_manual_allowed:
        assert supply_side.sniper_reject_reason(legacy_row) == "", legacy_row
        assert supply_side.whale_icp_reject_reason(legacy_row) != "", legacy_row

    whale_rejected_samples = [
        {"project": "TapStudio", "bio": "Low-CPM hyper-casual mobile game studio publishing ad-monetized games"},
        {"project": "PixelQuest", "bio": "Indie PC roguelike game studio launching on Steam"},
        {"project": "MobileRPG", "bio": "Mobile RPG browser game studio"},
        {"project": "SoloOS", "bio": "Solopreneur building my own AI micro-SaaS"},
        {"project": "WeekendTool", "bio": "Indie hacker without revenue yet, building a free tool in public"},
        {"project": "TinyNotes", "bio": "Solo founder, $500 MRR side project built over the weekend"},
        {"project": "BuildLog", "bio": "Hobby project for tracking bookmarks, $0 MRR"},
        {"project": "ArbDesk", "bio": "Micro-arbitrage media buying across native push traffic"},
        {"project": "CaseDrop", "bio": "Cheap low-margin e-com dropshipping $4 phone cases from AliExpress"},
        {"project": "GadgetBin", "bio": "One-product dropshipping store selling cheap trinkets"},
        {"project": "LowLtvCo", "bio": "Consumer app", "ltv_usd": 18},
        {
            "project": "MobileArcade",
            "category": "Gaming / AI",
            "monetization": "iap",
            "what_they_do": "Mobile arcade game with gems",
        },
    ]
    for sample in whale_rejected_samples:
        assert supply_side.whale_icp_reject_reason(sample) != "", f"Expected whale rejection for {sample}"


def test_supply_side_keeps_high_ltv_whales_crypto_fintech_infobiz_and_major_ecom():
    valid_whales = [
        {
            "name": "Elena Vance",
            "project": "AstroCoach",
            "project_url": "https://astrocoach.app",
            "bio": "Founder @ AstroCoach — AI astrology & coaching subscription app ($14.99/mo, LTV $120)",
            "category": "SaaS / AI app",
            "role": "Founder",
            "ltv_usd": 120,
        },
        {
            "name": "Liam Carter",
            "project": "FundedPeak",
            "project_url": "https://fundedpeak.io",
            "bio": "CEO at FundedPeak — prop trading challenge for futures traders ($50K-$200K accounts)",
            "category": "Проп трейдинг",
            "role": "Founder",
        },
        {
            "name": "Marcus Chen",
            "project": "PhantomPay",
            "project_url": "https://phantompay.io",
            "bio": "Head of Growth at PhantomPay — crypto B2C wallet & fintech debit card",
            "category": "Платёжки / финтех",
            "role": "Marketing",
        },
        {
            "name": "David Brooks",
            "project": "ScaleAcademy",
            "project_url": "https://scaleacademy.io",
            "bio": "Founder @ ScaleAcademy — high-ticket trader education & cohort ($80K/mo revenue)",
            "category": "Медиа / контент",
            "role": "Founder",
        },
        {
            "name": "Chloe Park",
            "project": "LuminaSkin",
            "project_url": "https://luminaskin.com",
            "bio": "VP Growth at LuminaSkin — D2C skincare & collagen supplements ($2M ARR on Shopify)",
            "category": "E-com / D2C",
            "role": "Marketing",
        },
        {
            "name": "Indie Habit App",
            "project": "Indie Habit",
            "project_url": "https://indiehabit.app",
            "bio": "Build better habits — iOS subscription app",
            "category": "SaaS / AI app",
            "role": "Founder",
        },
    ]
    for row in valid_whales:
        assert supply_side.sniper_reject_reason(row) == "", row
        assert supply_side.whale_icp_reject_reason(row) == "", row
        tagged = supply_side.tag_whale_lead(row)
        assert tagged["segment"] == "whale", row


def test_qualify_loaded_enforces_whale_gate_even_if_llm_accepts_and_tags_whale_segment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("TYPESAFE_ENABLED", "0")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-key")

    candidates = [
        # Caught deterministically in pre-gate
        {
            "name": "Jake",
            "username": "jake_roblox",
            "bio": "Making Roblox obby games with Robux passes",
            "project": "ObbyWorld",
            "project_url": "https://obbyworld.gg",
        },
        # Vague bio passes pre-gate, LLM tries to pass it as a mobile game studio -> caught post-LLM!
        {
            "name": "Ken",
            "username": "ken_stealth",
            "bio": "Founder building consumer products https://tapdash.gg",
            "project": "TapDash",
            "project_url": "https://tapdash.gg",
        },
        # Vague bio passes pre-gate, LLM reveals cheap dropshipping -> caught post-LLM!
        {
            "name": "Sam",
            "username": "sam_store",
            "bio": "Ecommerce brand owner https://casetrend.shop",
            "project": "CaseTrend",
            "project_url": "https://casetrend.shop",
        },
        # Vague bio passes pre-gate, LLM scores < 80 -> caught by MIN_ICP_SCORE
        {
            "name": "Nina",
            "username": "nina_low",
            "bio": "Building consumer app https://ninalow.app",
            "project": "NinaLow",
            "project_url": "https://ninalow.app",
        },
        # Valid high-LTV Whale SaaS
        {
            "name": "Elena",
            "username": "elena_astro",
            "bio": "Founder @ AstroCoach — AI astrology subscription ($19.99/mo)",
            "project": "AstroCoach",
            "project_url": "https://astrocoach.app",
            "has_monetization_signal": True,
        },
        # Valid Whale Prop Trading
        {
            "name": "Liam",
            "username": "liam_prop",
            "bio": "CEO @ FundedPeak — futures prop trading challenges",
            "project": "FundedPeak",
            "project_url": "https://fundedpeak.io",
            "has_monetization_signal": True,
        },
    ]

    def _mock_llm(batch: list[dict]) -> list[dict]:
        # Only 5 rows reach LLM because jake_roblox is dropped in gate_before_model
        assert [r["username"] for r in batch] == [
            "ken_stealth",
            "sam_store",
            "nina_low",
            "elena_astro",
            "liam_prop",
        ]
        return [
            {
                "is_lead": True,
                "icp_score": 88,
                "monetization": "iap",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "TapDash",
                "project_url": "https://tapdash.gg",
                "what_they_do": "Low-CPM hyper-casual mobile game studio",
            },
            {
                "is_lead": True,
                "icp_score": 84,
                "monetization": "ecom",
                "short_form_fit": True,
                "category": "E-com / D2C",
                "role": "Founder",
                "project": "CaseTrend",
                "project_url": "https://casetrend.shop",
                "what_they_do": "Cheap low-margin dropshipping store for $3 phone cases",
            },
            {
                "is_lead": True,
                "icp_score": 72,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "NinaLow",
                "project_url": "https://ninalow.app",
                "what_they_do": "Small unscaled tracker app without growth budget",
            },
            {
                "is_lead": True,
                "icp_score": 93,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Founder",
                "project": "AstroCoach",
                "project_url": "https://astrocoach.app",
                "what_they_do": "High-LTV AI astrology subscription app ($19.99/mo)",
            },
            {
                "is_lead": True,
                "icp_score": 91,
                "monetization": "cpa_deposit",
                "short_form_fit": True,
                "category": "Проп трейдинг",
                "role": "Founder",
                "project": "FundedPeak",
                "project_url": "https://fundedpeak.io",
                "what_they_do": "Futures prop trading challenges with paid evaluations",
            },
        ]

    monkeypatch.setattr(tg_llm_qualify, "call_llm_retry", _mock_llm)
    kept, dropped = tg_llm_qualify.qualify_loaded(candidates, batch_size=10)

    assert dropped == 4
    assert [r["username"] for r in kept] == ["elena_astro", "liam_prop"]
    assert all(r["qualified"] is True and r["llm_qualified"] is True for r in kept)
    assert all(r["segment"] == "whale" for r in kept)

    q_rows = [
        json.loads(line)
        for line in (tmp_path / "quarantined_unqualified.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    q_by_user = {r["username"]: r["quarantine_reason"] for r in q_rows}
    assert q_by_user["jake_roblox"].startswith("pre_gate:")
    assert q_by_user["ken_stealth"].startswith("whale_reject:")
    assert q_by_user["sam_store"].startswith("whale_reject:")
    assert q_by_user["nina_low"] == "low_icp_score:72"


def test_whale_segment_helpers_preserve_non_whale_segments_and_typesafe_fast_path(
    monkeypatch: pytest.MonkeyPatch,
):
    assert (
        supply_side.classify_whale_segment(
            {"project": "VacLead", "segment": "vacancy", "icp_score": 90, "qualified": True}
        )
        == "vacancy"
    )
    assert (
        supply_side.classify_whale_segment(
            {"project": "RuExp", "segment": "ru_expert", "icp_score": 90, "qualified": True}
        )
        == "ru_expert"
    )
    assert (
        supply_side.classify_whale_segment(
            {"project": "LowScore", "icp_score": 79, "monetization": "subscription"}
        )
        == ""
    )
    assert (
        supply_side.classify_whale_segment(
            {"project": "FortniteMap", "bio": "Fortnite UEFN creator", "icp_score": 95}
        )
        == ""
    )

    non_whale_row = {"project": "VacLead", "segment": "vacancy", "icp_score": 90, "qualified": True}
    assert tg_llm_qualify.annotate_whale_segment(non_whale_row)["segment"] == "vacancy"

    # TypeSafe fast-path drops Roblox even if TypeSafe marks keep, and tags valid Whale with segment='whale'
    monkeypatch.setenv("TYPESAFE_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_FAST_PATH", "1")
    monkeypatch.setenv("TYPESAFE_IN_SUBPROCESS", "0")
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(tg_llm_qualify, "API_KEY", "test-key")

    async def _fake_typesafe(rows):
        return [
            {"typesafe_fit": 0.92, "typesafe_category": "SaaS / AI app", "typesafe_role": "Founder"}
            for _ in rows
        ]

    monkeypatch.setattr(tg_llm_qualify, "qualify_creator_rows", _fake_typesafe)
    monkeypatch.setattr(tg_llm_qualify, "creator_filter", lambda ts: "keep")
    monkeypatch.setattr(
        tg_llm_qualify,
        "call_llm_retry",
        lambda batch: [{"is_lead": False, "icp_score": 10} for _ in batch],
    )

    out, dropped = tg_llm_qualify.qualify_loaded(
        [
            {
                "name": "BloxFounder",
                "username": "blox_ts",
                "bio": "Roblox simulator studio",
                "project": "BloxSim",
                "has_monetization_signal": True,
            },
            {
                "name": "LunaFounder",
                "username": "luna_ts",
                "bio": "Founder @ LunaChart — AI astrology subscription ($19/mo)",
                "project": "LunaChart",
                "project_url": "https://lunachart.app",
                "has_monetization_signal": True,
            },
        ],
        batch_size=10,
    )
    assert dropped == 1
    assert len(out) == 1
    assert out[0]["username"] == "luna_ts"
    assert out[0]["typesafe_qualified"] is True
    assert out[0]["segment"] == "whale"


def test_supply_side_russian_ugc_buyer_and_vacancy_social_manual_segments():
    # Russian buyer inquiries for UGC creators are NOT flagged as creator self-pitch
    for buyer_phrase in (
        "Ищем UGC creator в команду для съёмки роликов",
        "Нужен UGC creator на проект под Reels",
        "Требуется UGC для бренда косметики",
        "Найти UGC creator с опытом в товарке",
        "Заказ на UGC creator под короткие видео",
    ):
        assert supply_side.is_supply_side(buyer_phrase) is False, buyer_phrase

    # Creator self-pitch remains flagged
    assert supply_side.is_supply_side("UGC creator — мои кейсы / looking for brand deals") is True

    # Vacancy / client_reels segments allow benign job-posting terms while blocking job-seekers and Roblox/Fortnite/FiveM
    for vac_seg in ("vacancy", "vacancy_assisted", "client_reels"):
        for valid_job in (
            {
                "segment": vac_seg,
                "name": "FitBox Delivery",
                "bio": "Ищем freelancer / ugc creator с portfolio для роликов нашей marketing agency и open-source проекта",
            },
            {
                "segment": vac_seg,
                "name": "AutoImport Pro",
                "what_they_do": "Нужен UGC creator, присылайте portfolio",
            },
        ):
            assert supply_side.sniper_reject_reason(valid_job) == "", valid_job

        for bad_job in (
            {"segment": vac_seg, "bio": "[FOR HIRE] Video editor looking for work"},
            {"segment": vac_seg, "bio": "Монтажер рилс, ищу работу и ищу заказы"},
            {"segment": vac_seg, "bio": "Need editor for Roblox obby game"},
            {"segment": vac_seg, "bio": "Fortnite UEFN trailer job"},
            {"segment": vac_seg, "bio": "FiveM server promo"},
        ):
            assert supply_side.sniper_reject_reason(bad_job) != "", bad_job

    # social_manual allows whale targets mentioning soft portfolio / open-source SDK / Russian UGC buying text
    for valid_manual in (
        {
            "segment": "social_manual",
            "project": "LoomaAI",
            "bio": "Founder @ LoomaAI — enterprise AI video SaaS with an open-source SDK and product portfolio",
        },
        {
            "segment": "social_manual",
            "project": "GlowBrand",
            "bio": "Free, open-source mac app companion for our D2C suite; Ищем UGC creator",
        },
    ):
        assert supply_side.sniper_reject_reason(valid_manual) == "", valid_manual

    for bad_manual in (
        {"segment": "social_manual", "bio": "FINPR Agency — Web3 PR & Growth agency"},
        {"segment": "social_manual", "bio": "[FOR HIRE] Freelance marketer, ищу работу"},
        {"segment": "social_manual", "bio": "UGC creator — мои кейсы / looking for brand deals"},
        {"segment": "social_manual", "bio": "Roblox tycoon simulator studio"},
    ):
        assert supply_side.sniper_reject_reason(bad_manual) != "", bad_manual
