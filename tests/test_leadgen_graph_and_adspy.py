import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import tg_ad_spy, tg_channel_graph


class FakeChannel:
    def __init__(
        self,
        id: int,
        username: str,
        title: str,
        participants_count: int = 1500,
        access_hash: int = 111,
    ):
        self.id = id
        self.username = username
        self.title = title
        self.participants_count = participants_count
        self.access_hash = access_hash


class FakeUser:
    def __init__(
        self,
        id: int,
        username: str,
        first_name: str = "Alex",
        last_name: str = "Founder",
        bot: bool = False,
    ):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.bot = bot


class FakeMessage:
    def __init__(self, id: int, message: str):
        self.id = id
        self.message = message


class FakeGraphTelethonClient:
    def __init__(self, graph_data: dict):
        self.graph_data = graph_data

    async def get_entity(self, target):
        key = str(target).lstrip("@").lower()
        if key in self.graph_data:
            return self.graph_data[key]["entity"]
        raise ValueError(f"Unknown entity: {target}")

    async def get_messages(self, entity, limit: int = 3):
        key = getattr(entity, "username", str(entity)).lstrip("@").lower()
        info = self.graph_data.get(key, {})
        msgs = info.get("messages", [])
        return [FakeMessage(i + 1, text) for i, text in enumerate(msgs[:limit])]

    async def __call__(self, request):
        req_name = request.__class__.__name__
        if req_name == "GetChannelRecommendationsRequest":
            ch = getattr(request, "channel", None)
            key = getattr(ch, "username", str(ch)).lstrip("@").lower()
            recs = self.graph_data.get(key, {}).get("recommendations", [])
            return SimpleNamespace(chats=[self.graph_data[r]["entity"] for r in recs if r in self.graph_data])
        if req_name == "GetFullChannelRequest":
            ch = getattr(request, "channel", None)
            key = getattr(ch, "username", str(ch)).lstrip("@").lower()
            info = self.graph_data[key]
            entity = info["entity"]
            full_chat = SimpleNamespace(
                about=info.get("about", ""),
                participants_count=info.get("participants_count", entity.participants_count),
                linked_chat_id=info.get("linked_chat_id"),
            )
            return SimpleNamespace(full_chat=full_chat, chats=[entity])
        if req_name == "GetFullUserRequest":
            uid = getattr(request, "id", None)
            key = str(uid).lstrip("@").lower()
            info = self.graph_data.get(key, {})
            user = info.get("user") or FakeUser(id=999, username=key)
            return SimpleNamespace(
                users=[user],
                full_user=SimpleNamespace(about=info.get("user_bio", "Founder & CEO")),
            )
        raise NotImplementedError(req_name)


def test_channel_bio_parsing_extracts_founder_and_site_and_skips_bots():
    about = (
        "Авторский блог про запуск AI SaaS и маркетинг.\n"
        "Бот поддержки: @helper_support_bot\n"
        "Основатель: @ivan_saas_founder\n"
        "Наш продукт: https://superagent.ai/pricing"
    )
    posts = [
        "Запустили новый релиз https://superagent.ai — пишите фидбек в личку @ivan_saas_founder",
    ]
    parsed = tg_channel_graph.parse_channel_founder_info(
        about=about,
        posts=posts,
        channel_username="ivan_saas_blog",
        channel_title="Ivan SaaS Blog",
        subscribers=4200,
    )
    assert parsed is not None
    assert parsed["username"] == "ivan_saas_founder"
    assert parsed["site"] == "https://superagent.ai/pricing"


def test_channel_bio_filters_out_million_news_and_crypto_casino_and_spam_ad_managers():
    # >100k subscribers news channel -> rejected
    too_big = tg_channel_graph.parse_channel_founder_info(
        about="Главные новости технологий. Контакт: @editor_news",
        posts=[],
        channel_username="tech_news_million",
        channel_title="Tech News 1M",
        subscribers=150_000,
    )
    assert too_big is None

    # Crypto casino -> rejected
    casino = tg_channel_graph.parse_channel_founder_info(
        about="Лучшее крипто казино 2026! Слоты, ставки, бонус 500%. Админ @lucky_boss",
        posts=["Забирай фриспины в 1win casino"],
        channel_username="crypto_slots_vip",
        channel_title="Crypto Casino VIP",
        subscribers=5000,
    )
    assert casino is None

    # Only bot and ad manager on a meme/spam public -> rejected
    spam_pub = tg_channel_graph.parse_channel_founder_info(
        about="По всем вопросам рекламы: @ad_manager_pr, предложка @news_predlozhka_bot",
        posts=[],
        channel_username="memes_daily_ru",
        channel_title="Мемы дня | Халява и Розыгрыши",
        subscribers=45000,
    )
    assert spam_pub is None


