from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

import app.digest as app_digest
from app.db import connect, init_db, utc_now
from app.leadgen_settings import (
    DEFAULT_B2B_STEPS,
    get_leadgen_config,
    push_discovered_now,
    save_leadgen_config,
    sync_leadgen_campaigns,
)
from app.outreach import create_campaign, get_campaign


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "leadgen_config.json"
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    scraper_dir = tmp_path / "scraper_sessions"
    scraper_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_file))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(scraper_dir))
    monkeypatch.delenv("SCRAPER_TG_SESSION", raising=False)
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LEADGEN_WRITE_CASES_BANK", raising=False)

    db_path = tmp_path / "crm.db"
    conn = connect(db_path)
    init_db(conn)
    yield {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "conn": conn,
        "cfg_file": cfg_file,
        "state_dir": state_dir,
        "scraper_dir": scraper_dir,
    }
    conn.close()


def test_defect1_push_discovered_now_never_mutates_daily_limit_or_creates_empty_wa_campaign(
    isolated_env,
):
    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]

    camp_vse2 = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
        leadgen_daily_quota=140,
    )

    # 1) Empty state_dir: push_discovered_now must not lower daily_limit (140 -> 30)
    # and must not create empty Leadgen Auto (WA) or TG Вакансии campaigns.
    res_empty = push_discovered_now(conn)
    assert res_empty["ok"] is True
    assert res_empty["ingested"] == 0

    after_empty = get_campaign(conn, camp_vse2["id"])
    assert after_empty["daily_limit"] == 140
    assert after_empty["status"] == "running"

    camp_names = [
        r["name"] for r in conn.execute("SELECT name FROM campaigns ORDER BY id ASC").fetchall()
    ]
    assert camp_names == ["SoftUGC — База Все2"]

    # 2) Valid TG B2B lead in .final.jsonl (0 WA leads, 0 vacancy leads):
    # ingests into SoftUGC — База Все2 while keeping daily_limit == 140 and creating 0 WA/vacancy drafts.
    final_jsonl = state_dir / "batch_01.final.jsonl"
    final_jsonl.write_text(
        json.dumps(
            {
                "username": "@saas_founder_emma",
                "name": "Emma Stone",
                "role": "Founder",
                "roles": ["Founder"],
                "company": "PulseBoard",
                "website": "https://pulseboard.io",
                "category": "SaaS / AI app",
                "their_angle": "Self-serve customer retention analytics for mobile subscription apps",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    res_tg = push_discovered_now(conn)
    assert res_tg["ok"] is True
    assert res_tg["ingested"] == 1

    after_tg = get_campaign(conn, camp_vse2["id"])
    assert after_tg["daily_limit"] == 140
    camp_names_after = [
        r["name"] for r in conn.execute("SELECT name FROM campaigns ORDER BY id ASC").fetchall()
    ]
    assert "Leadgen Auto (WA)" not in camp_names_after
    assert "TG Вакансии" not in camp_names_after


def test_defect1b_sync_and_save_config_protect_running_daily_limit_and_empty_wa_and_zero_pytest_env_check(
    isolated_env,
):
    conn = isolated_env["conn"]
    tmp_path: Path = isolated_env["tmp_path"]

    leadgen_settings_src = (_WORKTREE_ROOT / "app" / "leadgen_settings.py").read_text(
        encoding="utf-8"
    )
    assert "PYTEST_CURRENT_TEST" not in leadgen_settings_src

    camp_running = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    # Save config with lower campaign_daily_limit=30 WITHOUT sync_campaign_limits=True
    saved_cfg = save_leadgen_config(
        conn,
        {
            "b2b": {"campaign_daily_limit": 30},
            "cases_bank_md": "# Тестовый банк кейсов без мутации репо",
        },
        sync_cases_file=False,
    )
    assert get_campaign(conn, camp_running["id"])["daily_limit"] == 140

    # Sync campaigns without sync_campaign_limits=True:
    # must NOT overwrite running campaign daily_limit downward (140 -> 30)
    # and must NOT create Leadgen Auto (WA) when 0 WA accounts & 0 WA leads exist.
    sync_res = sync_leadgen_campaigns(conn, saved_cfg)
    assert sync_res["ok"] is True
    assert get_campaign(conn, camp_running["id"])["daily_limit"] == 140
    names_no_wa = {c["name"] for c in sync_res["campaigns"]}
    assert "Leadgen Auto (WA)" not in names_no_wa
    assert "TG Вакансии" not in names_no_wa

    # Explicit sync_campaign_limits=True allows updating daily_limit to 30
    sync_explicit = sync_leadgen_campaigns(conn, saved_cfg, sync_campaign_limits=True)
    assert sync_explicit["ok"] is True
    assert get_campaign(conn, camp_running["id"])["daily_limit"] == 30

    # Adding a connected WhatsApp account unlocks Leadgen Auto (WA) creation on sync
    wa_sess = tmp_path / "wa_79990001122.session"
    wa_sess.write_text("wa-stub", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, health, provider)
        VALUES ('+79990001122', ?, 'connected', ?, 'active', 'whatsapp')
        """,
        (str(wa_sess), utc_now()),
    )
    conn.commit()

    sync_with_wa = sync_leadgen_campaigns(conn, saved_cfg)
    names_with_wa = {c["name"] for c in sync_with_wa["campaigns"]}
    assert "Leadgen Auto (WA)" in names_with_wa


def test_defect2_validate_isolated_scraper_session_blocks_live_outreach_sessions(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    from leadgen.vps_autonomous_replenish import (
        run_autonomous_replenish,
        validate_isolated_scraper_session,
    )

    conn = isolated_env["conn"]
    tmp_path: Path = isolated_env["tmp_path"]

    outreach_dir = tmp_path / "outreach_sessions"
    outreach_dir.mkdir(parents=True, exist_ok=True)
    live_session = outreach_dir / "vlad_outreach_main.session"
    live_session.write_text("telethon-live-outreach", encoding="utf-8")

    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, health, provider)
        VALUES ('+15550001111', ?, 'connected', ?, 'active', 'telegram')
        """,
        (str(live_session), utc_now()),
    )
    conn.commit()

    # 1) Exact path match -> ValueError
    with pytest.raises(ValueError, match="боевой сессией|session|vlad_outreach_main"):
        validate_isolated_scraper_session(conn, str(live_session))

    # 2) Symlink pointing to live outreach session (realpath match) -> ValueError
    symlink_session = tmp_path / "disguised_scraper.session"
    symlink_session.symlink_to(live_session)
    with pytest.raises(ValueError):
        validate_isolated_scraper_session(conn, str(symlink_session))

    # 3) Same filename in another folder -> ValueError
    copied_same_name = isolated_env["scraper_dir"] / "vlad_outreach_main.session"
    copied_same_name.write_text("duplicate-name", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_isolated_scraper_session(conn, str(copied_same_name))
    copied_same_name.unlink()

    # 4) Same path without .session suffix -> ValueError
    with pytest.raises(ValueError):
        validate_isolated_scraper_session(conn, str(outreach_dir / "vlad_outreach_main"))

    # 5) run_autonomous_replenish also fails fast with ValueError on colliding SCRAPER_TG_SESSION
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(symlink_session))
    with pytest.raises(ValueError):
        run_autonomous_replenish(conn)

    # 6) Truly isolated session passes cleanly and contains zero PYTEST_CURRENT_TEST checks
    isolated_session = isolated_env["scraper_dir"] / "dedicated_scraper_01.session"
    isolated_session.write_text("telethon-isolated-scraper", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(isolated_session))
    validated = validate_isolated_scraper_session(conn)
    assert validated is not None
    assert validated.resolve() == isolated_session.resolve()

    replenish_src = (_WORKTREE_ROOT / "leadgen" / "vps_autonomous_replenish.py").read_text(
        encoding="utf-8"
    )
    assert "PYTEST_CURRENT_TEST" not in replenish_src


def test_defect3_autonomous_low_water_mark_replenish_quarantine_subtraction_and_rule5_preview(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    from leadgen.vps_autonomous_replenish import run_autonomous_replenish

    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]

    # A) Disabled in config -> immediate clean skip
    save_leadgen_config(conn, {"enabled": False})
    disabled_res = run_autonomous_replenish(conn)
    assert disabled_res == {"ok": True, "skipped": "disabled_in_config"}

    # Re-enable leadgen
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "tg_groups_old": ["solofounders", "id1613398776"],
                "tg_groups_new": ["solofounderschat", "shopifyeasy"],
            },
            "vacancies": {
                "enabled": True,
                "extra_chats": ["itconnectbelgrade", "ai_jobs_clean"],
            },
        },
    )

    camp_b2b = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    # Quarantine bad sources in kv_settings['quarantined_leadgen_sources']
    now = utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('quarantined_leadgen_sources', ?, ?)
        """,
        (json.dumps(["@solofounderschat", "https://t.me/id1613398776", "itconnectbelgrade"]), now),
    )
    conn.commit()

    # B) Without SCRAPER_TG_SESSION, but with RELAYFAST_API_KEY:
    # runs web/API stages -> enrich -> llm_qualify -> curate, NEVER runs tg_scrape_posters or job_monitor.
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-fake-key-no-live-calls")
    calls_no_tg: list[list[str]] = []

    def runner_no_tg(cmd, **kwargs):
        cmd_list = [str(x) for x in cmd]
        calls_no_tg.append(cmd_list)
        if "tg_llm_qualify.py" in " ".join(cmd_list) and "--output" in cmd_list:
            out_file = Path(cmd_list[cmd_list.index("--output") + 1])
            out_file.write_text(
                json.dumps({"username": "@qual_probe", "icp_match": True}) + "\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res_no_tg = run_autonomous_replenish(conn, runner=runner_no_tg)
    assert res_no_tg["ok"] is True
    joined_no_tg = [" ".join(c) for c in calls_no_tg]
    assert any("buyer_catalogs.py" in c for c in joined_no_tg)
    assert any("yc_ph_leads.py" in c for c in joined_no_tg)
    assert any("creator_external_leads.py" in c for c in joined_no_tg)
    assert any("tg_enrich_leads.py" in c for c in joined_no_tg)
    assert any("tg_llm_qualify.py" in c for c in joined_no_tg)
    assert any("tg_curate.py" in c for c in joined_no_tg)
    assert not any("tg_scrape_posters.py" in c for c in joined_no_tg)
    assert not any("job_monitor.py" in c for c in joined_no_tg)

    # C) With isolated SCRAPER_TG_SESSION: runs TG scrapers with quarantined chats subtracted,
    # ingests fresh leads (3+ for Rule 5 sample), runs Rule 5 preview_outreach_batch,
    # preserves daily_limit=140, and stamps cloud_persistence_dirty_at!
    isolated_session = isolated_env["scraper_dir"] / "vps_isolated_scraper.session"
    isolated_session.write_text("isolated-session-bytes", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(isolated_session))

    calls_with_tg: list[tuple[list[str], dict]] = []

    def runner_with_tg(cmd, **kwargs):
        cmd_list = [str(x) for x in cmd]
        calls_with_tg.append((cmd_list, kwargs))
        cmd_str = " ".join(cmd_list)
        if "tg_llm_qualify.py" in cmd_str and "--output" in cmd_list:
            out_idx = cmd_list.index("--output") + 1
            Path(cmd_list[out_idx]).write_text(
                json.dumps({"username": "@founder_b2b_1", "icp_match": True}) + "\n",
                encoding="utf-8",
            )
        elif "tg_curate.py" in cmd_str:
            out_idx = cmd_list.index("--output") + 1
            out_file = Path(cmd_list[out_idx])
            rows = [
                {
                    "username": f"@founder_b2b_{idx}",
                    "name": f"Founder {idx}",
                    "role": "Founder",
                    "roles": ["Founder"],
                    "company": f"CloudApp{idx}",
                    "website": f"https://cloudapp{idx}.io",
                    "category": "SaaS / AI app",
                    "their_angle": f"B2B subscription analytics platform #{idx}",
                }
                for idx in (1, 2, 3)
            ]
            out_file.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                encoding="utf-8",
            )
        elif "job_monitor.py" in cmd_str and "--out" in cmd_list:
            out_idx = cmd_list.index("--out") + 1
            Path(cmd_list[out_idx]).write_text(
                json.dumps(
                    [
                        {
                            "hash": "vh_1",
                            "role": "AI Reels Producer",
                            "why": "Нужны вертикальные ролики",
                            "pay": "$2500",
                            "contact": "@hr_ai_reels",
                            "layer": "AI-контент",
                            "hot": True,
                            "remote": True,
                            "src": "ai_jobs_clean",
                            "url": "https://t.me/ai_jobs_clean/55",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        elif "job_reply.py" in cmd_str and "--out" in cmd_list:
            out_idx = cmd_list.index("--out") + 1
            Path(cmd_list[out_idx]).write_text(
                json.dumps(
                    [
                        {
                            "hash": "vh_1",
                            "text": "Добрый день! Готов закрыть продакшн AI-рилсов под ключ. Какой объём в месяц?",
                            "followup_1": "Короткий пинг по задаче AI Reels Producer.",
                            "followup_2": "Финальный пинг по актуальности вакансии.",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res_with_tg = run_autonomous_replenish(conn, runner=runner_with_tg)
    assert res_with_tg["ok"] is True
    assert res_with_tg["ingested_total"] >= 4
    assert get_campaign(conn, camp_b2b["id"])["daily_limit"] == 140

    # Check tg_scrape_posters.py got ONLY non-quarantined groups (solofounders, shopifyeasy)
    scrape_calls = [c for c, _ in calls_with_tg if any("tg_scrape_posters.py" in p for p in c)]
    assert len(scrape_calls) == 1
    groups_arg = scrape_calls[0][scrape_calls[0].index("--groups") + 1]
    groups_set = set(groups_arg.split(","))
    assert groups_set == {"solofounders", "shopifyeasy"}
    assert "solofounderschat" not in groups_set
    assert "id1613398776" not in groups_set

    # Check job_monitor.py received isolated TG_JOBS_SESSION and quarantined itconnectbelgrade removed
    jm_calls = [(c, kw) for c, kw in calls_with_tg if any("job_monitor.py" in p for p in c)]
    assert len(jm_calls) == 1
    jm_env = jm_calls[0][1].get("env") or {}
    assert Path(jm_env["TG_JOBS_SESSION"]).resolve() == isolated_session.resolve()
    eff_cfg = json.loads(Path(jm_env["LEADGEN_CONFIG_PATH"]).read_text(encoding="utf-8"))
    assert "itconnectbelgrade" not in eff_cfg["vacancies"]["extra_chats"]
    assert "ai_jobs_clean" in eff_cfg["vacancies"]["extra_chats"]

    # Check Rule 5 preview ran cleanly and cloud_persistence_dirty_at was updated
    assert res_with_tg["rule5_previews"]["SoftUGC — База Все2"]["ok"] is True
    dirty_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'cloud_persistence_dirty_at'"
    ).fetchone()
    assert dirty_row is not None and dirty_row["value"]


def test_defect4_early_warning_alert_idempotent_once_per_day_and_systemd_unit_wired(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    from leadgen.vps_autonomous_replenish import main as replenish_cli_main
    from leadgen.vps_autonomous_replenish import run_autonomous_replenish

    conn = isolated_env["conn"]
    db_path: Path = isolated_env["db_path"]

    # Configure operator bot credentials in kv_settings and capture app.digest._post
    now = utc_now()
    conn.executemany(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("bot_token", "123456:TEST_BOT_TOKEN_LOCAL", now),
            ("bot_chat_id", "-10099887766", now),
        ],
    )
    conn.commit()

    sent_alerts: list[dict] = []

    def fake_digest_post(url: str, payload: dict, opener=None) -> dict:
        sent_alerts.append({"url": url, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(app_digest, "_post", fake_digest_post)

    # Active running B2B campaign with only 4 pending recipients (< critical_low_mark=15)
    camp_b2b = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    for idx in range(4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, username, name, company, status, current_step, step1_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                camp_b2b["id"],
                f"seed_pending_{idx}",
                f"Seed {idx}",
                f"SeedCo{idx}",
                f"Hey Seed {idx}, quick SoftUGC idea for SeedCo{idx}.",
                now,
                now,
            ),
        )
    conn.commit()

    def noop_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # First run: pending=4 < 15, missing SCRAPER_TG_SESSION and RELAYFAST_API_KEY -> sends 1 alert
    res1 = run_autonomous_replenish(conn, runner=noop_runner)
    assert res1["ok"] is True
    assert res1["pending_b2b"] == 4
    assert len(sent_alerts) == 1
    alert_text = sent_alerts[0]["payload"]["text"]
    assert "4" in alert_text
    assert "SCRAPER_TG_SESSION" in alert_text
    assert "RELAYFAST_API_KEY" in alert_text

    kv_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'leadgen_low_water_alert'"
    ).fetchone()
    assert kv_row is not None
    alert_meta = json.loads(kv_row["value"])
    assert alert_meta["pending"] == 4
    assert alert_meta["alert_sent"] is True

    # Second run same day: idempotent (must NOT spam a 2nd alert within 24h)
    res2 = run_autonomous_replenish(conn, runner=noop_runner)
    assert res2["ok"] is True
    assert len(sent_alerts) == 1

    # Simulate >24h elapsed since last_alert_sent_at AND both session + LLM key configured ->
    # sends fresh alert citing "источники исчерпаны"
    isolated_session = isolated_env["scraper_dir"] / "alert_scraper.session"
    isolated_session.write_text("isolated", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(isolated_session))
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-fake-key")

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S")
    alert_meta["last_alert_sent_at"] = old_ts
    conn.execute(
        "UPDATE kv_settings SET value = ? WHERE key = 'leadgen_low_water_alert'",
        (json.dumps(alert_meta, ensure_ascii=False),),
    )
    conn.commit()

    res3 = run_autonomous_replenish(conn, runner=noop_runner)
    assert res3["ok"] is True
    assert len(sent_alerts) == 2
    assert "источники исчерпаны" in sent_alerts[1]["payload"]["text"]

    # CLI entrypoint check (--db when disabled returns 0 cleanly)
    save_leadgen_config(conn, {"enabled": False})
    assert replenish_cli_main(["--db", str(db_path)]) == 0

    # Check systemd unit ExecStart uses leadgen.vps_autonomous_replenish
    service_text = (
        _WORKTREE_ROOT / "deploy" / "systemd" / "crmtg-leadgen.service"
    ).read_text(encoding="utf-8")
    assert (
        "ExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish --db /opt/crmtg/data/crm.db"
        in service_text
    )
    assert "push_discovered_now" not in service_text


def test_rule5_strict_preview_blocks_forbidden_brand_on_replenished_campaign(
    isolated_env,
):
    from leadgen.vps_autonomous_replenish import run_autonomous_replenish

    conn = isolated_env["conn"]
    now = utc_now()
    camp_b2b = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=140,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    def poisoned_runner(cmd, **kwargs):
        cmd_str = " ".join(str(x) for x in cmd)
        if "tg_enrich_leads.py" in cmd_str:
            # Simulate a poisoned follow-up-as-step1 recipient inserted before Rule 5 strict gate
            conn.execute(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, username, name, company, status, current_step, step1_text, created_at, updated_at
                ) VALUES (?, 'poisoned_lead', 'Bad Copy', 'Acme', 'pending', 0, 'Just following up on my previous message — did you see my note?', ?, ?)
                """,
                (camp_b2b["id"], now, now),
            )
            conn.commit()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res_poison = run_autonomous_replenish(conn, runner=poisoned_runner)
    assert res_poison["ok"] is True
    assert res_poison["pending_b2b"] == 0
    rec_row = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'poisoned_lead'"
    ).fetchone()
    assert rec_row["status"] == "completed"
    assert rec_row["error_text"] == "Карантин Rule 5 Dry-Run"


