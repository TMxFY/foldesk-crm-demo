from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from leadgen import ru_experts
from leadgen.ru_experts import (
    RuExpertsFilter,
    build_ru_expert_row,
    discover_ru_experts,
    discover_ru_experts_from_channels,
    discover_ru_experts_from_chats,
    discover_ru_experts_from_instagram,
    extract_personal_tg_username,
    filter_from_config,
    is_agency_or_aggregator,
    is_bot_handle,
    main as ru_experts_main,
    matches_ru_expert_filter,
    normalize_ig_handle,
    normalize_tg_handle,
    parse_subscriber_count,
)


def _assert_sacred_row_invariants(row: dict[str, Any]) -> None:
    assert row["segment"] == "ru_expert"
    assert row["language"] == "ru"
    assert row["exclude_from_airtable"] == 1
    assert row["source"] == "ru_experts"
    assert row["lead_source"] == "ru_experts"
    assert row["provider"] == "telegram"
    assert row["username"] and not str(row["username"]).startswith("@")


def test_filter_from_config_and_zero_hardcoded_niche_lock_in() -> None:
    # Default config allows any non-agency/non-stop-word expert when Q23 niches are empty
    empty_filter = filter_from_config({"ru_experts": {"niches": [], "min_subs": 1000, "max_subs": 50000}})
    assert empty_filter == RuExpertsFilter(niches=(), min_subs=1000, max_subs=50000, stop_words=())
    ok_any, matched_empty, reason_empty = matches_ru_expert_filter(
        "Авторский курс по переговорам и продажам B2B",
        empty_filter,
        subs=4500,
    )
    assert ok_any is True
    assert matched_empty == []
    assert reason_empty == ""

    # Plugging Q23 niches dynamically (including string config & Russian morphology)
    q23_filter = filter_from_config(
        {
            "ru_experts": {
                "niches": "психология, нутрициология, инвестиции",
                "min_subs": 1500,
                "max_subs": 30000,
                "stop_words": ["казино", "ставки", "ad"],
            }
        }
    )
    ok_psy, matched_psy, _ = matches_ru_expert_filter(
        "Клинический психолог Елена | Помогаю справиться с выгоранием",
        q23_filter,
        subs=12000,
    )
    assert ok_psy is True
    assert matched_psy == ["психология"]

    # Word 'academy' must NOT be blocked by short ASCII stop-word 'ad'
    ok_acad, _, _ = matches_ru_expert_filter(
        "Академия нутрициологии: научный подход к питанию (Academy of Nutrition)",
        q23_filter,
        subs=8000,
    )
    assert ok_acad is True

    # Non-matching niche rejected
    ok_other, _, reason_other = matches_ru_expert_filter(
        "Авторский блог о коммерческой недвижимости в Москве",
        q23_filter,
        subs=8000,
    )
    assert ok_other is False
    assert reason_other == "niche_mismatch"

    # Out-of-range subscribers rejected
    ok_small, _, reason_small = matches_ru_expert_filter("Психолог", q23_filter, subs=900)
    ok_big, _, reason_big = matches_ru_expert_filter("Психолог", q23_filter, subs=45000)
    assert ok_small is False and reason_small == "subs_out_of_range"
    assert ok_big is False and reason_big == "subs_out_of_range"

    # Stop word rejected
    ok_stop, _, reason_stop = matches_ru_expert_filter(
        "Психология ставок на спорт и казино",
        q23_filter,
        subs=5000,
    )
    assert ok_stop is False and reason_stop.startswith("stop_word:")


def test_parse_subscriber_count_and_handle_normalization() -> None:
    assert parse_subscriber_count(12500) == 12500
    assert parse_subscriber_count("12.4K") == 12400
    assert parse_subscriber_count("12,5 тыс.") == 12500
    assert parse_subscriber_count("1.2M") == 1200000
    assert parse_subscriber_count("15 200 подписчиков") == 15200
    assert parse_subscriber_count("1,200") == 1200
    assert parse_subscriber_count(None) is None

    assert normalize_tg_handle("https://t.me/elena_psy?start=1") == "elena_psy"
    assert normalize_tg_handle("https://t.me/s/elena_channel") == "elena_channel"
    assert normalize_tg_handle("tg://resolve?domain=ivan_coach") == "ivan_coach"
    assert normalize_tg_handle("https://t.me/+SecretInviteHash") == ""
    assert normalize_tg_handle("https://t.me/joinchat/AbcDef") == ""

    assert normalize_ig_handle("https://www.instagram.com/elena.psy/?igsh=xyz") == "elena.psy"
    assert normalize_ig_handle("https://instagram.com/reel/C9xyz") == ""
    assert is_bot_handle("my_helper_bot") is True
    assert is_bot_handle("telega_in") is True
    assert is_bot_handle("elena_psy") is False


