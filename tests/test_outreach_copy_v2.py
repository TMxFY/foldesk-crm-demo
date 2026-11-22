import json
from pathlib import Path
import sys
import urllib.request

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from leadgen.outreach_copy import (
    DEFAULT_VACANCY_SHOWCASE_URL,
    REVSHARE_OFFER_TEXT,
    SOFTUGC_PITCH_TEXT,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_IMAGE_URL,
    VLAD_STEP3_TEXT,
    WHALE_OFFER_TEXT,
    WHALE_WHATSAPP_STEP3_TEXT,
    OutreachSequence,
    build_outreach_sequence,
    check_copy_language,
    first_touch,
    followup_one,
    followup_two,
    generate_outreach_copy_batch,
    generate_vlad_sonnet_batch,
    render_whale_offer,
    resolve_copy_profile,
    step3_image_for_profile,
    upgrade_to_vlad_step1,
    whop_content_rewards_hook,
)


def test_whale_offer_text_and_whale_mass_default_have_zero_revshare_or_cpa():
    offer_low = WHALE_OFFER_TEXT.lower()
    assert "revshare" not in offer_low
    assert "rev-share" not in offer_low
    assert "cpa" not in offer_low
    assert "183.5m views in 90 days" in offer_low
    assert "owned account network" in offer_low
    assert "1 500+ reels/mo" in offer_low
    assert "13-40x cheaper than standard clippers" in offer_low
    assert "want to see 3 demo reels for your brand?" in offer_low
    assert "—" not in WHALE_OFFER_TEXT and "–" not in WHALE_OFFER_TEXT

    rendered = render_whale_offer("Kalshi")
    assert rendered.endswith("Want to see 3 demo reels for Kalshi?")
    assert "revshare" not in rendered.lower()
    assert "cpa" not in rendered.lower()

    # Profile resolution: copy_profile="whale_mass" and segment="whale" both default to whale_mass
    assert resolve_copy_profile("whale_mass") == "whale_mass"
    assert resolve_copy_profile(None, segment="whale") == "whale_mass"
    assert resolve_copy_profile("default", segment="whale") == "whale_mass"

    # Legacy first_touch without profile/segment stays backwards-compatible
    legacy_s1 = first_touch("Acme", "B2B workflow SaaS", category="SaaS")
    assert REVSHARE_OFFER_TEXT in legacy_s1

    # whale_mass via copy_profile or segment="whale" NEVER uses REVSHARE_OFFER_TEXT
    s1_profile = first_touch(
        "Kalshi",
        "Prediction market app for US traders",
        category="Fintech",
        copy_profile="whale_mass",
    )
    s1_segment = first_touch(
        "Kalshi",
        "Prediction market app for US traders",
        category="Fintech",
        segment="whale",
    )
    assert s1_profile == s1_segment
    assert REVSHARE_OFFER_TEXT not in s1_profile
    assert "revshare" not in s1_profile.lower()
    assert "cpa" not in s1_profile.lower()
    assert "183.5M views in 90 days" in s1_profile
    assert "owned account network" in s1_profile.lower()
    assert "1 500+ reels/mo" in s1_profile
    assert "13-40x cheaper than standard clippers" in s1_profile
    assert "Want to see 3 demo reels for Kalshi?" in s1_profile

    seq = build_outreach_sequence(
        {
            "company": "Kalshi",
            "category": "Fintech",
            "their_angle": "Prediction market app",
            "segment": "whale",
        }
    )
    assert isinstance(seq, OutreachSequence)
    assert seq.copy_profile == "whale_mass"
    assert seq.language == "en"
    assert seq.provider == "telegram"
    assert [s.delay_hours for s in seq.steps] == [0, 48, 96]
    assert seq.step1_text == s1_profile
    assert "(which equals highly qualified users)" in seq.step2_text
    assert seq.step3_text == VLAD_STEP3_TEXT
    assert seq.step3_image_url == VLAD_STEP3_IMAGE_URL


