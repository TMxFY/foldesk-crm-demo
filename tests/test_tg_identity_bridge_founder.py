from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from leadgen import tg_identity_bridge, treg_enrich


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


def _channel_tme_html(username: str, title: str, subs: str = "5 400 subscribers") -> str:
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">{subs}</div>'
        f'<div class="tgme_page_description">Official channel @{username}</div>'
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">View in Telegram</a></div>'
        "</body></html>"
    )


def test_infer_founder_name_from_row_priority_and_linkedin_slug_parsing() -> None:
    infer = tg_identity_bridge._infer_founder_name_from_row

    # 1) Explicit founder_name wins over name and LinkedIn slug
    assert (
        infer(
            {
                "founder_name": "  Nadia   Szeinbaum ",
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/someone-else-123456",
            },
            "Earthia",
        )
        == "Nadia Szeinbaum"
    )

    # 2) Multi-word person `name` distinct from brand wins over LinkedIn slug
    assert (
        infer(
            {
                "name": "Theo Champion",
                "company": "Klap",
                "founder_linkedin": "https://www.linkedin.com/in/other-person",
            },
            "Klap",
        )
        == "Theo Champion"
    )

    # 3) When name == company/brand (Earthia, HMR ECOM, Klap) -> parses LinkedIn /in/<slug>
    assert (
        infer(
            {
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/nadia-szeinbaum",
            },
            "Earthia",
        )
        == "Nadia Szeinbaum"
    )
    assert (
        infer(
            {
                "name": "HMR ECOM",
                "company": "HMR ECOM",
                "founder_linkedin": "https://www.linkedin.com/in/sharlene-carman-76ba6132/",
            },
            "HMR ECOM",
        )
        == "Sharlene Carman"
    )
    assert (
        infer(
            {
                "name": "Klap",
                "company": "Klap",
                "linkedin_url": "https://uk.linkedin.com/in/andrew-dealy-batson-4bb44a6?trk=public",
            },
            "Klap",
        )
        == "Andrew Dealy Batson"
    )
    assert (
        infer(
            {
                "name": "Klap",
                "company": "Klap",
                "founder_linkedin": "https://www.linkedin.com/in/theo_champion_123456",
            },
            "Klap",
        )
        == "Theo Champion"
    )
    assert (
        infer(
            {
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/jane-doe-abcde",
            },
            "Earthia",
        )
        == "Jane Doe"
    )

    # 4) Never turns generic/single-word/brand slugs into fake person names
    assert (
        infer(
            {
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/nadiaszeinbaum",
            },
            "Earthia",
        )
        == ""
    )
    assert (
        infer(
            {
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/profile-in-www",
            },
            "Earthia",
        )
        == ""
    )
    assert (
        infer(
            {
                "name": "Earthia",
                "company": "Earthia",
                "founder_linkedin": "https://www.linkedin.com/in/a-b-123456",
            },
            "Earthia",
        )
        == ""
    )


def test_extract_social_or_email_candidates_linkedin_variants_and_slug_cleaning() -> None:
    cands = tg_identity_bridge._extract_social_or_email_candidates(
        {
            "founder_linkedin": "https://www.linkedin.com/in/nadia-szeinbaum-76ba6132/",
            "work_email": "info@earthia.co",
        }
    )
    nicks = [nick for nick, _ in cands]
    assert "nadia_szeinbaum" in nicks
    assert "nadiaszeinbaum" in nicks
    assert "nadias" in nicks
    assert "info" not in nicks

    cands_3w = tg_identity_bridge._extract_social_or_email_candidates(
        {
            "linkedin_url": "https://www.linkedin.com/in/andrew-dealy-batson-4bb44a6",
        }
    )
    nicks_3w = [nick for nick, _ in cands_3w]
    assert "andrew_dealy_batson" in nicks_3w
    assert "andrewdealybatson" in nicks_3w
    assert "andrew_batson" in nicks_3w
    assert "andrewbatson" in nicks_3w
    assert "andrewb" in nicks_3w