def test_extract_personal_username_rejects_bots_ads_agencies_and_emails() -> None:
    # Picks personal DM even when ad exchange & bot share the same bio/line
    bio_mixed = (
        "Нутрициолог Елена. Пишите на info@gmail.com\n"
        "Реклама: @ad_birzha_bot | Консультации и личка: @elena_nutri\n"
        "Посты в канале t.me/elena_channel/42 и сам @elena_channel"
    )
    assert extract_personal_tg_username(bio_mixed, channel_username="elena_channel") == "elena_nutri"

    # Rejects bot-only, ad-manager-only, exchange-only
    assert extract_personal_tg_username("Связь: @support_help_bot") == ""
    assert extract_personal_tg_username("По вопросам рекламы: @anna_pr_manager\nБиржа: @telega_in") == ""
    assert extract_personal_tg_username("Ассистент по рекламе: @kate_assistant") == ""

    # Pinned post fallback when bio has no personal handle
    bio_clean = "Канал о системном маркетинге. По вопросам рекламы: @ads_manager_tg"
    pinned = ["Закреп! Я Артём, автор канала. Запись на разбор и личка: @artem_expert"]
    assert extract_personal_tg_username(bio_clean, pinned_posts=pinned, channel_username="artem_blog") == "artem_expert"

    # Regular post without personal cue does not leak third-party shoutouts
    assert (
        extract_personal_tg_username(
            "Только блог",
            posts=["Подписывайтесь на крутой канал моего друга @random_friend"],
            channel_username="my_blog",
        )
        == ""
    )

    # Agency aggregators rejected, negated agency mentions preserved
    assert is_agency_or_aggregator("Мы — продюсерский центр и маркетинговое агентство для экспертов") is True
    assert is_agency_or_aggregator("Крупнейший каталог экспертов и биржа рекламы в Telegram") is True
    assert is_agency_or_aggregator("Помогаю экспертам делать запуски без агентства и продюсерского центра") is False


