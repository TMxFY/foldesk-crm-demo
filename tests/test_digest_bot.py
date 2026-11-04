import io
import json
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.digest import EMPTY_TEXT, send_morning_digest
from app.leads import create_lead
from app.main import create_app
from app.reminders import complete_reminder, create_reminder
from app.telegram_gateway import FakeGateway
import urllib.error
import urllib.request


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


def _lead(conn):
    return create_lead(
        conn,
        name="Иван Петров",
        username="ivan_lead",
        phone="+79990001122",
        telegram_user_id=424242,
    )


def _install(monkeypatch, status=200, body=None, raise_http=False):
    calls = []
    raw = body if body is not None else b'{"ok": true, "result": {"message_id": 7}}'

    def opener(req, timeout=None):
        calls.append(req)
        if raise_http:
            raise urllib.error.HTTPError(
                req.full_url,
                status,
                "no",
                Message(),
                io.BytesIO(raw),
            )
        return _Resp(status, raw)

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    return calls


def _body(req):
    return json.loads(req.data.decode("utf-8"))


def test_send_today_to_operator_not_lead(client, conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "operator-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    calls = _install(monkeypatch)
    lead = _lead(conn)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%dT%H:%M:%S")
    other = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    open_row = create_reminder(conn, lead["id"], today, "созвон")
    create_reminder(conn, lead["id"], other, "потом")
    create_reminder(conn, lead["id"], yesterday, "вчера")
    closed = create_reminder(conn, lead["id"], today, "закрыто")
    complete_reminder(conn, closed["id"])

    res = client.post("/api/digest/send")
    assert res.status_code == 200
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://api.telegram.org/botoperator-bot-token/sendMessage"
    payload = _body(req)
    assert payload["chat_id"] == "10001"
    assert payload["chat_id"] not in {"424242", "+79990001122", "ivan_lead", "@ivan_lead"}
    for bit in ("424242", "79990001122", "ivan_lead"):
        assert bit not in req.full_url
    assert "созвон" in payload["text"]
    assert "потом" not in payload["text"]
    assert "вчера" not in payload["text"]
    assert "закрыто" not in payload["text"]
    row = payload["reply_markup"]["inline_keyboard"][0]
    assert row[0] == {"text": "Сделано", "callback_data": f"done:{open_row['id']}"}
    assert row[1] == {"text": "К лиду", "callback_data": f"lead:{lead['id']}"}
    assert res.json()["chat_id"] == "10001"
    assert res.json()["count"] == 1


def test_empty_day_is_honest(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "operator-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    calls = _install(monkeypatch)
    lead = _lead(conn)
    create_reminder(conn, lead["id"], "2099-06-01T08:00:00", "не сегодня")
    sent = send_morning_digest(conn, now="2026-11-04T12:00:00")
    assert sent["text"] == EMPTY_TEXT
    assert len(calls) == 1
    payload = _body(calls[0])
    assert payload["text"] == "На сегодня задач нет."
    assert "reply_markup" not in payload
    assert payload["chat_id"] == "10001"
    assert payload["chat_id"] != "+79990001122"


def test_fixed_day_picks_only_that_utc_date(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "operator-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    calls = _install(monkeypatch)
    lead = _lead(conn)
    create_reminder(conn, lead["id"], "2026-11-04T08:15:00", "утро")
    create_reminder(conn, lead["id"], "2026-11-03T23:00:00", "вчера")
    create_reminder(conn, lead["id"], "2026-11-05T00:00:00", "завтра")
    sent = send_morning_digest(conn, now="2026-11-04T22:00:00")
    assert "утро" in sent["text"]
    assert "вчера" not in sent["text"]
    assert "завтра" not in sent["text"]
    assert len(calls) == 1


def test_missing_operator_does_not_claim_sent(client, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)
    calls = _install(monkeypatch)
    res = client.post("/api/digest/send")
    assert res.status_code == 400
    assert "отправлен" not in res.text.lower()
    assert calls == []
    monkeypatch.setenv("BOT_TOKEN", "operator-bot-token")
    res_chat = client.post("/api/digest/send")
    assert res_chat.status_code == 400
    assert "отправлен" not in res_chat.text.lower()
    assert calls == []


def test_telegram_reject_is_not_success(client, conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "operator-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    _lead(conn)
    calls = _install(monkeypatch, status=403, body=b'{"ok": false}', raise_http=True)
    res = client.post("/api/digest/send")
    assert res.status_code == 400
    assert res.json()["detail"] == "Telegram не принял сообщение"
    assert "отправлен" not in res.text.lower()
    assert len(calls) == 1
    false_ok = _install(monkeypatch, status=200, body=b'{"ok": false}')
    again = client.post("/api/digest/send")
    assert again.status_code == 400
    assert len(false_ok) == 1


def test_done_marks_reminder_and_lead_does_not_message(client, conn, monkeypatch):
    calls = _install(monkeypatch)
    lead = _lead(conn)
    row = create_reminder(conn, lead["id"], "2026-11-04T09:00:00", "созвон")
    done = client.post("/api/digest/callback", json={"data": f"done:{row['id']}"})
    assert done.status_code == 200
    body = done.json()
    assert body["action"] == "done"
    assert body["lead_id"] == lead["id"]
    assert body["done_at"]
    stored = conn.execute("SELECT done_at FROM reminders WHERE id = ?", (row["id"],)).fetchone()
    assert stored["done_at"]
    opened = client.post("/api/digest/callback", json={"data": f"lead:{lead['id']}"})
    assert opened.status_code == 200
    assert opened.json() == {"ok": True, "action": "open", "lead_id": lead["id"]}
    card = client.get(f"/api/leads/{opened.json()['lead_id']}")
    assert card.status_code == 200
    assert card.json()["username"] == "ivan_lead"
    assert calls == []
    hook = client.post(
        "/api/digest/webhook",
        json={"callback_query": {"data": f"lead:{lead['id']}"}},
    )
    assert hook.status_code == 200
    assert hook.json()["lead_id"] == lead["id"]
    assert calls == []


def test_page_has_digest_button(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Прислать дайджест" in page.text
    assert "sendDigest" in client.get("/static/app.js").text
