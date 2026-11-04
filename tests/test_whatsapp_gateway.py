import asyncio
import re
from fastapi.testclient import TestClient

from app.db import connect, init_db, resolve_session
from app.main import create_app
from app.whatsapp_gateway import WhatsAppGateway


def test_whatsapp_login_pairing_code_and_qr_without_tg_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        # 1. Start WhatsApp login via Pairing Code
        started = client.post(
            "/api/accounts/start",
            json={
                "phone": "+79991112233",
                "provider": "whatsapp",
                "auth_mode": "code",
            },
        )
        assert started.status_code == 200
        data = started.json()
        acc_id = data["id"]
        assert data["provider"] == "whatsapp"
        assert data["status"] == "pending_code"
        assert re.match(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$", data["pairing_code"])
        assert data["qr_code"].startswith("data:image/svg+xml;utf8,")

        acc_row = client.get("/api/accounts").json()[0]
        assert acc_row["provider"] == "whatsapp"
        assert acc_row["connection_label"] == "Не подключён · ждёт Pairing-код / QR"

        # Confirm with generated pairing code (without dash also supported)
        confirmed = client.post(
            f"/api/accounts/{acc_id}/code",
            json={"code": data["pairing_code"].replace("-", "")},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "connected"

        connected_row = client.get("/api/accounts").json()[0]
        assert connected_row["status"] == "connected"
        assert connected_row["health"] == "active"
        assert connected_row["wa_jid"] == "79991112233@s.whatsapp.net"
        assert connected_row["connection_label"] == "Подключён · WhatsApp Web активен"

        # 2. Start WhatsApp login via QR mode (phone optional)
        started_qr = client.post(
            "/api/accounts/start",
            json={
                "phone": "",
                "provider": "whatsapp",
                "auth_mode": "qr",
            },
        )
        assert started_qr.status_code == 200
        qr_acc_id = started_qr.json()["id"]
        assert started_qr.json()["qr_code"].startswith("data:image/svg+xml;utf8,")

        confirmed_qr = client.post(
            f"/api/accounts/{qr_acc_id}/code",
            json={"code": "qr"},
        )
        assert confirmed_qr.status_code == 200
        assert confirmed_qr.json()["status"] == "connected"

        # 3. Search dialogs by phone on WhatsApp account and send message
        dlgs = client.get(f"/api/accounts/{acc_id}/dialogs", params={"q": "+79005554433"})
        assert dlgs.status_code == 200
        found = dlgs.json()
        assert len(found) == 1
        dialog_id = found[0]["dialog_id"]

        sent = client.post(
            f"/api/dialogs/{dialog_id}/messages",
            json={"text": "Привет в WhatsApp!"},
        )
        assert sent.status_code == 200
        assert sent.json()["text"] == "Привет в WhatsApp!"


def test_whatsapp_session_import_and_health(tmp_path):
    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app) as client:
        res = client.post(
            "/api/accounts/import-session",
            json={
                "provider": "whatsapp",
                "sessions": [
                    {
                        "filename": "wa_79998887766.session",
                        "phone": "+79998887766",
                        "name": "WA Sales #1",
                    }
                ],
            },
        )
        assert res.status_code == 200
        imported = res.json()["imported"]
        assert len(imported) == 1
        acc = imported[0]
        assert acc["provider"] == "whatsapp"
        assert acc["wa_jid"] == "79998887766@s.whatsapp.net"
        assert acc["status"] == "connected"
        assert acc["health"] == "active"

        reconn = client.post(f"/api/accounts/{acc['id']}/reconnect")
        assert reconn.status_code == 200
        assert reconn.json()["health"] == "active"


def test_whatsapp_gateway_methods(tmp_path):
    gw = WhatsAppGateway()
    sess_path = str(tmp_path / "wa_unit.session")

    async def run():
        h0 = await gw.session_health(sess_path)
        assert h0["health"] == "unauthorized"

        code_hash = await gw.request_code("+79995556677", sess_path, auth_mode="code")
        assert code_hash.startswith("wa:")
        h_pending = await gw.session_health(sess_path)
        assert h_pending["health"] == "unauthorized"

        pcode = gw.generate_pairing_code("+79995556677")
        res = await gw.confirm_code("+79995556677", pcode, code_hash, sess_path)
        assert res["status"] == "connected"
        assert res["wa_jid"] == "79995556677@s.whatsapp.net"
        assert resolve_session(sess_path).exists()

        h1 = await gw.session_health(sess_path)
        assert h1["health"] == "active"

        msg = await gw.send_message(sess_path, 79001112233, "Тест WA", simulate_typing=True)
        assert msg["external_message_id"].startswith("3EB0")
        assert msg["presence"] == "composing"
        assert len(gw.presence_log) == 1

        checked = await gw.check_numbers(sess_path, ["+7 (900) 111-22-33"])
        assert len(checked) == 1
        assert checked[0]["found"] is True
        assert checked[0]["wa_jid"] == "79001112233@s.whatsapp.net"

    asyncio.run(run())
