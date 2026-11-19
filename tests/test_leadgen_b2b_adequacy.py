"""Tests for B2B leadgen starvation telemetry, bridge/segfault hardening, rotation, and retention."""

from __future__ import annotations

import asyncio
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db, utc_now
from app.hygiene import audit_server_leadgen_readiness, inspect_leadgen_run_anomalies, read_leadgen_health_summary
from app.leadgen_settings import push_discovered_now, save_leadgen_config
from app.main import create_app
from app.outreach import create_campaign
from leadgen import (
    buyer_catalogs,
    buyer_public,
    creator_external_leads,
    high_yield_veins,
    tg_ad_spy,
    tg_channel_graph,
    tg_identity_bridge,
    tg_llm_qualify,
    vps_autonomous_replenish,
)
from leadgen.crm_mcp_push import build_lead_payload_from_dict


def _make_valid_session(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, auth_key BLOB)")
        db.execute("INSERT INTO sessions (dc_id, auth_key) VALUES (2, ?)", (b"\x5a" * 256,))
        db.commit()
    return path


def _tme_html(nick: str, name: str, bio: str) -> str:
    return (
        '<html><body><div class="tgme_page_title"><span>'
        + name
        + '</span></div><div class="tgme_page_extra">@'
        + nick
        + '</div><div class="tgme_page_description">'
        + bio
        + '</div><a class="tgme_action_button_new">Send Message</a></body></html>'
    )


