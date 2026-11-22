from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_missing_hh_token_returns_auth_required_without_http_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    monkeypatch.delenv("HH_API_TOKEN", raising=False)
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(200, {"items": []})

    result = collect_hh_vacancies(http_get_fn=fake_get)
    assert result["status"] == "auth_required"
    assert result["rows"] == []
    assert "HH_API_TOKEN" in result["error"]
    assert calls == []


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_401_403_returns_auth_required(status_code: int) -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(status_code, {"errors": [{"type": "oauth", "value": "forbidden"}]})

    result = collect_hh_vacancies(token="test-hh-token", http_get_fn=fake_get)
    assert result["status"] == "auth_required"
    assert result["rows"] == []
    assert str(status_code) in result["error"]


def test_tuple_403_response_supported_and_returns_auth_required() -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    def fake_get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        return 403, {"description": "Forbidden"}

    result = collect_hh_vacancies(token="test-hh-token", http_get_fn=fake_get)
    assert result["status"] == "auth_required"
    assert result["rows"] == []


def test_queries_all_keywords_with_mandatory_headers_and_remote_period_7() -> None:
    from leadgen.hh_vacancies import DEFAULT_KEYWORDS, HH_USER_AGENT, collect_hh_vacancies

    seen_requests: list[dict[str, Any]] = []

    def fake_get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        seen_requests.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return _FakeResponse(200, {"items": [], "pages": 1, "found": 0})

    result = collect_hh_vacancies(token="my-secret-token", http_get_fn=fake_get)
    assert result["status"] == "ok"
    assert result["rows"] == []
    assert len(seen_requests) == len(DEFAULT_KEYWORDS)

    queried_texts = [req["params"].get("text") for req in seen_requests]
    assert queried_texts == list(DEFAULT_KEYWORDS)
    for expected_kw in (
        "рилсмейкер",
        "UGC креатор",
        "AI видео",
        "контент креатор",
        "монтаж reels shorts",
        "видеомейкер",
        "контент продюсер",
    ):
        assert expected_kw in queried_texts

    for req in seen_requests:
        assert req["url"] == "https://api.hh.ru/vacancies"
        assert req["params"]["schedule"] == "remote"
        assert req["params"]["period"] == 7
        assert req["headers"]["Authorization"] == "Bearer my-secret-token"
        assert req["headers"]["HH-User-Agent"] == HH_USER_AGENT == "SoftUGC-Leadgen/1.0 (ops@softugc.com)"