def test_channel_1_tg_channels_treg_monid_and_filtering() -> None:
    calls: list[tuple[str, str]] = []

    def fake_treg(endpoint: str, *, query: dict[str, str] | None = None, **_: Any) -> Any:
        ch = (query or {}).get("channel", "")
        calls.append(("treg:" + endpoint, ch))
        if endpoint == "tikhub.x.telegram-web-fetch-similar-channels":
            return {
                "code": 200,
                "data": {
                    "channel": ch,
                    "similar": [
                        {"username": "olga_psy_channel"},
                        {"username": "artem_pinned_only"},
                        {"username": "bot_only_chan"},
                        {"username": "agency_agg_chan"},
                        {"username": "too_big_chan"},
                        {"username": "stopword_chan"},
                    ],
                },
            }
        if endpoint == "tikhub.x.telegram-web-fetch-channel-info":
            infos = {
                "seed_mega_ru": {
                    "data": {
                        "username": "seed_mega_ru",
                        "title": "Mega Expert Media",
                        "description": "Автор @mega_admin",
                        "subscribers": "210K",
                    }
                },
                "olga_psy_channel": {
                    "data": {
                        "username": "olga_psy_channel",
                        "title": "Психолог Ольга Смирнова",
                        "description": "Помощь предпринимателям с выгоранием.\nЗапись в личку: @olga_smirnova_psy\nРеклама: @reklama_birzha",
                        "subscribers": "8.4K",
                    }
                },
                "artem_pinned_only": {
                    "data": {
                        "username": "artem_pinned_only",
                        "title": "Нутрициология с Артёмом",
                        "description": "Наука о питании и энергии. По вопросам рекламы: @pr_manager_x",
                        "subscribers": "14 200",
                    }
                },
                "bot_only_chan": {
                    "data": {
                        "username": "bot_only_chan",
                        "title": "Психология дня",
                        "description": "Вопросы только через бота @psy_support_bot",
                        "subscribers": "5000",
                    }
                },
                "agency_agg_chan": {
                    "data": {
                        "username": "agency_agg_chan",
                        "title": "Продюсерский центр ProLaunch",
                        "description": "Агентство полного цикла для экспертов. Личка @agency_owner",
                        "subscribers": "4500",
                    }
                },
                "too_big_chan": {
                    "data": {
                        "username": "too_big_chan",
                        "title": "Большой канал психологии",
                        "description": "Личка @big_psy",
                        "subscribers": "120K",
                    }
                },
                "stopword_chan": {
                    "data": {
                        "username": "stopword_chan",
                        "title": "Психология и казино",
                        "description": "Личка @casino_psy",
                        "subscribers": "3500",
                    }
                },
            }
            return infos.get(ch)
        return None

    def fake_monid(provider: str, endpoint: str, *, query: dict[str, Any] | None = None, **_: Any) -> Any:
        ch = (query or {}).get("channel", "")
        calls.append((f"monid:{provider}:{endpoint}", ch))
        if endpoint == "/telegram/posts" and ch == "artem_pinned_only":
            return {
                "posts": [
                    {
                        "id": 1,
                        "pinned": True,
                        "text": "Привет! Я Артём, нутрициолог. По консультациям пишите мне лично @artem_nutri_dm",
                    },
                    {"id": 2, "text": "Обычный пост про витамин D и сон https://artem-nutri.ru"},
                ]
            }
        if endpoint == "/telegram/posts" and ch == "olga_psy_channel":
            return {"posts": [{"id": 10, "text": "Разбор кейсов по психологии https://olga-psy.ru"}]}
        return {"posts": []}

    flt = RuExpertsFilter(
        niches=("психология", "нутрициология"),
        min_subs=1000,
        max_subs=50000,
        stop_words=("казино",),
    )
    rows = discover_ru_experts_from_channels(
        ["@seed_mega_ru"],
        filter_cfg=flt,
        treg_fn=fake_treg,
        monid_fn=fake_monid,
    )

    assert [r["username"] for r in rows] == ["olga_smirnova_psy", "artem_nutri_dm"]
    for row in rows:
        _assert_sacred_row_invariants(row)
        assert row["discovery_channel"] == "tg_channels"
    assert rows[0]["subscribers"] == 8400
    assert rows[0]["website"] == "https://olga-psy.ru"
    assert rows[1]["subscribers"] == 14200
    assert rows[1]["matched_niches"] == ["нутрициология"]
    assert ("treg:tikhub.x.telegram-web-fetch-similar-channels", "seed_mega_ru") in calls
    assert ("monid:litescrape:/telegram/posts", "artem_pinned_only") in calls


def test_channel_1_respects_budget_guard_and_zero_paid_calls_when_blocked() -> None:
    invoked: list[str] = []

    def deny_budget(provider: str, cost: float) -> tuple[bool, str]:
        invoked.append(f"guard:{provider}:{cost}")
        return False, "daily_cap_reached"

    rows = discover_ru_experts_from_channels(
        ["seed_expert"],
        budget_guard_fn=deny_budget,
        treg_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("treg must not run when budget blocked")),
        monid_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("monid must not run when budget blocked")),
    )
    assert rows == []
    assert invoked == ["guard:treg:0.001"]


