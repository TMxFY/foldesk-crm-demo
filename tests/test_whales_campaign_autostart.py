"""Tests for Whales EN — TG autostart, Meta Ads GEO rotation, Direct/X retro-bridge, and harness ratchet."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from app.db import connect, init_db, utc_now
from app.hygiene import (
    _audit_per_campaign_leadgen_status,
    audit_server_leadgen_readiness,
)
from app.leadgen_settings import (
    DEFAULT_WHALES_TG_STEPS,
    get_leadgen_config,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.outreach import create_campaign, get_campaign
from leadgen import vps_autonomous_replenish
from leadgen.language_router import CAMPAIGN_WHALES_EN_TG
import scripts.proactive_audit as proactive_audit


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[sqlite3.Connection, Path]:
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-whales")
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    return conn, state_dir


def _seed_account(
    conn: sqlite3.Connection,
    acc_id: int,
    *,
    provider: str = "telegram",
    status: str = "connected",
    health: str = "active",
    username: str | None = None,
) -> int:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, provider,
            telegram_user_id, telegram_username, daily_limit, sent_today, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 20, 0, ?, ?)
        """,
        (
            acc_id,
            f"+1555000{acc_id:04d}",
            f"data/sessions/acc_{acc_id}.session",
            status,
            health,
            provider,
            900000 + acc_id if provider == "telegram" else None,
            username or (f"tg_acc_{acc_id}" if provider == "telegram" else None),
            now,
            now,
        ),
    )
    conn.commit()
    return acc_id


def test_meta_ad_leads_geo_rotation_and_max_runtime_sec_in_replenish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    all_geos = ["US", "GB", "AE", "SA", "DE", "FR", "NL", "ES", "IT", "SE", "CA", "AU"]
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": CAMPAIGN_WHALES_EN_TG,
                "sources": {
                    "meta_ads": True,
                    "tg_groups": False,
                    "tg_comments": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "buyer_catalogs": False,
                    "yc_ph": False,
                    "social_x_reddit": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "facebook_groups": False,
                    "ofm_groups": False,
                },
                "limits": {"external_limit": 0},
            },
            "whales": {"enabled": True, "geos": all_geos},
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

    meta_cmds: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        cmd_list = [str(x) for x in cmd]
        if len(cmd_list) > 1 and Path(cmd_list[1]).name == "meta_ad_leads.py":
            meta_cmds.append(cmd_list)
            out_p = Path(cmd_list[cmd_list.index("--output") + 1])
            out_p.write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    meta_seen = state_dir / "meta_ads_seen.json"

    # Cursor missing -> slice 0..2 (US,GB,AE) and --max-runtime-sec 210
    vps_autonomous_replenish.run_autonomous_replenish(
        conn, runner=fake_runner, stage_timeout=300
    )
    assert len(meta_cmds) == 1
    cmd0 = meta_cmds[-1]
    assert cmd0[cmd0.index("--country") + 1] == "US,GB,AE"
    assert cmd0[cmd0.index("--max-runtime-sec") + 1] == "210"

    # Cursor = 1 -> slice 3..5 (SA,DE,FR) and stage_timeout=100 -> max(60, 100-60) = 60
    meta_seen.write_text(json.dumps({"advertisers": {}, "cursor": 1}), encoding="utf-8")
    vps_autonomous_replenish.run_autonomous_replenish(
        conn, runner=fake_runner, stage_timeout=100
    )
    cmd1 = meta_cmds[-1]
    assert cmd1[cmd1.index("--country") + 1] == "SA,DE,FR"
    assert cmd1[cmd1.index("--max-runtime-sec") + 1] == "60"

    # Cursor = 3 -> slice 9..11 (SE,CA,AU)
    meta_seen.write_text(json.dumps({"advertisers": {}, "cursor": 3}), encoding="utf-8")
    vps_autonomous_replenish.run_autonomous_replenish(
        conn, runner=fake_runner, stage_timeout=300
    )
    cmd3 = meta_cmds[-1]
    assert cmd3[cmd3.index("--country") + 1] == "SE,CA,AU"

    # Cursor = 4 -> wraps around to US,GB,AE
    meta_seen.write_text(json.dumps({"advertisers": {}, "cursor": 4}), encoding="utf-8")
    vps_autonomous_replenish.run_autonomous_replenish(
        conn, runner=fake_runner, stage_timeout=300
    )
    cmd4 = meta_cmds[-1]
    assert cmd4[cmd4.index("--country") + 1] == "US,GB,AE"
    conn.close()


