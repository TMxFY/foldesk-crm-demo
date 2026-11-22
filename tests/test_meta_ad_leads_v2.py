from __future__ import annotations

from datetime import date
from pathlib import Path

from leadgen import meta_ad_leads


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_search_queries_deleted_gaming_and_cover_high_ltv_whale_clusters():
    joined = " | ".join(meta_ad_leads.SEARCH_QUERIES).casefold()
    source_text = (REPO_ROOT / "leadgen" / "meta_ad_leads.py").read_text(encoding="utf-8").casefold()

    for banned in ("roblox", "fortnite", "fivem", "mobile rpg"):
        assert banned not in joined, f"Found banned gaming query substring: {banned}"
        assert banned not in source_text, f"Found banned gaming substring in meta_ad_leads.py: {banned}"
    assert "creatoros" not in source_text

    # 1) Subscription consumer / AI apps
    assert any(k in joined for k in ("coaching app", "astrology app", "language learning", "therapy app", "sleep tracker"))
    # 2) Crypto / fintech / prop-trading
    assert any(k in joined for k in ("crypto wallet", "prop trading", "forex prop firm", "wealth management"))
    # 3) Large Shopify / D2C brands
    assert any(k in joined for k in ("skincare", "supplement", "shopify", "longevity", "peptide"))
    # 4) High-ticket infobiz / coaching
    assert any(k in joined for k in ("executive coaching", "mastermind", "high ticket coaching", "mentorship"))


def test_multi_geo_rotation_and_country_forwarding(monkeypatch):
    assert meta_ad_leads.TARGET_COUNTRIES == (
        "US",
        "GB",
        "AE",
        "SA",
        "DE",
        "FR",
        "NL",
        "ES",
        "IT",
        "SE",
        "CA",
        "AU",
    )

    seen_countries: list[str] = []
    base_day = date(2026, 11, 20)
    for offset in range(4):
        seen_countries.extend(meta_ad_leads.countries_for_day(date.fromordinal(base_day.toordinal() + offset), count=3))
    assert set(seen_countries) == set(meta_ad_leads.TARGET_COUNTRIES)

    # _default_search forwards country parameter to treg.call
    captured_calls: list[tuple[str, dict]] = []

    def fake_treg_call(endpoint: str, *, query: dict, timeout: int = 60):
        captured_calls.append((endpoint, dict(query)))
        return {"searchResults": []}

    monkeypatch.setattr(meta_ad_leads, "call", fake_treg_call)
    meta_ad_leads._default_search("AI coaching app", country="ae")
    assert captured_calls == [
        (
            meta_ad_leads.ENDPOINT,
            {
                "query": "AI coaching app",
                "trim": "true",
                "status": "ACTIVE",
                "country": "AE",
                "media_type": "VIDEO",
            },
        )
    ]

    # collect() passes country to search_fn and tags country_code + segment='whale'
    search_calls: list[tuple[str, str]] = []

    def fake_multi_geo_search(query: str, country: str = "US"):
        search_calls.append((query, country))
        slug = f"{country.lower()}-{len(search_calls)}"
        return {
            "searchResults": [
                {
                    "is_active": True,
                    "page_id": f"900{len(search_calls)}",
                    "snapshot": {
                        "display_format": "VIDEO",
                        "page_name": f"Whale Brand {country} {len(search_calls)}",
                        "body": {"text": "Annual subscription AI workspace"},
                        "link_url": f"https://brand-{slug}.com",
                        "cta_text": "Learn More",
                    },
                }
            ]
        }

    rows, _ = meta_ad_leads.collect(
        day=base_day,
        seen={},
        cap=10,
        countries=("US", "GB", "AE", "DE"),
        search_fn=fake_multi_geo_search,
        enrich_fn=lambda company, domain: {"work_email": f"ceo@{domain}"},
    )
    assert [c for _, c in search_calls] == ["US", "GB", "AE", "DE"]
    assert [r["country_code"] for r in rows] == ["US", "GB", "AE", "DE"]
    assert all(r["segment"] == "whale" for r in rows)

    # Backward compatibility: 1-arg search_fn(query) still works
    one_arg_rows, _ = meta_ad_leads.collect(
        day=base_day,
        seen={},
        cap=5,
        search_fn=lambda q: {
            "searchResults": [
                {
                    "is_active": True,
                    "snapshot": {
                        "display_format": "VIDEO",
                        "page_name": "Legacy One Arg Brand",
                        "body": {"text": "Subscription app"},
                        "link_url": "https://legacyonearg.com",
                    },
                }
            ]
        },
        enrich_fn=lambda company, domain: {"work_email": "founder@legacyonearg.com"},
    )
    assert len(one_arg_rows) == 1
    assert one_arg_rows[0]["segment"] == "whale"
    assert one_arg_rows[0]["country_code"] in meta_ad_leads.TARGET_COUNTRIES


