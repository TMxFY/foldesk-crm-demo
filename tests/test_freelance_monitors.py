from __future__ import annotations

from email.message import EmailMessage
import json
from pathlib import Path
import re
import sys

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import freelance_monitors as fm
from leadgen import linkedin_jobs as lj


# ---------------------------------------------------------------------------
# 1. Classification: video/reels/UGC vs dev/storymaker/gaming/resume junk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,desc",
    [
        ("Нужен рилсмейкер для бренда косметики", "30 роликов Reels и Shorts в месяц, с хуками"),
        ("AI-видео и нейроаватары для товарного бренда", "Сделать 10 роликов на Kling / HeyGen под TikTok"),
        ("UGC креатор для рекламных роликов WB и Ozon", "Вертикальное видео 9:16 для таргета"),
        ("Нарезка подкаста в Shorts и Reels", "Нужен видеомонтажер в CapCut на постоянку"),
        ("Need AI UGC video creator for TikTok Ads", "Produce 20 short-form Reels variations/month"),
        ("Short-form Video Editor for SaaS YouTube Shorts", "Fast-paced retention editing + talking head"),
        ("Faceless AI avatar spokesperson videos", "Consistent ElevenLabs + HeyGen character for fintech"),
    ],
)
def test_classify_freelance_order_accepts_video_ugc_reels(title: str, desc: str) -> None:
    accepted, reason = fm.classify_freelance_order(title, desc)
    assert accepted is True
    assert reason == "matched:video_ugc_reels"


@pytest.mark.parametrize(
    "title,desc,expected_prefix",
    [
        ("Нужен сторисмейкер в Instagram", "Ежедневное оформление сторис и немного рилс", "rejected:storymaker"),
        ("Story maker for lifestyle blog", "Daily Instagram stories design", "rejected:storymaker"),
        ("Python / Django разработчик", "Доработка backend API и админки", "rejected:dev_or_non_video"),
        ("Разработка Telegram-бота на n8n", "Воронка заявок и интеграция с Битрикс24", "rejected:dev_or_non_video"),
        ("Full-stack React developer for SaaS", "Build customer dashboard and billing", "rejected:dev_or_non_video"),
        ("Build a Python TikTok video downloader bot", "Scrape reels links via Telegram bot", "rejected:dev_or_non_video"),
        ("Создать сайт на Tilda под ключ", "Лендинг с формой обратной связи", "rejected:dev_or_non_video"),
        ("Video editor for Roblox gaming montage", "Funny Roblox clips editing", "rejected:gaming_or_gambling"),
        ("Монтаж мемов для развлекательного паблика", "Нарезка мемов 150 руб ролик", "rejected:gaming_or_gambling"),
        ("#резюме Ищу заказы на монтаж Reels", "Моё портфолио и опыт 3 года", "rejected:resume_self_promo"),
        ("Бухгалтер на удалёнку ИП", "Ведение КУДиР и отчётность", "rejected:dev_or_non_video"),
        ("Настройка рекламы Яндекс Директ", "Нужен директолог для клиники", "rejected:no_video_signal"),
    ],
)
def test_classify_freelance_order_rejects_junk(title: str, desc: str, expected_prefix: str) -> None:
    accepted, reason = fm.classify_freelance_order(title, desc)
    assert accepted is False
    assert reason == expected_prefix


# ---------------------------------------------------------------------------
# 2. Strict non-bypass anti-bot / CAPTCHA handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,status_code,body,headers",
    [
        ("profi_ru", 403, "<html>Forbidden</html>", {}),
        ("kwork", 429, "Too Many Requests", {}),
        ("upwork", 200, "<html><title>Just a moment...</title>Cloudflare</html>", {}),
        ("profi_ru", 200, "<div>Yandex SmartCaptcha required</div>", {}),
        ("upwork", 200, "<div>Please verify you are human to continue</div>", {}),
        ("kwork", 200, "<form>Solve captcha</form>", {}),
        ("upwork", 200, "<html>Challenge</html>", {"cf-mitigated": "challenge"}),
    ],
)
def test_antibot_gates_return_blocked_by_antibot_without_retry_bypass(
    source: str,
    status_code: int,
    body: str,
    headers: dict[str, str],
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_kwargs):
        calls.append(url)
        return (status_code, headers, body)

    result = fm.monitor_source(source, http_get_fn=fake_get)
    assert result["status"] == "blocked_by_antibot"
    assert result["source"] == source
    assert result["rows"] == []
    assert result["reason"]
    # Must NEVER retry or bypass a challenge
    assert len(calls) == 1


