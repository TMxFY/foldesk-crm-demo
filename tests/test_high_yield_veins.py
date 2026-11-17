from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from leadgen import (
    buyer_catalogs,
    buyer_public,
    creator_external_leads,
    high_yield_veins,
    monid_client,
)


def test_monid_client_binary_and_run_monid_unwraps_output_and_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("MONID_ENRICH", "0")
    assert monid_client.check_monid_ready(binary_fn=lambda: "/usr/local/bin/monid") is False
    assert monid_client.run_monid("litescrape", "/google-play/product", binary_fn=lambda: "/bin/echo") is None

    monkeypatch.setenv("MONID_ENRICH", "1")
    monkeypatch.delenv("MONID_API_KEY", raising=False)
    monkeypatch.setenv("MONID_CONFIG_PATH", str(tmp_path / "missing_monid_creds.yaml"))
    assert monid_client.check_monid_ready(binary_fn=lambda: None) is False
    assert monid_client.check_monid_ready(binary_fn=lambda: "/usr/local/bin/monid") is False

    creds = tmp_path / "credentials.yaml"
    creds.write_text("apiKey: test-monid-token\n", encoding="utf-8")
    monkeypatch.setenv("MONID_CONFIG_PATH", str(creds))
    assert monid_client.check_monid_ready(binary_fn=lambda: "/usr/local/bin/monid") is True

    calls: list[list[str]] = []

    def fake_ok_run(cmd, capture_output=True, text=True, timeout=35):
        calls.append(cmd)
        payload = {
            "status": "COMPLETED",
            "output": {"developer_contact": {"website": "https://tarotai.app"}},
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(monid_client.subprocess, "run", fake_ok_run)
    res = monid_client.run_monid(
        "litescrape",
        "/google-play/product",
        query={"product_id": "horoscope.tarot.ai"},
        binary_fn=lambda: "/usr/local/bin/monid",
    )
    assert res == {"developer_contact": {"website": "https://tarotai.app"}}
    assert "--query" in calls[0]

    # HTTP 422 / provider error -> returns None
    monkeypatch.setattr(
        monid_client.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "COMPLETED", "providerResponse": {"httpStatus": 422}, "output": None}),
            stderr="",
        ),
    )
    assert (
        monid_client.run_monid(
            "tikhub",
            "/api/v1/tiktok/shop/web/fetch_search_products_list",
            binary_fn=lambda: "/usr/local/bin/monid",
        )
        is None
    )


def test_resolve_app_store_developer_ios_play_and_megacorp_filter():
    # 1) Apple App Store indie subscription app -> resolved cleanly
    ios_res = monid_client.resolve_app_store_developer(
        "https://apps.apple.com/us/app/ai-palm-astrology/id6479991111",
        http_get_fn=lambda url: SimpleNamespace(
            json=lambda: {
                "resultCount": 1,
                "results": [
                    {
                        "sellerName": "LunaChart Studio",
                        "trackName": "AI Palm & Tarot",
                        "sellerUrl": "https://lunachart.app",
                        "userRatingCount": 3200,
                    }
                ],
            }
        ),
    )
    assert ios_res["developer"] == "LunaChart Studio"
    assert ios_res["website"] == "https://lunachart.app"
    assert ios_res["domain"] == "lunachart.app"
    assert ios_res["rating_count"] == 3200
    assert ios_res["is_megacorp"] is False

    # 2) Google Play resolved via monid -> search fallback for website when developer_contact.website is empty
    play_res = monid_client.resolve_app_store_developer(
        "https://play.google.com/store/apps/details?id=horoscope.tarot.ai",
        monid_fn=lambda p, e, query=None: {
            "product_info": {"title": "Astrology: Palm Reader & Tarot"},
            "about_this_app": {"offered_by": "Kienmaly Mobi"},
            "developer_contact": {"support_email": "hello@kienmalymobi.com", "website": ""},
            "ratings": {"total_count": 4800},
        },
        search_fn=lambda q: {
            "results": [
                {"url": "https://play.google.com/store/apps/dev?id=123"},
                {"url": "https://kienmalymobi.com/tarot"},
            ]
        },
    )
    assert play_res["developer"] == "Kienmaly Mobi"
    assert play_res["email"] == "hello@kienmalymobi.com"
    assert play_res["website"] == "https://kienmalymobi.com/tarot"
    assert play_res["domain"] == "kienmalymobi.com"
    assert play_res["is_megacorp"] is False

    # 3) Megacorp rejection by rating_count > 50k and by brand name
    mega_by_rating = monid_client.resolve_app_store_developer(
        "https://apps.apple.com/us/app/huge-utility/id12345",
        http_get_fn=lambda url: SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "sellerName": "IndieCo",
                        "trackName": "Huge Utility",
                        "sellerUrl": "https://hugeutil.com",
                        "userRatingCount": 75000,
                    }
                ]
            }
        ),
    )
    assert mega_by_rating["is_megacorp"] is True

    mega_by_brand = monid_client.resolve_app_store_developer(
        "https://apps.apple.com/us/app/chatgpt/id6448311069",
        http_get_fn=lambda url: SimpleNamespace(
            json=lambda: {
                "results": [
                    {
                        "sellerName": "OpenAI OpCo, LLC",
                        "trackName": "ChatGPT",
                        "sellerUrl": "https://openai.com",
                        "userRatingCount": 12000,
                    }
                ]
            }
        ),
    )
    assert mega_by_brand["is_megacorp"] is True

    # 4) Apple App Store monid fallback with dict developer payload
    ios_dict_dev = monid_client.resolve_app_store_developer(
        "https://apps.apple.com/us/app/indie-habit/id6501112233",
        http_get_fn=lambda url: SimpleNamespace(json=lambda: {"results": []}),
        monid_fn=lambda p, e, query=None: {
            "product_info": {
                "title": "Indie Habit",
                "developer": {
                    "name": "Indie Habit LLC",
                    "url": "https://apps.apple.com/us/developer/id999",
                },
                "sellerUrl": "https://indiehabit.app",
                "rating_count": 410,
            }
        },
    )
    assert ios_dict_dev["developer"] == "Indie Habit LLC"
    assert ios_dict_dev["website"] == "https://indiehabit.app"
    assert ios_dict_dev["domain"] == "indiehabit.app"
    assert ios_dict_dev["is_megacorp"] is False

    # 5) litescrape string rating_count ("5.5M Ratings") & list ratings does not crash or bypass megacorp
    ios_compact_rc = monid_client.resolve_app_store_developer(
        "https://apps.apple.com/us/app/mega-social/id6509998888",
        http_get_fn=lambda url: SimpleNamespace(json=lambda: {"results": []}),
        monid_fn=lambda p, e, query=None: {
            "product_info": {
                "title": "Mega Social",
                "developer": "Mega Corp Studio",
                "sellerUrl": "https://megasocial.example.com",
                "rating_count": "5.5M Ratings",
            },
            "ratings": [{"star": 5}],
        },
    )
    assert ios_compact_rc["rating_count"] == 5500000
    assert ios_compact_rc["is_megacorp"] is True


