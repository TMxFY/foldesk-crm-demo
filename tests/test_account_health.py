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