def test_http_exception_403_returns_blocked_by_antibot() -> None:
    resp = requests.Response()
    resp.status_code = 403
    resp._content = b"Access guarded by Cloudflare"
    exc = requests.HTTPError("403 Client Error", response=resp)

    def failing_get(_url: str):
        raise exc

    result = fm.monitor_source("upwork", http_get_fn=failing_get)
    assert result["status"] == "blocked_by_antibot"
    assert result["rows"] == []


def test_http_401_returns_auth_required() -> None:
    result = fm.monitor_source("profi_ru", http_get_fn=lambda _u: (401, "Unauthorized"))
    assert result["status"] == "auth_required"
    assert result["rows"] == []


# ---------------------------------------------------------------------------
# 3. RSS / Atom & Public HTML Listings
# ---------------------------------------------------------------------------


def test_upwork_rss_feed_emits_en_vacancy_assisted_rows() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Upwork Jobs</title>
        <item>
          <title>Need AI UGC Video Creator for Skincare Reels &amp; TikTok</title>
          <link>https://www.upwork.com/jobs/~01aaa111</link>
          <description>&lt;p&gt;Looking for 15 short-form Reels/TikTok ads per month using AI avatars. Budget: $1,200&lt;/p&gt;</description>
          <guid>upwork-01aaa111</guid>
          <pubDate>Sun, 22 Nov 2026 10:00:00 GMT</pubDate>
        </item>
        <item>
          <title>Senior React Full-Stack Web Developer</title>
          <link>https://www.upwork.com/jobs/~02bbb222</link>
          <description>Build SaaS billing dashboard in Next.js and Postgres.</description>
        </item>
        <item>
          <title>Video editor for Roblox roleplay channel</title>
          <link>https://www.upwork.com/jobs/~03ccc333</link>
          <description>Need Roblox montage editor.</description>
        </item>
      </channel>
    </rss>"""

    res = fm.monitor_source(
        "upwork",
        feed_url="https://www.upwork.com/ab/feed/jobs/rss?q=ugc",
        http_get_fn=lambda _u: (200, rss_xml),
    )
    assert res["status"] == "ok"
    assert res["fetched_count"] == 3
    assert res["rejected_count"] == 2
    assert len(res["rows"]) == 1

    row = res["rows"][0]
    assert row["source"] == "upwork"
    assert row["segment"] == "vacancy_assisted"
    assert row["language"] == "en"
    assert row["manual_action_url"] == "https://www.upwork.com/jobs/~01aaa111"
    assert "Skincare Reels" in row["manual_draft_text"]
    assert "183.5M views" in row["manual_draft_text"]
    assert "https://tmxfy.github.io/" in row["manual_draft_text"]
    assert not re.search(r"[А-Яа-яЁё]", row["manual_draft_text"])
    assert "creatoros" not in row["manual_draft_text"].lower()


def test_atom_feed_parsing() -> None:
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Custom Kwork Feed</title>
      <entry>
        <title>Нужен рилсмейкер для запуска Reels воронки</title>
        <link rel="alternate" href="https://kwork.ru/projects/777001" />
        <id>kwork-777001</id>
        <updated>2026-11-22T12:00:00Z</updated>
        <summary>20 коротких видео в месяц, бюджет 25 000 ₽</summary>
      </entry>
    </feed>"""

    res = fm.monitor_source("kwork", http_get_fn=lambda _u: (200, atom_xml))
    assert res["status"] == "ok"
    assert len(res["rows"]) == 1
    row = res["rows"][0]
    assert row["segment"] == "vacancy_assisted"
    assert row["language"] == "ru"
    assert row["manual_action_url"] == "https://kwork.ru/projects/777001"
    assert "25 000 ₽" in row["budget"]


