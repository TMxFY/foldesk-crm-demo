from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from leadgen import meta_ad_leads, tg_ad_spy, tg_channel_graph


def _personal_tme_html(username: str, title: str, bio: str = "") -> str:
    desc_html = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{username}</div>'
        f"{desc_html}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Send Message</a></div>'
        "</body></html>"
    )


def _channel_tme_html(username: str, title: str, subs: str = "14 200 subscribers") -> str:
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">{subs}</div>'
        f'<div class="tgme_page_description">Official channel @{username}</div>'
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">View in Telegram</a></div>'
        "</body></html>"
    )


def _group_tme_html(username: str, title: str) -> str:
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        '<div class="tgme_page_extra">1 250 members, 84 online</div>'
        f'<div class="tgme_page_description">Community @{username}</div>'
        "</body></html>"
    )


def _bot_tme_html(username: str, title: str) -> str:
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{username}</div>'
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Start Bot</a></div>'
        "</body></html>"
    )


def test_meta_ad_leads_resolves_google_play_and_app_store_links_and_skips_megacorps(monkeypatch):
    play_url = "https://play.google.com/store/apps/details?id=horoscope.tarot.ai"
    ios_url = "https://apps.apple.com/us/app/ai-conversation-assistant/id6479991234"
    megacorp_flag_url = "https://apps.apple.com/us/app/big-corp-game/id111111111"
    megacorp_ratings_url = "https://play.google.com/store/apps/details?id=com.huge.millionreviews"

    ad_payload = {
        "searchResults": [
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Horoscope Tarot AI",
                    "body": {"text": "Daily AI tarot & birth chart readings"},
                    "link_url": play_url,
                    "cta_text": "Install Now",
                },
            },
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "AI Conversation Assistant",
                    "body": {"text": "Practice languages with real-time AI voice coach"},
                    "link_url": ios_url,
                    "cta_text": "Download",
                },
            },
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "MegaCorp Flagged App",
                    "body": {"text": "Play now with millions of players"},
                    "link_url": megacorp_flag_url,
                    "cta_text": "Install Now",
                },
            },
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Huge Studio App",
                    "body": {"text": "Top grossing puzzle game"},
                    "link_url": megacorp_ratings_url,
                    "cta_text": "Install Now",
                },
            },
        ]
    }

    resolver_calls: list[str] = []
    enrich_calls: list[tuple[str, str]] = []
    scraped_urls: list[str] = []

    def fake_store_resolver(url: str) -> dict:
        resolver_calls.append(url)
        if url == play_url:
            return {
                "developer": "TarotAI Studio",
                "website": "https://tarotai.app",
                "domain": "tarotai.app",
                "email": "support@tarotai.app",
                "rating_count": 4200,
                "is_megacorp": False,
            }
        if url == ios_url:
            return {
                "developer": "ConvoMind Labs",
                "website": "https://convomind.ai",
                "domain": "convomind.ai",
                "email": "",
                "rating_count": 1850,
                "is_megacorp": False,
            }
        if url == megacorp_flag_url:
            return {
                "developer": "Supercell Oy",
                "website": "https://supercell.com",
                "domain": "supercell.com",
                "rating_count": 12000,
                "is_megacorp": True,
            }
        if url == megacorp_ratings_url:
            return {
                "developer": "Huge Games Ltd",
                "website": "https://hugegames.io",
                "domain": "hugegames.io",
                "rating_count": 85000,
                "is_megacorp": False,
            }
        return {}

    def fake_enrich(company: str, domain: str) -> dict:
        enrich_calls.append((company, domain))
        if domain == "convomind.ai":
            return {
                "founder_linkedin": "https://www.linkedin.com/in/claire-convomind",
                "work_email": "claire@convomind.ai",
            }
        return {}

    def fake_scrape_site_contacts(url: str) -> dict[str, str]:
        scraped_urls.append(url)
        if url == "https://tarotai.app":
            return {
                "username": "tarot_founder",
                "founder_x": "https://x.com/tarot_founder",
            }
        return {}

    monkeypatch.setattr(meta_ad_leads, "_scrape_site_contacts", fake_scrape_site_contacts)

    calls_count = 0

    def fake_search(query: str):
        nonlocal calls_count
        calls_count += 1
        return ad_payload if calls_count == 1 else {"searchResults": []}

    rows, updated_seen = meta_ad_leads.collect(
        day=date(2026, 11, 17),
        seen={},
        cap=10,
        search_fn=fake_search,
        enrich_fn=fake_enrich,
        store_resolver_fn=fake_store_resolver,
    )

    assert len(rows) == 2
    assert ("TarotAI Studio", "tarotai.app") in enrich_calls
    assert ("ConvoMind Labs", "convomind.ai") in enrich_calls
    assert "https://tarotai.app" in scraped_urls
    assert "https://convomind.ai" in scraped_urls

    by_project = {r["project"]: r for r in rows}
    r_play = by_project["Horoscope Tarot AI"]
    assert r_play["project_url"] == "https://tarotai.app"
    assert r_play["work_email"] == "support@tarotai.app"
    assert r_play["username"] == "tarot_founder"
    assert r_play["founder_x"] == "https://x.com/tarot_founder"
    assert play_url in r_play["links"]
    assert "https://tarotai.app" in r_play["links"]

    r_ios = by_project["AI Conversation Assistant"]
    assert r_ios["project_url"] == "https://convomind.ai"
    assert r_ios["work_email"] == "claire@convomind.ai"
    assert r_ios["founder_linkedin"] == "https://www.linkedin.com/in/claire-convomind"
    assert ios_url in r_ios["links"]
    assert "https://convomind.ai" in r_ios["links"]

    assert "domain:tarotai.app" in updated_seen
    assert "domain:convomind.ai" in updated_seen
    assert "domain:supercell.com" not in updated_seen
    assert "domain:hugegames.io" not in updated_seen