def test_tiktok_api_request_contracts_and_nested_ads_info(monkeypatch):
    from app.hygiene import _normalize_direct_x_record
    from leadgen import treg_enrich

    # 1) _default_tiktok_ads_search uses POST body= with int period & limit
    captured_monid: list[dict] = []
    monkeypatch.setattr(high_yield_veins, "check_monid_ready", lambda: True)
    monkeypatch.setattr(
        high_yield_veins,
        "run_monid",
        lambda p, e, query=None, body=None, wait_sec=25, timeout=35: (
            captured_monid.append({"p": p, "e": e, "query": query, "body": body})
            or {"ok": True}
        ),
    )
    assert high_yield_veins._default_tiktok_ads_search("ai tarot") == {"ok": True}
    assert captured_monid == [
        {
            "p": "tikhub",
            "e": "/api/v1/tiktok/ads/search_ads",
            "query": None,
            "body": {"keyword": "ai tarot", "country_code": "US", "period": 30, "limit": 15},
        }
    ]

    # 2) _default_tiktok_shop_search + scrapecreators ad-library Treg fallback contracts
    captured_treg: list[dict] = []
    monkeypatch.setattr(high_yield_veins, "check_monid_ready", lambda: False)
    monkeypatch.setattr(high_yield_veins, "check_treg_ready", lambda: True)
    monkeypatch.setattr(
        high_yield_veins,
        "treg_call",
        lambda ep, query=None, method=None, data=None, timeout=40: (
            captured_treg.append({"ep": ep, "query": query})
            or (None if "tikhub" in ep else ({"ads": []} if "ad-library" in ep else {"products": []}))
        ),
    )
    high_yield_veins._default_tiktok_shop_search("supplements")
    high_yield_veins._default_tiktok_ads_search("skincare")
    assert captured_treg[0] == {
        "ep": "scrapecreators.x.v1-tiktok-shop-search",
        "query": {"query": "supplements", "region": "US"},
    }
    assert {
        "ep": "scrapecreators.x.v1-tiktok-ad-library-search",
        "query": {"query": "skincare"},
    } in captured_treg

    # 3) Nested ads_info + desc + Play Store without website does NOT shadow dm['project_url']
    scraped_urls: list[str] = []
    rows, _ = high_yield_veins.collect_tiktok_creative_ads(
        limit=3,
        day=date(2026, 11, 17),
        seen={},
        ads_search_fn=lambda kw: {
            "data": {
                "list": [
                    {
                        "desc": "AI habit coach app for daily creators",
                        "ads_info": {
                            "advertiser_name": "NestedHabit",
                            "landing_page": "https://play.google.com/store/apps/details?id=com.nested.habit",
                        },
                    }
                ]
            }
        }
        if kw == high_yield_veins.TIKTOK_AD_QUERIES[0]
        else {"data": {"list": []}},
        store_resolver_fn=lambda u: {
            "developer": "Nested Studio",
            "website": "",
            "domain": "",
            "rating_count": 300,
            "is_megacorp": False,
        },
        enrich_fn=lambda c, d: {"project_url": "https://nestedhabit.ai", "work_email": "hi@nestedhabit.ai"},
        site_scrape_fn=lambda u: (scraped_urls.append(u) or {"username": "nested_ceo"}),
    )
    assert len(rows) == 1
    assert scraped_urls == ["https://nestedhabit.ai"]
    assert rows[0]["project_url"] == "https://nestedhabit.ai"
    assert "https://play.google.com/store/apps/details?id=com.nested.habit" in rows[0]["links"]
    assert rows[0]["username"] == "nested_ceo"

    # 4) ScrapeCreators ad-library shape: item["name"] + library.tiktok.com url does NOT shadow dm["project_url"]
    sc_scraped: list[str] = []
    sc_rows, _ = high_yield_veins.collect_tiktok_creative_ads(
        limit=3,
        day=date(2026, 11, 17),
        seen={},
        ads_search_fn=lambda kw: {
            "ads": [
                {
                    "id": "ad777",
                    "name": "AuraSkin Labs",
                    "url": "https://library.tiktok.com/ads/detail/?ad_id=777",
                    "title": "UGC glow serum campaign",
                }
            ]
        }
        if kw == high_yield_veins.TIKTOK_AD_QUERIES[0]
        else {"ads": []},
        enrich_fn=lambda c, d: {"project_url": "https://auraskinlabs.com", "work_email": "growth@auraskinlabs.com"},
        site_scrape_fn=lambda u: (sc_scraped.append(u) or {"founder_x": "https://x.com/auraskinlabs"}),
    )
    assert len(sc_rows) == 1
    assert sc_rows[0]["name"] == "AuraSkin Labs"
    assert sc_scraped == ["https://auraskinlabs.com"]
    assert sc_rows[0]["project_url"] == "https://auraskinlabs.com"
    assert "https://library.tiktok.com/ads/detail/?ad_id=777" in sc_rows[0]["links"]

    # 5) _extract_shop_brand reads seller_info["shop_name"] despite leading-noise product titles
    assert (
        high_yield_veins._extract_shop_brand(
            {
                "title": "Organic Mushroom Gummies",
                "seller_info": {"seller_id": "s1", "shop_name": "MicroIngredients"},
            }
        )
        == "MicroIngredients"
    )

    # 6) Scheme hardening against javascript:/data: XSS across backend & renderLeadgenHarvest
    assert treg_enrich.domain_of("javascript://evil.com/%0aalert(1)") == ""
    assert treg_enrich.domain_of("javascript:evil.com") == ""
    sanitized_dx = _normalize_direct_x_record(
        {
            "company": "BadCo",
            "url": "javascript://evil.com/%0aalert(1)",
            "project_url": "javascript:alert(1)",
            "x_url": "javascript:alert(2)",
            "linkedin_url": "data:text/html,<script>",
        }
    )
    assert sanitized_dx["url"] is None
    assert sanitized_dx["project_url"] is None
    assert sanitized_dx["x_url"] is None
    assert sanitized_dx["founder_x"] is None
    assert sanitized_dx["linkedin_url"] is None

    app_js = (WORKTREE_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    harvest_start = app_js.index("function renderLeadgenHarvest")
    harvest_block = app_js[harvest_start : harvest_start + 5000]
    assert "safeHref(item.url)" in harvest_block
    assert "safeHref(contactLink)" in harvest_block

    # 7) collect_tiktok_shop_brands does NOT let tiktok.com/javascript:// shadow dm["project_url"]
    shop_scraped: list[str] = []
    shop_clean_rows, _ = high_yield_veins.collect_tiktok_shop_brands(
        limit=3,
        day=date(2026, 11, 17),
        seen={},
        shop_search_fn=lambda kw: {
            "products": [
                {
                    "title": "Sleep Magnesium Gummies",
                    "seller_info": {"shop_name": "VibeRest"},
                    "website": "https://www.tiktok.com/@viberest_shop",
                },
                {
                    "title": "XSS Cream",
                    "seller_info": {"shop_name": "EvilShop"},
                    "website": "javascript://evil.com/%0aalert(1)",
                },
            ]
        }
        if kw == high_yield_veins.TIKTOK_SHOP_QUERIES[0]
        else {"products": []},
        enrich_fn=lambda comp, dom: (
            {"project_url": "https://viberest.co", "work_email": "hi@viberest.co"}
            if comp == "VibeRest"
            else {}
        ),
        site_scrape_fn=lambda u: (shop_scraped.append(u) or {"founder_x": "https://x.com/viberest"}),
    )
    assert len(shop_clean_rows) == 1
    assert shop_clean_rows[0]["project"] == "VibeRest"
    assert shop_clean_rows[0]["project_url"] == "https://viberest.co"
    assert shop_scraped == ["https://viberest.co"]

    # 8) Prod apply_sniper_icp_cleanup_v7 (tg_groups=False + buyer sources True) keeps new veins ENABLED
    import sqlite3
    from app.db import init_db, apply_sniper_icp_cleanup_v7
    from app.leadgen_settings import get_leadgen_config, save_leadgen_config

    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    init_db(mem)
    mem.execute("DELETE FROM kv_settings WHERE key = 'sniper_icp_cleanup_v7'")
    save_leadgen_config(
        mem,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "tg_groups": True,
                    "meta_ads": True,
                    "social_x_reddit": True,
                    "buyer_catalogs": True,
                    "yc_ph": True,
                },
            },
        },
    )
    apply_sniper_icp_cleanup_v7(mem, force=True)
    prod_cfg = get_leadgen_config(mem)
    assert prod_cfg["b2b"]["sources"]["tg_groups"] is False
    for vk in ("whop_content_rewards", "tiktok_ads", "tiktok_shop", "tg_identity_bridge"):
        assert prod_cfg["b2b"]["sources"][vk] is True, f"{vk} silently disabled by v7 cleanup!"
    mem.close()


