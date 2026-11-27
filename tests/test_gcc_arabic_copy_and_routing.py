from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db
from app.outreach import (
    assert_campaign_copy_language,
    create_campaign,
    preview_outreach_batch,
    update_campaign,
)
from leadgen.language_router import (
    CAMPAIGN_GCC_MENA_TG,
    CAMPAIGN_GCC_MENA_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
    EXPLICIT_SEGMENTS,
    LeadRoute,
    assert_copy_matches_language,
    detect_lead_language,
    is_gcc_mena_lead,
    resolve_target_campaign,
    route_lead,
)
from leadgen.outreach_copy import (
    PROFILE_METADATA,
    SUPPORTED_COPY_PROFILES,
    VLAD_STEP3_TEXT,
    build_outreach_sequence,
    check_copy_language,
    clean_brand_name,
    generate_outreach_copy_batch,
    resolve_copy_profile,
)

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def test_arabic_language_detection_and_validation() -> None:
    assert detect_lead_language({"bio": "عيادة متخصصة في زراعة الأسنان بالرياض"}) == "ar"
    assert detect_lead_language({"language_hint": "ar", "company": "Adam Health"}) == "ar"
    assert detect_lead_language({"language": "ar-SA", "company": "Riyadh Clinic"}) == "ar"
    assert detect_lead_language({"country_code": "AE", "company": "Dubai Crypto App"}) == "en"
    assert detect_lead_language({"name": "Иван", "language_hint": "ar"}) == "ru"

    valid_ar = (
        "السلام عليكم، ندير منظومة SoftUGC لتوزيع فيديوهات AI UGC عبر TikTok و Snapchat "
        "و WhatsApp بتكلفة $1.5k شهرياً. هل نرسل 3 فيديوهات تجريبية مجانية؟"
    )
    assert assert_copy_matches_language(valid_ar, "ar") == (True, "")
    assert check_copy_language(valid_ar, "ar") == (True, "")
    assert assert_campaign_copy_language(valid_ar, "ar") == (True, "")

    ok_cyr, reason_cyr = assert_copy_matches_language(
        "السلام عليكم، ندير منظومة SoftUGC في الرياض — привет", "ar"
    )
    assert ok_cyr is False
    assert reason_cyr

    ok_en_leak, _ = assert_copy_matches_language(
        "مرحبا! We run organic short-form video distribution across our owned network.", "ar"
    )
    assert ok_en_leak is False

    ok_empty, _ = assert_copy_matches_language("SoftUGC TikTok $1.5k", "ar")
    assert ok_empty is False


def test_gcc_december_vs_global_routing_and_is_gcc_mena_lead() -> None:
    assert "gcc_mena" in EXPLICIT_SEGMENTS
    assert is_gcc_mena_lead({"country_code": "SA"}) is True
    assert is_gcc_mena_lead({"website": "https://adamhealth.ae"}) is True
    assert is_gcc_mena_lead({"bio": "عيادة تجميل في دبي"}) is True
    assert is_gcc_mena_lead({"language_hint": "ar"}) is True
    assert is_gcc_mena_lead({"source": "ar"}) is True
    assert is_gcc_mena_lead({"country_code": "US", "website": "https://acme.com"}) is False

    en_dubai_whale = {
        "source": "meta_ads",
        "company": "DesertGlow UAE",
        "country_code": "AE",
        "website": "https://desertglow.ae",
        "username": "desertglow_founder",
    }
    # Global profile leaves EN GCC routing 100% unchanged
    assert route_lead(en_dubai_whale, cfg={"active_market_profile": "global"}) == LeadRoute(
        language="en",
        segment="whale",
        provider="telegram",
        target_campaign=CAMPAIGN_WHALES_EN_TG,
    )
    assert route_lead(en_dubai_whale, provider="whatsapp") == LeadRoute(
        language="en",
        segment="whale",
        provider="whatsapp",
        target_campaign=CAMPAIGN_WHALES_EN_WA,
    )

    # gcc_december profile routes GCC/MENA whales & B2B into GCC / MENA campaigns
    assert route_lead(en_dubai_whale, cfg={"active_market_profile": "gcc_december"}) == LeadRoute(
        language="en",
        segment="gcc_mena",
        provider="telegram",
        target_campaign=CAMPAIGN_GCC_MENA_TG,
    )
    ar_ksa_wa = {
        "source": "follower_graph",
        "company": "عيادة آدم",
        "country_code": "SA",
        "bio": "عيادة طبية متخصصة في الرياض",
        "whatsapp": "https://wa.me/966500000001",
    }
    assert route_lead(ar_ksa_wa, cfg={"active_market_profile": "gcc_december"}) == LeadRoute(
        language="ar",
        segment="gcc_mena",
        provider="whatsapp",
        target_campaign=CAMPAIGN_GCC_MENA_WA,
    )
    assert resolve_target_campaign("b2b", "ar", "telegram") == CAMPAIGN_GCC_MENA_TG
    assert resolve_target_campaign("gcc_mena", "en", "whatsapp") == CAMPAIGN_GCC_MENA_WA


