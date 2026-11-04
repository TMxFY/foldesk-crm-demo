import json

import pytest
from fastapi.testclient import TestClient

from app.bot_login import NO_TOKEN, apply_start_update, operator_status, poll_bot_login
from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _update(chat_id, text="/start"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": text,
            "chat": {"id": chat_id, "type": "private"},
        },
    }


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "crm.db")
    init_db(db)
    return db


@pytest.fixture
def client(conn):
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as test_client:
        yield test_client


def test_phone_code_path_still_starts(client):
    started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
    assert started.status_code == 200
    assert started.json()["status"] == "pending_code"


def test_page_has_bot_login_button(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="bot-login"' in page.text
    assert "Войти через бота" in page.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "Вы вошли через бота" in script.text
    assert "/api/operator/bot-login" in script.text


def test_start_from_operator_opens_and_sticks(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    assert operator_status(conn)["logged_in"] is False
    foreign = apply_start_update(conn, _update(99999))
    assert foreign["logged_in"] is False
    assert foreign["opened"] is False
    opened = apply_start_update(conn, _update(10001, "/start@crm_bot payload"))
    assert opened["logged_in"] is True
    assert opened["opened"] is True
    assert opened["chat_id"] == "10001"
    again = apply_start_update(conn, _update(99999))
    assert again["logged_in"] is True
    assert again["opened"] is False
    assert operator_status(conn)["logged_in"] is True


def test_empty_token_does_not_open(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "   ")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    with pytest.raises(ValueError, match=NO_TOKEN):
        apply_start_update(conn, _update(10001))
    assert operator_status(conn)["logged_in"] is False
    called = {"n": 0}

    def opener(req, timeout=15):
        called["n"] += 1
        raise AssertionError("network")

    with pytest.raises(ValueError, match=NO_TOKEN):
        poll_bot_login(conn, opener)
    assert called["n"] == 0
    assert operator_status(conn)["logged_in"] is False


def test_api_fake_update_and_repeat_visit(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as first:
        assert first.get("/api/operator").json()["logged_in"] is False
        stranger = first.post("/api/operator/update", json=_update("10002"))
        assert stranger.status_code == 200
        assert stranger.json()["logged_in"] is False
        assert "вошли" not in stranger.text
        opened = first.post("/api/operator/update", json=_update(10001))
        assert opened.status_code == 200
        assert opened.json()["logged_in"] is True
    with TestClient(app) as second:
        again = second.get("/api/operator")
        assert again.status_code == 200
        assert again.json()["logged_in"] is True
        assert again.json()["chat_id"] == "10001"


def test_api_empty_token_is_not_a_login(client, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    denied = client.post("/api/operator/update", json=_update(10001))
    assert denied.status_code == 400
    assert denied.json()["detail"] == NO_TOKEN
    assert "вошли" not in denied.text
    button = client.post("/api/operator/bot-login")
    assert button.status_code == 400
    assert button.json()["detail"] == NO_TOKEN
    assert client.get("/api/operator").json()["logged_in"] is False


def test_poll_uses_fake_get_updates_without_network(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    seen = {}

    def opener(req, timeout=15):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        body = json.dumps(
            {"ok": True, "result": [_update(99999), _update(10001)]}
        ).encode("utf-8")
        return _Resp(200, body)

    result = poll_bot_login(conn, opener)
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/getUpdates")
    assert "test-bot-token" in seen["url"]
    assert result["logged_in"] is True
    assert result["opened"] is True
    assert operator_status(conn)["chat_id"] == "10001"

    def empty(req, timeout=15):
        return _Resp(200, json.dumps({"ok": True, "result": []}).encode("utf-8"))

    repeat = poll_bot_login(conn, empty)
    assert repeat["logged_in"] is True
    assert repeat["opened"] is False

    def rejected(req, timeout=15):
        return _Resp(200, json.dumps({"ok": False}).encode("utf-8"))

    other = connect(tmp_path / "other.db")
    init_db(other)
    with pytest.raises(ValueError, match="Telegram не принял запрос"):
        poll_bot_login(other, rejected)
    assert operator_status(other)["logged_in"] is False
