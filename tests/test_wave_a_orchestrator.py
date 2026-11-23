"""Stage 2 tests for Wave A orchestrator, RO pool, cooldown, dedup, verify fail-closed, batch loop, and daily digest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from app.db import connect, init_db, utc_now
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
    monkeypatch.setenv("RELAYFAST_API_KEY", "rk-test-wave-a")
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    return conn, state_dir


def _seed_running_whales_campaign(conn: sqlite3.Connection, *, daily_limit: int = 140) -> dict[str, Any]:
    return create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=daily_limit,
        status="running",
        steps=DEFAULT_WHALES_TG_STEPS,
        leadgen_enabled=True,
    )


def _configure_b2b_for_fast_mock(
    conn: sqlite3.Connection,
    *,
    limits: dict[str, Any] | None = None,
    sources_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = get_leadgen_config(conn)
    cfg["enabled"] = True
    cfg["vacancies"]["enabled"] = False
    cfg["ru_experts"]["enabled"] = False
    cfg["b2b"]["enabled"] = True
    cfg["b2b"]["target_campaign"] = "Whales EN — TG"
    cfg["b2b"]["tg_groups_new"] = ["solofounders"]
    cfg["b2b"]["tg_groups_old"] = []
    sources = {
        "buyer_catalogs": True,
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
        "yc_ph": True,
        "tg_groups": True,
        "ofm_groups": False,
        "channel_graph": False,
        "tg_ad_spy": False,
        "tg_identity_bridge": True,
    }
    if sources_override:
        sources.update(sources_override)
    cfg["b2b"]["sources"] = sources
    if limits:
        cfg["b2b"]["limits"].update(limits)
    return save_leadgen_config(conn, cfg)


def test_find_ro_scraper_sessions_pool_and_outreach_guard(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    outreach_dir = tmp_path / "sessions"
    outreach_dir.mkdir(parents=True, exist_ok=True)
    live_outreach = outreach_dir / "signals_ro_hijacked.session"
    live_outreach.write_text("live-outreach", encoding="utf-8")
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, provider,
            telegram_user_id, telegram_username, daily_limit, sent_today, created_at, updated_at
        ) VALUES (1, '+15550001111', ?, 'connected', 'active', 'telegram', 990001, 'live_acc', 20, 0, ?, ?)
        """,
        (str(live_outreach), now, now),
    )
    conn.commit()

    scraper_dir = tmp_path / "scraper_sessions"
    scraper_dir.mkdir(parents=True, exist_ok=True)
    primary = scraper_dir / "main_scraper.session"
    sig1 = scraper_dir / "signals_ro_1.session"
    sig2 = scraper_dir / "signals_ro_2.session"
    pc1 = scraper_dir / "phone_checker_1.session"
    pc2 = scraper_dir / "phone_checker_2.session"
    pc3 = scraper_dir / "phone_checker_3.session"
    for p in (primary, sig1, sig2, pc1, pc2, pc3):
        p.write_text("isolated", encoding="utf-8")

    pool = repl.find_ro_scraper_sessions(
        conn,
        sessions_dir=scraper_dir,
        primary_session=primary,
        allow_phone_checker_ro_fallback=True,
    )
    pool_names = [p.name for p in pool]
    assert pool_names == [
        "main_scraper.session",
        "signals_ro_1.session",
        "signals_ro_2.session",
        "phone_checker_2.session",
        "phone_checker_3.session",
    ]
    assert "phone_checker_1.session" not in pool_names

    pool_no_pc = repl.find_ro_scraper_sessions(
        conn,
        sessions_dir=scraper_dir,
        primary_session=primary,
        allow_phone_checker_ro_fallback=False,
    )
    assert [p.name for p in pool_no_pc] == [
        "main_scraper.session",
        "signals_ro_1.session",
        "signals_ro_2.session",
    ]

    with pytest.raises(ValueError):
        repl.find_ro_scraper_sessions(
            conn,
            sessions_dir=outreach_dir,
            primary_session=live_outreach,
        )
    conn.close()