def test_gcc_mena_four_sequences_and_profiles() -> None:
    assert "gcc_mena_ar" in SUPPORTED_COPY_PROFILES
    assert "gcc_mena_en" in SUPPORTED_COPY_PROFILES
    assert PROFILE_METADATA["gcc_mena_ar"]["allows_step3_image"] is False
    assert PROFILE_METADATA["gcc_mena_en"]["allows_step3_image"] is False
    assert resolve_copy_profile("gcc_mena", language="ar") == "gcc_mena_ar"
    assert resolve_copy_profile("gcc_mena", language="en") == "gcc_mena_en"
    assert clean_brand_name("عيادة آدم", allow_arabic=True) == "عيادة آدم"

    legacy_brand = "Creator" + "OS"

    # 1. AR + fix_retainer (Adam Health clinic/D2C anchor)
    seq_ar_fix = build_outreach_sequence(
        {
            "company": "عيادة آدم",
            "category": "Clinic / HealthTech",
            "business_type": "clinic_healthtech",
            "offer_model": "fix_retainer",
            "gcc_tone": "brand",
            "country_code": "SA",
        },
        copy_profile="gcc_mena_ar",
    )
    assert seq_ar_fix.language == "ar"
    assert seq_ar_fix.step3_image_url is None
    assert [s.delay_hours for s in seq_ar_fix.steps] == [0, 48, 96]
    assert "$1.5k" in seq_ar_fix.step1_text
    assert "$1.2k" in seq_ar_fix.step1_text
    assert "CPA" in seq_ar_fix.step1_text
    assert "3 فيديوهات تجريبية مجانية" in seq_ar_fix.step1_text
    assert "عيادة آدم" in seq_ar_fix.step1_text
    for txt in (seq_ar_fix.step1_text, seq_ar_fix.step2_text, seq_ar_fix.step3_text):
        assert legacy_brand.lower() not in txt.lower()
        assert not _CYRILLIC_RE.search(txt)
        assert _ARABIC_RE.search(txt)
        assert check_copy_language(txt, "ar")[0] is True

    # 2. AR + revshare_cpa (growth tone for crypto/infobiz, brand tone for apps)
    seq_ar_growth = build_outreach_sequence(
        {
            "company": "DubaiAlpha",
            "business_type": "crypto_infobiz",
            "offer_model": "revshare_cpa",
            "gcc_tone": "growth",
            "language_hint": "ar",
        },
        copy_profile="gcc_mena_ar",
    )
    seq_ar_brand = build_outreach_sequence(
        {
            "company": "RiyadhPay",
            "business_type": "ai_saas_app",
            "offer_model": "revshare_cpa",
            "gcc_tone": "brand",
            "language_hint": "ar",
        },
        copy_profile="gcc_mena_ar",
    )
    assert seq_ar_growth.step1_text != seq_ar_brand.step1_text
    for seq_ar in (seq_ar_growth, seq_ar_brand):
        assert seq_ar.step3_image_url is None
        assert "revshare" in seq_ar.step1_text.lower()
        assert "CPA" in seq_ar.step1_text
        assert "3 فيديوهات تجريبية مجانية" in seq_ar.step1_text
        for txt in (seq_ar.step1_text, seq_ar.step2_text, seq_ar.step3_text):
            assert legacy_brand.lower() not in txt.lower()
            assert not _CYRILLIC_RE.search(txt)
            assert check_copy_language(txt, "ar")[0] is True

    # 3. EN + fix_retainer
    seq_en_fix = build_outreach_sequence(
        {
            "company": "Adam Health",
            "business_type": "clinic_healthtech",
            "offer_model": "fix_retainer",
            "gcc_tone": "brand",
            "country_code": "SA",
        },
        copy_profile="gcc_mena_en",
    )
    assert seq_en_fix.language == "en"
    assert seq_en_fix.step3_image_url is None
    assert "$1.5k" in seq_en_fix.step1_text
    assert "$1.2k" in seq_en_fix.step1_text
    assert "3 free demo reels" in seq_en_fix.step1_text
    assert seq_en_fix.step3_text != VLAD_STEP3_TEXT
    assert "doing me dirty" not in seq_en_fix.step3_text.lower()

    # 4. EN + revshare_cpa (growth tone)
    seq_en_rev = build_outreach_sequence(
        {
            "company": "GulfTrade",
            "business_type": "crypto_infobiz",
            "offer_model": "revshare_cpa",
            "gcc_tone": "growth",
            "country_code": "AE",
        },
        copy_profile="gcc_mena_en",
    )
    assert seq_en_rev.language == "en"
    assert seq_en_rev.step3_image_url is None
    assert "revshare" in seq_en_rev.step1_text.lower()
    assert "cpa" in seq_en_rev.step1_text.lower()
    assert seq_en_rev.step3_text != VLAD_STEP3_TEXT
    for txt in (seq_en_fix.step1_text, seq_en_fix.step2_text, seq_en_fix.step3_text, seq_en_rev.step1_text, seq_en_rev.step2_text, seq_en_rev.step3_text):
        assert legacy_brand.lower() not in txt.lower()
        assert not _CYRILLIC_RE.search(txt)
        assert check_copy_language(txt, "en")[0] is True

    # LLM batch validation rejects wrong offer_model / Cyrillic and falls back cleanly
    seq_bad_llm = build_outreach_sequence(
        {"company": "عيادة النور", "offer_model": "fix_retainer", "language_hint": "ar"},
        copy_profile="gcc_mena_ar",
        use_llm=True,
        llm_result={"step1_text": "Привет, делаем ролики", "audience": "buyers"},
    )
    assert seq_bad_llm.used_llm is False
    assert "$1.5k" in seq_bad_llm.step1_text

    batch = generate_outreach_copy_batch(
        [{"company": "عيادة النور", "offer_model": "fix_retainer", "language_hint": "ar"}],
        copy_profile="gcc_mena_ar",
        llm_post_fn=lambda **_: [{"i": 0, "step1_text": "Привет, делаем ролики", "audience": "buyers"}],
    )
    assert "$1.5k" in batch[0]["step1_text"]
    assert "Привет" not in batch[0]["step1_text"]
    assert check_copy_language(batch[0]["step1_text"], "ar")[0] is True