def test_retro_bridge_direct_x_leads_bounded_budget_live_verify_and_social_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "auto_push_to_crm": True,
            "b2b": {
                "enabled": True,
                "target_campaign_tg": CAMPAIGN_WHALES_EN_TG,
                "sources": {
                    "meta_ads": False,
                    "tg_groups": False,
                    "tg_comments": False,
                    "channel_graph": False,
                    "tg_ad_spy": False,
                    "buyer_catalogs": False,
                    "yc_ph": False,
                    "social_x_reddit": False,
                    "whop_content_rewards": False,
                    "tiktok_ads": False,
                    "tiktok_shop": False,
                    "app_stores": False,
                    "ats_hiring": False,
                    "ph_detailed": False,
                    "d2c_whatsapp": False,
                    "facebook_groups": False,
                    "ofm_groups": False,
                },
                "limits": {"external_limit": 0},
            },
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

    # Pre-seed duplicate social_manual lead to verify no crash on duplicate
    dx_path = state_dir / "direct_x_leads.jsonl"
    rows_seed = [
        {
            "id": "dx_already_tried",
            "name": "Already Tried",
            "company": "OldCorp",
            "founder_x": "https://x.com/oldcorp",
            "icp_score": 88,
            "source": "yc",
            "tg_bridge_attempted_at": "2026-11-20T00:00:00",
        },
        {
            "id": "dx_bridge_ok",
            "name": "Alice Bridge",
            "company": "BridgeFlow",
            "project": "BridgeFlow",
            "project_url": "https://bridgeflow.ai",
            "founder_linkedin": "https://www.linkedin.com/in/alice-bridge",
            "what_they_do": "AI video SaaS scaling UGC ads",
            "category": "SaaS / AI app",
            "icp_score": 90,
            "source": "meta_ads",
        },
        {
            "id": "dx_channel_stripped",
            "name": "Bob Channel",
            "company": "ChanBrand",
            "project": "ChanBrand",
            "project_url": "https://chanbrand.io",
            "founder_linkedin": "https://www.linkedin.com/in/bob-chanbrand",
            "what_they_do": "D2C supplement subscription brand",
            "category": "E-com / D2C",
            "icp_score": 86,
            "source": "tiktok_shop",
        },
        {
            "id": "dx_x_high",
            "name": "Cara X",
            "company": "CaraApp",
            "project": "CaraApp",
            "project_url": "https://caraapp.io",
            "founder_x": "https://x.com/carax_founder",
            "what_they_do": "Consumer AI photo app",
            "icp_score": 88,
            "source": "yc",
        },
        {
            "id": "dx_ig_high",
            "name": "Dana IG",
            "company": "GlowIG",
            "project": "GlowIG",
            "project_url": "https://glowig.com",
            "instagram_url": "https://instagram.com/danaglow_founder",
            "what_they_do": "Beauty D2C brand",
            "icp_score": 84,
            "source": "meta_ads",
        },
        {
            "id": "dx_email_high",
            "name": "Eli Email",
            "company": "MailPulse",
            "project": "MailPulse",
            "project_url": "https://mailpulse.co",
            "email": "eli@mailpulse.co",
            "what_they_do": "AI email automation SaaS",
            "icp_score": 85,
            "source": "whop",
        },
        {
            "id": "dx_phone_high",
            "name": "Finn Phone",
            "company": "PhoneCo",
            "project": "PhoneCo",
            "project_url": "https://phoneco.io",
            "phone": "+14155550177",
            "what_they_do": "Subscription fitness coaching app",
            "icp_score": 83,
            "source": "app_stores",
        },
        {
            "id": "dx_low_icp",
            "name": "Gabe Low",
            "company": "LowScoreApp",
            "project": "LowScoreApp",
            "project_url": "https://lowscoreapp.io",
            "founder_x": "https://x.com/gabelow",
            "what_they_do": "Mobile utility app",
            "icp_score": 80,
            "source": "yc",
        },
    ]
    for idx in range(6):
        rows_seed.append(
            {
                "id": f"dx_extra_{idx}",
                "name": f"Extra {idx}",
                "company": f"ExtraCo{idx}",
                "project": f"ExtraCo{idx}",
                "project_url": f"https://extraco{idx}.io",
                "founder_x": f"https://x.com/extraco{idx}",
                "what_they_do": "Consumer AI tool",
                "icp_score": 80,
                "source": "yc",
            }
        )
    # Also pre-insert duplicate mailto lead for dx_email_high? Wait: let's test repeat run safety instead,
    # and insert dx_x_high first only on run 2 or duplicate check below.
    dx_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows_seed) + "\n",
        encoding="utf-8",
    )

    bridge_calls: list[tuple[str, bool]] = []

    def fake_bridge(row: dict, **kwargs) -> dict:
        allow_paid = bool(kwargs.get("allow_paid", True))
        bridge_calls.append((str(row.get("id") or ""), allow_paid))
        out = dict(row)
        if row.get("id") == "dx_bridge_ok":
            out["username"] = "alice_bridge_dm"
            out["telegram_username"] = "alice_bridge_dm"
            out["provider"] = "telegram"
            out["tg_bridge_match"] = "founder_search_verified"
        elif row.get("id") == "dx_channel_stripped":
            out["username"] = "chanbrand_broadcast"
            out["telegram_username"] = "chanbrand_broadcast"
            out["provider"] = "telegram"
            out["tg_bridge_match"] = "website_direct"
        return out

    monkeypatch.setattr(vps_autonomous_replenish, "bridge_lead_to_telegram", fake_bridge)

    from telethon.tl.types import Channel, User

    def fake_resolver(_cli, handle: str):
        if handle == "alice_bridge_dm":
            return User(id=555101, username="alice_bridge_dm", first_name="Alice", bot=False)
        if handle == "chanbrand_broadcast":
            return Channel(
                id=555102,
                title="ChanBrand",
                photo=None,
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                broadcast=True,
                megagroup=False,
                username="chanbrand_broadcast",
                access_hash=1,
            )
        return None

    # Test budget guard (paid_allowed=False when balance=0.10) passed to bridge_lead_to_telegram
    res = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=None,
        budget_treg_balance_fn=lambda: 0.10,
        budget_monid_balance_fn=lambda: 10.0,
        tg_entity_resolver_fn=fake_resolver,
    )
    assert res["ok"] is True

    # Up to 12 un-attempted rows processed (skipped dx_already_tried, left dx_extra_5 untouched)
    assert len(bridge_calls) == 12
    assert "dx_already_tried" not in [c[0] for c in bridge_calls]
    assert "dx_extra_5" not in [c[0] for c in bridge_calls]
    assert all(paid_flag is False for _, paid_flag in bridge_calls)
    assert res["tg_bridged_count"] >= 1

    # dx_bridge_ok ingested into Whales EN — TG!
    whale_rec = conn.execute(
        """
        SELECT cr.username, cr.status, c.name AS camp_name
        FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE cr.username = 'alice_bridge_dm'
        """
    ).fetchone()
    assert whale_rec is not None
    assert whale_rec["camp_name"] == CAMPAIGN_WHALES_EN_TG

    # dx_channel_stripped was stripped & NOT enrolled into Whales EN — TG
    chan_rec = conn.execute(
        "SELECT id FROM campaign_recipients WHERE username = 'chanbrand_broadcast'"
    ).fetchone()
    assert chan_rec is None

    # High-ICP >= 82 remaining rows entered CRM `leads` as segment='social_manual'
    social_rows = {
        r["manual_action_url"]: dict(r)
        for r in conn.execute(
            "SELECT name, segment, manual_action_url, manual_draft_text FROM leads WHERE segment = 'social_manual'"
        ).fetchall()
    }
    assert "https://www.linkedin.com/in/bob-chanbrand" in social_rows
    assert "https://x.com/carax_founder" in social_rows
    assert "https://instagram.com/danaglow_founder" in social_rows
    assert "mailto:eli@mailpulse.co" in social_rows
    assert "https://wa.me/14155550177" in social_rows
    # Low ICP=80 must NOT enter social_manual
    assert "https://x.com/gabelow" not in social_rows
    # Clean draft text without CreatorOS
    for url_k, srow in social_rows.items():
        assert srow["manual_draft_text"], f"Missing manual_draft_text for {url_k}"
        assert "creatoros" not in srow["manual_draft_text"].lower()

    # direct_x_leads.jsonl removed dx_bridge_ok, stamped attempted rows, left dx_extra_5 un-attempted
    remaining_dx = [
        json.loads(ln)
        for ln in dx_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    by_id = {r["id"]: r for r in remaining_dx}
    assert "dx_bridge_ok" not in by_id
    assert by_id["dx_channel_stripped"].get("tg_bridge_attempted_at")
    assert by_id["dx_x_high"].get("tg_bridge_attempted_at")
    assert by_id["dx_low_icp"].get("tg_bridge_attempted_at")
    assert not by_id["dx_extra_5"].get("tg_bridge_attempted_at")

    # Re-run: picks up only dx_extra_5 (1 remaining un-attempted), does not crash on duplicate social_manual
    bridge_calls.clear()
    res2 = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        runner=None,
        budget_treg_balance_fn=lambda: 10.0,
        budget_monid_balance_fn=lambda: 10.0,
        tg_entity_resolver_fn=fake_resolver,
    )
    assert res2["ok"] is True
    assert bridge_calls == [("dx_extra_5", True)]
    conn.close()


