import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from app.db import connect, init_db, utc_now
from app.digest import (
    format_lead_reply_notification,
    resolve_colleague_bot_credentials,
    send_lead_reply_notification,
)
from app.inbox import apply_incoming, stop_outreach_on_reply, sync_inbox
from app.leads import create_lead
from app.outreach import (
    _sync_campaign_replies,
    create_campaign,
    run_campaign_batch,
)
from app.telegram_gateway import FakeGateway


def _create_account(
    conn,
    phone="+79998887766",
    telegram_username=None,
    telegram_name=None,
    status="connected",
):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_username, telegram_name, session_path, status, health, created_at, updated_at
        ) VALUES (?, ?, ?, 'data/sessions/test.session', ?, 'active', ?, ?)
        """,
        (phone, telegram_username, telegram_name, status, now, now),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "phone": phone,
        "telegram_username": telegram_username,
        "telegram_name": telegram_name,
    }


def _add_recipient(
    conn,
    campaign_id,
    username=None,
    telegram_user_id=None,
    name=None,
    phone=None,
    lead_id=None,
    status="pending",
    current_step=0,
    last_sent_at=None,
):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, telegram_user_id, name, phone, lead_id,
            status, current_step, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            username,
            telegram_user_id,
            name,
            phone,
            lead_id,
            status,
            current_step,
            last_sent_at,
            now,
            now,
        ),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "campaign_id": campaign_id,
        "username": username,
        "telegram_user_id": telegram_user_id,
        "name": name,
    }


class _FakeResp:
    def __init__(self, status=200, body=b'{"ok": true, "result": {"message_id": 99}}'):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_mock_opener(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(req)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _parse_req(req):
    body = json.loads(req.data.decode("utf-8")) if req.data else {}
    return {
        "url": req.full_url,
        "method": req.get_method(),
        "chat_id": body.get("chat_id"),
        "text": body.get("text"),
        "raw": body,
    }


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "crm_notify_test.db")
    init_db(db)
    return db


def test_format_lead_reply_notification_exact_text():
    text = format_lead_reply_notification("@phoenixIam", "@saabath")
    assert text == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_send_lead_reply_notification_exact_call(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    ok = send_lead_reply_notification(
        conn,
        lead_handle="@phoenixIam",
        account_handle="@saabath",
    )
    assert ok is True
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["url"] == "https://api.telegram.org/botcolleague-token-123/sendMessage"
    assert info["chat_id"] == "colleague-chat-456"
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_notification_on_apply_incoming_with_active_lead(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
        telegram_name="Sasha",
    )
    lead = create_lead(
        conn,
        name="Phoenix Lead",
        username="phoenixIam",
        telegram_user_id=1001,
        status="waiting",
        account_id=acc["id"],
    )

    msg = {
        "telegram_user_id": 1001,
        "telegram_message_id": 501,
        "username": "phoenixIam",
        "name": "Phoenix Lead",
        "text": "Здравствуйте, расскажите подробнее!",
        "direction": "in",
    }
    dlg = apply_incoming(conn, acc["id"], msg)
    assert dlg["last_from_peer"] == 1

    # Lead should be transitioned to 'talking'
    lrow = conn.execute("SELECT status FROM leads WHERE id = ?", (lead["id"],)).fetchone()
    assert lrow["status"] == "talking"

    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_notification_on_apply_incoming_when_lead_id_is_null_and_recipient_replied(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    camp = create_campaign(
        conn,
        name="Cold Outreach",
        account_id=acc["id"],
        steps=[{"template_text": "Привет"}],
    )
    # Recipient without lead_id, already marked 'replied'
    rec = _add_recipient(
        conn,
        camp["id"],
        username="phoenixIam",
        telegram_user_id=2002,
        name="Phoenix Client",
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'replied', lead_id = NULL WHERE id = ?",
        (rec["id"],),
    )
    conn.commit()

    msg = {
        "telegram_user_id": 2002,
        "telegram_message_id": 601,
        "username": "phoenixIam",
        "name": "Phoenix Client",
        "text": "Да, я готов обсудить",
        "direction": "in",
    }
    apply_incoming(conn, acc["id"], msg)

    # Notification must be sent even though lead_id IS NULL and recipient was already 'replied'
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_notification_on_sync_inbox_and_no_duplicates_on_repeat(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    camp = create_campaign(
        conn,
        name="Camp 1",
        account_id=acc["id"],
        steps=[{"template_text": "Step 1"}],
    )
    rec = _add_recipient(
        conn,
        camp["id"],
        username="phoenixIam",
        telegram_user_id=3003,
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'in_progress', last_sent_at = '2026-11-12T10:00:00' WHERE id = ?",
        (rec["id"],),
    )
    conn.commit()

    inbox_payload = [
        {
            "telegram_user_id": 3003,
            "username": "phoenixIam",
            "name": "Phoenix",
            "last_from_peer": 1,
            "last_message_preview": "Мне интересно!",
            "last_message_at": "2026-11-12T10:05:00",
            "unread_count": 1,
        }
    ]

    # First sync_inbox call -> triggers notification
    sync_inbox(conn, acc["id"], inbox_payload)
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"

    rec_row = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec["id"],)).fetchone()
    assert rec_row["status"] == "replied"

    # Repeated sync_inbox with identical dialog -> MUST NOT send duplicate notification!
    sync_inbox(conn, acc["id"], inbox_payload)
    assert len(calls) == 1  # Still 1 call! No duplicate!


def test_notification_on_sync_campaign_replies_and_no_duplicates(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    camp = create_campaign(
        conn,
        name="Camp Replies",
        account_id=acc["id"],
        steps=[{"template_text": "Step 1"}],
    )
    rec = _add_recipient(
        conn,
        camp["id"],
        username="phoenixIam",
        telegram_user_id=4004,
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'in_progress', current_step = 1, last_sent_at = '2026-11-12T10:00:00' WHERE id = ?",
        (rec["id"],),
    )
    # Put a dialog with client reply in dialogs
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 4004, 'phoenixIam', 'Phoenix', 1, '2026-11-12T10:05:00', 'Отвечаю!', 1, '2026-11-12T10:00:00', '2026-11-12T10:05:00')
        """,
        (acc["id"],),
    )
    conn.commit()

    # First _sync_campaign_replies call
    n_replied = _sync_campaign_replies(conn, camp["id"], acc["id"])
    assert n_replied == 1
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"

    # Second _sync_campaign_replies call -> NO duplicate notification!
    n_repeat = _sync_campaign_replies(conn, camp["id"], acc["id"])
    assert n_repeat == 0
    assert len(calls) == 1  # Call count stays 1!