def test_high_yield_veins_whop_tiktok_ads_and_shop_with_dedup_and_budget_cap(tmp_path):
    # 1) Whop Content Rewards
    def fake_whop_search(q: str):
        return {
            "results": [
                {
                    "url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/discover/fffe2df2",
                    "title": "Earn $1 per 1000 Views Clipping Fexr by Plox Media",
                    "snippet": "Budget: $3,056. CPM: $1 per 1,000 views. DM https://t.me/fexr_founder",
                },
                {
                    "url": "https://whop.com/blog/what-is-content-clipping/",
                    "title": "What is clipping? The ultimate guide",
                    "snippet": "Budget: $1,000 CPM: $1 per 1,000 views",
                },
                {
                    "url": "https://whop.com/clip-hub-agency/",
                    "title": "Clipper Agency Hub",
                    "snippet": "We are a clipping agency helping brands grow. CPM: $2 per 1,000 views",
                },
            ]
        }

    whop_rows, seen_after_whop = high_yield_veins.collect_whop_content_rewards(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        search_fn=fake_whop_search,
        enrich_fn=lambda comp, dom: {
            "project_url": "https://fexr.io",
            "founder_linkedin": "https://www.linkedin.com/in/plox-ceo",
            "work_email": "team@fexr.io",
        },
        site_scrape_fn=lambda u: {"founder_x": "https://x.com/fexrapp"},
    )
    assert len(whop_rows) == 1
    w0 = whop_rows[0]
    assert w0["source"] == "whop_content_rewards"
    assert w0["project"] == "Fexr"
    assert w0["username"] == "fexr_founder"
    assert w0["founder_x"] == "https://x.com/fexrapp"
    assert "$3,056 budget" in w0["what_they_do"]
    assert w0["has_monetization_signal"] is True

    # Re-run on same day -> deduped!
    whop_repeat, _ = high_yield_veins.collect_whop_content_rewards(
        limit=5,
        day=date(2026, 11, 17),
        seen=seen_after_whop,
        search_fn=fake_whop_search,
        enrich_fn=lambda c, d: {},
        site_scrape_fn=lambda u: {},
    )
    assert whop_repeat == []

    # 2) TikTok Creative Center Ads with App Store developer bridge & megacorp skip
    def fake_tiktok_ads(kw: str):
        if kw == high_yield_veins.TIKTOK_AD_QUERIES[0] or "AI" in kw or "astrology" in kw:
            return {
                "data": {
                    "materials": [
                        {
                            "brand_name": "OpenAI",
                            "ad_title": "ChatGPT App",
                            "landing_page": "https://openai.com",
                        },
                        {
                            "brand_name": "LunaTarot",
                            "ad_title": "AI Birth Chart & Palm Reader",
                            "landing_page": "https://play.google.com/store/apps/details?id=com.lunatarot.ai",
                        },
                    ]
                }
            }
        return {"data": {"materials": []}}

    tt_rows, _ = high_yield_veins.collect_tiktok_creative_ads(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        ads_search_fn=fake_tiktok_ads,
        store_resolver_fn=lambda u: {
            "developer": "LunaTarot Labs",
            "website": "https://lunatarot.app",
            "domain": "lunatarot.app",
            "email": "hi@lunatarot.app",
            "rating_count": 2100,
            "is_megacorp": False,
        },
        enrich_fn=lambda c, d: {"founder_linkedin": "https://www.linkedin.com/in/luna-founder"},
        site_scrape_fn=lambda u: {"username": "luna_founder"},
    )
    assert len(tt_rows) == 1
    assert tt_rows[0]["source"] == "tiktok_ads"
    assert tt_rows[0]["project_url"] == "https://lunatarot.app"
    assert tt_rows[0]["work_email"] == "hi@lunatarot.app"
    assert tt_rows[0]["username"] == "luna_founder"

    # 3) TikTok Shop US Sellers
    def fake_shop_search(kw: str):
        return {
            "products": [
                {"title": "POWERBIRTH 5mg Melatonin Sleep Gummies", "seller_name": "POWERBIRTH"},
                {"title": "NoContact Brand Gummies", "seller_name": "NoContactShop"},
            ]
        }

    shop_rows, _ = high_yield_veins.collect_tiktok_shop_brands(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        shop_search_fn=fake_shop_search,
        enrich_fn=lambda comp, dom: (
            {"project_url": "https://powerbirth.com", "work_email": "partners@powerbirth.com"}
            if "POWERBIRTH" in comp
            else {}
        ),
        site_scrape_fn=lambda u: {"founder_x": "https://x.com/powerbirth_co"},
    )
    assert len(shop_rows) == 1
    assert shop_rows[0]["source"] == "tiktok_shop"
    assert shop_rows[0]["project"] == "POWERBIRTH"
    assert shop_rows[0]["project_url"] == "https://powerbirth.com"
    assert shop_rows[0]["category"] == "SaaS / AI app"

    # 4) Budget cap stops cleanly at 0
    budget = [0]
    capped_rows, _ = high_yield_veins.collect_whop_content_rewards(
        limit=5,
        search_fn=lambda q: (_ for _ in ()).throw(AssertionError("Budget=0 must prevent calls")),
        call_budget=budget,
    )
    assert capped_rows == []


