from fastapi.testclient import TestClient
from telethon.tl.types import DialogFilter, DialogFilterChatlist

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import CHAT_TYPE_REASON, FakeGateway, _folder_block_reason

PEOPLE = [
    {"telegram_user_id": 11, "username": "anya", "name": "Аня", "peer_type": "user"},
    {"telegram_user_id": 12, "username": "boris", "name": "Борис", "peer_type": "user"},
    {"telegram_user_id": 13, "username": "vera", "name": "Вера", "peer_type": "user"},
    {"telegram_user_id": 900, "name": "Команда", "peer_type": "chat", "is_group": True},
    {"telegram_user_id": 901, "name": "Канал", "peer_type": "channel"},
]


def _account(conn):
    conn.execute(
        """
        INSERT INTO accounts (phone, session_path, status, created_at)
        VALUES ('+79990000000', 'data/sessions/folders.session', 'connected', '2020-01-01T00:00:00')
        """
    )
    conn.commit()
    return conn.execute("SELECT id FROM accounts").fetchone()[0]


def test_chat_type_folder_is_blocked():
    clean = DialogFilter(
        id=1, title="Клиенты", pinned_peers=[], include_peers=[], exclude_peers=[],
    )
    muted = DialogFilter(
        id=2,
        title="Тихие",
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        exclude_muted=True,
    )
    groups = DialogFilter(
        id=3,
        title="Группы",
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        groups=True,
    )
    contacts = DialogFilter(
        id=4,
        title="Контакты",
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        contacts=True,
    )
    invites = DialogFilterChatlist(
        id=5, title="Инвайт", pinned_peers=[], include_peers=[], has_my_invites=True,
    )
    assert _folder_block_reason(clean) is None
    assert _folder_block_reason(muted) is None
    assert _folder_block_reason(groups) == CHAT_TYPE_REASON
    assert _folder_block_reason(contacts) == CHAT_TYPE_REASON
    assert _folder_block_reason(invites) == CHAT_TYPE_REASON


def test_sync_own_folders_and_change_on_card(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "folders" in names
    lead_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    assert "folder_id" in lead_cols
    account_id = _account(conn)
    gateway = FakeGateway(
        folders=[
            {
                "telegram_folder_id": 2,
                "title": "Клиенты",
                "peers": PEOPLE,
            },
            {
                "telegram_folder_id": 3,
                "title": "Архив",
                "peers": [],
            },
            {
                "telegram_folder_id": 4,
                "title": "Все контакты",
                "blocked_reason": CHAT_TYPE_REASON,
                "peers": [
                    {
                        "telegram_user_id": 77,
                        "username": "extra",
                        "name": "Лишний",
                        "peer_type": "user",
                    }
                ],
            },
        ]
    )
    app = create_app(conn=conn, gateway=gateway)
    with TestClient(app) as client:
        pulled = client.post("/api/folders/pull", json={"account_id": account_id})
        assert pulled.status_code == 200
        rows = pulled.json()
        assert {row["title"] for row in rows} == {"Клиенты", "Архив", "Все контакты"}
        clients = next(row for row in rows if row["title"] == "Клиенты")
        archive = next(row for row in rows if row["title"] == "Архив")
        dirty = next(row for row in rows if row["title"] == "Все контакты")
        assert dirty["blocked_reason"] == CHAT_TYPE_REASON
        assert dirty["sync_enabled"] == 0

        denied = client.post(f"/api/folders/{dirty['id']}/sync")
        assert denied.status_code == 400
        assert denied.json()["detail"] == CHAT_TYPE_REASON
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0

        first = client.post(f"/api/folders/{clients['id']}/sync")
        assert first.status_code == 200
        assert first.json()["added"] == 3
        leads = client.get("/api/leads").json()
        assert {lead["name"] for lead in leads} == {"Аня", "Борис", "Вера"}
        assert {lead["folder_id"] for lead in leads} == {clients["id"]}
        assert {lead["telegram_user_id"] for lead in leads} == {11, 12, 13}
        ids = sorted(lead["id"] for lead in leads)

        again = client.post(f"/api/folders/{clients['id']}/sync")
        assert again.status_code == 200
        assert again.json()["added"] == 0
        assert again.json()["lead_ids"] == ids or sorted(again.json()["lead_ids"]) == ids
        listed = client.get("/api/leads").json()
        assert sorted(lead["id"] for lead in listed) == ids
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3

        lead_id = ids[0]
        card = client.get(f"/api/leads/{lead_id}/card").json()
        assert card["folder_name"] == "Клиенты"
        assert card["folder_id"] == clients["id"]

        moved = client.patch(f"/api/leads/{lead_id}", json={"folder_id": archive["id"]})
        assert moved.status_code == 200
        assert moved.json()["folder_id"] == archive["id"]
        card = client.get(f"/api/leads/{lead_id}/card").json()
        assert card["folder_name"] == "Архив"
        assert card["folder_id"] == archive["id"]

        cleared = client.patch(f"/api/leads/{lead_id}", json={"folder_id": None})
        assert cleared.status_code == 200
        assert cleared.json()["folder_id"] is None
        card = client.get(f"/api/leads/{lead_id}/card").json()
        assert card["folder_id"] is None
        assert card["folder_name"] is None

        conn.execute(
            """
            INSERT INTO accounts (phone, session_path, status, created_at)
            VALUES ('+79991111111', 'data/sessions/other.session', 'connected', '2020-01-01T00:00:00')
            """
        )
        conn.commit()
        other_id = conn.execute(
            "SELECT id FROM accounts WHERE phone = '+79991111111'"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO folders (
                account_id, telegram_folder_id, title, sync_enabled,
                created_at, updated_at
            ) VALUES (?, 9, 'Чужая', 0, '2020-01-01T00:00:00', '2020-01-01T00:00:00')
            """,
            (other_id,),
        )
        conn.commit()
        foreign_id = conn.execute(
            "SELECT id FROM folders WHERE title = 'Чужая'"
        ).fetchone()[0]
        foreign = client.patch(f"/api/leads/{lead_id}", json={"folder_id": foreign_id})
        assert foreign.status_code == 400
