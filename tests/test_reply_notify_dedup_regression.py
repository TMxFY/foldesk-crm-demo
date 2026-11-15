import json
import threading
import urllib.request

import pytest

from app.bot_login import _resolve_dialog_id_for_bot_reply
from app.db import connect, init_db, locked_conn, utc_now
from app.digest import _norm_ts, ensure_bot_reply_notifications_table, send_lead_reply_notification
from app.inbox import (
    _cache_messages,
    apply_inbox_read_update,
    apply_incoming,
    apply_live_outgoing,
    get_thread,
    mark_dialog_read,
    remember_dialogs,
    send_reply,
    stop_outreach_on_reply,
    sync_inbox,
)
from app.leads import create_lead
from app.outreach import _sync_campaign_replies, create_campaign, run_campaign_batch
from app.telegram_gateway import FakeGateway


class _FakeResp:
    def __init__(self, msg_id=100):
        self.status = 200
        self._body = json.dumps(
            {"ok": True, "result": {"message_id": msg_id}}
        ).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_bot_spy(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    calls = []
    counter = {"n": 1000}

    def fake_urlopen(req, timeout=None, context=None):
        counter["n"] += 1
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append({"url": req.full_url, "body": body})
        return _FakeResp(msg_id=counter["n"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _seed_account(conn, phone="+79991112233", username="mgr_acc"):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_username, telegram_name, session_path,
            status, health, created_at, updated_at
        ) VALUES (?, ?, 'Manager', 'data/sessions/test_dedup.session', 'connected', 'active', ?, ?)
        """,
        (phone, username, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _add_recipient(
    conn,
    campaign_id: int,
    *,
    account_id: int,
    peer_id: int,
    username: str,
    lead_id: int | None = None,
    status: str = "in_progress",
    last_sent_at: str = "2026-11-15T09:00:00",
):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, username, telegram_user_id, name, lead_id,
            status, current_step, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (campaign_id, username, peer_id, username, lead_id, status, account_id, last_sent_at, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "reply_notify_dedup.db")
    init_db(db)
    return db


def test_case1_sync_inbox_unread_zero_never_notifies(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Already Read Lead",
        username="read_lead",
        telegram_user_id=10101,
        status="waiting",
        account_id=acc_id,
    )
    camp = create_campaign(conn, name="Camp 1", account_id=acc_id, steps=[{"template_text": "Hi"}])
    rec_id = _add_recipient(conn, camp["id"], account_id=acc_id, peer_id=10101, username="read_lead", lead_id=lead["id"])

    # Dialog in Telegram already read (unread_count=0), even though last_from_peer=1
    sync_inbox(
        conn,
        acc_id,
        [
            {
                "telegram_user_id": 10101,
                "username": "read_lead",
                "name": "Already Read Lead",
                "last_from_peer": 1,
                "last_message_preview": "Давно прочитано",
                "last_message_at": "2026-11-15T10:00:00",
                "unread_count": 0,
            }
        ],
    )

    # Outreach stopping and CRM status move still work
    rec = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec_id,)).fetchone()
    lrow = conn.execute("SELECT status FROM leads WHERE id = ?", (lead["id"],)).fetchone()
    assert rec["status"] == "replied"
    assert lrow["status"] == "talking"

    # Zero notifications sent for already-read dialog
    assert len(calls) == 0


def test_case2_remember_dialogs_unread_zero_or_none_never_notifies(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead_zero = create_lead(
        conn,
        name="Zero Lead",
        username="zero_lead",
        telegram_user_id=20201,
        status="waiting",
        account_id=acc_id,
    )
    lead_none = create_lead(
        conn,
        name="None Lead",
        username="none_lead",
        telegram_user_id=20202,
        status="new",
        account_id=acc_id,
    )

    remember_dialogs(
        conn,
        acc_id,
        [
            {
                "telegram_user_id": 20201,
                "username": "zero_lead",
                "name": "Zero Lead",
                "last_from_peer": 1,
                "last_message_preview": "Прочитано",
                "last_message_at": "2026-11-15T10:00:00",
                "unread_count": 0,
            },
            {
                "telegram_user_id": 20202,
                "username": "none_lead",
                "name": "None Lead",
                "last_from_peer": 1,
                "last_message_preview": "Из поиска без unread",
                "last_message_at": "2026-11-15T10:01:00",
            },
        ],
    )

    assert len(calls) == 0
    assert conn.execute("SELECT status FROM leads WHERE id = ?", (lead_zero["id"],)).fetchone()["status"] == "talking"
    assert conn.execute("SELECT status FROM leads WHERE id = ?", (lead_none["id"],)).fetchone()["status"] == "talking"


def test_case4_send_lead_reply_notification_direct_guards_and_dedup(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Direct Guard Lead",
        username="direct_guard",
        telegram_user_id=30301,
        status="talking",
        account_id=acc_id,
    )
    now = utc_now()
    # 1. Dialog is read (unread_count=0) -> blocked by default allow_read=False
    dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 30301, 'direct_guard', 'Direct Guard', 1, ?, 'Привет', 0, ?, ?, ?)
        """,
        (acc_id, now, lead["id"], now, now),
    ).lastrowid
    conn.commit()

    assert send_lead_reply_notification(
        conn,
        lead_handle="@direct_guard",
        account_handle="@mgr_acc",
        dialog_id=dlg_id,
        lead_id=lead["id"],
        account_id=acc_id,
        peer_id=30301,
    ) is False
    assert len(calls) == 0

    # 2. Dialog is unread (unread_count=1) AND last_from_peer=0 (already replied) -> blocked
    conn.execute("UPDATE dialogs SET unread_count = 1, last_from_peer = 0 WHERE id = ?", (dlg_id,))
    conn.commit()
    assert send_lead_reply_notification(
        conn,
        lead_handle="@direct_guard",
        account_handle="@mgr_acc",
        dialog_id=dlg_id,
        lead_id=lead["id"],
        account_id=acc_id,
        peer_id=30301,
    ) is False
    assert len(calls) == 0

    # 3. Dialog is unread (unread_count=1, last_from_peer=1), BUT latest message in messages is 'out' -> blocked
    conn.execute("UPDATE dialogs SET unread_count = 1, last_from_peer = 1 WHERE id = ?", (dlg_id,))
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 900, 'out', 'Мы уже ответили', '2026-11-15T12:00:00', ?, ?)
        """,
        (dlg_id, now, now),
    )
    conn.commit()
    assert send_lead_reply_notification(
        conn,
        lead_handle="@direct_guard",
        account_handle="@mgr_acc",
        dialog_id=dlg_id,
        incoming_sent_at="2026-11-15T11:55:00",
    ) is False
    assert len(calls) == 0

    # 4. Now incoming message arrives AFTER 'out' -> 1 notification sent; second direct call deduped
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 901, 'in', 'Новый ответ', '2026-11-15T12:05:00', ?, ?)
        """,
        (dlg_id, now, now),
    )
    conn.commit()
    assert send_lead_reply_notification(
        conn,
        lead_handle="@direct_guard",
        account_handle="@mgr_acc",
        dialog_id=dlg_id,
        account_id=acc_id,
        peer_id=30301,
        telegram_message_id=901,
        incoming_sent_at="2026-11-15T12:05:00",
    ) is True
    assert len(calls) == 1

    # Repeat direct call (even with a different lead_id or no telegram_message_id) -> deduped!
    assert send_lead_reply_notification(
        conn,
        lead_handle="@direct_guard",
        account_handle="@mgr_acc",
        dialog_id=dlg_id,
        lead_id=99999,
        account_id=acc_id,
        peer_id=30301,
    ) is False
    assert len(calls) == 1