def test_meta_ad_leads_custom_enrich_fn_without_store_resolver_skips_network(monkeypatch):
    def boom_resolver(url: str):
        raise AssertionError(f"Should not call default store resolver when enrich_fn is custom: {url}")

    monkeypatch.setattr(meta_ad_leads, "_default_store_resolver", boom_resolver, raising=False)

    payload = {
        "searchResults": [
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Indie Habit App",
                    "body": {"text": "Build better habits"},
                    "link_url": "https://apps.apple.com/us/app/indie-habit/id999999",
                },
            }
        ]
    }
    rows, _ = meta_ad_leads.collect(
        day=date(2026, 11, 17),
        seen={},
        cap=5,
        search_fn=lambda q: payload,
        enrich_fn=lambda company, domain: {"work_email": "ceo@indiehabit.app"},
    )
    assert len(rows) == 1
    assert rows[0]["work_email"] == "ceo@indiehabit.app"


def test_meta_ad_leads_builtin_store_resolver_parses_itunes_and_play_html(monkeypatch):
    import requests

    def fake_get(url, headers=None, timeout=6, **kwargs):
        if "itunes.apple.com/lookup" in url:
            body = json.dumps(
                {
                    "resultCount": 1,
                    "results": [
                        {
                            "sellerName": "ConvoMind Inc",
                            "sellerUrl": "https://convomind.ai/home",
                            "userRatingCount": 3100,
                        }
                    ],
                }
            )
            return SimpleNamespace(ok=True, status_code=200, text=body, json=lambda: json.loads(body))
        if "play.google.com/store/apps/details" in url:
            html_doc = (
                '<html><head><script type="application/ld+json">'
                '{"@type":"SoftwareApplication","author":{"@type":"Organization","name":"TarotAI Labs"},'
                '"aggregateRating":{"ratingCount":"4900"}}'
                "</script></head><body>"
                '<a href="https://www.google.com/url?q=https%3A%2F%2Ftarotai.app&amp;sa=D">Visit website</a>'
                '<a href="mailto:founders@tarotai.app">founders@tarotai.app</a>'
                "</body></html>"
            )
            return SimpleNamespace(ok=True, status_code=200, text=html_doc)
        raise AssertionError(f"Unexpected HTTP call: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    # Force fallback path without monid_client
    monkeypatch.setitem(sys.modules, "leadgen.monid_client", None)
    monkeypatch.setitem(sys.modules, "monid_client", None)

    ios_res = meta_ad_leads._default_store_resolver(
        "https://apps.apple.com/us/app/ai-conversation-assistant/id6479991234"
    )
    assert ios_res["developer"] == "ConvoMind Inc"
    assert ios_res["domain"] == "convomind.ai"
    assert ios_res["website"].startswith("https://convomind.ai")
    assert ios_res["rating_count"] == 3100
    assert ios_res["is_megacorp"] is False

    play_res = meta_ad_leads._default_store_resolver(
        "https://play.google.com/store/apps/details?id=horoscope.tarot.ai"
    )
    assert play_res["developer"] == "TarotAI Labs"
    assert play_res["domain"] == "tarotai.app"
    assert play_res["website"] == "https://tarotai.app"
    assert play_res["email"] == "founders@tarotai.app"
    assert play_res["rating_count"] == 4900
    assert play_res["is_megacorp"] is False


def test_tg_ad_spy_and_channel_graph_default_seeds_and_no_default_flags(monkeypatch, tmp_path):
    assert len(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS) >= 12
    for expected_ch in (
        "gonzo_ML",
        "ai_newz",
        "startupoftheday",
        "productradar_official",
        "indiehackers_ru",
        "vc_blogs",
        "toncoin_rus",
        "forklog",
        "rbc_crypto",
        "gamedev_ru",
        "neurochannel",
        "ecom_digest",
    ):
        assert expected_ch in tg_ad_spy.DEFAULT_AD_SPY_CHANNELS

    assert len(tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS) >= 8
    for expected_seed in (
        "startupoftheday",
        "productradar_official",
        "gonzo_ML",
        "ai_newz",
        "indiehackers_ru",
        "neurochannel",
        "gamedev_ru",
        "ton_builders",
    ):
        assert expected_seed in tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS

    monkeypatch.setenv("TG_AD_SPY_CHANNELS", "")
    monkeypatch.setenv("TG_GRAPH_SEED_CHANNELS", "")
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "fakehash123")

    class DummyTelethonClient:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            return self

        async def disconnect(self):
            return None

    monkeypatch.setattr(tg_ad_spy, "TelegramClient", DummyTelethonClient)
    monkeypatch.setattr(tg_channel_graph, "TelegramClient", DummyTelethonClient)

    spy_captured: dict = {}

    async def fake_scan_ad_channels(**kwargs):
        spy_captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("", encoding="utf-8")
        return []

    monkeypatch.setattr(tg_ad_spy, "scan_ad_channels", fake_scan_ad_channels)

    spy_out = tmp_path / "spy.jsonl"
    asyncio.run(
        tg_ad_spy._async_main(
            argparse.Namespace(
                session="dummy.session",
                channels="",
                no_default_channels=False,
                msg_limit=20,
                seen=str(tmp_path / "spy_seen.json"),
                output=str(spy_out),
                delay=0.0,
            )
        )
    )
    assert spy_captured["channels"] == list(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS)
    assert spy_captured["public_posts_fn"] is tg_ad_spy.fetch_public_channel_posts_http

    # With --no-default-channels, skips without calling scan_ad_channels
    spy_captured.clear()
    spy_empty_out = tmp_path / "spy_empty.jsonl"
    asyncio.run(
        tg_ad_spy._async_main(
            argparse.Namespace(
                session="dummy.session",
                channels="",
                no_default_channels=True,
                msg_limit=20,
                seen=str(tmp_path / "spy_seen.json"),
                output=str(spy_empty_out),
                delay=0.0,
            )
        )
    )
    assert spy_captured == {}
    assert spy_empty_out.exists() and spy_empty_out.read_text(encoding="utf-8") == ""

    graph_captured: dict = {}

    async def fake_crawl_channel_graph(**kwargs):
        graph_captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("", encoding="utf-8")
        return []

    monkeypatch.setattr(tg_channel_graph, "crawl_channel_graph", fake_crawl_channel_graph)

    graph_out = tmp_path / "graph.jsonl"
    asyncio.run(
        tg_channel_graph._async_main(
            argparse.Namespace(
                session="dummy.session",
                seeds="",
                no_default_seeds=False,
                depth=2,
                limit=15,
                state=str(tmp_path / "graph_seen.json"),
                output=str(graph_out),
                delay=0.0,
            )
        )
    )
    assert graph_captured["seeds"] == list(tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS)

    # With --no-default-seeds, skips without calling crawl_channel_graph
    graph_captured.clear()
    graph_empty_out = tmp_path / "graph_empty.jsonl"
    asyncio.run(
        tg_channel_graph._async_main(
            argparse.Namespace(
                session="dummy.session",
                seeds="",
                no_default_seeds=True,
                depth=2,
                limit=15,
                state=str(tmp_path / "graph_seen.json"),
                output=str(graph_empty_out),
                delay=0.0,
            )
        )
    )
    assert graph_captured == {}
    assert graph_empty_out.exists() and graph_empty_out.read_text(encoding="utf-8") == ""


