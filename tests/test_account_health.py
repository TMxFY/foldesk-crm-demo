from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway, _classify_session_error


def test_reconnect_writes_health_without_telegram(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111")
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        assert started.status_code == 200
        account_id = started.json()["id"]
        confirmed = client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "connected"
        row = client.get("/api/accounts").json()[0]
        assert row["status"] == "connected"
        assert row["health"] == "active"
        assert row["last_error"] is None

        gateway.health = "unauthorized"
        dead = client.post(f"/api/accounts/{account_id}/reconnect")
        assert dead.status_code == 200
        body = dead.json()
        assert body["status"] == "connected"
        assert body["health"] == "unauthorized"
        assert body["last_error"]
        saved = client.get("/api/accounts").json()[0]
        assert saved["health"] == "unauthorized"
        assert saved["last_error"] == body["last_error"]
        assert saved["status"] == "connected"

        gateway.health = "active"
        live = client.post(f"/api/accounts/{account_id}/reconnect")
        assert live.status_code == 200
        assert live.json()["health"] == "active"
        assert live.json()["last_error"] is None
        again = client.get("/api/accounts").json()[0]
        assert again["health"] == "active"
        assert again["last_error"] is None

        script = client.get("/static/app.js")
        assert script.status_code == 200
        assert "Переподключить" in script.text
        assert "активен" in script.text
        assert "не авторизован" in script.text


def test_session_error_class_does_not_need_network():
    from telethon import errors

    banned = _classify_session_error(errors.UserDeactivatedBanError(None))
    assert banned["health"] == "banned"
    frozen = _classify_session_error(RuntimeError("FROZEN_METHOD_INVALID"))
    assert frozen["health"] == "frozen"
    offline = _classify_session_error(ConnectionError("connect failed"))
    assert offline["health"] == "offline"
    dead = _classify_session_error(errors.SessionRevokedError(None))
    assert dead["health"] == "unauthorized"


def test_relogin_on_unauthorized_account_moves_corrupted_session_and_sends_code(tmp_path, monkeypatch):
    import sqlite3
    import app.accounts as acc_mod
    from app.accounts import restore_disk_sessions, start_login
    import asyncio

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )

    # Create a fake 8.session with a 256-byte auth_key that is marked unauthorized in DB
    corrupted_sess = sess_dir / "8.session"
    with sqlite3.connect(corrupted_sess) as sc:
        sc.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
        sc.execute("INSERT INTO sessions VALUES (2, ?)", (b"x" * 256,))
        sc.commit()
    # Pad file size > 4096 bytes so _inspect_telethon_session sees it
    with open(corrupted_sess, "ab") as f:
        f.write(b"\x00" * 4096)

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, last_error, created_at, session_blob)
        VALUES (8, '+998336999642', 'data/sessions/8.session', 'connected', 'unauthorized', 'InvokeWithLayerRequest', '2026-11-08T00:00:00', ?)
        """,
        (corrupted_sess.read_bytes(),),
    )
    conn.commit()

    # restore_disk_sessions must NOT reset health='unauthorized' back to 'active'
    restore_disk_sessions(conn)
    row_after_restore = conn.execute("SELECT status, health FROM accounts WHERE id = 8").fetchone()
    assert row_after_restore["health"] == "unauthorized"

    # Now start_login for +998336999642 must move corrupted 8.session to backup_8.session and request code cleanly
    gateway = FakeGateway(code="55555")
    res = asyncio.run(start_login(conn, "+998336999642", gateway))
    assert res["id"] == 8
    assert res["status"] == "pending_code"
    assert not corrupted_sess.exists()
    assert (sess_dir / "backup_8.session").exists()
    row_pending = conn.execute("SELECT status, health, last_error, session_blob FROM accounts WHERE id = 8").fetchone()
    assert row_pending["status"] == "pending_code"
    assert row_pending["health"] is None
    assert row_pending["last_error"] is None
    assert row_pending["session_blob"] is None


def test_fallback_nodemaven_stays_in_same_country_and_skips_retry_on_unauthorized(monkeypatch, tmp_path):
    import asyncio
    import telethon
    from telethon import errors
    from app.telegram_gateway import TelethonGateway

    gw = TelethonGateway()
    assert not hasattr(gw, "_fallback_nodemaven_urls")

    uz_url = "socks5://nmuser-country-uz-sid-foldeskacc8-ttl-86400-filter-medium:secret@gate.nodemaven.com:1080"
    sess_path = str(tmp_path / "8.session")
    gw.set_proxy(sess_path, uz_url)
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    attempts = []

    class UnauthorizedDummyClient:
        def __init__(self, session_name, api_id, api_hash, **kwargs):
            self.kwargs = kwargs

        async def connect(self):
            attempts.append(self.kwargs.get("proxy"))
            raise errors.AuthKeyUnregisteredError(request=None)

        async def disconnect(self):
            pass

    monkeypatch.setattr(telethon, "TelegramClient", UnauthorizedDummyClient)
    try:
        asyncio.run(gw._open(sess_path))
    except errors.AuthKeyUnregisteredError:
        pass
    # Must stop after 1 attempt instead of hammering fallback proxies when auth_key is unregistered
    assert len(attempts) == 1


def test_delete_account_moves_session_to_backup_so_restore_never_resurrects_it(tmp_path, monkeypatch):
    import json
    import shutil
    import sqlite3
    import app.accounts as acc_mod
    from app.accounts import restore_disk_sessions

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "real-token")
    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")

    def _write_valid_session(path: sqlite3. Connection | object):
        with sqlite3.connect(path) as sc:
            sc.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
            sc.execute("INSERT INTO sessions VALUES (2, ?)", (b"y" * 256,))
            sc.commit()
        with open(path, "ab") as f:
            f.write(b"\x00" * 4096)

    sess_file = sess_dir / "998336999642.session"
    _write_valid_session(sess_file)
    # Also create numeric stem files (< 7 digits) that must never be auto-registered
    _write_valid_session(sess_dir / "2.session")
    _write_valid_session(sess_dir / "3.session")

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, created_at)
        VALUES (8, '+998336999642', 'data/sessions/998336999642.session', 'connected', 'unauthorized', '2026-11-08T00:00:00')
        """
    )
    conn.commit()

    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        resp = client.delete("/api/accounts/8")
        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted"] is True
        assert body["id"] == 8
        assert body["session_path"] == "data/sessions/998336999642.session"

    assert conn.execute("SELECT id FROM accounts WHERE id = 8").fetchone() is None
    assert not sess_file.exists()
    backup_file = sess_dir / "backup_998336999642.session"
    assert backup_file.exists()

    kv_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'deleted_sessions_v1'"
    ).fetchone()
    assert kv_row is not None
    deleted_items = set(json.loads(kv_row[0]))
    assert "data/sessions/998336999642.session" in deleted_items
    assert "998336999642.session" in deleted_items
    assert "phone:998336999642" in deleted_items

    # Even if the .session file reappears on disk (e.g. from an older snapshot), restore_disk_sessions
    # must NOT resurrect the deleted session and must NOT auto-register 2.session / 3.session (< 7 digits)
    shutil.copy2(backup_file, sess_file)
    restore_disk_sessions(conn)
    rows = conn.execute("SELECT id, phone, session_path FROM accounts").fetchall()
    assert len(rows) == 0