def test_channel_graph_bfs_crawls_recommendations_and_saves_linked_chats(tmp_path):
    graph_data = {
        "seed_chan": {
            "entity": FakeChannel(1001, "seed_chan", "Seed Founder Channel", 3200),
            "about": "Строю B2B SaaS https://seedflow.io | Автор: @seed_founder",
            "linked_chat_id": 777001,
            "messages": ["Новое демо на https://seedflow.io"],
            "recommendations": ["rec_one", "big_news", "casino_vip"],
        },
        "rec_one": {
            "entity": FakeChannel(1002, "rec_one", "AI Product Notes", 1800),
            "about": "Making Voice AI agents at https://voicecraft.dev. DM @voice_ceo",
            "linked_chat_id": 777002,
            "messages": ["Shipped v2 of VoiceCraft!"],
            "recommendations": ["rec_two"],
        },
        "rec_two": {
            "entity": FakeChannel(1003, "rec_two", "Indie Dev Log", 950),
            "about": "Solo founder building https://indiedesk.app — @indie_maker",
            "linked_chat_id": None,
            "messages": ["Revenue update: $12k MRR"],
            "recommendations": [],
        },
        "big_news": {
            "entity": FakeChannel(1004, "big_news", "Breaking News", 250_000),
            "about": "News 24/7 @news_admin",
            "linked_chat_id": 777999,
            "messages": [],
            "recommendations": [],
        },
        "casino_vip": {
            "entity": FakeChannel(1005, "casino_vip", "Crypto Casino Slots", 8000),
            "about": "Слоты и ставки @casino_owner",
            "linked_chat_id": None,
            "messages": [],
            "recommendations": [],
        },
        "seed_founder": {"user": FakeUser(501, "seed_founder", "Max", "Seed")},
        "voice_ceo": {"user": FakeUser(502, "voice_ceo", "Elena", "Voice")},
        "indie_maker": {"user": FakeUser(503, "indie_maker", "Dan", "Indie")},
    }

    client = FakeGraphTelethonClient(graph_data)
    state_file = tmp_path / "channel_graph_seen.json"
    linked_file = tmp_path / "discovered_comment_channels.txt"
    out_file = tmp_path / "graph_leads.jsonl"

    rows = asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=client,
            seeds=["seed_chan"],
            depth=2,
            limit=10,
            state_path=str(state_file),
            output_path=str(out_file),
            linked_chats_path=str(linked_file),
            delay=0.0,
        )
    )

    assert len(rows) == 3
    usernames = [r["username"] for r in rows]
    assert usernames == ["seed_founder", "voice_ceo", "indie_maker"]

    for r in rows:
        assert r["source"] == "tg_channel_graph"
        assert r["contact_channels"] == ["telegram"]
        assert r["site"].startswith("https://")
        assert r["channel_username"] in {"seed_chan", "rec_one", "rec_two"}
        assert isinstance(r["sample_messages"], list) and len(r["sample_messages"]) >= 1

    # Check output JSONL
    written = [json.loads(line) for line in out_file.read_text().splitlines() if line.strip()]
    assert len(written) == 3

    # Check discovered linked comment channels
    linked_lines = linked_file.read_text().splitlines()
    assert "id777001" in linked_lines
    assert "id777002" in linked_lines
    assert "id777999" not in linked_lines


def test_ad_spy_detects_utm_erid_and_external_cta_mentions():
    # 1. UTM link post
    utm_post = (
        "Недавно протестировали крутой сервис для аналитики продаж "
        "https://metricflow.io/?utm_source=telegram&utm_medium=cpc&utm_campaign=saas_chat "
        "— очень советую попробовать!"
    )
    sig1 = tg_ad_spy.detect_ad_post(utm_post, host_channel="saas_chat")
    assert sig1["is_ad"] is True
    assert "https://metricflow.io" in sig1["product_site"]

    # 2. #реклама / erid marker + external channel handle
    erid_post = (
        "Как автоматизировать отдел продаж за 1 вечер? Читайте в канале @sales_ai_product "
        "и забирайте гайд по промокоду START.\n\n#реклама ИП Иванов erid: 2Vtzqv12345"
    )
    sig2 = tg_ad_spy.detect_ad_post(erid_post, host_channel="marketing_daily")
    assert sig2["is_ad"] is True
    assert "sales_ai_product" in sig2["tg_targets"]

    # 3. CTA + external t.me handle without explicit #ad tag
    cta_post = (
        "Ребята из @clipfarm_ai запустили платформу генерации рилсов. "
        "Переходите по ссылке https://t.me/clipfarm_ai и тестируйте бесплатно!"
    )
    sig3 = tg_ad_spy.detect_ad_post(cta_post, host_channel="indie_founders")
    assert sig3["is_ad"] is True
    assert "clipfarm_ai" in sig3["tg_targets"]

    # 4. Regular post mentioning own channel only -> not an ad
    self_post = "Подписывайтесь на наш канал @indie_founders, завтра разбор метрик."
    sig4 = tg_ad_spy.detect_ad_post(self_post, host_channel="indie_founders")
    assert sig4["is_ad"] is False


