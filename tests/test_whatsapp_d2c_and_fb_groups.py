from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from app.db import apply_whatsapp_d2c_and_fb_groups_v9, connect, init_db
from app.leadgen_settings import (
    DEFAULT_LEADGEN_CONFIG,
    get_leadgen_config,
    get_leadgen_status,
    push_discovered_now,
)
from leadgen import crm_mcp_push, high_yield_veins, ofm_and_groups, tg_to_gsheet, vps_autonomous_replenish


def test_whatsapp_url_and_d2c_whatsapp_vein_filters_and_extracts() -> None:
    # 1) Handles wa.me, wa.me/c/<digits>, api.whatsapp.com, and Shopify WhatsApp / Tel E.164 text
    assert tg_to_gsheet.whatsapp_url("https://wa.me/c/917397366251") == "https://wa.me/917397366251"
    assert tg_to_gsheet.whatsapp_url("WhatsApp / Tel NL: +31 648462612 · hello@witlofskincare.com") == "https://wa.me/31648462612"
    assert tg_to_gsheet.whatsapp_url("https://wa.me/your-whatsapp-number") == ""
    assert tg_to_gsheet.whatsapp_url("+1111111111") == ""

    # 2) collect_d2c_whatsapp_brands keeps verified Shopify D2C skincare & supplement brands
    #    while rejecting local massage/cat-hotel clinics and +7 RU numbers
    search_hits = {
        "results": [
            {
                "url": "https://witlofskincare.nl/en/policies/contact-information",
                "title": "Contact information – Witlof Skincare",
                "snippet": "WhatsApp / Tel NL: +31 648462612 · hello@witlofskincare.com",
            },
            {
                "url": "https://linktr.ee/tauraeskin_demo",
                "title": "Taurae Skin Links",
                "snippet": "Luxury natural skincare store",
            },
            {
                "url": "https://www.forestlifecathotel.com/en/pages/contact",
                "title": "Forest Life Cat Hotel",
                "snippet": "wa.me/85267142741. Please bring L-Lysine supplements for your cat hotel stay",
            },
            {
                "url": "https://ruskincare.example/pages/contact",
                "title": "RU Skin Store",
                "snippet": "WhatsApp: +7 999 123 45 67 · Powered by Shopify",
            },
            {
                "url": "https://rubrand.ru/pages/contact",
                "title": "RuBrand",
                "snippet": "WhatsApp: +31 612345678",
            },
        ]
    }
    pages = {
        "https://witlofskincare.nl/products.json?limit=3": json.dumps(
            {"products": [{"title": "Refreshing Cleanser"}, {"title": "Nourishing Facial Cream"}]}
        ),
        "https://witlofskincare.nl/en/policies/contact-information": (
            "<html><script>Shopify.theme={};</script>"
            "Contact Witlof Skincare. WhatsApp / Tel NL: +31 648462612 email sarah@witlofskincare.nl</html>"
        ),
        "https://linktr.ee/tauraeskin_demo": (
            '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<script src="https://assets.production.linktr.ee/web.js"></script>'
            '<script src="https://www.googletagmanager.com/gtm.js"></script>'
            '<svg xmlns="http://www.w3.org/2000/svg"></svg></head><body>'
            '<a href="https://tauraeskin.com/collections/all">Shop Skincare</a>'
            '<a href="https://wa.me/c/447911223344">Order on WhatsApp</a></body></html>'
        ),
        "https://tauraeskin.com/products.json?limit=3": json.dumps(
            {"products": [{"title": "Whipped Body Scrub"}]}
        ),
        "https://tauraeskin.com/collections/all": "<html><script src='https://cdn.shopify.com/s/app.js'></script></html>",
        "https://www.forestlifecathotel.com/products.json?limit=3": json.dumps({"products": [{"title": "Cat stay"}]}),
        "https://www.forestlifecathotel.com/en/pages/contact": "<html>Powered by Shopify wa.me/85267142741</html>",
        "https://glowmedspa.example/pages/contact": "<html>Glow Medspa shop now WhatsApp: +44 7911 555666</html>",
        "https://ruskincare.example/products.json?limit=3": json.dumps({"products": [{"title": "Serum"}]}),
        "https://ruskincare.example/pages/contact": "<html>Shopify.theme whatsapp +79991234567</html>",
        "https://silfearth.com/products.json?limit=3": json.dumps({"products": [{"title": "Organic Lip Balm"}]}),
        "https://silfearth.com/pages/contact": "<html><script>Shopify.theme={};</script>WhatsApp: +7 999 000 11 22 / WhatsApp UK: +44 7700 900777</html>",
    }
    search_hits["results"].extend(
        [
            {
                "url": "https://www.instagram.com/silfearth_official/",
                "title": "Silf Earth (@silfearth_official)",
                "snippet": "Clean botanical balm skincare silfearth.com · WhatsApp wa.me/447700900777",
            },
            {
                "url": "https://glowmedspa.example/pages/contact",
                "title": "Glow Aesthetics Medspa",
                "snippet": "Facial clinic · WhatsApp +44 7911 555666",
            },
        ]
    )

    rows, seen = high_yield_veins.collect_d2c_whatsapp_brands(
        limit=5,
        day=date(2026, 11, 17),
        seen={},
        call_budget=[10],
        search_fn=lambda q: search_hits if "skincare" in q or "linktr.ee" in q or "instagram.com" in q else {"results": []},
        http_get_fn=lambda u: SimpleNamespace(status_code=200, text=pages.get(u, "")) if u in pages else SimpleNamespace(status_code=404, text=""),
    )
    assert len(rows) == 3
    by_dom = {r["project_url"]: r for r in rows}
    w_row = by_dom["https://witlofskincare.nl"]
    assert w_row["name"] == "Witlof Skincare"
    assert w_row["category"] == "E-com / D2C"
    assert w_row["phone"] == "+31648462612"
    assert w_row["whatsapp"] == "https://wa.me/31648462612"
    assert w_row["provider"] == "whatsapp"
    assert w_row["work_email"] == "sarah@witlofskincare.nl"
    assert w_row["source"] == "d2c_whatsapp"
    t_row = by_dom["https://tauraeskin.com"]
    assert t_row["phone"] == "+447911223344"
    assert t_row["whatsapp"] == "https://wa.me/447911223344"
    s_row = by_dom["https://silfearth.com"]
    assert s_row["phone"] == "+447700900777"
    assert "d2c_wa:witlofskincare.nl" in seen
    assert "d2c_wa:tauraeskin.com" in seen
    assert "d2c_wa:silfearth.com" in seen

    # 3) Toll-free WA filter & SSRF validation on _validate_public_http_url
    assert tg_to_gsheet.whatsapp_url("WhatsApp: +1 800 555 0199") == ""
    assert high_yield_veins._validate_public_http_url("http://127.0.0.1:18084/health") == ""
    assert high_yield_veins._validate_public_http_url("http://169.254.169.254/latest/meta-data/") == ""
    assert high_yield_veins._validate_public_http_url("https://user:pass@witlofskincare.nl/") == ""


