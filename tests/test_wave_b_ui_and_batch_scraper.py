from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now
from app.hygiene import read_leadgen_health_summary
from app.main import create_app
from app.outreach import create_campaign
from app.telegram_gateway import FakeGateway


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _resolve_playwright_node_path() -> str:
    root = Path(__file__).resolve().parent.parent
    candidates = [root / "e2e" / "node_modules"]
    try:
        common = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            text=True,
        ).strip()
        if common:
            main_root = (root / common).resolve().parent
            candidates.append(main_root / "e2e" / "node_modules")
    except Exception:
        pass
    parts = [str(p) for p in candidates if p.is_dir()]
    if os.environ.get("NODE_PATH"):
        parts.append(os.environ["NODE_PATH"])
    return os.pathsep.join(parts)


def _make_sqlite_session(path: Path, auth_key: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, auth_key BLOB)")
    conn.execute("INSERT INTO sessions VALUES (2, '149.154.167.50', 443, ?)", (auth_key,))
    conn.commit()
    conn.close()
    return path


def _make_client(tmp_path: Path, monkeypatch) -> tuple[sqlite3.Connection, TestClient]:
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path / "leadgen_state"))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(tmp_path / "scraper_sessions"))
    conn = connect(tmp_path / "wave_b_test.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    return conn, TestClient(app)


def _insert_lead(
    conn: sqlite3.Connection,
    *,
    name: str,
    username: str,
    lead_source: str,
    created_at: str,
    company: str = "AcmeCo",
    segment: str = "whale",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO leads (
            username, name, company, segment, language, provider, lead_source, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'en', 'telegram', ?, 'new', ?, ?)
        """,
        (username, name, company, segment, lead_source, created_at, created_at),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_health_summary_source_funnel_scraper_health_and_reverse_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sess_dir = tmp_path / "scraper_sessions"
    _make_sqlite_session(sess_dir / "signals_ro_1.session", b"R" * 256)
    _make_sqlite_session(sess_dir / "phone_checker_1.session", b"P" * 256)
    _make_sqlite_session(sess_dir / "phone_checker_2.session", b"Q" * 256)

    now_dt = datetime.now(timezone.utc)
    ts_2h = (now_dt - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    ts_3d = (now_dt - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    ts_12d = (now_dt - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%S")
    cooldown_future = (now_dt + timedelta(hours=24)).timestamp()

    (state_dir / "last_run_summary.json").write_text(
        json.dumps(
            {
                "timestamp": ts_2h,
                "stage_error_count": 0,
                "per_source_counts": {
                    "tg_ad_spy": 18,
                    "tma_apps": 9,
                    "channel_graph": 12,
                    "buyer_public": 0,
                },
                "cooldown_sources": ["buyer_public"],
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "source_yield_state.json").write_text(
        json.dumps(
            {
                "sources": {
                    "buyer_public": {
                        "zero_streak": 3,
                        "cooldown_until": cooldown_future,
                        "last_yield": 0,
                    },
                    "tg_ad_spy": {
                        "zero_streak": 0,
                        "cooldown_until": 0.0,
                        "last_yield": 18,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "pending_tg_verify_queue.jsonl").write_text(
        json.dumps({"username": "cand_1"}) + "\n" + json.dumps({"username": "cand_2"}) + "\n",
        encoding="utf-8",
    )
    (state_dir / "manual_join_queue.jsonl").write_text(
        json.dumps({"group": "captcha_chat_1"}) + "\n",
        encoding="utf-8",
    )
    (state_dir / "direct_x_leads.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"company": "EmailOnly", "email": "ceo@emailonly.ai", "lead_source": "yc"}),
                json.dumps({"company": "WaOnly", "whatsapp": "+14155550199", "lead_source": "d2c_whatsapp"}),
                json.dumps({"company": "XOnly", "x_url": "https://x.com/xonly", "lead_source": "ph_detailed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('leadgen_stage_errors', ?, ?)",
        (json.dumps([{"stage": "facebook_groups", "error": "timeout 60s", "at": ts_2h}]), ts_2h),
    )

    camp = create_campaign(conn, name="Whales EN — TG", daily_limit=30)
    cid = int(camp["id"])

    l1 = _insert_lead(conn, name="Lead Spy 1", username="spy_1", lead_source="tg_ad_spy", created_at=ts_2h)
    l2 = _insert_lead(conn, name="Lead Spy 2", username="spy_2", lead_source="tg_ad_spy", created_at=ts_3d)
    _insert_lead(conn, name="Lead Spy Old", username="spy_old", lead_source="tg_ad_spy", created_at=ts_12d)
    l_cg = _insert_lead(conn, name="Lead CG", username="cg_1", lead_source="tg_channel_graph", created_at=ts_2h)
    l_tma = _insert_lead(conn, name="Lead TMA", username="tma_1", lead_source="tma_apps", created_at=ts_2h)

    for lid, st in ((l1, "replied"), (l2, "completed"), (l_cg, "pending"), (l_tma, "replied")):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, username, name, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (cid, lid, f"u_{lid}", f"Name {lid}", st, ts_2h, ts_2h),
        )
    conn.commit()

    try:
        with client:
            resp = client.get("/api/leadgen/health-summary")
            assert resp.status_code == 200
            data = resp.json()

        assert data["reverse_bridge_ready_count"] == 2

        sh = data["scraper_health"]
        assert [s["name"] for s in sh["ro_sessions"]] == ["signals_ro_1.session"]
        assert sh["ro_sessions"][0]["present"] is True
        assert sh["ro_sessions"][0]["size_bytes"] > 0
        assert sh["ro_sessions"][0]["updated_at"]
        assert [s["name"] for s in sh["phone_checker_sessions"]] == [
            "phone_checker_1.session",
            "phone_checker_2.session",
        ]
        assert sh["pending_tg_verify_count"] == 2
        assert sh["manual_join_queue_count"] == 1
        assert "buyer_public" in sh["cooldown_sources"]
        assert len(sh["recent_errors"]) == 1
        assert sh["recent_errors"][0]["stage"] == "facebook_groups"
        assert sh["last_run_timestamp"] == ts_2h

        funnel_by_src = {row["source"]: row for row in data["source_funnel"]}
        spy = funnel_by_src["tg_ad_spy"]
        assert spy["raw_last_run"] == 18
        assert spy["leads_24h"] == 1
        assert spy["leads_7d"] == 2
        assert spy["in_campaign_7d"] == 2
        assert spy["replied_7d"] == 1
        assert spy["conversion_pct"] == 50.0
        assert spy["cooldown_until"] is None

        # tg_channel_graph lead aligns cleanly onto channel_graph row
        cg = funnel_by_src["channel_graph"]
        assert cg["raw_last_run"] == 12
        assert cg["leads_24h"] == 1
        assert cg["in_campaign_7d"] == 1
        assert cg["replied_7d"] == 0
        assert cg["conversion_pct"] == 0.0

        tma = funnel_by_src["tma_apps"]
        assert tma["raw_last_run"] == 9
        assert tma["conversion_pct"] == 100.0

        bp = funnel_by_src["buyer_public"]
        assert bp["cooldown_until"] is not None

        direct = read_leadgen_health_summary(conn, state_dir=state_dir)
        assert direct["reverse_bridge_ready_count"] == 2
    finally:
        conn.close()


def test_batch_scrape_validation_400_and_limit_clamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        with client:
            client.app.state._batch_scrape_runner = lambda job: {"leads": [], "leads_found": 0}

            for bad_payload in (
                {"targets": []},
                {"targets": "not_a_list"},
                {"targets": [f"@chan_{i}" for i in range(11)]},
                {"targets": ["https://t.me/+privateInviteHash"]},
                {"targets": ["https://t.me/joinchat/AAAAAE"]},
                {"targets": ["https://evil.example.com/chan"]},
                {"targets": ["bad target with spaces"]},
                {"targets": ["@ab"]},
                {"targets": ["@valid_chan"], "mode": "mtproto"},
                {"targets": ["@valid_chan"], "limit": True},
            ):
                r = client.post("/api/leadgen/batch-scrape", json=bad_payload)
                assert r.status_code == 400, f"Expected 400 for {bad_payload}, got {r.status_code}: {r.text}"

            r_low = client.post(
                "/api/leadgen/batch-scrape",
                json={"targets": ["@ChanOne", "https://t.me/chan_two", "https://t.me/s/chan_three/99"], "limit": 1},
            )
            assert r_low.status_code == 200
            j_low = r_low.json()["job"]
            assert j_low["targets"] == ["ChanOne", "chan_two", "chan_three"]
            assert j_low["limit"] == 5

            r_high = client.post(
                "/api/leadgen/batch-scrape",
                json={"targets": ["@ChanOne"], "limit": 999},
            )
            assert r_high.status_code == 200
            assert r_high.json()["job"]["limit"] == 200

            assert client.get("/api/leadgen/batch-scrape/nonexistent").status_code == 404
    finally:
        conn.close()


def test_batch_scrape_web_zero_cost_no_bot_and_auto_ingest_rule5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import leadgen.tg_web_client as twc

    conn, client = _make_client(tmp_path, monkeypatch)
    fetch_calls: list[tuple[str, dict]] = []
    probed_handles: list[str] = []

    def fake_fetch_posts(channel: str, limit: int = 20, *, allow_paid: bool = True, **_kw):
        fetch_calls.append((channel, {"limit": limit, "allow_paid": allow_paid}))
        assert allow_paid is False
        return (
            {"title": "SaaS Founders Club", "about": "Founder community https://saasclub.io"},
            [
                "DM @alice_founder for SaaS reels https://aliceapp.io",
                "Support via @helper_bot or @spammybot",
                "Also reach https://t.me/bob_growth and channel @another_chan",
            ],
        )

    def fake_probe(handle: str, **_kw):
        probed_handles.append(handle)
        if handle == "alice_founder":
            return {"handle": "alice_founder", "kind": "user", "title": "Alice Founder", "bio": "CEO @ AliceApp"}
        if handle == "bob_growth":
            return {"handle": "bob_growth", "kind": "user", "title": "Bob Growth", "bio": "Head of Growth @ SaaS"}
        if handle == "another_chan":
            return {"handle": "another_chan", "kind": "channel", "title": "Another Chan", "bio": ""}
        return {"handle": handle, "kind": "bot", "title": "Bot", "bio": ""}

    monkeypatch.setattr(twc, "fetch_channel_posts_web", fake_fetch_posts)
    monkeypatch.setattr(twc, "probe_tme_handle_web", fake_probe)

    try:
        with client:
            r = client.post(
                "/api/leadgen/batch-scrape",
                json={
                    "targets": ["https://t.me/saas_founders"],
                    "mode": "web",
                    "limit": 25,
                    "auto_ingest": True,
                },
            )
            assert r.status_code == 200, r.text
            job_id = r.json()["job_id"]

            detail = None
            for _ in range(40):
                d = client.get(f"/api/leadgen/batch-scrape/{job_id}")
                assert d.status_code == 200
                detail = d.json()
                if detail["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)

            assert detail is not None
            assert detail["status"] == "completed", detail
            assert detail["leads_found"] == 2
            usernames = [ld["username"] for ld in detail["leads"]]
            assert usernames == ["alice_founder", "bob_growth"]
            assert all(not u.lower().endswith("bot") for u in usernames)
            assert all("bot" not in h.lower() for h in probed_handles)
            assert fetch_calls and fetch_calls[0][1]["allow_paid"] is False
            assert detail["ingested_count"] == 2
            assert detail["rule5_preview"]["ok"] is True
            assert detail["rule5_preview"]["forbidden_pattern_count"] == 0

            listing = client.get("/api/leadgen/batch-scrape").json()
            assert listing["count"] == 1
            assert listing["jobs"][0]["job_id"] == job_id

            db_users = {
                r["username"]
                for r in conn.execute("SELECT username FROM leads").fetchall()
                if r["username"]
            }
            assert {"alice_founder", "bob_growth"}.issubset(db_users)
    finally:
        conn.close()


def test_batch_scrape_hybrid_fail_closed_and_bounded_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    sess_dir = tmp_path / "scraper_sessions"
    outreach_dir = tmp_path / "outreach_sessions"
    outreach_auth = b"LIVE_OUTREACH_KEY_" + b"X" * 238
    live_sess = _make_sqlite_session(outreach_dir / "live_1.session", outreach_auth)
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, health, provider, daily_limit, created_at)
        VALUES ('+14155550111', ?, 'connected', 'active', 'telegram', 20, ?)
        """,
        (str(live_sess), utc_now()),
    )
    # Only phone_checker_1 exists -> NOT allowed as RO fallback for hybrid scraping!
    _make_sqlite_session(sess_dir / "phone_checker_1.session", b"P1" + b"A" * 254)
    conn.commit()

    try:
        with client:
            r_fail = client.post(
                "/api/leadgen/batch-scrape",
                json={"targets": ["@saasfounders"], "mode": "hybrid", "limit": 10},
            )
            assert r_fail.status_code == 400
            assert "RO-сессия" in r_fail.json()["detail"]

            # Even a copied auth_key from outreach in signals_ro_evil must fail closed
            _make_sqlite_session(sess_dir / "signals_ro_evil.session", outreach_auth)
            r_fail2 = client.post(
                "/api/leadgen/batch-scrape",
                json={"targets": ["@saasfounders"], "mode": "hybrid", "limit": 10},
            )
            assert r_fail2.status_code == 400

            # Valid isolated signals_ro_1 passes and bounded history keeps max 20 jobs
            _make_sqlite_session(sess_dir / "signals_ro_1.session", b"RO_ISOLATED_" + b"B" * 244)
            client.app.state._batch_scrape_runner = lambda job: {
                "leads": [{"username": "isolated_lead", "name": "Isolated Lead"}],
                "ro_session": "signals_ro_1.session",
            }
            for i in range(25):
                r_ok = client.post(
                    "/api/leadgen/batch-scrape",
                    json={"targets": [f"@chan_{i}"], "mode": "hybrid", "limit": 10},
                )
                assert r_ok.status_code == 200
            time.sleep(0.1)

            listed = client.get("/api/leadgen/batch-scrape").json()
            assert listed["count"] == 20
            assert len(client.app.state._batch_scrape_jobs) == 20
            assert listed["jobs"][0]["targets"] == ["chan_24"]
    finally:
        conn.close()


def test_batch_scrape_auth_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongWaveBPass!2026")
    conn, client = _make_client(tmp_path, monkeypatch)
    ua_headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0"}
    try:
        with client:
            client.app.state._batch_scrape_runner = lambda job: {"leads": []}
            assert client.get("/api/leadgen/batch-scrape", headers=ua_headers).status_code == 401
            assert (
                client.post(
                    "/api/leadgen/batch-scrape",
                    json={"targets": ["@saasfounders"], "mode": "web"},
                    headers=ua_headers,
                ).status_code
                == 401
            )
            login = client.post(
                "/api/auth/login",
                json={"username": "tim", "password": "StrongWaveBPass!2026"},
                headers=ua_headers,
            )
            assert login.status_code == 200
            assert client.post(
                "/api/leadgen/batch-scrape",
                json={"targets": ["@saasfounders"], "mode": "web"},
                headers=ua_headers,
            ).status_code == 200
            assert client.get("/api/leadgen/batch-scrape", headers=ua_headers).status_code == 200
    finally:
        conn.close()


def test_wave_b_funnel_and_batch_scraper_ui_playwright(tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "wave_b_ui.db"
    state_dir = tmp_path / "leadgen_state"
    sess_dir = tmp_path / "scraper_sessions"
    cfg_path = tmp_path / "leadgen_config.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    _make_sqlite_session(sess_dir / "signals_ro_1.session", b"R" * 256)
    _make_sqlite_session(sess_dir / "phone_checker_1.session", b"P" * 256)

    now_dt = datetime.now(timezone.utc)
    ts_now = now_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cooldown_future = (now_dt + timedelta(hours=36)).timestamp()

    (state_dir / "last_run_summary.json").write_text(
        json.dumps(
            {
                "timestamp": ts_now,
                "stage_error_count": 0,
                "per_source_counts": {"tg_ad_spy": 24, "tma_apps": 11, "buyer_public": 0},
                "cooldown_sources": ["buyer_public"],
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "source_yield_state.json").write_text(
        json.dumps(
            {
                "sources": {
                    "buyer_public": {"zero_streak": 3, "cooldown_until": cooldown_future},
                    "tg_ad_spy": {"zero_streak": 0, "cooldown_until": 0},
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "pending_tg_verify_queue.jsonl").write_text('{"username":"p1"}\n', encoding="utf-8")
    (state_dir / "manual_join_queue.jsonl").write_text('{"group":"captcha_group"}\n', encoding="utf-8")
    (state_dir / "direct_x_leads.jsonl").write_text(
        json.dumps({"company": "BridgeReadyCo", "email": "hi@bridgeready.io", "lead_source": "yc"}) + "\n",
        encoding="utf-8",
    )

    conn = connect(db_path)
    init_db(conn)
    camp = create_campaign(conn, name="Whales EN — TG", daily_limit=30)
    cid = int(camp["id"])
    l1 = _insert_lead(conn, name="Alice Spy", username="alice_spy", lead_source="tg_ad_spy", created_at=ts_now)
    l2 = _insert_lead(conn, name="Bob TMA", username="bob_tma", lead_source="tma_apps", created_at=ts_now)
    for lid, st in ((l1, "replied"), (l2, "pending")):
        step_idx = 1 if st == "replied" else 0
        sent_at = ts_now if step_idx >= 1 else None
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, username, name, company, website, their_angle,
                step1_text, current_step, status, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'AcmeCo', 'https://acmeco.io', 'B2B SaaS scaling daily AI UGC reels',
                      'Hey — saw AcmeCo scaling. SoftUGC builds daily AI UGC variations.', ?, ?, ?, ?, ?)
            """,
            (cid, lid, f"u_{lid}", f"User {lid}", step_idx, st, sent_at, ts_now, ts_now),
        )
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('leadgen_stage_errors', ?, ?)",
        (json.dumps([{"stage": "facebook_groups", "error": "timeout 60s", "at": ts_now}]), ts_now),
    )
    conn.commit()
    conn.close()

    admin_password = "WaveBPlaywrightPass!2026"
    screenshot_path = Path(os.environ.get("WAVE_B_SCREENSHOT_PATH") or (tmp_path / "wave-b-funnel.png"))
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["PYTHONPATH"] = str(root)
    env["CRM_REQUIRE_AUTH"] = "1"
    env["CRM_ADMIN_PASSWORD"] = admin_password
    env["CRM_VIEW_MODE"] = "sheet"
    env["CLOUD_BACKUP_ENABLED"] = "0"
    env["DISABLE_TG_WATCHES"] = "1"
    env["DEMO_AUTOSEED"] = "0"
    env["BOOTSTRAP_PROD_SNAPSHOT"] = "0"
    env["LEADGEN_CONFIG_PATH"] = str(cfg_path)
    env["LEADGEN_STATE_DIR"] = str(state_dir)
    env["SCRAPER_SESSIONS_DIR"] = str(sess_dir)

    server_code = f"""
import uvicorn
from pathlib import Path
import leadgen.tg_web_client as twc
from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway

def _fake_posts(ch, limit=20, **_kw):
    return (
        {{"title": "SaaS Founders", "about": "Product founders https://saasdemo.io"}},
        ["DM @alice_founder for UGC demo", "Bot @helper_bot ignore"],
    )

def _fake_probe(handle, **_kw):
    if handle == "alice_founder":
        return {{"handle": "alice_founder", "kind": "user", "title": "Alice Founder", "bio": "Founder @ SaaSDemo"}}
    return {{"handle": handle, "kind": "bot", "title": "Bot", "bio": ""}}

twc.fetch_channel_posts_web = _fake_posts
twc.probe_tme_handle_web = _fake_probe

conn = connect(Path({str(db_path)!r}))
init_db(conn)
app = create_app(conn=conn, gateway=FakeGateway())
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""

    server = subprocess.Popen(
        [sys.executable, "-c", server_code],
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                with urlopen(f"{base_url}/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "Wave B Playwright test server failed to start"

        pw_script = f"""
const fs = require('fs');
const {{ chromium }} = require('playwright');

(async () => {{
  const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const launchOpts = {{ headless: true }};
  if (fs.existsSync(chromePath)) launchOpts.executablePath = chromePath;
  const browser = await chromium.launch(launchOpts);
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();

  const consoleErrors = [];
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => {{
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  }});

  const loginRes = await page.request.post('{base_url}/api/auth/login', {{
    data: {{ username: 'tim', password: '{admin_password}' }},
  }});
  if (!loginRes.ok()) throw new Error('Login failed');

  await page.goto('{base_url}/#outreach', {{ waitUntil: 'domcontentloaded' }});
  await page.locator('#leadgen-health-strip').waitFor({{ state: 'visible', timeout: 15000 }});
  await page.locator('#open-leadgen-funnel-btn').waitFor({{ state: 'visible' }});

  // Check #lg-pane-budget updates ($10.00 default, tma_apps, tg_catalog_discovery, reverse bridge)
  await page.locator('#open-leadgen-settings-btn').click();
  await page.locator('#leadgen-settings-modal').waitFor({{ state: 'visible' }});
  const spentCapText = (await page.locator('#leadgen-budget-spent-value').textContent()).trim();
  if (!spentCapText.includes('$10.00')) {{
    throw new Error(`Expected $10.00 default cap in #leadgen-budget-spent-value, got ${{spentCapText}}`);
  }}
  await page.locator('[data-b2b-source="tma_apps"]').waitFor({{ state: 'attached' }});
  await page.locator('[data-b2b-source="tg_catalog_discovery"]').waitFor({{ state: 'attached' }});
  await page.locator('#leadgen-b2b-reverse-bridge-tg').fill('@softugc_team');
  await page.locator('#leadgen-save-btn').click();
  await page.waitForTimeout(300);
  await page.locator('#leadgen-settings-close-btn').click();

  // Open Funnel & Batch Scraper tab directly via #open-leadgen-funnel-btn
  await page.locator('#open-leadgen-funnel-btn').click();
  await page.locator('#lg-pane-funnel').waitFor({{ state: 'visible' }});
  const funnelSelected = await page.locator('#lg-tab-funnel').getAttribute('aria-selected');
  if (funnelSelected !== 'true') throw new Error('Expected #lg-tab-funnel aria-selected=true');

  // Wait for scraper health and source funnel table to populate
  await page.locator('#lg-scraper-health-grid').waitFor({{ state: 'visible' }});
  await page.waitForFunction(() => {{
    const txt = document.querySelector('#lg-scraper-health-grid')?.textContent || '';
    const tbl = document.querySelector('#lg-source-funnel-table')?.textContent || '';
    return txt.includes('signals_ro_1.session') && tbl.includes('tg_ad_spy');
  }}, null, {{ timeout: 10000 }});

  const gridText = await page.locator('#lg-scraper-health-grid').textContent();
  if (!gridText.includes('phone_checker_1.session')) {{
    throw new Error(`Missing phone_checker_1.session in scraper health grid: ${{gridText}}`);
  }}
  const errText = await page.locator('#lg-scraper-recent-errors').textContent();
  if (!errText.includes('facebook_groups')) {{
    throw new Error(`Missing recent error in #lg-scraper-recent-errors: ${{errText}}`);
  }}
  const tableText = await page.locator('#lg-source-funnel-table').textContent();
  if (!tableText.includes('100.0%') || !tableText.includes('buyer_public')) {{
    throw new Error(`Unexpected funnel table content: ${{tableText}}`);
  }}

  // Submit Batch Scrape form (Web $0 + auto_ingest)
  await page.locator('#lg-batch-targets').fill('@saas_founders');
  await page.locator('#lg-batch-mode').selectOption('web');
  await page.locator('#lg-batch-limit').fill('25');
  await page.locator('#lg-batch-auto-ingest').check();
  await page.locator('#lg-batch-start-btn').click();

  try {{
    await page.waitForFunction(() => {{
      const txt = document.querySelector('#lg-batch-scrape-status')?.textContent || '';
      return txt.includes('Завершено') && txt.includes('@alice_founder');
    }}, null, {{ timeout: 15000 }});
  }} catch (waitErr) {{
    const curStatus = await page.locator('#lg-batch-scrape-status').textContent();
    throw new Error(`${{waitErr.message}} | status=${{curStatus}} | pageErrors=${{JSON.stringify(pageErrors)}} | consoleErrors=${{JSON.stringify(consoleErrors)}}`);
  }}

  const batchStatusText = await page.locator('#lg-batch-scrape-status').textContent();
  if (batchStatusText.includes('helper_bot') || !batchStatusText.includes('Rule 5 запреты: 0')) {{
    throw new Error(`Unexpected batch scrape status output: ${{batchStatusText}}`);
  }}

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
  if (overflow) throw new Error('Horizontal overflow detected on desktop viewport');

  await page.locator('#leadgen-settings-body').evaluate((el) => {{ el.scrollTop = 0; }});
  await page.waitForTimeout(150);
  await page.screenshot({{ path: {str(screenshot_path)!r}, fullPage: false }});

  if (pageErrors.length || consoleErrors.length) {{
    throw new Error(`Browser errors: page=${{JSON.stringify(pageErrors)}} console=${{JSON.stringify(consoleErrors)}}`);
  }}
  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = _resolve_playwright_node_path()
        proc = subprocess.run(
            ["node", "-e", pw_script],
            cwd=str(root / "e2e"),
            env=node_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"Playwright failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        assert screenshot_path.is_file() and screenshot_path.stat().st_size > 1024
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_skeptic_batch_scrape_connect_auth_rule5_rollback_and_bot_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace
    import app.main as app_main

    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    state_dir = tmp_path / "leadgen_state"
    state_dir.mkdir(parents=True)
    sessions_dir = tmp_path / "scraper_sessions"
    sessions_dir.mkdir(parents=True)
    ro_sess = sessions_dir / "signals_ro_batch.session"
    ro_sess.write_bytes(b"SQLite format 3\x00" + b"0" * 32)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    monkeypatch.setenv("SCRAPER_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setenv("SCRAPER_TG_SESSION", str(ro_sess))
    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "fakehash123")

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    create_campaign(
        conn,
        name="Whales EN — TG",
        daily_limit=50,
        status="running",
        segment="whale",
        language="en",
        copy_profile="whale_mass",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        leadgen_enabled=True,
    )

    # 1) Unauthorized RO session uses connect() + is_user_authorized(), NEVER client.start()
    import telethon

    client_events: list[str] = []

    class FakeUnauthClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def start(self) -> None:
            client_events.append("start_called")
            raise AssertionError("client.start() MUST NOT be called in batch-scrape event loop")

        async def connect(self) -> None:
            client_events.append("connect")

        async def is_user_authorized(self) -> bool:
            client_events.append("is_user_authorized")
            return False

        async def disconnect(self) -> None:
            client_events.append("disconnect")

    monkeypatch.setattr(telethon, "TelegramClient", FakeUnauthClient)
    monkeypatch.setattr(
        "leadgen.tg_web_client.fetch_channel_posts_web",
        lambda ch, limit=20: ({"title": "Test", "about": ""}, []),
    )
    with pytest.raises(RuntimeError, match="не авторизована"):
        asyncio.run(app_main._collect_hybrid_batch_leads(conn, ["saas_founders"], 10))
    assert client_events == ["connect", "is_user_authorized", "disconnect"]

    # 2) Rule 5 violation rolls back newly created campaign_recipients
    from app import main as main_mod

    monkeypatch.setattr(
        main_mod,
        "preview_outreach_batch",
        lambda *_a, **_kw: {"ok": False, "sample_count": 1, "forbidden_pattern_count": 1},
    )
    ing_cnt, _stats, r5_summary = app_main._auto_ingest_batch_leads_with_rule5(
        conn,
        [
            {
                "name": "Alice Whale",
                "username": "alice_whale_ok",
                "company": "AliceApp",
                "website": "https://aliceapp.io",
                "their_angle": "Subscription AI video studio",
                "source": "batch_scrape",
                "segment": "whale",
                "icp_score": 88,
            }
        ],
    )
    assert ing_cnt == 0
    assert r5_summary["forbidden_pattern_count"] == 1
    assert r5_summary["rolled_back_count"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM campaign_recipients WHERE username = 'alice_whale_ok'").fetchone()[0] == 0

    # 3) _execute_batch_scrape_job filters *bot via telegram_username/tg_username aliases & fails custom_runner on Rule 5 > 0
    app = create_app(conn=conn)
    app.state._batch_scrape_runner = lambda _job: {
        "leads": [
            {"username": "", "telegram_username": "alias_bot", "name": "Alias Bot"},
            {"username": "human_founder", "tg_username": "human_founder", "name": "Human"},
        ],
        "leads_found": 99,  # stale count must NOT override post-filter len(leads)
        "rule5_preview": {"forbidden_pattern_count": 1, "ok": False},
    }
    job: dict[str, object] = {
        "job_id": "jtest1",
        "targets": ["saas_founders"],
        "mode": "web",
        "limit": 10,
        "auto_ingest": False,
        "leads": [],
        "leads_found": 0,
        "error": None,
    }
    asyncio.run(app_main._execute_batch_scrape_job(app, job))
    assert job["leads_found"] == 1
    assert [r["username"] for r in job["leads"]] == ["human_founder"]
    assert job["status"] == "failed"
    assert "Rule 5" in str(job["error"])
    conn.close()