def test_buyer_catalogs_and_creator_external_leads_cleanup_and_megacorp_filter(monkeypatch):
    source_names = [name for name, _ in buyer_catalogs.SOURCES]
    assert "fivem" not in source_names
    assert "gta" not in source_names
    assert "groover" in source_names
    assert "gumroad" in source_names

    # Direct parse_catalog("fivem"/"gta", ...) remains backward-compatible
    gta_html = '<ol><li><a href="https://coolrp.gg">Cool RP</a></li></ol>'
    assert len(buyer_catalogs.parse_catalog("gta", gta_html, "https://6charts.com/gta-6-servers")) == 1

    # creator_external_leads.app_stores filters megacorps (OpenAI / >25k reviews)
    itunes_sample = {
        "results": [
            {
                "trackName": "ChatGPT",
                "artistName": "OpenAI",
                "sellerUrl": "https://openai.com",
                "trackViewUrl": "https://apps.apple.com/us/app/chatgpt/id6448311069",
                "userRatingCount": 800000,
                "description": "Official AI assistant",
            },
            {
                "trackName": "Giant Calorie Counter",
                "artistName": "IndieHuge Inc",
                "sellerUrl": "https://hugecal.com",
                "trackViewUrl": "https://apps.apple.com/us/app/hugecal/id2222",
                "userRatingCount": 30000,
                "description": "Calorie counter",
            },
            {
                "trackName": "AstraCoach AI",
                "artistName": "AstraCoach Labs",
                "sellerUrl": "https://astracoach.app",
                "trackViewUrl": "https://apps.apple.com/us/app/astracoach/id3333",
                "userRatingCount": 1400,
                "description": "Personalized AI fitness & astrology coaching subscription",
            },
        ]
    }
    monkeypatch.setattr(
        creator_external_leads,
        "_get_json",
        lambda url, params=None: itunes_sample if "itunes.apple.com" in url else {},
    )
    monkeypatch.setattr(creator_external_leads, "_get_text", lambda *a, **kw: "")

    apps = creator_external_leads.app_stores(limit=5)
    app_names = {r["project"] for r in apps}
    assert "ChatGPT" not in app_names
    assert "Giant Calorie Counter" not in app_names
    assert "AstraCoach AI" in app_names

    # buyer_public preserves custom fetch_fn behavior without external search
    assert buyer_public.whop_rows(fetch_fn=lambda url: "<html>no contacts</html>") == []


