import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db, utc_now
from app.main import create_app
from app.telegram_gateway import FakeGateway, GatewayError, TelethonGateway


def _write_sqlite_session(path, dc_id: int = 2, key_byte: bytes = b"\xab"):
    with sqlite3.connect(path) as sconn:
        sconn.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, auth_key BLOB, takeout_id INTEGER)"
        )
        sconn.execute(
            "INSERT INTO sessions VALUES (?, '149.154.167.50', 443, ?, NULL)",
            (dc_id, key_byte * 256),
        )
        sconn.commit()
    with open(path, "ab") as f:
        f.write(b"\x00" * 4096)


def test_tg_web_init_js_never_exposes_telethon_auth_key_and_scrubs_browser_storage(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    sess_file = tmp_path / "27.session"
    secret_key = bytes(range(256))
    with sqlite3.connect(sess_file) as sconn:
        sconn.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, auth_key BLOB, takeout_id INTEGER)"
        )
        sconn.execute(
            "INSERT INTO sessions VALUES (2, '149.154.167.50', 443, ?, NULL)",
            (secret_key,),
        )
        sconn.commit()
    with open(sess_file, "ab") as f:
        f.write(b"\x00" * 4096)

    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+79088819595', ?, 'connected', 978069541, 'vladwilhelm', 'Vladislav Wilhelm', ?, 'active', ?, 20, 0, 'telegram')
        """,
        (str(sess_file), now, now),
    )
    acc_id = int(cur.lastrowid)
    conn.commit()

    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        accs = client.get("/api/accounts").json()
        assert len(accs) == 1
        # Server Telethon sessions must never be marked tg_web_ready for browser auth_key sharing
        assert accs[0]["tg_web_ready"] is False

        js_res = client.get(f"/a/foldesk-session-init.js?account_id={acc_id}")
        assert js_res.status_code in (200, 404)
        if js_res.status_code == 200:
            # Secret 256-byte auth_key and domino auto-switcher must NEVER appear in browser JS
            assert secret_key.hex() not in js_res.text
            assert "__foldeskDupGuardInstalled" not in js_res.text
            assert "window.location.replace" not in js_res.text
            # Cleanup logic must scrub any stale cloned keys from browser localStorage
            assert 'localStorage.removeItem("dc" + i + "_auth_key")' in js_res.text


def test_telethon_gateway_never_rotates_proxy_sid_and_uses_string_session(monkeypatch, tmp_path):
    import telethon
    from telethon import errors
    from telethon.sessions import StringSession

    gw = TelethonGateway()
    assert not hasattr(gw, "_fallback_nodemaven_urls")

    primary_url = "socks5://nmuser-country-kz-sid-foldeskacc27-ttl-24h-filter-medium:secret@gate.nodemaven.com:1080"
    sess_path = str(tmp_path / "27.session")
    _write_sqlite_session(sess_path, dc_id=2, key_byte=b"\xcd")

    gw.set_proxy(sess_path, primary_url)
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    seen_calls = []

    class TransientFailClient:
        def __init__(self, session_obj, api_id, api_hash, **kwargs):
            seen_calls.append((session_obj, kwargs.get("proxy")))

        async def connect(self):
            raise ConnectionError("SOCKS5 upstream peer timeout")

        async def disconnect(self):
            pass

    monkeypatch.setattr(telethon, "TelegramClient", TransientFailClient)
    with pytest.raises(ConnectionError):
        asyncio.run(gw._open(sess_path))

    # Retries at most once on the EXACT SAME proxy username/sid (never fb1/fb2), using StringSession
    assert len(seen_calls) == 2
    first_sess, first_proxy = seen_calls[0]
    second_sess, second_proxy = seen_calls[1]
    assert isinstance(first_sess, StringSession)
    assert isinstance(second_sess, StringSession)
    assert first_sess.auth_key.key == b"\xcd" * 256
    assert first_proxy == second_proxy
    assert "foldeskacc27fb" not in str(first_proxy)

    # On AuthKeyUnregisteredError / AuthKeyDuplicatedError, stops immediately after 1 attempt
    auth_calls = []

    class AuthDupClient:
        def __init__(self, session_obj, api_id, api_hash, **kwargs):
            auth_calls.append(kwargs.get("proxy"))

        async def connect(self):
            raise errors.AuthKeyDuplicatedError(request=None)

        async def disconnect(self):
            pass

    monkeypatch.setattr(telethon, "TelegramClient", AuthDupClient)
    with pytest.raises(errors.AuthKeyDuplicatedError):
        asyncio.run(gw._open(sess_path))
    assert len(auth_calls) == 1


def test_sqlite_session_migrates_to_string_session_and_survives_file_removal(tmp_path, monkeypatch):
    import app.accounts as acc_mod
    from app.accounts import restore_disk_sessions

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )

    sess_file = sess_dir / "30.session"
    _write_sqlite_session(sess_file, dc_id=2, key_byte=b"\xef")

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, created_at)
        VALUES (30, '+79951210659', 'data/sessions/30.session', 'connected', 'active', '2026-11-10T00:00:00')
        """
    )
    conn.commit()

    restore_disk_sessions(conn)
    row = conn.execute("SELECT session_string FROM accounts WHERE id = 30").fetchone()
    assert row is not None
    assert row["session_string"] and len(row["session_string"]) > 100

    # Even if the SQLite .session file is removed from disk, account remains session_exists=True via session_string
    sess_file.unlink()
    accs = acc_mod.list_accounts(conn)
    assert len(accs) == 1
    assert accs[0]["session_exists"] is True


