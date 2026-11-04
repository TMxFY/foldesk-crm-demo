"""Local uvicorn app for e2e. FakeGateway only: no Telegram network."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect, init_db
from app.main import create_app
from app.telegram_gateway import FakeGateway

DIALOGS = [
    {"telegram_user_id": 42, "username": "ivan", "name": "Иван Петров"},
    {"telegram_user_id": 43, "username": "olga", "name": "Ольга"},
    {"telegram_user_id": 44, "username": "emile", "name": "Кафе Émile 😀"},
]

TMP = Path(__file__).resolve().parent / ".tmp"
TMP.mkdir(exist_ok=True)
DB_PATH = TMP / "crm.db"


class E2EGateway(FakeGateway):
    def __init__(self):
        super().__init__(
            code="11111",
            password="secret",
            needs_password=False,
            dialogs=DIALOGS,
        )

    async def request_code(self, phone: str, session_path: str) -> str:
        # +79990000002 asks for 2FA. Any other phone connects after the code.
        self.needs_password = (phone or "").strip().endswith("0002")
        return await super().request_code(phone, session_path)


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    init_db(conn)
    gateway = E2EGateway()
    app = create_app(conn=conn, gateway=gateway)

    @app.post("/api/test/reset")
    def reset():
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM dialogs")
        conn.execute("DELETE FROM reminders")
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM accounts")
        conn.commit()
        gateway._pending.clear()
        gateway.needs_password = False
        return {"ok": True}

    return app


app = build()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ["PORT"])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