def test_case4_redelivery_edit_cache_messages_ui_read_and_new_turn(conn, monkeypatch):
    import asyncio

    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Turn Lifecycle",
        username="turn_user",
        telegram_user_id=40401,
        status="waiting",
        account_id=acc_id,
    )

    # 1. First incoming message -> 1 notification, unread=1, 1 reply_received event
    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 40401,
            "telegram_message_id": 501,
            "username": "turn_user",
            "name": "Turn Lifecycle",
            "text": "Привет, сколько стоит?",
            "sent_at": "2026-11-15T12:00:00",
            "direction": "in",
        },
    )
    assert dlg["unread_count"] == 1
    assert len(calls) == 1

    # 2. Exact redelivery of msg 501 (not is_edit) -> unread MUST stay 1, NO notification, NO duplicate event
    dlg_redeliv = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 40401,
            "telegram_message_id": 501,
            "username": "turn_user",
            "name": "Turn Lifecycle",
            "text": "Привет, сколько стоит?",
            "sent_at": "2026-11-15T12:00:00",
            "direction": "in",
        },
    )
    assert dlg_redeliv["unread_count"] == 1
    assert len(calls) == 1

    # 3. Edit of msg 501 (is_edit=True) -> NO notification
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 40401,
            "telegram_message_id": 501,
            "username": "turn_user",
            "name": "Turn Lifecycle",
            "text": "Привет, сколько стоит пакет?",
            "sent_at": "2026-11-15T12:00:00",
            "edited_at": "2026-11-15T12:00:30",
            "is_edit": True,
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # 4. Opening thread in UI (_cache_messages / get_thread) -> NO repeat notification
    gw = FakeGateway()

    async def fake_fetch(session_path, peer_id, limit=50):
        return [
            {
                "telegram_message_id": 501,
                "direction": "in",
                "text": "Привет, сколько стоит пакет?",
                "sent_at": "2026-11-15T12:00:00",
            }
        ]

    gw.fetch_messages = fake_fetch
    asyncio.run(get_thread(conn, dlg["id"], gw))
    assert len(calls) == 1

    # 5. Manager marks dialog read in CRM UI
    read_dlg = mark_dialog_read(conn, dlg["id"])
    assert read_dlg["unread_count"] == 0

    # Stale sync_inbox still claiming unread=1 for the same timestamp -> NO repeat notification
    sync_inbox(
        conn,
        acc_id,
        [
            {
                "telegram_user_id": 40401,
                "username": "turn_user",
                "name": "Turn Lifecycle",
                "last_from_peer": 1,
                "last_message_preview": "Привет, сколько стоит пакет?",
                "last_message_at": "2026-11-15T12:00:00",
                "unread_count": 1,
            }
        ],
    )
    assert len(calls) == 1

    # 6. Manager sends reply via send_reply -> clears turn
    asyncio.run(send_reply(conn, dlg["id"], "Отправил прайс!", None))
    assert len(calls) == 1

    # 7. Brand new incoming message in NEW turn -> MUST send 1 new notification!
    dlg2 = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 40401,
            "telegram_message_id": 503,
            "username": "turn_user",
            "name": "Turn Lifecycle",
            "text": "Супер, давайте созвонимся!",
            "sent_at": "2026-11-15T12:10:00",
            "direction": "in",
        },
    )
    assert dlg2["unread_count"] == 1
    assert len(calls) == 2

    # Check reply_received events: 1 for first turn, 1 for second turn (2 total, no spam on redelivery/edit/sync)
    events = conn.execute(
        "SELECT * FROM lead_events WHERE lead_id = ? AND event_type = 'reply_received' ORDER BY id",
        (lead["id"],),
    ).fetchall()
    assert len(events) == 2