def test_bridge_stage3b_fallback_search_and_linkedin_slug_name_confirmation() -> None:
    # Prod bug 1: Earthia with name == company, missing founder_name, Stage 3a hits only a channel,
    # Stage 3b tries "<founder>" "<brand>" then falls back to `site:t.me "<founder>"`,
    # and t.me bio does NOT repeat brand/domain — confirmed via inferred founder name!
    queries: list[str] = []

    def fake_search(q: str):
        queries.append(q)
        if q == 'site:t.me "earthia.co"':
            return {
                "results": [
                    {
                        "url": "https://t.me/earthia_official",
                        "title": "Earthia Channel",
                        "snippet": "Official updates earthia.co",
                    }
                ]
            }
        if q == 'site:t.me "Nadia Szeinbaum" "Earthia"':
            return {"results": []}
        if q == 'site:t.me "Nadia Szeinbaum"':
            return {
                "results": [
                    {
                        "url": "https://t.me/nadia_sz_tg",
                        "title": "Nadia Szeinbaum",
                        "snippet": "Founder building consumer products",
                    }
                ]
            }
        return {"results": []}

    tme_pages = {
        "earthia_official": _channel_tme_html("earthia_official", "Earthia Official"),
        "nadia_sz_tg": _personal_tme_html("nadia_sz_tg", "Nadia Szeinbaum", "San Francisco | Building cool things"),
    }

    res = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Earthia",
            "company": "Earthia",
            "project": "Earthia",
            "project_url": "https://earthia.co",
            "founder_linkedin": "https://www.linkedin.com/in/nadia-szeinbaum-76ba6132",
        },
        x_profile_fn=lambda _h: None,
        search_fn=fake_search,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: tme_pages.get(nick, ""),
    )

    assert 'site:t.me "Nadia Szeinbaum" "Earthia"' in queries
    assert 'site:t.me "Nadia Szeinbaum"' in queries
    assert res["username"] == "nadia_sz_tg"
    assert res["tg_bridge_match"] == "founder_search_verified"
    assert res["founder_name"] == "Nadia Szeinbaum"


def test_bridge_stage4_linkedin_variants_and_x_founder_inferred_fallback() -> None:
    # Prod bug 2: HMR ECOM with sharlene-carman-76ba6132 -> search empty -> Stage 4 resolves `sharlenecarman`
    # when t.me title is "Sharlene Carman" (without repeating HMR ECOM).
    res_hmr = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "HMR ECOM",
            "company": "HMR ECOM",
            "project_url": "https://hmrecom.com",
            "founder_linkedin": "https://www.linkedin.com/in/sharlene-carman-76ba6132",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: {"results": []},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: (
            _personal_tme_html("sharlenecarman", "Sharlene Carman", "D2C growth")
            if nick == "sharlenecarman"
            else ""
        ),
    )
    assert res_hmr["username"] == "sharlenecarman"
    assert res_hmr["tg_bridge_match"] == "linkedin_handle_confirmed"

    # Also test `nadias` initial variant in Stage 4
    res_initial = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Earthia",
            "company": "Earthia",
            "project_url": "https://earthia.co",
            "founder_linkedin": "https://www.linkedin.com/in/nadia-szeinbaum",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: {"results": []},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: (
            _personal_tme_html("nadias", "Nadia Szeinbaum", "Wellness")
            if nick == "nadias"
            else ""
        ),
    )
    assert res_initial["username"] == "nadias"
    assert res_initial["tg_bridge_match"] == "linkedin_handle_confirmed"

    # Prod bug 3: Brand X profile has no person name, but founder_linkedin provides 3-word name;
    # x_founder_cands uses inferred name -> tries `andrew_batson` and confirms on "Andrew Batson" title!
    res_klap = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Klap",
            "company": "Klap",
            "project_url": "https://klap.app",
            "founder_x": "https://x.com/klap_app",
            "founder_linkedin": "https://www.linkedin.com/in/andrew-dealy-batson-4bb44a6",
        },
        x_profile_fn=lambda _h: {"name": "Klap", "bio": "Turn long videos into shorts"},
        search_fn=lambda _q: {"results": []},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: (
            _personal_tme_html("andrew_batson", "Andrew Batson", "Building video tools")
            if nick == "andrew_batson"
            else ""
        ),
    )
    assert res_klap["username"] == "andrew_batson"
    assert res_klap["tg_bridge_match"] == "x_founder_handle"

    # Founder name only (no X, no LinkedIn) -> Stage 4 still resolves firstname_lastname with strict confirm
    res_fn_only = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Klap",
            "company": "Klap",
            "founder_name": "Theo Champion",
            "project_url": "https://klap.app",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: {"results": []},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: (
            _personal_tme_html("theo_champion", "Theo Champion", "Paris")
            if nick == "theo_champion"
            else ""
        ),
    )
    assert res_fn_only["username"] == "theo_champion"


def test_bridge_strict_anti_namesake_blocks_single_word_and_unconfirmed_hits() -> None:
    # Single-word TG title ("Nadia") without surname or brand MUST be rejected even when Stage 3b snippet looks good
    res_single_word = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Earthia",
            "company": "Earthia",
            "project_url": "https://earthia.co",
            "founder_linkedin": "https://www.linkedin.com/in/nadia-szeinbaum",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda q: (
            {
                "results": [
                    {
                        "url": "https://t.me/nadia_random",
                        "title": "Nadia Szeinbaum",
                        "snippet": "Earthia founder t.me/nadia_random",
                    }
                ]
            }
            if "Nadia Szeinbaum" in q
            else {"results": []}
        ),
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: _personal_tme_html(nick, "Nadia", "Fitness & coffee in Berlin"),
    )
    assert not res_single_word.get("username")

    # Single-word concatenated LinkedIn slug + unconfirmed TG bio MUST be rejected
    res_concat_unconfirmed = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Earthia",
            "company": "Earthia",
            "project_url": "https://earthia.co",
            "founder_linkedin": "https://www.linkedin.com/in/nadiaszeinbaum",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: {"results": []},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda nick: _personal_tme_html(nick, "Nadia", "Crypto trader unrelated"),
    )
    assert not res_concat_unconfirmed.get("username")