def _write_sqlite_telethon_session(path: Path, auth_key: bytes, dc_id: int = 2) -> Path:
    import sqlite3 as _sqlite3

    assert len(auth_key) == 256
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with _sqlite3.connect(path) as sconn:
        sconn.execute(
            """
            CREATE TABLE sessions (
                dc_id INTEGER PRIMARY KEY,
                server_address TEXT,
                port INTEGER,
                auth_key BLOB,
                takeout_id INTEGER
            )
            """
        )
        sconn.execute(
            "INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id) VALUES (?, '149.154.167.50', 443, ?, NULL)",
            (dc_id, auth_key),
        )
        sconn.commit()
    return path


def _encode_telethon_string_session(auth_key: bytes, dc_id: int = 2) -> str:
    import base64
    import socket
    import struct

    assert len(auth_key) == 256
    packed = struct.pack(">B4sH256s", dc_id, socket.inet_aton("149.154.167.50"), 443, auth_key)
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


def test_skeptic1_fail_closed_llm_qualify_yc_ph_stage_errors_and_bounded_external(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    import leadgen.creator_external_leads as cel
    from leadgen.vps_autonomous_replenish import run_autonomous_replenish

    conn = isolated_env["conn"]
    save_leadgen_config(
        conn,
        {
            "enabled": True,
            "b2b": {
                "enabled": True,
                "sources": {"buyer_catalogs": True, "yc_ph": True, "tg_groups": True},
            },
            "vacancies": {"enabled": False},
        },
    )
    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=30,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-fake-llm-key")

    seen_cmds: list[list[str]] = []

    def fail_closed_runner(cmd, **kwargs):
        cmd_list = [str(x) for x in cmd]
        seen_cmds.append(cmd_list)
        cmd_str = " ".join(cmd_list)
        if "buyer_catalogs.py" in cmd_str:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="catalog upstream 502")
        if "creator_external_leads.py" in cmd_str:
            out_file = Path(cmd_list[cmd_list.index("--output") + 1])
            out_file.write_text(
                json.dumps(
                    {
                        "username": "@unqualified_agency_spam",
                        "telegram_username": "unqualified_agency_spam",
                        "name": "Random Dev",
                        "company": "SpamSoft",
                        "project": "SpamSoft",
                        "project_url": "https://spamsoft.io",
                        "bio": "Building SaaS dashboards",
                        "what_they_do": "SaaS analytics tool for teams",
                        "category": "SaaS / AI app",
                        "roles": ["Founder"],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_enrich_leads.py" in cmd_str:
            inp_file = Path(cmd_list[cmd_list.index("--input") + 1])
            out_file = Path(cmd_list[cmd_list.index("--output") + 1])
            out_file.write_text(inp_file.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_llm_qualify.py" in cmd_str:
            # LLM qualifier rejects ALL candidates -> writes 0-byte qualified_jsonl!
            out_file = Path(cmd_list[cmd_list.index("--output") + 1])
            out_file.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "tg_curate.py" in cmd_str:
            # If called at all on fallback, it would pass unqualified leads through!
            inp_file = Path(cmd_list[cmd_list.index("--input") + 1])
            out_file = Path(cmd_list[cmd_list.index("--output") + 1])
            out_file.write_text(inp_file.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = run_autonomous_replenish(conn, runner=fail_closed_runner)
    joined = [" ".join(c) for c in seen_cmds]

    # 1) yc_ph_leads.py wired in web sources in --no-tg mode when scraper session is absent
    yc_calls = [c for c in seen_cmds if any("yc_ph_leads.py" in part for part in c)]
    assert len(yc_calls) == 1
    assert "--no-tg" in yc_calls[0]

    # 2) Fail-Closed: 0 leads pass when qualified_jsonl is empty (never falls back to enrich_src!)
    assert not any("tg_curate.py" in c for c in joined)
    assert res["ingested_total"] == 0
    assert res["pending_b2b"] == 0

    # 3) Stage errors recorded in return payload AND kv_settings
    assert isinstance(res.get("stage_errors"), list)
    assert any(err.get("stage") == "buyer_catalogs" and err.get("returncode") == 2 for err in res["stage_errors"])
    kv_err = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'leadgen_stage_errors'"
    ).fetchone()
    assert kv_err is not None
    stored_errs = json.loads(kv_err["value"])
    assert any(err.get("stage") == "buyer_catalogs" for err in stored_errs)

    # 4) creator_external_leads bounded HTTP timeout (<= 4s) and ThreadPoolExecutor concurrency
    assert cel.TIMEOUT <= 4
    cel_src = (_WORKTREE_ROOT / "leadgen" / "creator_external_leads.py").read_text(encoding="utf-8")
    assert "ThreadPoolExecutor" in cel_src


def test_skeptic2_push_discovered_now_watermark_prevents_reprocessing_same_final_jsonl(
    isolated_env, monkeypatch: pytest.MonkeyPatch
):
    import leadgen.crm_mcp_push as crm_mcp_push

    conn = isolated_env["conn"]
    state_dir: Path = isolated_env["state_dir"]

    create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=30,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    final_jsonl = state_dir / "wm_batch_01.final.jsonl"
    row1 = {
        "username": "@wm_founder_one",
        "name": "Liam Vance",
        "role": "Founder",
        "roles": ["Founder"],
        "company": "MetricPulse",
        "website": "https://metricpulse.ai",
        "category": "SaaS / AI app",
        "their_angle": "Product analytics for mobile AI apps",
    }
    final_jsonl.write_text(json.dumps(row1, ensure_ascii=False) + "\n", encoding="utf-8")

    build_calls: list[str] = []
    orig_build = crm_mcp_push.build_lead_payload_from_dict

    def spy_build(rec, *args, **kwargs):
        build_calls.append(str(rec.get("username") or ""))
        return orig_build(rec, *args, **kwargs)

    monkeypatch.setattr(crm_mcp_push, "build_lead_payload_from_dict", spy_build)

    res1 = push_discovered_now(conn)
    assert res1["ingested"] == 1
    assert len(build_calls) == 1

    wm_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'leadgen_push_watermark'"
    ).fetchone()
    assert wm_row is not None and "wm_batch_01.final.jsonl" in wm_row["value"]

    # Second tick on unchanged file: watermark skips re-reading lines from scratch (0 build calls)
    build_calls.clear()
    res2 = push_discovered_now(conn)
    assert res2["ingested"] == 0
    assert build_calls == []

    # Appending a 2nd line to the same file reads ONLY from the saved line offset!
    row2 = {
        "username": "@wm_founder_two",
        "name": "Noah Sterling",
        "role": "Founder",
        "roles": ["Founder"],
        "company": "ClipOrbit",
        "website": "https://cliporbit.io",
        "category": "SaaS / AI app",
        "their_angle": "Subscription growth toolkit for consumer apps",
    }
    with final_jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row2, ensure_ascii=False) + "\n")

    res3 = push_discovered_now(conn)
    assert res3["ingested"] == 1
    assert [u.lstrip("@") for u in build_calls] == ["wm_founder_two"]


def test_skeptic3_rule5_quarantines_defective_newly_inserted_recipients(isolated_env):
    from leadgen.vps_autonomous_replenish import run_autonomous_replenish

    conn = isolated_env["conn"]
    now = utc_now()
    camp_b2b = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=30,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=True,
    )

    def mixed_runner(cmd, **kwargs):
        cmd_str = " ".join(str(x) for x in cmd)
        if "tg_enrich_leads.py" in cmd_str:
            conn.executemany(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, username, name, company, status, current_step, step1_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                [
                    (
                        camp_b2b["id"],
                        "clean_fresh_founder",
                        "Clean Founder",
                        "CleanSaaS",
                        "Hey Clean Founder, quick SoftUGC growth idea for CleanSaaS.",
                        now,
                        now,
                    ),
                    (
                        camp_b2b["id"],
                        "bad_creatoros_lead",
                        "Bad Brand",
                        "LegacyCo",
                        "Hey! CreatorOS can scale LegacyCo with UGC videos.",
                        now,
                        now,
                    ),
                    (
                        camp_b2b["id"],
                        "bad_followup_lead",
                        "Bad Follow",
                        "FollowCo",
                        "Just following up on my previous message — did you see my note?",
                        now,
                        now,
                    ),
                ],
            )
            conn.commit()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    res = run_autonomous_replenish(conn, runner=mixed_runner)
    assert res["ok"] is True

    rows = {
        r["username"]: dict(r)
        for r in conn.execute(
            "SELECT username, status, error_text FROM campaign_recipients WHERE campaign_id = ?",
            (camp_b2b["id"],),
        ).fetchall()
    }
    assert rows["clean_fresh_founder"]["status"] == "pending"
    assert rows["bad_creatoros_lead"]["status"] == "completed"
    assert rows["bad_creatoros_lead"]["error_text"] == "Карантин Rule 5 Dry-Run"
    assert rows["bad_followup_lead"]["status"] == "completed"
    assert rows["bad_followup_lead"]["error_text"] == "Карантин Rule 5 Dry-Run"