def test_local_mac_guard_blocks_prod_session_autoconnect_when_not_on_render(tmp_path, monkeypatch):
    from app.main import should_enable_live_tg_runtime

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    conn.executemany(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?)",
        [
            ("r2_account_id", "acc123", now),
            ("r2_access_key", "key123", now),
            ("r2_secret_key", "sec123", now),
            ("r2_bucket", "foldesk", now),
        ],
    )
    conn.commit()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "real-prod-token")
    monkeypatch.delenv("DISABLE_TG_WATCHES", raising=False)
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_TG_SESSIONS", raising=False)

    # On local Mac (CRMTG_VPS_RUNTIME unset) with cloud R2 configured, live TG runtime must be blocked
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is False

    # On Render (RENDER=true), live TG runtime MUST be disabled to prevent VPS vs Render split-brain
    monkeypatch.setenv("RENDER", "true")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is False
    monkeypatch.setenv("ALLOW_LOCAL_TG_SESSIONS", "1")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is False

    # On canonical VPS (CRMTG_VPS_RUNTIME=1), live TG runtime is enabled
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_TG_SESSIONS", raising=False)
    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is True

    # Or on local Mac with explicit override ALLOW_LOCAL_TG_SESSIONS=1 (and RENDER unset)
    monkeypatch.delenv("CRMTG_VPS_RUNTIME", raising=False)
    monkeypatch.setenv("ALLOW_LOCAL_TG_SESSIONS", "1")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is True


def test_r2_lease_waits_for_old_container_release_before_acquiring(monkeypatch, tmp_path):
    import app.cloud_persistence as cp

    store: dict[str, bytes] = {}

    class FakeResp:
        def __init__(self, status_code: int, content: bytes = b""):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="replace")

    class FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, headers=None):
            key = url.split("/", 4)[-1]
            if key not in store:
                return FakeResp(404, b"")
            return FakeResp(200, store[key])

        def put(self, url, content=b"", headers=None):
            key = url.split("/", 4)[-1]
            store[key] = bytes(content)
            return FakeResp(200, b"")

    monkeypatch.setattr(cp.httpx, "Client", FakeHttpxClient)
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda conn=None: {
            "account_id": "acc",
            "access_key": "ak",
            "secret_key": "sk",
            "bucket": "bkt",
        },
    )

    # Simulate Old Container holding an active lease
    now_epoch = time.time()
    store[cp.R2_LEASE_KEY] = json.dumps(
        {
            "instance_id": "old-render-container-111",
            "heartbeat_epoch": now_epoch,
            "released": False,
        }
    ).encode("utf-8")

    # Schedule Old Container releasing the lease after 0.15s
    import threading

    def _release_soon():
        time.sleep(0.15)
        store[cp.R2_LEASE_KEY] = json.dumps(
            {
                "instance_id": "old-render-container-111",
                "heartbeat_epoch": time.time(),
                "released": True,
            }
        ).encode("utf-8")

    t = threading.Thread(target=_release_soon, daemon=True)
    t.start()

    t0 = time.monotonic()
    try:
        acquired = cp.wait_and_acquire_tg_runtime_lease(
            max_wait_seconds=2.0,
            poll_interval=0.05,
            lease_ttl_seconds=30.0,
            verify_delay_seconds=0.05,
            lock_path=tmp_path / "lease.lock",
        )
    finally:
        cp.release_tg_local_lock()
    elapsed = time.monotonic() - t0
    assert acquired is True
    assert elapsed >= 0.12
    saved_lease = json.loads(store[cp.R2_LEASE_KEY].decode("utf-8"))
    assert saved_lease["instance_id"] == cp.INSTANCE_ID
    assert saved_lease["released"] is False
    assert saved_lease.get("host")
    assert saved_lease.get("public_ip")
    assert isinstance(saved_lease.get("pid"), int)
    assert saved_lease.get("git_sha")