def test_fetch_public_channel_posts_http_parses_monid_treg_and_tme_s_html_and_wires_scan(tmp_path):
    # 1) monid litescrape payload
    posts_monid = tg_ad_spy.fetch_public_channel_posts_http(
        "@gonzo_ML",
        limit=5,
        monid_fn=lambda ch, lim: {"posts": [{"text": "Ad post https:// clipboost.ai/?utm_source=tg #реклама"}]},
        treg_fn=lambda ch, lim: (_ for _ in ()).throw(AssertionError("Should not hit treg when monid succeeded")),
        http_get_fn=lambda url: (_ for _ in ()).throw(AssertionError("Should not hit t.me/s when monid succeeded")),
    )
    assert len(posts_monid) == 1
    assert "clipboost.ai" in posts_monid[0]

    # 2) treg crawl4ai.telegram.channel.posts fallback when monid returns empty
    posts_treg = tg_ad_spy.fetch_public_channel_posts_http(
        "ai_newz",
        limit=5,
        monid_fn=lambda ch, lim: None,
        treg_fn=lambda ch, lim: {
            "output": {
                "messages": [
                    {"message": "Try https://voicepilot.io/?ref=ai_newz today!"},
                    {"message": "Regular AI news post."},
                ]
            }
        },
        http_get_fn=lambda url: (_ for _ in ()).throw(AssertionError("Should not hit t.me/s when treg succeeded")),
    )
    assert len(posts_treg) == 2
    assert "https://voicepilot.io/?ref=ai_newz" in posts_treg[0]

    # 3) https://t.me/s/<channel> .tgme_widget_message_text HTML fallback (including hidden <a href> UTM URL!)
    tme_s_html = """
    <html><body>
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          Запустили AI-видео генератор для селлеров —
          <a href="https://sellerclips.ai/?utm_source=telegram&amp;utm_campaign=startupoftheday" target="_blank">попробовать бесплатно</a>!
          Вопросы в личку @sellerclips_ceo #реклама erid: 2Vtzqx777
        </div>
      </div>
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          Обычный обзор рынка без рекламных ссылок.
        </div>
      </div>
    </body></html>
    """
    posts_html = tg_ad_spy.fetch_public_channel_posts_http(
        "startupoftheday",
        limit=10,
        monid_fn=lambda ch, lim: None,
        treg_fn=lambda ch, lim: None,
        http_get_fn=lambda url: tme_s_html if url == "https://t.me/s/startupoftheday" else "",
    )
    assert len(posts_html) == 2
    assert "https://sellerclips.ai/?utm_source=telegram&utm_campaign=startupoftheday" in posts_html[0]
    assert "@sellerclips_ceo" in posts_html[0]

    # 4) scan_ad_channels uses public_posts_fn WITHOUT calling client.get_messages, and falls back only when empty
    get_messages_called_for: list[str] = []

    class FakeSpyClient:
        async def get_entity(self, target):
            key = str(target).lstrip("@").lower()
            if key == "fallback_chan":
                return SimpleNamespace(id=3001, username="fallback_chan", title="Fallback Chan")
            return SimpleNamespace(id=901, username=key, first_name="Max", last_name="Seller")

        async def get_messages(self, entity, limit: int = 40):
            ch_name = getattr(entity, "username", "")
            get_messages_called_for.append(ch_name)
            return [
                SimpleNamespace(
                    id=1,
                    message="Реклама сервиса https://fallbacksaas.io/?utm_source=tg — пишите @fallback_ceo #реклама",
                )
            ]

        async def __call__(self, request):
            uid = str(getattr(request, "id", "")).lstrip("@")
            return SimpleNamespace(
                users=[SimpleNamespace(id=902, username=uid, first_name="Max", last_name="Founder")],
                full_user=SimpleNamespace(about="Founder & CEO"),
            )

    def fake_public_posts(ch: str, limit: int = 30) -> list[str]:
        if ch == "startupoftheday":
            return posts_html
        return []  # empty -> forces fallback to client.get_messages!

    rows = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=FakeSpyClient(),
            channels=["startupoftheday", "fallback_chan"],
            msg_limit=15,
            seen_path=str(tmp_path / "seen.json"),
            output_path=str(tmp_path / "out.jsonl"),
            delay=0.0,
            fetch_html=None,
            public_posts_fn=fake_public_posts,
        )
    )
    assert get_messages_called_for == ["fallback_chan"]
    by_user = {r["username"]: r for r in rows}
    assert "sellerclips_ceo" in by_user
    assert by_user["sellerclips_ceo"]["site"] == "https://sellerclips.ai"
    assert "fallback_ceo" in by_user
    assert by_user["fallback_ceo"]["site"] == "https://fallbacksaas.io"