def test_channel_2_tg_chats_parses_telethon_client_and_filters_agencies_bots_spam() -> None:
    class FakeTelethonClient:
        def __init__(self) -> None:
            self.resolved_entities: list[str] = []

        async def get_entity(self, chat_ref: str) -> Any:
            self.resolved_entities.append(chat_ref)
            return SimpleNamespace(id=5001, username=chat_ref, title="Клуб экспертов РФ")

        async def get_messages(self, entity: Any, limit: int = 200) -> list[Any]:
            return [
                # Valid RU expert (bio missing on sender -> resolved via __call__ GetFullUserRequest)
                SimpleNamespace(
                    id=101,
                    sender_id=701,
                    post=False,
                    sender=SimpleNamespace(id=701, username="irina_coach_ru", first_name="Ирина", last_name="Волкова", bot=False, deleted=False),
                    message="Веду онлайн-наставничество по карьере и психологии, ищу продюсера для рилс.",
                ),
                # Second message from same author should aggregate, not duplicate
                SimpleNamespace(
                    id=102,
                    sender_id=701,
                    post=False,
                    sender=SimpleNamespace(id=701, username="irina_coach_ru", first_name="Ирина", last_name="Волкова", bot=False, deleted=False),
                    message="Есть бюджет 150-200к фикс + процент от запусков.",
                ),
                # Agency pitch -> must be rejected
                SimpleNamespace(
                    id=103,
                    sender_id=702,
                    post=False,
                    sender=SimpleNamespace(
                        id=702,
                        username="boost_agency_owner",
                        first_name="Олег",
                        bio="SMM-агентство полного цикла, настроим рекламу клиентам",
                        bot=False,
                        deleted=False,
                    ),
                    message="Наше агентство делает запуски под ключ.",
                ),
                # Bot -> rejected
                SimpleNamespace(
                    id=104,
                    sender_id=703,
                    post=False,
                    sender=SimpleNamespace(id=703, username="helper_chat_bot", first_name="Bot", bot=True, deleted=False),
                    message="Приветствуем в чате психологов!",
                ),
                # Job-seeker resume -> rejected
                SimpleNamespace(
                    id=105,
                    sender_id=704,
                    post=False,
                    sender=SimpleNamespace(id=704, username="vasya_freelance", first_name="Вася", bio="Психология", bot=False, deleted=False),
                    message="#резюме ищу работу монтажником рилс за отзыв",
                ),
                # Channel anonymous post -> ignored
                SimpleNamespace(
                    id=106,
                    sender_id=705,
                    post=True,
                    sender=SimpleNamespace(id=705, username="anon_channel", bot=False),
                    message="Анонс мероприятия по психологии",
                ),
            ]

        async def __call__(self, request: Any) -> Any:
            return SimpleNamespace(
                users=[SimpleNamespace(id=701, username="irina_coach_ru", first_name="Ирина", last_name="Волкова", bot=False, deleted=False)],
                full_user=SimpleNamespace(about="Карьерный психолог. Канал t.me/irina_career_blog Сайт https://irina-coach.ru"),
            )

    client = FakeTelethonClient()
    flt = RuExpertsFilter(niches=("психология", "наставничество"), min_subs=1000, max_subs=50000)
    rows = asyncio.run(discover_ru_experts_from_chats(["@experts_club_ru"], client=client, filter_cfg=flt))

    assert len(rows) == 1
    row = rows[0]
    _assert_sacred_row_invariants(row)
    assert row["username"] == "irina_coach_ru"
    assert row["telegram_user_id"] == 701
    assert row["discovery_channel"] == "tg_chats"
    assert row["channel_username"] == "irina_career_blog"
    assert row["website"] == "https://irina-coach.ru"

    # Also supports pre-injected messages_by_chat + llm_filter_fn override
    async def mock_llm(cand: dict[str, Any]) -> bool:
        return cand["username"] == "sergey_fin"

    rows_msgs = asyncio.run(
        discover_ru_experts_from_chats(
            ["fin_chat"],
            messages_by_chat={
                "fin_chat": [
                    {"sender_id": 801, "username": "sergey_fin", "name": "Сергей", "bio": "Финансовый консультант", "text": "Помогаю с личным бюджетом"},
                    {"sender_id": 802, "username": "chatter_only", "name": "Иван", "bio": "Эксперт", "text": "Привет"},
                ]
            },
            filter_cfg=RuExpertsFilter(),
            llm_filter_fn=mock_llm,
        )
    )
    assert [r["username"] for r in rows_msgs] == ["sergey_fin"]
    _assert_sacred_row_invariants(rows_msgs[0])


