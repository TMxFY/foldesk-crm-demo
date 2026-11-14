import asyncio
import json
from datetime import datetime, timezone

import pytest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageEntityUrl,
    User,
)

from app.db import connect, init_db, utc_now
from app.inbox import (
    _cache_messages,
    apply_incoming,
    edit_dialog_message,
    get_inbox_counts,
    list_inbox,
    list_messages,
    send_dialog_media,
    send_reply,
    stop_outreach_on_reply,
    sync_inbox,
)
from app.leads import create_lead
from app.outreach import _sync_campaign_replies, create_campaign, run_campaign_batch
from app.telegram_gateway import (
    FakeGateway,
    TelethonGateway,
    listening_event,
    render_formatted_html,
)


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "crm_v6_test.db")
    init_db(db)
    return db


def _make_account(conn, phone="+79990001122", username="sales_acc", name="Sales"):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at, updated_at, health
        ) VALUES (?, 'data/sessions/v6_test.session', 'connected', 1001, ?, ?, ?, ?, 'active')
        """,
        (phone, username, name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_db_migration_columns_and_bot_reply_notifications_table(conn):
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    expected_cols = {
        "media_url",
        "media_type",
        "media_mime",
        "media_filename",
        "media_duration",
        "entities_json",
        "formatted_html",
        "edited_at",
    }
    assert expected_cols.issubset(msg_cols)

    notif_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(bot_reply_notifications)").fetchall()
    }
    assert {
        "bot_message_id",
        "chat_id",
        "dialog_id",
        "lead_id",
        "account_id",
        "peer_id",
        "lead_handle",
        "account_handle",
        "created_at",
    }.issubset(notif_cols)

    # Idempotent re-init
    init_db(conn)


def test_render_formatted_html_entities_and_markdown_and_xss():
    # 1. XSS escaping + entities
    raw = "Привет <script>alert(1)</script> мир ссылка код"
    entities = [
        {"type": "bold", "offset": 0, "length": 6},
        {"type": "italic", "offset": 33, "length": 3},
        {"type": "text_link", "offset": 37, "length": 6, "url": "https://softugc.com/demo?a=1&b=2"},
        {"type": "code", "offset": 44, "length": 3},
    ]
    html_out = render_formatted_html(raw, entities)
    assert "<script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out
    assert "<strong>Привет</strong>" in html_out
    assert "<em>мир</em>" in html_out
    assert '<a href="https://softugc.com/demo?a=1&amp;b=2" target="_blank" rel="noopener">ссылка</a>' in html_out
    assert "<code>код</code>" in html_out

    # Additional entity types: pre, blockquote, spoiler, underline, strikethrough, url
    raw2 = "preblock quote secret under strike https://example.com/path_1"
    entities2 = [
        {"type": "pre", "offset": 0, "length": 8},
        {"type": "blockquote", "offset": 9, "length": 5},
        {"type": "spoiler", "offset": 15, "length": 6},
        {"type": "underline", "offset": 22, "length": 5},
        {"type": "strikethrough", "offset": 28, "length": 6},
        {"type": "url", "offset": 35, "length": 26},
    ]
    html2 = render_formatted_html(raw2, entities2)
    assert "<pre><code>preblock</code></pre>" in html2
    assert "<blockquote>quote</blockquote>" in html2
    assert '<span class="tg-spoiler">secret</span>' in html2
    assert "<u>under</u>" in html2
    assert "<s>strike</s>" in html2
    assert '<a href="https://example.com/path_1" target="_blank" rel="noopener">https://example.com/path_1</a>' in html2

    # 2. Markdown fallback when entities is empty
    md_text = (
        "> Цитата клиента\n"
        "Привет **жирный** и __курсив__ и `inline_code`!\n"
        "```python\nprint('<hi>')\n```\n"
        "Ссылка: https://example.com/my_page_test"
    )
    md_html = render_formatted_html(md_text, None)
    assert "<blockquote>Цитата клиента</blockquote>" in md_html
    assert "<strong>жирный</strong>" in md_html
    assert "<em>курсив</em>" in md_html
    assert "<code>inline_code</code>" in md_html
    assert "<pre><code>" in md_html and "&lt;hi&gt;" in md_html
    assert '<a href="https://example.com/my_page_test" target="_blank" rel="noopener">https://example.com/my_page_test</a>' in md_html


def test_telethon_gateway_history_and_watch_edited_and_media():
    gateway = TelethonGateway()
    user = User(id=42)
    user.username = "client_user"
    user.first_name = "Клиент"

    voice_doc = type(
        "Doc",
        (),
        {
            "mime_type": "audio/ogg",
            "attributes": [DocumentAttributeAudio(duration=14, voice=True)],
        },
    )()
    msg_voice = type(
        "Msg",
        (),
        {
            "id": 101,
            "message": None,
            "date": datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc),
            "edit_date": None,
            "out": False,
            "reply_to": None,
            "voice": True,
            "document": voice_doc,
            "entities": None,
        },
    )()

    msg_rich = type(
        "Msg",
        (),
        {
            "id": 102,
            "message": "Важно: подробности тут",
            "date": datetime(2026, 11, 14, 10, 5, tzinfo=timezone.utc),
            "edit_date": datetime(2026, 11, 14, 10, 6, tzinfo=timezone.utc),
            "out": False,
            "reply_to": None,
            "voice": None,
            "document": None,
            "photo": None,
            "entities": [
                MessageEntityBold(offset=0, length=5),
                MessageEntityTextUrl(offset=7, length=11, url="https://softugc.com"),
            ],
        },
    )()

    class FakeClient:
        def __init__(self):
            self.handlers = []

        async def get_entity(self, target):
            return user

        async def get_messages(self, entity, limit=50, ids=None):
            if ids is not None:
                return msg_voice
            return [msg_rich, msg_voice]

        def add_event_handler(self, callback, event_Builder):
            self.handlers.append((callback, event_Builder))

        def remove_event_handler(self, callback):
            self.handlers = [(cb, eb) for cb, eb in self.handlers if cb != callback]

        async def catch_up(self):
            pass

        async def run_until_disconnected(self):
            await asyncio.Future()

    fake_client = FakeClient()

    async def run():
        async def open_client(_session):
            return fake_client, False

        gateway._open = open_client
        rows = await gateway.fetch_messages("sess", 42, limit=10)
        assert len(rows) == 2
        v_row = rows[0]
        assert v_row["telegram_message_id"] == 101
        assert v_row["media_type"] == "voice"
        assert v_row["media_mime"] == "audio/ogg"
        assert v_row["media_duration"] == 14
        assert v_row["text"] == ""

        r_row = rows[1]
        assert r_row["telegram_message_id"] == 102
        assert r_row["edited_at"] == "2026-11-14T10:06:00"
        assert "<strong>Важно</strong>" in r_row["formatted_html"]
        assert 'href="https://softugc.com"' in r_row["formatted_html"]

        # Test watch_private with MessageEdited
        seen = []

        async def on_msg(payload):
            seen.append(payload)

        task = asyncio.create_task(gateway.watch_private("sess", on_msg))
        await asyncio.wait_for(listening_event(gateway, "sess").wait(), timeout=2)
        # Find the MessageEdited handler
        edited_cb = None
        for cb, eb in fake_client.handlers:
            if eb.__class__.__name__ == "MessageEdited" or "MessageEdited" in str(eb):
                edited_cb = cb
                break
        assert edited_cb is not None

        edit_event = type(
            "EditEv",
            (),
            {
                "is_private": True,
                "is_group": False,
                "is_channel": False,
                "message": msg_rich,
                "get_sender": lambda self=None: asyncio.sleep(0, result=user),
            },
        )()
        await edited_cb(edit_event)
        assert len(seen) == 1
        assert seen[0]["is_edit"] is True
        assert seen[0]["edited_at"] == "2026-11-14T10:06:00"
        assert "<strong>Важно</strong>" in seen[0]["formatted_html"]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_inbox_rich_messages_edit_and_send_media(conn, tmp_path, monkeypatch):
    import app.inbox as inbox_mod

    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "inbox_media", raising=False)
    acc_id = _make_account(conn)
    gw = FakeGateway()

    # 1. Incoming voice message
    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 5001,
            "telegram_message_id": 10,
            "username": "ivan_client",
            "name": "Иван",
            "text": "",
            "media_type": "voice",
            "media_mime": "audio/ogg",
            "media_duration": 9,
            "direction": "in",
        },
    )
    assert dlg["last_message_preview"] == "🎤 Голосовое сообщение"

    # 2. Incoming message with entities, then edited by peer
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 5001,
            "telegram_message_id": 11,
            "username": "ivan_client",
            "name": "Иван",
            "text": "Старый текст",
            "direction": "in",
        },
    )
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 5001,
            "telegram_message_id": 11,
            "username": "ivan_client",
            "name": "Иван",
            "text": "Новый **жирный** текст",
            "edited_at": "2026-11-14T12:00:00",
            "is_edit": True,
            "direction": "in",
        },
    )
    msgs = list_messages(conn, dlg["id"])
    assert len(msgs) == 2
    assert msgs[0]["media_type"] == "voice"
    assert msgs[0]["media_duration"] == 9
    assert msgs[1]["text"] == "Новый **жирный** текст"
    assert msgs[1]["edited_at"] == "2026-11-14T12:00:00"
    assert "<strong>жирный</strong>" in msgs[1]["formatted_html"]

    # Cannot edit incoming message
    with pytest.raises(ValueError):
        asyncio.run(edit_dialog_message(conn, dlg["id"], 11, "Попытка", gateway=gw))

    # 3. Send outgoing reply and edit it
    out_msg = asyncio.run(send_reply(conn, dlg["id"], "Отправляю **оффер**", gw))
    assert "<strong>оффер</strong>" in out_msg["formatted_html"]

    edited_out = asyncio.run(
        edit_dialog_message(
            conn,
            dlg["id"],
            out_msg["telegram_message_id"],
            "Отправляю **обновлённый оффер**",
            gateway=gw,
        )
    )
    assert edited_out["text"] == "Отправляю **обновлённый оффер**"
    assert edited_out["edited_at"] is not None
    assert "<strong>обновлённый оффер</strong>" in edited_out["formatted_html"]
    dlg_row = conn.execute("SELECT last_message_preview FROM dialogs WHERE id = ?", (dlg["id"],)).fetchone()
    assert dlg_row["last_message_preview"] == "Отправляю **обновлённый оффер**"

    # 4. Send outgoing voice media via send_dialog_media
    media_msg = asyncio.run(
        send_dialog_media(
            conn,
            dlg["id"],
            file_bytes=b"OggS-fake-voice-bytes",
            filename="voice_reply.ogg",
            mime_type="audio/ogg",
            media_type="voice",
            caption="",
            duration=7,
            gateway=gw,
        )
    )
    assert media_msg["direction"] == "out"
    assert media_msg["media_type"] == "voice"
    assert media_msg["media_duration"] == 7
    assert media_msg["media_url"].startswith("/media/inbox/")
    dlg_row2 = conn.execute("SELECT last_message_preview FROM dialogs WHERE id = ?", (dlg["id"],)).fetchone()
    assert dlg_row2["last_message_preview"] == "🎤 Голосовое сообщение"


def test_crm_only_strict_filter_and_auto_create_lead_on_outreach_reply(conn):
    acc_id = _make_account(conn)
    now = utc_now()

    # 1. Random personal chat from Telegram (NOT in leads, NOT in campaign_recipients)
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 70001, 'random_friend', 'Личный Друг', 0, 2, ?, 'Пойдём в бар?', 1, 0, NULL, ?, ?)
        """,
        (acc_id, now, now, now),
    )

    # 2. Group chat
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, created_at, updated_at
        ) VALUES (?, 'chat', 70002, 'crypto_chat', 'Крипто Чат', 1, 5, ?, 'Всем привет', 1, 0, NULL, ?, ?)
        """,
        (acc_id, now, now, now),
    )

    # 3. Cold outreach recipient who has NOT replied (step 1 sent, last_from_peer=0, no incoming messages)
    camp = create_campaign(
        conn,
        name="B2B Cold",
        account_id=acc_id,
        steps=[{"template_text": "Привет, {name}!"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 70003, 'cold_silent', 'Молчаливый Лид', 'SilentCorp', 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], now, now, now),
    )
    cold_dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 70003, 'cold_silent', 'Молчаливый Лид', 0, 0, ?, 'Привет!', 0, 0, NULL, ?, ?)
        """,
        (acc_id, now, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 1, 'out', 'Привет!', ?, ?, ?)
        """,
        (cold_dlg_id, now, now, now),
    )

    # 4. Cold outreach recipient WITH a lead in status='waiting' who also has NOT replied yet
    cold_lead = create_lead(
        conn,
        name="Waiting Cold Lead",
        username="cold_waiting_lead",
        telegram_user_id=70004,
        status="waiting",
        account_id=acc_id,
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, 70004, 'cold_waiting_lead', 'Waiting Cold Lead', 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], cold_lead["id"], now, now, now),
    )
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 70004, 'cold_waiting_lead', 'Waiting Cold Lead', 0, 0, ?, 'Шаг 1', 0, 0, ?, ?, ?)
        """,
        (acc_id, now, cold_lead["id"], now, now),
    )

    # 5. Active CRM lead in status='talking'
    talking_lead = create_lead(
        conn,
        name="Активный Клиент",
        username="talking_client",
        telegram_user_id=70005,
        status="talking",
        account_id=acc_id,
    )
    talking_dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 70005, 'talking_client', 'Активный Клиент', 0, 0, ?, 'Договорились', 0, 0, ?, ?, ?)
        """,
        (acc_id, now, talking_lead["id"], now, now),
    ).lastrowid

    # 6. Cold outreach recipient WITHOUT lead_id who REPLIES now!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, company, note,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, NULL, 70006, 'replied_prospect', 'Ответивший Фаундер', 'FounderAI', 'B2B AI SaaS', 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], now, now, now),
    )
    conn.commit()

    replied_dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 70006,
            "telegram_message_id": 555,
            "username": "replied_prospect",
            "name": "Ответивший Фаундер",
            "text": "Привет! Давайте созвонимся завтра",
            "direction": "in",
        },
    )

    # Verify lead was auto-created in `leads` and linked to both campaign_recipients and dialogs
    rec_after = conn.execute(
        "SELECT status, lead_id FROM campaign_recipients WHERE telegram_user_id = 70006"
    ).fetchone()
    assert rec_after["status"] == "replied"
    assert rec_after["lead_id"] is not None
    auto_lead_id = int(rec_after["lead_id"])

    lead_after = conn.execute("SELECT * FROM leads WHERE id = ?", (auto_lead_id,)).fetchone()
    assert lead_after is not None
    assert lead_after["status"] == "talking"
    assert lead_after["airtable_status"] == "In conversation"
    assert lead_after["username"] == "replied_prospect"
    assert lead_after["company"] == "FounderAI"
    assert replied_dlg["lead_id"] == auto_lead_id

    # Now check `list_inbox(..., crm_only=True)` vs `list_inbox(..., crm_only=False)`
    all_dialogs = list_inbox(conn, account_id=0, crm_only=False)
    assert len(all_dialogs) == 6

    crm_dialogs = list_inbox(conn, account_id=0, crm_only=True)
    crm_usernames = {d["username"] for d in crm_dialogs}
    assert crm_usernames == {"talking_client", "replied_prospect"}

    crm_counts = get_inbox_counts(conn, account_id=0, crm_only=True)
    assert crm_counts["waiting"] == 1  # replied_prospect
    assert crm_counts["our_turn"] == 1  # talking_client
    assert crm_counts["groups"] == 0

    # Verify sync_inbox with crm_only=True ignores random Telegram people
    sync_people = [
        {
            "telegram_user_id": 88888,
            "username": "stranger_from_tg",
            "name": "Stranger",
            "last_from_peer": 1,
            "last_message_preview": "Спам/личный чат",
            "last_message_at": now,
            "unread_count": 1,
        },
        {
            "telegram_user_id": 70005,
            "username": "talking_client",
            "name": "Активный Клиент",
            "last_from_peer": 1,
            "last_message_preview": "Новый ответ от CRM лида",
            "last_message_at": now,
            "unread_count": 1,
        },
    ]
    synced = sync_inbox(conn, acc_id, sync_people, crm_only=True)
    assert [d["username"] for d in synced] == ["talking_client"]
    stranger_row = conn.execute(
        "SELECT id FROM dialogs WHERE peer_id = 88888"
    ).fetchone()
    assert stranger_row is None


def test_api_endpoints_crm_only_edit_and_media_and_bot_webhook(conn, tmp_path, monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    import app.inbox as inbox_mod
    import app.main as main_mod

    media_dir = tmp_path / "inbox_media_api"
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", media_dir, raising=False)
    monkeypatch.setattr(main_mod, "INBOX_MEDIA_DIR", media_dir, raising=False)

    acc_id = _make_account(conn, phone="+79995556677", username="api_acc", name="API Acc")
    now = utc_now()

    # Random personal chat (not CRM)
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            created_at, updated_at
        ) VALUES (?, 'user', 91001, 'random_friend_api', 'Friend API', 0, 1, ?, 'Привет', 1, 0, ?, ?)
        """,
        (acc_id, now, now, now),
    )

    # CRM lead + dialog
    lead = create_lead(
        conn,
        name="API Lead",
        username="api_lead_user",
        telegram_user_id=91002,
        status="talking",
        account_id=acc_id,
    )
    dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer, muted,
            lead_id, created_at, updated_at
        ) VALUES (?, 'user', 91002, 'api_lead_user', 'API Lead', 0, 1, ?, 'Жду КП', 1, 0, ?, ?, ?)
        """,
        (acc_id, now, lead["id"], now, now),
    ).lastrowid
    conn.commit()

    gw = FakeGateway()
    app = main_mod.create_app(conn=conn, gateway=gw)
    client = TestClient(app)

    # 1. GET /api/inbox?crm_only=1 returns only CRM lead dialog
    r_crm = client.get("/api/inbox?crm_only=1&with_counts=true")
    assert r_crm.status_code == 200
    data_crm = r_crm.json()
    assert [d["username"] for d in data_crm["dialogs"]] == ["api_lead_user"]

    # 2. Send message with reply_to_msg_id and edit it via PATCH
    r_send = client.post(
        f"/api/dialogs/{dlg_id}/messages",
        json={"text": "Привет **API**!", "reply_to_msg_id": 100},
    )
    assert r_send.status_code == 200
    sent_msg = r_send.json()
    assert sent_msg["reply_to_telegram_message_id"] == 100
    assert "<strong>API</strong>" in sent_msg["formatted_html"]

    mid = sent_msg["telegram_message_id"]
    r_edit = client.patch(
        f"/api/dialogs/{dlg_id}/messages/{mid}",
        json={"text": "Привет **обновлённый API**!"},
    )
    assert r_edit.status_code == 200
    edited_msg = r_edit.json()
    assert edited_msg["edited_at"] is not None
    assert "<strong>обновлённый API</strong>" in edited_msg["formatted_html"]

    # 3. Upload voice via POST /api/dialogs/{id}/media and fetch via GET .../media
    b64_audio = base64.b64encode(b"OggS-test-voice-stream").decode("ascii")
    r_media = client.post(
        f"/api/dialogs/{dlg_id}/media",
        json={
            "media_type": "voice",
            "filename": "note.ogg",
            "mime_type": "audio/ogg",
            "data_base64": b64_audio,
            "duration": 5,
        },
    )
    assert r_media.status_code == 200
    m_json = r_media.json()
    assert m_json["media_type"] == "voice"
    assert m_json["media_duration"] == 5

    r_fetch_media = client.get(
        f"/api/dialogs/{dlg_id}/messages/{m_json['telegram_message_id']}/media"
    )
    assert r_fetch_media.status_code == 200
    assert r_fetch_media.content == b"OggS-test-voice-stream"

    # 4. Bot webhook reply via POST /api/bot/webhook
    r_bot_cb = client.post(
        "/api/bot/webhook",
        json={
            "callback_query": {
                "id": "cb1",
                "from": {"id": 978069541},
                "message": {"chat": {"id": 978069541}},
                "data": f"reply_dlg:{dlg_id}",
            }
        },
    )
    assert r_bot_cb.status_code == 200
    assert r_bot_cb.json()["action"] == "reply_prompt"

    r_bot_msg = client.post(
        "/api/bot/webhook",
        json={
            "message": {
                "message_id": 888,
                "chat": {"id": 978069541, "type": "private"},
                "text": "Ответ прямо из вебхука бота",
            }
        },
    )
    assert r_bot_msg.status_code == 200
    assert r_bot_msg.json()["replied"] is True
    assert r_bot_msg.json()["dialog_id"] == dlg_id


def test_playwright_v6_rich_inbox_voice_edit_and_miniapp(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    from urllib.request import urlopen

    root = Path(__file__).resolve().parent.parent
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e_v6.db"
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["E2E_DB_PATH"] = str(db_path)
    env["PYTEST_CURRENT_TEST"] = "test_v6_ui_e2e"

    server = subprocess.Popen(
        [sys.executable, str(root / "e2e" / "serve.py")],
        cwd=str(root / "e2e"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                with urlopen(f"{base_url}/api/accounts", timeout=1) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.15)
        assert ready, "V6 E2E server did not start in time"

        pw_script = f"""
