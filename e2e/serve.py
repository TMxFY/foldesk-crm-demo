"""Local uvicorn app for e2e. FakeGateway only: no Telegram network."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Fake operator bot & disabled cloud/tg watchers must be set before importing app.main.
os.environ["BOT_TOKEN"] = "e2e-not-a-secret"
os.environ["BOT_CHAT_ID"] = "900001"
os.environ["CLOUD_BACKUP_ENABLED"] = "0"
os.environ["DISABLE_TG_WATCHES"] = "1"

from app.db import connect, init_db, resolve_session
from app.main import create_app, stop_inbox_watches
from app.telegram_gateway import FakeGateway, GatewayError
from app.whatsapp_gateway import WhatsAppGateway, _render_real_qr_svg

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


class E2EWhatsAppGateway(WhatsAppGateway):
    """Isolated Playwright E2E double kept strictly out of production app/."""

    async def request_code(
        self, phone: str, session_path: str, auth_mode: str = "code"
    ) -> str:
        clean_phone = (phone or "").strip() or "+79995554433"
        pcode = "WA00-9911"
        qr_svg = _render_real_qr_svg(f"e2e:{session_path}:{clean_phone}")
        h = f"wa:{auth_mode}:{pcode}"
        self._pending[session_path] = {
            "phone": clean_phone,
            "hash": h,
            "pairing_code": pcode,
            "qr_code": qr_svg,
            "auth_mode": auth_mode,
            "state": "pending_code",
        }
        return h

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        import re as _re

        clean_phone = (phone or "").strip() or "+79995554433"
        digits = _re.sub(r"\D", "", clean_phone)[-15:] or "79995554433"
        if not (code or "").strip():
            raise GatewayError("Укажите код")
        self._pending[session_path] = {
            "phone": f"+{digits}",
            "state": "connected",
            "wa_jid": f"{digits}@s.whatsapp.net",
        }
        return {
            "status": "connected",
            "phone": f"+{digits}",
            "telegram_user_id": int(digits),
            "telegram_username": None,
            "telegram_name": f"WhatsApp +{digits}",
            "wa_jid": f"{digits}@s.whatsapp.net",
        }

    async def session_health(self, session_path: str) -> dict:
        if (self._pending.get(session_path) or {}).get("state") == "connected":
            return {"health": "active", "last_error": None}
        return await super().session_health(session_path)

    async def check_numbers(self, session_path: str, phones: list[str]) -> list[dict]:
        import re as _re

        out = []
        for raw in phones or []:
            d = _re.sub(r"\D", "", str(raw or ""))[-15:]
            if len(d) >= 7:
                out.append(
                    {
                        "phone": f"+{d}",
                        "found": True,
                        "wa_jid": f"{d}@s.whatsapp.net",
                        "telegram_user_id": int(d),
                        "username": None,
                        "name": f"WA +{d}",
                    }
                )
        return out

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        import re as _re

        found = await super().search_dialogs(session_path, query, limit=limit)
        if found:
            return found
        d = _re.sub(r"\D", "", str(query or ""))[-15:]
        if len(d) >= 7:
            return [
                {
                    "telegram_user_id": int(d),
                    "username": None,
                    "name": f"WA +{d}",
                    "phone": f"+{d}",
                    "wa_jid": f"{d}@s.whatsapp.net",
                    "peer_type": "user",
                }
            ]
        return []

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        simulate_typing: bool = True,
        image_path: str | None = None,
    ) -> dict:
        import hashlib as _hs
        import re as _re
        from app.db import utc_now

        d = _re.sub(r"\D", "", str(peer_id or ""))[-15:]
        peer_key = int(d) if d else peer_id
        seq_id = self._next_message_seq()
        ext_id = f"3EB0E2E{_hs.sha1(text.encode()).hexdigest()[:12].upper()}"
        rec = {
            "telegram_message_id": seq_id,
            "external_message_id": ext_id,
            "text": text,
            "sent_at": utc_now(),
            "direction": "out",
            "reply_to_telegram_message_id": int(reply_to) if reply_to else None,
            "presence": "composing" if simulate_typing else "available",
        }
        self.sent.append({"peer_id": peer_key, "text": text, "session_path": session_path})
        self.history.setdefault(peer_key, []).append(dict(rec))
        return rec


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)
    init_db(conn)
    gateway = E2EGateway()
    wa_gateway = E2EWhatsAppGateway(db_path=DB_PATH)
    # Fake operator bot. Real BOT_TOKEN from the parent shell must not be used.
    os.environ["BOT_TOKEN"] = "e2e-not-a-secret"
    os.environ["BOT_CHAT_ID"] = "900001"
    app = create_app(conn=conn, gateway=gateway, wa_gateway=wa_gateway)
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
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=1)
