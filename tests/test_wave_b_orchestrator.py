"""Stage 2 tests for Wave B orchestrator wiring, 24h catalog cadence, tma_apps, reverse-bridge, and Gate Z."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from app.db import connect, init_db
from app.leadgen_settings import (
    DEFAULT_WHALES_TG_STEPS,
    get_leadgen_config,
    save_leadgen_config,
)
from app.outreach import create_campaign
from leadgen import vps_autonomous_replenish as repl


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Path]:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-wave-b")
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    return conn, state_dir


def _seed_whales_campaign(conn: sqlite3.Connection) -> None:
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=140,
        status="running",
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )


def _configure_wave_b(conn: sqlite3.Connection, *, reverse_handle: str = "timofei_reels") -> dict[str, Any]:
    cfg = get_leadgen_config(conn)
    cfg["enabled"] = True
    cfg["vacancies"]["enabled"] = False
    cfg["ru_experts"]["enabled"] = False
    cfg["b2b"]["enabled"] = True
    cfg["b2b"]["reverse_bridge_tg_handle"] = reverse_handle
    cfg["b2b"]["tg_groups_new"] = []
    cfg["b2b"]["tg_groups_old"] = []
    cfg["b2b"]["limits"]["external_limit"] = 0
    cfg["b2b"]["sources"] = {
        "buyer_catalogs": False,
        "whop_content_rewards": False,
        "tiktok_ads": False,
        "tiktok_shop": False,
        "app_stores": False,
        "ats_hiring": False,
        "ph_detailed": False,
        "d2c_whatsapp": False,
        "facebook_groups": False,
        "meta_ads": False,
        "social_x_reddit": False,
        "yc_ph": False,
        "tg_groups": False,
        "ofm_groups": False,
        "channel_graph": True,
        "tg_ad_spy": True,
        "tg_identity_bridge": True,
        "tma_apps": True,
        "tg_catalog_discovery": True,
    }
    return save_leadgen_config(conn, cfg)


def test_wave_b_orchestrator_24h_catalog_cadence_tma_apps_and_budget_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_whales_campaign(conn)
    _configure_wave_b(conn)

    sessions_dir = tmp_path / "scraper_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    ro_sess = sessions_dir / "signals_ro.session"
    ro_sess.write_bytes(b"SQLite format 3\x00" + b"0" * 64)

    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(ro_sess))
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "fakehash")
    monkeypatch.setattr(repl, "emit_leadgen_daily_digest", lambda *a, **kw: {"daily_sent": False, "error": None})

    seen_calls: list[list[str]] = []

    def fake_runner(cmd: list[str] | str, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        parts = [str(x) for x in cmd] if isinstance(cmd, list) else [str(cmd)]
        seen_calls.append(parts)
        joined = " ".join(parts)
        if "tg_catalog_discovery.py" in joined:
            (state_dir / "tg_catalog_state.json").write_text(
                json.dumps({"last_run_at": datetime.now(timezone.utc).isoformat()}),
                encoding="utf-8",
            )
        elif "tma_apps.py" in joined and "--output" in parts:
            out_file = Path(parts[parts.index("--output") + 1])
            out_file.write_text(
                json.dumps(
                    {
                        "id": "tma:focusvpn",
                        "project": "FocusVPN TMA",
                        "company": "FocusVPN TMA",
                        "project_url": "https://focusvpn.example",
                        "tma_bot_handle": "focusvpn_bot",
                        "username": "alex_focusvpn",
                        "lead_source": "tma_apps",
                        "source": "tma_apps",
                        "segment": "whale",
                        "category": "tma",
                        "icp_score": 84,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Run 1: no tg_catalog_state.json -> catalog discovery runs BEFORE tg_ad_spy & tg_channel_graph; tma_apps runs & counts
    res1 = repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        scraper_session=ro_sess,
        scraper_sessions_dir=sessions_dir,
        max_waves=1,
        budget_treg_balance_fn=lambda: 5.0,
        budget_monid_balance_fn=lambda: 5.0,
    )
    assert res1["ok"] is True
    assert "tg_catalog_discovery" in res1["stages_run"]
    assert "tma_apps" in res1["stages_run"]
    assert res1["per_source_counts"].get("tma_apps") == 1

    cat_idx = next(i for i, c in enumerate(seen_calls) if "tg_catalog_discovery.py" in " ".join(c))
    spy_idx = next(i for i, c in enumerate(seen_calls) if "tg_ad_spy.py" in " ".join(c))
    graph_idx = next(i for i, c in enumerate(seen_calls) if "tg_channel_graph.py" in " ".join(c))
    assert cat_idx < graph_idx
    assert cat_idx < spy_idx
    assert "--state-dir" in seen_calls[spy_idx]
    assert "--state-dir" in seen_calls[graph_idx]
    assert "--no-paid" not in seen_calls[cat_idx]

    # Run 2: tg_catalog_state.json is fresh (<24h), paid budget exhausted -> catalog skipped, tma_apps gets --no-paid
    seen_calls.clear()
    res2 = repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        scraper_session=ro_sess,
        scraper_sessions_dir=sessions_dir,
        max_waves=1,
        budget_treg_balance_fn=lambda: 0.10,
        budget_monid_balance_fn=lambda: 5.0,
    )
    assert res2["ok"] is True
    assert "tg_catalog_discovery" not in res2["stages_run"]
    tma_cmds = [c for c in seen_calls if "tma_apps.py" in " ".join(c)]
    assert len(tma_cmds) == 1
    assert "--no-paid" in tma_cmds[0]

    # Stale catalog (>24h) + low balance -> runs catalog with --no-paid
    (state_dir / "tg_catalog_state.json").write_text(
        json.dumps({"last_run_at": "2026-11-20T00:00:00+00:00"}),
        encoding="utf-8",
    )
    seen_calls.clear()
    res3 = repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        scraper_session=ro_sess,
        scraper_sessions_dir=sessions_dir,
        max_waves=1,
        budget_treg_balance_fn=lambda: 0.10,
        budget_monid_balance_fn=lambda: 5.0,
    )
    cat_cmds3 = [c for c in seen_calls if "tg_catalog_discovery.py" in " ".join(c)]
    assert len(cat_cmds3) == 1
    assert "--no-paid" in cat_cmds3[0]
    conn.close()


def test_split_tg_and_direct_x_attaches_reverse_bridge_drafts(tmp_path: Path) -> None:
    final_jsonl = tmp_path / "raw_test.final.jsonl"
    dx_jsonl = tmp_path / "direct_x_leads.jsonl"
    final_jsonl.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "whale_email",
                        "company": "Acme AI Video",
                        "project": "Acme AI Video",
                        "project_url": "https://acme.ai",
                        "work_email": "founder@acme.ai",
                        "category": "SaaS",
                        "what_they_do": "AI video creation SaaS",
                        "lead_source": "meta_ads",
                        "icp_score": 88,
                    }
                ),
                json.dumps(
                    {
                        "id": "whale_x_only",
                        "company": "XOnly Brand",
                        "project_url": "https://xonly.io",
                        "founder_x": "https://x.com/xonlyfounder",
                        "icp_score": 85,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = repl.split_tg_and_direct_x_leads(
        final_jsonl,
        dx_jsonl,
        reverse_bridge_tg_handle="@timofei_reels",
    )
    assert out["direct_x_added"] == 2
    saved = [json.loads(line) for line in dx_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {row["id"]: row for row in saved}
    email_row = by_id["whale_email"]
    assert email_row["reverse_bridge_tg_link"] == "https://t.me/timofei_reels"
    assert "https://t.me/timofei_reels" in email_row["reverse_bridge_wa_draft"]
    assert "https://t.me/timofei_reels" in email_row["reverse_bridge_email_body"]
    assert email_row["reverse_bridge_email_subject"]
    combined = (
        email_row["reverse_bridge_wa_draft"]
        + email_row["reverse_bridge_email_subject"]
        + email_row["reverse_bridge_email_body"]
    ).lower()
    assert "creatoros" not in combined
    assert "reverse_bridge_wa_draft" not in by_id["whale_x_only"]


def test_verify_sh_gate_z_fails_on_unsafe_ro_joins(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    sandbox = tmp_path / "repo"
    (sandbox / "scripts").mkdir(parents=True)
    (sandbox / "leadgen").mkdir(parents=True)
    (sandbox / ".claude" / "skills" / "replica-design").mkdir(parents=True)
    (sandbox / ".claude" / "skills" / "replica-launch").mkdir(parents=True)
    (sandbox / "replica" / "design").mkdir(parents=True)
    (sandbox / "replica" / "launch").mkdir(parents=True)
    (sandbox / "app" / "static").mkdir(parents=True, exist_ok=True)
    (sandbox / "app" / "static" / "app.js").write_text("// stub for fast static check\n", encoding="utf-8")

    for rel in (
        "scripts/verify.sh",
        "leadgen/tma_apps.py",
        "leadgen/tg_catalog_discovery.py",
        ".claude/skills/replica-design/contrast.py",
        ".claude/skills/replica-launch/listing.py",
        "replica/design/tokens.json",
        "replica/launch/listing.json",
    ):
        src = repo_root / rel
        if src.is_file():
            shutil.copy2(src, sandbox / rel)

    # Break Gate Z invariant: set MAX_RO_JOINS_PER_DAY = 5 (> 2)
    cat_path = sandbox / "leadgen" / "tg_catalog_discovery.py"
    txt = cat_path.read_text(encoding="utf-8")
    new_txt = re.sub(
        r"(?m)^MAX_RO_JOINS_PER_DAY(?:\s*:\s*int)?\s*=\s*2\b",
        "MAX_RO_JOINS_PER_DAY = 5",
        txt,
        count=1,
    )
    assert new_txt != txt
    cat_path.write_text(new_txt, encoding="utf-8")

    proc = subprocess.run(
        ["bash", "scripts/verify.sh", "--fast-checks-only"],
        cwd=str(sandbox),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PYTHON_BIN": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "Gate Z violation" in proc.stderr


def test_skeptic_tma_unresolved_pipe_and_mcp_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.mcp_server import handle_jsonrpc_message
    from leadgen import tg_to_gsheet

    # 1) tma_apps row WITHOUT personal username (only tma_bot_handle + project_url) MUST survive _merge_discovered_jsonl_into_raw
    tma_in = tmp_path / "tma_apps_raw.jsonl"
    merged_out = tmp_path / "merged_raw.jsonl"
    tma_in.write_text(
        json.dumps(
            {
                "id": "tma:focusvpn",
                "name": "FocusVPN Bot",
                "project": "FocusVPN",
                "company": "FocusVPN",
                "project_url": "https://focusvpn.example",
                "website": "https://focusvpn.example",
                "tma_bot_handle": "focusvpn_bot",
                "username": "",
                "bio": "Paid Telegram Mini App VPN on Telegram Stars",
                "what_they_do": "Subscription VPN Mini App for privacy",
                "source": "tma_apps",
                "lead_source": "tma_apps",
                "category": "tma",
                "segment": "whale",
                "icp_score": 82,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    admitted = tg_to_gsheet.admit_discovered_row(json.loads(tma_in.read_text(encoding="utf-8").strip()))
    assert admitted is not None
    assert admitted["contact_url"] == "https://focusvpn.example"
    merged_cnt = repl._merge_discovered_jsonl_into_raw([tma_in], merged_out)
    assert merged_cnt == 1

    # 2) ingest_discovered_leads strips *bot username & scrubs revshare/CPA on Whales EN — TG (whale_mass)
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_whales_campaign(conn)
    mcp_resp = handle_jsonrpc_message(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "ingest_discovered_leads",
                "arguments": {
                    "enroll_in_campaign": True,
                    "leads": [
                        {
                            "username": "pure_bot",
                            "company": "BotOnlyCo",
                            "name": "Bot Only",
                            "their_angle": "Subscription AI studio SaaS",
                            "icp_score": 85,
                            "segment": "whale",
                        },
                        {
                            "username": "real_tma_founder",
                            "company": "StarsFlow",
                            "name": "Leo Founder",
                            "website": "https://starsflow.app",
                            "their_angle": "Telegram Mini App SaaS on Stars",
                            "source": "tma_apps",
                            "segment": "whale",
                            "step1_text": (
                                "Hey,\n\nSaw StarsFlow.\n\nWe run SoftUGC.\n\n"
                                "We work on revshare or CPA — want 3 demo reels?"
                            ),
                            "icp_score": 88,
                        },
                    ],
                },
            },
        },
    )
    res_obj = mcp_resp["result"]["structuredContent"]
    assert res_obj["enrolled_in_campaign"] == 1
    assert res_obj["skipped_non_icp"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE LOWER(username) LIKE '%bot'").fetchone()[0] == 0
    rec = conn.execute(
        "SELECT username, step1_text FROM campaign_recipients WHERE username = 'real_tma_founder'"
    ).fetchone()
    assert rec is not None
    assert "revshare" not in (rec["step1_text"] or "").lower()
    assert "cpa" not in (rec["step1_text"] or "").lower()
    assert "1 500+ reels/mo" in (rec["step1_text"] or "")
    conn.close()