def test_whop_content_rewards_personalized_hook_threshold_and_copy():
    hook_25k = whop_content_rewards_hook("Polymarket", 25000)
    assert "$25,000" in hook_25k
    assert "Content Rewards" in hook_25k
    assert "next cycle" in hook_25k
    assert "parallel test" in hook_25k
    assert "without junk clips" in hook_25k

    # Exact threshold 5000 triggers hook; < 5000 or None does not
    assert "$5,000" in whop_content_rewards_hook("WhopBrand", 5000)
    assert "$10,000" in whop_content_rewards_hook("WhopBrand", "$10,000")
    assert whop_content_rewards_hook("WhopBrand", 4999) == ""
    assert whop_content_rewards_hook("WhopBrand", None) == ""

    seq_whop = build_outreach_sequence(
        {
            "company": "Polymarket",
            "source": "whop_content_rewards",
            "pool_usd": 25000,
            "segment": "whale",
            "category": "Crypto",
            "their_angle": "Whop clipping bounty campaign",
        }
    )
    assert "$25,000 Content Rewards campaign" in seq_whop.step1_text
    assert "next cycle or a parallel test" in seq_whop.step1_text
    assert "without junk clips" in seq_whop.step1_text
    assert "Want to see 3 demo reels for Polymarket?" in seq_whop.step1_text
    assert "revshare" not in seq_whop.step1_text.lower()
    assert "cpa" not in seq_whop.step1_text.lower()

    # Sub-5000 pool falls back to normal pain paragraph without $4,000 Content Rewards hook
    seq_small = build_outreach_sequence(
        {
            "company": "SmallBrand",
            "source": "whop_content_rewards",
            "pool_usd": 4000,
            "segment": "whale",
            "category": "SaaS",
        }
    )
    assert "Content Rewards campaign" not in seq_small.step1_text
    assert "Saw SmallBrand." in seq_small.step1_text


def test_whatsapp_provider_never_attaches_meme_image_or_meme_caption():
    assert step3_image_for_profile("whale_mass", provider="telegram") == VLAD_STEP3_IMAGE_URL
    assert step3_image_for_profile("whale_mass", provider="whatsapp") is None
    assert (
        step3_image_for_profile(
            "whale_mass",
            provider="whatsapp",
            custom_step_templates={"step3_image_url": VLAD_STEP3_IMAGE_URL},
        )
        is None
    )

    seq_wa = build_outreach_sequence(
        {
            "company": "FinFlow",
            "category": "Fintech",
            "their_angle": "Corporate treasury app",
            "segment": "whale",
        },
        provider="whatsapp",
    )
    assert seq_wa.provider == "whatsapp"
    assert seq_wa.step3_image_url is None
    assert all(s.image_url is None for s in seq_wa.steps)
    assert seq_wa.step3_text != VLAD_STEP3_TEXT
    assert "doing me dirty" not in seq_wa.step3_text.lower()
    assert "FinFlow" in seq_wa.step3_text
    assert seq_wa.steps[2].template_text == seq_wa.step3_text

    # Even if custom_step_templates accidentally puts VLAD_STEP3_TEXT and image on WhatsApp, it is blocked
    seq_wa_override = build_outreach_sequence(
        {"company": "FinFlow", "segment": "whale"},
        provider="whatsapp",
        custom_step_templates={
            "step3": VLAD_STEP3_TEXT,
            "step3_image_url": VLAD_STEP3_IMAGE_URL,
        },
    )
    assert seq_wa_override.step3_image_url is None
    assert seq_wa_override.step3_text != VLAD_STEP3_TEXT

    f2_wa = followup_two("FinFlow", provider="whatsapp", copy_profile="whale_mass")
    assert f2_wa != VLAD_STEP3_TEXT
    assert "FinFlow" in f2_wa
    assert WHALE_WHATSAPP_STEP3_TEXT.split("{brand}")[0] in f2_wa