def test_verify_sh_gate_g_rejects_dead_fivem_gta_and_missing_veins(tmp_path):
    import subprocess

    sandbox = tmp_path / "gate_g_repo"
    for sub in ("scripts", "app/static", "leadgen", "tests", "deploy/systemd"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for link_dir in (".claude", "replica"):
        (sandbox / link_dir).symlink_to(WORKTREE_ROOT / link_dir)
    if (WORKTREE_ROOT / ".venv").exists():
        (sandbox / ".venv").symlink_to((WORKTREE_ROOT / ".venv").resolve())

    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text((WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8"), encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "app" / "static" / "app.js").write_text("const ok = 1;\n", encoding="utf-8")
    (sandbox / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "leadgen" / "meta_ad_leads.py").write_text("CAP = 10\n", encoding="utf-8")
    (sandbox / "leadgen" / "buyer_catalogs.py").write_text(
        'SOURCES = (\n    ("fivem", "https://fivem-server.org/servers/english"),\n)\n',
        encoding="utf-8",
    )
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import argparse\ndef run_autonomous_replenish(conn): return {}\nif __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "clean.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )

    bad = subprocess.run([str(sb_verify), "--fast-checks-only"], cwd=str(sandbox), capture_output=True, text=True)
    assert bad.returncode == 1
    assert "Gate G violation" in bad.stderr
    assert "fivem/gta" in bad.stderr
    assert "high_yield_veins.py" in bad.stderr


def test_real_play_head_fonts_link_x_intent_skip_and_tme_s_newest_posts(monkeypatch):
    from leadgen import meta_ad_leads, tg_ad_spy, treg_enrich

    # 1) Play HTML with <head><link href="https://fonts.googleapis.com"> must extract real <a href> site, not font CDN
    monkeypatch.setitem(sys.modules, "leadgen.monid_client", None)
    monkeypatch.setitem(sys.modules, "monid_client", None)
    import requests

    play_html = (
        '<html><head>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">'
        '<script type="application/ld+json">'
        '{"@type":"SoftwareApplication","author":{"@type":"Organization","name":"AstraLabs"},'
        '"aggregateRating":{"ratingCount":"1200"}}'
        '</script></head><body>'
        '<a href="https://www.google.com/url?q=https%3A%2F%2Fastralabs.ai&amp;sa=D">Website</a>'
        '<a href="mailto:founders@astralabs.ai">founders@astralabs.ai</a>'
        '</body></html>'
    )
    site_html = (
        '<html><body>'
        '<a href="https://x.com/intent/tweet?text=hi">Share</a>'
        '<a href="https://x.com/astralabs_ceo">Follow CEO</a>'
        '</body></html>'
    )
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, **kw: SimpleNamespace(
            ok=True,
            status_code=200,
            text=(play_html if "play.google.com" in url else site_html),
        ),
    )
    resolved = meta_ad_leads._default_store_resolver(
        "https://play.google.com/store/apps/details?id=ai.astralabs.app"
    )
    assert resolved["domain"] == "astralabs.ai"
    assert resolved["website"] == "https://astralabs.ai"

    scraped = meta_ad_leads._scrape_site_contacts("https://astralabs.ai")
    assert scraped["founder_x"] == "https://x.com/astralabs_ceo"

    # treg_enrich.domain_of must ignore store & CDN domains
    assert treg_enrich.domain_of("https://apps.apple.com/us/app/test/id123") == ""
    assert treg_enrich.domain_of("https://tiktok.com/@brand") == ""
    assert treg_enrich.domain_of("https://fonts.googleapis.com/css") == ""

    # 2) t.me/s/ HTML with 15 posts & limit=3 must return the newest 3 posts (#13..#15), NOT #1..#3
    msgs_html = "".join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">Post #{i}</div></div>'
        for i in range(1, 16)
    )
    newest = tg_ad_spy.fetch_public_channel_posts_http(
        "startupoftheday",
        limit=3,
        monid_fn=lambda c, l: None,
        treg_fn=lambda c, l: None,
        http_get_fn=lambda u: f"<html><body>{msgs_html}</body></html>",
    )
    assert newest == ["Post #13", "Post #14", "Post #15"]


def test_seam_end_to_end_high_yield_veins_through_enrich_curate_bridge_split_and_crm(
    monkeypatch, tmp_path
):
    from app.db import connect, init_db
    from app.hygiene import audit_server_leadgen_readiness
    from app.leadgen_settings import DEFAULT_B2B_STEPS, get_leadgen_config, save_leadgen_config
    from app.outreach import create_campaign
    from leadgen import tg_curate, tg_enrich_leads, tg_identity_bridge, tg_llm_qualify, vps_autonomous_replenish
    from leadgen.tg_to_gsheet import apply_master_merge, keep_for_sheet

    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # 1) Legacy safe_warmup config preservation & monid-only readiness
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    saved_legacy = save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "tg_groups": True,
                    "tg_comments": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "meta_ads": False,
                    "yc_ph": True,
                    "social_x_reddit": False,
                    "buyer_catalogs": False,
                    "linkedin_jobs": False,
                },
            },
            "vacancies": {"enabled": False},
        },
    )
    assert saved_legacy["b2b"]["sources"]["tiktok_ads"] is False
    assert saved_legacy["b2b"]["sources"]["whop_content_rewards"] is False
    assert saved_legacy["b2b"]["sources"]["tg_identity_bridge"] is False

    # Enable monid-only tiktok_ads + tiktok_shop without treg -> ready=True
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "tg_groups": False,
                    "tg_comments": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "meta_ads": False,
                    "yc_ph": False,
                    "social_x_reddit": False,
                    "buyer_catalogs": False,
                    "linkedin_jobs": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": True,
                    "tiktok_shop": True,
                    "tg_identity_bridge": True,
                },
            },
            "vacancies": {"enabled": False},
        },
    )
    monkeypatch.setattr("app.hygiene._check_treg_ready", lambda: False)
    monkeypatch.setattr("app.hygiene._check_monid_ready", lambda: True)
    readiness = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert readiness["treg_required"] is False
    assert readiness["monid_ready"] is True

    # 2) Unmocked seam flow from collect_all_veins -> CRM & direct_x_leads.jsonl
    monkeypatch.setattr(
        high_yield_veins,
        "_default_web_search",
        lambda q: {
            "results": [
                {
                    "url": "https://whop.com/discover/whop-clippulse/",
                    "title": "Earn $2 per 1,000 Views Clipping ClipPulse",
                    "snippet": "Budget: $4,000. CPM: $2 per 1,000 views. Telegram @clippulse_ceo",
                },
                {
                    "url": "https://whop.com/discover/fundedfox/",
                    "title": "Earn $1.50 per 1,000 Views Clipping FundedFox",
                    "snippet": "Prop trading challenge for funded traders. Budget: $5,000. CPM: $1.50 per 1,000 views. DM @fundedfox_ceo",
                },
            ]
        },
    )
    monkeypatch.setattr(
        high_yield_veins,
        "_default_tiktok_ads_search",
        lambda kw: {
            "data": {
                "materials": [
                    {
                        "brand_name": "TarotFlow",
                        "ad_title": "AI Birth Chart & Palm Reader for content creator & trader",
                        "landing_page": "https://tarotflow.app",
                    }
                ]
            }
        }
        if kw == high_yield_veins.TIKTOK_AD_QUERIES[0]
        else {"data": {"materials": []}},
    )
    monkeypatch.setattr(
        high_yield_veins,
        "_default_tiktok_shop_search",
        lambda kw: {"products": [{"title": "GlowLab Skincare Serum", "seller_name": "GlowLab"}]}
        if kw == high_yield_veins.TIKTOK_SHOP_QUERIES[0]
        else {"products": []},
    )
    monkeypatch.setattr(
        high_yield_veins,
        "find_company_decision_maker",
        lambda brand, domain="": (
            {"project_url": "https://clippulse.ai", "work_email": "ceo@clippulse.ai"}
            if "ClipPulse" in brand
            else (
                {"work_email": "team@fundedfox.io"}
                if "FundedFox" in brand
                else (
                    {"project_url": "https://tarotflow.app", "founder_linkedin": "https://www.linkedin.com/in/tarotflow-ceo"}
                    if "TarotFlow" in brand
                    else {"project_url": "https://glowlab.shop", "work_email": "growth@glowlab.shop"}
                )
            )
        ),
    )
    monkeypatch.setattr(
        high_yield_veins,
        "_scrape_site_contacts",
        lambda url: (
            {"founder_x": "https://x.com/tarotflow_ceo"} if "tarotflow" in url else {}
        ),
    )

    vein_rows, _ = high_yield_veins.collect_all_veins(day=date(2026, 11, 17), limit=12)
    assert len(vein_rows) == 4
    assert {r["source"] for r in vein_rows} == {"whop_content_rewards", "tiktok_ads", "tiktok_shop"}
    funded_raw = next(r for r in vein_rows if r["project"] == "FundedFox")
    assert funded_raw["project_url"] == ""
    assert funded_raw["source_url"] == "https://whop.com/discover/fundedfox/"

    src_jsonl = state_dir / "hy.jsonl"
    raw_jsonl = state_dir / "raw_seam.jsonl"
    master_jsonl = state_dir / "master_ids.jsonl"
    src_jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in vein_rows) + "\n", encoding="utf-8")

    added_raw = vps_autonomous_replenish._merge_discovered_jsonl_into_raw([src_jsonl], raw_jsonl)
    assert added_raw == 4
    downstream_cnt = apply_master_merge(str(raw_jsonl), str(master_jsonl))
    assert downstream_cnt == 4

    def _fake_site_get(url, headers=None, timeout=5, allow_redirects=True):
        if "whop.com" in url:
            return SimpleNamespace(
                status_code=403,
                ok=False,
                headers={"Content-Type": "text/html"},
                text="<html><head><title>Just a moment...</title></head><body>Enable JavaScript and cookies</body></html>",
            )
        return SimpleNamespace(
            status_code=200,
            ok=True,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><head><meta name='description' content='Subscription consumer product with monthly plan $19/mo'></head>"
                "<body><p>Start free trial $19/month</p></body></html>"
            ),
        )

    monkeypatch.setattr(tg_enrich_leads.requests, "get", _fake_site_get)

    raw_new_rows = [json.loads(ln) for ln in Path(f"{raw_jsonl}.new").read_text(encoding="utf-8").splitlines() if ln.strip()]
    enriched = tg_enrich_leads.enrich_rows(raw_new_rows, fetch=True, skip_copy=True)
    for er in enriched:
        if er["project"] == "FundedFox":
            assert er["category"] == "Проп трейдинг"
            assert er["role"] == "Founder"
            assert er["project_url"] == ""
            assert "just a moment" not in (er.get("what_they_do") or "").lower()
        elif er["source"] in ("whop_content_rewards", "tiktok_shop"):
            assert er["category"] == "SaaS / AI app", f"Category clobbered for {er['source']}: {er['category']}"
            assert er["role"] in {"Founder", "Marketing"}
        else:
            assert er["category"] in {"SaaS / AI app", "Gaming / AI"}, f"Bad category for {er['source']}: {er['category']}"
            assert er["role"] == "Marketing"
        assert keep_for_sheet(er) is True
        er["icp_score"] = 88
        er["monetization"] = "subscription"
        er["short_form_fit"] = True

    kept_pre_gate, gate_notes = tg_llm_qualify.gate_before_model(enriched)
    assert gate_notes == []
    assert len(kept_pre_gate) == 4
    assert [tg_llm_qualify._product_reality_reject_reason(r) for r in kept_pre_gate] == ["", "", "", ""]

    curated = tg_curate.curate_rows(kept_pre_gate)
    assert len(curated) == 4

    bridged = tg_identity_bridge.enrich_rows_with_telegram_bridge(
        curated,
        max_rows=5,
        x_profile_fn=lambda h: {"bio": "Founder @ TarotFlow"},
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: (
            '<html><body><div class="tgme_page_title">TarotFlow CEO</div>'
            '<div class="tgme_page_extra">@tarotflow_ceo</div>'
            '<div class="tgme_page_description">Founder of TarotFlow.app</div></body></html>'
            if nick == "tarotflow_ceo"
            else ""
        ),
    )

    final_jsonl = state_dir / "raw_20261117_190000.final.jsonl"
    # Inject a corrupted line & non-dict line to verify malformed JSONL resilience
    final_jsonl.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in bridged) + "\n{broken-json\n[1,2,3]\n",
        encoding="utf-8",
    )
    assert vps_autonomous_replenish._count_personal_tg_rows(final_jsonl) == 2

    dx_path = state_dir / "direct_x_leads.jsonl"
    split_res = vps_autonomous_replenish.split_tg_and_direct_x_leads(final_jsonl, dx_path)
    assert split_res == {"tg_wa_kept": 2, "direct_x_added": 2, "dropped": 0}
    dx_projects = {json.loads(ln)["project"] for ln in dx_path.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert dx_projects == {"FundedFox", "GlowLab"}

    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=50,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    push_res = vps_autonomous_replenish.push_discovered_now(conn)
    assert push_res["ingested"] == 2
    tg_users = {
        r["username"]
        for r in conn.execute("SELECT username FROM leads WHERE username IN ('clippulse_ceo', 'tarotflow_ceo')").fetchall()
    }
    assert tg_users == {"clippulse_ceo", "tarotflow_ceo"}
    conn.close()


def test_live_quality_hardening_rejects_noise_and_ungrounded_linkedin_and_resolves_brand_sites(monkeypatch):
    from leadgen import tg_enrich_leads, treg_enrich

    # 1) Noise placeholder & Whop docs rejection + LinkedIn-only without domain rejected
    noise_whop_hits = {
        "results": [
            {
                "url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/brands-terms",
                "title": "Organization terms of service",
                "snippet": "Budget: $1,000. CPM: $5.1",
            },
            {
                "url": "https://whop.com/content-campaigns-access/",
                "title": "Content Campaigns",
                "snippet": "Budget: $1,000. CPM: $1.00 per 1,000 views",
            },
            {
                "url": "https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/discover/de8b89",
                "title": "Slideshows [English]",
                "snippet": "Post slideshows about Content Rewards, $1.00 CPM",
            },
            {
                "url": "https://whop.com/discover/realbrand/",
                "title": "Earn $2 per 1,000 Views Clipping RealBrandApp",
                "snippet": "Budget: $2,500",
            },
        ]
    }
    whop_rows, _ = high_yield_veins.collect_whop_content_rewards(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        search_fn=lambda q: noise_whop_hits,
        enrich_fn=lambda b, d: {"founder_linkedin": "https://www.linkedin.com/in/random-stranger"},
        site_scrape_fn=lambda u: {},
    )
    # LinkedIn-only without domain/work_email/founder_x/username MUST be dropped cleanly!
    assert whop_rows == []

    # 2) TikTok Ads rejects "Not Mention" & "Sponsored", resolves brand website via web_search_fn
    tt_ads, _ = high_yield_veins.collect_tiktok_creative_ads(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        ads_search_fn=lambda kw: {
            "data": {
                "materials": [
                    {"brand_name": "Not Mention", "ad_title": "AI app test"},
                    {"brand_name": "Sponsored", "ad_title": "AI video"},
                    {"brand_name": "Jitt – App of Fame", "ad_title": "challenges, music challenge, dance challenge"},
                ]
            }
        }
        if kw == high_yield_veins.TIKTOK_AD_QUERIES[0]
        else {"data": {"materials": []}},
        web_search_fn=lambda q: {
            "results": [
                {"url": "https://apps.apple.com/us/app/jitt-app-of-fame/id6500001111", "title": "Jitt - App of Fame"}
            ]
        },
        store_resolver_fn=lambda u: {
            "developer": "Jitt Labs Inc.",
            "website": "https://jitt.app",
            "domain": "jitt.app",
            "email": "founders@jitt.app",
            "rating_count": 420,
            "is_megacorp": False,
        },
        enrich_fn=lambda c, d: {},
        site_scrape_fn=lambda u: {"founder_x": "https://x.com/jittapp"},
    )
    assert len(tt_ads) == 1
    assert tt_ads[0]["name"] == "Jitt – App of Fame"
    assert tt_ads[0]["project_url"] == "https://jitt.app"
    assert tt_ads[0]["work_email"] == "founders@jitt.app"
    # Bare "dance challenge" MUST NOT classify as Prop trading!
    enriched_jitt = tg_enrich_leads.enrich_rows(tt_ads, fetch=False, skip_copy=True)
    assert enriched_jitt[0]["category"] != "Проп трейдинг"
    assert tg_enrich_leads.classify_cat("Prop trading challenge for funded traders") == "Проп трейдинг"

    # 3) TikTok Shop resolves "POWERBIRTH" -> official site (ignoring Amazon) and rejects LinkedIn-only guess
    shop_rows, _ = high_yield_veins.collect_tiktok_shop_brands(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        shop_search_fn=lambda kw: {
            "products": [
                {"title": "POWERBIRTH Sleep Gummies", "seller_info": {"shop_name": "POWERBIRTH"}},
                {"title": "UnresolvedBrand Gummies", "seller_info": {"shop_name": "UnresolvedCo"}},
            ]
        }
        if kw == high_yield_veins.TIKTOK_SHOP_QUERIES[0]
        else {"products": []},
        web_search_fn=lambda q: (
            {
                "results": [
                    {"url": "https://www.amazon.com/powerbirth-gummies", "title": "POWERBIRTH on Amazon"},
                    {"url": "https://powerbirth.com", "title": "POWERBIRTH Official Store"},
                ]
            }
            if "POWERBIRTH" in q
            else {"results": []}
        ),
        enrich_fn=lambda comp, dom: {"founder_linkedin": "https://www.linkedin.com/in/stranger"},
        site_scrape_fn=lambda u: {"work_email": "hello@powerbirth.com"} if "powerbirth.com" in u else {},
    )
    assert len(shop_rows) == 1
    assert shop_rows[0]["project"] == "POWERBIRTH"
    assert shop_rows[0]["project_url"] == "https://powerbirth.com"
    assert shop_rows[0]["work_email"] == "hello@powerbirth.com"

    # 4) find_company_decision_maker rejects search hits that don't mention the brand/domain
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/greg-moore-411a4b4",
                    "title": "Greg Moore - Random Consultant",
                    "snippet": "Helping SaaS teams grow",  # zero mention of Jitt!
                }
            ]
        },
    )
    assert treg_enrich.find_company_decision_maker("Jitt – App of Fame", "") == {}

    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/tarotflow-ceo",
                    "title": "Jane Doe - Founder at TarotFlow",
                    "snippet": "Building tarotflow.app consumer AI app",
                }
            ]
        },
    )
    matched = treg_enrich.find_company_decision_maker("TarotFlow", "tarotflow.app")
    assert matched["founder_linkedin"] == "https://www.linkedin.com/in/tarotflow-ceo"