def test_per_campaign_starvation_ofm_never_masks_b2b_and_api_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    sess = _make_valid_session(tmp_path / "scraper" / "signals_ro.session")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_treg = bin_dir / "treg"
    fake_treg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_treg.chmod(0o755)

    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(sess))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-valid")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-valid")
    monkeypatch.setenv("TREG_API_KEY", "treg-test-valid")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    conn = connect(tmp_path / "starvation.db")
    init_db(conn)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {"enabled": True, "sources": {"tg_groups": True, "meta_ads": True}},
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )

    b2b = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        leadgen_enabled=True,
    )
    ofm = create_campaign(
        conn,
        name="SoftUGC — OFM (TG)",
        daily_limit=60,
        status="running",
        leadgen_enabled=True,
    )

    # Fill OFM with 56 pending fresh leads while B2B has 0 pending!
    now_iso = utc_now()
    for idx in range(56):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, status, current_step, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (ofm["id"], 90000 + idx, f"ofm_lead_{idx}", f"OFM Buyer {idx}", now_iso, now_iso),
        )
    conn.commit()

    (state_dir / "last_run_summary.json").write_text(
        json.dumps(
            {
                "timestamp": now_iso,
                "ingested_total": 0,
                "tg_bridged_count": 0,
                "per_source_counts": {"meta_ads": 3, "buyer_catalogs": 16},
                "stages_run": ["buyer_catalogs", "meta_ad_leads"],
                "stage_error_count": 0,
                "reasons": ["sources_exhausted"],
                "low_water_alert": {
                    "campaign_id": b2b["id"],
                    "campaign_name": b2b["name"],
                    "pending": 0,
                    "reason": "sources_exhausted",
                },
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "leadgen_run_history.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": f"2026-11-19T0{h}:00:00",
                    "tg_bridged_count": 0,
                    "source_hashes": {"buyer_catalogs": "abc123"},
                }
            )
            for h in (1, 2, 3)
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "direct_x_leads.jsonl").write_text(
        json.dumps(
            {
                "name": "Granola",
                "company": "Granola",
                "x_url": "https://x.com/meetgranola",
                "url": "https://granola.ai",
                "lead_source": "yc",
                "their_angle": "AI meeting notepad",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    readiness = audit_server_leadgen_readiness(conn, enforce_vps_checks=True)
    assert readiness["starvation_alert"] is True
    assert len(readiness["campaigns"]) == 2
    by_id = {c["campaign_id"]: c for c in readiness["campaigns"]}
    assert by_id[b2b["id"]]["pending"] == 0
    assert by_id[b2b["id"]]["low_water_alert"] is True
    assert by_id[b2b["id"]]["sources_exhausted"] is True
    assert by_id[ofm["id"]]["pending"] == 56
    assert by_id[ofm["id"]]["low_water_alert"] is False
    assert [c["campaign_id"] for c in readiness["campaigns_starved"]] == [b2b["id"]]
    assert readiness["tg_bridged_zero_streak"] == 3
    assert readiness["tg_bridged_zero_alert"] is True
    assert readiness["static_dump_alert"] is True

    strict_readiness = audit_server_leadgen_readiness(
        conn, enforce_vps_checks=True, fail_on_starvation=True
    )
    assert strict_readiness["ok"] is False
    assert "campaign_starvation" in strict_readiness["missing_components"]

    app = create_app(conn=conn)
    with TestClient(app) as client:
        deep = client.get("/api/health?deep=1").json()
        assert deep["leadgen_starvation_alert"] is True
        assert len(deep["leadgen_campaigns"]) == 2
        assert deep["campaigns_starved"][0]["campaign_id"] == b2b["id"]
        assert deep["tg_bridged_zero_streak"] == 3
        assert deep["static_dump_alert"] is True

        summary = client.get("/api/leadgen/health-summary").json()
        assert summary["ok"] is True
        assert summary["starvation_alert"] is True
        assert summary["starvation_ok"] is False
        assert summary["direct_x_count"] == 1
        assert summary["direct_x_leads"][0]["company"] == "Granola"
        assert summary["tg_bridged_zero_streak"] == 3
        assert summary["static_dump_repeats"][0]["source"] == "buyer_catalogs"

        dx_json = client.get("/api/leadgen/direct-x-leads").json()
        assert dx_json["total"] == 1
        assert dx_json["items"][0]["x_url"] == "https://x.com/meetgranola"

        dx_csv = client.get("/api/leadgen/direct-x-leads?format=csv")
        assert dx_csv.status_code == 200
        assert "text/csv" in dx_csv.headers["content-type"]
        assert "https://x.com/meetgranola" in dx_csv.text
        assert "Granola" in dx_csv.text

    direct_summary = read_leadgen_health_summary(conn, state_dir=state_dir)
    assert direct_summary["starvation_alert"] is True
    assert len(direct_summary["campaigns"]) == 2
    conn.close()


def test_zero_bridge_3_run_detector_and_static_hash_repeat_reset(tmp_path: Path):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    for idx in range(3):
        z, rep = vps_autonomous_replenish._update_run_history_and_detect(
            state_dir,
            {
                "timestamp": f"2026-11-19T0{idx}:00:00",
                "tg_bridged_count": 0,
                "source_hashes": {"buyer_catalogs": "samehash"},
            },
        )
    assert z == 3
    assert rep and rep[0]["source"] == "buyer_catalogs" and rep[0]["repeat_runs"] == 3

    anomalies = inspect_leadgen_run_anomalies(state_dir)
    assert anomalies["tg_bridged_zero_alert"] is True
    assert anomalies["static_dump_alert"] is True

    z2, rep2 = vps_autonomous_replenish._update_run_history_and_detect(
        state_dir,
        {
            "timestamp": "2026-11-19T04:00:00",
            "tg_bridged_count": 2,
            "source_hashes": {"buyer_catalogs": "freshhash"},
        },
    )
    assert z2 == 0
    assert rep2 == []


def test_tg_bridged_count_captures_contact_waterfall_and_bridge_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    sess = _make_valid_session(tmp_path / "scraper" / "signals_ro.session")
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(sess))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-valid")

    conn = connect(tmp_path / "bridge_count.db")
    init_db(conn)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {
                    "tg_groups": False,
                    "buyer_catalogs": False,
                    "meta_ads": True,
                    "social_x_reddit": False,
                    "yc_ph": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "tg_identity_bridge": True,
                },
            },
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )
    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        leadgen_enabled=True,
    )

    def _out_flag(cmd: list[str]) -> Path | None:
        if "--output" in cmd:
            return Path(cmd[cmd.index("--output") + 1])
        return None

    def fake_runner(cmd, **kwargs):
        script = Path(cmd[1]).name
        out_p = _out_flag(cmd)
        if script == "meta_ad_leads.py" and out_p is not None:
            out_p.write_text(
                json.dumps(
                    {
                        "id": "meta_ads:growly",
                        "name": "Baby Photo Editor",
                        "company": "Growly",
                        "project": "Growly",
                        "project_url": "https://growly.life",
                        "source": "meta_ads",
                        "bio": "Active Meta video ads for baby app",
                        "username": "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        elif script == "tg_enrich_leads.py" and out_p is not None:
            in_p = Path(cmd[cmd.index("--input") + 1])
            out_p.write_text(in_p.read_text(encoding="utf-8"), encoding="utf-8")
        elif script == "tg_llm_qualify.py" and out_p is not None:
            in_p = Path(cmd[cmd.index("--input") + 1])
            rows = [json.loads(ln) for ln in in_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r.update({"qualified": True, "llm_qualified": True, "icp_score": 88, "category": "SaaS / AI app", "role": "Founder", "roles": ["Founder"], "what_they_do": "AI baby photo editor"})
            out_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        elif script == "tg_curate.py" and out_p is not None:
            in_p = Path(cmd[cmd.index("--input") + 1])
            out_p.write_text(in_p.read_text(encoding="utf-8"), encoding="utf-8")
        elif script == "contact_waterfall.py" and out_p is not None:
            # Simulates contact_waterfall resolving TG before tg_identity_bridge runs
            in_p = Path(cmd[cmd.index("--input") + 1])
            rows = [json.loads(ln) for ln in in_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in rows:
                r["username"] = "hellogrowly"
                r["telegram_username"] = "hellogrowly"
                r["tg_bridge_match"] = "domain_search_explicit"
                r["provider"] = "telegram"
            out_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        elif script == "tg_identity_bridge.py" and out_p is not None:
            in_p = Path(cmd[cmd.index("--input") + 1])
            out_p.write_text(in_p.read_text(encoding="utf-8"), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = vps_autonomous_replenish.run_autonomous_replenish(conn, runner=fake_runner)
    assert res["tg_bridged_count"] == 1
    assert res["tg_bridged_zero_streak"] == 0
    conn.close()


def test_lead_source_preserved_into_db_and_direct_x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # 1) tg_scrape_posters payload without explicit source still preserves tg_groups, never Cold outreach
    tg_built = build_lead_payload_from_dict(
        {
            "group": "saasfounders",
            "name": "Alex Maker",
            "company": "MetricPulse",
            "username": "alex_metricpulse",
            "project_url": "https://metricpulse.io",
            "bio": "Founder @ MetricPulse SaaS analytics",
            "role": "Founder",
            "roles": ["Founder"],
            "category": "SaaS / AI app",
            "icp_score": 88,
        }
    )
    assert tg_built["lead_source"] == "tg_groups"

    # 2) Ingest meta_ads TG lead + yc X-only lead into CRM and direct_x_leads.jsonl
    conn = connect(tmp_path / "sources.db")
    init_db(conn)
    save_leadgen_config(conn, {"enabled": True}, sync_cases_file=False)
    create_campaign(conn, name="SoftUGC — База Все2", status="running", leadgen_enabled=True)

    (state_dir / "raw_20261119_100000.final.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "name": "Fluzy: Speak English AI",
                        "company": "Fluzy",
                        "project": "Fluzy",
                        "username": "fluzy_founder",
                        "project_url": "https://fluzy.ai",
                        "category": "SaaS / AI app",
                        "role": "Founder",
                        "roles": ["Founder"],
                        "bio": "AI English speaking coach subscription app",
                        "what_they_do": "AI English speaking coach app",
                        "their_angle": "Active Meta video ads for English speaking AI app",
                        "first_touch": "Hey,\n\nSaw Fluzy. We run SoftUGC, 183.5M views in 90 days on revshare or CPA. Want 3 sample reels?",
                        "source": "meta_ads",
                        "icp_score": 88,
                    }
                ),
                json.dumps(
                    {
                        "name": "Granola",
                        "company": "Granola",
                        "project": "Granola",
                        "founder_x": "https://x.com/meetgranola",
                        "project_url": "https://granola.ai",
                        "category": "SaaS / AI app",
                        "role": "Founder",
                        "roles": ["Founder"],
                        "bio": "AI notepad for meetings",
                        "what_they_do": "AI notepad for back-to-back meetings",
                        "source": "yc",
                        "icp_score": 86,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    res = push_discovered_now(conn)
    assert res["ingested"] == 1
    row = conn.execute("SELECT lead_source FROM leads WHERE username = 'fluzy_founder'").fetchone()
    assert row is not None
    assert row["lead_source"] == "meta_ads"

    dx_lines = [
        json.loads(ln)
        for ln in (state_dir / "direct_x_leads.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert dx_lines[0]["lead_source"] == "yc"
    conn.close()


def test_tg_llm_qualify_survives_typesafe_segfault_and_admits_meta_ads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TYPESAFE_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_IN_SUBPROCESS", "1")
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-valid")

    def fake_sigsegv_subproc(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], -11, stdout="", stderr="Segmentation fault")

    monkeypatch.setattr(tg_llm_qualify.subprocess, "run", fake_sigsegv_subproc)
    monkeypatch.setattr(
        tg_llm_qualify,
        "call_llm",
        lambda batch, model: [
            {
                "is_lead": True,
                "icp_score": 88,
                "monetization": "subscription",
                "short_form_fit": True,
                "category": "SaaS / AI app",
                "role": "Marketing",
                "project": "Fluzy",
                "project_url": "https://fluzy.ai",
                "what_they_do": "AI language tutor app",
            }
            for _ in batch
        ],
    )

    quarantine: list[dict] = []
    out, dropped = tg_llm_qualify.qualify_loaded(
        [
            {
                "name": "Fluzy: Speak English AI",
                "source": "meta_ads",
                "role": "Marketing",
                "bio": "Active Meta video ads (Download): Fluzy AI Tutor",
                "project_url": "https://fluzy.ai",
                "has_monetization_signal": True,
            }
        ],
        batch_size=12,
        quarantine_out=quarantine,
        quarantine_dir=str(tmp_path),
    )
    assert dropped == 0
    assert len(out) == 1
    assert out[0]["project"] == "Fluzy"
    assert out[0]["qualified"] is True


def test_tg_identity_bridge_uses_x_founder_name_and_strict_tme_confirm():
    resolved = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Coursology",
            "company": "Coursology",
            "project_url": "https://coursology.com",
            "founder_x": "https://x.com/coursology",
        },
        x_profile_fn=lambda h: {"data": {"username": "coursology", "name": "Gavin Lee Godby", "bio": "Study AI"}},
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: _tme_html("gavin_godby", "Gavin Godby", "Founder @ Coursology") if nick == "gavin_godby" else "",
    )
    assert resolved["username"] == "gavin_godby"
    assert resolved["tg_bridge_match"] == "x_founder_handle"

    # Anti-namesake protection stays enforced on founder handle guesses
    rejected = tg_identity_bridge.bridge_lead_to_telegram(
        {
            "name": "Coursology",
            "company": "Coursology",
            "project_url": "https://coursology.com",
            "founder_x": "https://x.com/coursology",
        },
        x_profile_fn=lambda h: {"data": {"username": "coursology", "name": "Gavin Lee Godby", "bio": "Study AI"}},
        search_fn=lambda q: {},
        tme_fetch_fn=lambda nick: _tme_html("gavin_godby", "Random Guy", "Crypto signals") if nick == "gavin_godby" else "",
    )
    assert not rejected.get("username")


def test_prune_leadgen_state_jsonl_age_and_size_preserves_ssot(tmp_path: Path):
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    now_ts = time.time()

    # Protected SSOT files
    for pname in ("master_ids.jsonl", "direct_x_leads.jsonl", "ofm_review_leads.jsonl"):
        p = state_dir / pname
        p.write_text(json.dumps({"keep": pname}) + "\n", encoding="utf-8")
        os.utime(p, (now_ts - 30 * 86400, now_ts - 30 * 86400))

    # Expired (>14d) ephemeral run artifact
    old_raw = state_dir / "raw_20261001_000000.jsonl"
    old_raw.write_text('{"old": true}\n', encoding="utf-8")
    os.utime(old_raw, (now_ts - 16 * 86400, now_ts - 16 * 86400))

    # Recent large ephemeral artifacts pushing over max_total_bytes
    big1 = state_dir / "raw_20261118_000000.enriched.jsonl"
    big1.write_text("x" * 1500 + "\n", encoding="utf-8")
    os.utime(big1, (now_ts - 200, now_ts - 200))

    big2 = state_dir / "raw_20261119_000000.enriched.jsonl"
    big2.write_text("y" * 1500 + "\n", encoding="utf-8")
    os.utime(big2, (now_ts - 50, now_ts - 50))

    stats = vps_autonomous_replenish.prune_leadgen_state_jsonl(
        state_dir,
        max_age_days=14,
        max_total_bytes=2000,
        now_ts=now_ts,
    )
    assert not old_raw.exists()
    assert not big1.exists()
    assert (state_dir / "master_ids.jsonl").exists()
    assert (state_dir / "direct_x_leads.jsonl").exists()
    assert (state_dir / "ofm_review_leads.jsonl").exists()
    assert stats["total_bytes_after"] <= 2000


def test_discovery_rotation_and_deadline_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 1) buyer_catalogs cursor rotation & dedup avoids repeating identical static dump
    seen_file = tmp_path / "catalog_seen.json"
    gumroad_body = (
        '{"permalink":"acmeai","name":"AcmeAI SaaS Assistant","url":"https://acmeai.gumroad.com/l/pro"}'
    )
    rows_run1 = buyer_catalogs.collect(
        fetch=lambda url: gumroad_body if "gumroad.com" in url else "", seen_path=seen_file
    )
    rows_run2 = buyer_catalogs.collect(
        fetch=lambda url: gumroad_body if "gumroad.com" in url else "", seen_path=seen_file
    )
    assert len(rows_run1) == 1
    assert rows_run2 == []

    # 2) high_yield_veins rotates queries with cursor & stops cleanly on tight deadline
    day = date(2026, 11, 19)
    q0 = high_yield_veins._rotate_for_day(high_yield_veins.D2C_WHATSAPP_QUERIES, day, 2, cursor=0)
    q1 = high_yield_veins._rotate_for_day(high_yield_veins.D2C_WHATSAPP_QUERIES, day, 2, cursor=1)
    assert q0 != q1
    monkeypatch.setattr(high_yield_veins, "_default_web_search", lambda q: {"results": []})
    rows_dl, _ = high_yield_veins.collect_all_veins(
        day=day, seen={}, limit=10, veins=("d2c_whatsapp",), max_runtime_sec=1e-9
    )
    assert rows_dl == []

    # 3) tg_channel_graph re-seeds from EN fallback when frontier and seeds are exhausted
    cg_state = tmp_path / "cg_seen.json"
    cg_state.write_text(
        json.dumps(
            {
                "seen": ["seed_one"],
                "expanded": ["seed_one"],
                "frontier": [],
            }
        ),
        encoding="utf-8",
    )
    seen_targets: list[str] = []

    class _FakeGraphClient:
        async def get_entity(self, target):
            seen_targets.append(str(target))
            raise ValueError("skip")

    asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=_FakeGraphClient(),
            seeds=["seed_one"],
            state_path=str(cg_state),
            output_path=str(tmp_path / "cg_out.jsonl"),
            delay=0.0,
            max_runtime_sec=5.0,
        )
    )
    assert any(t in tg_channel_graph.FALLBACK_EN_GRAPH_SEEDS for t in seen_targets)

    # 4) buyer_public rotates broader crawl4ai Whop queries and parses t.me / x.com snippets cleanly
    monkeypatch.setattr(
        "leadgen.treg_client.check_treg_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "leadgen.treg_client.call",
        lambda ep, **kw: {
            "results": [
                {
                    "title": "Clip Capital",
                    "url": "https://whop.com/clip-capital/",
                    "snippet": "Submit bounty on Telegram: https://t.me/jaidevbhogal X: https://x.com/jaidevbhogalw",
                }
            ]
        },
    )
    bp_rows = buyer_public._whop_treg_search_rows()
    tg_users = [
        item["username"]
        for row in bp_rows
        for item in (row.get("telegram_verified") or [])
        if item.get("username")
    ]
    assert "jaidevbhogal" in tg_users

    # 5) creator_external_leads.app_stores marks commercial apps with has_monetization_signal=True
    monkeypatch.setattr(
        creator_external_leads,
        "_get_json",
        lambda url, params=None: {
            "results": [
                {
                    "trackName": "LangSpeak AI",
                    "description": "AI spoken tutor",
                    "trackViewUrl": "https://apps.apple.com/us/app/langspeak/id12345",
                    "sellerUrl": "https://langspeak.ai",
                    "artistName": "LangSpeak Inc",
                    "userRatingCount": 420,
                }
            ]
        },
    )
    monkeypatch.setattr(creator_external_leads, "_get_text", lambda url, params=None: "")
    ext_rows = creator_external_leads.app_stores(1)
    assert ext_rows and ext_rows[0]["has_monetization_signal"] is True

    # 6) tg_ad_spy tries fast public t.me/s HTML first in production mode
    monkeypatch.setattr(
        tg_ad_spy,
        "default_fetch_html",
        lambda url: '<div class="tgme_widget_message_text">Sponsored <a href="https://coolsaas.io/?utm_source=tg">try</a> @cool_ceo #ad</div>',
    )
    spy_posts = tg_ad_spy.fetch_public_channel_posts_http("coolchan", limit=5)
    assert spy_posts and "coolsaas.io" in spy_posts[0]