def test_contact_extraction_routing_and_dedup() -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    vacancies_by_kw: dict[str, list[dict[str, Any]]] = {
        "рилсмейкер": [
            {
                "id": "101",
                "name": "Рилсмейкер / Reels-креатор",
                "alternate_url": "https://hh.ru/vacancy/101",
                "published_at": "2026-11-22T10:00:00+0300",
                "schedule": {"id": "remote", "name": "Удалённая работа"},
                "salary": {"from": 80000, "to": 120000, "currency": "RUR", "gross": False},
                "employer": {
                    "id": "e1",
                    "name": "GlowSkin Brand",
                    "site_url": "https://glowskin.ru",
                    "alternate_url": "https://hh.ru/employer/e1",
                },
                "contacts": {"name": "Анна", "email": "hr@glowskin.ru", "phones": []},
                "description": (
                    "<p>Ищем рилсмейкера на поток Reels и Shorts.</p>"
                    "<p>Отклики шлите в Telegram @anna_glow_hr или почту hr@glowskin.ru.</p>"
                    "<p>Бот @helper_bot не писать.</p>"
                ),
            },
            {
                "id": "102",
                "name": "UGC креатор для бренда",
                "alternate_url": "https://hh.ru/vacancy/102",
                "published_at": "2026-11-22T09:00:00+0300",
                "schedule": {"id": "remote", "name": "Удалённая работа"},
                "employer": {
                    "id": "e2",
                    "name": "FitBox",
                    "site_url": "https://fitbox.pro",
                    "alternate_url": "https://hh.ru/employer/e2",
                },
                "contacts": {
                    "name": "Дмитрий",
                    "phones": [{"country": "7", "city": "916", "number": "5554219", "comment": "WhatsApp"}],
                },
                "description": "<p>Съемка UGC видео и вертикальных роликов.</p>",
            },
        ],
        "UGC креатор": [
            # Duplicate of 101 across second keyword — must be deduplicated
            {
                "id": "101",
                "name": "Рилсмейкер / Reels-креатор",
                "alternate_url": "https://hh.ru/vacancy/101",
                "employer": {"id": "e1", "name": "GlowSkin Brand", "site_url": "https://glowskin.ru"},
                "description": "<p>Telegram @anna_glow_hr</p>",
            },
            # No direct TG or phone -> vacancy_assisted
            {
                "id": "103",
                "name": "Контент продюсер (Reels / TikTok)",
                "alternate_url": "https://hh.ru/vacancy/103",
                "published_at": "2026-11-21T15:00:00+0300",
                "schedule": {"id": "remote", "name": "Удалённая работа"},
                "employer": {
                    "id": "e3",
                    "name": "EdTech Академия",
                    "alternate_url": "https://hh.ru/employer/e3",
                },
                "contacts": None,
                "description": (
                    "<p>Нужен контент-продюсер для запуска вертикальных роликов и воронки в Reels/Shorts. "
                    "Отклик только через hh.ru, инвайт в чат t.me/+AbCdEf12345 после отклика.</p>"
                ),
            },
        ],
        "AI видео": [
            # HTML href link t.me/
            {
                "id": "104",
                "name": "AI видео креатор (Kling, Sora)",
                "alternate_url": "https://hh.ru/vacancy/104",
                "employer": {
                    "id": "e4",
                    "name": "NeuroMarket",
                    "site_url": "https://neuromarket.ai",
                    "alternate_url": "https://hh.ru/employer/e4",
                },
                "contacts": None,
                "description": '<p>Делаем AI-ролики для соцсетей. Пишите <a href="https://t.me/neuro_founder">менеджеру</a>.</p>',
            },
        ],
    }

    def fake_get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        if url == "https://api.hh.ru/vacancies":
            kw = str((params or {}).get("text") or "")
            return _FakeResponse(200, {"items": vacancies_by_kw.get(kw, []), "pages": 1})
        return _FakeResponse(404, {"description": "Not Found"})

    result = collect_hh_vacancies(token="tok", http_get_fn=fake_get)
    assert result["status"] == "ok"
    rows = result["rows"]
    assert [r["vacancy_id"] for r in rows] == ["101", "102", "103", "104"]

    # Row 101: direct @username (ignoring email & _bot)
    r101 = rows[0]
    assert r101["segment"] == "vacancy"
    assert r101["language"] == "ru"
    assert r101["source"] == "hh_ru"
    assert r101["username"] == "anna_glow_hr"
    assert r101["provider"] == "telegram"
    assert r101["project_url"] == "https://glowskin.ru"
    assert r101["company_domain"] == "glowskin.ru"
    assert r101["email"] == "hr@glowskin.ru"

    # Row 102: direct phone from contacts.phones
    r102 = rows[1]
    assert r102["segment"] == "vacancy"
    assert r102["language"] == "ru"
    assert r102["source"] == "hh_ru"
    assert r102["username"] == ""
    assert r102["phone"] == "+79165554219"
    assert r102["phones"] == ["+79165554219"]
    assert r102["provider"] == "whatsapp"
    assert r102["project_url"] == "https://fitbox.pro"

    # Row 103: no direct TG/phone -> vacancy_assisted
    r103 = rows[2]
    assert r103["segment"] == "vacancy_assisted"
    assert r103["language"] == "ru"
    assert r103["source"] == "hh_ru"
    assert r103["username"] == ""
    assert r103["phone"] == ""
    assert r103["manual_action_url"] == "https://hh.ru/vacancy/103"
    assert r103["project_url"] == "https://hh.ru/employer/e3"
    assert r103["company_domain"] == ""
    assert "EdTech Академия" in r103["manual_draft_text"]
    assert "Контент продюсер" in r103["manual_draft_text"]
    assert "https://tmxfy.github.io/" in r103["manual_draft_text"]
    assert "creatoros" not in r103["manual_draft_text"].lower()

    # Row 104: <a href="https://t.me/...">
    r104 = rows[3]
    assert r104["segment"] == "vacancy"
    assert r104["username"] == "neuro_founder"
    assert r104["project_url"] == "https://neuromarket.ai"


