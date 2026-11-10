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
        assert js_res.status_code == 200
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
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("ALLOW_LOCAL_TG_SESSIONS", raising=False)

    # On local Mac (RENDER unset) with cloud R2 configured, live TG runtime must be blocked
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is False

    # On Render (RENDER=true), live TG runtime is enabled
    monkeypatch.setenv("RENDER", "true")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is True

    # Or on local Mac with explicit override ALLOW_LOCAL_TG_SESSIONS=1
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("ALLOW_LOCAL_TG_SESSIONS", "1")
    assert should_enable_live_tg_runtime(conn=conn, gateway=None) is True


def test_r2_lease_waits_for_old_container_release_before_acquiring(monkeypatch):
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
    acquired = cp.wait_and_acquire_tg_runtime_lease(
        max_wait_seconds=2.0,
        poll_interval=0.05,
        lease_ttl_seconds=30.0,
    )
    elapsed = time.monotonic() - t0
    assert acquired is True
    assert elapsed >= 0.12
    saved_lease = json.loads(store[cp.R2_LEASE_KEY].decode("utf-8"))
    assert saved_lease["instance_id"] == cp.INSTANCE_ID
    assert saved_lease["released"] is False


def test_tg_web_proxy_blocks_path_traversal_and_scrubs_account2_without_legacy_sig(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import app.tg_web_proxy as twp

    monkeypatch.setattr(twp, "CACHE_DIR", tmp_path / "tg_web_cache")
    secret_db = tmp_path / "data" / "crm.db"
    secret_db.parent.mkdir(parents=True, exist_ok=True)
    secret_db.write_bytes(b"SUPER_SECRET_SQLITE_BYTES")

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
        assert js_res.status_code == 200
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


def test_r2_lease_transient_503_does_not_treat_lease_as_free_and_detects_theft(monkeypatch):
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

    acquired = cp.wait_and_acquire_tg_runtime_lease(
        max_wait_seconds=2.0,
        poll_interval=0.02,
        lease_ttl_seconds=30.0,
    )
    assert acquired is True
    # Must NOT have acquired on poll #1 (503) or poll #2 (unreleased other owner)
    assert calls["get"] >= 3

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