def test_case4_multi_campaign_and_duplicate_leads_single_notification(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)

    # Two duplicate lead rows pointing to same telegram_user_id / username (one with account_id=acc_id, one with account_id=None)
    lead1 = create_lead(
        conn,
        name="Dup Lead 1",
        username="dup_prospect",
        telegram_user_id=50501,
        status="waiting",
        account_id=acc_id,
    )
    # Create a second lead via phone/username without colliding unique index
    now = utc_now()
    lead2_id = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, status, created_at, updated_at
        ) VALUES (NULL, NULL, 'dup_prospect', 'Dup Lead 2', 'new', ?, ?)
        """,
        (now, now),
    ).lastrowid
    conn.commit()

    camp1 = create_campaign(conn, name="Camp A", account_id=acc_id, steps=[{"template_text": "Step A"}])
    camp2 = create_campaign(conn, name="Camp B", account_id=acc_id, steps=[{"template_text": "Step B"}])
    rec1 = _add_recipient(conn, camp1["id"], account_id=acc_id, peer_id=50501, username="dup_prospect", lead_id=lead1["id"])
    rec2 = _add_recipient(conn, camp2["id"], account_id=acc_id, peer_id=50501, username="dup_prospect", lead_id=lead2_id)

    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 50501,
            "telegram_message_id": 601,
            "username": "dup_prospect",
            "name": "Dup Lead",
            "text": "Интересно!",
            "sent_at": "2026-11-15T13:00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1
    assert conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec1,)).fetchone()["status"] == "replied"
    assert conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec2,)).fetchone()["status"] == "replied"

    # Subsequent outreach sync / stop_outreach_on_reply for lead2_id or campaign 2 -> 0 additional notifications
    _sync_campaign_replies(conn, camp2["id"], acc_id)
    stop_outreach_on_reply(conn, account_id=acc_id, peer_id=50501, lead_id=lead2_id, force_event=True)
    assert len(calls) == 1


def test_case5_edit_after_out_reply_does_not_clobber_dialog_or_notify(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    create_lead(
        conn,
        name="Edit After Out",
        username="edit_after_out",
        telegram_user_id=60601,
        status="waiting",
        account_id=acc_id,
    )

    # 1. Peer sends incoming msg 701
    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 60601,
            "telegram_message_id": 701,
            "username": "edit_after_out",
            "name": "Edit After Out",
            "text": "Сколько стоит?",
            "sent_at": "2026-11-15T14:00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # 2. Manager replies externally (apply_live_outgoing) with msg 702
    after_out = apply_live_outgoing(
        conn,
        acc_id,
        {
            "telegram_user_id": 60601,
            "telegram_message_id": 702,
            "username": "edit_after_out",
            "name": "Edit After Out",
            "text": "Пакет стоит $1500",
            "sent_at": "2026-11-15T14:05:00",
            "direction": "out",
        },
    )
    assert after_out["last_from_peer"] == 0
    assert after_out["unread_count"] == 0
    assert after_out["last_message_preview"] == "Пакет стоит $1500"

    # 3. Peer edits their earlier incoming msg 701 (is_edit=True)
    after_edit = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 60601,
            "telegram_message_id": 701,
            "username": "edit_after_out",
            "name": "Edit After Out",
            "text": "Сколько стоит 10 роликов?",
            "sent_at": "2026-11-15T14:00:00",
            "edited_at": "2026-11-15T14:06:00",
            "is_edit": True,
            "direction": "in",
        },
    )
    # Dialog metadata MUST NOT be clobbered by old message edit
    assert after_edit["last_from_peer"] == 0
    assert after_edit["unread_count"] == 0
    assert after_edit["last_message_at"] == "2026-11-15T14:05:00"
    assert after_edit["last_message_preview"] == "Пакет стоит $1500"
    # Message 701 in messages table IS updated in place
    msg_701 = conn.execute(
        "SELECT text, edited_at FROM messages WHERE dialog_id = ? AND telegram_message_id = 701",
        (dlg["id"],),
    ).fetchone()
    assert msg_701["text"] == "Сколько стоит 10 роликов?"
    assert msg_701["edited_at"] == "2026-11-15T14:06:00"
    # No repeat notification
    assert len(calls) == 1


def test_apply_live_outgoing_monotonic_and_apply_inbox_read_update(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    create_lead(
        conn,
        name="External Sync User",
        username="ext_user",
        telegram_user_id=70701,
        status="waiting",
        account_id=acc_id,
    )

    # Nonexistent dialog for apply_inbox_read_update -> None
    assert apply_inbox_read_update(conn, acc_id, 999999, max_id=10) is None

    # Peer sends incoming msg 801
    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 70701,
            "telegram_message_id": 801,
            "username": "ext_user",
            "name": "External Sync User",
            "text": "Жду ответ",
            "sent_at": "2026-11-15T15:10:00",
            "direction": "in",
        },
    )
    assert dlg["unread_count"] == 1
    assert len(calls) == 1

    # Stale outgoing backfill (sent_at < 15:10:00) must NOT clobber newer incoming dialog state
    stale_res = apply_live_outgoing(
        conn,
        acc_id,
        {
            "telegram_user_id": 70701,
            "telegram_message_id": 800,
            "text": "Старый исходящий",
            "sent_at": "2026-11-15T15:00:00",
            "direction": "out",
        },
    )
    assert stale_res["last_from_peer"] == 1
    assert stale_res["unread_count"] == 1
    assert stale_res["last_message_at"] == "2026-11-15T15:10:00"
    assert stale_res["last_message_preview"] == "Жду ответ"

    # External Telegram read update marks dialog read (unread_count=0)
    read_res = apply_inbox_read_update(conn, acc_id, 70701, max_id=801)
    assert read_res is not None
    assert read_res["unread_count"] == 0

    # Subsequent sync_inbox with stale unread_count=1 does NOT notify
    sync_inbox(
        conn,
        acc_id,
        [
            {
                "telegram_user_id": 70701,
                "username": "ext_user",
                "name": "External Sync User",
                "last_from_peer": 1,
                "last_message_preview": "Жду ответ",
                "last_message_at": "2026-11-15T15:10:00",
                "unread_count": 1,
            }
        ],
    )
    assert len(calls) == 1

    # New live outgoing message at 15:20:00 updates dialog cleanly
    fresh_out = apply_live_outgoing(
        conn,
        acc_id,
        {
            "telegram_user_id": 70701,
            "telegram_message_id": 802,
            "text": "Отвечаю с телефона!",
            "sent_at": "2026-11-15T15:20:00",
            "direction": "out",
        },
    )
    assert fresh_out["last_from_peer"] == 0
    assert fresh_out["unread_count"] == 0
    assert fresh_out["last_message_at"] == "2026-11-15T15:20:00"
    assert fresh_out["last_message_preview"] == "Отвечаю с телефона!"

    # New incoming message at 15:25:00 (new turn after out reply) notifies once!
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 70701,
            "telegram_message_id": 803,
            "username": "ext_user",
            "name": "External Sync User",
            "text": "Спасибо, всё получил!",
            "sent_at": "2026-11-15T15:25:00",
            "direction": "in",
        },
    )
    assert len(calls) == 2


def test_same_second_new_turn_after_reply_or_mark_read_still_notifies(conn, monkeypatch):
    import asyncio

    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    create_lead(
        conn,
        name="Fast Test User",
        username="fast_user",
        telegram_user_id=80801,
        status="waiting",
        account_id=acc_id,
    )
    same_ts = utc_now()

    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 80801,
            "telegram_message_id": 1001,
            "username": "fast_user",
            "text": "Первое",
            "sent_at": same_ts,
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Consecutive incoming in same second -> deduped
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 80801,
            "telegram_message_id": 1002,
            "username": "fast_user",
            "text": "Второе без ответа",
            "sent_at": same_ts,
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Manager replies in the exact same second (offline send_reply assigns 1003)
    asyncio.run(send_reply(conn, dlg["id"], "Ответил!", None))

    # Client sends third message (mid=1004) in the exact same second -> NEW turn, MUST notify!
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 80801,
            "telegram_message_id": 1004,
            "username": "fast_user",
            "text": "Третье после ответа",
            "sent_at": same_ts,
            "direction": "in",
        },
    )
    assert len(calls) == 2

    # Manager marks dialog read in the exact same second
    mark_dialog_read(conn, dlg["id"])

    # Client sends fourth message (mid=1005) in the exact same second -> NEW message after read -> MUST notify!
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 80801,
            "telegram_message_id": 1005,
            "username": "fast_user",
            "text": "Четвёртое после прочтения",
            "sent_at": same_ts,
            "direction": "in",
        },
    )
    assert len(calls) == 3


def test_ensure_bot_reply_notifications_table_idempotent_migration(conn):
    # Simulate legacy table missing new columns
    conn.execute("DROP TABLE IF EXISTS bot_reply_notifications")
    conn.execute(
        """
        CREATE TABLE bot_reply_notifications (
            bot_message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            dialog_id INTEGER,
            lead_id INTEGER,
            account_id INTEGER,
            peer_id INTEGER,
            lead_handle TEXT,
            account_handle TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, bot_message_id)
        )
        """
    )
    conn.commit()
    ensure_bot_reply_notifications_table(conn)
    ensure_bot_reply_notifications_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bot_reply_notifications)").fetchall()}
    assert {
        "bot_message_id",
        "chat_id",
        "dialog_id",
        "lead_id",
        "account_id",
        "peer_id",
        "incoming_message_id",
        "incoming_sent_at",
        "turn_key",
    }.issubset(cols)


def test_cache_messages_direct_never_notifies_and_respects_newer_outgoing(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Cache History User",
        username="cache_hist_user",
        telegram_user_id=90901,
        status="waiting",
        account_id=acc_id,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 90901, 'cache_hist_user', 'Cache History User', 'telegram',
                  0, 0, '2026-11-15T16:10:00', 'Уже ответил', 0, ?, ?, ?)
        """,
        (acc_id, lead["id"], now, now),
    )
    dlg_id = conn.execute("SELECT id FROM dialogs WHERE peer_id = 90901").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 950, 'out', 'Уже ответил', '2026-11-15T16:10:00', ?, ?)
        """,
        (dlg_id, now, now),
    )
    conn.commit()

    # Loading historical incoming messages via _cache_messages MUST NOT flip last_from_peer=1 or notify
    _cache_messages(
        conn,
        dlg_id,
        [
            {
                "telegram_message_id": 949,
                "direction": "in",
                "text": "Старое входящее",
                "sent_at": "2026-11-15T16:05:00",
            }
        ],
    )
    dlg_row = conn.execute("SELECT last_from_peer, unread_count FROM dialogs WHERE id = ?", (dlg_id,)).fetchone()
    assert dlg_row["last_from_peer"] == 0
    assert dlg_row["unread_count"] == 0
    assert len(calls) == 0


def test_sync_inbox_read_inbox_max_id_advance_while_unread_no_duplicate_notify(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    create_lead(
        conn,
        name="Max ID User",
        username="max_id_user",
        telegram_user_id=91901,
        status="waiting",
        account_id=acc_id,
    )

    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 91901,
            "telegram_message_id": 100,
            "username": "max_id_user",
            "name": "Max ID User",
            "text": "Новое непрочитанное",
            "sent_at": "2026-11-15T17:00:00",
            "direction": "in",
        },
    )
    assert dlg["unread_count"] == 1
    assert len(calls) == 1

    # Telegram sync updates read_inbox_max_id=95 (previous history read), mid=100 is still unread
    sync_inbox(
        conn,
        acc_id,
        [
            {
                "telegram_user_id": 91901,
                "username": "max_id_user",
                "name": "Max ID User",
                "last_from_peer": 1,
                "last_message_preview": "Новое непрочитанное",
                "last_message_at": "2026-11-15T17:00:00",
                "unread_count": 1,
                "read_inbox_max_id": 95,
            }
        ],
    )
    assert len(calls) == 1


def test_send_lead_reply_notification_releases_conn_lock_around_http_and_serializes_concurrent_calls(conn, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Concurrent Lock User",
        username="conc_lock_user",
        telegram_user_id=92901,
        status="waiting",
        account_id=acc_id,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, lead_id, created_at, updated_at
        ) VALUES (?, 'user', 92901, 'conc_lock_user', 'Concurrent Lock User', 'telegram',
                  0, 1, '2026-11-15T17:10:00', 'Пинг', 1, ?, ?, ?)
        """,
        (acc_id, lead["id"], now, now),
    )
    dlg_id = conn.execute("SELECT id FROM dialogs WHERE peer_id = 92901").fetchone()["id"]
    conn.commit()

    in_http = threading.Event()
    allow_http_finish = threading.Event()
    calls = []
    call_guard = threading.Lock()

    def fake_opener(req, timeout=15):
        with call_guard:
            calls.append(len(calls) + 1)
            msg_num = len(calls) + 2000
        if msg_num == 2001:
            in_http.set()
            assert allow_http_finish.wait(timeout=2.0), "SQLite conn lock blocked DB thread while HTTP was running"
        return _FakeResp(msg_id=msg_num)

    def db_reader_worker():
        assert in_http.wait(timeout=2.0)
        with locked_conn(conn):
            val = conn.execute("SELECT 1").fetchone()[0]
            assert val == 1
        allow_http_finish.set()

    results = []
    t_http_1 = threading.Thread(
        target=lambda: results.append(
            send_lead_reply_notification(
                conn,
                lead_handle="@conc_lock_user",
                account_handle="@mgr_acc",
                dialog_id=dlg_id,
                lead_id=lead["id"],
                account_id=acc_id,
                peer_id=92901,
                telegram_message_id=101,
                opener=fake_opener,
            )
        )
    )
    t_db = threading.Thread(target=db_reader_worker)
    t_http_1.start()
    t_db.start()
    assert in_http.wait(timeout=2.0)

    # Concurrent second thread targeting the same dialog (via account_id + peer_id) waits on inflight_lock
    # and sees the committed notification once t_http_1 finishes, sending 0 extra HTTP requests.
    t_http_2 = threading.Thread(
        target=lambda: results.append(
            send_lead_reply_notification(
                conn,
                lead_handle="@conc_lock_user",
                account_handle="@mgr_acc",
                dialog_id=None,
                lead_id=lead["id"],
                account_id=acc_id,
                peer_id=92901,
                telegram_message_id=102,
                opener=fake_opener,
            )
        )
    )
    t_http_2.start()
    t_http_1.join(timeout=3.0)
    t_db.join(timeout=3.0)
    t_http_2.join(timeout=3.0)
    assert len(calls) == 1
    assert sorted(results) == [False, True]