def test_click_to_whatsapp_extraction_and_domain_hygiene():
    payload = {
        "searchResults": [
            # 1) Direct wa.me link without website domain
            {
                "is_active": True,
                "page_id": "111222333",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Aura Peptide Clinic",
                    "page_profile_uri": "https://www.facebook.com/AuraPeptideClinic/?__tn__=kC-R",
                    "instagram_actor_name": "@aurapeptides",
                    "body": {"text": "Custom longevity peptide protocol"},
                    "link_url": "https://wa.me/14155550199?text=Book",
                    "cta_type": "WHATSAPP_MESSAGE",
                    "cta_text": "Send WhatsApp message",
                },
            },
            # 2) api.whatsapp.com wrapped inside Facebook l.facebook.com redirect
            {
                "is_active": True,
                "page_id": "222333444",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Dubai Prop Desk",
                    "body": {"text": "Funded trader program for MENA traders"},
                    "link_url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fapi.whatsapp.com%2Fsend%2F%3Fphone%3D%252B971501234567%26text%3Dhi&h=xyz",
                    "caption": "api.whatsapp.com",
                    "cta_type": "WHATSAPP_MESSAGE",
                },
            },
            # 3) Website link_url + WHATSAPP_MESSAGE CTA with phone in caption
            {
                "is_active": True,
                "page_id": "333444555",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Lumina Longevity",
                    "body": {"text": "NAD+ daily longevity supplement subscription"},
                    "link_url": "https://luminalongevity.com/offer",
                    "caption": "+44 7911 123456",
                    "cta_type": "WHATSAPP_MESSAGE",
                    "cta_text": "WhatsApp Us",
                },
            },
            # 4) Carousel card with WhatsApp CTA and video_hd_url + phone in card caption
            {
                "is_active": True,
                "page_id": "444555666",
                "snapshot": {
                    "display_format": "CAROUSEL",
                    "page_name": "Nova Executive Coaching",
                    "cards": [
                        {
                            "video_hd_url": "https://cdn.example.com/ad.mp4",
                            "link_url": "https://novaexecutive.io",
                            "caption": "491701234567",
                            "cta_type": "WHATSAPP_MESSAGE",
                            "body": "1-on-1 executive leadership coaching",
                        }
                    ],
                },
            },
            # 5) App Store link with WHATSAPP_MESSAGE must NOT parse app id as phone number
            {
                "is_active": True,
                "page_id": "555666777",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "SleepSync AI",
                    "body": {"text": "AI sleep tracking subscription"},
                    "link_url": "https://apps.apple.com/us/app/sleepsync-ai/id6479991234",
                    "cta_type": "WHATSAPP_MESSAGE",
                },
            },
        ]
    }

    rows, _ = meta_ad_leads.collect(
        day=date(2026, 11, 20),
        seen={},
        cap=10,
        countries=("AE",),
        search_fn=lambda q, country="US": payload,
        enrich_fn=lambda company, domain: (
            {"work_email": f"hi@{domain}"} if domain else {}
        ),
    )

    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {
        "Aura Peptide Clinic",
        "Dubai Prop Desk",
        "Lumina Longevity",
        "Nova Executive Coaching",
        "SleepSync AI",
    }

    r1 = by_name["Aura Peptide Clinic"]
    assert r1["whatsapp"] == "https://wa.me/14155550199"
    assert r1["phone"] == "+14155550199"
    assert r1["provider"] == "whatsapp"
    assert r1["cta_type"] == "WHATSAPP_MESSAGE"
    assert r1["project_url"] == ""
    assert r1["id"] == "meta_ads:aura-peptide-clinic"
    assert "https://wa.me/14155550199" in r1["links"]

    r2 = by_name["Dubai Prop Desk"]
    assert r2["whatsapp"] == "https://wa.me/971501234567"
    assert r2["phone"] == "+971501234567"
    assert r2["project_url"] == ""

    r3 = by_name["Lumina Longevity"]
    assert r3["project_url"] == "https://luminalongevity.com"
    assert r3["whatsapp"] == "https://wa.me/447911123456"
    assert r3["phone"] == "+447911123456"
    assert r3["work_email"] == "hi@luminalongevity.com"

    r4 = by_name["Nova Executive Coaching"]
    assert r4["project_url"] == "https://novaexecutive.io"
    assert r4["whatsapp"] == "https://wa.me/491701234567"
    assert r4["phone"] == "+491701234567"

    r5 = by_name["SleepSync AI"]
    assert r5["whatsapp"] == ""
    assert r5["phone"] == ""