def test_notification_in_run_campaign_batch(conn, monkeypatch):
    import asyncio

    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
        status="connected",
    )
    camp = create_campaign(
        conn,
        name="Batch Camp",
        account_id=acc["id"],
        status="running",
        steps=[{"template_text": "Привет 1", "delay_hours": 0}, {"template_text": "Привет 2", "delay_hours": 24}],
    )
    rec = _add_recipient(
        conn,
        camp["id"],
        username="phoenixIam",
        telegram_user_id=5005,
    )
    # Recipient has already received step 1
    conn.execute(
        """
        UPDATE campaign_recipients
        SET status = 'in_progress', current_step = 1, last_sent_at = '2026-11-11T10:00:00',
            sent_account_id = ?
        WHERE id = ?
        """,
        (acc["id"], rec["id"]),
    )
    # Dialog in DB shows client replied
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 5005, 'phoenixIam', 'Phoenix', 1, '2026-11-12T09:00:00', 'Я ответил!', 1, '2026-11-11T10:00:00', '2026-11-12T09:00:00')
        """,
        (acc["id"],),
    )
    conn.commit()

    gateway = FakeGateway()
    res = asyncio.run(
        run_campaign_batch(
            conn,
            camp["id"],
            gateway=gateway,
            max_send=10,
        )
    )
    # Step 2 should not be sent because reply was detected
    assert res.get("sent", 0) == 0

    # Recipient marked replied
    rrow = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec["id"],)).fetchone()
    assert rrow["status"] == "replied"

    # Notification sent
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_pull_username_from_dialogs_when_not_passed(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    # Dialog has username 'phoenixIam'
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            created_at, updated_at
        ) VALUES (?, 'user', 6006, 'phoenixIam', 'Phoenix Person', 1, '2026-11-12T10:00:00', '2026-11-12T10:00:00')
        """,
        (acc["id"],),
    )
    conn.commit()

    # Call stop_outreach_on_reply without username, only peer_id and account_id
    stop_outreach_on_reply(
        conn,
        account_id=acc["id"],
        peer_id=6006,
        username=None,
        force_event=True,
    )
    assert len(calls) == 1
    info = _parse_req(calls[0])
    # Must pull @phoenixIam from dialogs table
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_fallbacks_when_no_username(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    # Subcase A: Lead has name but no username, Account has name but no username
    acc1 = _create_account(
        conn,
        phone="+79998881111",
        telegram_name="Александр Менеджер",
    )
    lead1 = create_lead(
        conn,
        name="Иван Руководитель",
        telegram_user_id=7001,
        status="waiting",
        account_id=acc1["id"],
    )
    stop_outreach_on_reply(
        conn,
        account_id=acc1["id"],
        peer_id=7001,
        lead_id=lead1["id"],
        force_event=True,
    )
    assert len(calls) == 1
    assert _parse_req(calls[0])["text"] == "You received a response from Иван Руководитель\n\nAccount: Александр Менеджер"

    # Subcase B: Only phones available (no usernames, no names)
    acc2 = _create_account(
        conn,
        phone="+79998882222",
    )
    lead2 = create_lead(
        conn,
        phone="+79990003333",
        status="waiting",
        account_id=acc2["id"],
    )
    stop_outreach_on_reply(
        conn,
        account_id=acc2["id"],
        phone="+79990003333",
        lead_id=lead2["id"],
        force_event=True,
    )
    assert len(calls) == 2
    assert _parse_req(calls[1])["text"] == "You received a response from +79990003333\n\nAccount: +79998882222"

    # Subcase C: Account with no phone/username/name -> falls back to #{id}
    acc3 = _create_account(
        conn,
        phone="",
    )
    lead3 = create_lead(
        conn,
        phone="+79990004444",
        status="waiting",
        account_id=acc3["id"],
    )
    stop_outreach_on_reply(
        conn,
        account_id=acc3["id"],
        phone="+79990004444",
        lead_id=lead3["id"],
        force_event=True,
    )
    assert len(calls) == 3
    assert _parse_req(calls[2])["text"] == f"You received a response from +79990004444\n\nAccount: #{acc3['id']}"


def test_credentials_from_kv_settings_when_env_not_set(conn, monkeypatch):
    # Ensure env variables are completely absent
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    now = utc_now()
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('bot_token', 'token_from_kv_999', ?)",
        (now,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('bot_chat_id', 'chat_from_kv_888', ?)",
        (now,),
    )
    conn.commit()

    token, chat_id = resolve_colleague_bot_credentials(conn)
    assert token == "token_from_kv_999"
    assert chat_id == "chat_from_kv_888"

    calls = _install_mock_opener(monkeypatch)
    ok = send_lead_reply_notification(
        conn,
        lead_handle="@phoenixIam",
        account_handle="@saabath",
    )
    assert ok is True
    assert len(calls) == 1
    info = _parse_req(calls[0])
    assert info["url"] == "https://api.telegram.org/bottoken_from_kv_999/sendMessage"
    assert info["chat_id"] == "chat_from_kv_888"
    assert info["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"


def test_credentials_from_vps_softugcbot_env_fallback(conn, monkeypatch, tmp_path):
    import app.digest as digest_mod

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("ALLOW_FILE_BOT_CREDS_IN_TEST", "1")

    fake_vps_env = tmp_path / "opt_softugcbot.env"
    fake_vps_env.write_text("TELEGRAM_BOT_TOKEN=vps_token_777\n", encoding="utf-8")
    monkeypatch.setattr(digest_mod, "ROOT", tmp_path / "empty_root")
    monkeypatch.setattr(digest_mod, "SOFTUGCBOT_ENV", tmp_path / "missing_mac_env")
    monkeypatch.setattr(digest_mod, "VPS_SOFTUGCBOT_ENV", fake_vps_env, raising=False)

    token, chat_id = resolve_colleague_bot_credentials(conn)
    assert token == "vps_token_777"
    assert chat_id == "978069541"

    # Explicit kv_settings in DB must take precedence over VPS_SOFTUGCBOT_ENV fallback
    now = utc_now()
    conn.execute(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES ('bot_token', 'custom_kv_token_888', ?)",
        (now,),
    )
    conn.commit()
    token_kv, chat_kv = resolve_colleague_bot_credentials(conn)
    assert token_kv == "custom_kv_token_888"
    assert chat_kv == "978069541"




def test_consecutive_apply_incoming_does_not_spam_duplicates(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    create_lead(
        conn,
        name="Phoenix Lead",
        username="phoenixIam",
        telegram_user_id=9001,
        status="waiting",
        account_id=acc["id"],
    )

    # 1st incoming message -> triggers notification
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 9001,
            "telegram_message_id": 701,
            "username": "phoenixIam",
            "name": "Phoenix Lead",
            "text": "Привет!",
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # 2nd consecutive incoming message from the same peer before any reply -> NO spam notification
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 9001,
            "telegram_message_id": 702,
            "username": "phoenixIam",
            "name": "Phoenix Lead",
            "text": "И еще один вопрос вдогонку",
            "direction": "in",
        },
    )
    assert len(calls) == 1


def test_telegram_service_account_777000_ignored(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 777000,
            "telegram_message_id": 801,
            "name": "Telegram",
            "text": "Login code: 12345",
            "direction": "in",
        },
    )
    assert len(calls) == 0


def test_account_resolved_from_lead_even_when_username_passed(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "colleague-token-123")
    monkeypatch.setenv("BOT_CHAT_ID", "colleague-chat-456")
    calls = _install_mock_opener(monkeypatch)

    acc = _create_account(
        conn,
        phone="+79998887766",
        telegram_username="saabath",
    )
    lead = create_lead(
        conn,
        name="Phoenix Lead",
        username="phoenixIam",
        telegram_user_id=9500,
        status="waiting",
        account_id=acc["id"],
    )
    stop_outreach_on_reply(
        conn,
        account_id=None,
        username="phoenixIam",
        lead_id=lead["id"],
    )
    assert len(calls) == 1
    assert _parse_req(calls[0])["text"] == "You received a response from @phoenixIam\n\nAccount: @saabath"