def test_skeptic4_validate_isolated_scraper_session_blocks_copied_auth_key_fingerprint(
    isolated_env,
):
    from leadgen.vps_autonomous_replenish import validate_isolated_scraper_session

    conn = isolated_env["conn"]
    tmp_path: Path = isolated_env["tmp_path"]
    scraper_dir: Path = isolated_env["scraper_dir"]

    shared_key = bytes([0x5A]) * 256
    distinct_key = bytes([0xA5]) * 256

    copied_scraper = _write_sqlite_telethon_session(
        scraper_dir / "renamed_innocent_scraper.session", shared_key
    )

    # Case A: Outreach account stores identical auth_key in accounts.session_blob under a totally different filename
    blob_bytes = copied_scraper.read_bytes()
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, health, provider, session_blob)
        VALUES ('+15551112222', 'data/sessions/27.session', 'connected', ?, 'active', 'telegram', ?)
        """,
        (utc_now(), blob_bytes),
    )
    conn.commit()

    with pytest.raises(
        ValueError, match="Scraper session shares MTProto auth_key with outreach account"
    ):
        validate_isolated_scraper_session(conn, str(copied_scraper))

    conn.execute("DELETE FROM accounts")
    conn.commit()

    # Case B: Outreach account stores identical auth_key ONLY in accounts.session_string
    sess_str = _encode_telethon_string_session(shared_key)
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at, health, provider, session_string)
        VALUES ('+15553334444', 'data/sessions/28.session', 'connected', ?, 'active', 'telegram', ?)
        """,
        (utc_now(), sess_str),
    )
    conn.commit()

    with pytest.raises(
        ValueError, match="Scraper session shares MTProto auth_key with outreach account"
    ):
        validate_isolated_scraper_session(conn, str(copied_scraper))

    conn.execute("DELETE FROM accounts")
    conn.commit()

    # Case C: A live session file in data/sessions/*.session (adjacent to crm.db) shares auth_key
    sessions_disk_dir = tmp_path / "sessions"
    _write_sqlite_telethon_session(sessions_disk_dir / "29.session", shared_key)

    with pytest.raises(
        ValueError, match="Scraper session shares MTProto auth_key with outreach account"
    ):
        validate_isolated_scraper_session(conn, str(copied_scraper))

    # Distinct 256-byte auth_key passes cleanly
    clean_scraper = _write_sqlite_telethon_session(
        scraper_dir / "truly_independent_scraper.session", distinct_key
    )
    validated = validate_isolated_scraper_session(conn, str(clean_scraper))
    assert validated is not None
    assert validated.resolve() == clean_scraper.resolve()


