import asyncio
import hashlib
import hmac
import json
import urllib.parse
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app import crm_auth
from app.bot_login import handle_bot_reply_to_lead
from app.db import connect, init_db, utc_now
from app.digest import (
    apply_digest_callback,
    format_lead_reply_notification,
    send_lead_reply_notification,
)
from app.leads import create_lead
from app.main import create_app
from app.telegram_gateway import FakeGateway


class _FakeTelegramResp:
    def __init__(self, status=200, message_id=777):
        self.status = status
        self._body = json.dumps(
            {"ok": True, "result": {"message_id": message_id}}
        ).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _build_signed_init_data(bot_token: str, user_dict: dict, auth_date: int = 1794614400) -> str:
    params = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, ensure_ascii=False, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = sig
    return urllib.parse.urlencode(params)


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "crm_v6_test.db")
    init_db(db)
    return db


def _seed_connected_account_and_dialog(conn):
    now = utc_now()
    cur_acc = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_username, telegram_name, session_path, status, health, created_at, updated_at
        ) VALUES ('+79991112233', 'saabath', 'Sasha Manager', 'data/sessions/demo.session', 'connected', 'active', ?, ?)
        """,
        (now, now),
    )
    acc_id = int(cur_acc.lastrowid)
    lead = create_lead(
        conn,
        name="Phoenix Client",
        username="phoenixIam",
        telegram_user_id=505050,
        status="talking",
        account_id=acc_id,
    )
    cur_dlg = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 505050, 'phoenixIam', 'Phoenix Client', 1, ?, 'Привет, сколько стоит?', 1, ?, ?, ?)
        """,
        (acc_id, now, lead["id"], now, now),
    )
    dlg_id = int(cur_dlg.lastrowid)
    conn.commit()
    return acc_id, lead["id"], dlg_id


def test_format_and_send_lead_reply_notification_with_preview_and_buttons(conn, monkeypatch):
    # 1. Without preview: exact backward-compatible string
    base_text = format_lead_reply_notification("@phoenixIam", "@saabath")
    assert base_text == "You received a response from @phoenixIam\n\nAccount: @saabath"

    # 2. With preview: preserves base string and appends preview + reply hint
    rich_text = format_lead_reply_notification(
        "@phoenixIam",
        "@saabath",
        preview="Привет, сколько стоит пакет на 30 рилсов?",
        lead_name="Phoenix Client",
    )
    assert rich_text.startswith("You received a response from @phoenixIam\n\nAccount: @saabath")
    assert "\n💬 Привет, сколько стоит пакет на 30 рилсов?\n\n↩️ Сделайте Reply на это сообщение, чтобы ответить клиенту прямо из бота." in rich_text

    # 3. send_lead_reply_notification stores bot_reply_notifications and sends inline keyboard
    monkeypatch.setenv("BOT_TOKEN", "bot-token-v6")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    acc_id, lead_id, dlg_id = _seed_connected_account_and_dialog(conn)

    sent_payloads = []

    def fake_opener(req, timeout=15):
        sent_payloads.append(json.loads(req.data.decode("utf-8")))
        return _FakeTelegramResp(status=200, message_id=4321)

    ok = send_lead_reply_notification(
        conn,
        lead_handle="@phoenixIam",
        account_handle="@saabath",
        preview="Привет, сколько стоит?",
        dialog_id=dlg_id,
        lead_id=lead_id,
        account_id=acc_id,
        peer_id=505050,
        crm_url="https://crm.softugc.com",
        opener=fake_opener,
    )
    assert ok is True
    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert "💬 Привет, сколько стоит?" in payload["text"]
    kb = payload["reply_markup"]["inline_keyboard"][0]
    assert {"text": "✍️ Ответить", "callback_data": f"reply_dlg:{dlg_id}"} in kb
    assert {
        "text": "📱 Открыть CRM",
        "web_app": {"url": "https://crm.softugc.com/?tg_miniapp=1#inbox"},
    } in kb

    saved = conn.execute(
        "SELECT * FROM bot_reply_notifications WHERE chat_id = '978069541' AND bot_message_id = 4321"
    ).fetchone()
    assert saved is not None
    assert int(saved["dialog_id"]) == dlg_id
    assert int(saved["lead_id"]) == lead_id
    assert int(saved["account_id"]) == acc_id
    assert int(saved["peer_id"]) == 505050