def test_vacancy_ru_and_vacancy_en_profiles():
    seq_ru = build_outreach_sequence(
        {
            "company": "Онлайн-школа Прогресс",
            "role": "Рилсмейкер на AI-ролики",
            "source": "hh_ru",
            "their_angle": "Нужен поток рилс для школы",
        },
        copy_profile="vacancy_ru",
        showcase_url="https://softugc.com/vacancies",
    )
    assert seq_ru.copy_profile == "vacancy_ru"
    assert seq_ru.language == "ru"
    assert [s.delay_hours for s in seq_ru.steps] == [0, 24, 72]
    assert seq_ru.step3_image_url is None
    assert "система" in seq_ru.step1_text.lower() or "систему лидгена" in seq_ru.step1_text.lower()
    assert "https://softugc.com/vacancies" in seq_ru.step1_text
    assert "183,5" in seq_ru.step1_text
    assert "$80" not in seq_ru.step1_text
    # Step 2 introduces test series details ($80-100 / 10-15s)
    assert "$80-100" in seq_ru.step2_text
    assert "тестов" in seq_ru.step2_text.lower()
    assert "актуальн" in seq_ru.step3_text.lower() or "открыта" in seq_ru.step3_text.lower()
    assert check_copy_language(seq_ru.step1_text, "ru")[0] is True
    assert check_copy_language(seq_ru.step2_text, "ru")[0] is True
    assert check_copy_language(seq_ru.step3_text, "ru")[0] is True

    # Default showcase URL fallback works when showcase_url omitted
    seq_ru_default = build_outreach_sequence(
        {"company": "Студия", "segment": "vacancy", "language": "ru"},
    )
    assert seq_ru_default.copy_profile == "vacancy_ru"
    assert DEFAULT_VACANCY_SHOWCASE_URL in seq_ru_default.step1_text

    # vacancy_en profile
    seq_en = build_outreach_sequence(
        {
            "company": "MetricPulse",
            "role": "Short-form AI Video Producer",
            "segment": "vacancy",
            "language": "en",
        }
    )
    assert seq_en.copy_profile == "vacancy_en"
    assert seq_en.language == "en"
    assert [s.delay_hours for s in seq_en.steps] == [0, 24, 72]
    assert seq_en.step3_image_url is None
    assert "leadgen system" in seq_en.step1_text.lower()
    assert DEFAULT_VACANCY_SHOWCASE_URL in seq_en.step1_text
    assert "$80" not in seq_en.step1_text
    assert "$80-100" in seq_en.step2_text
    assert check_copy_language(seq_en.step1_text, "en")[0] is True
    assert check_copy_language(seq_en.step2_text, "en")[0] is True
    assert check_copy_language(seq_en.step3_text, "en")[0] is True


def test_ru_expert_and_social_manual_bridge_profiles():
    # ru_expert: 2-step (0h / 48h), soft personal from Tim RbM, NO meme, NO 100-300K pricing in Step 1
    seq_exp = build_outreach_sequence(
        {
            "name": "Алексей Смирнов",
            "company": "Канал про личные финансы",
            "segment": "ru_expert",
        }
    )
    assert seq_exp.copy_profile == "ru_expert"
    assert seq_exp.language == "ru"
    assert len(seq_exp.steps) == 2
    assert [s.delay_hours for s in seq_exp.steps] == [0, 48]
    assert seq_exp.step3_text == ""
    assert seq_exp.step3_image_url is None
    assert "Привет, Алексей!" in seq_exp.step1_text
    assert "Tim RbM" in seq_exp.step1_text
    assert "100" not in seq_exp.step1_text and "300" not in seq_exp.step1_text
    assert check_copy_language(seq_exp.step1_text, "ru")[0] is True
    assert check_copy_language(seq_exp.step2_text, "ru")[0] is True

    # social_manual_bridge: 1-line EN or RU DM asking WhatsApp vs Telegram for 15-sec custom sample reel
    seq_bridge_en = build_outreach_sequence(
        {
            "name": "Sarah Jenkins",
            "company": "AuraFit",
            "segment": "social_manual",
        },
        language="en",
    )
    assert seq_bridge_en.copy_profile == "social_manual_bridge"
    assert seq_bridge_en.language == "en"
    assert len(seq_bridge_en.steps) == 1
    assert [s.delay_hours for s in seq_bridge_en.steps] == [0]
    assert "\n" not in seq_bridge_en.step1_text
    assert seq_bridge_en.step2_text == ""
    assert seq_bridge_en.step3_text == ""
    assert seq_bridge_en.step3_image_url is None
    assert "Hey Sarah" in seq_bridge_en.step1_text
    assert "AuraFit" in seq_bridge_en.step1_text
    assert "15-sec custom sample reel" in seq_bridge_en.step1_text
    assert "WhatsApp" in seq_bridge_en.step1_text and "Telegram" in seq_bridge_en.step1_text

    # Auto-detect RU for social_manual_bridge when lead has Cyrillic/RU hints
    seq_bridge_ru = build_outreach_sequence(
        {
            "name": "Мария Козлова",
            "company": "Школа Вокала",
            "copy_profile": "social_manual_bridge",
        }
    )
    assert seq_bridge_ru.language == "ru"
    assert len(seq_bridge_ru.steps) == 1
    assert "\n" not in seq_bridge_ru.step1_text
    assert "Привет, Мария!" in seq_bridge_ru.step1_text
    assert "15-сек" in seq_bridge_ru.step1_text
    assert "WhatsApp" in seq_bridge_ru.step1_text and "Telegram" in seq_bridge_ru.step1_text
    assert check_copy_language(seq_bridge_ru.step1_text, "ru")[0] is True


