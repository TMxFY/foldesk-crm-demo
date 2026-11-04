import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from telethon.tl.types import Channel, User

from app.db import connect, init_db, utc_now
from app.leads import create_lead, search_leads
from app.main import create_app
from app.reminders import create_reminder
from app.telegram_gateway import FakeGateway, GatewayError, TelethonGateway

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
        closed = client.get(f"/api/dialogs/{dialog['id']}/messages")
        assert closed.status_code == 400
        assert "не подключён" in closed.text
        stored = conn.execute(
            "SELECT text FROM messages WHERE dialog_id = ?", (dialog["id"],)
        ).fetchall()
        assert [row[0] for row in stored] == ["один ответ"]


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


def _history_rows(count=60):
    rows = []
    for i in range(1, count + 1):
        rows.append({
            "telegram_message_id": 100 + i,
            "text": f"m{i}",
            "sent_at": f"2020-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
            "direction": "in" if i % 2 else "out",
        })
    return rows


def test_personal_thread_loads_fifty_then_keeps_reply(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111", dialogs=DIALOGS, history={42: _history_rows()})
    fetches = []

    async def spy(session_path, peer_id, limit=50):
        fetches.append((int(peer_id), limit))
        return await FakeGateway.fetch_messages(gateway, session_path, peer_id, limit)

    gateway.fetch_messages = spy
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        account_id = started.json()["id"]
        client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        dialog = client.get(
            f"/api/accounts/{account_id}/dialogs", params={"q": "@ivan"}
        ).json()[0]
        listed = client.get(f"/api/dialogs/{dialog['id']}/messages")
        assert listed.status_code == 200
        body = listed.json()
        assert [row["text"] for row in body] == [f"m{i}" for i in range(11, 61)]
        assert body[0]["direction"] == "in"
        assert fetches == [(42, 50)]
        sent = client.post(
            f"/api/dialogs/{dialog['id']}/messages", json={"text": "живой ответ"}
        )
        assert sent.status_code == 200
        assert sent.json()["direction"] == "out"
        assert sent.json()["telegram_message_id"] not in {100 + i for i in range(1, 61)}
        again = client.get(f"/api/dialogs/{dialog['id']}/messages")
        assert again.status_code == 200
        texts = [row["text"] for row in again.json()]
        assert texts[-1] == "живой ответ"
        assert "живой ответ" in texts
        assert len(texts) == 50
        assert "m11" not in texts


def test_thread_refuses_closed_account_and_non_personal(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111", dialogs=DIALOGS)
    fetches = []

    async def spy(session_path, peer_id, limit=50):
        fetches.append(int(peer_id))
        return await FakeGateway.fetch_messages(gateway, session_path, peer_id, limit)

    gateway.fetch_messages = spy
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+100"})
        account_id = started.json()["id"]
        client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        dialog = client.get(
            f"/api/accounts/{account_id}/dialogs", params={"q": "@ivan"}
        ).json()[0]
        conn.execute("UPDATE accounts SET status = 'error' WHERE id = ?", (account_id,))
        conn.commit()
        closed = client.get(f"/api/dialogs/{dialog['id']}/messages")
        assert closed.status_code == 400
        assert "не подключён" in closed.text
        assert closed.json() != []
        now = utc_now()
        conn.execute("UPDATE accounts SET status = 'connected' WHERE id = ?", (account_id,))
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, title, is_group,
                unread_count, last_from_peer, muted, created_at, updated_at
            ) VALUES (?, 'channel', 99, 'канал', 0, 0, 0, 0, ?, ?)
            """,
            (account_id, now, now),
        )
        conn.execute(
            "UPDATE dialogs SET is_group = 1 WHERE id = ?", (dialog["id"],)
        )
        conn.commit()
        channel_id = conn.execute(
            "SELECT id FROM dialogs WHERE peer_type = 'channel'"
        ).fetchone()[0]
        channel = client.get(f"/api/dialogs/{channel_id}/messages")
        grouped = client.get(f"/api/dialogs/{dialog['id']}/messages")
        assert channel.status_code == 400
        assert "не личный" in channel.text
        assert grouped.status_code == 400
        assert "не личный" in grouped.text
        assert fetches == []


def test_telethon_history_keeps_users_and_last_fifty():
    gateway = TelethonGateway()
    user = User(id=42)
    channel = Channel(id=7, title="news", photo=None, date=None, broadcast=True)

    class Msg:
        def __init__(self, mid):
            self.id = mid
            self.out = mid % 2 == 0
            self.message = f"m{mid}"
            self.date = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=mid)
            self.reply_to = type("Reply", (), {"reply_to_msg_id": 3})() if mid == 12 else None

    class Client:
        def __init__(self, entity):
            self.entity = entity
            self.reads = 0

        async def get_entity(self, peer):
            return self.entity

        async def get_messages(self, entity, limit=50):
            self.reads += 1
            assert limit == 50
            assert entity is user
            return [Msg(i) for i in range(60, 0, -1)]

    user_client = Client(user)
    channel_client = Client(channel)

    async def run():
        rows = await gateway._history(user_client, 42, 80)
        assert user_client.reads == 1
        assert [row["telegram_message_id"] for row in rows] == list(range(11, 61))
        assert rows[0]["direction"] == "in"
        assert rows[1]["reply_to_telegram_message_id"] == 3
        assert rows[1]["sent_at"] == "2020-01-01T00:12:00"
        try:
            await gateway._history(channel_client, 7, 50)
        except GatewayError as exc:
            assert "не личный" in str(exc)
        else:
            raise AssertionError("канал не должен открываться")
        assert channel_client.reads == 0

    asyncio.run(run())


def test_create_lead_keeps_optional_blanks_empty(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    lead = create_lead(conn, name="Иван", email="  ", url="", note="")
    assert lead["email"] is None
    assert lead["url"] is None
    assert lead["note"] is None
    reminder = create_reminder(conn, lead["id"], "2099-01-01T00:00:00", "позже")
    assert reminder["done_at"] is None