def test_norm_ts_timezones_and_camp2_second_campaign_no_duplicate_reply_received_event(conn, monkeypatch):
    assert _norm_ts("2026-11-15T09:00:00+00:00") == "2026-11-15T09:00:00"
    assert _norm_ts("2026-11-15 09:00:00.123456Z") == "2026-11-15T09:00:00"
    assert _norm_ts("2026-11-15T12:00:00+03:00") == "2026-11-15T09:00:00"
    assert _norm_ts("2026-11-15T09:00:00") == "2026-11-15T09:00:00"

    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Multi Camp Single Event",
        username="camp2_dedup",
        telegram_user_id=93901,
        status="waiting",
        account_id=acc_id,
    )
    camp1 = create_campaign(conn, name="Camp 1", account_id=acc_id, steps=[{"template_text": "Hi 1"}])
    _add_recipient(conn, camp1["id"], account_id=acc_id, peer_id=93901, username="camp2_dedup", lead_id=lead["id"])

    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 93901,
            "telegram_message_id": 1101,
            "username": "camp2_dedup",
            "name": "Multi Camp Single Event",
            "text": "Да, давайте",
            "sent_at": "2026-11-15T18:00:00+00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Add a second campaign recipient later during the same active turn, then run _cache_messages & stop_outreach_on_reply
    camp2 = create_campaign(conn, name="Camp 2", account_id=acc_id, steps=[{"template_text": "Hi 2"}])
    rec2 = _add_recipient(conn, camp2["id"], account_id=acc_id, peer_id=93901, username="camp2_dedup", lead_id=lead["id"])
    _cache_messages(
        conn,
        dlg["id"],
        [
            {
                "telegram_message_id": 1101,
                "direction": "in",
                "text": "Да, давайте",
                "sent_at": "2026-11-15 18:00:00.123456Z",
            }
        ],
    )
    stop_outreach_on_reply(
        conn,
        account_id=acc_id,
        peer_id=93901,
        lead_id=lead["id"],
        preview="Да, давайте",
        source="sync_inbox",
    )
    assert conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec2,)).fetchone()["status"] == "replied"
    assert len(calls) == 1
    events = conn.execute(
        "SELECT * FROM lead_events WHERE lead_id = ? AND event_type = 'reply_received'",
        (lead["id"],),
    ).fetchall()
    assert len(events) == 1