def test_hydrates_vacancy_detail_and_employer_site_url_with_caching() -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    employer_fetches = 0

    def fake_get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        nonlocal employer_fetches
        if url == "https://api.hh.ru/vacancies":
            kw = (params or {}).get("text")
            if kw == "видеомейкер":
                return _FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": "501",
                                "name": "Видеомейкер Reels / TikTok",
                                "alternate_url": "https://hh.ru/vacancy/501",
                                "snippet": {"requirement": "Монтаж коротких видео", "responsibility": "Reels"},
                                "employer": {
                                    "id": "emp-77",
                                    "name": "Alpha Goods",
                                    "url": "https://api.hh.ru/employers/emp-77",
                                    "alternate_url": "https://hh.ru/employer/emp-77",
                                },
                            },
                            {
                                "id": "502",
                                "name": "Монтажёр Shorts",
                                "alternate_url": "https://hh.ru/vacancy/502",
                                "snippet": {"requirement": "CapCut", "responsibility": "Shorts"},
                                "employer": {
                                    "id": "emp-77",
                                    "name": "Alpha Goods",
                                    "url": "https://api.hh.ru/employers/emp-77",
                                    "alternate_url": "https://hh.ru/employer/emp-77",
                                },
                            },
                        ]
                    },
                )
            return _FakeResponse(200, {"items": []})
        if url == "https://api.hh.ru/vacancies/501":
            return _FakeResponse(
                200,
                {
                    "id": "501",
                    "name": "Видеомейкер Reels / TikTok",
                    "alternate_url": "https://hh.ru/vacancy/501",
                    "description": "<p>Съемка и монтаж Reels. Пишите https://t.me/alphagoods_hr</p>",
                    "contacts": {"phones": []},
                    "employer": {"id": "emp-77", "name": "Alpha Goods", "alternate_url": "https://hh.ru/employer/emp-77"},
                },
            )
        if url == "https://api.hh.ru/vacancies/502":
            return _FakeResponse(
                200,
                {
                    "id": "502",
                    "name": "Монтажёр Shorts",
                    "alternate_url": "https://hh.ru/vacancy/502",
                    "description": "<p>Монтаж вертикальных роликов Shorts и Reels на потоке.</p>",
                    "contacts": None,
                    "employer": {"id": "emp-77", "name": "Alpha Goods", "alternate_url": "https://hh.ru/employer/emp-77"},
                },
            )
        if url == "https://api.hh.ru/employers/emp-77":
            employer_fetches += 1
            return _FakeResponse(
                200,
                {
                    "id": "emp-77",
                    "name": "Alpha Goods",
                    "site_url": "https://alphagoods.ru",
                    "alternate_url": "https://hh.ru/employer/emp-77",
                },
            )
        return _FakeResponse(404, {})

    res = collect_hh_vacancies(token="tok", keywords=("видеомейкер",), http_get_fn=fake_get)
    assert res["status"] == "ok"
    assert employer_fetches == 1
    assert len(res["rows"]) == 2
    assert res["rows"][0]["segment"] == "vacancy"
    assert res["rows"][0]["username"] == "alphagoods_hr"
    assert res["rows"][0]["project_url"] == "https://alphagoods.ru"
    assert res["rows"][1]["segment"] == "vacancy_assisted"
    assert res["rows"][1]["project_url"] == "https://alphagoods.ru"
    assert res["rows"][1]["manual_action_url"] == "https://hh.ru/vacancy/502"


