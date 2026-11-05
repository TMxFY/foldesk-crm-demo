import os
from pathlib import Path
import socket
import subprocess

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.snippets import render_snippet

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_snippets_schema_and_default_seeding(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)

    cols = [row["name"] for row in conn.execute("PRAGMA table_info(snippets)")]
    assert cols == ["id", "shortcut", "title", "body", "created_at"]

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        res = client.get("/api/snippets")
        assert res.status_code == 200
        items = res.json()
        shortcuts = [item["shortcut"] for item in items]
        assert "/price" in shortcuts
        assert "/cases" in shortcuts
        assert "/call" in shortcuts
        for item in items:
            assert "{name}" in item["body"]
            assert "{company}" in item["body"]

        # Drop table and re-init -> defaults re-seeded
        conn.execute("DROP TABLE snippets")
        conn.commit()
        init_db(conn)
        reseeded = client.get("/api/snippets").json()
        assert {item["shortcut"] for item in reseeded} >= {"/price", "/cases", "/call"}


def test_snippets_crud_and_validation_edge_cases(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        initial = client.get("/api/snippets").json()
        assert len(initial) >= 3

        # 1. Create snippet without leading slash -> normalized to /demo
        created_res = client.post(
            "/api/snippets",
            json={
                "shortcut": "  DEMO  ",
                "title": "  Демо-сессия  ",
                "body": "  Привет, {name}! Покажу демо для {company}.  ",
            },
        )
        assert created_res.status_code == 200
        created = created_res.json()
        assert created["shortcut"] == "/demo"
        assert created["title"] == "Демо-сессия"
        assert created["body"] == "Привет, {name}! Покажу демо для {company}."
        assert created["id"] > 0
        assert created["created_at"]

        # 2. Duplicate shortcut rejected with 400 (case-insensitive)
        dup_res = client.post(
            "/api/snippets",
            json={
                "shortcut": "/demo",
                "title": "Дубль",
                "body": "Текст",
            },
        )
        assert dup_res.status_code == 400

        # 3. Invalid inputs rejected with 400
        for bad_payload in (
            {"shortcut": "", "title": "Тест", "body": "Текст"},
            {"shortcut": "   ", "title": "Тест", "body": "Текст"},
            {"shortcut": "/", "title": "Тест", "body": "Текст"},
            {"shortcut": "/with space", "title": "Тест", "body": "Текст"},
            {"shortcut": "/valid", "title": "   ", "body": "Текст"},
            {"shortcut": "/valid", "title": "Тест", "body": "   "},
        ):
            bad_res = client.post("/api/snippets", json=bad_payload)
            assert bad_res.status_code == 400

        # 4. Delete existing snippet
        del_res = client.delete(f"/api/snippets/{created['id']}")
        assert del_res.status_code == 200
        assert del_res.json() == {"deleted": True, "id": created["id"]}

        # 5. Delete already-deleted or non-existent snippet -> 404
        assert client.delete(f"/api/snippets/{created['id']}").status_code == 404
        assert client.delete("/api/snippets/999999").status_code == 404

        # 6. Delete all snippets -> should stay empty on re-init, and re-seed on /api/demo/seed
        remaining = client.get("/api/snippets").json()
        for item in remaining:
            assert client.delete(f"/api/snippets/{item['id']}").status_code == 200
        assert client.get("/api/snippets").json() == []

        init_db(conn)
        assert client.get("/api/snippets").json() == []

        seed_res = client.post("/api/demo/seed")
        assert seed_res.status_code == 200
        after_seed = client.get("/api/snippets").json()
        assert {item["shortcut"] for item in after_seed} >= {"/price", "/cases", "/call"}


def test_render_snippet_variable_substitution():
    template = "Привет, {name}! Предложение для {company}."

    # Lead with 'Name · Company' format
    rendered_dot = render_snippet(
        template,
        {"name": "Алексей Соколов · TON Labs"},
    )
    assert rendered_dot == "Привет, Алексей Соколов! Предложение для TON Labs."

    # Lead with custom property 'Компания'
    rendered_prop = render_snippet(
        template,
        {
            "name": "Елена Романова",
            "properties": [{"name": "Компания", "key": "компания", "value_text": "Studio 42"}],
        },
    )
    assert rendered_prop == "Привет, Елена Романова! Предложение для Studio 42."

    # Explicit overrides
    rendered_explicit = render_snippet(
        template,
        {"name": "Иван"},
        name="Дмитрий",
        company="FinBridge",
    )
    assert rendered_explicit == "Привет, Дмитрий! Предложение для FinBridge."

    # Empty lead fallback
    rendered_fallback = render_snippet(template, None)
    assert rendered_fallback == "Привет, коллега! Предложение для вашей компании."

    # Dialog-style dict with 'title' and case-insensitive placeholders + special chars
    rendered_dialog = render_snippet(
        "Привет, {NAME}! План для {Company}.",
        {"title": r"Пётр \1 · Cash$&Co"},
    )
    assert rendered_dialog == r"Привет, Пётр \1! План для Cash$&Co."


def test_snippets_ui_markers_and_playwright_e2e(tmp_path):
    conn = connect(tmp_path / "crm_ui.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        assert 'id="side-snippets-panel"' in html
        assert 'id="side-snippets-list"' in html
        assert 'id="reply-snippets-bar"' in html
        assert 'id="reply-snippets-list"' in html
        assert 'id="snippet-create-form"' in html
        assert 'data-snippet-shortcut="/price"' in html
        assert 'data-snippet-shortcut="/cases"' in html
        assert 'data-snippet-shortcut="/call"' in html

    env = dict(os.environ)
    env["E2E_PORT"] = str(_free_port())
    proc = subprocess.run(
        ["npx", "playwright", "test", "f23-v4-block2-snippets.spec.ts"],
        cwd=str(ROOT / "e2e"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"Playwright failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