def test_treg_enrich_extracts_founder_name_and_personal_tme_and_preserves_on_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 1) LinkedIn snippet contains explicit personal t.me/<nick> while filtering out channels/bots/support
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/nadia-szeinbaum-76ba6132",
                    "title": "Nadia Szeinbaum, Founder at Earthia | LinkedIn",
                    "snippet": (
                        "Building earthia.co | News: https://t.me/s/earthia_news/12 | "
                        "Support: https://t.me/earthia_support | Bot: https://t.me/earthia_bot | "
                        "DM me: https://t.me/nadia_szeinbaum"
                    ),
                }
            ]
        },
    )
    dm1 = treg_enrich.find_company_decision_maker("Earthia", "earthia.co")
    assert dm1["founder_name"] == "Nadia Szeinbaum"
    assert dm1["founder_linkedin"] == "https://www.linkedin.com/in/nadia-szeinbaum-76ba6132"
    assert dm1["telegram_username"] == "nadia_szeinbaum"
    assert dm1["username"] == "nadia_szeinbaum"

    # 2) Direct t.me URL hit + title without name but clean LinkedIn slug
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://t.me/theo_champion",
                    "title": "Theo Champion - CEO at Klap",
                    "snippet": "Building klap.app",
                }
            ]
        },
    )
    dm2 = treg_enrich.find_company_decision_maker("Klap", "klap.app")
    assert dm2["founder_name"] == "Theo Champion"
    assert dm2["telegram_username"] == "theo_champion"
    assert dm2["username"] == "theo_champion"

    # 3) LinkedIn hit whose title omits person name ("Founder at Earthia | LinkedIn") falls back to clean person slug
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/sharlene-carman-76ba6132",
                    "title": "Founder at HMR ECOM | LinkedIn",
                    "snippet": "Scaling hmrecom.com",
                }
            ]
        },
    )
    dm3 = treg_enrich.find_company_decision_maker("HMR ECOM", "hmrecom.com")
    assert dm3["founder_name"] == "Sharlene Carman"

    # 4) enrich_creator_rows preserves founder_name + personal telegram_username/username on meta_ads & linkedin_jobs rows
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/nadia-szeinbaum",
                    "title": "Nadia Szeinbaum - Co-Founder at Earthia | LinkedIn",
                    "snippet": "earthia.co | Telegram: https://t.me/nadia_szeinbaum",
                }
            ]
        },
    )
    cache = tmp_path / "treg_market.json"
    cache.write_text(json.dumps({"creator": {"at": 9999999999, "text": "cached"}}), encoding="utf-8")
    enriched = treg_enrich.enrich_creator_rows(
        [
            {
                "source": "meta_ads",
                "name": "Earthia",
                "company": "Earthia",
                "project": "Earthia",
                "project_url": "https://earthia.co",
            }
        ],
        cache,
    )
    assert len(enriched) == 1
    assert enriched[0]["founder_name"] == "Nadia Szeinbaum"
    assert enriched[0]["founder_linkedin"] == "https://www.linkedin.com/in/nadia-szeinbaum"
    assert enriched[0]["telegram_username"] == "nadia_szeinbaum"
    assert enriched[0]["username"] == "nadia_szeinbaum"
    assert enriched[0]["telegram_url"] == "https://t.me/nadia_szeinbaum"

    # 5) enrich_creator_rows preserves explicit t.me in links + derives founder_name from founder_linkedin without burning Treg calls
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, **kw: (_ for _ in ()).throw(AssertionError(f"Should not call treg for already-enriched row: {ep}")),
    )
    enriched_existing = treg_enrich.enrich_creator_rows(
        [
            {
                "source": "tiktok_shop",
                "name": "HMR ECOM",
                "company": "HMR ECOM",
                "project_url": "https://hmrecom.com",
                "founder_linkedin": "https://www.linkedin.com/in/sharlene-carman-76ba6132",
                "links": ["https://hmrecom.com", "https://t.me/sharlenecarman"],
            }
        ],
        cache,
    )
    assert len(enriched_existing) == 1
    assert enriched_existing[0]["founder_name"] == "Sharlene Carman"
    assert enriched_existing[0]["telegram_username"] == "sharlenecarman"
    assert enriched_existing[0]["username"] == "sharlenecarman"
    assert enriched_existing[0]["telegram_url"] == "https://t.me/sharlenecarman"