def test_fb_groups_extracts_anyapi_nested_output_and_whatsapp_crm_routing(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")

    # 1) Regression: top-level empty "items": [] must NOT shadow output.data.posts!
    nested_payload = {
        "items": [],
        "output": {
            "data": {
                "posts": [
                    {
                        "id": "fb1",
                        "authorName": "Maya Lin",
                        "text": "Scaled our skincare Shopify store https://glowskin.co — looking for UGC creators, WhatsApp: https://wa.me/c/447700900123",
                    }
                ]
            }
        },
        "provider": "anyapi",
    }
    posts = ofm_and_groups._extract_fb_posts(nested_payload)
    assert len(posts) == 1
    fb_res = ofm_and_groups.collect_facebook_groups(
        group_urls=["https://www.facebook.com/groups/ShopifyStorePros"],
        state_dir=state_dir,
        treg_fn=lambda ep, **kw: nested_payload if ep == "anyapi.facebook.group.posts" else {},
    )
    assert len(fb_res["b2b_rows"]) == 1
    assert fb_res["b2b_rows"][0]["whatsapp"] == "https://wa.me/447700900123"

    # Regression: Fansly / "OF model" posts must NEVER leak into b2b_rows
    ofm_leak_payload = {
        "output": {
            "data": {
                "posts": [
                    {
                        "id": "fb_ofm_1",
                        "authorName": "OFM Guy",
                        "text": "Fansly agency looking for reels traffic https://myagency.co WhatsApp: wa.me/447700900555",
                    },
                    {
                        "id": "fb_ofm_2",
                        "authorName": "Paris Model Mgr",
                        "text": "Need UGC clips for our OF model https://ofmodel.co WhatsApp: wa.me/447700900666",
                    },
                ]
            }
        }
    }
    ofm_leak_res = ofm_and_groups.collect_facebook_groups(
        group_urls=["https://www.facebook.com/groups/startugc"],
        state_dir=state_dir,
        treg_fn=lambda ep, **kw: ofm_leak_payload if ep == "anyapi.facebook.group.posts" else {},
    )
    assert ofm_leak_res["b2b_rows"] == []

    # 2) Unmocked pipeline survival: collect_d2c -> admit -> tg_enrich -> tg_llm_qualify.norm_result -> tg_curate
    from leadgen import tg_curate, tg_enrich_leads, tg_identity_bridge, tg_llm_qualify

    d2c_admitted = {
        "id": "d2c_wa:witlofskincare.nl",
        "name": "Witlof Skincare",
        "company": "Witlof Skincare",
        "project": "Witlof Skincare",
        "project_url": "https://witlofskincare.nl",
        "url": "https://witlofskincare.nl",
        "whatsapp": "https://wa.me/31648462612?text=Promo%202026",
        "phone": "+31648462612",
        "provider": "whatsapp",
        "username": "",
        "bio": "D2C Shopify brand (Refreshing Cleanser): Natural skincare studio",
        "category": "E-com / D2C",
        "monetization": "ecom",
        "role": "Founder",
        "roles": ["Founder"],
        "icp_score": 88,
        "has_monetization_signal": True,
        "their_angle": "D2C Shopify natural skincare brand",
        "what_they_do": "D2C Shopify natural skincare brand",
        "source": "d2c_whatsapp",
        "lead_source": "d2c_whatsapp",
    }
    enriched_rows = tg_enrich_leads.enrich_rows([d2c_admitted], fetch=False, skip_copy=True)
    assert len(enriched_rows) == 1
    assert enriched_rows[0]["category"] == "E-com / D2C"
    assert tg_llm_qualify.norm_result({"is_lead": True, "icp_score": 88, "category": "Web3 (общее)", "monetization": "ecom", "role": "Founder"})["category"] == "E-com / D2C"
    curated_rows = tg_curate.curate_rows(enriched_rows)
    assert len(curated_rows) == 1
    assert curated_rows[0]["category"] == "E-com / D2C"
    assert tg_to_gsheet.keep_for_sheet(curated_rows[0]) is True

    # 3) TG bridge overrides provider="whatsapp" -> "telegram"; bare office phone does NOT turn into WA
    bridged_row = tg_identity_bridge.bridge_lead_to_telegram(
        dict(curated_rows[0]),
        x_profile_fn=lambda h: {},
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: '<html><meta property="og:title" content="Witlof Founder"/><meta property="og:description" content="Founder of witlofskincare.nl"/></html>' if nick == "witlofskincare" else "",
    )
    if bridged_row.get("username"):
        assert bridged_row["provider"] == "telegram"
    bridged_payload = crm_mcp_push.build_lead_payload_from_dict(
        {**d2c_admitted, "username": "witlof_founder"},
        touch_mode="direct",
    )
    assert bridged_payload["provider"] == "telegram"

    payload = crm_mcp_push.build_lead_payload_from_dict(d2c_admitted, touch_mode="direct")
    assert payload["provider"] == "whatsapp"
    assert payload["phone"] == "+31648462612"

    final_jsonl = state_dir / "raw_20261117_225000.final.jsonl"
    office_phone_row = {
        "id": "office_phone_only",
        "name": "App Founder",
        "company": "AppFlow",
        "project": "AppFlow",
        "project_url": "https://appflow.io",
        "work_email": "ceo@appflow.io",
        "phone": "+14155550199",
        "username": "",
        "category": "SaaS / AI app",
        "role": "Founder",
        "roles": ["Founder"],
        "icp_score": 88,
        "source": "yc_ph",
    }
    final_jsonl.write_text(
        json.dumps(curated_rows[0], ensure_ascii=False)
        + "\n"
        + json.dumps(office_phone_row, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    split_out = vps_autonomous_replenish.split_tg_and_direct_x_leads(
        final_jsonl, state_dir / "direct_x_leads.jsonl"
    )
    assert split_out["tg_wa_kept"] == 1
    assert split_out["direct_x_added"] == 1
    kept_row = json.loads(final_jsonl.read_text(encoding="utf-8").strip())
    assert kept_row["phone"] == "+31648462612"
    assert kept_row["provider"] == "whatsapp"

    # 4) v9 JSON-only config migration + mcp_server WA isolation against SoftUGC — База Все2 + UI counts
    cfg_json = tmp_path / "leadgen_config.json"
    cfg_json.write_text(json.dumps({"b2b": {"sources": {"meta_ads": True, "d2c_whatsapp": False}, "fb_group_urls": []}}), encoding="utf-8")

    conn = connect(tmp_path / "crm_wa_route.db")
    init_db(conn)
    from app.outreach import create_campaign
    from app.mcp_server import _handle_ingest_discovered_leads

    create_campaign(conn, name="SoftUGC — База Все2", daily_limit=30, status="running", leadgen_enabled=True)
    conn.execute("DELETE FROM kv_settings WHERE key IN ('whatsapp_d2c_and_fb_groups_v9', 'leadgen_config')")
    conn.commit()
    v9_res = apply_whatsapp_d2c_and_fb_groups_v9(conn)
    assert v9_res["already_applied"] is False
    assert apply_whatsapp_d2c_and_fb_groups_v9(conn)["already_applied"] is True
    cfg_after = get_leadgen_config(conn)
    assert cfg_after["b2b"]["sources"]["d2c_whatsapp"] is True
    assert len(cfg_after["b2b"]["fb_group_urls"]) == len(DEFAULT_LEADGEN_CONFIG["b2b"]["fb_group_urls"])

    # Direct MCP call naming SoftUGC — База Все2 with WA-only lead must NOT enroll into Все2!
    mcp_res = _handle_ingest_discovered_leads(
        conn,
        {
            "leads": [
                {
                    "name": "Taurae Skin",
                    "company": "Taurae Skin",
                    "phone": "+447911223344",
                    "provider": "whatsapp",
                    "website": "https://tauraeskin.com",
                    "category": "E-com / D2C",
                    "their_angle": "D2C Shopify natural skincare store",
                    "lead_source": "d2c_whatsapp",
                    "step1_text": "Saw Taurae Skin. You're probably running into ad CPMs...",
                    "icp_score": 88,
                }
            ],
            "campaign_name": "SoftUGC — База Все2",
            "enroll_in_campaign": True,
        },
    )
    assert mcp_res["enrolled_in_campaign"] == 1

    push_res = push_discovered_now(conn)
    assert push_res["ingested"] == 1

    wa_lead = conn.execute(
        "SELECT id, phone, provider, lead_source, url FROM leads WHERE phone = '+31648462612'"
    ).fetchone()
    assert wa_lead is not None
    assert wa_lead["provider"] == "whatsapp"
    assert wa_lead["lead_source"] == "d2c_whatsapp"

    wa_camp = conn.execute(
        "SELECT id, name, status FROM campaigns WHERE name = 'Whales EN — WA'"
    ).fetchone()
    assert wa_camp is not None
    assert wa_camp["status"] == "draft"
    wa_rec_cnt = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'pending'",
        (int(wa_camp["id"]),),
    ).fetchone()[0]
    assert wa_rec_cnt == 2

    vse2_rec_cnt = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients cr JOIN campaigns c ON c.id = cr.campaign_id WHERE c.name = 'SoftUGC — База Все2'"
    ).fetchone()[0]
    assert vse2_rec_cnt == 0

    status = get_leadgen_status(conn)
    assert status["whatsapp_leads_count"] == 2
    assert status["crm_leadgen_counts"]["whatsapp_leads_count"] == 2
    assert status["crm_leadgen_counts"]["whales_leads"] == 2
    assert status["crm_leadgen_counts"]["b2b_leads"] == 2
    conn.close()


def test_crm_mcp_push_jsonl_splits_whatsapp_from_tg_campaign(tmp_path: Path, monkeypatch) -> None:
    mixed_jsonl = tmp_path / "mixed.jsonl"
    mixed_jsonl.write_text(
        json.dumps(
            {
                "name": "Alice Founder",
                "username": "alice_saas",
                "company": "ClipPulse",
                "project": "ClipPulse",
                "website": "https://clippulse.ai",
                "category": "SaaS / AI app",
                "their_angle": "AI video clipper subscription app",
                "lead_source": "yc_ph",
                "roles": ["Founder"],
                "icp_score": 88,
            },
            ensure_ascii=False,
        )
        + "\n"
        + json.dumps(
            {
                "name": "Witlof Skincare",
                "company": "Witlof Skincare",
                "project": "Witlof Skincare",
                "website": "https://witlofskincare.nl",
                "whatsapp": "https://wa.me/31648462612",
                "category": "E-com / D2C",
                "their_angle": "Natural skincare D2C Shopify store",
                "lead_source": "d2c_whatsapp",
                "roles": ["Founder"],
                "icp_score": 88,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "nonexistent_cfg.json"))
    recorded_calls: list[dict[str, object]] = []

    def _fake_push(leads_slice, *, enroll_in_campaign=True, campaign_id=None, campaign_name="Leadgen Auto (TG)", mcp_url=None):
        recorded_calls.append({"leads": leads_slice, "enroll": enroll_in_campaign, "campaign_name": campaign_name})
        return {"processed": len(leads_slice), "created_leads": len(leads_slice), "updated_leads": 0, "enrolled_in_campaign": len(leads_slice), "campaign_id": len(recorded_calls)}

    monkeypatch.setattr(crm_mcp_push, "push_leads_via_mcp", _fake_push)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "crm_mcp_push.py",
            "--jsonl",
            str(mixed_jsonl),
            "--campaign-name",
            "SoftUGC — База Все2",
            "--campaign-name-wa",
            "Leadgen Auto (WA)",
        ],
    )
    rc = crm_mcp_push.main()
    assert rc == 0
    assert len(recorded_calls) == 2
    by_camp = {c["campaign_name"]: c["leads"] for c in recorded_calls}
    assert len(by_camp["SoftUGC — База Все2"]) == 1
    assert by_camp["SoftUGC — База Все2"][0]["username"] == "alice_saas"
    assert len(by_camp["Leadgen Auto (WA)"]) == 1
    assert by_camp["Leadgen Auto (WA)"][0]["provider"] == "whatsapp"
    assert by_camp["Leadgen Auto (WA)"][0]["phone"] == "+31648462612"