def test_kwork_and_profi_public_html_listings() -> None:
    kwork_html = """
    <html><body>
      <div class="want-card">
        <h3><a href="/projects/555111/view">Сделать 15 AI UGC роликов для Reels и Shorts</a></h3>
        <p class="description">Нужны реалистичные нейроперсонажи под товарку на WB, тест на месяц.</p>
        <span class="price">30 000 ₽</span>
        <span class="user-name">Алина</span>
      </div>
      <div class="want-card">
        <h3><a href="/projects/555222/view">Нужен сторисмейкер на каждый день</a></h3>
        <p class="description">Ведение сторис в инстаграм.</p>
      </div>
      <a href="https://kwork.ru/projects">Все проекты</a>
    </body></html>
    """
    kwork_res = fm.monitor_source("kwork", http_get_fn=lambda _u: (200, kwork_html))
    assert kwork_res["status"] == "ok"
    assert len(kwork_res["rows"]) == 1
    krow = kwork_res["rows"][0]
    assert krow["manual_action_url"] == "https://kwork.ru/projects/555111/view"
    assert krow["segment"] == "vacancy_assisted"
    assert krow["language"] == "ru"
    assert "Алина" in krow["manual_draft_text"]
    assert "183,5 млн" in krow["manual_draft_text"]
    assert "creatoros" not in krow["manual_draft_text"].lower()

    profi_html = """
    <html><body>
      <div class="order-card">
        <a href="https://profi.ru/orders/987654">Видеомонтаж Reels и YouTube Shorts для онлайн-школы</a>
        <p class="order-text">12 вертикальных роликов в месяц из исходников вебинара.</p>
        <div class="order-budget">18 000 ₽</div>
      </div>
      <div class="order-card">
        <a href="https://profi.ru/orders/987655">Разработка сайта на WordPress</a>
        <p class="order-text">Интернет-магазин под ключ.</p>
      </div>
    </body></html>
    """
    profi_res = fm.monitor_source("profi_ru", http_get_fn=lambda _u: (200, profi_html))
    assert profi_res["status"] == "ok"
    assert len(profi_res["rows"]) == 1
    prow = profi_res["rows"][0]
    assert prow["manual_action_url"] == "https://profi.ru/orders/987654"
    assert prow["segment"] == "vacancy_assisted"
    assert prow["language"] == "ru"
    assert "Видеомонтаж Reels и YouTube Shorts" in prow["manual_draft_text"]


# ---------------------------------------------------------------------------
# 4. Email Digest Ingestion (.eml HTML & plain-text fallback)
# ---------------------------------------------------------------------------


def test_email_digest_html_and_plain_text() -> None:
    msg_html = EmailMessage()
    msg_html["From"] = "noreply@kwork.ru"
    msg_html["Subject"] = "Новые проекты на Kwork"
    msg_html.set_content("Смотрите проекты в HTML-версии.")
    msg_html.add_alternative(
        """
        <html><body>
          <div class="order-item">
            <h4>Монтаж вертикальных видео Reels / TikTok на постоянку</h4>
            <p>Нужно 25 коротких роликов в месяц. Бюджет: 40 000 ₽</p>
            <a href="https://kwork.ru/projects/888001">Откликнуться</a>
          </div>
          <div class="order-item">
            <h4>Создать телеграм-бота на Python</h4>
            <a href="https://kwork.ru/projects/888002">Откликнуться</a>
          </div>
        </body></html>
        """,
        subtype="html",
    )

    msg_plain = EmailMessage()
    msg_plain["From"] = "orders@profi.ru"
    msg_plain["Subject"] = "Новые заказы по категории Видеомонтаж"
    msg_plain.set_content(
        "Новый заказ: Нужен UGC видеомейкер для Reels рекламы\n"
        "https://profi.ru/backoffice/n.php?id=443322\n"
        "Бюджет: 15 000 ₽\n"
    )

    res = fm.monitor_all_sources(
        sources=("profi_ru", "kwork"),
        email_digests=[msg_html.as_bytes(), msg_plain.as_bytes()],
    )
    assert res["status"] == "ok"
    assert len(res["rows"]) == 2
    by_src = {r["source"]: r for r in res["rows"]}
    assert by_src["kwork"]["manual_action_url"] == "https://kwork.ru/projects/888001"
    assert by_src["kwork"]["language"] == "ru"
    assert by_src["kwork"]["segment"] == "vacancy_assisted"
    assert by_src["profi_ru"]["manual_action_url"] == "https://profi.ru/backoffice/n.php?id=443322"
    assert by_src["profi_ru"]["language"] == "ru"
    assert by_src["profi_ru"]["segment"] == "vacancy_assisted"