@pytest.mark.parametrize(
    ("title", "description", "schedule_id", "should_reject"),
    [
        ("Python-разработчик (AI видео стартап)", "Разработка FastAPI, PostgreSQL, Docker", "remote", True),
        ("Frontend разработчик React", "Верстка, TypeScript, Redux", "remote", True),
        ("Программист 1С", "Доработка конфигураций 1С УТ", "remote", True),
        ("Разработчик Telegram-ботов на n8n", "Интеграции CRM, вебхуки, Make", "remote", True),
        ("QA инженер / тестировщик", "Автотесты на pytest", "remote", True),
        ("Сторисмейкер / оформление сторис", "Ежедневный постинг сторис в Instagram", "remote", True),
        ("Storymaker для блогера", "Делать сторис и прогревы", "remote", True),
        ("Иллюстратор детских книг", "Рисуем сказки в Midjourney", "remote", True),
        ("Нарезчик мемов для начинающих", "Склейка мемов в TikTok", "remote", True),
        ("Unity Developer (Roblox / Fortnite)", "Создание карт в Roblox Studio", "remote", True),
        ("Копирайтер Telegram-канала", "Писать посты и новости текстом, без видео", "remote", True),
        ("Рилсмейкер", "Съемка в офисе в Москве 5/2", "fullDay", True),
        ("Рилсмейкер в SaaS-компанию", "Наша команда разрабатывает SaaS-сервис. Ищем рилсмейкера для Reels и Shorts @saas_hr", "remote", False),
        ("AI видео креатор (Kling / HeyGen)", "Генерация вертикальных AI-видео и UGC для рекламы @ai_hr", "remote", False),
        ("Контент-продюсер Reels и TikTok", "Продюсирование роликов, сценарии, контент-воронка", "remote", False),
    ],
)
def test_junk_filter_rejects_dev_storymaker_gaming_and_keeps_video_roles(
    title: str, description: str, schedule_id: str, should_reject: bool
) -> None:
    from leadgen.hh_vacancies import is_junk_hh_vacancy

    vacancy = {
        "id": "999",
        "name": title,
        "description": description,
        "schedule": {"id": schedule_id},
        "employer": {"name": "Test Brand", "site_url": "https://testbrand.ru"},
        "alternate_url": "https://hh.ru/vacancy/999",
    }
    rejected, reason = is_junk_hh_vacancy(vacancy)
    assert rejected is should_reject, f"title={title!r} got rejected={rejected} reason={reason!r}"
    if should_reject:
        assert reason != ""


