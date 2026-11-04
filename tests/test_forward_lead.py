import json

import pytest
from fastapi.testclient import TestClient

from app.bot_login import NO_TOKEN, apply_forward_update, poll_bot_login
from app.db import connect, init_db
from app.leads import create_lead
from app.main import create_app
from app.notes import list_notes
from app.telegram_gateway import FakeGateway


class _Resp:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _no_network(*args, **kwargs):
    raise AssertionError("network")


def _forward(chat_id, *, sender=None, sender_name=None, text="Привет", origin=None, update_id=1, caption=None):
    message = {
        "message_id": update_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": int(chat_id), "first_name": "Оператор"},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    if sender is not None:
        message["forward_from"] = sender
    if sender_name is not None:
        message["forward_sender_name"] = sender_name
    if origin is not None:
        message["forward_origin"] = origin
    return {"update_id": update_id, "message": message}


def _ivan():
    return {"id": 777, "first_name": "Иван", "last_name": "Петров", "username": "ivan"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    db = connect(tmp_path / "crm.db")
    init_db(db)
    return db


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_forward_from_creates_lead_and_note(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    result = apply_forward_update(conn, _forward(10001, sender=_ivan(), text="Привет"))
    assert result["created"] is True
    assert _count(conn, "leads") == 1
    lead = conn.execute("SELECT * FROM leads").fetchone()
    assert lead["name"] == "Иван Петров"
    assert lead["username"] == "ivan"
    assert lead["telegram_user_id"] == 777
    assert lead["account_id"] is None
    assert (lead["note"] or "") == ""
    notes = list_notes(conn, lead["id"])
    assert len(notes) == 1
    assert notes[0]["body"] == "Привет"
    assert notes[0]["source"] == "forwarded"
    assert result["note_id"] == notes[0]["id"]
    assert result["lead_id"] == lead["id"]


def test_same_forward_id_adds_note_not_lead(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    create_lead(conn, name="Уже", telegram_user_id=777, username="old")
    first = apply_forward_update(conn, _forward(10001, sender=_ivan(), text="Раз", update_id=1))
    second = apply_forward_update(conn, _forward(10001, sender=_ivan(), text="Два", update_id=2))
    assert first["created"] is False
    assert second["created"] is False
    assert first["lead_id"] == second["lead_id"]
    assert _count(conn, "leads") == 1
    notes = list_notes(conn, first["lead_id"])
    assert [row["body"] for row in notes] == ["Раз", "Два"]
    replay = apply_forward_update(conn, _forward(10001, sender=_ivan(), text="Раз", update_id=1))
    assert replay["note_id"] is None
    assert [row["body"] for row in list_notes(conn, first["lead_id"])] == ["Раз", "Два"]


def test_hidden_sender_has_name_and_empty_username(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    result = apply_forward_update(
        conn,
        _forward(10001, sender_name="Скрытый Автор", text="тихо", update_id=3),
    )
    assert result["created"] is True
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (result["lead_id"],)).fetchone()
    assert lead["name"] == "Скрытый Автор"
    assert lead["username"] is None
    assert lead["telegram_user_id"] is None
    assert list_notes(conn, lead["id"])[0]["body"] == "тихо"
    origin = apply_forward_update(
        conn,
        _forward(
            10001,
            text="из origin",
            update_id=4,
            origin={"type": "hidden_user", "sender_user_name": "Невидимка", "date": 1},
        ),
    )
    hidden = conn.execute("SELECT * FROM leads WHERE id = ?", (origin["lead_id"],)).fetchone()
    assert hidden["name"] == "Невидимка"
    assert hidden["username"] is None
    assert _count(conn, "leads") == 2


def test_empty_text_creates_lead_without_note(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    result = apply_forward_update(
        conn,
        _forward(10001, sender={"id": 888, "first_name": "Оля"}, text="  ", update_id=5),
    )
    assert result["created"] is True
    assert result["note_id"] is None
    assert list_notes(conn, result["lead_id"]) == []
    caption = apply_forward_update(
        conn,
        _forward(
            10001,
            sender={"id": 889, "first_name": "Катя", "username": "@katya"},
            text=None,
            caption="Подпись",
            update_id=6,
        ),
    )
    assert list_notes(conn, caption["lead_id"])[0]["body"] == "Подпись"
    lead = conn.execute("SELECT username FROM leads WHERE id = ?", (caption["lead_id"],)).fetchone()
    assert lead["username"] == "katya"


def test_foreign_chat_and_empty_token_create_nothing(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    foreign = apply_forward_update(conn, _forward(99999, sender=_ivan(), text="Чужой"))
    assert foreign == {"created": False, "lead_id": None, "note_id": None}
    plain = apply_forward_update(conn, _forward(10001, text="просто текст", update_id=8))
    assert plain["lead_id"] is None
    channel = apply_forward_update(
        conn,
        _forward(
            10001,
            text="канал",
            update_id=9,
            origin={"type": "channel", "chat": {"id": -100, "title": "News"}, "date": 1},
        ),
    )
    assert channel["lead_id"] is None
    assert _count(conn, "leads") == 0
    assert _count(conn, "notes") == 0
    monkeypatch.setenv("BOT_TOKEN", "   ")
    with pytest.raises(ValueError, match=NO_TOKEN):
        apply_forward_update(conn, _forward(10001, sender=_ivan(), text="Нет токена"))
    assert _count(conn, "leads") == 0


def test_api_and_poll_do_not_send(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "10001")
    app = create_app(conn=conn, gateway=FakeGateway())
    update = _forward(
        10001,
        text="Из origin",
        update_id=11,
        origin={
            "type": "user",
            "date": 1,
            "sender_user": {"id": 555, "first_name": "Оля", "username": "olya"},
        },
    )
    with TestClient(app) as client:
        res = client.post("/api/operator/update", json=update)
        assert res.status_code == 200
        body = res.json()
        assert body["lead_created"] is True
        assert body["logged_in"] is False
        lead_id = body["lead_id"]
        card = client.get(f"/api/leads/{lead_id}/card")
        assert card.status_code == 200
        assert card.json()["name"] == "Оля"
        assert card.json()["username"] == "olya"
        assert card.json()["notes"][0]["body"] == "Из origin"
        assert card.json()["notes"][0]["source"] == "forwarded"
    urls = []

    def opener(req, timeout=15):
        urls.append(req.full_url)
        payload = {"ok": True, "result": [_forward(10001, sender=_ivan(), text="Из очереди", update_id=12)]}
        return _Resp(json.dumps(payload).encode("utf-8"))

    poll_bot_login(conn, opener)
    poll_bot_login(conn, opener)
    assert urls
    assert all(url.endswith("/getUpdates") for url in urls)
    assert all("sendMessage" not in url for url in urls)
    queued = conn.execute(
        "SELECT id FROM leads WHERE telegram_user_id = 777"
    ).fetchone()
    assert [row["body"] for row in list_notes(conn, queued["id"])] == ["Из очереди"]