# ---------------------------------------------------------------------------
# 5. feed_items injection, custom template scrubbing, partial failure resilience
# ---------------------------------------------------------------------------


def test_feed_items_and_custom_template_scrubs_legacy_brand() -> None:
    res = fm.monitor_source(
        "profi_ru",
        feed_items=[
            {
                "id": "p1",
                "title": "Нужен AI-видео продюсер для Reels",
                "url": "https://profi.ru/orders/112233",
                "description": "Поток 30 вертикальных видео в месяц",
                "budget": "50 000 ₽",
            },
            {
                "id": "p1-dup",
                "title": "Нужен AI-видео продюсер для Reels",
                "url": "https://profi.ru/orders/112233",
            },
            {
                "id": "p2-bad-url",
                "title": "Нужен рилсмейкер",
                "url": "javascript:alert(1)",
            },
        ],
        showcase_url="https://softugc.example/cases",
        custom_template="Привет! От CreatorOS по «{title}»: кейсы {showcase_url}",
    )
    assert res["status"] == "ok"
    assert len(res["rows"]) == 1
    draft = res["rows"][0]["manual_draft_text"]
    assert "SoftUGC" in draft
    assert "creatoros" not in draft.lower()
    assert "https://softugc.example/cases" in draft


def test_monitor_all_sources_isolates_blocked_source_and_cli(tmp_path: Path) -> None:
    def mixed_get(url: str, **_kwargs):
        if "profi.ru" in url:
            return (403, "SmartCaptcha block")
        if "kwork.ru" in url:
            return (
                200,
                '<div class="want-card"><a href="/projects/9911">Рилсмейкер для Reels и Shorts</a></div>',
            )
        return (429, "Cloudflare rate limit")

    summary = fm.monitor_all_sources(http_get_fn=mixed_get)
    assert summary["status"] == "partial"
    assert summary["sources"]["profi_ru"]["status"] == "blocked_by_antibot"
    assert summary["sources"]["upwork"]["status"] == "blocked_by_antibot"
    assert summary["sources"]["kwork"]["status"] == "ok"
    assert len(summary["rows"]) == 1

    # Disabled source in config does not invoke http_get_fn
    called_urls: list[str] = []
    disabled_summary = fm.monitor_all_sources(
        cfg={"vacancies": {"sources": {"profi_ru": False, "kwork": True, "upwork": False}}},
        http_get_fn=lambda u, **_kw: called_urls.append(u) or (200, ""),
    )
    assert disabled_summary["sources"]["profi_ru"]["status"] == "disabled"
    assert disabled_summary["sources"]["upwork"]["status"] == "disabled"
    assert len(called_urls) == 1 and "kwork.ru" in called_urls[0]

    # CLI exits 0 and writes JSONL when run with --eml
    eml = EmailMessage()
    eml["From"] = "alerts@upwork.com"
    eml.set_content("UGC Video Creator for TikTok\nhttps://www.upwork.com/jobs/~01xyz999\nBudget: $900\n")
    eml_path = tmp_path / "upwork.eml"
    eml_path.write_bytes(eml.as_bytes())
    out_jsonl = tmp_path / "out.jsonl"
    rc = fm.main(["--source", "upwork", "--eml", str(eml_path), "--output", str(out_jsonl)])
    assert rc == 0
    written = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(written) == 1
    assert written[0]["segment"] == "vacancy_assisted"
    assert written[0]["language"] == "en"


# ---------------------------------------------------------------------------
# 6. leadgen/linkedin_jobs.py: gaming deletion, domain hint, vacancy+en tags
# ---------------------------------------------------------------------------