def test_custom_step_templates_overrides_legacy_brand_scrub_and_language_lock():
    # JSON string custom_step_templates_json overrides steps, delays, showcase_url, and scrubs legacy CreatorOS
    legacy_brand = "Creator" + "OS"
    custom_json = json.dumps(
        {
            "showcase_url": "https://softugc.com/reels",
            "step1": f"Добрый день! Система лидгена {legacy_brand} для {{brand}} ({{role}}). Витрина: {{showcase_url}}",
            "step2": "Тест-серия для {brand}: покажем 10-15 сек фрагмент.",
            "step3": "Подскажите, задача по {brand} актуальна?",
            "step2_delay_hours": 36,
        },
        ensure_ascii=False,
    )
    seq = build_outreach_sequence(
        {"company": "ФитнесБокс", "role": "AI видео"},
        copy_profile="vacancy_ru",
        custom_step_templates=custom_json,
    )
    assert "SoftUGC" in seq.step1_text
    assert legacy_brand.lower() not in seq.step1_text.lower()
    assert "https://softugc.com/reels" in seq.step1_text
    assert "ФитнесБокс" in seq.step1_text
    assert seq.steps[1].delay_hours == 36
    assert "Тест-серия для ФитнесБокс" in seq.step2_text

    # List-shaped "steps" override works too
    seq_list_ov = build_outreach_sequence(
        {"company": "WhaleCo", "segment": "whale"},
        custom_step_templates={
            "steps": [
                {"step_order": 1, "delay_hours": 0, "template_text": f"Hey, {legacy_brand} built 1 500+ reels/mo for {{brand}}."},
                {"step_order": 2, "delay_hours": 48, "template_text": "Quick check for {brand} ({audience})."},
            ]
        },
    )
    assert "SoftUGC" in seq_list_ov.step1_text
    assert legacy_brand.lower() not in seq_list_ov.step1_text.lower()
    assert "WhaleCo" in seq_list_ov.step1_text
    assert "Quick check for WhaleCo (users)." == seq_list_ov.step2_text

    # Strict language lock: mismatching copy_profile + language raises ValueError
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence({"company": "Acme"}, copy_profile="whale_mass", language="ru")
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence({"company": "Школа"}, copy_profile="vacancy_ru", language="en")
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence({"company": "Школа"}, copy_profile="ru_expert", language="en")
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence({"company": "Acme"}, copy_profile="vacancy_en", language="ru")

    # Strict language lock on custom_step_templates content
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence(
            {"company": "Школа"},
            copy_profile="vacancy_ru",
            custom_step_templates={"step1": "Hey, pure English custom text for a Russian campaign!"},
        )
    with pytest.raises(ValueError, match="Language mismatch"):
        build_outreach_sequence(
            {"company": "Acme"},
            copy_profile="whale_mass",
            custom_step_templates={"step1": "Привет, русский текст в английскую кампанию китов!"},
        )


