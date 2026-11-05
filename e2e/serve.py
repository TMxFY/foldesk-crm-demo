"""Local uvicorn app for e2e. FakeGateway only: no Telegram network."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect, init_db
from app.main import create_app, stop_inbox_watches
from app.telegram_gateway import FakeGateway

DIALOGS = [
    {"telegram_user_id": 42, "username": "ivan", "name": "Иван Петров"},
    {"telegram_user_id": 43, "username": "olga", "name": "Ольга"},
    {"telegram_user_id": 44, "username": "emile", "name": "Кафе Émile 😀"},
    {"telegram_user_id": 45, "username": "petr", "name": "Пётр"},
]

PETR_HISTORY = [
    {
        "telegram_message_id": 9001,
        "text": "был в телеграме",
        "sent_at": "2020-01-01T00:00:00",
        "direction": "in",
    }
]


def seed_history(gateway) -> None:
    gateway.history.clear()
    gateway.history[45] = [dict(row) for row in PETR_HISTORY]

TMP = Path(__file__).resolve().parent / ".tmp"
TMP.mkdir(exist_ok=True)
DB_PATH = Path(
    os.environ.get("E2E_DB_PATH")
    or (TMP / f"crm_{os.environ.get('PORT', 'default')}.db")
)


class E2EGateway(FakeGateway):
    def __init__(self):
        super().__init__(
            code="11111",
            password="secret",
            needs_password=False,
            dialogs=DIALOGS,
        )
        seed_history(self)

    async def request_code(self, phone: str, session_path: str) -> str:
        # +79990000002 asks for 2FA. Any other phone connects after the code.
        self.needs_password = (phone or "").strip().endswith("0002")
        return await super().request_code(phone, session_path)


class DigestBox:
    def __init__(self):
        self.mode = "ok"

    def __call__(self, req, timeout=None):
        class Resp:
            def __init__(self, status, body):
                self.status = status
                self._body = body

            def read(self_inner):
                return self_inner._body

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

        if self.mode == "fail":
            return Resp(403, b'{"ok": false}')
        return Resp(200, b'{"ok": true, "result": {"message_id": 1}}')


digest_box = DigestBox()


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    init_db(conn)
    gateway = E2EGateway()
    # Fake operator bot. Real BOT_TOKEN from the parent shell must not be used.
    os.environ["BOT_TOKEN"] = "e2e-not-a-secret"
    os.environ["BOT_CHAT_ID"] = "900001"
    app = create_app(conn=conn, gateway=gateway)
    app.state.digest_opener = digest_box

    @app.post("/api/test/reset")
    async def reset():
        await stop_inbox_watches(app)
        with gateway._lock:
            gateway._listeners.clear()
            gateway._backlog.clear()
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM dialogs")
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM reminders")
        conn.execute("DELETE FROM lead_events")
        conn.execute("DELETE FROM property_values")
        conn.execute("DELETE FROM campaign_recipients")
        conn.execute("DELETE FROM campaign_steps")
        conn.execute("DELETE FROM campaigns")
        conn.execute("DELETE FROM folders")
        conn.execute("DELETE FROM saved_views")
        conn.execute("DELETE FROM snippets")
        conn.execute("DELETE FROM kv_settings WHERE key = 'snippets_user_modified'")
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM property_defs")
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM pipeline_stages")
        conn.commit()
        from app.db import _ensure_default_pipeline_stages
        from app.snippets import seed_default_snippets

        _ensure_default_pipeline_stages(conn)
        conn.commit()
        seed_default_snippets(conn)
        gateway._pending.clear()
        gateway.sent.clear()
        gateway.needs_password = False
        gateway.health = None
        gateway._message_seq = 0
        seed_history(gateway)
        digest_box.mode = "ok"
        return {"ok": True}

    @app.post("/api/test/digest-transport")
    async def digest_transport(body: dict):
        mode = body.get("mode")
        if mode not in ("ok", "fail"):
            return {"ok": False}
        digest_box.mode = mode
        return {"mode": mode}

    @app.post("/api/test/session-health")
    async def session_health(body: dict):
        health = body.get("health")
        gateway.health = health or None
        return {"health": gateway.health}

    return app


app = build()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ["PORT"])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
