"""Tests for Wave A Track 3: tg_identity_bridge, Multi-DM contact_waterfall, and backfill_direct_x."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from app.db import connect, init_db  # noqa: E402
from app.leadgen_settings import DEFAULT_WHALES_TG_STEPS, save_leadgen_config  # noqa: E402
from app.outreach import create_campaign  # noqa: E402
from leadgen import backfill_direct_x, contact_waterfall, tg_identity_bridge  # noqa: E402
from leadgen.language_router import CAMPAIGN_WHALES_EN_TG  # noqa: E402


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


def _channel_tme_html(username: str, title: str, subs: str = "4 200 subscribers") -> str:
    return (
        "<html><body>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">{subs}</div>'
        f'<div class="tgme_page_description">Official channel @{username}</div>'
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# 1. tg_identity_bridge: @the_insider fix, tg_handle_is_guessed, 7d cache, parallel
# ---------------------------------------------------------------------------


def test_tg_identity_bridge_rejects_the_insider_and_pseudo_names() -> None:
    tried_nicks: list[str] = []

    def spy_tme(nick: str) -> str:
        tried_nicks.append(nick)
        return _personal_tme_html(nick, "The Insider", "Daily wellness news & reviews")

    pseudo_rows = [
        {"name": "The Insider", "company": "AcmeBrand", "project_url": "https://acmebrand.io"},
        {"founder_name": "The Insider", "company": "AcmeBrand", "project_url": "https://acmebrand.io"},
        {"name": "The Insider", "company": "The Insider", "project_url": "https://theinsidernews.io"},
        {"name": "Best Skincare", "company": "GlowCo", "project_url": "https://glowco.io"},
        {"name": "A Daily Digest", "company": "DigestCo", "project_url": "https://digestco.io"},
        {"name": "Top Wellness Labs", "company": "PulseCo", "project_url": "https://pulseco.io"},
        {"name": "Our Beauty Club", "company": "ClubCo", "project_url": "https://clubco.io"},
        {
            "name": "Acme",
            "company": "Acme",
            "project_url": "https://acme.io",
            "founder_linkedin": "https://www.linkedin.com/in/the-insider-123456",
        },
        {
            "name": "The Insider",
            "company": "AcmeBrand",
            "project_url": "https://acmebrand.io",
            "founder_x": "https://x.com/acmebrand",
        },
    ]

    for row in pseudo_rows:
        assert tg_identity_bridge._infer_founder_name_from_row(row, row.get("company", "")) == ""
        res = tg_identity_bridge.bridge_lead_to_telegram(
            row,
            x_profile_fn=lambda _h: {"name": "The Insider", "bio": "Wellness & supplements"},
            search_fn=lambda _q: {},
            fetch_site_fn=lambda _d: "",
            tme_fetch_fn=spy_tme,
        )
        assert not res.get("username"), f"False positive resolved for pseudo row: {row} -> {res}"

    assert "the_insider" not in tried_nicks
    assert "theinsider" not in tried_nicks
    assert "best_skincare" not in tried_nicks


def test_tg_identity_bridge_tg_handle_is_guessed_flag() -> None:
    pages = {
        "explicit_link": _personal_tme_html("explicit_link", "Alex", "Founder"),
        "xbio_dm": _personal_tme_html("xbio_dm", "Sam", "CEO"),
        "site_dm": _personal_tme_html("site_dm", "Cara", "Founder"),
        "dom_dm": _personal_tme_html("dom_dm", "Dan", "CEO"),
        "dana_smith": _personal_tme_html("dana_smith", "Dana Smith", "Founder"),
        "elena_jones": _personal_tme_html("elena_jones", "Elena Jones", "Founder"),
        "ivan_petrov": _personal_tme_html("ivan_petrov", "Ivan Petrov", "Founder"),
        "olga_kim": _personal_tme_html("olga_kim", "Olga Kim", "Founder"),
        "ig_brand": _personal_tme_html("ig_brand", "Nina", "Building GlowUp.io"),
    }

    # Explicit links -> tg_handle_is_guessed == False
    r_links = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Alpha", "project_url": "https://alpha.io", "links": ["https://t.me/explicit_link"]},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_links["tg_bridge_match"] == "links_explicit"
    assert r_links["tg_handle_is_guessed"] is False

    r_xbio = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Beta", "project_url": "https://beta.io", "founder_x": "@beta_x"},
        x_profile_fn=lambda _h: {"bio": "TG https://t.me/xbio_dm"},
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_xbio["tg_bridge_match"] == "x_bio_explicit"
    assert r_xbio["tg_handle_is_guessed"] is False

    r_site = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Gamma", "project_url": "https://gamma.io"},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: '<a href="https://t.me/site_dm">TG</a>',
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_site["tg_bridge_match"] == "website_direct"
    assert r_site["tg_handle_is_guessed"] is False

    r_dom = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Delta", "project_url": "https://delta.io"},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: {"results": [{"url": "https://t.me/dom_dm", "snippet": "delta.io founder"}]},
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_dom["tg_bridge_match"] == "domain_search_explicit"
    assert r_dom["tg_handle_is_guessed"] is False

    # Stage 4 heuristics -> tg_handle_is_guessed == True
    r_founder = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Zeta", "project_url": "https://zeta.io", "founder_name": "Dana Smith"},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_founder["tg_bridge_match"] == "founder_handle_confirmed"
    assert r_founder["tg_handle_is_guessed"] is True

    r_li = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "company": "Eta",
            "project_url": "https://eta.io",
            "founder_linkedin": "https://www.linkedin.com/in/elena-jones-1234",
        },
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_li["tg_bridge_match"] == "linkedin_handle_confirmed"
    assert r_li["tg_handle_is_guessed"] is True

    r_xf = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "Theta", "project_url": "https://theta.io", "founder_x": "@theta_co"},
        x_profile_fn=lambda _h: {"name": "Ivan Petrov", "bio": "Founder"},
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_xf["tg_bridge_match"] == "x_founder_handle"
    assert r_xf["tg_handle_is_guessed"] is True

    r_email = tg_identity_bridge.bridge_lead_to_telegram(
        {"name": "Olga Kim", "company": "Iota", "project_url": "https://iota.io", "work_email": "olga.kim@iota.io"},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_email["tg_bridge_match"] == "email_handle_confirmed"
    assert r_email["tg_handle_is_guessed"] is True

    r_ig = tg_identity_bridge.bridge_lead_to_telegram(
        {"company": "GlowUp", "project_url": "https://glowup.io", "instagram_url": "https://instagram.com/ig.brand"},
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=lambda n: pages.get(n, ""),
    )
    assert r_ig["tg_bridge_match"] == "instagram_handle_confirmed"
    assert r_ig["tg_handle_is_guessed"] is True


def test_default_tme_fetch_7d_cache_strict_5s_timeout_and_thread_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))

    http_calls: list[tuple[str, float, bool]] = []
    call_lock = threading.Lock()

    def fake_get(url: str, *, headers=None, timeout=None, allow_redirects=True):
        with call_lock:
            http_calls.append((url, float(timeout), bool(allow_redirects)))
        nick = url.rsplit("/", 1)[-1]
        if nick == "cached_chan":
            return SimpleNamespace(
                status_code=200,
                is_redirect=False,
                headers={},
                text=_channel_tme_html("cached_chan", "Channel"),
            )
        if nick == "transient_err":
            return SimpleNamespace(status_code=503, is_redirect=False, headers={}, text="Retry later")
        return SimpleNamespace(
            status_code=200,
            is_redirect=False,
            headers={},
            text=_personal_tme_html(nick, f"User {nick}", "Building Acme.io"),
        )

    monkeypatch.setattr(tg_identity_bridge.requests, "get", fake_get)

    # 1) Cache miss -> strict 5s timeout + allow_redirects=False -> writes tme_probe_cache.json
    resp1 = tg_identity_bridge._default_tme_fetch("alice_dm")
    ok1, title1, bio1 = tg_identity_bridge._parse_tme_response(resp1, "alice_dm")
    assert ok1 is True
    assert title1 == "User alice_dm"
    assert "Acme.io" in bio1
    assert len(http_calls) == 1
    assert http_calls[0][1] == pytest.approx(5.0)
    assert http_calls[0][2] is False

    cache_file = state_dir / "tme_probe_cache.json"
    assert cache_file.is_file()
    cache_obj = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cache_obj["alice_dm"]["kind"] == "user"

    # 2) Cache hit -> ZERO HTTP calls
    resp2 = tg_identity_bridge._default_tme_fetch("alice_dm")
    ok2, title2, bio2 = tg_identity_bridge._parse_tme_response(resp2, "alice_dm")
    assert ok2 is True
    assert title2 == "User alice_dm"
    assert "Acme.io" in bio2
    assert len(http_calls) == 1

    # 3) Non-user cached as channel -> returns empty on second call without HTTP
    tg_identity_bridge._default_tme_fetch("cached_chan")
    assert len(http_calls) == 2
    assert tg_identity_bridge._default_tme_fetch("cached_chan") == ""
    assert len(http_calls) == 2

    # 4) 503 transient failure is NOT cached
    tg_identity_bridge._default_tme_fetch("transient_err")
    tg_identity_bridge._default_tme_fetch("transient_err")
    assert len(http_calls) == 4

    # 5) Expired (> 7 days) entry triggers refresh
    stale = json.loads(cache_file.read_text(encoding="utf-8"))
    stale["alice_dm"]["ts"] = time.time() - (tg_identity_bridge.TME_CACHE_TTL_SEC + 60)
    cache_file.write_text(json.dumps(stale), encoding="utf-8")
    tg_identity_bridge._default_tme_fetch("alice_dm")
    assert len(http_calls) == 5

    # 6) Concurrent writes across 12 handles keep cache valid JSON
    threads = [
        threading.Thread(target=tg_identity_bridge._default_tme_fetch, args=(f"thread_user_{i}",))
        for i in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final_cache = json.loads(cache_file.read_text(encoding="utf-8"))
    for i in range(12):
        assert final_cache[f"thread_user_{i}"]["kind"] == "user"


def test_enrich_rows_with_telegram_bridge_parallel_order_and_checkpoints(tmp_path: Path) -> None:
    rows = [
        {"id": "skip_tg", "username": "existing_tg", "project_url": "https://skip.io"},
    ]
    for idx in range(10):
        rows.append(
            {
                "id": f"lead_{idx}",
                "company": f"Brand{idx}",
                "project_url": f"https://brand{idx}.io",
                "founder_x": f"@founder_{idx}",
            }
        )
    rows.append({"id": "over_cap", "company": "OverCap", "project_url": "https://overcap.io", "founder_x": "@over"})

    active_lock = threading.Lock()
    active_count = 0
    max_concurrent = 0

    def slow_tme(nick: str) -> str:
        nonlocal active_count, max_concurrent
        with active_lock:
            active_count += 1
            if active_count > max_concurrent:
                max_concurrent = active_count
        # Reverse sleep so later indices finish before earlier ones -> tests order preservation
        if nick.startswith("founder_"):
            num = int(nick.split("_")[1])
            time.sleep(0.03 * (10 - num))
        with active_lock:
            active_count -= 1
        return _personal_tme_html(nick, "Alex", f"Building {nick.replace('founder_', 'brand')}.io")

    ckpt_jsonl = tmp_path / "bridge_ckpt.jsonl"
    out = tg_identity_bridge.enrich_rows_with_telegram_bridge(
        rows,
        max_rows=10,
        max_workers=8,
        checkpoint_path=ckpt_jsonl,
        x_profile_fn=lambda _h: None,
        search_fn=lambda _q: None,
        fetch_site_fn=lambda _d: "",
        tme_fetch_fn=slow_tme,
    )

    assert len(out) == 12
    assert [r["id"] for r in out] == [r["id"] for r in rows]
    assert out[0]["username"] == "existing_tg"
    for idx in range(10):
        assert out[idx + 1]["username"] == f"founder_{idx}"
        assert out[idx + 1]["tg_handle_is_guessed"] is True
    assert not out[-1].get("username")
    assert max_concurrent >= 2

    ckpt_lines = [json.loads(ln) for ln in ckpt_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(ckpt_lines) == 12
    assert [r["id"] for r in ckpt_lines] == [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# 2. contact_waterfall: Multi-DM 3 roles, elif-not-li_url fix, limadata, thresholds
# ---------------------------------------------------------------------------


def test_contact_waterfall_multi_dm_3_roles_limadata_and_bridge_promotion() -> None:
    assert contact_waterfall.MAX_DM_CANDIDATES == 3
    assert contact_waterfall.COST_LIMADATA_EMP_USD == pytest.approx(0.002)
    assert contact_waterfall.CONTACTOUT_VERIFIED_MIN_ICP == 80

    # Role priority: Founder/CEO > CMO/Head of Growth/Marketing > Paid Social/Performance > Engineer
    picked = contact_waterfall._pick_decision_makers(
        {
            "results": [
                {"full_name": "Dan Dev", "title": "Senior Backend Engineer", "linkedin_url": "https://linkedin.com/in/dan-dev"},
                {"full_name": "Paula Paid", "title": "Paid Social Lead", "linkedin_url": "https://linkedin.com/in/paula-paid"},
                {"full_name": "Gabe Growth", "title": "Head of Growth", "linkedin_url": "https://linkedin.com/in/gabe-growth"},
                {"full_name": "Fiona Founder", "title": "Founder & CEO", "linkedin_url": "https://linkedin.com/in/fiona-founder"},
            ]
        }
    )
    assert len(picked) == 3
    assert [p["founder_name"] for p in picked] == ["Fiona Founder", "Gabe Growth", "Paula Paid"]

    # Bug fix: even when founder_linkedin IS ALREADY POPULATED on row + personal email exists,
    # Tier 2 still queries limadata.people.employees.search for 2nd/3rd DMs and bridges them until TG is found!
    treg_calls: list[tuple[str, dict[str, Any]]] = []
    bridge_names: list[str] = []

    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        treg_calls.append((ep, kw))
        if ep == "limadata.people.employees.search":
            return {
                "results": [
                    {
                        "full_name": "Fiona Founder",
                        "title": "Founder & CEO",
                        "linkedin_url": "https://www.linkedin.com/in/fiona-founder",
                        "email": "fiona@brandscale.io",
                    },
                    {
                        "full_name": "Gabe Growth",
                        "title": "CMO & Head of Growth",
                        "linkedin_url": "https://www.linkedin.com/in/gabe-growth",
                    },
                    {
                        "full_name": "Paula Paid",
                        "title": "Performance & Paid Social",
                        "linkedin_url": "https://www.linkedin.com/in/paula-paid",
                    },
                ],
                "_treg": {"cost_usd": 0.002},
            }
        return {}

    def fake_bridge(row_in: dict[str, Any]) -> dict[str, Any]:
        nm = str(row_in.get("founder_name") or row_in.get("founder_linkedin") or "")
        bridge_names.append(nm)
        out = dict(row_in)
        if "Paula Paid" in nm or "paula-paid" in nm:
            out["username"] = "paula_paid_tg"
            out["telegram_username"] = "paula_paid_tg"
            out["tg_bridge_match"] = "linkedin_handle_confirmed"
            out["tg_handle_is_guessed"] = True
            out["provider"] = "telegram"
        return out

    out = contact_waterfall.enrich_lead_contacts(
        {
            "company": "BrandScale",
            "project_url": "https://brandscale.io",
            "founder_name": "Fiona Founder",
            "founder_linkedin": "https://www.linkedin.com/in/fiona-founder",
            "work_email": "fiona@brandscale.io",
            "icp_score": 75,
        },
        http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
        treg_fn=fake_treg,
        tg_bridge_fn=fake_bridge,
        budget_guard_fn=lambda _p, _ep, _c: True,
    )

    lima_calls = [kw for ep, kw in treg_calls if ep == "limadata.people.employees.search"]
    assert len(lima_calls) == 1
    assert lima_calls[0]["method"] == "POST"
    assert lima_calls[0]["data"] == {"company_domains": ["brandscale.io"], "limit": 6}
    assert not any(ep == "leadmagic.x.employee-finder" for ep, _ in treg_calls)
    assert out["username"] == "paula_paid_tg"
    assert out["founder_name"] == "Paula Paid"
    assert out["tg_handle_is_guessed"] is True
    assert out["contact_waterfall_tier"] == "tier2"
    # Phone waterfall should NOT run because DM3 resolved TG
    assert not any(ep == "crawl4ai.people.phone.find" for ep, _ in treg_calls)
    sec = json.loads(out["secondary_contact_json"])
    assert sec["name"] == "Fiona Founder"


def test_contact_waterfall_two_dms_with_tg_keeps_top_primary_and_second_in_secondary_json() -> None:
    def multi_dms(_company: str, _domain: str, limit: int = 3) -> list[dict[str, str]]:
        return [
            {
                "founder_name": "Alice CEO",
                "role": "Founder & CEO",
                "founder_linkedin": "https://www.linkedin.com/in/alice-ceo",
                "telegram_username": "alice_ceo_tg",
            },
            {
                "founder_name": "Bob CMO",
                "role": "CMO",
                "founder_linkedin": "https://www.linkedin.com/in/bob-cmo",
                "telegram_username": "bob_cmo_tg",
            },
            {
                "founder_name": "Cara Paid",
                "role": "Paid Social",
                "founder_linkedin": "https://www.linkedin.com/in/cara-paid",
            },
        ][:limit]

    out = contact_waterfall.enrich_lead_contacts(
        {"company": "DualTG", "project_url": "https://dualtg.io", "icp_score": 82},
        http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
        dm_multi_finder_fn=multi_dms,
        treg_fn=lambda ep, **kw: {},
        tg_bridge_fn=lambda r: {**r, "username": r["founder_name"].lower().replace(" ", "_")} if "Alice" in r.get("founder_name", "") else r,
    )
    assert out["username"] == "alice_ceo_tg"
    assert out["founder_name"] == "Alice CEO"
    sec = json.loads(out["secondary_contact_json"])
    assert sec["name"] == "Bob CMO"
    assert sec["telegram_username"] == "bob_cmo_tg"


def test_contact_waterfall_limadata_fallback_to_leadmagic_and_icp_thresholds() -> None:
    eps: list[str] = []

    def treg_fallback(ep: str, **kw: Any) -> dict[str, Any]:
        eps.append(ep)
        if ep == "limadata.people.employees.search":
            return {"results": []}  # empty -> falls back to leadmagic
        if ep == "leadmagic.x.employee-finder":
            return {
                "data": [
                    {"full_name": "Sam CMO", "title": "CMO", "profile_url": "https://www.linkedin.com/in/sam-cmo"},
                ],
                "_treg": {"cost_usd": 0.00375},
            }
        if ep == "contactout.people.phone.available":
            return {"available": True}
        if ep == "contactout.people.contact.phone":
            raise AssertionError("ContactOut $0.25 must NOT run for ICP 75 (< 80)")
        return {}

    out_75 = contact_waterfall.enrich_lead_contacts(
        {
            "company": "MidScore",
            "project_url": "https://midscore.io",
            "founder_linkedin": "https://www.linkedin.com/in/main-founder",
            "work_email": "main@midscore.io",
            "icp_score": 75,
        },
        http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
        treg_fn=treg_fallback,
        tg_bridge_fn=lambda r: r,
    )
    assert "limadata.people.employees.search" in eps
    assert "leadmagic.x.employee-finder" in eps
    assert "contactout.people.contact.phone" not in eps
    assert json.loads(out_75["secondary_contact_json"])["name"] == "Sam CMO"


# ---------------------------------------------------------------------------
# 3. backfill_direct_x.py: retry-v2, canary, dry-run, checkpointing, Rule 5
# ---------------------------------------------------------------------------


def _setup_backfill_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Path, Path]:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    db_path = tmp_path / "crm.db"
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-backfill")
    conn = connect(db_path)
    init_db(conn)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "auto_push_to_crm": True,
            "b2b": {"enabled": True, "target_campaign_tg": CAMPAIGN_WHALES_EN_TG},
            "whales": {"enabled": True},
            "vacancies": {"enabled": False},
            "ru_experts": {"enabled": False},
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        status="running",
        daily_limit=30,
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )
    return conn, state_dir, db_path


def test_backfill_direct_x_dry_run_canary_retry_v2_checkpoint_and_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir, db_path = _setup_backfill_env(tmp_path, monkeypatch)
    dx_path = state_dir / "direct_x_leads.jsonl"
    seed_rows = [
        {
            "id": "skip_v2",
            "company": "AlreadyV2",
            "project_url": "https://alreadyv2.io",
            "icp_score": 88,
            "tg_bridge_v2_attempted_at": "2026-11-22T00:00:00",
        },
        {
            "id": "legacy_v1_admin",
            "company": "GroupAdminBrand",
            "project": "GroupAdminBrand",
            "project_url": "https://groupadmin.io",
            "links": ["https://t.me/groupadmin_chat"],
            "what_they_do": "AI video subscription platform",
            "icp_score": 88,
            "tg_bridge_attempted_at": "2026-11-20T00:00:00",
        },
        {
            "id": "fresh_multi_dm",
            "company": "MultiDMBrand",
            "project": "MultiDMBrand",
            "project_url": "https://multidm.io",
            "founder_linkedin": "https://www.linkedin.com/in/dm1-multidm",
            "work_email": "dm1@multidm.io",
            "what_they_do": "D2C consumer wellness subscription",
            "icp_score": 85,
        },
        {
            "id": "fresh_phone_tg",
            "company": "PhoneTGBrand",
            "project": "PhoneTGBrand",
            "project_url": "https://phonetg.io",
            "phone": "+14155550195",
            "what_they_do": "Consumer AI app",
            "icp_score": 82,
        },
        {
            "id": "fresh_unresolved",
            "company": "NoContactBrand",
            "project": "NoContactBrand",
            "project_url": "https://nocontact.io",
            "what_they_do": "SaaS analytics",
            "icp_score": 78,
        },
    ]
    raw_seed_text = "\n".join(json.dumps(r) for r in seed_rows) + "\n"
    dx_path.write_text(raw_seed_text, encoding="utf-8")

    # 1) --dry-run without --retry-v2: skips skip_v2 AND legacy_v1_admin; canary=1 runs only fresh_multi_dm;
    #    leaves direct_x_leads.jsonl and DB completely untouched!
    def fake_treg(ep: str, **kw: Any) -> dict[str, Any]:
        if ep == "limadata.people.employees.search":
            domains = (kw.get("data") or {}).get("company_domains") or []
            if "multidm.io" in domains:
                return {
                    "results": [
                        {
                            "full_name": "Nina Growth",
                            "title": "Head of Growth",
                            "linkedin_url": "https://www.linkedin.com/in/nina-growth",
                        }
                    ],
                    "_treg": {"cost_usd": 0.002},
                }
        return {}

    def fake_bridge(row_in: dict[str, Any]) -> dict[str, Any]:
        out = dict(row_in)
        if "nina-growth" in str(row_in.get("founder_linkedin") or ""):
            out["username"] = "nina_growth_tg"
            out["telegram_username"] = "nina_growth_tg"
            out["provider"] = "telegram"
            out["tg_bridge_match"] = "linkedin_handle_confirmed"
            out["tg_handle_is_guessed"] = True
        return out

    dry_res = backfill_direct_x.backfill_direct_x_leads(
        conn,
        state_dir=state_dir,
        canary=1,
        retry_v2=False,
        dry_run=True,
        treg_balance_fn=lambda: 10.0,
        monid_balance_fn=lambda: 10.0,
        http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
        treg_fn=fake_treg,
        tg_bridge_fn=fake_bridge,
    )
    assert dry_res["eligible_rows"] == 1
    assert dry_res["attempted"] == 1
    assert dry_res["tg_resolved"] == 1
    assert dry_res["whales_ingested"] == 0
    assert dry_res["rule5_previews"]["dry_run_preview"]["ok"] is True
    assert dry_res["rule5_previews"]["dry_run_preview"]["forbidden_pattern_count"] == 0
    assert dx_path.read_text(encoding="utf-8") == raw_seed_text
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients").fetchone()[0] == 0

    # 2) Live run with --retry-v2: skips skip_v2, processes legacy_v1_admin (tg_group_admins),
    #    fresh_multi_dm (Multi-DM bridge), fresh_phone_tg (phone_checker), and fresh_unresolved (stamped v2)!
    from telethon.tl.types import User

    def fake_resolver(_cli, handle: str):
        return User(id=700001 + len(handle), username=handle, first_name="Founder", bot=False)

    def fake_admin_extractor(_cli, handle: str):
        if handle == "groupadmin_chat":
            return [{"username": "group_founder_tg", "user_id": 77001, "role_guess": "Founder / CEO / Owner"}]
        return []

    class FakeCheckerClient:
        pass

    # Write fake phone_checker session file for resolve_and_route_phone_leads
    sess_dir = tmp_path / "scraper_sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    checker_sess = sess_dir / "phone_checker_1.session"
    with sqlite3.connect(checker_sess) as sc:
        sc.execute("CREATE TABLE sessions (dc_id INT, server_address TEXT, port INT, auth_key BLOB, takeout_id INT)")
        sc.execute("INSERT INTO sessions VALUES (2, '149.154.167.50', 443, ?, NULL)", (os.urandom(256),))
        sc.commit()

    from leadgen.phone_messenger_resolver import ResolvedMessenger
    import leadgen.vps_autonomous_replenish as _repl

    async def fake_resolve_tg(client, phones, **kw):
        return {
            p: ResolvedMessenger(
                phone_e164=p,
                telegram_username="phonetg_founder",
                telegram_user_id=77002,
                tg_check_status="found",
            )
            for p in phones
        }

    monkeypatch.setattr(_repl, "resolve_phones_telegram", fake_resolve_tg)

    live_res = backfill_direct_x.backfill_direct_x_leads(
        conn,
        state_dir=state_dir,
        canary=0,
        retry_v2=True,
        dry_run=False,
        treg_balance_fn=lambda: 10.0,
        monid_balance_fn=lambda: 10.0,
        http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
        treg_fn=fake_treg,
        tg_bridge_fn=fake_bridge,
        tg_client=object(),
        tg_admin_extractor_fn=fake_admin_extractor,
        tg_entity_resolver_fn=fake_resolver,
        tg_checker_client=FakeCheckerClient(),
        scraper_sessions_dir=sess_dir,
    )

    assert live_res["eligible_rows"] == 4
    assert live_res["attempted"] == 4
    assert live_res["tg_resolved"] == 3
    assert live_res["whales_ingested"] == 3
    assert live_res["tg_group_admins_promoted"] == 1
    assert live_res["phones_checked"] == 1
    assert CAMPAIGN_WHALES_EN_TG in live_res["rule5_previews"]
    assert live_res["rule5_previews"][CAMPAIGN_WHALES_EN_TG]["ok"] is True
    assert live_res["rule5_previews"][CAMPAIGN_WHALES_EN_TG]["forbidden_pattern_count"] == 0

    # Verify DB ingested all 3 TG whales into Whales EN — TG
    ingested_users = {
        r[0]
        for r in conn.execute(
            "SELECT username FROM campaign_recipients ORDER BY id"
        ).fetchall()
    }
    assert ingested_users == {"group_founder_tg", "nina_growth_tg", "phonetg_founder"}

    # Verify direct_x_leads.jsonl removed the 3 ingested whales, kept skip_v2 (untouched) and fresh_unresolved (stamped v2)
    remaining = [json.loads(ln) for ln in dx_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_id = {r["id"]: r for r in remaining}
    assert set(by_id.keys()) == {"skip_v2", "fresh_unresolved"}
    assert by_id["skip_v2"]["tg_bridge_v2_attempted_at"] == "2026-11-22T00:00:00"
    assert str(by_id["fresh_unresolved"].get("tg_bridge_v2_attempted_at") or "").strip() != ""

    conn.close()

    # 3) CLI smoke test (--canary 1 --dry-run)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "leadgen.backfill_direct_x",
            "--db",
            str(db_path),
            "--state-dir",
            str(state_dir),
            "--canary",
            "1",
            "--dry-run",
        ],
        cwd=str(WORKTREE_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    cli_out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert cli_out["dry_run"] is True
    assert cli_out["eligible_rows"] == 0


def test_backfill_direct_x_writes_per_row_checkpoint_before_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir, _db_path = _setup_backfill_env(tmp_path, monkeypatch)
    dx_path = state_dir / "direct_x_leads.jsonl"
    dx_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "row_ok", "company": "RowOK", "project_url": "https://rowok.io", "icp_score": 78}),
                json.dumps({"id": "row_crash", "company": "RowCrash", "project_url": "https://rowcrash.io", "icp_score": 78}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def flaky_bridge(row_in: dict[str, Any]) -> dict[str, Any]:
        if row_in.get("id") == "row_crash":
            raise RuntimeError("simulated mid-run crash")
        return row_in

    with pytest.raises(RuntimeError, match="simulated mid-run crash"):
        backfill_direct_x.backfill_direct_x_leads(
            conn,
            state_dir=state_dir,
            dry_run=False,
            treg_balance_fn=lambda: 10.0,
            monid_balance_fn=lambda: 10.0,
            http_get_fn=lambda _u, **_kw: SimpleNamespace(status_code=200, text="<html></html>"),
            treg_fn=lambda _ep, **_kw: {},
            tg_bridge_fn=flaky_bridge,
        )

    persisted = {
        r["id"]: r
        for ln in dx_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
        for r in [json.loads(ln)]
    }
    assert str(persisted["row_ok"].get("tg_bridge_v2_attempted_at") or "").strip() != ""
    assert not persisted["row_crash"].get("tg_bridge_v2_attempted_at")
    conn.close()

