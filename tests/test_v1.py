import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.accounts import confirm_login, confirm_password, list_accounts, start_login
from app.db import connect, init_db
from app.leads import add_lead_from_dialog, create_lead, search_leads, update_lead
from app.main import create_app
from app.reminders import complete_reminder, create_reminder, list_reminders
from app.telegram_gateway import FakeGateway, GatewayError

DIALOGS = [
    {"telegram_user_id": 42, "username": "ivan", "name": "Иван Петров"},
    {"telegram_user_id": 43, "username": "olga", "name": "Ольга"},
]


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "crm.db")
    init_db(db)
    return db


@pytest.fixture
def client(conn):
    gateway = FakeGateway(code="11111", dialogs=DIALOGS)
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as test_client:
        yield test_client


def test_model_search_status_overdue(conn):
    ivan = create_lead(conn, name="Иван Петров", username="ivan_p")
    create_lead(conn, name="Ольга", username="olga")
    by_name = search_leads(conn, q="иван")
    assert [row["id"] for row in by_name] == [ivan["id"]]
    by_username = search_leads(conn, q="@ivan_p")
    assert by_username[0]["id"] == ivan["id"]
    assert update_lead(conn, ivan["id"], status="talking")["status"] == "talking"
    with pytest.raises(ValueError):
        update_lead(conn, ivan["id"], status="nope")
    reminder = create_reminder(conn, ivan["id"], "2000-01-01T00:00:00", "написать")
    create_reminder(conn, ivan["id"], "2099-01-01T00:00:00", "потом")
    overdue = list_reminders(conn, scope="overdue")
    assert [row["id"] for row in overdue] == [reminder["id"]]
    assert len(list_reminders(conn, scope="open")) == 2
    complete_reminder(conn, reminder["id"])
    assert list_reminders(conn, scope="overdue") == []
    assert len(list_reminders(conn, scope="open")) == 1


def test_login_states_and_dialog_lead_once(conn):
    gateway = FakeGateway(code="11111", password="pw", needs_password=True, dialogs=DIALOGS)

    async def scenario():
        started = await start_login(conn, "+79990001122", gateway)
        assert started["status"] == "pending_code"
        assert list_accounts(conn)[0]["status"] == "pending_code"
        with pytest.raises(Exception):
            await confirm_login(conn, started["id"], "00000", gateway)
        assert list_accounts(conn)[0]["status"] == "pending_code"
        waiting = await confirm_login(conn, started["id"], "11111", gateway)
        assert waiting["status"] == "pending_password"
        with pytest.raises(Exception):
            await confirm_password(conn, started["id"], "nope", gateway)
        connected = await confirm_password(conn, started["id"], "pw", gateway)
        assert connected["status"] == "connected"
        assert list_accounts(conn)[0]["telegram_username"] == "operator"
        found = await gateway.search_dialogs(
            f"data/sessions/{started['id']}.session", "@ivan"
        )
        assert found[0]["telegram_user_id"] == 42
        first = add_lead_from_dialog(
            conn, started["id"], found[0]["telegram_user_id"], found[0]["username"], found[0]["name"]
        )
        second = add_lead_from_dialog(
            conn, started["id"], found[0]["telegram_user_id"], found[0]["username"], found[0]["name"]
        )
        assert first["id"] == second["id"]
        assert len(search_leads(conn)) == 1

    asyncio.run(scenario())