def test_ad_spy_pipeline_creates_valid_rows_compatible_with_gsheet(tmp_path):
    graph_data = {
        "saas_digest": {
            "entity": FakeChannel(2001, "saas_digest", "SaaS Digest", 18000),
            "messages": [
                (
                    "Попробуйте AI-копилот для фаундеров https://copilothq.ai/?utm_source=tg&utm_campaign=digest "
                    "Вопросы фаундеру: @copilot_founder #реклама erid: 2Vtzq999"
                ),
                (
                    "Рекомендую канал про рост стартапов @growth_lab_channel — переходите и читайте разборы!"
                ),
                "Обычный утренний пост без рекламы.",
            ],
        },
        "growth_lab_channel": {
            "entity": FakeChannel(2002, "growth_lab_channel", "Growth Lab", 6400),
            "about": "Канал студии роста https://growthlab.io | Основатель @growth_ceo",
        },
        "copilot_founder": {"user": FakeUser(601, "copilot_founder", "Anton", "Copilot")},
        "growth_ceo": {"user": FakeUser(602, "growth_ceo", "Sergey", "Growth")},
    }

    client = FakeGraphTelethonClient(graph_data)
    seen_file = tmp_path / "ad_spy_seen.json"
    out_file = tmp_path / "ad_spy_leads.jsonl"

    rows = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=client,
            channels=["saas_digest"],
            msg_limit=20,
            seen_path=str(seen_file),
            output_path=str(out_file),
            delay=0.0,
            fetch_html=None,
        )
    )

    assert len(rows) == 2
    by_user = {r["username"]: r for r in rows}
    assert "copilot_founder" in by_user
    assert "growth_ceo" in by_user

    r1 = by_user["copilot_founder"]
    assert r1["source"] == "tg_ad_spy"
    assert r1["site"] == "https://copilothq.ai"
    assert "telegram" in r1["contact_channels"]
    assert len(r1["sample_messages"]) == 1

    r2 = by_user["growth_ceo"]
    assert r2["source"] == "tg_ad_spy"
    assert r2["site"] == "https://growthlab.io"
    assert "telegram" in r2["contact_channels"]

    # Verify compatibility with external site HTML contact extraction (X handle fallback)
    html_post = (
        "Сервис видео-генерации https://vividreel.ai/?ref=saas_digest "
        "дарит скидку 20% по промокоду DIGEST!"
    )
    graph_data["saas_digest"]["messages"] = [html_post]
    out_file2 = tmp_path / "ad_spy_leads2.jsonl"

    def fake_fetch_html(url: str) -> str:
        return "<html><head><title>VividReel AI — Short Video Generator</title></head><body>Follow founder https://x.com/vivid_founder</body></html>"

    rows2 = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=client,
            channels=["saas_digest"],
            msg_limit=10,
            seen_path=str(seen_file),
            output_path=str(out_file2),
            delay=0.0,
            fetch_html=fake_fetch_html,
        )
    )
    assert len(rows2) == 1
    rx = rows2[0]
    assert rx["company"] == "VividReel AI — Short Video Generator"
    assert rx["site"] == "https://vividreel.ai"
    assert rx["founder_x"] == "https://x.com/vivid_founder"
    assert "x" in rx["contact_channels"]

    from leadgen.tg_to_gsheet import admit_discovered_row

    admitted_tg = admit_discovered_row(r1)
    assert admitted_tg is not None
    assert admitted_tg["contact_url"] == "https://t.me/copilot_founder"
    assert admitted_tg["icp_match"] is True

    admitted_x = admit_discovered_row(rx)
    assert admitted_x is not None
    assert admitted_x["contact_url"] == "https://x.com/vivid_founder"
    assert admitted_x["icp_match"] is True