def test_tg_web_proxy_blocks_path_traversal_and_scrubs_account2_without_legacy_sig(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import app.tg_web_proxy as twp

    if hasattr(twp, "CACHE_DIR"):
        monkeypatch.setattr(twp, "CACHE_DIR", tmp_path / "tg_web_cache")
    secret_db = tmp_path / "data" / "crm.db"
    secret_db.parent.mkdir(parents=True, exist_ok=True)
    secret_db.write_bytes(b"SUPER_SECRET_SQLITE_BYTES")

    if hasattr(twp, "_fetch_upstream_asset"):
        def _forbid_upstream(*args, **kwargs):
            raise AssertionError("Upstream must never be called on path traversal")

        monkeypatch.setattr(twp.urllib.request, "urlopen", _forbid_upstream)
        assert twp._fetch_upstream_asset("../../data/crm.db") is None
        assert twp._fetch_upstream_asset("..%2f..%2fdata/crm.db") is None

    conn = connect(tmp_path / "app.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        trav = client.get("/a/..%2f..%2fdata/crm.db")
        assert trav.status_code == 404
        assert b"SUPER_SECRET_SQLITE_BYTES" not in trav.content

        js_res = client.get("/a/foldesk-session-init.js")
        assert js_res.status_code in (200, 404)
        if js_res.status_code == 200:
            js_code = js_res.text
            assert 'localStorage.getItem("foldesk_tg_scrub_v2")' in js_code
            assert 'localStorage.setItem("foldesk_tg_scrub_v2", "1")' in js_code
            assert 'localStorage.removeItem("dc" + i + "_server_salt")' in js_code
            assert 'localStorage.removeItem("auth_key_fingerprint")' in js_code

            node_bin = shutil.which("node")
            if node_bin:
                harness = (
                    "const store = { account2: '{\"dc2_auth_key\":\"deadbeef\"}', dc2_auth_key: 'deadbeef', dc2_server_salt: '1122', user_auth: '{\"id\":1}', keep_me: 'ok' };\n"
                    "global.localStorage = {\n"
                    "  getItem: (k) => Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null,\n"
                    "  setItem: (k, v) => { store[k] = String(v); },\n"
                    "  removeItem: (k) => { delete store[k]; },\n"
                    "};\n"
                    + js_code
                    + "\nconsole.log(JSON.stringify(store));\n"
                )
                proc = subprocess.run(
                    [node_bin, "-e", harness],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                after = json.loads(proc.stdout.strip())
                assert "account2" not in after
                assert "dc2_auth_key" not in after
                assert "dc2_server_salt" not in after
                assert "user_auth" not in after
                assert after.get("foldesk_tg_scrub_v2") == "1"
                assert after.get("keep_me") == "ok"


def test_lease_gate_allows_healthcheck_200_while_blocking_mtproto_with_503(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+79088819595', 'data/sessions/27.session', 'connected', 978069541, 'vladwilhelm', 'Vladislav', ?, 'active', ?, 20, 0, 'telegram')
        """,
        (now, now),
    )
    acc_id = int(cur.lastrowid)
    conn.commit()

    app = create_app(conn=conn, gateway=None)
    app.state.tg_lease_ready = False
    with TestClient(app) as client:
        # Render rolling-deploy healthcheck (/api/accounts) must return 200 immediately
        hc = client.get("/api/accounts")
        assert hc.status_code == 200
        assert len(hc.json()) == 1

        # Meanwhile any MTProto endpoint must return 503 while waiting for old container lease release
        reconn = client.post(f"/api/accounts/{acc_id}/reconnect")
        assert reconn.status_code == 503


def test_r2_lease_transient_503_does_not_treat_lease_as_free_and_detects_theft(monkeypatch, tmp_path):
    import app.cloud_persistence as cp

    calls = {"get": 0}
    store: dict[str, bytes] = {
        cp.R2_LEASE_KEY: json.dumps(
            {
                "instance_id": "other-container-999",
                "heartbeat_epoch": time.time(),
                "released": False,
            }
        ).encode("utf-8")
    }

    class FakeResp:
        def __init__(self, status_code: int, content: bytes = b""):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="replace")

    class FlakyHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, headers=None):
            calls["get"] += 1
            if calls["get"] == 1:
                return FakeResp(503, b"Service Unavailable")
            if calls["get"] == 2:
                return FakeResp(200, store[cp.R2_LEASE_KEY])
            if calls["get"] == 3:
                # On 3rd poll, old container releases lease
                return FakeResp(
                    200,
                    json.dumps(
                        {
                            "instance_id": "other-container-999",
                            "heartbeat_epoch": time.time(),
                            "released": True,
                        }
                    ).encode("utf-8"),
                )
            # Subsequent read-after-write verification reads current stored lease
            return FakeResp(200, store[cp.R2_LEASE_KEY])

        def put(self, url, content=b"", headers=None):
            store[cp.R2_LEASE_KEY] = bytes(content)
            return FakeResp(200, b"")

    monkeypatch.setattr(cp.httpx, "Client", FlakyHttpxClient)
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda conn=None: {
            "account_id": "acc",
            "access_key": "ak",
            "secret_key": "sk",
            "bucket": "bkt",
        },
    )

    try:
        acquired = cp.wait_and_acquire_tg_runtime_lease(
            max_wait_seconds=2.0,
            poll_interval=0.02,
            lease_ttl_seconds=30.0,
            verify_delay_seconds=0.02,
            lock_path=tmp_path / "flaky.lock",
        )
    finally:
        cp.release_tg_local_lock()
    assert acquired is True
    # Must NOT have acquired on poll #1 (503) or poll #2 (unreleased other owner);
    # poll #3 sees release, poll #4 confirms read-after-write ownership.
    assert calls["get"] >= 4

    # Verify check_tg_runtime_lease_owned returns True when we own it, False when stolen
    class SimpleClient(FlakyHttpxClient):
        def get(self, url, headers=None):
            return FakeResp(200, store[cp.R2_LEASE_KEY])

    monkeypatch.setattr(cp.httpx, "Client", SimpleClient)
    assert cp.check_tg_runtime_lease_owned() is True

    store[cp.R2_LEASE_KEY] = json.dumps(
        {
            "instance_id": "new-container-777",
            "heartbeat_epoch": time.time(),
            "released": False,
        }
    ).encode("utf-8")
    assert cp.check_tg_runtime_lease_owned() is False


def test_cached_gateway_blocked_when_tg_lease_not_ready_and_cleared_on_stop_watches(tmp_path):
    from app.main import _resolve_app_gateway, stop_inbox_watches

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+79088819595', 'data/sessions/27.session', 'connected', 978069541, 'vladwilhelm', 'Vladislav', ?, 'active', ?, 20, 0, 'telegram')
        """,
        (now, now),
    )
    acc_id = int(cur.lastrowid)
    conn.commit()

    cached_gw = FakeGateway()
    app = create_app(conn=conn, gateway=cached_gw)
    with TestClient(app) as client:
        assert app.state.gateway is cached_gw
        app.state.tg_lease_ready = False

        # Even with a cached gateway in app.state.gateway, lease gate MUST block MTProto calls!
        assert _resolve_app_gateway(app, "telegram") is None
        reconn = client.post(f"/api/accounts/{acc_id}/reconnect")
        assert reconn.status_code == 503

        # stop_inbox_watches must clear app.state.gateway to None
        app.state.tg_lease_ready = True
        asyncio.run(stop_inbox_watches(app))
        assert app.state.gateway is None


def test_heartbeat_lease_loss_closes_sessions_before_reacquire_zero_split_brain(tmp_path, monkeypatch):
    import app.cloud_persistence as cp
    import app.main as main_mod

    conn = connect(tmp_path / "hb.db")
    init_db(conn)
    timeline: list[tuple[str, bool, bool]] = []

    class TrackingGateway(FakeGateway):
        async def close_all(self):
            timeline.append(
                ("gw_close_all", bool(app.state.tg_lease_ready), app.state.gateway is None)
            )

    monkeypatch.setenv("CRMTG_VPS_RUNTIME", "1")
    monkeypatch.setattr(cp, "check_tg_runtime_lease_owned", lambda conn=None: False)

    def _fake_release_local():
        timeline.append(
            ("release_local_lock", bool(app.state.tg_lease_ready), app.state.gateway is None)
        )

    def _fake_reacquire(conn=None, **kwargs):
        timeline.append(
            ("reacquire_attempt", bool(app.state.tg_lease_ready), app.state.gateway is None)
        )
        return False

    monkeypatch.setattr(cp, "release_tg_local_lock", _fake_release_local)
    monkeypatch.setattr(cp, "wait_and_acquire_tg_runtime_lease", _fake_reacquire)

    orig_stop_ap = main_mod.stop_autopilot_loop
    orig_stop_watches = main_mod.stop_inbox_watches

    async def _tracked_stop_ap(a):
        timeline.append(("stop_autopilot", bool(a.state.tg_lease_ready), a.state.gateway is None))
        await orig_stop_ap(a)

    async def _tracked_stop_watches(a):
        timeline.append(("stop_watches", bool(a.state.tg_lease_ready), a.state.gateway is None))
        await orig_stop_watches(a)

    monkeypatch.setattr(main_mod, "stop_autopilot_loop", _tracked_stop_ap)
    monkeypatch.setattr(main_mod, "stop_inbox_watches", _tracked_stop_watches)

    real_sleep = asyncio.sleep
    sleep_calls = {"n": 0}

    async def _fast_sleep(_sec):
        sleep_calls["n"] += 1
        if sleep_calls["n"] == 1:
            await real_sleep(0)
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr(main_mod.asyncio, "sleep", _fast_sleep)

    app = create_app(conn=conn, gateway=TrackingGateway())
    app.state.tg_lease_ready = True

    async def _run_hb():
        try:
            await main_mod._tg_lease_heartbeat_loop(app)
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_hb())

    names = [step[0] for step in timeline]
    assert "stop_autopilot" in names
    assert "stop_watches" in names
    assert "gw_close_all" in names
    assert "release_local_lock" in names
    assert "reacquire_attempt" in names
    # Every teardown step MUST happen strictly BEFORE any reacquire attempt (0s split-brain)
    reacquire_idx = names.index("reacquire_attempt")
    assert names.index("stop_autopilot") < reacquire_idx
    assert names.index("stop_watches") < reacquire_idx
    assert names.index("gw_close_all") < reacquire_idx
    assert names.index("release_local_lock") < reacquire_idx
    # At reacquire time, tg_lease_ready is False and app.state.gateway is None
    assert timeline[reacquire_idx][1] is False
    assert timeline[reacquire_idx][2] is True


def test_local_os_flock_and_read_after_write_verify_prevent_dual_owner(monkeypatch, tmp_path):
    import fcntl
    import app.cloud_persistence as cp

    lock_file = tmp_path / "crmtg-tg.lock"
    cp.release_tg_local_lock()

    # Contender holds exclusive OS flock on lock_file
    contender_fh = open(lock_file, "a+", encoding="utf-8")
    try:
        fcntl.flock(contender_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cp.acquire_tg_local_lock(lock_path=lock_file) is False
        assert cp.is_tg_local_lock_held() is False
    finally:
        fcntl.flock(contender_fh.fileno(), fcntl.LOCK_UN)
        contender_fh.close()

    # Once contender releases, acquisition succeeds and blocks a second fd
    assert cp.acquire_tg_local_lock(lock_path=lock_file) is True
    assert cp.is_tg_local_lock_held() is True
    probe_fh = open(lock_file, "a+", encoding="utf-8")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        probe_fh.close()
        cp.release_tg_local_lock()
    assert cp.is_tg_local_lock_held() is False

    # Read-after-write race: another host overwrites R2 lease during verify_delay_seconds
    store: dict[str, bytes] = {}

    class FakeResp:
        def __init__(self, status_code: int, content: bytes = b""):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="replace")

    class RacingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, headers=None):
            if cp.R2_LEASE_KEY not in store:
                return FakeResp(404, b"")
            return FakeResp(200, store[cp.R2_LEASE_KEY])

        def put(self, url, content=b"", headers=None):
            # Immediately overwritten by rival host before read-after-write verify completes
            store[cp.R2_LEASE_KEY] = json.dumps(
                {
                    "instance_id": "rival-vps-999",
                    "heartbeat_epoch": time.time(),
                    "released": False,
                }
            ).encode("utf-8")
            return FakeResp(200, b"")

    monkeypatch.setattr(cp.httpx, "Client", RacingClient)
    monkeypatch.setattr(
        cp,
        "_resolve_r2_config",
        lambda conn=None: {"account_id": "a", "access_key": "b", "secret_key": "c", "bucket": "d"},
    )
    acquired = cp.wait_and_acquire_tg_runtime_lease(
        max_wait_seconds=0.15,
        poll_interval=0.03,
        verify_delay_seconds=0.03,
        lock_path=lock_file,
    )
    assert acquired is False
    # Failed acquisition MUST release local OS lock too
    assert cp.is_tg_local_lock_held() is False


def test_safe_disconnect_cancels_sender_connection_and_borrowed_tasks_without_leaks():
    async def _run():
        gw = TelethonGateway()
        gw._disconnect_timeout = 0.05

        async def _forever():
            await asyncio.sleep(3600)

        class FakeWriter:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeConn:
            def __init__(self):
                self._send_task = asyncio.create_task(_forever())
                self._recv_task = asyncio.create_task(_forever())
                self._writer = FakeWriter()

        class FakeSender:
            def __init__(self):
                self._send_loop_handle = asyncio.create_task(_forever())
                self._recv_loop_handle = asyncio.create_task(_forever())
                self._reconnect_task = asyncio.create_task(_forever())
                self._ack_handle = asyncio.create_task(_forever())
                self._connection = FakeConn()

        class FakeSession:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class HangingTelethonClient:
            def __init__(self):
                self._sender = FakeSender()
                self._borrowed = FakeSender()
                self._borrowed_senders = {4: (object(), self._borrowed)}
                self._updates_handle = asyncio.create_task(_forever())
                self.session = FakeSession()

            def is_connected(self):
                return True

            async def disconnect(self):
                await asyncio.sleep(3600)

        client = HangingTelethonClient()
        all_tasks = [
            client._updates_handle,
            client._sender._send_loop_handle,
            client._sender._recv_loop_handle,
            client._sender._reconnect_task,
            client._sender._ack_handle,
            client._sender._connection._send_task,
            client._sender._connection._recv_task,
            client._borrowed._send_loop_handle,
            client._borrowed._recv_loop_handle,
            client._borrowed._reconnect_task,
            client._borrowed._ack_handle,
            client._borrowed._connection._send_task,
            client._borrowed._connection._recv_task,
        ]
        await gw._safe_disconnect(client)

        for t in all_tasks:
            assert t.done() and t.cancelled()
        assert client._sender._connection._writer.closed is True
        assert client._borrowed._connection._writer.closed is True
        assert client._borrowed_senders == {}
        assert client.session.closed is True
        tel = gw.get_telemetry()
        assert tel["leaked_tasks_1h"] == len(all_tasks)

    asyncio.run(_run())


def test_apply_proxy_and_set_proxy_never_spawn_bg_disconnect_tasks():
    async def _run():
        gw = TelethonGateway()
        assert not hasattr(gw, "_schedule_bg_disconnect")
        assert not hasattr(gw, "_bg_disconnect_tasks")

        disconnected: list[str] = []

        class StubClient:
            def __init__(self, tag: str):
                self.tag = tag
                self.connected = True

            def is_connected(self):
                return self.connected

            async def disconnect(self):
                self.connected = False
                disconnected.append(self.tag)

        sess = "data/sessions/27.session"
        gw._proxies[sess] = "socks5://u1:p1@1.2.3.4:1080"
        gw._clients[sess] = StubClient("c1")

        # apply_proxy awaits clean disconnect synchronously under per-session lock
        await gw.apply_proxy(sess, "socks5://u2:p2@5.6.7.8:1080")
        assert disconnected == ["c1"]
        assert sess not in gw._clients
        assert gw.get_telemetry()["disconnects_1h_by_account"].get("27") == 1

        # Sync set_proxy marks proxy changed without fire-and-forget disconnect; next _open tears down old client
        gw._clients[sess] = StubClient("c2")
        gw.set_proxy(sess, "socks5://u3:p3@9.9.9.9:1080")
        assert disconnected == ["c1"]
        assert sess in gw._proxy_changed

    asyncio.run(_run())


def test_resolve_proxy_fails_closed_on_db_error_and_never_mutates_sid_or_falls_back_direct(
    monkeypatch, tmp_path
):
    gw = TelethonGateway()
    sess = "data/sessions/381629258845.session"

    # 1. DB error -> MUST raise GatewayError (fail closed, never swallow!)
    class LockedConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(GatewayError):
        gw._resolve_proxy(sess, conn=LockedConn())

    # 2. NodeMaven configured, missing DB row -> MUST raise GatewayError (never use session_path as sid!)
    conn = connect(tmp_path / "proxy_guard.db")
    init_db(conn)
    monkeypatch.setenv("NODEMAVEN_USER", "nmuser")
    monkeypatch.setenv("NODEMAVEN_PASS", "nmpass")
    with pytest.raises(GatewayError):
        gw._resolve_proxy(sess, conn=conn)

    # 3. Account has proxy_url='auto' (or preset) when NodeMaven is NOT configured -> MUST raise GatewayError (never direct!)
    monkeypatch.delenv("NODEMAVEN_USER", raising=False)
    monkeypatch.delenv("NODEMAVEN_PASS", raising=False)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, proxy_url, created_at)
        VALUES (34, '+381629258845', ?, 'connected', 'active', 'auto', ?)
        """,
        (sess, now),
    )
    conn.commit()
    with pytest.raises(GatewayError):
        gw._resolve_proxy(sess, conn=conn)

    # 4. Explicit 'direct' returns None cleanly
    conn.execute("UPDATE accounts SET proxy_url = 'direct' WHERE id = 34")
    conn.commit()
    assert gw._resolve_proxy(sess, conn=conn) is None


def test_canonical_session_string_ssot_repairs_mismatched_disk_session_and_blob(tmp_path, monkeypatch):
    import app.accounts as acc_mod
    from app.telegram_gateway import (
        extract_auth_key_sha256_from_sqlite,
        extract_auth_key_sha256_from_string,
        extract_string_session_from_sqlite,
    )

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )

    # Build canonical session_string with key 0x11*256
    canon_tmp = tmp_path / "canon_src.session"
    _write_sqlite_session(canon_tmp, dc_id=2, key_byte=b"\x11")
    canon_str = extract_string_session_from_sqlite(canon_tmp)
    assert canon_str is not None
    canon_sha = extract_auth_key_sha256_from_string(canon_str)

    # Write mismatched stale key 0x99*256 on disk & in session_blob (exact prod #33 failure mode)
    disk_file = sess_dir / "33.session"
    _write_sqlite_session(disk_file, dc_id=2, key_byte=b"\x99")
    stale_bytes = disk_file.read_bytes()
    assert extract_auth_key_sha256_from_sqlite(disk_file) != canon_sha

    conn = connect(tmp_path / "ssot.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, session_string, session_blob, created_at)
        VALUES (33, '+79990001122', 'data/sessions/33.session', 'connected', 'active', ?, ?, '2026-11-10T00:00:00')
        """,
        (canon_str, stale_bytes),
    )
    conn.commit()

    res = acc_mod.reconcile_disk_sessions_with_db(conn)
    assert res["repaired"] == 1
    assert 33 in res["repaired_ids"]
    assert extract_auth_key_sha256_from_sqlite(disk_file) == canon_sha

    # Verify DB session_blob also matches canonical auth_key
    blob_row = conn.execute("SELECT session_blob FROM accounts WHERE id = 33").fetchone()
    verify_blob_file = tmp_path / "blob_check.session"
    verify_blob_file.write_bytes(bytes(blob_row["session_blob"]))
    assert extract_auth_key_sha256_from_sqlite(verify_blob_file) == canon_sha