def test_fb_and_ig_metadata_preservation():
    payload = {
        "searchResults": [
            # Explicit page_id without page_profile_uri -> constructs canonical FB URL;
            # instagram_profile_uri wins over display name with spaces
            {
                "is_active": True,
                "page_id": "98765432101",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "KetoPulse Labs",
                    "instagram_actor_name": "KetoPulse Official",
                    "instagram_profile_uri": "https://www.instagram.com/ketopulse_labs/?igsh=abc123",
                    "body": {"text": "Metabolic coaching subscription"},
                    "link_url": "https://ketopulselabs.com",
                },
            },
            # profile.php?id=... extracts fb_page_id and ignores reserved IG /reel/ path
            {
                "is_active": True,
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Velvet Serum Co",
                    "page_profile_uri": "https://m.facebook.com/profile.php?id=1000888999777&fref=nf",
                    "instagram_profile_uri": "https://www.instagram.com/reel/Cxyz123/",
                    "body": {"text": "Peptide anti-aging serum https://instagram.com/velvetserumco"},
                    "link_url": "https://velvetserum.co",
                },
            },
        ]
    }

    rows, _ = meta_ad_leads.collect(
        day=date(2026, 11, 21),
        seen={},
        cap=10,
        countries=("GB",),
        search_fn=lambda q, country="US": payload,
        enrich_fn=lambda company, domain: {"work_email": f"founder@{domain}"},
    )
    by_name = {r["name"]: r for r in rows}

    r_keto = by_name["KetoPulse Labs"]
    assert r_keto["fb_page_id"] == "98765432101"
    assert r_keto["fb_page_url"] == "https://www.facebook.com/98765432101"
    assert r_keto["instagram_handle"] == "ketopulse_labs"
    assert r_keto["instagram_url"] == "https://instagram.com/ketopulse_labs"
    assert r_keto["country_code"] == "GB"
    assert r_keto["segment"] == "whale"
    assert "https://www.facebook.com/98765432101" in r_keto["links"]
    assert "https://instagram.com/ketopulse_labs" in r_keto["links"]

    r_velvet = by_name["Velvet Serum Co"]
    assert r_velvet["fb_page_id"] == "1000888999777"
    assert r_velvet["fb_page_url"] == "https://www.facebook.com/profile.php?id=1000888999777"
    assert r_velvet["instagram_handle"] == "velvetserumco"
    assert r_velvet["instagram_url"] == "https://instagram.com/velvetserumco"
    assert r_velvet["country_code"] == "GB"
    assert r_velvet["segment"] == "whale"


def test_meta_ad_leads_cursor_rotation_and_channel_handle_routing(monkeypatch) -> None:
    d0 = date(2026, 11, 22)
    q0 = meta_ad_leads.queries_for_day(d0, count=6, cursor=0)
    q1 = meta_ad_leads.queries_for_day(d0, count=6, cursor=1)
    assert len(q0) == 6 and len(q1) == 6
    assert set(q0).isdisjoint(set(q1))

    c0 = meta_ad_leads.countries_for_day(d0, count=6, cursor=0)
    c1 = meta_ad_leads.countries_for_day(d0, count=6, cursor=1)
    assert len(c0) == 6 and len(c1) == 6
    assert set(c0) != set(c1)

    monkeypatch.setattr(
        meta_ad_leads,
        "_scrape_site_contacts",
        lambda _url: {"username": "Plus500_Official", "founder_x": "https://x.com/plus500"},
    )
    payload = {
        "searchResults": [
            {
                "is_active": True,
                "page_id": "398561793349460",
                "snapshot": {
                    "display_format": "VIDEO",
                    "page_name": "Plus500 US",
                    "cta_type": "INSTALL_MOBILE_APP",
                    "body": {"text": "Start Trading Today"},
                    "link_url": "https://us.plus500.com",
                },
            }
        ]
    }
    rows, _ = meta_ad_leads.collect(
        day=d0,
        seen={},
        cap=5,
        countries=("US",),
        search_fn=lambda q, country="US": payload,
        enrich_fn=None,
        store_resolver_fn=lambda _u: {},
    )
    assert len(rows) == 1
    assert rows[0]["username"] == ""
    assert rows[0]["provider"] == ""
    assert rows[0]["telegram_group"] == "https://t.me/Plus500_Official"
    assert "https://t.me/Plus500_Official" in rows[0]["links"]