def test_tg_identity_bridge_accepts_x_bio_domain_search_and_verified_x_handle():
    from leadgen import tg_identity_bridge

    tme_pages = {
        "rea_personal": _personal_tme_html("rea_personal", "Liam", "Building cool things"),
        "sarah_astro": _personal_tme_html("sarah_astro", "Sarah", "Product & growth"),
        "dan_pulse": _personal_tme_html("dan_pulse", "Dan", "Founder @ Pulseboard.io | B2B Analytics"),
        "elenavance": _personal_tme_html("elenavance", "Elena Vance", "San Francisco"),
        "skin_ceo": _personal_tme_html("skin_ceo", "Alex Rivera", "Founder of SkinGlow"),
    }

    # 1) Explicit t.me/<nick> in founder's own X bio
    row_x_bio = {
        "name": "ReaVideo",
        "project": "ReaVideo",
        "project_url": "https://reavideo.ai",
        "founder_x": "https://x.com/reavideo_ceo",
        "contact_channels": ["x"],
        "links": ["https://reavideo.ai", "https://x.com/reavideo_ceo"],
    }
    res1 = tg_identity_bridge.bridge_lead_to_telegram(
        row_x_bio,
        x_profile_fn=lambda h: {"bio": "Co-founder @ReaVideo | DM me on TG: https://t.me/rea_personal"},
        search_fn=lambda q: None,
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )
    assert res1["username"] == "rea_personal"
    assert res1["telegram_username"] == "rea_personal"
    assert res1["tg_username"] == "rea_personal"
    assert res1["telegram_url"] == "https://t.me/rea_personal"
    assert "telegram" in res1["contact_channels"]
    assert res1["tg_bridge_match"] == "x_bio_explicit"

    # 2) Domain search snippet with exact domain match (ignoring /s/, bots, joinchat, share)
    row_dom = {
        "name": "AstroPulse",
        "company": "AstroPulse",
        "project_url": "https://astropulse.app",
        "contact_channels": ["site"],
    }
    search_queries: list[str] = []

    def fake_search_astro(q: str):
        search_queries.append(q)
        return {
            "results": [
                {
                    "url": "https://t.me/s/startupoftheday/991",
                    "snippet": "Check https://t.me/joinchat/AAAA and https://t.me/share/url?url=astropulse.app or @astro_support_bot",
                },
                {
                    "url": "https://t.me/sarah_astro",
                    "title": "Sarah — AstroPulse",
                    "snippet": "Founder of astropulse.app — AI astrology subscription app. Write https://t.me/sarah_astro",
                },
            ]
        }

    res2 = tg_identity_bridge.bridge_lead_to_telegram(
        row_dom,
        x_profile_fn=lambda h: None,
        search_fn=fake_search_astro,
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )
    assert len(search_queries) == 1
    assert "site:t.me" in search_queries[0]
    assert '"astropulse.app"' in search_queries[0]
    assert res2["username"] == "sarah_astro"
    assert res2["telegram_url"] == "https://t.me/sarah_astro"
    assert res2["tg_bridge_match"] == "domain_search_explicit"

    # 3a) Same-handle @<x_handle> verified by brand/domain token (>= 4 chars) in TG bio
    row_x_brand = {
        "name": "Dan",
        "company": "Pulseboard",
        "project_url": "https://pulseboard.io",
        "founder_x": "https://x.com/dan_pulse",
    }
    res3a = tg_identity_bridge.bridge_lead_to_telegram(
        row_x_brand,
        x_profile_fn=lambda h: {"bio": "Founder building B2B dashboards"},
        search_fn=lambda q: {"results": []},
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )
    assert res3a["username"] == "dan_pulse"
    assert res3a["tg_bridge_match"] == "x_handle_verified"

    # 3b) Same-handle @<x_handle> verified by founder's full first+last name (2+ words) in TG title
    row_x_fullname = {
        "name": "Elena Vance",
        "company": "StealthCo",
        "project_url": "https://stealthco.xyz",
        "founder_twitter": "@elenavance",
    }
    res3b = tg_identity_bridge.bridge_lead_to_telegram(
        row_x_fullname,
        x_profile_fn=lambda h: {"bio": "Building next-gen consumer AI"},
        search_fn=lambda q: {"results": []},
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )
    assert res3b["username"] == "elenavance"
    assert res3b["tg_bridge_match"] == "x_handle_verified"

    # 4) Existing valid personal TG handle is skipped with 0 external calls; bot username IS bridged
    already_tg = {"name": "Astro Founder", "username": "@existing_founder", "project_url": "https://astro.io"}
    res_skip = tg_identity_bridge.bridge_lead_to_telegram(
        already_tg,
        x_profile_fn=lambda h: (_ for _ in ()).throw(AssertionError("Should skip existing TG row")),
        search_fn=lambda q: (_ for _ in ()).throw(AssertionError("Should skip existing TG row")),
        tme_fetch_fn=lambda n: (_ for _ in ()).throw(AssertionError("Should skip existing TG row")),
    )
    assert res_skip["username"] == "@existing_founder"
    assert "tg_bridge_match" not in res_skip

    bot_row = {
        "name": "Alex Rivera",
        "username": "@skinglow_support_bot",
        "company": "SkinGlow",
        "project_url": "https://skinglow.shop",
        "founder_x": "https://x.com/skin_ceo",
    }
    res_bot_replaced = tg_identity_bridge.bridge_lead_to_telegram(
        bot_row,
        x_profile_fn=lambda h: {"bio": "Founder @ SkinGlow"},
        search_fn=lambda q: {"results": []},
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )
    assert res_bot_replaced["username"] == "skin_ceo"
    assert res_bot_replaced["tg_bridge_match"] == "x_handle_verified"


