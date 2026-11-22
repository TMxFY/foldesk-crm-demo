from __future__ import annotations

import sqlite3

import pytest

from leadgen.language_router import (
    CAMPAIGN_OFM_TG,
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
    CAMPAIGN_VACANCY_RU_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
    QUEUE_ASSISTED_JOB,
    QUEUE_MANUAL_SOCIAL,
    LeadRoute,
    assert_copy_matches_language,
    detect_lead_language,
    detect_lead_segment,
    resolve_target_campaign,
    route_lead,
)


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "bio",
        "what_they_do",
        "title",
        "description",
        "note",
        "company",
        "project",
        "sample",
    ],
)
def test_detect_lead_language_cyrillic_fields(field: str) -> None:
    row = {"name": "Alex", "source": "manual", field: "Делаем ролики под ключ"}
    assert detect_lead_language(row) == "ru"


def test_detect_lead_language_cyrillic_by_and_kz_letters() -> None:
    assert detect_lead_language({"bio": "Праект у Мінску ў беларусі"}) == "ru"
    assert detect_lead_language({"company": "Қазақстан Жобасы"}) == "ru"


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_domain", "brand.ru"),
        ("domain", "https://sub.agency.рф/cases"),
        ("website", "https://company.by"),
        ("url", "http://startup.kz:8080/about"),
        ("project_url", "https://xn--e1afmkfd.xn--p1ai"),
        ("email", "founder@studio.ru"),
        ("site_url", "mailto:hr@corp.kz"),
        ("manual_action_url", "https://hh.ru/vacancy/123456"),
        ("links", ["https://example.com", "https://partner.by/promo"]),
    ],
)
def test_detect_lead_language_ru_domains(field: str, value: object) -> None:
    assert detect_lead_language({"name": "Acme", field: value}) == "ru"


@pytest.mark.parametrize(
    "row",
    [
        {"url": "https://ruby.com"},
        {"url": "https://example.com/ru/pricing"},
        {"company_domain": "by.example.com"},
        {"website": "https://kz-app.io"},
        {"domain": "@ivan_ru"},
        {"email": "user@example.com?ref=.ru"},
        {"url": "not a [valid url"},
    ],
)
def test_detect_lead_language_domain_false_positives_stay_en(row: dict[str, object]) -> None:
    assert detect_lead_language(row) == "en"


@pytest.mark.parametrize(
    "src",
    [
        "hh_ru",
        "profi_ru",
        "kwork",
        "ru_experts",
        "tg_jobs_ru",
        "vacancies_ru",
        "tg_jobs",
        "TG Вакансии (@reels_jobs)",
    ],
)
def test_detect_lead_language_ru_sources(src: str) -> None:
    assert detect_lead_language({"name": "ReelMaker", "source": src}) == "ru"
    assert detect_lead_language({"name": "ReelMaker", "lead_source": src}) == "ru"


@pytest.mark.parametrize(
    "src",
    [
        "upwork",
        "linkedin_jobs",
        "meta_ads",
        "whop_content_rewards",
        "ofm_tg",
        "tg_jobs_en",
        "Google Sheet (Все2)",
    ],
)
def test_detect_lead_language_en_sources(src: str) -> None:
    assert detect_lead_language({"name": "Acme Corp", "lead_source": src}) == "en"


def test_detect_lead_language_ignores_stale_default_en_and_handles_sqlite_row() -> None:
    assert detect_lead_language({"language": "en", "name": "Иван"}) == "ru"
    assert detect_lead_language({}) == "en"

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT 'Мария' AS name, 'en' AS language, 'meta_ads' AS source").fetchone()
    assert detect_lead_language(row) == "ru"


@pytest.mark.parametrize(
    "segment",
    [
        "whale",
        "vacancy",
        "client_reels",
        "ru_expert",
        "social_manual",
        "vacancy_assisted",
        "ofm",
    ],
)
def test_detect_lead_segment_explicit_valid(segment: str) -> None:
    assert detect_lead_segment({"segment": segment.upper(), "source": "unknown"}) == segment


def test_detect_lead_segment_b2b_default_falls_through_to_source_inference() -> None:
    assert detect_lead_segment({"segment": "b2b", "source": "meta_ads"}) == "whale"
    assert detect_lead_segment({"segment": "b2b", "source": "hh_ru"}) == "vacancy"
    assert detect_lead_segment({"segment": "b2b", "source": "tg_groups"}) == "b2b"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("ofm", "ofm"),
        ("ofm_tg", "ofm"),
        ("ofm_groups", "ofm"),
        ("ru_experts", "ru_expert"),
        ("ru_expert", "ru_expert"),
        ("hh_ru", "vacancy"),
        ("profi_ru", "vacancy"),
        ("kwork", "vacancy"),
        ("upwork", "vacancy"),
        ("linkedin_jobs", "vacancy"),
        ("tg_jobs", "vacancy"),
        ("tg_jobs_ru", "vacancy"),
        ("job_monitor", "vacancy"),
        ("meta_ads", "whale"),
        ("whop", "whale"),
        ("whop_content_rewards", "whale"),
        ("app_store", "whale"),
        ("app_stores", "whale"),
        ("tiktok_ads", "whale"),
        ("tiktok_shop", "whale"),
        ("crypto", "whale"),
        ("crypto_fintech", "whale"),
        ("yc", "whale"),
        ("ph_detailed", "whale"),
        ("ph", "b2b"),
        ("tg_groups", "b2b"),
        ("", "b2b"),
    ],
)
def test_detect_lead_segment_source_inference(src: str, expected: str) -> None:
    assert detect_lead_segment({"source": src}) == expected
    assert detect_lead_segment({"lead_source": src}) == expected