def test_phone_prefix_rs_and_proxy_mode_classification_and_api_exposure(monkeypatch, tmp_path):
    from app.telegram_gateway import classify_proxy_mode, infer_proxy_country

    assert infer_proxy_country("+381629258845") == "rs"
    assert infer_proxy_country("+37120000001") == "lv"
    assert infer_proxy_country("+994501112233") == "az"
    assert infer_proxy_country("+972501112233") == "il"
    assert infer_proxy_country("+380501112233") == "ua"
    assert infer_proxy_country("+79088819595") == "kz"

    assert classify_proxy_mode(None, nodemaven_configured=True) == "rotating_nodemaven"
    assert classify_proxy_mode("auto", nodemaven_configured=True) == "rotating_nodemaven"
    assert (
        classify_proxy_mode(
            "socks5://nm-country-rs-sid-foldeskacc34-ttl-24h:pw@gate.nodemaven.com:1080",
            nodemaven_configured=False,
        )
        == "rotating_nodemaven"
    )
    assert (
        classify_proxy_mode(
            "socks5://ispuser:isppass@45.67.89.10:1080",
            nodemaven_configured=True,
        )
        == "static_isp"
    )
    assert classify_proxy_mode("direct", nodemaven_configured=True) == "direct"
    assert classify_proxy_mode(None, nodemaven_configured=False) == "direct"

    conn = connect(tmp_path / "pm.db")
    init_db(conn)
    now = utc_now()
    monkeypatch.setenv("NODEMAVEN_USER", "nmuser")
    monkeypatch.setenv("NODEMAVEN_PASS", "nmpass")
    conn.executemany(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, proxy_url, created_at)
        VALUES (?, ?, ?, 'connected', 'active', ?, ?)
        """,
        [
            (1, "+381629258845", "data/sessions/1.session", None, now),
            (2, "+79088819595", "data/sessions/2.session", "socks5://u:p@185.10.20.30:1080", now),
            (3, "+14155550101", "data/sessions/3.session", "direct", now),
        ],
    )
    conn.commit()
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        items = {a["id"]: a for a in client.get("/api/accounts").json()}
        assert items[1]["proxy_mode"] == "rotating_nodemaven"
        assert items[2]["proxy_mode"] == "static_isp"
        assert items[3]["proxy_mode"] == "direct"


def test_deterministic_device_profiles_unique_per_account_and_preserve_custom(tmp_path):
    from app.accounts import backfill_account_device_profiles

    conn = connect(tmp_path / "devprof.db")
    init_db(conn)
    now = utc_now()
    for i in range(1, 9):
        custom_dev = "Custom Work Laptop" if i == 5 else None
        conn.execute(
            """
            INSERT INTO accounts (id, phone, session_path, status, health, device_model, created_at)
            VALUES (?, ?, ?, 'connected', 'active', ?, ?)
            """,
            (i, f"+7999000000{i}", f"data/sessions/{i}.session", custom_dev, now),
        )
    conn.commit()

    updated = backfill_account_device_profiles(conn)
    assert updated == 8
    rows = conn.execute(
        "SELECT id, device_model, system_version, app_version, lang_code, system_lang_code FROM accounts ORDER BY id"
    ).fetchall()
    models = [r["device_model"] for r in rows]
    assert all(models)
    assert rows[4]["device_model"] == "Custom Work Laptop"
    non_custom_models = [r["device_model"] for r in rows if r["id"] != 5]
    assert len(set(non_custom_models)) == len(non_custom_models)
    # Idempotent on second call
    assert backfill_account_device_profiles(conn) == 0


def test_account_pool_summary_and_deep_health_exclude_frozen_and_cooldown_and_report_telemetry(
    tmp_path, monkeypatch
):
    from app.accounts import compute_account_pool_summary

    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")
    conn = connect(tmp_path / "pool.db")
    init_db(conn)
    now = utc_now()
    # 7 connected accounts x 20/day, with #32 frozen (exact prod layout)
    for aid in (27, 28, 29, 30, 31, 32, 34):
        h = "frozen" if aid == 32 else "active"
        conn.execute(
            """
            INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, created_at)
            VALUES (?, ?, ?, 'connected', ?, 20, 0, ?)
            """,
            (aid, f"+799911100{aid}", f"data/sessions/{aid}.session", h, now),
        )
    conn.commit()

    pool1 = compute_account_pool_summary(conn)
    assert pool1["connected_accounts"] == 7
    assert pool1["active_accounts"] == 6
    assert pool1["daily_pool"] == 120
    assert pool1["frozen_accounts"] == 1

    # Put account #28 in active future cooldown
    future_ts = time.time() + 3600.0
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('account_cooldown_until:28', ?, ?)",
        (str(future_ts), now),
    )
    conn.commit()

    pool2 = compute_account_pool_summary(conn)
    assert pool2["active_accounts"] == 5
    assert pool2["daily_pool"] == 100
    assert pool2["cooldown_accounts"] == 1

    gw = TelethonGateway()
    gw.record_disconnect(27, reason="watch_eof")
    gw.record_disconnect(27, reason="ConnectionError")
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        res = client.get("/api/health?deep=1")
        assert res.status_code == 200
        body = res.json()
        assert body["send_eligible_accounts"] == 5
        assert body["healthy_accounts"] == 5
        assert body["daily_pool"] == 100
        assert body["account_pool"]["frozen_accounts"] == 1
        assert body["account_pool"]["cooldown_accounts"] == 1
        assert body["disconnects_1h_by_account"].get("27") == 2
        assert body["disconnects_1h_total"] == 2
        assert body["leaked_tasks_1h"] == 0
        assert isinstance(body["active_watches"], int)
        assert body["tg_lease_owner"] is not None


def test_systemd_service_graceful_shutdown_timeout_window():
    from pathlib import Path

    svc = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "crmtg.service"
    text = svc.read_text(encoding="utf-8")
    assert "--timeout-graceful-shutdown 25" in text
    assert "TimeoutStopSec=35" in text