def test_untruncated_yc_and_external_limits_and_zero_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"yc_limit": 80, "ph_limit": 12, "external_limit": 40},
        sources_override={"tg_groups": False, "buyer_catalogs": False, "tg_identity_bridge": False},
    )

    recorded_cmds: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        clist = [str(x) for x in cmd]
        recorded_cmds.append(clist)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = repl.run_autonomous_replenish(conn, runner=fake_runner, max_waves=1)
    assert res["ok"] is True

    yc_cmds = [c for c in recorded_cmds if any("yc_ph_leads.py" in part for part in c)]
    ext_cmds = [c for c in recorded_cmds if any("creator_external_leads.py" in part for part in c)]
    assert len(yc_cmds) == 1
    assert yc_cmds[0][yc_cmds[0].index("--yc-limit") + 1] == "80"
    assert yc_cmds[0][yc_cmds[0].index("--ph-limit") + 1] == "12"
    assert len(ext_cmds) == 1
    assert ext_cmds[0][ext_cmds[0].index("--limit") + 1] == "40"

    # Zero external_limit disables creator_external_leads completely
    _configure_b2b_for_fast_mock(
        conn,
        limits={"yc_limit": 50, "external_limit": 0},
        sources_override={"tg_groups": False, "buyer_catalogs": False, "tg_identity_bridge": False},
    )
    recorded_cmds.clear()
    repl.run_autonomous_replenish(conn, runner=fake_runner, max_waves=1)
    assert not any("creator_external_leads.py" in " ".join(c) for c in recorded_cmds)
    conn.close()


def test_comment_channels_rotation_and_hybrid_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"external_limit": 0},
        sources_override={"yc_ph": False, "buyer_catalogs": False, "tg_groups": True, "tg_identity_bridge": False},
    )
    scraper_dir = tmp_path / "scraper_sessions"
    scraper_dir.mkdir(parents=True, exist_ok=True)
    primary = scraper_dir / "main.session"
    sig1 = scraper_dir / "signals_ro_1.session"
    primary.write_text("s", encoding="utf-8")
    sig1.write_text("s", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(primary))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(scraper_dir))

    now = utc_now()
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('quarantined_leadgen_sources', ?, ?)",
        (json.dumps(["bad_quarantined_chat"]), now),
    )
    conn.commit()

    comments_txt = state_dir / "discovered_comment_channels.txt"
    channels = ["solofounders", "bad_quarantined_chat"] + [f"founder_comments_{i}" for i in range(1, 16)]
    comments_txt.write_text("\n".join(channels) + "\n", encoding="utf-8")

    scrape_cmds: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        clist = [str(x) for x in cmd]
        if any("tg_scrape_posters.py" in p for p in clist):
            scrape_cmds.append(clist)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        scraper_session=primary,
        scraper_sessions_dir=scraper_dir,
        max_waves=1,
    )
    assert len(scrape_cmds) == 2
    assert scrape_cmds[0][scrape_cmds[0].index("--mode") + 1] == "hybrid"
    assert scrape_cmds[1][scrape_cmds[1].index("--mode") + 1] == "hybrid"
    first_batch = scrape_cmds[1][scrape_cmds[1].index("--groups") + 1].split(",")
    assert len(first_batch) == 10
    assert "bad_quarantined_chat" not in first_batch
    assert "solofounders" not in first_batch
    assert first_batch[0] == "founder_comments_1"

    cursor_data = json.loads((state_dir / "comment_chats_cursor.json").read_text(encoding="utf-8"))
    assert cursor_data["offset"] == 10

    # Second run picks rotated next 10 chats with wrap-around
    scrape_cmds.clear()
    repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        scraper_session=primary,
        scraper_sessions_dir=scraper_dir,
        max_waves=1,
    )
    second_batch = scrape_cmds[1][scrape_cmds[1].index("--groups") + 1].split(",")
    assert second_batch[:5] == [f"founder_comments_{i}" for i in range(11, 16)]
    assert second_batch[5:] == [f"founder_comments_{i}" for i in range(1, 6)]
    conn.close()