def test_channel_3_instagram_to_telegram_via_taplink_and_channel_bio() -> None:
    monid_calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_monid(provider: str, endpoint: str, *, query=None, body=None) -> Any:
        monid_calls.append((provider, endpoint, body or query))
        if provider == "anyapi" and endpoint == "/instagram.profile_contact":
            handle = (body or {}).get("handle")
            route = (body or {}).get("route")
            if handle == "maria.endo":
                # Simulate octopus lacking bioLinks -> fallback to chameleon!
                if route == "octopus":
                    return {"handle": "maria.endo", "displayName": "Мария | Эндокринолог"}
                return {
                    "handle": "maria.endo",
                    "displayName": "Мария | Эндокринолог",
                    "bio": "Врач-эндокринолог. Запись на курс через ссылку ниже 👇",
                    "bioLinks": [
                        {"url": "https://l.instagram.com/?u=https%3A%2F%2Ftaplink.cc%2Fmaria_endo", "title": "Все ссылки"}
                    ],
                }
            if handle == "bot_only_ig":
                return {
                    "handle": "bot_only_ig",
                    "displayName": "Нутрициолог Бот",
                    "bio": "Мой ТГ канал t.me/bot_only_tg_ch",
                }
            if handle == "captcha_ig":
                return {
                    "handle": "captcha_ig",
                    "displayName": "Капча Эксперт",
                    "externalUrl": "https://taplink.cc/captcha_page",
                }
        if provider == "litescrape" and endpoint == "/telegram/posts":
            return {"posts": []}
        return None

    def fake_http_get(url: str) -> str:
        if url == "https://taplink.cc/maria_endo":
            # JSON-escaped link inside Taplink hydration script + regular HTML
            return (
                '<html><body><div>Онлайн-программа по гормонам и питанию</div>'
                '<script>window.__DATA__={"url":"https:\\/\\/t.me\\/maria_endo_blog"};</script>'
                "</body></html>"
            )
        if url == "https://taplink.cc/captcha_page":
            return "<html><body>Please verify you are human (SmartCaptcha)</body></html>"
        return ""

    def fake_treg(endpoint: str, *, query: dict[str, str] | None = None, **_: Any) -> Any:
        ch = (query or {}).get("channel", "")
        if endpoint == "tikhub.x.telegram-web-fetch-channel-info":
            if ch == "maria_endo_blog":
                return {
                    "data": {
                        "username": "maria_endo_blog",
                        "title": "Эндокринолог Мария",
                        "description": "По вопросам консультаций и личка: @maria_endo_personal",
                        "subscribers": "11.2K",
                    }
                }
            if ch == "bot_only_tg_ch":
                return {
                    "data": {
                        "username": "bot_only_tg_ch",
                        "title": "Нутрициология",
                        "description": "Связь только с ботом @endo_helper_bot",
                        "subscribers": "6000",
                    }
                }
        return None

    flt = RuExpertsFilter(niches=("эндокринолог", "нутрициолог"), min_subs=1000, max_subs=50000)
    rows = discover_ru_experts_from_instagram(
        ["https://www.instagram.com/maria.endo/", "@bot_only_ig", "captcha_ig"],
        filter_cfg=flt,
        treg_fn=fake_treg,
        monid_fn=fake_monid,
        http_get_fn=fake_http_get,
    )

    assert len(rows) == 1
    row = rows[0]
    _assert_sacred_row_invariants(row)
    assert row["username"] == "maria_endo_personal"
    assert row["discovery_channel"] == "ig_to_tg"
    assert row["instagram_handle"] == "maria.endo"
    assert row["channel_username"] == "maria_endo_blog"
    assert row["subscribers"] == 11200
    assert ("anyapi", "/instagram.profile_contact", {"handle": "maria.endo", "route": "octopus"}) in monid_calls
    assert ("anyapi", "/instagram.profile_contact", {"handle": "maria.endo", "route": "chameleon"}) in monid_calls


