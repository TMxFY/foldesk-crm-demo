from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.leads import create_lead
from app.main import create_app
from app.reminders import create_reminder
from app.telegram_gateway import FakeGateway


def test_existing_file_gains_card_tables(tmp_path):
    path = tmp_path / "crm.db"
    conn = connect(path)
    init_db(conn)
    lead = create_lead(conn, name="Анна", note="не трогать")
    reminder = create_reminder(conn, lead["id"], "2099-01-01T00:00:00", "позже")
    inode = path.stat().st_ino
    for table in ("property_values", "property_defs", "notes", "tasks"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    init_db(conn)
    assert path.stat().st_ino == inode
    assert conn.execute(
        "SELECT note FROM leads WHERE id = ?", (lead["id"],)
    ).fetchone()["note"] == "не трогать"
    assert conn.execute(
        "SELECT text FROM reminders WHERE id = ?", (reminder["id"],)
    ).fetchone()["text"] == "позже"
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "accounts", "leads", "reminders", "dialogs", "messages",
        "notes", "tasks", "property_defs", "property_values",
    } <= names
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert cols == {"id", "lead_id", "title", "done_at", "created_at", "updated_at"}


def test_card_note_task_and_property(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        lead = client.post("/api/leads", json={"name": "Иван", "note": "старая"}).json()
        card = client.get(f"/api/leads/{lead['id']}/card").json()
        assert card["note"] == "старая"
        assert card["notes"][0]["body"] == "старая"
        assert card["notes"][0]["id"] is None
        assert card["notes"][0]["created_at"]
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0

        missing = client.post("/api/leads/999999/notes", json={"body": "нет"})
        assert missing.status_code == 404
        empty = client.post(f"/api/leads/{lead['id']}/notes", json={"body": "  "})
        assert empty.status_code == 400

        first = client.post(
            f"/api/leads/{lead['id']}/notes", json={"body": "  новая  "}
        ).json()
        second = client.post(
            f"/api/leads/{lead['id']}/notes", json={"body": "ещё"}
        ).json()
        assert first["created_at"] and second["created_at"]
        card = client.get(f"/api/leads/{lead['id']}/card").json()
        assert [row["body"] for row in card["notes"]] == ["новая", "ещё"]
        assert all(row["created_at"] for row in card["notes"])
        assert card["note"] == "старая"
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2

        denied = client.post(
            f"/api/leads/{lead['id']}/tasks",
            json={"title": "позвонить", "due_at": "2099-01-01T00:00:00"},
        )
        assert denied.status_code == 422
        task = client.post(
            f"/api/leads/{lead['id']}/tasks", json={"title": " позвонить "}
        ).json()
        assert task["title"] == "позвонить"
        assert task["done_at"] is None
        assert "due_at" not in task
        done = client.post(f"/api/tasks/{task['id']}/done", json={"done": True}).json()
        assert done["done_at"]
        assert client.get("/api/reminders").json() == []

        for kind in ("", "   ", "date"):
            rejected = client.post("/api/property-defs", json={"name": "Город", "type": kind})
            assert rejected.status_code == 400
        prop = client.post(
            "/api/property-defs", json={"name": "Город", "type": "text"}
        ).json()
        assert prop["type"] == "text"
        saved = client.put(
            f"/api/leads/{lead['id']}/properties/{prop['id']}",
            json={"value": "Казань"},
        )
        assert saved.status_code == 200
        amount = client.post(
            "/api/property-defs", json={"name": "Сумма", "type": "amount"}
        ).json()
        client.put(
            f"/api/leads/{lead['id']}/properties/{amount['id']}",
            json={"value": "10.50"},
        )

        card = client.get(f"/api/leads/{lead['id']}/card").json()
        assert card["tasks"][0]["title"] == "позвонить"
        assert card["tasks"][0]["done_at"]
        assert "due_at" not in card["tasks"][0]
        city = next(row for row in card["properties"] if row["name"] == "Город")
        money = next(row for row in card["properties"] if row["name"] == "Сумма")
        assert city["type"] == "text"
        assert city["value_text"] == "Казань"
        assert money["type"] == "amount"
        assert money["value_text"] == "10.50"
        assert money["value_int"] == 1050