def test_purge_dead_accounts_endpoint_preserves_only_connected_active(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.executemany(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, created_at)
        VALUES (?, ?, ?, ?, ?, '2026-11-08T00:00:00')
        """,
        [
            (1, "+905346380943", "data/sessions/1.session", "connected", "active"),
            (2, "+79990000002", "data/sessions/2.session", "pending_code", None),
            (3, "+79990000003", "data/sessions/3.session", "error", None),
            (4, "+998336999642", "data/sessions/4.session", "connected", "unauthorized"),
            (5, "+998917912439", "data/sessions/5.session", "connected", "banned"),
            (6, "+998908147349", "data/sessions/6.session", "connected", "frozen"),
        ],
    )
    conn.commit()

    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        resp = client.post("/api/accounts/purge-dead")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted_ids"] == [2, 3, 4, 5, 6]
        assert data["count"] == 5
        remaining = data["accounts"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == 1
        assert remaining[0]["phone"] == "+905346380943"
        assert remaining[0]["status"] == "connected"
        assert remaining[0]["health"] == "active"


def test_import_session_accounts_persists_credentials_and_device_profile_in_sqlite(tmp_path, monkeypatch):
    import app.accounts as acc_mod
    import app.telegram_gateway as gw_mod
    from app.accounts import import_session_accounts
    from app.telegram_gateway import TelethonGateway

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "crm.db"
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )
    monkeypatch.setattr(gw_mod, "DEFAULT_DB", db_file)

    conn = connect(db_file)
    init_db(conn)
    gw1 = TelethonGateway()

    imported = import_session_accounts(
        conn,
        gw1,
        {
            "sessions": [
                {
                    "filename": "79991234567.session",
                    "json_data": {
                        "phone": "+79991234567",
                        "id": 123456789,
                        "username": "clean_tester",
                        "first_name": "Clean",
                        "last_name": "Tester",
                        "app_id": 2040,
                        "app_hash": "b18441a1ff607e10a989891a5462e627",
                        "device": "ASUS Zenbook UX3402",
                        "sdk": "Windows 11",
                        "app_version": "4.16.8 x64",
                        "lang_pack": "ru",
                        "system_lang_pack": "ru-RU",
                    },
                }
            ]
        },
    )
    assert len(imported) == 1
    session_path = "data/sessions/79991234567.session"

    row = conn.execute(
        """
        SELECT api_id, api_hash, device_model, system_version, app_version, lang_code, system_lang_code
        FROM accounts
        WHERE session_path = ?
        """,
        (session_path,),
    ).fetchone()
    assert row is not None
    assert row["api_id"] == 2040
    assert row["api_hash"] == "b18441a1ff607e10a989891a5462e627"
    assert row["device_model"] == "ASUS Zenbook UX3402"
    assert row["system_version"] == "Windows 11"
    assert row["app_version"] == "4.16.8 x64"
    assert row["lang_code"] == "ru"
    assert row["system_lang_code"] == "ru-RU"

    # Simulate server restart with a fresh TelethonGateway instance (empty in-memory caches)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    fresh_gw = TelethonGateway()
    assert fresh_gw._credentials(session_path) == (2040, "b18441a1ff607e10a989891a5462e627")
    prof = fresh_gw._resolve_device_profile(session_path)
    assert prof == {
        "device_model": "ASUS Zenbook UX3402",
        "system_version": "Windows 11",
        "app_version": "4.16.8 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    }