def test_tg_group_admins_runs_after_waterfall_and_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, _state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"external_limit": 0},
        sources_override={"yc_ph": False, "buyer_catalogs": False, "tg_groups": False, "tg_identity_bridge": True},
    )

    order_log: list[str] = []

    def fake_runner(cmd, **kwargs):
        clist = [str(x) for x in cmd]
        cstr = " ".join(clist)
        if "tg_llm_qualify.py" in cstr:
            Path(clist[clist.index("--output") + 1]).write_text(
                json.dumps({"company": "WhaleCo", "project": "WhaleCo", "icp_match": True}) + "\n",
                encoding="utf-8",
            )
        elif "tg_curate.py" in cstr:
            Path(clist[clist.index("--output") + 1]).write_text(
                json.dumps({
                    "name": "Whale Founder",
                    "company": "WhaleCo",
                    "project": "WhaleCo",
                    "project_url": "https://whaleco.io",
                    "category": "SaaS",
                    "their_angle": "UGC subscription growth",
                    "icp_score": 88,
                    "source": "meta_ads",
                    "step1_text": "Saw WhaleCo scaling UGC.",
                }) + "\n",
                encoding="utf-8",
            )
        elif "tg_identity_bridge.py" in cstr:
            order_log.append("tg_identity_bridge")
            in_rows = Path(clist[clist.index("--input") + 1]).read_text(encoding="utf-8")
            Path(clist[clist.index("--output") + 1]).write_text(in_rows, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_waterfall_enrich(row: dict[str, Any], **kwargs) -> dict[str, Any]:
        order_log.append("contact_waterfall")
        out = dict(row)
        out["telegram_group"] = "https://t.me/whaleco_community"
        return out

    async def fake_admin_extractor(client: Any, group_handle: str):
        order_log.append(f"tg_group_admins:{group_handle}")
        return [{"username": "whale_ceo_real", "user_id": 555001, "role_guess": "Founder"}]

    res = repl.run_autonomous_replenish(
        conn,
        runner=fake_runner,
        waterfall_enrich_fn=fake_waterfall_enrich,
        tg_admin_extractor_fn=fake_admin_extractor,
        max_waves=1,
    )
    assert res["ok"] is True
    assert order_log == ["contact_waterfall", "tg_identity_bridge", "tg_group_admins:whaleco_community"]
    assert res["tg_group_admins"]["leads_promoted"] == 1
    db_lead = conn.execute("SELECT username FROM leads WHERE username = 'whale_ceo_real'").fetchone()
    assert db_lead is not None
    conn.close()


def test_verify_personal_tg_handles_web_first_ro_pool_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (a) Web-first handles user / channel / bot without calling Telethon;
    # only 'unknown' goes to RO session pool, which fails over from sess1 (FloodWait) to sess2!
    sess1 = tmp_path / "signals_ro_1.session"
    sess2 = tmp_path / "signals_ro_2.session"
    sess1.write_text("s1", encoding="utf-8")
    sess2.write_text("s2", encoding="utf-8")

    class FloodWaitError(Exception):
        pass

    sess_calls: list[tuple[str, str]] = []

    def fake_with_client(sess: Path, coro_fn):
        class FakeClient:
            async def get_entity(self, handle: str):
                sess_calls.append((sess.name, handle))
                if sess.name == "signals_ro_1.session":
                    raise FloodWaitError("Flood wait 30s")
                if handle == "edge_user":
                    return SimpleNamespace(id=777111, bot=False, deleted=False, scam=False, fake=False)
                return None

        return repl._run_coro_sync(coro_fn(FakeClient()))

    monkeypatch.setattr(repl, "_with_isolated_tg_client", fake_with_client)

    rows = [
        {"username": "good_founder", "project": "App1"},
        {"username": "brand_hub", "project": "App2"},
        {"username": "help_bot_real", "project": "App3"},
        {"username": "edge_user", "project": "App4", "tg_handle_is_guessed": True},
    ]

    def fake_web_probe(handle: str):
        return {
            "good_founder": {"kind": "user"},
            "brand_hub": {"kind": "channel"},
            "help_bot_real": {"kind": "bot"},
            "edge_user": {"kind": "unknown"},
        }[handle]

    out, stats = repl.verify_personal_tg_handles_live(
        rows,
        scraper_sessions=[sess1, sess2],
        web_probe_fn=fake_web_probe,
        use_web_first=True,
    )
    assert stats["web_resolved"] == 3
    assert stats["flood_waits"] == 1
    assert stats["verified_users"] == 2
    assert stats["channels_stripped"] == 1
    assert stats["bots_or_invalid_stripped"] == 1
    assert stats["pending_verify_deferred"] == 0
    assert sess_calls == [("signals_ro_1.session", "edge_user"), ("signals_ro_2.session", "edge_user")]
    assert out[0]["username"] == "good_founder"
    assert out[1]["username"] == "" and out[1]["telegram_group"] == "https://t.me/brand_hub"
    assert out[2]["username"] == ""
    assert out[3]["username"] == "edge_user" and out[3]["telegram_user_id"] == 777111

    # (b) Fail-closed when ALL RO sessions fail with FloodWait on a guessed handle
    def always_flood(sess: Path, coro_fn):
        class FloodClient:
            async def get_entity(self, handle: str):
                raise FloodWaitError("Flood wait 120s")

        return repl._run_coro_sync(coro_fn(FloodClient()))

    monkeypatch.setattr(repl, "_with_isolated_tg_client", always_flood)
    queue_path = tmp_path / "pending_tg_verify_queue.jsonl"
    deferred_out, deferred_stats = repl.verify_personal_tg_handles_live(
        [
            {"username": "john_guessed", "project": "GuessApp", "tg_handle_is_guessed": True},
            {"username": "explicit_bio", "project": "BioApp", "tg_handle_is_guessed": False},
        ],
        scraper_sessions=[sess1, sess2],
        web_probe_fn=lambda h: {"kind": "unknown"},
        use_web_first=True,
        pending_verify_queue_path=queue_path,
    )
    assert deferred_stats["sessions_exhausted"] == 1
    assert deferred_stats["pending_verify_deferred"] == 1
    assert deferred_out[0]["username"] == ""
    assert deferred_out[0]["tg_verify_deferred"] is True
    assert deferred_out[1]["username"] == "explicit_bio"
    queued = [json.loads(ln) for ln in queue_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(queued) == 1
    assert queued[0]["deferred_tg_handle"] == "john_guessed"


def test_filter_existing_crm_leads_dedups_before_waterfall(tmp_path: Path) -> None:
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO leads (
            username, name, company, url, company_domain, phone, provider, status, created_at, updated_at
        ) VALUES ('old_founder', 'Old Founder', 'ExistingApp', 'https://existingapp.io/pricing', 'existingapp.io', '+14155551111', 'telegram', 'new', ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    incoming = [
        {"username": "@old_founder", "project_url": "https://brandnew.ai"},  # dup username
        {"username": "", "project_url": "https://www.existingapp.io/blog"},  # dup company domain without TG
        {"username": "", "phone": "+1 (415) 555-1111", "project_url": "https://other.io"},  # dup phone
        {"username": "", "project_url": "https://whop.com/newbrand"},  # generic platform allowed
        {"username": "cofounder_new", "project_url": "https://existingapp.io"},  # distinct TG handle kept
    ]
    kept, dropped = repl._filter_existing_crm_leads(conn, incoming)
    assert dropped == 3
    assert [r["project_url"] for r in kept] == ["https://whop.com/newbrand", "https://existingapp.io"]
    conn.close()


def test_batch_loop_runs_multiple_waves_until_low_water_mark_and_stops_on_zero_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"external_limit": 0},
        sources_override={"tg_groups": False, "yc_ph": False, "buyer_catalogs": True, "tg_identity_bridge": False},
    )

    wave_counter = {"n": 0}

    def multi_wave_runner(cmd, **kwargs):
        clist = [str(x) for x in cmd]
        cstr = " ".join(clist)
        if "buyer_catalogs.py" in cstr:
            wave_counter["n"] += 1
            n = wave_counter["n"]
            rows = [
                {"username": f"wave{n}_ceo_{idx}", "project": f"Wave{n}App{idx}", "project_url": f"https://w{n}a{idx}.io"}
                for idx in range(3)
            ]
            Path(clist[clist.index("--output") + 1]).write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
        elif "tg_enrich_leads.py" in cstr:
            in_txt = Path(clist[clist.index("--input") + 1]).read_text(encoding="utf-8")
            Path(clist[clist.index("--output") + 1]).write_text(in_txt, encoding="utf-8")
        elif "tg_llm_qualify.py" in cstr:
            in_rows = [json.loads(ln) for ln in Path(clist[clist.index("--input") + 1]).read_text(encoding="utf-8").splitlines() if ln.strip()]
            for r in in_rows:
                r["icp_match"] = True
                r["icp_score"] = 85
            Path(clist[clist.index("--output") + 1]).write_text(
                "\n".join(json.dumps(r) for r in in_rows) + "\n", encoding="utf-8"
            )
        elif "tg_curate.py" in cstr:
            in_rows = [json.loads(ln) for ln in Path(clist[clist.index("--input") + 1]).read_text(encoding="utf-8").splitlines() if ln.strip()]
            out_rows = [
                {
                    "username": r["username"],
                    "name": r["username"],
                    "company": r["project"],
                    "project": r["project"],
                    "project_url": r["project_url"],
                    "category": "SaaS",
                    "their_angle": "Paid social creative scale",
                    "icp_score": 85,
                    "source": "buyer_catalogs",
                    "step1_text": f"Saw {r['project']}. Paid acquisition gets expensive fast.",
                }
                for r in in_rows
            ]
            Path(clist[clist.index("--output") + 1]).write_text(
                "\n".join(json.dumps(r) for r in out_rows) + "\n", encoding="utf-8"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = repl.run_autonomous_replenish(conn, runner=multi_wave_runner, low_water_mark=5, max_waves=3)
    assert res["ok"] is True
    assert res["waves_executed"] == 2
    assert res["pending_b2b"] == 6

    # Early stop on 0 new rows when queue still below high low_water_mark
    res_zero = repl.run_autonomous_replenish(
        conn,
        runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
        low_water_mark=50,
        max_waves=3,
    )
    assert res_zero["waves_executed"] == 1
    assert state_dir.exists()
    conn.close()


def test_split_tg_and_direct_x_threshold_70_and_tg_phone_direct(tmp_path: Path) -> None:
    final_jsonl = tmp_path / "raw_test.final.jsonl"
    dx_jsonl = tmp_path / "direct_x_leads.jsonl"
    rows = [
        {"username": "mid_icp_ceo", "project": "Mid72", "icp_score": 72, "source": "yc_ph"},
        {"username": "low_icp_ceo", "project": "Low69", "icp_score": 69, "source": "yc_ph"},
        {
            "username": "",
            "phone": "+447911223344",
            "provider": "telegram",
            "telegram_user_id": 888999,
            "tg_phone_direct": True,
            "project": "PhoneDirectApp",
            "icp_score": 76,
            "source": "meta_ads",
        },
        {
            "username": "",
            "phone": "+14155550199",
            "work_email": "ceo@officeapp.io",
            "project": "OfficeApp",
            "icp_score": 78,
            "source": "yc_ph",
        },
    ]
    final_jsonl.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    split_res = repl.split_tg_and_direct_x_leads(final_jsonl, dx_jsonl)
    assert split_res == {"tg_wa_kept": 2, "direct_x_added": 1, "dropped": 1}
    kept = [json.loads(ln) for ln in final_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["project"] for r in kept] == ["Mid72", "PhoneDirectApp"]
    assert kept[1]["provider"] == "telegram"
    assert kept[1]["tg_phone_direct"] is True
    dx_rows = [json.loads(ln) for ln in dx_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["project"] for r in dx_rows] == ["OfficeApp"]


def test_auto_cooldown_48h_on_buyer_public_and_facebook_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"external_limit": 0},
        sources_override={
            "buyer_catalogs": True,
            "facebook_groups": True,
            "yc_ph": False,
            "tg_groups": False,
            "tg_identity_bridge": False,
        },
    )

    st = repl._load_source_yield_state(state_dir)
    t0 = 1_700_000_000.0
    assert repl._record_source_yield(st, "buyer_public", 0, now_ts=t0) is False
    assert repl._record_source_yield(st, "buyer_public", 0, now_ts=t0 + 60) is False
    assert repl._record_source_yield(st, "buyer_public", 0, now_ts=t0 + 120) is True
    assert repl._is_source_in_cooldown(st, "buyer_public", now_ts=t0 + 3600) is True
    assert repl._is_source_in_cooldown(st, "buyer_public", now_ts=t0 + 49 * 3600) is False

    # Put live state into cooldown now so run_autonomous_replenish skips buyer_public
    live_st = repl._load_source_yield_state(state_dir)
    for _ in range(3):
        repl._record_source_yield(live_st, "buyer_public", 0)
    repl._save_source_yield_state(state_dir, live_st)

    cmds: list[str] = []
    res = repl.run_autonomous_replenish(
        conn,
        runner=lambda c, **kw: cmds.append(" ".join(str(x) for x in c)) or subprocess.CompletedProcess(c, 0, stdout="", stderr=""),
        max_waves=1,
    )
    assert not any("buyer_public.py" in c for c in cmds)
    assert "cooldown_48h:buyer_public" in res["reasons"]
    assert "buyer_public" in res["cooldown_sources"]
    conn.close()


