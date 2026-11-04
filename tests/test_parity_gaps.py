from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.leads import create_lead, search_leads
from app.main import create_app
from app.reminders import create_reminder
from app.telegram_gateway import FakeGateway, GatewayError

DIALOGS = [
    {"telegram_user_id": 42, "username": "ivan", "name": "Иван Петров"},
]


def test_manual_fields_and_postpone(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        created = client.post("/api/leads", json={
            "name": "Анна",
            "username": "@anna",
            "phone": "+79991112233",
            "note": "созвон",
            "email": "anna@example.com",
            "url": "https://example.com/anna",
        })
        assert created.status_code == 200
        lead = created.json()
        assert lead["username"] == "anna"
        assert lead["email"] == "anna@example.com"
        assert lead["url"] == "https://example.com/anna"
        assert lead["note"] == "созвон"
        fetched = client.get(f"/api/leads/{lead['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["email"] == "anna@example.com"
        assert client.get("/api/leads/999999").status_code == 404
        found = search_leads(conn, q="anna@example.com")
        assert [row["id"] for row in found] == [lead["id"]]

        reminder = client.post("/api/reminders", json={
            "lead_id": lead["id"],
            "due_at": "2000-01-01T00:00:00",
            "text": "позвонить",
        }).json()
        moved = client.post(
            f"/api/reminders/{reminder['id']}/postpone",
            json={"due_at": "2099-06-01T15:30:00"},
        )
        assert moved.status_code == 200
        assert moved.json()["due_at"] == "2099-06-01T15:30:00"
        assert moved.json()["done_at"] is None
        bad = client.post(
            f"/api/reminders/{reminder['id']}/postpone",
            json={"due_at": "не дата"},
        )
        assert bad.status_code == 400
        still = client.get("/api/reminders", params={"scope": "all"}).json()[0]
        assert still["due_at"] == "2099-06-01T15:30:00"
        assert client.post("/api/reminders/999999/postpone", json={"due_at": "2099-01-01T00:00:00"}).status_code == 404
        assert client.post(f"/api/reminders/{reminder['id']}/done").status_code == 200
        closed = client.post(
            f"/api/reminders/{reminder['id']}/postpone",
            json={"due_at": "2099-07-01T00:00:00"},
        )
        assert closed.status_code == 400
        assert "закрыто" in closed.text
        final = client.get("/api/reminders", params={"scope": "all"}).json()[0]
        assert final["due_at"] == "2099-06-01T15:30:00"


def test_inbox_cache_and_one_reply(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111", dialogs=DIALOGS)
    calls = []

    async def spy(session_path, peer_id, text, reply_to=None):
        calls.append(text)
        return await FakeGateway.send_message(gateway, session_path, peer_id, text, reply_to)

    gateway.send_message = spy
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        account_id = started.json()["id"]
        assert client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"}).json()["status"] == "connected"
        found = client.get(f"/api/accounts/{account_id}/dialogs", params={"q": "@ivan"})
        assert found.status_code == 200
        dialog = found.json()[0]
        assert dialog["name"] == "Иван Петров"
        assert dialog["id"]

        async def offline(*_args, **_kwargs):
            raise AssertionError("поиск не должен звать Telegram повторно")

        gateway.search_dialogs = offline
        cached = client.get("/api/inbox", params={"account_id": account_id})
        assert cached.status_code == 200
        assert cached.json()[0]["id"] == dialog["id"]
        assert cached.json()[0]["telegram_user_id"] == 42

        empty = client.post(f"/api/dialogs/{dialog['id']}/messages", json={"text": "  "})
        assert empty.status_code == 400
        assert calls == []
        sent = client.post(f"/api/dialogs/{dialog['id']}/messages", json={"text": "один ответ"})
        assert sent.status_code == 200
        assert sent.json()["text"] == "один ответ"
        assert sent.json()["direction"] == "out"
        assert calls == ["один ответ"]
        listed = client.get(f"/api/dialogs/{dialog['id']}/messages").json()
        assert [row["text"] for row in listed] == ["один ответ"]

        conn.execute("UPDATE accounts SET status = 'error' WHERE id = ?", (account_id,))
        conn.commit()
        refused = client.post(f"/api/dialogs/{dialog['id']}/messages", json={"text": "ещё"})
        assert refused.status_code == 400
        assert "не подключён" in refused.text
        assert calls == ["один ответ"]
        assert len(client.get(f"/api/dialogs/{dialog['id']}/messages").json()) == 1


def test_reply_does_not_store_when_gateway_fails(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111", dialogs=DIALOGS)

    async def boom(*_args, **_kwargs):
        raise GatewayError("сеть отвалилась")

    gateway.send_message = boom
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+100"})
        account_id = started.json()["id"]
        client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        dialog = client.get(f"/api/accounts/{account_id}/dialogs", params={"q": "@ivan"}).json()[0]
        failed = client.post(f"/api/dialogs/{dialog['id']}/messages", json={"text": "привет"})
        assert failed.status_code == 400
        assert "сеть" in failed.text
        assert client.get(f"/api/dialogs/{dialog['id']}/messages").json() == []


def test_create_lead_keeps_optional_blanks_empty(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    lead = create_lead(conn, name="Иван", email="  ", url="", note="")
    assert lead["email"] is None
    assert lead["url"] is None
    assert lead["note"] is None
    reminder = create_reminder(conn, lead["id"], "2099-01-01T00:00:00", "позже")
    assert reminder["done_at"] is None