def test_api_search_status_reminder_and_page(client):
    page = client.get("/")
    assert page.status_code == 200
    for word in ("Аккаунт", "Поиск", "Лиды", "Напоминания", "В CRM", "В Telegram"):
        assert word in page.text
    for anchor in ("s04", "s05", "s06", "s07", "s09", "digest"):
        assert f'id="{anchor}"' in page.text
    assert "/design/tokens.css" in page.text
    brand = json.loads((Path(__file__).resolve().parents[1] / "replica" / "brand.json").read_text(encoding="utf-8"))
    for host in brand["domains"]:
        assert host not in page.text
    css = client.get("/static/app.css")
    assert css.status_code == 200
    for token in ("var(--bg)", "var(--text)", "var(--accent)", "var(--space-4)"):
        assert token in css.text
    assert "#f4f1ea" not in css.text
    assert "#1c1915" not in css.text
    assert "#0f6e56" not in css.text
    tokens = client.get("/design/tokens.css")
    assert tokens.status_code == 200
    assert "--bg:" in tokens.text
    assert "--accent:" in tokens.text

    ivan = client.post("/api/leads", json={"name": "Иван Петров", "username": "ivan"}).json()
    client.post("/api/leads", json={"name": "Мария", "username": "maria"})
    by_name = client.get("/api/leads", params={"q": "иван"}).json()
    assert [row["id"] for row in by_name] == [ivan["id"]]
    by_user = client.get("/api/leads", params={"q": "@maria"}).json()
    assert by_user[0]["name"] == "Мария"
    patched = client.patch(f"/api/leads/{ivan['id']}", json={"status": "waiting", "note": "счёт"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "waiting"
    assert client.patch(f"/api/leads/{ivan['id']}", json={"status": "nope"}).status_code == 400

    past = client.post(
        "/api/reminders",
        json={"lead_id": ivan["id"], "due_at": "2000-01-01T00:00:00", "text": "позвонить"},
    ).json()
    client.post(
        "/api/reminders",
        json={"lead_id": ivan["id"], "due_at": "2099-01-01T00:00", "text": "позже"},
    )
    overdue = client.get("/api/reminders", params={"scope": "overdue"}).json()
    assert [row["text"] for row in overdue] == ["позвонить"]
    assert client.post(f"/api/reminders/{past['id']}/done").status_code == 200
    assert client.get("/api/reminders", params={"scope": "overdue"}).json() == []


def test_api_login_dialog_once(conn):
    gateway = FakeGateway(code="11111", dialogs=DIALOGS)
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        started = client.post("/api/accounts/start", json={"phone": "+79990001122"})
        assert started.status_code == 200
        assert started.json()["status"] == "pending_code"
        account_id = started.json()["id"]
        assert client.post(f"/api/accounts/{account_id}/code", json={"code": "000"}).status_code == 400
        confirmed = client.post(f"/api/accounts/{account_id}/code", json={"code": "11111"})
        assert confirmed.json()["status"] == "connected"
        dialogs = client.get(f"/api/accounts/{account_id}/dialogs", params={"q": "@ivan"})
        assert dialogs.status_code == 200
        assert dialogs.json()[0]["name"] == "Иван Петров"
        payload = {"account_id": account_id, **dialogs.json()[0]}
        first = client.post("/api/leads", json=payload)
        second = client.post("/api/leads", json=payload)
        assert first.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert len(client.get("/api/leads").json()) == 1


def test_due_offset_and_space_compare_as_utc(conn):
    lead = create_lead(conn, name="Иван")
    shifted = create_reminder(conn, lead["id"], "2026-11-04T08:00:00-03:00", "смещение")
    assert shifted["due_at"] == "2026-11-04T11:00:00"
    spaced = create_reminder(conn, lead["id"], "2026-11-04 18:00", "пробел")
    assert spaced["due_at"] == "2026-11-04T18:00:00"
    morning = create_reminder(conn, lead["id"], "2026-11-04T08:30:00Z", "утро")
    assert morning["due_at"] == "2026-11-04T08:30:00"
    conn.execute(
        """
        INSERT INTO reminders (lead_id, due_at, text, created_at)
        VALUES (?, '2026-11-04 18:00:00', 'сырой пробел', '2020-01-01T00:00:00')
        """,
        (lead["id"],),
    )
    conn.execute(
        """
        INSERT INTO reminders (lead_id, due_at, text, created_at)
        VALUES (?, '2026-11-04T08:00:00-03:00', 'сырое смещение', '2020-01-01T00:00:00')
        """,
        (lead["id"],),
    )
    conn.commit()
    moment = "2026-11-04T09:00:00"
    overdue = {row["text"] for row in list_reminders(conn, scope="overdue", now=moment)}
    assert overdue == {"утро"}


def test_manual_telegram_id_unique_without_account(conn):
    first = create_lead(conn, name="А", telegram_user_id=42)
    with pytest.raises(ValueError, match="telegram"):
        create_lead(conn, name="Б", telegram_user_id=42)
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at)
        VALUES ('+100', 'data/sessions/1.session', 'connected', '2020-01-01T00:00:00')
        """
    )
    conn.commit()
    account_id = conn.execute("SELECT id FROM accounts").fetchone()[0]
    other = create_lead(conn, name="В", telegram_user_id=42, account_id=account_id)
    assert other["id"] != first["id"]
    plain = create_lead(conn, name="Г", phone="+700")
    assert plain["telegram_user_id"] is None


def test_api_duplicate_telegram_without_account(client):
    first = client.post("/api/leads", json={"name": "А", "telegram_user_id": 42})
    second = client.post("/api/leads", json={"name": "Б", "telegram_user_id": 42})
    assert first.status_code == 200
    assert second.status_code == 400


def test_password_connect_error_keeps_pending(conn):
    gateway = FakeGateway(code="11111", password="pw", needs_password=True, dialogs=DIALOGS)

    class DropConnect(FakeGateway):
        async def confirm_password(self, password, session_path):
            raise ConnectionError("connect failed")

    async def scenario():
        started = await start_login(conn, "+79990001122", gateway)
        waiting = await confirm_login(conn, started["id"], "11111", gateway)
        assert waiting["status"] == "pending_password"
        with pytest.raises(GatewayError):
            await confirm_password(conn, started["id"], "pw", DropConnect())
        assert list_accounts(conn)[0]["status"] == "pending_password"
        connected = await confirm_password(conn, started["id"], "pw", gateway)
        assert connected["status"] == "connected"

    asyncio.run(scenario())


def test_start_requires_telegram_keys(conn, monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        response = client.post("/api/accounts/start", json={"phone": "+100"})
    assert response.status_code == 400
    assert "https://my.telegram.org/apps" in response.text