def test_tg_identity_bridge_strictly_rejects_channels_bots_groups_redirects_and_namesakes():
    from leadgen import tg_identity_bridge

    bad_tme_pages = {
        "metricflow_news": _channel_tme_html("metricflow_news", "MetricFlow Official", "18 900 subscribers"),
        "metricflow_chat": _group_tme_html("metricflow_chat", "MetricFlow Community"),
        "metricflow_helper": _bot_tme_html("metricflow_helper", "MetricFlow Helper"),
        "redir_user": SimpleNamespace(status_code=302, text="", headers={"Location": "https://telegram.org"}, is_redirect=True),
        "missing_user": SimpleNamespace(status_code=404, text="Not Found", headers={}, is_redirect=False),
        "alex": _personal_tme_html("alex", "Alex", "Living in Berlin, love coffee & photography"),
        "random_mike": _personal_tme_html("random_mike", "Mike", "Video creator & football fan"),
    }

    # 1) Explicit X bio / domain search pointing to channel, group, bot, redirect, or 404 -> ALL rejected!
    row_channel_trap = {
        "name": "Alex Smith",
        "company": "MetricFlow",
        "project_url": "https://metricflow.io",
        "founder_x": "https://x.com/alex",
    }
    res_trap = tg_identity_bridge.bridge_lead_to_telegram(
        row_channel_trap,
        x_profile_fn=lambda h: {
            "bio": "News: t.me/metricflow_news | Chat: t.me/metricflow_chat | Help: t.me/metricflow_helper"
        },
        search_fn=lambda q: {
            "results": [
                {"url": "https://t.me/redir_user", "snippet": "metricflow.io founder t.me/redir_user"},
                {"url": "https://t.me/missing_user", "snippet": "metricflow.io ceo t.me/missing_user"},
            ]
        },
        tme_fetch_fn=lambda nick: bad_tme_pages.get(nick, ""),
    )
    # Note: @alex also fails anti-namesake because t.me/alex only has "Alex" (missing "Smith" and missing "MetricFlow")!
    assert not res_trap.get("username")
    assert "tg_bridge_match" not in res_trap

    # 2) Single-word founder name "Alex" + @alex on TG with title "Alex" -> REJECTED (prevents @alex namesake!)
    row_single_name = {
        "name": "Alex",
        "company": "MetricFlow",
        "project_url": "https://metricflow.io",
        "founder_x": "https://x.com/alex",
    }
    res_single = tg_identity_bridge.bridge_lead_to_telegram(
        row_single_name,
        x_profile_fn=lambda h: {"bio": "SaaS builder"},
        search_fn=lambda q: {"results": []},
        tme_fetch_fn=lambda nick: bad_tme_pages.get(nick, ""),
    )
    assert not res_single.get("username")

    # 3) Brand-only search snippet WITHOUT exact domain + generic word "Video" in stranger's bio -> REJECTED!
    row_brand_only = {
        "name": "Mike",
        "company": "Vivid Video Studio",
        "project_url": "https://vividreel.ai",
    }
    res_brand_only = tg_identity_bridge.bridge_lead_to_telegram(
        row_brand_only,
        x_profile_fn=lambda h: None,
        search_fn=lambda q: {
            "results": [
                {
                    "url": "https://t.me/random_mike",
                    "snippet": "Vivid Video Studio discussion with t.me/random_mike",  # NO "vividreel.ai"!
                }
            ]
        },
        tme_fetch_fn=lambda nick: bad_tme_pages.get(nick, ""),
    )
    assert not res_brand_only.get("username")


