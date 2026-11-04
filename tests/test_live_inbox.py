import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from telethon.tl.types import Channel, User

from app.db import connect, init_db
from app.main import create_app, stop_inbox_watches
from app.telegram_gateway import FakeGateway, TelethonGateway, listening_event


def _connect(client):
    started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
    assert started.status_code == 200
    account_id = started.json()["id"]
    confirmed = client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "connected"
    return account_id


def _session(conn, account_id):
    row = conn.execute(
        "SELECT session_path FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return row["session_path"]


def _incoming(**extra):
    payload = {
        "telegram_user_id": 77,
        "username": "newcomer",
        "name": "Новый",
        "text": "привет из телеграма",
        "telegram_message_id": 501,
        "sent_at": "2024-05-01T12:00:00",
        "direction": "in",
        "peer_type": "user",
    }
    payload.update(extra)
    return payload


def test_offline_private_appears_without_search(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111")
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        account_id = started.json()["id"]
        gateway.push_private(_session(conn, account_id), _incoming())
        gateway.push_private(
            _session(conn, account_id),
            _incoming(
                telegram_user_id=88,
                username="room",
                name="Группа",
                text="шум",
                telegram_message_id=502,
                is_group=True,
                peer_type="chat",
            ),
        )

        async def boom(*_args, **_kwargs):
            raise AssertionError("поиск не нужен")

        gateway.search_dialogs = boom
        confirmed = client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        assert confirmed.status_code == 200
        listed = client.get("/api/inbox", params={"account_id": account_id})
        assert listed.status_code == 200
        rows = listed.json()
        assert [row["username"] for row in rows] == ["newcomer"]
        assert rows[0]["last_message_preview"] == "привет из телеграма"
        assert rows[0]["unread_count"] == 1
        assert rows[0]["last_from_peer"] == 1


def test_connected_inbox_without_search_or_event(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(
        code="11111",
        dialogs=[
            {"telegram_user_id": 77, "username": "newcomer", "name": "Новый"},
            {
                "telegram_user_id": 88,
                "username": "room",
                "name": "Группа",
                "is_group": True,
                "peer_type": "chat",
            },
            {
                "telegram_user_id": 99,
                "username": "news",
                "name": "Канал",
                "peer_type": "channel",
            },
        ],
    )
    loads = {"n": 0}
    original = gateway.load_inbox

    async def counted(session_path, limit=30):
        loads["n"] += 1
        return await original(session_path, limit)

    async def boom(*_args, **_kwargs):
        raise AssertionError("поиск не нужен")

    gateway.load_inbox = counted
    gateway.search_dialogs = boom
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        account_id = _connect(client)
        listed = client.get("/api/inbox", params={"account_id": account_id})
        assert listed.status_code == 200
        rows = listed.json()
        assert [row["username"] for row in rows] == ["newcomer"]
        assert rows[0]["name"] == "Новый"
        assert rows[0]["unread_count"] == 0
        again = client.get("/api/inbox", params={"account_id": account_id})
        assert [row["username"] for row in again.json()] == ["newcomer"]
        assert loads["n"] == 1


def test_live_private_appears_without_search(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111")
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        account_id = _connect(client)

        async def boom(*_args, **_kwargs):
            raise AssertionError("поиск не нужен")

        gateway.search_dialogs = boom
        gateway.push_private(_session(conn, account_id), _incoming())
        gateway.push_private(
            _session(conn, account_id),
            _incoming(
                telegram_user_id=99,
                username="news",
                name="Канал",
                text="эфир",
                telegram_message_id=503,
                peer_type="channel",
            ),
        )
        listed = client.get("/api/inbox", params={"account_id": account_id})
        assert listed.status_code == 200
        rows = listed.json()
        assert [row["telegram_user_id"] for row in rows] == [77]
        assert rows[0]["name"] == "Новый"


async def _asgi(app, method, path, body=None, query=b""):
    payload = b"" if body is None else json.dumps(body).encode()
    sent = False
    response = {"status": None, "headers": [], "body": b""}
    finished = asyncio.Event()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
            response["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response["body"] += message.get("body", b"")
            if not message.get("more_body", False):
                finished.set()

    await app(scope, receive, send)
    return response


def test_sse_dialog_and_live_flag(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    gateway = FakeGateway(code="11111")
    app = create_app(conn=conn, gateway=gateway)

    async def run_stream():
        started = await _asgi(app, "POST", "/api/accounts/start", {"phone": "+79990001122"})
        assert started["status"] == 200
        account_id = json.loads(started["body"])["id"]
        confirmed = await _asgi(app, "POST", f"/api/accounts/{account_id}/code", {"code": "11111"})
        assert confirmed["status"] == 200
        assert json.loads(confirmed["body"])["status"] == "connected"
        chunks = []
        content_type = []

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/inbox/events",
            "raw_path": b"/api/inbox/events",
            "query_string": f"account_id={account_id}".encode(),
            "headers": [],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }

        async def receive():
            await asyncio.Future()

        async def send(message):
            if message["type"] == "http.response.start":
                content_type.extend(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        task = asyncio.create_task(app(scope, receive, send))
        try:
            blob = b""
            for _ in range(100):
                blob = b"".join(chunks)
                if b'"ok": true' in blob and b"event: live" in blob:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError(blob.decode() or "live event did not arrive")
            header = b" ".join(value for key, value in content_type if key == b"content-type")
            assert header.startswith(b"text/event-stream")
            gateway.push_private(_session(conn, account_id), _incoming())
            for _ in range(100):
                blob = b"".join(chunks)
                if b"newcomer" in blob and b"event: dialog" in blob:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError(blob.decode() or "dialog event did not arrive")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await stop_inbox_watches(app)

    asyncio.run(run_stream())


class _Event:
    def __init__(self, sender, text, mid, private):
        self.is_private = private
        self.is_group = not private
        self.is_channel = not private
        self._sender = sender
        self.message = type(
            "Msg",
            (),
            {
                "id": mid,
                "message": text,
                "date": datetime(2024, 5, 1, tzinfo=timezone.utc),
                "out": False,
            },
        )()

    async def get_sender(self):
        return self._sender


def test_telethon_watch_skips_channel_and_catches_up():
    gateway = TelethonGateway()
    user = User(id=7)
    user.username = "ann"
    user.first_name = "Ann"
    channel = Channel(id=9, title="news", photo=None, date=None, broadcast=True)
    seen = []

    class Client:
        def __init__(self):
            self.handlers = []
            self.caught = 0

        def add_event_handler(self, callback, _event):
            self.handlers.append(callback)

        def remove_event_handler(self, callback):
            self.handlers.remove(callback)

        async def catch_up(self):
            self.caught += 1

        async def run_until_disconnected(self):
            await asyncio.Future()

    client = Client()

    async def run():
        async def open_client(_session):
            return client, False

        gateway._open = open_client

        async def on_message(payload):
            seen.append(payload)

        task = asyncio.create_task(gateway.watch_private("sess", on_message))
        await asyncio.wait_for(listening_event(gateway, "sess").wait(), timeout=2)
        assert client.caught == 1
        await client.handlers[0](_Event(channel, "эфир", 6, False))
        await client.handlers[0](_Event(user, "привет", 5, True))
        assert [row["telegram_user_id"] for row in seen] == [7]
        assert seen[0]["direction"] == "in"
        assert seen[0]["username"] == "ann"
        assert seen[0]["text"] == "привет"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