def test_skeptic5_sync_campaigns_never_mutates_existing_limits_or_enabled_and_verify_gates(
    isolated_env,
):
    conn = isolated_env["conn"]

    # 1) Canary running campaign with manual daily_limit=5 (< 30) and leadgen_enabled=0
    camp_canary = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        daily_limit=5,
        status="running",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=False,
    )
    # 2) Paused vacancies campaign with manual daily_limit=100 (> 20) and leadgen_enabled=0
    camp_paused_vac = create_campaign(
        conn,
        name="TG Вакансии",
        daily_limit=100,
        status="paused",
        steps=DEFAULT_B2B_STEPS,
        leadgen_enabled=False,
    )

    cfg = save_leadgen_config(
        conn,
        {
            "b2b": {"campaign_daily_limit": 30},
            "vacancies": {"daily_limit": 20},
        },
        sync_cases_file=False,
    )
    sync_leadgen_campaigns(conn, cfg, sync_campaign_limits=False)

    after_canary = get_campaign(conn, camp_canary["id"])
    after_vac = get_campaign(conn, camp_paused_vac["id"])
    assert after_canary["daily_limit"] == 5
    assert bool(after_canary["leadgen_enabled"]) is False
    assert after_vac["daily_limit"] == 100
    assert bool(after_vac["leadgen_enabled"]) is False

    # 3) Zero *_IN_TEST env vars in app/ and leadgen/ + CRMTG_VPS_RUNTIME=1 in all systemd .service + verify.sh gates
    for folder in ("app", "leadgen"):
        for py_file in (_WORKTREE_ROOT / folder).rglob("*.py"):
            assert "_IN_TEST" not in py_file.read_text(encoding="utf-8"), f"Found _IN_TEST in {py_file}"

    for svc_file in (_WORKTREE_ROOT / "deploy" / "systemd").glob("*.service"):
        svc_txt = svc_file.read_text(encoding="utf-8")
        assert 'Environment="CRMTG_VPS_RUNTIME=1"' in svc_txt, f"Missing CRMTG_VPS_RUNTIME=1 in {svc_file}"

    verify_txt = (_WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    assert "_IN_TEST" in verify_txt
    assert "CRMTG_VPS_RUNTIME=1" in verify_txt
    assert "leadgen.vps_autonomous_replenish" in verify_txt and "--help" in verify_txt