def test_seen_ids_and_resilience_on_partial_500_or_429() -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies

    def fake_partial_error(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        kw = (params or {}).get("text")
        if kw == "рилсмейкер":
            return _FakeResponse(500, {"error": "upstream oops"})
        if kw == "UGC креатор":
            return _FakeResponse(
                200,
                {
                    "items": [
                        {
                            "id": "201",
                            "name": "UGC креатор",
                            "alternate_url": "https://hh.ru/vacancy/201",
                            "description": "Ролики UGC в Reels @seen_hr",
                            "employer": {"name": "Brand1", "site_url": "https://b1.ru"},
                        },
                        {
                            "id": "202",
                            "name": "UGC креатор (Shorts)",
                            "alternate_url": "https://hh.ru/vacancy/202",
                            "description": "Ролики UGC в Shorts @fresh_hr",
                            "employer": {"name": "Brand2", "site_url": "https://b2.ru"},
                        },
                    ]
                },
            )
        return _FakeResponse(200, {"items": []})

    res = collect_hh_vacancies(
        token="tok",
        keywords=("рилсмейкер", "UGC креатор"),
        seen_ids={"201"},
        http_get_fn=fake_partial_error,
    )
    assert res["status"] == "ok"
    assert [r["vacancy_id"] for r in res["rows"]] == ["202"]

    def fake_429(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        return _FakeResponse(429, {"description": "Too Many Requests"})

    res_429 = collect_hh_vacancies(token="tok", keywords=("рилсмейкер",), http_get_fn=fake_429)
    assert res_429["status"] == "rate_limited"
    assert res_429["rows"] == []


def test_cli_main_writes_jsonl_and_never_crashes_on_missing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadgen.hh_vacancies import main

    monkeypatch.delenv("HH_API_TOKEN", raising=False)
    out_file = tmp_path / "hh_out.jsonl"
    rc = main(["--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == ""


def test_pagination_limit_wa_description_phone_and_custom_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leadgen.hh_vacancies import collect_hh_vacancies, main

    pages_seen: list[int] = []

    def fake_get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> _FakeResponse:
        page = int((params or {}).get("page") or 0)
        pages_seen.append(page)
        if page == 0:
            return _FakeResponse(
                200,
                {
                    "pages": 2,
                    "items": [
                        {
                            "id": "601",
                            "name": "Рилсмейкер на бренд",
                            "alternate_url": "https://hh.ru/vacancy/601",
                            "employer": {"name": "Aura Care", "site_url": "auracare.ru"},
                            "contacts": {"phones": [{"country": "8", "city": "926", "number": "7418529", "comment": "TG @aura_hr"}]},
                            "description": "Монтаж и съемка Reels.",
                        }
                    ],
                },
            )
        return _FakeResponse(
            200,
            {
                "pages": 2,
                "items": [
                    {
                        "id": "602",
                        "name": "Видеомейкер Shorts",
                        "alternate_url": "https://hh.ru/vacancy/602",
                        "employer": {"name": "Velvet Lab", "site_url": "https://velvetlab.ru"},
                        "contacts": None,
                        "description": "Короткие видео Shorts. Связь https://wa.me/79165553311",
                    },
                    {
                        "id": "603",
                        "name": "Контент креатор Reels",
                        "alternate_url": "https://hh.ru/vacancy/603",
                        "employer": {"name": "Extra Brand", "site_url": "https://extrabrand.ru"},
                        "contacts": None,
                        "description": "Создание Reels.",
                    },
                ],
            },
        )

    res = collect_hh_vacancies(
        token="tok",
        keywords=("рилсмейкер",),
        max_pages=2,
        limit=2,
        showcase_url="https://softugc.com/cases",
        custom_draft_template="Привет, {company}! По роли «{title}» наши кейсы: {showcase_url}",
        http_get_fn=fake_get,
    )
    assert res["status"] == "ok"
    assert pages_seen == [0, 1]
    assert len(res["rows"]) == 2
    r1, r2 = res["rows"]
    assert r1["segment"] == "vacancy"
    assert r1["username"] == "aura_hr"
    assert r1["phone"] == "+79267418529"
    assert r1["project_url"] == "https://auracare.ru"
    assert r1["company_domain"] == "auracare.ru"
    assert r1["step1_text"] == "Привет, Aura Care! По роли «Рилсмейкер на бренд» наши кейсы: https://softugc.com/cases"

    assert r2["segment"] == "vacancy"
    assert r2["phone"] == "+79165553311"
    assert r2["provider"] == "whatsapp"

    # CLI with mocked requests.get writing JSONL rows
    monkeypatch.setenv("HH_API_TOKEN", "cli-tok")
    monkeypatch.setattr("leadgen.hh_vacancies.requests.get", fake_get)
    cli_out = tmp_path / "cli_rows.jsonl"
    rc = main(["--output", str(cli_out), "--keyword", "рилсмейкер", "--limit", "1", "--no-hydrate"])
    assert rc == 0
    written = [json.loads(ln) for ln in cli_out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(written) == 1
    assert written[0]["vacancy_id"] == "601"