def test_upgrade_to_vlad_step1_strips_revshare_for_whale_mass_and_whop():
    legacy_brand = "Creator" + "OS"
    old_draft = (
        "Hey Alex,\n\n"
        "Saw Floopi. Getting steady crypto merchant leads gets expensive once paid social fatigues.\n\n"
        f"We run {legacy_brand} — organic AI short-form distribution with 183.5M views in 90 days and zero shoots. "
        "We work on revshare or CPA and you only cover production costs. Want to see 3 example reels?"
    )
    upgraded_whale = upgrade_to_vlad_step1(
        old_draft,
        company="Floopi",
        category="Fintech",
        copy_profile="whale_mass",
    )
    assert upgraded_whale.startswith("Hey,\n\nSaw Floopi.")
    assert legacy_brand.lower() not in upgraded_whale.lower()
    assert "SoftUGC" in upgraded_whale
    assert "revshare" not in upgraded_whale.lower()
    assert "cpa" not in upgraded_whale.lower()
    assert REVSHARE_OFFER_TEXT not in upgraded_whale
    assert "1 500+ reels/mo" in upgraded_whale
    assert "owned account network" in upgraded_whale.lower()
    assert "Want to see 3 demo reels for Floopi?" in upgraded_whale

    # Whop lead with pool_usd >= 5000 gets upgraded with Content Rewards hook
    upgraded_whop = upgrade_to_vlad_step1(
        old_draft,
        company="WhopKing",
        source="whop_content_rewards",
        pool_usd=15000,
        segment="whale",
    )
    assert "$15,000 Content Rewards campaign" in upgraded_whop
    assert "without junk clips" in upgraded_whop
    assert "revshare" not in upgraded_whop.lower()


