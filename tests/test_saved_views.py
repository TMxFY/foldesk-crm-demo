from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


def _folder(conn):
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at)
        VALUES ('+70000000001', 'data/sessions/views.session', 'connected', '2020-01-01T00:00:00')
        """
    )
    account_id = conn.execute("SELECT id FROM accounts").fetchone()[0]
    conn.execute(
        """
        INSERT INTO folders (
            account_id, telegram_folder_id, title, sync_enabled, created_at, updated_at
        ) VALUES (?, 7, 'Клиенты', 0, '2020-01-01T00:00:00', '2020-01-01T00:00:00')
        """,
        (account_id,),
    )
    conn.commit()
    folder_id = conn.execute("SELECT id FROM folders").fetchone()[0]
    return account_id, folder_id


def test_saved_views_save_apply_and_reset(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    conn.execute("DROP TABLE saved_views")
    conn.commit()
    init_db(conn)
    cols = [
        row["name"]
        for row in conn.execute("PRAGMA table_info(saved_views)")
    ]
    assert cols == ["id", "name", "status", "folder_id", "created_at"]
    account_id, folder_id = _folder(conn)
    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Сохранить вид" in page.text
        assert 'id="saved-view"' in page.text
        assert ">Сбросить<" in page.text
        assert "Save for everyone" not in page.text
        assert "для всех" not in page.text

        anya = client.post("/api/leads", json={
            "name": "Аня",
            "account_id": account_id,
            "status": "waiting",
        }).json()
        boris = client.post("/api/leads", json={
            "name": "Борис",
            "account_id": account_id,
            "status": "talking",
        }).json()
        assert client.patch(
            f"/api/leads/{anya['id']}", json={"folder_id": folder_id}
        ).status_code == 200
        assert client.patch(
            f"/api/leads/{boris['id']}", json={"folder_id": folder_id}
        ).status_code == 200
        vera = client.post("/api/leads", json={"name": "Вера", "status": "new"}).json()

        waiting = client.get("/api/leads", params={
            "status": "waiting",
            "folder_id": folder_id,
        }).json()
        assert [row["id"] for row in waiting] == [anya["id"]]

        empty = client.post("/api/views", json={"name": "   ", "status": "waiting"})
        assert empty.status_code == 400
        assert client.get("/api/views").json() == []

        saved = client.post("/api/views", json={
            "name": "Ждём клиентов",
            "status": "waiting",
            "folder_id": folder_id,
        })
        assert saved.status_code == 200
        view = saved.json()
        assert view["name"] == "Ждём клиентов"
        assert view["status"] == "waiting"
        assert view["folder_id"] == folder_id
        applied = client.get("/api/leads", params={
            "status": view["status"],
            "folder_id": view["folder_id"],
        }).json()
        assert [row["name"] for row in applied] == ["Аня"]

        again = client.post("/api/views", json={
            "name": "  Ждём клиентов  ",
            "status": "talking",
            "folder_id": folder_id,
        })
        assert again.status_code == 200
        updated = again.json()
        assert updated["id"] == view["id"]
        assert updated["created_at"] == view["created_at"]
        assert updated["status"] == "talking"
        listed = client.get("/api/views").json()
        assert len(listed) == 1
        assert listed[0]["id"] == view["id"]

        other = client.post("/api/views", json={"name": "Все новые", "status": "new"})
        assert other.status_code == 200
        assert len(client.get("/api/views").json()) == 2
        assert client.post("/api/views", json={"name": "Нет", "status": "nope"}).status_code == 400
        assert client.post(
            "/api/views", json={"name": "Чужая", "folder_id": 999999}
        ).status_code == 400

        talking = client.get("/api/leads", params={
            "status": updated["status"],
            "folder_id": updated["folder_id"],
        }).json()
        assert [row["name"] for row in talking] == ["Борис"]
        reset = client.get("/api/leads").json()
        assert {row["name"] for row in reset} == {"Аня", "Борис", "Вера"}
        assert vera["id"] in {row["id"] for row in reset}