def test_discover_ru_experts_orchestrator_deduplicates_and_cli_writes_jsonl(tmp_path: Path) -> None:
    def fake_treg(endpoint: str, *, query=None, **_: Any) -> Any:
        ch = (query or {}).get("channel", "")
        if endpoint == "tikhub.x.telegram-web-fetch-similar-channels":
            return {"data": {"similar": ["shared_expert_ch"]}}
        if endpoint == "tikhub.x.telegram-web-fetch-channel-info":
            return {
                "data": {
                    "username": ch,
                    "title": "Коуч Алексей",
                    "description": "Бизнес-коучинг. Личка: @alexey_coach_dm",
                    "subscribers": "5200",
                }
            }
        return None

    def fake_monid(provider: str, endpoint: str, *, query=None, body=None) -> Any:
        if provider == "anyapi" and endpoint == "/instagram.profile_contact":
            return {
                "handle": "alexey.coach",
                "displayName": "Алексей Коуч",
                "bio": "Мой ТГ канал https://t.me/shared_expert_ch",
            }
        return {"posts": []}

    cfg = {
        "ru_experts": {
            "enabled": True,
            "exclude_from_airtable": True,
            "niches": ["коуч"],
            "min_subs": 1000,
            "max_subs": 50000,
            "stop_words": [],
            "seed_channels": ["shared_expert_ch"],
            "seed_chats": ["coach_chat_ru"],
            "ig_handles": ["alexey.coach"],
            "daily_limit": 20,
        }
    }

    res = asyncio.run(
        discover_ru_experts(
            cfg=cfg,
            messages_by_chat={
                "coach_chat_ru": [
                    {
                        "sender_id": 909,
                        "username": "anna_coach_chat",
                        "name": "Анна",
                        "bio": "Бизнес-коуч для руководителей",
                        "text": "Ищу команду для reels-воронки",
                    }
                ]
            },
            treg_fn=fake_treg,
            monid_fn=fake_monid,
        )
    )

    assert res["status"] == "ok"
    assert res["stats"]["channels_found"] == 1
    assert res["stats"]["chats_found"] == 1
    assert res["stats"]["ig_to_tg_found"] == 1
    # @alexey_coach_dm found via both TG channel & IG->TG deduplicates to 1 row, plus @anna_coach_chat = 2 unique
    assert res["stats"]["total_unique"] == 2
    usernames = {r["username"] for r in res["rows"]}
    assert usernames == {"alexey_coach_dm", "anna_coach_chat"}
    for row in res["rows"]:
        _assert_sacred_row_invariants(row)

    alexey_row = next(r for r in res["rows"] if r["username"] == "alexey_coach_dm")
    assert alexey_row["instagram_handle"] == "alexey.coach"

    # CLI smoke test writing JSONL cleanly from config file
    cfg_path = tmp_path / "leadgen_config.json"
    out_path = tmp_path / "out_experts.jsonl"
    cfg_path.write_text(json.dumps({"ru_experts": {"niches": [], "seed_channels": [], "seed_chats": []}}), encoding="utf-8")
    exit_code = ru_experts_main(["--config", str(cfg_path), "--output", str(out_path)])
    assert exit_code == 0
    assert out_path.is_file()
    assert out_path.read_text(encoding="utf-8") == ""

    # build_ru_expert_row direct contract verification
    built = build_ru_expert_row(
        username="@test_expert",
        name="Тест Эксперт",
        bio="Психолог",
        discovery_channel="tg_channels",
    )
    _assert_sacred_row_invariants(built)
    assert ru_experts.__name__ == "leadgen.ru_experts"