def test_handle_bot_reply_to_lead_via_swipe_and_fallback_and_callback(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "bot-token-v6")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    acc_id, lead_id, dlg_id = _seed_connected_account_and_dialog(conn)
    gateway = FakeGateway()

    bot_outbox = []

    def fake_opener(req, timeout=15):
        bot_outbox.append(json.loads(req.data.decode("utf-8")))
        return _FakeTelegramResp(status=200, message_id=5555)

    # Send notification so message_id=5555 is recorded in bot_reply_notifications
    send_lead_reply_notification(
        conn,
        lead_handle="@phoenixIam",
        account_handle="@saabath",
        preview="Привет, сколько стоит?",
        dialog_id=dlg_id,
        lead_id=lead_id,
        account_id=acc_id,
        peer_id=505050,
        opener=fake_opener,
    )
    bot_outbox.clear()

    # Case A1: Operator swipes Reply on message_id=5555
    update_swipe = {
        "update_id": 101,
        "message": {
            "message_id": 5556,
            "chat": {"id": 978069541, "type": "private"},
            "text": "Добрый день! Отправляю КП и примеры работ.",
            "reply_to_message": {
                "message_id": 5555,
                "text": "You received a response from @phoenixIam\n\nAccount: @saabath",
            },
        },
    }
    res_a1 = asyncio.run(
        handle_bot_reply_to_lead(conn, update_swipe, gateway=gateway, opener=fake_opener)
    )
    assert res_a1["ok"] is True
    assert res_a1["replied"] is True
    assert res_a1["dialog_id"] == dlg_id
    assert res_a1["message"]["text"] == "Добрый день! Отправляю КП и примеры работ."
    assert len(bot_outbox) == 1
    assert "✅ Ответ отправлен для" in bot_outbox[0]["text"]

    # Case A2: Fallback text parsing when bot_message_id is NOT in bot_reply_notifications
    bot_outbox.clear()
    update_fallback = {
        "update_id": 102,
        "message": {
            "message_id": 6001,
            "chat": {"id": 978069541, "type": "private"},
            "text": "Удобно созвониться завтра в 14:00?",
            "reply_to_message": {
                "message_id": 999999,
                "text": "You received a response from @phoenixIam\n\nAccount: @saabath\n💬 Привет",
            },
        },
    }
    res_a2 = asyncio.run(
        handle_bot_reply_to_lead(conn, update_fallback, gateway=gateway, opener=fake_opener)
    )
    assert res_a2["ok"] is True
    assert res_a2["replied"] is True
    assert res_a2["dialog_id"] == dlg_id

    # Case B: Inline button callback reply_dlg:{dlg_id} -> next text message replies and resets active state
    cb_res = apply_digest_callback(conn, f"reply_dlg:{dlg_id}", chat_id="978069541")
    assert cb_res == {"ok": True, "action": "reply_prompt", "dialog_id": dlg_id}

    bot_outbox.clear()
    update_plain = {
        "update_id": 103,
        "message": {
            "message_id": 6002,
            "chat": {"id": 978069541, "type": "private"},
            "text": "Отправил ссылку на Zoom!",
        },
    }
    res_b = asyncio.run(
        handle_bot_reply_to_lead(conn, update_plain, gateway=gateway, opener=fake_opener)
    )
    assert res_b["ok"] is True
    assert res_b["replied"] is True
    assert res_b["dialog_id"] == dlg_id

    # Subsequent plain message after active reply dialog is cleared should NOT send another reply
    res_b_after = asyncio.run(
        handle_bot_reply_to_lead(conn, update_plain, gateway=gateway, opener=fake_opener)
    )
    assert res_b_after["replied"] is False


def test_telegram_miniapp_init_data_auth_and_single_password_login(tmp_path, monkeypatch):
    bot_token = "888777:AAH_test_miniapp_secret_token"
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("BOT_TOKEN", bot_token)
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "TimSecretPass#2026")
    monkeypatch.setenv("CRM_VLAD_PASSWORD", "VladSecretPass#2026")
    monkeypatch.setenv("CRM_QUICK_PIN", "2580")

    db = connect(tmp_path / "miniapp_auth.db")
    init_db(db)
    app = create_app(conn=db, gateway=FakeGateway())
    client = TestClient(app, follow_redirects=False)

    # 1. Verify HMAC helper directly
    vlad_init_data = _build_signed_init_data(
        bot_token, {"id": 978069541, "first_name": "Vlad", "username": "vlad_sales"}
    )
    parsed_user = crm_auth.verify_telegram_webapp_init_data(vlad_init_data, bot_token)
    assert parsed_user is not None
    assert parsed_user["id"] == 978069541
    assert crm_auth.verify_telegram_webapp_init_data(vlad_init_data + "tampered", bot_token) is None

    # 2. POST /api/auth/webapp logs Vlad in automatically under CRM_REQUIRE_AUTH=1
    r_webapp = client.post(
        "/api/auth/webapp",
        json={"init_data": vlad_init_data},
        headers={"User-Agent": "Mozilla/5.0 TelegramWebApp"},
    )
    assert r_webapp.status_code == 200
    body_wa = r_webapp.json()
    assert body_wa["ok"] is True
    assert body_wa["authenticated"] is True
    assert body_wa["user"]["username"] == "vlad"
    assert crm_auth.AUTH_COOKIE_NAME in r_webapp.cookies

    # 3. Stranger not in team whitelist is rejected with 401
    stranger_init_data = _build_signed_init_data(
        bot_token, {"id": 123456789, "first_name": "Stranger"}
    )
    r_stranger = client.post(
        "/api/auth/webapp",
        json={"init_data": stranger_init_data},
        headers={"User-Agent": "Mozilla/5.0 TelegramWebApp"},
    )
    assert r_stranger.status_code == 401

    # 4. Single-field password login (username="" or omitted) matches Vlad, Tim, and CRM_QUICK_PIN
    r_vlad_pwd = client.post(
        "/api/auth/login",
        json={"password": "VladSecretPass#2026"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_vlad_pwd.status_code == 200
    assert r_vlad_pwd.json()["user"]["username"] == "vlad"

    r_pin = client.post(
        "/api/auth/login",
        json={"username": "", "password": "2580"},
        headers={"User-Agent": "Mozilla/5.0"},
    )
    assert r_pin.status_code == 200
    assert r_pin.json()["user"]["username"] == "tim"

    # 5. Check minimalist /login HTML structure
    r_login_page = client.get("/login", headers={"User-Agent": "Mozilla/5.0"})
    assert r_login_page.status_code == 200
    html = r_login_page.text
    assert "https://telegram.org/js/telegram-web-app.js" in html
    assert "/api/auth/webapp" in html
    assert 'placeholder="Введите пароль или PIN доступа"' in html
    assert 'class="login-advanced-toggle"' in html
    assert "Войти по логину сотрудника" in html