def test_detect_lead_segment_vacancy_assisted_vs_vacancy() -> None:
    assert (
        detect_lead_segment(
            {
                "source": "hh_ru",
                "provider": "telegram",  # SQLite default without username/id
                "manual_action_url": "https://hh.ru/vacancy/123",
            }
        )
        == "vacancy_assisted"
    )
    assert (
        detect_lead_segment(
            {
                "source": "profi_ru",
                "phone": "+79991112233",
                "whatsapp_exists": False,
                "manual_action_url": "https://profi.ru/order/99",
            }
        )
        == "vacancy_assisted"
    )
    assert detect_lead_segment({"source": "kwork", "assisted": True}) == "vacancy_assisted"
    assert detect_lead_segment({"source": "upwork", "queue": "vacancy_assisted"}) == "vacancy_assisted"

    # When TG or WA contact IS resolved, stay in direct `vacancy`
    assert (
        detect_lead_segment(
            {
                "source": "hh_ru",
                "username": "hr_maria",
                "manual_action_url": "https://hh.ru/vacancy/123",
            }
        )
        == "vacancy"
    )
    assert (
        detect_lead_segment(
            {
                "source": "hh_ru",
                "whatsapp": "https://wa.me/79991112233",
                "manual_action_url": "https://hh.ru/vacancy/123",
            }
        )
        == "vacancy"
    )
    assert (
        detect_lead_segment(
            {
                "source": "hh_ru",
                "provider": "whatsapp",
                "phone": "+79991112233",
                "manual_action_url": "https://hh.ru/vacancy/123",
            }
        )
        == "vacancy"
    )


def test_resolve_target_campaign_matrix() -> None:
    assert resolve_target_campaign("ru_expert", "ru", "telegram") == CAMPAIGN_RU_EXPERTS_TG
    assert resolve_target_campaign("ru_expert", "en", "telegram") == CAMPAIGN_RU_EXPERTS_TG
    assert resolve_target_campaign("ofm", "en", "telegram") == CAMPAIGN_OFM_TG
    assert (
        resolve_target_campaign(
            "ofm",
            "en",
            "telegram",
            cfg={"b2b": {"target_campaign_ofm": "Custom OFM Campaign"}},
        )
        == "Custom OFM Campaign"
    )
    assert resolve_target_campaign("whale", "en", "whatsapp") == CAMPAIGN_WHALES_EN_WA
    assert resolve_target_campaign("whale", "en", "wa") == CAMPAIGN_WHALES_EN_WA
    assert resolve_target_campaign("whale", "en", "telegram") == CAMPAIGN_WHALES_EN_TG
    assert resolve_target_campaign("whale", "en", "tg") == CAMPAIGN_WHALES_EN_TG

    for seg in ("vacancy", "client_reels"):
        assert resolve_target_campaign(seg, "ru", "whatsapp") == CAMPAIGN_VACANCY_RU_WA
        assert resolve_target_campaign(seg, "ru", "telegram") == CAMPAIGN_VACANCY_RU_TG
        assert resolve_target_campaign(seg, "en", "telegram") == CAMPAIGN_VACANCY_EN_TG
        assert resolve_target_campaign(seg, "en", "whatsapp") == CAMPAIGN_WHALES_EN_WA

    assert resolve_target_campaign("social_manual", "en", "telegram") == QUEUE_MANUAL_SOCIAL
    assert resolve_target_campaign("vacancy_assisted", "ru", "telegram") == QUEUE_ASSISTED_JOB

    # Fallback b2b
    assert resolve_target_campaign("b2b", "en", "telegram") == CAMPAIGN_WHALES_EN_TG
    assert resolve_target_campaign("b2b", "en", "whatsapp") == CAMPAIGN_WHALES_EN_WA
    assert resolve_target_campaign("b2b", "ru", "telegram") == CAMPAIGN_VACANCY_RU_TG
    assert resolve_target_campaign("b2b", "ru", "whatsapp") == CAMPAIGN_VACANCY_RU_WA
    assert (
        resolve_target_campaign(
            "b2b",
            "en",
            "telegram",
            cfg={"b2b": {"target_campaign_tg": "SoftUGC — База Все2 (2 045 лидов)"}},
        )
        == CAMPAIGN_WHALES_EN_TG
    )