def test_ensure_whales_tg_campaign_ready_assigns_unassigned_tg_and_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.leadgen_settings import ensure_whales_tg_campaign_ready

    conn, _state_dir = _setup_env(tmp_path, monkeypatch)
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "auto_push_to_crm": True,
            "b2b": {"enabled": True, "target_campaign_tg": CAMPAIGN_WHALES_EN_TG},
            "vacancies": {"enabled": False},
        },
        sync_cases_file=False,
    )
    cfg = get_leadgen_config(conn)

    # Seed accounts:
    # - 27: connected TG assigned to OFM
    # - 39, 40: unassigned healthy connected TG
    # - 41: frozen TG
    # - 42: connected TG on active FloodWait cooldown
    # - 43: pending_code WA
    # - 44: connected WA
    _seed_account(conn, 27, provider="telegram", status="connected", health="active")
    _seed_account(conn, 39, provider="telegram", status="connected", health="active")
    _seed_account(conn, 40, provider="telegram", status="connected", health="active")
    _seed_account(conn, 41, provider="telegram", status="connected", health="frozen")
    _seed_account(conn, 42, provider="telegram", status="connected", health="active")
    _seed_account(conn, 43, provider="whatsapp", status="pending_code", health="active")
    _seed_account(conn, 44, provider="whatsapp", status="connected", health="active")
    future_ts = (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('account_cooldown_until:42', ?, ?)",
        (str(future_ts), utc_now()),
    )
    create_campaign(conn, name="SoftUGC — OFM (TG)", status="running", account_ids=[27])

    whales = create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        status="draft",
        daily_limit=30,
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )
    cid = int(whales["id"])

    # 1) Autopilot ON + pending == 0 -> assigns unassigned healthy TG [39, 40], stays draft!
    res_empty = ensure_whales_tg_campaign_ready(conn, cfg)
    assert res_empty["assigned_account_ids"] == [39, 40]
    assert res_empty["promoted_to_running"] is False
    c_after_assign = get_campaign(conn, cid)
    assert c_after_assign["account_ids"] == [39, 40]
    assert c_after_assign["account_id"] == 39
    assert c_after_assign["status"] == "draft"

    # Insert 1 poisoned recipient (followup_before_step1 + Russian copy in EN campaign) -> Rule 5 preview blocks promotion!
    now = utc_now()
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, hook, current_step, status, created_at, updated_at
        ) VALUES (?, 6001, 'bad_followup', 'Bad Lead', 'Acme', 'Привет! Возвращаюсь по прошлому письму.', 'Привет! Возвращаюсь по прошлому письму.', 1, 'pending', ?, ?)
        """,
        (cid, now, now),
    )
    conn.commit()
    res_bad = ensure_whales_tg_campaign_ready(conn, cfg)
    assert res_bad["promoted_to_running"] is False
    assert get_campaign(conn, cid)["status"] == "draft"

    # Replace with clean recipient, test Autopilot OFF blocks promotion
    conn.execute(
        "UPDATE campaign_recipients SET username = 'good_whale', current_step = 0, step1_text = ?, hook = ? WHERE campaign_id = ?",
        (
            "Hey Bad, saw Acme. Want to see 3 demo reels for Acme?",
            "Hey Bad, saw Acme. Want to see 3 demo reels for Acme?",
            cid,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '0', ?)",
        (now,),
    )
    conn.commit()
    res_no_auto = ensure_whales_tg_campaign_ready(conn, cfg)
    assert res_no_auto["promoted_to_running"] is False
    assert get_campaign(conn, cid)["status"] == "draft"

    # Turn Autopilot ON -> promotes to 'running'!
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '1', ?)",
        (now,),
    )
    conn.commit()
    res_ok = sync_leadgen_campaigns(conn, cfg)
    assert res_ok["ok"] is True
    assert get_campaign(conn, cid)["status"] == "running"

    # 2) Fallback pool test: when ALL healthy connected TG accounts are assigned to other campaigns,
    # falls back to full connected non-vacancy healthy TG pool (never frozen/cooldown/WA/pending_code)
    conn.execute("DELETE FROM campaign_recipients WHERE campaign_id = ?", (cid,))
    conn.execute("DELETE FROM campaigns WHERE id = ?", (cid,))
    whales2 = create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        status="draft",
        daily_limit=30,
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )
    # Assign 39 and 40 to another non-vacancy campaign too
    create_campaign(conn, name="SoftUGC — Other", status="paused", account_ids=[39, 40])
    res_fallback = ensure_whales_tg_campaign_ready(conn, cfg)
    assert set(res_fallback["assigned_account_ids"]) == {27, 39, 40}

    # 3) run_autonomous_replenish also promotes draft Whales EN — TG even when pending >= low_water_mark
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, hook, current_step, status, created_at, updated_at
        ) VALUES (?, 6002, 'ready_whale_2', 'Ready Lead', 'ReadyCo',
                  'Hey Ready, saw ReadyCo. Want to see 3 demo reels for ReadyCo?',
                  'Hey Ready, saw ReadyCo. Want to see 3 demo reels for ReadyCo?',
                  0, 'pending', ?, ?)
        """,
        (whales2["id"], now, now),
    )
    conn.commit()
    res_rep = vps_autonomous_replenish.run_autonomous_replenish(
        conn,
        low_water_mark=1,
        runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    assert res_rep["ok"] is True
    assert res_rep["whales_ready"]["promoted_to_running"] is True
    assert get_campaign(conn, whales2["id"])["status"] == "running"
    conn.close()


def test_harness_ratchet_flags_draft_whales_tg_when_b2b_and_autopilot_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    save_leadgen_config(
        conn,
        {"enabled": True, "b2b": {"enabled": True}},
        sync_cases_file=False,
    )
    now = utc_now()
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '1', ?)",
        (now,),
    )
    whales = create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        status="draft",
        daily_limit=30,
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )
    # Even if 20 pending leads sit in draft Whales EN — TG, 0 running B2B TG campaigns must fail!
    for i in range(20):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hey, saw Acme. Want 3 demo reels?', 'pending', ?, ?)
            """,
            (whales["id"], 7100 + i, f"whale_draft_{i}", f"Founder {i}", now, now),
        )
    conn.commit()

    per_camp = _audit_per_campaign_leadgen_status(conn, state_dir=state_dir)
    assert any(
        int(c["campaign_id"]) == int(whales["id"]) and c.get("low_water_alert") is True
        for c in per_camp
    )

    readiness = audit_server_leadgen_readiness(conn, enforce_vps_checks=True, fail_on_starvation=True)
    assert readiness["starvation_alert"] is True
    assert "campaign_starvation" in readiness["missing_components"]
    assert readiness["ok"] is False

    audit_fail = proactive_audit._audit_leadgen_starvation_and_anomalies(conn, state_dir=state_dir)
    assert audit_fail["ok"] is False
    assert audit_fail["starvation_alert"] is True
    assert any(
        "b2b_tg_campaign_not_running" in iss or "whales_en_tg_starved" in iss
        for iss in (audit_fail.get("issues") or [])
    )

    # Autopilot disabled -> draft Whales EN — TG is NOT flagged as not_running
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '0', ?)",
        (now,),
    )
    conn.commit()
    audit_paused_auto = proactive_audit._audit_leadgen_starvation_and_anomalies(conn, state_dir=state_dir)
    assert audit_paused_auto["ok"] is True

    # Re-enable autopilot AND promote Whales EN — TG to running -> healthy (ok=True)
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('outreach_autopilot_enabled', '1', ?)",
        (now,),
    )
    conn.execute("UPDATE campaigns SET status = 'running' WHERE id = ?", (whales["id"],))
    conn.commit()
    audit_ok = proactive_audit._audit_leadgen_starvation_and_anomalies(conn, state_dir=state_dir)
    assert audit_ok["ok"] is True
    conn.close()
