from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from leadgen import contact_waterfall


def _empty_http(url: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(status_code=200, text="<html><body>No direct links</body></html>")


def test_tier2_triggers_with_x_and_personal_email_when_no_tg_or_wa() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        calls.append((ep, kw))
        if ep == "contactout.people.phone.available":
            return {"available": True}
        if ep == "crawl4ai.people.phone.find":
            return {
                "data": {
                    "phone": {"value": "+14155550101", "kind": "direct", "line": "mobile"}
                }
            }
        if ep == "hlrlookup.people.phone.validate":
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    budget = [0.05]
    out = contact_waterfall.enrich_lead_contacts(
        {
            "project": "BrandX",
            "project_url": "https://brandx.io",
            "work_email": "alex@brandx.io",
            "founder_x": "https://x.com/alex_brandx",
            "founder_linkedin": "https://www.linkedin.com/in/alex-brandx",
            "founder_name": "Alex Rivera",
            "icp_score": 85,
        },
        budget_usd=budget,
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )

    called_eps = [ep for ep, _ in calls]
    assert "contactout.people.phone.available" in called_eps
    assert "crawl4ai.people.phone.find" in called_eps
    assert "hlrlookup.people.phone.validate" in called_eps
    # Already has personal email -> do not waste money re-running email lookup
    assert "crawl4ai.people.email.find" not in called_eps
    assert "blitzapi.people.email.find" not in called_eps
    assert out["phone"] == "+14155550101"
    assert out["phone_line_type"] == "MOBILE"
    assert out["contact_waterfall_tier"] == "tier2"
    assert out["lead_cost"] == pytest.approx(contact_waterfall.COST_CRAWL4AI_PHONE_USD, rel=1e-5)
    assert budget[0] == pytest.approx(0.05 - contact_waterfall.COST_CRAWL4AI_PHONE_USD, rel=1e-5)


def test_tier2_skipped_when_tg_or_wa_present_or_icp_low() -> None:
    calls: list[str] = []

    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        calls.append(ep)
        return {}

    out_tg = contact_waterfall.enrich_lead_contacts(
        {"project": "HasTG", "project_url": "https://hastg.io", "username": "founder_tg", "icp_score": 95},
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )
    out_wa = contact_waterfall.enrich_lead_contacts(
        {
            "project": "HasWA",
            "project_url": "https://haswa.io",
            "whatsapp": "https://wa.me/14155550102",
            "icp_score": 95,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )
    out_low_icp = contact_waterfall.enrich_lead_contacts(
        {
            "project": "LowICP",
            "project_url": "https://lowicp.io",
            "founder_linkedin": "https://www.linkedin.com/in/low-icp",
            "icp_score": 79,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )

    assert calls == []
    assert out_tg["contact_waterfall_tier"] == "tier1"
    assert out_wa["contact_waterfall_tier"] == "tier1"
    assert out_low_icp["contact_waterfall_tier"] == "tier1"


def test_per_lead_cap_and_super_whale_cap() -> None:
    assert contact_waterfall.PER_LEAD_CAP_USD == pytest.approx(0.04)
    assert contact_waterfall.SUPER_WHALE_CAP_USD == pytest.approx(0.15)
    assert contact_waterfall.resolve_per_lead_cap_usd({"icp_score": 85}) == pytest.approx(0.04)
    assert contact_waterfall.resolve_per_lead_cap_usd({"icp_score": 92}) == pytest.approx(0.15)
    assert contact_waterfall.resolve_per_lead_cap_usd({"icp_score": 84, "pool_usd": 10000}) == pytest.approx(0.15)
    assert contact_waterfall.resolve_per_lead_cap_usd({"icp_score": 84, "pool_usd": "$12,500"}) == pytest.approx(0.15)
    assert contact_waterfall.resolve_per_lead_cap_usd({"icp_score": 84, "pool_usd": 5000}) == pytest.approx(0.04)

    seen_headers: list[tuple[int, str]] = []

    def _run_for(icp: int, pool: int) -> None:
        def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
            if ep == "treg.people.phone.find":
                seen_headers.append((icp, kw.get("headers", {}).get("X-Treg-Route-Max-Cost", "")))
                return {"output": {"phone": "+14155550133"}, "_treg": {"cost_usd": 0.02}}
            if ep == "hlrlookup.people.phone.validate":
                return {"results": [{"telephone_number_type": "MOBILE"}]}
            return {}

        contact_waterfall.enrich_lead_contacts(
            {
                "project": f"Whale{icp}",
                "project_url": f"https://whale{icp}.io",
                "work_email": f"ceo@whale{icp}.io",
                "founder_linkedin": f"https://www.linkedin.com/in/ceo-whale-{icp}",
                "icp_score": icp,
                "pool_usd": pool,
            },
            http_get_fn=_empty_http,
            treg_fn=fake_treg,
            dm_finder_fn=lambda c, d: {},
            tg_bridge_fn=lambda r: r,
        )

    _run_for(85, 0)
    _run_for(92, 0)
    _run_for(84, 10000)

    assert len(seen_headers) == 3
    assert float(seen_headers[0][1]) == pytest.approx(0.04, abs=1e-4)
    assert float(seen_headers[1][1]) == pytest.approx(0.15, abs=1e-4)
    assert float(seen_headers[2][1]) == pytest.approx(0.15, abs=1e-4)


def test_layer05_fetch_fb_ig_business_contacts_and_fallback() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_monid_success(provider: str, endpoint: str, *, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((provider, endpoint, body))
        if endpoint == "/facebook.page_contact":
            return {
                "phone": "+1 (415) 555-0144",
                "website": "https://glowdrop.co",
                "email": "founders@glowdrop.co",
                "links": ["https://instagram.com/glowdrop", "https://t.me/glowdrop_founder"],
            }
        if endpoint == "/instagram.profile_contact" and body.get("route") == "octopus":
            return {
                "publicPhone": "+44 7540 822872",
                "publicEmail": "growth@glowdrop.co",
                "externalUrl": "https://glowdrop.co",
            }
        raise AssertionError("chameleon fallback should not run when octopus succeeded")

    res = contact_waterfall.fetch_fb_ig_business_contacts(
        {
            "fb_page_id": "123456789",
            "fb_page_url": "https://www.facebook.com/glowdrop",
            "instagram_url": "https://www.instagram.com/glowdrop/?hl=en",
        },
        monid_fn=fake_monid_success,
    )
    assert calls[0] == ("anyapi", "/facebook.page_contact", {"page": "https://www.facebook.com/glowdrop", "route": "giraffe"})
    assert calls[1] == ("anyapi", "/instagram.profile_contact", {"handle": "glowdrop", "route": "octopus"})
    assert len(calls) == 2
    assert "+14155550144" in res["phones"]
    assert "+447540822872" in res["phones"]
    assert res["website"] == "https://glowdrop.co"
    assert res["telegram_username"] == "glowdrop_founder"
    assert res["email"] == "founders@glowdrop.co"

    # Now test octopus -> chameleon fallback when octopus returns empty/None
    fb_calls: list[str] = []

    def fake_monid_fallback(provider: str, endpoint: str, *, body: dict[str, Any]) -> dict[str, Any] | None:
        route = str(body.get("route") or "")
        fb_calls.append(f"{endpoint}:{route}")
        if endpoint == "/instagram.profile_contact" and route == "octopus":
            return None
        if endpoint == "/instagram.profile_contact" and route == "chameleon":
            return {
                "publicPhone": "+14155550188",
                "phones": ["+14155550189"],
                "bioLinks": [{"url": "https://linktr.ee/lumaskin"}],
                "externalUrl": "https://lumaskin.co",
                "publicEmail": "hello@lumaskin.co",
            }
        return None

    res_fb = contact_waterfall.fetch_fb_ig_business_contacts(
        {"instagram_handle": "@lumaskin"},
        monid_fn=fake_monid_fallback,
    )
    assert fb_calls == ["/instagram.profile_contact:octopus", "/instagram.profile_contact:chameleon"]
    assert res_fb["phone"] == "+14155550188"
    assert "+14155550189" in res_fb["phones"]
    assert "https://linktr.ee/lumaskin" in res_fb["links"]
    assert res_fb["website"] == "https://lumaskin.co"
    assert res_fb["email"] == "hello@lumaskin.co"


def test_layer05_in_waterfall_discovers_site_and_rejects_landline_via_hlr() -> None:
    pages = {
        "https://linktr.ee/lumaskin": '<a href="https://x.com/lumaskin_ceo">X</a>',
        "https://lumaskin.co": "<html><body>Reach us</body></html>",
    }
    treg_eps: list[str] = []

    def fake_monid(provider: str, endpoint: str, *, body: dict[str, Any]) -> dict[str, Any]:
        if endpoint == "/facebook.page_contact":
            return {
                "phone": "+14155550190",  # will be flagged as LANDLINE by HLR!
                "website": "https://lumaskin.co",
                "links": ["https://linktr.ee/lumaskin"],
                "email": "ceo@lumaskin.co",
            }
        return {}

    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        treg_eps.append(ep)
        if ep == "leadmagic.x.employee-finder":
            return {
                "employees": [
                    {
                        "name": "Maya Lin",
                        "title": "Founder & CEO",
                        "linkedin_url": "https://www.linkedin.com/in/maya-lin-luma",
                    }
                ]
            }
        if ep == "crawl4ai.people.phone.find":
            return {"data": {"phone": {"value": "+14155550191", "line": "mobile"}}}
        if ep == "hlrlookup.people.phone.validate":
            num = (kw.get("data") or {}).get("telephone_number")
            if num == "14155550190":
                return {"results": [{"telephone_number_type": "LANDLINE"}]}
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    out = contact_waterfall.enrich_lead_contacts(
        {"company": "LumaSkin", "fb_page_id": "998877", "icp_score": 88},
        http_get_fn=lambda url, **kw: SimpleNamespace(status_code=200, text=pages.get(url.rstrip("/"), "")),
        monid_fn=fake_monid,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )

    assert out["project_url"] == "https://lumaskin.co"
    assert out["founder_x"] == "https://x.com/lumaskin_ceo"
    assert out["work_email"] == "ceo@lumaskin.co"
    # FB landline was dropped by HLR and Layer 1 found mobile instead!
    assert out["phone"] == "+14155550191"
    assert out["phone_line_type"] == "MOBILE"
    assert "leadmagic.x.employee-finder" in treg_eps
    assert "crawl4ai.people.phone.find" in treg_eps


def test_contactout_negative_caps_treg_and_hlr_filters_landline_and_toll_free() -> None:
    calls_std: list[tuple[str, dict[str, Any]]] = []

    def fake_treg_std(ep: str, **kw: Any) -> dict[str, Any]:
        calls_std.append((ep, kw))
        if ep == "contactout.people.phone.available":
            assert kw.get("query") == {"profile": "https://www.linkedin.com/in/std-ceo"}
            return {"available": False}
        if ep == "crawl4ai.people.phone.find":
            # Returns a toll-free number -> HLR must drop it!
            return {"data": {"phone": {"value": "+18005550199"}}}
        if ep == "treg.people.phone.find":
            return {
                "output": {
                    "phones": ["+14155550170", "+447540822872"],
                },
                "_treg": {"cost_usd": 0.005},
            }
        if ep == "hlrlookup.people.phone.validate":
            num = (kw.get("data") or {}).get("telephone_number")
            if num == "18005550199":
                return {"results": [{"telephone_number_type": "TOLL_FREE"}]}
            if num == "14155550170":
                return {"results": [{"telephone_number_type": "LANDLINE"}]}
            if num == "447540822872":
                return {"results": [{"telephone_number_type": "FIXED_LINE_OR_MOBILE"}]}
        return {}

    out_std = contact_waterfall.enrich_lead_contacts(
        {
            "project": "StdApp",
            "project_url": "https://stdapp.io",
            "work_email": "ceo@stdapp.io",
            "founder_linkedin": "https://www.linkedin.com/in/std-ceo",
            "icp_score": 85,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg_std,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )

    assert out_std["phone"] == "+447540822872"
    assert out_std["phone_line_type"] == "FIXED_LINE_OR_MOBILE"
    treg_call_kw = [kw for ep, kw in calls_std if ep == "treg.people.phone.find"][0]
    assert treg_call_kw["method"] == "POST"
    assert treg_call_kw["data"] == {"linkedin_url": "https://www.linkedin.com/in/std-ceo"}
    # ContactOut available=False caps treg.people.phone.find <= 0.01 for non-super-whale!
    assert float(treg_call_kw["headers"]["X-Treg-Route-Max-Cost"]) <= 0.01

    # Super-whale (icp >= 92) is NOT capped at 0.01 even if ContactOut says available=False
    whale_caps: list[float] = []

    def fake_treg_whale(ep: str, **kw: Any) -> dict[str, Any]:
        if ep == "contactout.people.phone.available":
            return {"has_phone": False}
        if ep == "crawl4ai.people.phone.find":
            return {}
        if ep == "treg.people.phone.find":
            whale_caps.append(float(kw["headers"]["X-Treg-Route-Max-Cost"]))
            return {"output": {"phone": "+14155550175"}, "_treg": {"cost_usd": 0.08}}
        if ep == "hlrlookup.people.phone.validate":
            return {}  # unknown line type -> kept safely
        return {}

    out_whale = contact_waterfall.enrich_lead_contacts(
        {
            "project": "SuperWhale",
            "project_url": "https://superwhale.io",
            "work_email": "founder@superwhale.io",
            "founder_linkedin": "https://www.linkedin.com/in/super-whale",
            "icp_score": 94,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg_whale,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )
    assert out_whale["phone"] == "+14155550175"
    assert out_whale["phone_line_type"] == "UNKNOWN"
    assert whale_caps and whale_caps[0] == pytest.approx(0.15, abs=1e-4)


def test_multi_dm_secondary_contact_json_and_dm2_promotion() -> None:
    def multi_dms(company: str, domain: str, limit: int = 2) -> list[dict[str, str]]:
        return [
            {
                "founder_name": "Alice Doe",
                "role_title": "Founder & CEO",
                "founder_linkedin": "https://www.linkedin.com/in/alice-doe",
                "work_email": "alice@twodms.io",
            },
            {
                "founder_name": "Bob Smith",
                "role_title": "Head of Growth",
                "founder_linkedin": "https://www.linkedin.com/in/bob-smith",
                "work_email": "bob@twodms.io",
            },
        ][:limit]

    # 1) Both DMs get mobile phones -> DM 1 stays primary, DM 2 in secondary_contact_json
    def fake_treg_both(ep: str, **kw: Any) -> dict[str, Any]:
        if ep == "crawl4ai.people.phone.find":
            li = (kw.get("query") or {}).get("linkedin", "")
            if "alice-doe" in li:
                return {"data": {"phone": {"value": "+14155550111"}}}
            if "bob-smith" in li:
                return {"data": {"phone": {"value": "+14155550222"}}}
        if ep == "hlrlookup.people.phone.validate":
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    out_both = contact_waterfall.enrich_lead_contacts(
        {"project": "TwoDMs", "project_url": "https://twodms.io", "icp_score": 88},
        http_get_fn=_empty_http,
        treg_fn=fake_treg_both,
        dm_multi_finder_fn=multi_dms,
        tg_bridge_fn=lambda r: r,
    )
    assert out_both["phone"] == "+14155550111"
    assert out_both["founder_linkedin"] == "https://www.linkedin.com/in/alice-doe"
    sec = json.loads(out_both["secondary_contact_json"])
    assert sec == {
        "name": "Bob Smith",
        "role": "Head of Growth",
        "founder_linkedin": "https://www.linkedin.com/in/bob-smith",
        "phone": "+14155550222",
        "telegram_username": "",
        "whatsapp": "",
        "fallback_after_days": 6,
    }

    # 2) DM 1 fails to get a mobile phone, DM 2 gets one -> DM 2 promoted to primary!
    def fake_treg_dm2_only(ep: str, **kw: Any) -> dict[str, Any]:
        if ep == "crawl4ai.people.phone.find":
            li = (kw.get("query") or {}).get("linkedin", "")
            if "bob-smith" in li:
                return {"data": {"phone": {"value": "+14155550222"}}}
        if ep == "hlrlookup.people.phone.validate":
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    out_promo = contact_waterfall.enrich_lead_contacts(
        {"project": "TwoDMs", "project_url": "https://twodms.io", "icp_score": 88},
        http_get_fn=_empty_http,
        treg_fn=fake_treg_dm2_only,
        dm_multi_finder_fn=multi_dms,
        tg_bridge_fn=lambda r: r,
    )
    assert out_promo["phone"] == "+14155550222"
    assert out_promo["founder_name"] == "Bob Smith"
    assert out_promo["founder_linkedin"] == "https://www.linkedin.com/in/bob-smith"
    sec_promo = json.loads(out_promo["secondary_contact_json"])
    assert sec_promo["name"] == "Alice Doe"
    assert sec_promo["founder_linkedin"] == "https://www.linkedin.com/in/alice-doe"
    assert sec_promo["fallback_after_days"] == 6


def test_budget_guard_fn_blocks_paid_layers_and_records_costs() -> None:
    paid_calls: list[str] = []
    recorded: list[tuple[str, str, float, bool]] = []

    out_blocked = contact_waterfall.enrich_lead_contacts(
        {
            "id": "lead_1",
            "project": "Guarded",
            "project_url": "https://guarded.io",
            "fb_page_id": "111222",
            "founder_linkedin": "https://www.linkedin.com/in/guarded-ceo",
            "icp_score": 90,
        },
        http_get_fn=lambda url, **kw: SimpleNamespace(
            status_code=200,
            text='<a href="https://x.com/guarded_ceo">X</a>',
        ),
        monid_fn=lambda *a, **kw: paid_calls.append("monid") or {},
        treg_fn=lambda ep, **kw: paid_calls.append(ep) or {},
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
        budget_guard_fn=lambda provider, est: (False, "daily_cap_reached"),
    )
    # Free tier 1 still worked; paid calls blocked; free contactout/hlr didn't trigger paid endpoints
    assert out_blocked["founder_x"] == "https://x.com/guarded_ceo"
    assert "monid" not in paid_calls
    assert "crawl4ai.people.phone.find" not in paid_calls
    assert "treg.people.phone.find" not in paid_calls
    assert out_blocked["lead_cost"] == 0.0

    # Allowed budget records cost events cleanly
    out_allowed = contact_waterfall.enrich_lead_contacts(
        {
            "id": "lead_2",
            "project": "AllowedCo",
            "project_url": "https://allowed.io",
            "founder_name": "Jane Roe",
            "work_email": "jane@allowed.io",
            "icp_score": 86,
        },
        http_get_fn=_empty_http,
        treg_fn=lambda ep, **kw: (
            {"output": {"phone": "+14155550155"}, "_treg": {"cost_usd": 0.00483}}
            if ep == "treg.people.phone.find"
            else (
                {"results": [{"telephone_number_type": "MOBILE"}]}
                if ep == "hlrlookup.people.phone.validate"
                else {}
            )
        ),
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
        budget_guard_fn=lambda provider, est: True,
        cost_record_fn=lambda *, provider, endpoint, lead_key, cost_usd, hit: recorded.append(
            (provider, endpoint, cost_usd, hit)
        ),
    )
    assert out_allowed["phone"] == "+14155550155"
    assert out_allowed["lead_cost"] == pytest.approx(0.00483, rel=1e-5)
    assert ("treg", "treg.people.phone.find", 0.00483, True) in recorded


def test_name_domain_phone_waterfall_without_linkedin() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        calls.append((ep, kw))
        if ep == "crawl4ai.people.phone.find":
            return {}
        if ep == "treg.people.phone.find":
            return {"output": {"phone": "+14155550166"}, "_treg": {"cost_usd": 0.00483}}
        if ep == "hlrlookup.people.phone.validate":
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    out = contact_waterfall.enrich_lead_contacts(
        {
            "project": "NoLiBrand",
            "project_url": "https://nolibrand.io",
            "founder_name": "Sam Patel",
            "work_email": "sam@nolibrand.io",
            "icp_score": 86,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        dm_finder_fn=lambda c, d: {},
        tg_bridge_fn=lambda r: r,
    )

    eps = [ep for ep, _ in calls]
    assert "contactout.people.phone.available" not in eps
    c4_kw = [kw for ep, kw in calls if ep == "crawl4ai.people.phone.find"][0]
    assert c4_kw["query"] == {"name": "Sam Patel", "domain": "nolibrand.io"}
    tr_kw = [kw for ep, kw in calls if ep == "treg.people.phone.find"][0]
    assert tr_kw["method"] == "POST"
    assert tr_kw["data"] == {"domain": "nolibrand.io", "full_name": "Sam Patel", "email": "sam@nolibrand.io"}
    assert out["phone"] == "+14155550166"


def test_treg_enrich_multi_dm_fallback_and_validate_phone_hlr() -> None:
    # Direct validate_phone_hlr helper checks
    landline_hlr = contact_waterfall.validate_phone_hlr(
        "+1 (415) 555-0180",
        treg_fn=lambda ep, **kw: {"results": [{"telephone_number_type": "LANDLINE"}]},
    )
    assert landline_hlr["phone"] == "+14155550180"
    assert landline_hlr["line_type"] == "LANDLINE"
    assert landline_hlr["keep"] is False

    bad_format_hlr = contact_waterfall.validate_phone_hlr(
        "+2085550085",
        treg_fn=lambda ep, **kw: {"results": [{"telephone_number_type": "BAD_FORMAT"}]},
    )
    assert bad_format_hlr["line_type"] == "BAD_FORMAT"
    assert bad_format_hlr["keep"] is False

    mobile_hlr = contact_waterfall.validate_phone_hlr(
        "+44 7540 822872",
        treg_fn=lambda ep, **kw: {"results": [{"telephone_number_type": "MOBILE"}]},
    )
    assert mobile_hlr["phone"] == "+447540822872"
    assert mobile_hlr["keep"] is True

    # Fallback to treg_enrich.find_company_decision_makers when dm_multi_finder_fn & dm_finder_fn are None
    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        if ep == "crawl4ai.web.search":
            return {
                "results": [
                    {
                        "title": "Elena Vance - Founder & CEO at GrowthGrid",
                        "url": "https://www.linkedin.com/in/elena-vance-gg",
                        "snippet": "Founder & CEO at growthgrid.io",
                    },
                    {
                        "title": "Marcus Cole - CMO at GrowthGrid",
                        "url": "https://www.linkedin.com/in/marcus-cole-gg",
                        "snippet": "CMO at growthgrid.io",
                    },
                ]
            }
        if ep == "crawl4ai.people.phone.find":
            li = (kw.get("query") or {}).get("linkedin", "")
            if "elena-vance-gg" in li:
                return {"data": {"phone": {"value": "+14155550181"}}}
            if "marcus-cole-gg" in li:
                return {"data": {"phone": {"value": "+14155550182"}}}
        if ep == "hlrlookup.people.phone.validate":
            return {"results": [{"telephone_number_type": "MOBILE"}]}
        return {}

    out = contact_waterfall.enrich_lead_contacts(
        {
            "company": "GrowthGrid",
            "project_url": "https://growthgrid.io",
            "work_email": "elena@growthgrid.io",
            "icp_score": 89,
        },
        http_get_fn=_empty_http,
        treg_fn=fake_treg,
        tg_bridge_fn=lambda r: r,
    )
    assert out["founder_linkedin"] == "https://www.linkedin.com/in/elena-vance-gg"
    assert out["phone"] == "+14155550181"
    sec = json.loads(out["secondary_contact_json"])
    assert sec["name"] == "Marcus Cole"
    assert sec["founder_linkedin"] == "https://www.linkedin.com/in/marcus-cole-gg"
    assert sec["phone"] == "+14155550182"
    assert sec["fallback_after_days"] == 6


def test_allow_paid_false_blocks_live_treg_and_monid_in_dm_search_and_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leadgen import monid_client, tg_identity_bridge, treg_client

    paid_calls: list[str] = []
    monkeypatch.setattr(treg_client, "check_treg_ready", lambda **kw: True)
    monkeypatch.setattr(monid_client, "check_monid_ready", lambda **kw: True)
    monkeypatch.setattr(
        treg_client,
        "call",
        lambda ep, **kw: paid_calls.append(f"treg:{ep}") or {},
    )
    monkeypatch.setattr(
        contact_waterfall,
        "treg_call",
        lambda ep, **kw: paid_calls.append(f"treg:{ep}") or {},
    )
    monkeypatch.setattr(
        monid_client,
        "run_monid",
        lambda prov, ep, **kw: paid_calls.append(f"monid:{prov}{ep}") or {},
    )

    tme_html = (
        '<html><div class="tgme_page_title">Clara Founder</div>'
        '<div class="tgme_page_description">Building FreeStage</div>'
        '<div class="tgme_page_extra">@clara_free</div></html>'
    )
    monkeypatch.setattr(
        tg_identity_bridge.requests,
        "get",
        lambda url, **kw: SimpleNamespace(
            status_code=200,
            is_redirect=False,
            headers={},
            text=tme_html if "t.me/clara_free" in url else "<html><body>No TG</body></html>",
        ),
    )

    # 1) allow_paid_tier2=False with Stage 0 links_explicit TG still resolves via free HTTP, 0 paid calls!
    out_stage0 = contact_waterfall.enrich_lead_contacts(
        {
            "company": "FreeStage",
            "project_url": "https://freestage.io",
            "founder_x": "https://x.com/freestage_ceo",
            "links": ["https://t.me/clara_free"],
            "icp_score": 90,
        },
        allow_paid_tier2=False,
        http_get_fn=_empty_http,
    )
    assert out_stage0["username"] == "clara_free"
    assert out_stage0["contact_waterfall_tier"] == "tier1"
    assert paid_calls == []

    # 2) allow_paid_tier2=False WITHOUT explicit link still scrapes website (free HTTP), 0 paid DM/search/X calls!
    out_no_link = contact_waterfall.enrich_lead_contacts(
        {
            "company": "PaidBlocked",
            "project_url": "https://paidblocked.io",
            "founder_x": "https://x.com/paidblocked_ceo",
            "icp_score": 92,
        },
        allow_paid_tier2=False,
        http_get_fn=lambda url, **kw: SimpleNamespace(
            status_code=200,
            text='<a href="mailto:ceo@paidblocked.io">Email</a>',
        ),
    )
    assert out_no_link["work_email"] == "ceo@paidblocked.io"
    assert paid_calls == []


def test_budget_guard_denying_spend_blocks_paid_bridge_x_and_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leadgen import monid_client, tg_identity_bridge, treg_client

    paid_calls: list[str] = []
    guard_checks: list[tuple[str, str, float]] = []

    monkeypatch.setattr(treg_client, "check_treg_ready", lambda **kw: True)
    monkeypatch.setattr(monid_client, "check_monid_ready", lambda **kw: True)
    monkeypatch.setattr(
        treg_client,
        "call",
        lambda ep, **kw: paid_calls.append(f"treg:{ep}") or {},
    )
    monkeypatch.setattr(
        contact_waterfall,
        "treg_call",
        lambda ep, **kw: paid_calls.append(f"treg:{ep}") or {},
    )
    monkeypatch.setattr(
        monid_client,
        "run_monid",
        lambda prov, ep, **kw: paid_calls.append(f"monid:{prov}{ep}") or {},
    )
    monkeypatch.setattr(
        tg_identity_bridge.requests,
        "get",
        lambda url, **kw: SimpleNamespace(status_code=404, is_redirect=False, headers={}, text=""),
    )

    def deny_all_guard(provider: str, endpoint: str, est_cost: float) -> bool:
        guard_checks.append((provider, endpoint, est_cost))
        return False

    out = contact_waterfall.enrich_lead_contacts(
        {
            "company": "GuardBridgeCo",
            "project_url": "https://guardbridge.io",
            "founder_x": "https://x.com/guardbridge_ceo",
            "icp_score": 91,
        },
        allow_paid_tier2=True,
        http_get_fn=_empty_http,
        budget_guard_fn=deny_all_guard,
    )
    assert paid_calls == []
    assert any(ep == "crawl4ai.web.search" for _, ep, _ in guard_checks)
    assert any(ep == "twitterdata.io:/users" for _, ep, _ in guard_checks)
    assert any(ep == "scrapecreators.x.user.profile" for _, ep, _ in guard_checks)
    assert out["lead_cost"] == 0.0

    # Also test direct bridge_lead_to_telegram with cost_record_fn + monid_fn/treg_fn when guard allows
    recorded: list[tuple[str, str, float, bool]] = []
    tme_ok = (
        '<html><div class="tgme_page_title">Alex</div>'
        '<div class="tgme_page_description">Founder of GuardBridge</div>'
        '<div class="tgme_page_extra">@alex_gb</div></html>'
    )
    monkeypatch.setattr(
        tg_identity_bridge.requests,
        "get",
        lambda url, **kw: SimpleNamespace(
            status_code=200,
            is_redirect=False,
            headers={},
            text=tme_ok if "t.me/alex_gb" in url else "",
        ),
    )
    bridged = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "company": "GuardBridge",
            "project_url": "https://guardbridge.io",
            "founder_x": "https://x.com/alex_x",
        },
        allow_paid=True,
        budget_guard_fn=lambda p, ep, est: True,
        cost_record_fn=lambda *, provider, endpoint, lead_key, cost_usd, hit: recorded.append(
            (provider, endpoint, cost_usd, hit)
        ),
        monid_fn=lambda prov, ep, **kw: {"bio": "TG: https://t.me/alex_gb"},
        treg_fn=lambda ep, **kw: {},
    )
    assert bridged["username"] == "alex_gb"
    assert ("monid", "twitterdata.io:/users", 0.001, True) in recorded