def test_linkedin_jobs_removed_mobile_games_query_and_blocks_gaming() -> None:
    queries = [str(item.get("query") or "").casefold() for item in lj.SEARCHES]
    assert "creative producer mobile games" not in queries
    for banned in ("mobile games", "roblox", "fortnite", "fivem", "casino"):
        assert all(banned not in q for q in queries)

    payload = {
        "rows": [
            {
                "job_id": "g1",
                "title": "Creative Producer mobile games",
                "company": "PlayStudio",
                "location": "Remote, US",
                "url": "https://www.linkedin.com/jobs/view/1001",
            },
            {
                "job_id": "g2",
                "title": "UGC Video Editor",
                "company": "Roblox Creator Hub",
                "location": "United States",
                "url": "https://www.linkedin.com/jobs/view/1002",
            },
            {
                "job_id": "ok1",
                "title": "UGC Creator & Creative Strategist",
                "company": "Aura Health",
                "location": "United States (Remote)",
                "url": "https://www.linkedin.com/jobs/view/1003",
                "company_website": "https://aurahealth.io/careers",
            },
        ]
    }
    rows = lj.rows_from_payload(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "Aura Health"
    assert row["company_domain"] == "aurahealth.io"
    assert row["project_url"] == "https://aurahealth.io/careers"
    assert row["segment"] == "vacancy"
    assert row["language"] == "en"
    assert row["source"] == "linkedin_jobs"


def test_linkedin_jobs_domain_extraction_hints_and_no_fabrication() -> None:
    payload = {
        "rows": [
            {
                "job_id": "d1",
                "title": "TikTok Content Creator",
                "company": "FinFlow",
                "location": "London, UK",
                "url": "https://www.linkedin.com/jobs/view/2001",
                "description": "Apply on https://boards.greenhouse.io/finflow or visit https://finflow.app/about or email talent@gmail.com",
            },
            {
                "job_id": "d2",
                "title": "Performance Creative Lead",
                "company": "Loom.ai",
                "location": "Remote, US",
                "url": "https://www.linkedin.com/jobs/view/2002",
            },
            {
                "job_id": "d3",
                "title": "Short-form Content Lead",
                "company": "Quiet Brand LLC",
                "location": "Remote, Canada",
                "url": "https://www.linkedin.com/jobs/view/2003",
            },
            {
                "job_id": "ru1",
                "title": "UGC Creator",
                "company": "Moscow Shop",
                "location": "Moscow, Russia",
                "url": "https://www.linkedin.com/jobs/view/2004",
            },
        ]
    }
    rows = lj.rows_from_payload(payload)
    assert len(rows) == 3

    by_comp = {r["company"]: r for r in rows}
    assert by_comp["FinFlow"]["company_domain"] == "finflow.app"
    assert by_comp["FinFlow"]["project_url"] == "https://finflow.app"
    assert by_comp["Loom.ai"]["company_domain"] == "loom.ai"
    assert by_comp["Loom.ai"]["project_url"] == "https://loom.ai"
    # Never fabricate a domain when none exists on the card
    assert by_comp["Quiet Brand LLC"]["company_domain"] == ""
    assert by_comp["Quiet Brand LLC"]["project_url"] == ""
    assert by_comp["Quiet Brand LLC"]["links"] == ["https://www.linkedin.com/jobs/view/2003"]
    assert all(r["segment"] == "vacancy" and r["language"] == "en" for r in rows)


def test_linkedin_jobs_collect_dedup_and_resilience() -> None:
    def fake_call(_endpoint: str, *, method=None, data=None):
        assert method == "POST"
        if data["query"] == "UGC Creator":
            raise RuntimeError("transient scrape error")
        return {
            "rows": [
                {
                    "job_id": "shared-id",
                    "title": "TikTok Content Creator",
                    "company": "ScaleApp",
                    "location": "New York, NY",
                    "url": "https://www.linkedin.com/jobs/view/3001",
                    "email": "jobs@scaleapp.io",
                }
            ]
        }

    rows = lj.collect(call=fake_call)
    assert len(rows) == 1
    assert rows[0]["company_domain"] == "scaleapp.io"
    assert rows[0]["project_url"] == "https://scaleapp.io"
    assert rows[0]["segment"] == "vacancy"
    assert rows[0]["language"] == "en"


# ---------------------------------------------------------------------------
# 7. Sacred Rules static check on owned files
# ---------------------------------------------------------------------------


def test_owned_leadgen_files_follow_sacred_rules() -> None:
    for rel in ("leadgen/freelance_monitors.py", "leadgen/linkedin_jobs.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "PYTEST_CURRENT_TEST" not in src
        assert 'sys.modules["pytest"]' not in src
        assert "sys.modules['pytest']" not in src
        assert not re.search(r"\b[A-Z][A-Z0-9_]*_IN_TEST\b", src)