def test_llm_guided_generation_for_whales_ru_profiles_and_fallbacks(monkeypatch):
    legacy_brand = "Creator" + "OS"
    calls: list[dict] = []

    def _fake_llm(*, system_prompt, items, model, timeout):
        calls.append({"system_prompt": system_prompt, "items": items, "model": model})
        first = items[0]
        if "Content Rewards" in system_prompt and first.get("source") == "whop_content_rewards":
            return [
                {
                    "i": idx,
                    "pain": f"Saw {it['brand']}'s $20,000 Content Rewards campaign. We can run your next cycle or a parallel test through our owned account network without junk clips.",
                    "pitch": f"We run {legacy_brand}: 183.5M views in 90 days across our owned account network, shipping 1 500+ reels/mo, 13-40x cheaper than standard clippers.",
                    "audience": "users",
                }
                for idx, it in enumerate(items)
            ]
        if "Tim RbM" in system_prompt:
            return [
                {
                    "i": it["i"],
                    "step1_text": f"Привет! Это Тим (Tim RbM), увидел {it['brand'] or 'твой канал'}. Помогаем выстроить продюсирование и контент-воронку под ключ. Удобно созвониться на 15 минут?",
                    "audience": "buyers",
                }
                for it in items
            ]
        return []

    # 1. Whale Whop LLM-guided batch scrubs legacy brand, keeps scale + Whop hook, NO revshare/CPA
    whale_res = generate_outreach_copy_batch(
        [
            {
                "company": "BountyApp",
                "source": "whop_content_rewards",
                "pool_usd": 20000,
                "category": "SaaS",
                "their_angle": "Running clipper rewards on Whop",
            }
        ],
        copy_profile="whale_mass",
        provider="whatsapp",
        llm_post_fn=_fake_llm,
    )
    assert len(whale_res) == 1
    w0 = whale_res[0]
    assert w0["used_llm"] is True
    assert w0["provider"] == "whatsapp"
    assert w0["step3_image_url"] is None
    assert w0["step3_text"] != VLAD_STEP3_TEXT
    assert "$20,000 Content Rewards campaign" in w0["step1_text"]
    assert "SoftUGC" in w0["step1_text"]
    assert legacy_brand.lower() not in w0["step1_text"].lower()
    assert "revshare" not in w0["step1_text"].lower()
    assert "cpa" not in w0["step1_text"].lower()
    assert w0["step1_text"].endswith("Want to see 3 demo reels for BountyApp?")

    # 2. If LLM sneaks revshare/CPA into whale_mass, validator rejects it and falls back cleanly to deterministic whale_mass
    def _bad_revshare_llm(*, system_prompt, items, model, timeout):
        return [
            {
                "i": 0,
                "pain": "Saw BadLLM. Scaling reels is hard.",
                "pitch": "We run SoftUGC on revshare and CPA with 183.5M views in 90 days.",
                "audience": "users",
            }
        ]

    safe_whale = generate_vlad_sonnet_batch(
        [{"company": "SafeCo", "segment": "whale", "category": "Fintech"}],
        llm_post_fn=_bad_revshare_llm,
    )
    assert len(safe_whale) == 1
    assert "revshare" not in safe_whale[0]["step1_text"].lower()
    assert "cpa" not in safe_whale[0]["step1_text"].lower()
    assert "Want to see 3 demo reels for SafeCo?" in safe_whale[0]["step1_text"]

    # 3. ru_expert LLM-guided batch + custom system_prompt override
    exp_res = generate_outreach_copy_batch(
        [{"name": "Егор", "company": "Блог про крипту"}],
        copy_profile="ru_expert",
        custom_step_templates={"system_prompt": f"Упомяни подход {legacy_brand} мягко."},
        llm_post_fn=_fake_llm,
    )
    assert exp_res[0]["used_llm"] is True
    assert "Tim RbM" in exp_res[0]["step1_text"]
    assert len(exp_res[0]["steps"]) == 2
    assert exp_res[0]["step3_image_url"] is None
    assert "Упомяни подход SoftUGC мягко." in calls[-1]["system_prompt"]
    assert legacy_brand.lower() not in calls[-1]["system_prompt"].lower()

    # 4. Deterministic fallback when no API key is configured and use_llm=True
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    det_res = generate_outreach_copy_batch(
        [{"company": "NoKeyCo", "segment": "social_manual", "name": "Dan"}],
        use_llm=True,
    )
    assert det_res[0]["used_llm"] is False
    assert det_res[0]["copy_profile"] == "social_manual_bridge"
    assert "Hey Dan, put together a quick 15-sec custom sample reel for NoKeyCo" in det_res[0]["step1_text"]

    # 5. Legacy generate_vlad_sonnet_batch via urllib still preserves REVSHARE_OFFER_TEXT when segment != whale
    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    [
                                        {
                                            "i": 0,
                                            "pain": "Saw LegacyCo. Paid ads cost too much.",
                                            "pitch": "We run SoftUGC: 183.5M views in 90 days, no shoots, 13 to 40x less than standard clippers.",
                                            "audience": "users",
                                        }
                                    ]
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResp())
    legacy_batch = generate_vlad_sonnet_batch([{"company": "LegacyCo", "category": "SaaS"}])
    assert legacy_batch[0]["step1_text"].endswith(REVSHARE_OFFER_TEXT)


def test_followup_one_and_two_respect_profiles_and_language():
    f1_vac_ru = followup_one("Студия", copy_profile="vacancy_ru", role="Монтаж Shorts")
    assert "$80-100" in f1_vac_ru
    assert "Монтаж Shorts" in f1_vac_ru
    assert check_copy_language(f1_vac_ru, "ru")[0] is True

    f2_vac_en = followup_two("Acme", copy_profile="vacancy_en", role="Video Editor")
    assert "Video Editor" in f2_vac_en
    assert check_copy_language(f2_vac_en, "en")[0] is True

    f1_whale = followup_one("CohortLab", "Coding bootcamp", category="EdTech", copy_profile="whale_mass")
    assert f1_whale == VLAD_STEP2_TEMPLATE.format(audience="students")