def test_edge_cases_per_provider_budget_iter_messages_and_ig_channel_posts_fallback() -> None:
    # 1) Channel 1: monid blocked by budget guard while treg allowed -> bio contact still emitted
    rows_treg_only = discover_ru_experts_from_channels(
        ["bio_seed"],
        filter_cfg=RuExpertsFilter(niches=("фитнес",), min_subs=1000, max_subs=50000),
        budget_guard_fn=lambda provider, cost: provider == "treg",
        treg_fn=lambda ep, query=None: (
            {"data": {"similar": []}}
            if "similar-channels" in ep
            else {
                "data": {
                    "username": "bio_seed",
                    "title": "Фитнес с Максом",
                    "description": "Фитнес-наставник. Личка: @max_fitness_dm",
                    "subscribers": 3200,
                }
            }
        ),
        monid_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("monid should be blocked by budget_guard_fn")),
    )
    assert [r["username"] for r in rows_treg_only] == ["max_fitness_dm"]
    _assert_sacred_row_invariants(rows_treg_only[0])

    # 2) Channel 2: iter_messages async generator + resilient get_entity failure on broken chat
    class IterOnlyClient:
        async def get_entity(self, chat_ref: str) -> Any:
            if chat_ref == "broken_chat":
                raise ValueError("Cannot find chat")
            return SimpleNamespace(id=777, username=chat_ref)

        async def iter_messages(self, entity: Any, limit: int = 200):
            yield SimpleNamespace(
                id=1,
                sender_id=950,
                post=False,
                sender=SimpleNamespace(
                    id=950,
                    username="dmitry_mentor",
                    first_name="Дмитрий",
                    last_name="",
                    bio="Бизнес-наставник, автор курса по продажам",
                    bot=False,
                    deleted=False,
                ),
                message="Запускаю поток менторства, ищу команду для коротких роликов",
            )

    chat_rows = asyncio.run(
        discover_ru_experts_from_chats(
            ["broken_chat", "ok_chat"],
            client=IterOnlyClient(),
            filter_cfg=RuExpertsFilter(niches=("продаж", "наставник")),
            resolve_full_user=False,
        )
    )
    assert [r["username"] for r in chat_rows] == ["dmitry_mentor"]
    _assert_sacred_row_invariants(chat_rows[0])

    # 3) Channel 3: IG -> TG channel where bio lacks @username, resolved via monid /telegram/posts;
    #    plus negative case where TG channel has NO personal username -> channel handle is NOT emitted!
    ig_rows = discover_ru_experts_from_instagram(
        ["@igor.fin", "@channel_without_dm"],
        filter_cfg=RuExpertsFilter(niches=("финанс", "инвестиц"), min_subs=1000, max_subs=50000),
        monid_fn=lambda prov, ep, query=None, body=None: (
            {
                "handle": (body or {}).get("handle"),
                "displayName": "Игорь Финансы",
                "bio": "Личные финансы и инвестиции. Канал t.me/igor_fin_ch",
            }
            if prov == "anyapi" and (body or {}).get("handle") == "igor.fin"
            else (
                {
                    "handle": "channel_without_dm",
                    "displayName": "Канал без лички",
                    "bio": "Инвестиции t.me/silent_fin_ch",
                }
                if prov == "anyapi"
                else (
                    {
                        "posts": [
                            {
                                "id": 1,
                                "pinned": True,
                                "text": "Закреп: по личным консультациям пишите @igor_fin_personal",
                            }
                        ]
                    }
                    if ep == "/telegram/posts" and (query or {}).get("channel") == "igor_fin_ch"
                    else {"posts": []}
                )
            )
        ),
        treg_fn=lambda ep, query=None: {
            "data": {
                "username": (query or {}).get("channel"),
                "title": "Канал про инвестиции и финансы",
                "description": "Полезные разборы каждый день",
                "subscribers": 4500,
            }
        },
    )
    assert [r["username"] for r in ig_rows] == ["igor_fin_personal"]
    assert ig_rows[0]["channel_username"] == "igor_fin_ch"
    _assert_sacred_row_invariants(ig_rows[0])


def test_budget_guard_sqlite_ledger_and_airtable_isolation(tmp_path: Path) -> None:
    import sqlite3
    from app.airtable_sync import is_lead_airtable_excluded
    from leadgen.budget_guard import ensure_enrichment_ledger_table, get_daily_enrichment_spend

    conn = sqlite3.connect(str(tmp_path / "test_ru_experts_budget.db"), check_same_thread=False)
    try:
        ensure_enrichment_ledger_table(conn)
        rows = discover_ru_experts_from_channels(
            ["coach_seed"],
            filter_cfg=RuExpertsFilter(niches=("коуч",), min_subs=1000, max_subs=50000),
            treg_fn=lambda ep, query=None: (
                {"data": {"similar": []}}
                if "similar-channels" in ep
                else {
                    "data": {
                        "username": "coach_seed",
                        "title": "Коуч Олег",
                        "description": "Бизнес-коучинг. Личка: @oleg_coach_dm",
                        "subscribers": 4000,
                    }
                }
            ),
            monid_fn=lambda prov, ep, query=None: {"posts": [{"id": 1, "text": "Пост про коучинг"}]},
            conn=conn,
            cfg={"enrichment": {"daily_budget_cap_usd": 5.0}},
        )
        assert len(rows) == 1
        _assert_sacred_row_invariants(rows[0])
        assert is_lead_airtable_excluded(rows[0]) is True

        # 0.001 (similar) + 0.001 (channel-info) + 0.00015 (monid posts) = 0.00215
        spent = get_daily_enrichment_spend(conn)
        assert spent > 0.002

        # With daily cap already smaller than next treg call, paid discovery stops cleanly
        blocked_rows = discover_ru_experts_from_channels(
            ["coach_seed"],
            treg_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call treg above cap")),
            monid_fn=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not call monid above cap")),
            conn=conn,
            cfg={"enrichment": {"daily_budget_cap_usd": 0.001}},
        )
        assert blocked_rows == []
    finally:
        conn.close()