const {{ chromium }} = require('playwright');

(async () => {{
  const browser = await chromium.launch({{
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  }});
  const context = await browser.newContext({{ viewport: {{ width: 1440, height: 900 }} }});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
  page.on('console', (msg) => {{
    if (msg.type() === 'error' && !/status of 4\\d\\d/.test(msg.text())) {{
      errors.push('console.error: ' + msg.text());
    }}
  }});

  await page.request.post('{base_url}/api/test/reset');
  await page.request.post('{base_url}/api/demo/seed');
  await page.goto('{base_url}/#inbox');
  await page.locator('#inbox-crm-only-badge').waitFor({{ state: 'visible' }});

  // Wait for CRM dialogs in #dialog-results and click the first one
  await page.locator('#dialog-results .dialog').first().waitFor({{ state: 'visible' }});
  await page.locator('#dialog-results .dialog').first().click();

  // Send a formatted message with bold + code + spoiler
  await page.locator('#reply-text').fill('Привет! Вот **спецпредложение** и `ПРОМО2026` и ||бонус||');
  await page.locator('#reply-submit-btn').click();
  await page.locator('#messages li.out strong', {{ hasText: 'спецпредложение' }}).waitFor();
  await page.locator('#messages li.out code', {{ hasText: 'ПРОМО2026' }}).waitFor();

  // Edit the outgoing message via .msg-edit-btn
  await page.locator('#messages li.out .msg-edit-btn').last().click();
  await page.locator('#reply-context-bar').waitFor({{ state: 'visible' }});
  await page.locator('#reply-text').fill('Привет! Вот **обновлённое КП** и `ПРОМО2026`');
  await page.locator('#reply-submit-btn').click();
  await page.locator('#messages li.out .msg-edited-badge', {{ hasText: 'изменено' }}).waitFor();

  // Send a voice message via #reply-voice-btn click (start -> stop)
  await page.locator('#reply-voice-btn').click();
  await page.waitForTimeout(300);
  await page.locator('#reply-voice-btn').click();
  await page.locator('#messages .voice-player').first().waitFor({{ state: 'visible' }});
  const rateBtn = page.locator('#messages .voice-rate-btn').first();
  await rateBtn.click();
  const rateText = await rateBtn.textContent();
  if (rateText !== '1.5x') {{
    throw new Error(`Expected voice rate 1.5x after click, got ${{rateText}}`);
  }}

  // Test mobile Telegram Mini App mode (390x844)
  await page.setViewportSize({{ width: 390, height: 844 }});
  await page.goto('{base_url}/?tg_miniapp=1#inbox');
  await page.locator('#miniapp-bottom-nav').waitFor({{ state: 'visible' }});

  if (errors.length) {{
    throw new Error('Browser errors: ' + errors.join('; '));
  }}

  await browser.close();
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
        proc = subprocess.run(
            ["node", "-e", pw_script],
            cwd=str(root / "e2e"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"Playwright V6 E2E failed:\\nSTDOUT:\\n{proc.stdout}\\nSTDERR:\\n{proc.stderr}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