def test_assert_copy_matches_language_ru_valid_and_invalid() -> None:
    valid_ru = (
        "Добрый день! Работаю в HeyGen, Kling, ElevenLabs, делаю Reels и Shorts "
        "для TikTok и Instagram. Сделал 183.5M просмотров за 90 дней. "
        "Кейсы SoftUGC: https://softugc.com/cases"
    )
    ok, reason = assert_copy_matches_language(valid_ru, "ru")
    assert (ok, reason) == (True, "")

    short_ru = "Привет, {name}! Посмотрите SoftUGC Reels на softugc.com — актуально?"
    assert assert_copy_matches_language(short_ru, "ru") == (True, "")

    pure_en = "We run SoftUGC. 183.5M views in 90 days. Want 3 demo reels for Acme?"
    ok, reason = assert_copy_matches_language(pure_en, "ru")
    assert ok is False
    assert reason

    mixed_en_with_ru_greeting = (
        "Привет, Alex! We run SoftUGC. It's organic distribution using short-form video "
        "and an owned network of creator accounts for your brand."
    )
    ok, reason = assert_copy_matches_language(mixed_en_with_ru_greeting, "ru")
    assert ok is False
    assert reason

    for empty_or_non_prose in ("", "   ", "https://softugc.com", "{step1_text}", "12345"):
        ok, reason = assert_copy_matches_language(empty_or_non_prose, "ru")
        assert ok is False
        assert reason


def test_assert_copy_matches_language_en_valid_and_invalid() -> None:
    valid_en = (
        "Hey Vlad! Saw Acme is scaling ads. We run SoftUGC: 183.5M views in 90 days, "
        "13-40x cheaper than clippers. Want 3 demo reels for {Brand}? https://softugc.com"
    )
    assert assert_copy_matches_language(valid_en, "en") == (True, "")

    en_with_cyrillic_recipient_name = "Hey Константин, quick intro from SoftUGC"
    assert assert_copy_matches_language(en_with_cyrillic_recipient_name, "en") == (True, "")

    en_with_long_cyrillic_name = (
        "Hey Константинопольский, quick intro from SoftUGC — we run organic short-form "
        "video distribution across our owned network."
    )
    assert assert_copy_matches_language(en_with_long_cyrillic_name, "en") == (True, "")

    ru_leak = "Hey Alex! We run SoftUGC — сделаем 3 демо ролика под ваш бренд."
    ok, reason = assert_copy_matches_language(ru_leak, "en")
    assert ok is False
    assert reason

    for mixed_ru in (
        "Привет! Check out SoftUGC reels",
        "Hey Привет, quick intro from SoftUGC",
        "Смотрите, SoftUGC works great",
        "Hey alex, посмотрите demo?",
    ):
        ok_m, reason_m = assert_copy_matches_language(mixed_ru, "en")
        assert ok_m is False, f"Expected EN rejection for: {mixed_ru!r}"
        assert reason_m

    # Strict template mode rejects ANY Cyrillic char even when capitalized after Hey
    ok_tpl_cyr, reason_tpl = assert_copy_matches_language(
        "Hey Константин, quick intro from SoftUGC", "en", allow_cyrillic_name=False
    )
    assert ok_tpl_cyr is False
    assert reason_tpl
    assert assert_copy_matches_language(
        "Hey {name}, quick intro from SoftUGC", "en", allow_cyrillic_name=False
    ) == (True, "")

    pure_ru = "Привет, мы делаем ролики"
    ok, reason = assert_copy_matches_language(pure_ru, "en")
    assert ok is False
    assert reason

    ru_sentence = "Добрый день! Можем сделать 90 AI-роликов в месяц под ключ."
    ok, reason = assert_copy_matches_language(ru_sentence, "en")
    assert ok is False
    assert reason

    for empty_or_non_prose in ("", "   ", "https://softugc.com", "{step1_text}", "12345"):
        ok, reason = assert_copy_matches_language(empty_or_non_prose, "en")
        assert ok is False
        assert reason

    ok, reason = assert_copy_matches_language("Bonjour!", "fr")  # type: ignore[arg-type]
    assert ok is False
    assert "Unsupported" in reason


def test_route_lead_end_to_end() -> None:
    ru_hh_assisted = route_lead(
        {
            "source": "hh_ru",
            "title": "Рилсмейкер на удалёнку",
            "manual_action_url": "https://hh.ru/vacancy/987654",
        }
    )
    assert ru_hh_assisted == LeadRoute(
        language="ru",
        segment="vacancy_assisted",
        provider="telegram",
        target_campaign=QUEUE_ASSISTED_JOB,
    )

    ru_hh_direct = route_lead(
        {
            "source": "hh_ru",
            "title": "Рилсмейкер",
            "username": "hr_director",
            "manual_action_url": "https://hh.ru/vacancy/987654",
        }
    )
    assert ru_hh_direct == LeadRoute(
        language="ru",
        segment="vacancy",
        provider="telegram",
        target_campaign=CAMPAIGN_VACANCY_RU_TG,
    )

    whale_wa = route_lead(
        {
            "source": "meta_ads",
            "company": "GlowSkin US",
            "whatsapp": "https://wa.me/14155550101",
            "provider": "whatsapp",
        }
    )
    assert whale_wa == LeadRoute(
        language="en",
        segment="whale",
        provider="whatsapp",
        target_campaign=CAMPAIGN_WHALES_EN_WA,
    )
