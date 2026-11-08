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
    uz_url = "socks5://nmuser-country-uz-sid-foldeskacc8-ttl-86400-filter-medium:secret@gate.nodemaven.com:1080"
    fallbacks = gw._fallback_nodemaven_urls("data/sessions/8.session", uz_url)
    assert len(fallbacks) == 2
    assert all("-country-uz-" in u for u in fallbacks)
    assert all("-country-nl-" not in u and "-country-de-" not in u for u in fallbacks)

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
    import sqlite3
    import app.accounts as acc_mod
    from app.accounts import delete_account, restore_disk_sessions

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        acc_mod,
        "resolve_session",
        lambda p: sess_dir if str(p).rstrip("/").endswith("data/sessions") else sess_dir / str(p).split("/")[-1],
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "real-token")

    sess_file = sess_dir / "8.session"
    with sqlite3.connect(sess_file) as sc:
        sc.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
        sc.execute("INSERT INTO sessions VALUES (2, ?)", (b"y" * 256,))
        sc.commit()
    with open(sess_file, "ab") as f:
        f.write(b"\x00" * 4096)

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, created_at)
        VALUES (8, '+998336999642', 'data/sessions/8.session', 'connected', 'unauthorized', '2026-11-08T00:00:00')
        """
    )
    conn.commit()

    delete_account(conn, 8)
    assert not sess_file.exists()
    assert (sess_dir / "backup_8.session").exists()

    restore_disk_sessions(conn)
    rows = conn.execute("SELECT id FROM accounts").fetchall()
    assert len(rows) == 0