def test_tg_identity_bridge_batch_and_cli_respects_max_rows(monkeypatch, tmp_path):
    from leadgen import tg_identity_bridge

    pages = {
        "founder_one": _personal_tme_html("founder_one", "Founder One", "CEO @ AlphaDeck.io"),
        "founder_two": _personal_tme_html("founder_two", "Founder Two", "CEO @ BetaPulse.io"),
        "founder_three": _personal_tme_html("founder_three", "Founder Three", "CEO @ GammaLoop.io"),
    }

    input_rows = [
        {"name": "Already TG", "username": "already_has_tg", "project_url": "https://zero.io"},
        {"name": "AlphaDeck", "company": "AlphaDeck", "project_url": "https://alphadeck.io", "founder_x": "@founder_one"},
        {"name": "BetaPulse", "company": "BetaPulse", "project_url": "https://betapulse.io", "founder_x": "@founder_two"},
        {"name": "GammaLoop", "company": "GammaLoop", "project_url": "https://gammaloop.io", "founder_x": "@founder_three"},
    ]

    enriched = tg_identity_bridge.enrich_rows_with_telegram_bridge(
        input_rows,
        max_rows=2,
        x_profile_fn=lambda h: {},
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: pages.get(nick, ""),
    )
    assert len(enriched) == 4
    assert enriched[0]["username"] == "already_has_tg"
    assert enriched[1]["username"] == "founder_one"
    assert enriched[2]["username"] == "founder_two"
    # 3rd non-TG row exceeds max_rows=2 -> untouched
    assert not enriched[3].get("username")

    # Test CLI (--input, --output, --max-rows)
    inp_file = tmp_path / "in.jsonl"
    out_file = tmp_path / "out.jsonl"
    inp_file.write_text("\n".join(json.dumps(r) for r in input_rows) + "\n", encoding="utf-8")

    monkeypatch.setattr(tg_identity_bridge, "_default_x_profile", lambda h: {})
    monkeypatch.setattr(tg_identity_bridge, "_default_search", lambda q: {})
    monkeypatch.setattr(tg_identity_bridge, "_default_tme_fetch", lambda nick: pages.get(nick, ""))
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg_identity_bridge.py", "--input", str(inp_file), "--output", str(out_file), "--max-rows", "2"],
    )
    tg_identity_bridge.main()

    written = [json.loads(ln) for ln in out_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(written) == 4
    assert written[1]["username"] == "founder_one"
    assert written[2]["username"] == "founder_two"
    assert not written[3].get("username")


def test_identity_bridge_scrapecreators_legacy_and_adspy_rows_key():
    from leadgen import tg_ad_spy, tg_identity_bridge

    # 1) ScrapeCreators x.user.profile nests bio & expanded_url under profile["legacy"]
    sc_profile = {
        "success": True,
        "__typename": "User",
        "legacy": {
            "description": "Building AlphaDeck AI",
            "entities": {
                "url": {
                    "urls": [
                        {"url": "https://t.co/xyz", "expanded_url": "https://t.me/alphadeck_ceo"}
                    ]
                }
            },
        },
    }
    blob = tg_identity_bridge._extract_x_bio_blob(sc_profile)
    assert "Building AlphaDeck AI" in blob
    assert "https://t.me/alphadeck_ceo" in blob

    resolved = tg_identity_bridge.bridge_lead_to_telegram(
        {"name": "AlphaDeck", "company": "AlphaDeck", "project_url": "https://alphadeck.io", "founder_x": "@alphadeck_x"},
        x_profile_fn=lambda h: sc_profile,
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: _personal_tme_html("alphadeck_ceo", "Max", "CEO at AlphaDeck.io")
        if nick == "alphadeck_ceo"
        else "",
    )
    assert resolved["username"] == "alphadeck_ceo"
    assert resolved["tg_bridge_match"] == "x_bio_explicit"

    # 2) crawl4ai.telegram.channel.posts returns {"rows": [{"text": "..."}]}
    posts = tg_ad_spy.fetch_public_channel_posts_http(
        "test_channel",
        limit=5,
        monid_fn=lambda c, l: None,
        treg_fn=lambda c, l: {"rows": [{"id": 1, "text": "Sponsored: Try @demo_app_tg now"}]},
        http_get_fn=lambda u: "",
    )
    assert posts == ["Sponsored: Try @demo_app_tg now"]