def test_bot_reply_resolve_ignores_group_dialogs_and_multi_account_same_handle_independent(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc1 = _seed_account(conn, phone="+79991112233", username="mgr_one")
    acc2 = _seed_account(conn, phone="+79994445566", username="mgr_two")
    now = utc_now()

    # Group/channel chats sharing handles must NEVER be matched by _resolve_dialog_id_for_bot_reply
    chat_dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_from_peer, created_at, updated_at
        ) VALUES (?, 'chat', -100777, 'shared_handle', 'Shared Group', 'telegram',
                  1, 0, '2026-11-15T19:00:00', 1, ?, ?)
        """,
        (acc1, now, now),
    ).lastrowid
    chan_dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_from_peer, created_at, updated_at
        ) VALUES (?, 'channel', -100888, 'shared_handle', 'Shared Channel', 'telegram',
                  1, 0, '2026-11-15T19:05:00', 1, ?, ?)
        """,
        (acc2, now, now),
    ).lastrowid
    group_only_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_from_peer, created_at, updated_at
        ) VALUES (?, 'chat', -100999, 'only_group_chan', 'Group Only', 'telegram',
                  1, 0, '2026-11-15T19:10:00', 1, ?, ?)
        """,
        (acc1, now, now),
    ).lastrowid
    conn.commit()
    with monkeypatch.context() as mp:
        mp.setattr("app.bot_login.open_dialog_by_lead", lambda *a, **kw: None)
        assert (
            _resolve_dialog_id_for_bot_reply(
                conn,
                dialog_id=group_only_id,
                peer_id=-100999,
                lead_handle="@only_group_chan",
                account_id=acc1,
            )
            is None
        )

    # Private dialogs on acc1 and acc2 with same handle / peer_id stay independent
    lead1 = create_lead(
        conn,
        name="Shared User",
        username="shared_handle",
        telegram_user_id=94901,
        status="waiting",
        account_id=acc1,
    )
    dlg1 = apply_incoming(
        conn,
        acc1,
        {
            "telegram_user_id": 94901,
            "telegram_message_id": 1201,
            "username": "shared_handle",
            "text": "Пишу на первый аккаунт",
            "sent_at": "2026-11-15T18:30:00",
            "direction": "in",
        },
    )
    # Even though group/channel dialogs have newer last_message_at (19:00 / 19:05 > 18:30),
    # _resolve_dialog_id_for_bot_reply picks the private 'user' dialog!
    resolved_id = _resolve_dialog_id_for_bot_reply(conn, lead_handle="@shared_handle")
    assert resolved_id == dlg1["id"]
    assert resolved_id not in (chat_dlg_id, chan_dlg_id)
    assert len(calls) == 1

    # Same peer replies to acc2 -> independent dialog, MUST still notify once for acc2!
    dlg2 = apply_incoming(
        conn,
        acc2,
        {
            "telegram_user_id": 94901,
            "telegram_message_id": 1301,
            "username": "shared_handle",
            "text": "Пишу на второй аккаунт",
            "sent_at": "2026-11-15T18:35:00",
            "direction": "in",
        },
    )
    assert dlg2["id"] != dlg1["id"]
    assert len(calls) == 2

    # Repeating acc1 for same turn with lead1 stays deduped
    assert send_lead_reply_notification(
        conn,
        lead_handle="@shared_handle",
        account_handle="@mgr_one",
        dialog_id=dlg1["id"],
        lead_id=lead1["id"],
        account_id=acc1,
        peer_id=94901,
    ) is False
    assert len(calls) == 2


def test_camp2_cache_messages_after_mark_dialog_read_no_duplicate_reply_received_event(conn, monkeypatch):
    calls = _install_bot_spy(monkeypatch)
    acc_id = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Case4 Unminimised Lead",
        username="case4_unmin",
        telegram_user_id=95901,
        status="waiting",
        account_id=acc_id,
    )
    camp1 = create_campaign(conn, name="Camp 1", account_id=acc_id, steps=[{"template_text": "Step 1"}])
    _add_recipient(conn, camp1["id"], account_id=acc_id, peer_id=95901, username="case4_unmin", lead_id=lead["id"])

    dlg = apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 95901,
            "telegram_message_id": 777,
            "username": "case4_unmin",
            "text": "Первое",
            "sent_at": "2026-11-15T09:00:00",
            "direction": "in",
        },
    )
    apply_incoming(
        conn,
        acc_id,
        {
            "telegram_user_id": 95901,
            "telegram_message_id": 778,
            "username": "case4_unmin",
            "text": "Второе",
            "sent_at": "2026-11-15T09:00:10",
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Mark dialog read (increments reply_turn_seq=1, sets read_inbox_max_id=778, last_read_at=09:00:10)
    mark_dialog_read(conn, dlg["id"])

    # Camp 2 pending recipient added later, then _cache_messages caches already-read msg 778
    camp2 = create_campaign(conn, name="Camp 2", account_id=acc_id, steps=[{"template_text": "Step 2"}])
    rec2 = _add_recipient(conn, camp2["id"], account_id=acc_id, peer_id=95901, username="case4_unmin", lead_id=lead["id"])
    _cache_messages(
        conn,
        dlg["id"],
        [
            {
                "telegram_message_id": 778,
                "direction": "in",
                "text": "Второе",
                "sent_at": "2026-11-15T09:00:10",
            }
        ],
    )
    assert conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec2,)).fetchone()["status"] == "replied"
    assert len(calls) == 1
    events = conn.execute(
        "SELECT * FROM lead_events WHERE lead_id = ? AND event_type = 'reply_received'",
        (lead["id"],),
    ).fetchall()
    assert len(events) == 1
