from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway


def test_landing_is_not_the_desk(tmp_path):
    db = connect(tmp_path / "crm.db")
    init_db(db)
    app = create_app(conn=db, gateway=FakeGateway())
    with TestClient(app) as client:
        desk = client.get("/")
        page = client.get("/landing")
        shot = client.get("/static/landing-desk.png")
    assert desk.status_code == 200
    assert page.status_code == 200
    assert shot.status_code == 200
    assert shot.headers["content-type"].startswith("image/")
    assert "Войти через бота" in desk.text
    assert "Один стол для своих чатов" not in desk.text
    text = page.text
    assert "Один стол для своих чатов" in text
    assert "Открыть стол" in text
    assert 'href="/"' in text
    assert "/static/landing-desk.png" in text
    lowered = text.lower()
    assert "crmchat" not in lowered
    assert "crm chat" not in lowered
    assert "hintsflow" not in lowered