def test_arabic_campaign_draft_and_preview_outreach_batch_strict(tmp_path: Path) -> None:
    conn = connect(tmp_path / "gcc_ar_campaign.db")
    init_db(conn)
    try:
        acc_id = int(
            conn.execute(
                """
                INSERT INTO accounts (
                    provider, phone, session_path, telegram_name, telegram_username,
                    status, health, daily_limit, created_at, updated_at
                ) VALUES (
                    'telegram', '+966500000001', 'data/sessions/gcc_sender.session',
                    'GCC Sender', 'gcc_sender', 'connected', 'active', 20,
                    '2026-11-27T10:00:00Z', '2026-11-27T10:00:00Z'
                )
                """
            ).lastrowid
        )
        camp = create_campaign(
            conn,
            name="GCC / MENA — TG",
            account_ids=[acc_id],
            daily_limit=20,
            status="draft",
            segment="gcc_mena",
            language="ar",
            copy_profile="gcc_mena_ar",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        )
        assert camp["status"] == "draft"
        assert camp["language"] == "ar"
        assert camp["copy_profile"] == "gcc_mena_ar"

        # update_campaign preserves language="ar" and validates Arabic steps
        updated = update_campaign(
            conn,
            camp["id"],
            daily_limit=25,
            language="ar",
        )
        assert updated["daily_limit"] == 25
        assert updated["language"] == "ar"

        # Seed 3 fresh GCC Arabic leads
        for idx in range(3):
            lead_id = int(
                conn.execute(
                    """
                    INSERT INTO leads (
                        username, name, status, company, url, niche, segment, language,
                        created_at, updated_at
                    ) VALUES (?, ?, 'new', ?, ?, 'D2C Skincare', 'gcc_mena', 'ar', '2026-11-27T10:00:00Z', '2026-11-27T10:00:00Z')
                    """,
                    (
                        f"gcc_founder_{idx}",
                        f"خالد {idx}",
                        f"نور بيوتي {idx}",
                        f"https://noor{idx}.sa",
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, lead_id, username, name, company, website, category,
                    current_step, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'D2C Skincare', 0, 'pending', '2026-11-27T10:00:00Z', '2026-11-27T10:00:00Z')
                """,
                (
                    camp["id"],
                    lead_id,
                    f"gcc_founder_{idx}",
                    f"خالد {idx}",
                    f"نور بيوتي {idx}",
                    f"https://noor{idx}.sa",
                ),
            )
        conn.commit()

        preview = preview_outreach_batch(conn, camp["id"], sample_size=3, strict=True)
        assert preview["ok"] is True, f"Unexpected preview violations: {preview.get('violations')}"
        assert len(preview["previews"]) == 3
        for item in preview["previews"]:
            assert item["image_url"] is None
            assert item["forbidden_patterns"] == []
            assert "SoftUGC" in item["message_text"]
            assert "$1.5k" in item["message_text"]
            assert "3 فيديوهات تجريبية مجانية" in item["message_text"]
            assert not _CYRILLIC_RE.search(item["message_text"])
    finally:
        conn.close()