def test_emit_leadgen_daily_digest_and_instant_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_key in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_CHAT_ID", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    conn.executemany(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("bot_token", "123:testtoken", now),
            ("bot_chat_id", "998877", now),
        ],
    )
    conn.commit()

    posted: list[str] = []

    class FakeResp:
        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_opener(req, timeout=15):
        payload = json.loads(req.data.decode("utf-8"))
        posted.append(str(payload["text"]))
        return FakeResp()

    fixed_now = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
    summary = {
        "ingested_total": 42,
        "pending_b2b": 65,
        "direct_x_routed": 9,
        "waves_executed": 2,
        "per_source_counts": {"meta_ads": 20, "tg_ad_spy": 15, "yc_ph": 7},
        "budget_state": {"spent_usd": 3.25, "daily_cap_usd": 10.0, "treg_balance_usd": 1.20, "monid_balance_usd": 1.07},
        "tg_handle_verification": {"sessions_exhausted": 1, "pending_verify_deferred": 4},
        "cooldown_sources": ["buyer_public"],
        "stage_errors": [],
    }
    first = repl.emit_leadgen_daily_digest(conn, summary=summary, digest_opener=fake_opener, now_dt=fixed_now)
    assert first["instant_alert_sent"] is True
    assert first["daily_sent"] is True
    assert len(posted) == 2
    assert "Leadgen Critical Alert" in posted[0]
    assert len(posted[1].splitlines()) == 4

    # Same day within 6h -> both skipped cleanly
    second = repl.emit_leadgen_daily_digest(
        conn,
        summary=summary,
        digest_opener=fake_opener,
        now_dt=fixed_now + timedelta(hours=2),
    )
    assert second["instant_alert_sent"] is False
    assert second["daily_sent"] is False
    assert second["daily_skipped"] == "already_sent_today"
    assert len(posted) == 2
    conn.close()


def test_parallel_web_discovery_executes_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    conn, _state_dir = _setup_env(tmp_path, monkeypatch)
    _seed_running_whales_campaign(conn)
    _configure_b2b_for_fast_mock(
        conn,
        limits={"external_limit": 20},
        sources_override={
            "buyer_catalogs": True,
            "yc_ph": True,
            "tg_groups": False,
            "tg_identity_bridge": False,
        },
    )

    barrier = threading.Barrier(2, timeout=3.0)
    concurrent_stages: list[str] = []
    lock = threading.Lock()

    def concurrent_runner(cmd, **kwargs):
        cstr = " ".join(str(x) for x in cmd)
        if "buyer_catalogs.py" in cstr or "yc_ph_leads.py" in cstr:
            with lock:
                concurrent_stages.append(Path(cmd[1]).name)
            barrier.wait()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = repl.run_autonomous_replenish(
        conn,
        runner=concurrent_runner,
        max_waves=1,
        web_discovery_workers=5,
    )
    assert res["ok"] is True
    assert set(concurrent_stages) == {"buyer_catalogs.py", "yc_ph_leads.py"}
    conn.close()


def test_gate_y_blocks_wave_a_regressions(tmp_path: Path) -> None:
    worktree_root = Path(__file__).resolve().parents[1]
    sandbox = tmp_path / "gate_y_sb"
    for sub in ("scripts", "app/static", "leadgen", "tests", "deploy/systemd"):
        (sandbox / sub).mkdir(parents=True, exist_ok=True)
    for link_dir in (".claude", "replica"):
        src = worktree_root / link_dir
        if src.exists():
            (sandbox / link_dir).symlink_to(src)
    venv_src = worktree_root / ".venv"
    if venv_src.exists():
        (sandbox / ".venv").symlink_to(venv_src.resolve())

    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text((worktree_root / "scripts" / "verify.sh").read_text(encoding="utf-8"), encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "app" / "static" / "app.js").write_text("const ok = 1;\n", encoding="utf-8")
    (sandbox / "leadgen" / "__init__.py").write_text("", encoding="utf-8")
    (sandbox / "leadgen" / "tg_web_client.py").write_text("def probe(): return {}\n", encoding="utf-8")
    (sandbox / "leadgen" / "tg_ad_spy.py").write_text("import tg_web_client\ndef scan_ad_channels(): pass\n", encoding="utf-8")
    (sandbox / "leadgen" / "tg_channel_graph.py").write_text("import tg_web_client\ndef crawl_channel_graph(): pass\n", encoding="utf-8")
    (sandbox / "leadgen" / "tg_llm_qualify.py").write_text("MIN_ICP_SCORE = 80\n", encoding="utf-8")
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text(
        "import argparse\n"
        "def find_ro_scraper_sessions(): return []\n"
        "def emit_leadgen_daily_digest(): return {}\n"
        "def _run_autonomous_replenish_locked():\n"
        "    yc_lim = min(12, int(limits_cfg.get('yc_limit') or 12))\n"
        "    enrich_whales_with_tg_group_admins()\n"
        "    run_contact_waterfall_with_budget()\n"
        "if __name__ == '__main__': argparse.ArgumentParser().parse_args()\n",
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "clean.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    (sandbox / "deploy" / "systemd" / "crmtg-leadgen.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* 07,11,15,19:15:00 UTC\n",
        encoding="utf-8",
    )

    bad = subprocess.run(
        ["bash", str(sb_verify), "--fast-checks-only"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "MIN_ICP_SCORE must be 70" in bad.stderr
    assert "min(12, yc_limit)" in bad.stderr
    assert "enrich_whales_with_tg_group_admins after run_contact_waterfall_with_budget" in bad.stderr
    assert "00,03,06,09,12,15,18,21:15:00 UTC" in bad.stderr
