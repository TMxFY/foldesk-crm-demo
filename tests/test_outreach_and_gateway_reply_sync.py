import asyncio
import json
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import events, tl
from telethon.tl.types import PeerUser, User

from app.db import connect, init_db, utc_now
from app.funnel_analytics import (
    ensure_funnel_columns,
    get_outreach_funnel,
    sync_dialog_read_receipts,
)
from app.outreach import (
    _dialog_has_client_reply,
    _sync_campaign_replies,
    create_campaign,
    run_campaign_batch,
)
from app.telegram_gateway import (
    FakeGateway,
    TelegramGateway,
    TelethonGateway,
    listening_event,
)
from app.whatsapp_gateway import WhatsAppGateway


class _FakeBotResp:
    def __init__(self):
        self.status = 200

    def read(self):
        return b'{"ok": true, "result": {"message_id": 777}}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_bot_spy(monkeypatch):
    calls = []

    def _fake_urlopen(req, timeout=None, context=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append({"url": req.full_url, "body": body})
        return _FakeBotResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return calls


def _seed_account(conn, *, phone="+79990001122", username="mgr_acc"):
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_username, telegram_name, session_path, status, health,
            daily_limit, created_at, updated_at
        ) VALUES (?, ?, 'Manager', 'data/sessions/test_mgr.session', 'connected', 'active', 40, ?, ?)
        """,
        (phone, username, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_dialog_has_client_reply_handles_none_dialog_id(tmp_path):
    conn = connect(tmp_path / "none_dlg.db")
    init_db(conn)
    rec = {"current_step": 1, "last_sent_at": "2026-11-12T10:00:00"}

    assert (
        _dialog_has_client_reply(
            conn,
            {"id": None, "last_from_peer": 0, "last_message_at": "2026-11-12T10:05:00"},
            rec,
        )
        is False
    )
    assert (
        _dialog_has_client_reply(
            conn,
            {"id": None, "last_from_peer": 1, "last_message_at": "2026-11-12T10:05:00"},
            rec,
        )
        is True
    )
    assert (
        _dialog_has_client_reply(
            conn,
            {"id": None, "last_from_peer": 1, "last_message_at": "2026-11-12T08:00:00"},
            rec,
        )
        is False
    )
    assert _dialog_has_client_reply(conn, {"id": None}, rec) is False


def test_sync_campaign_replies_and_batch_already_replied_no_bot_notification(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BOT_TOKEN", "bot-token-test")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    calls = _install_bot_spy(monkeypatch)

    conn = connect(tmp_path / "sync_reply.db")
    init_db(conn)
    acc_id = _seed_account(conn)
    camp = create_campaign(
        conn,
        name="Outreach Sync Test",
        account_id=acc_id,
        status="running",
        steps=[
            {"template_text": "Step 1", "delay_hours": 0},
            {"template_text": "Step 2", "delay_hours": 24},
        ],
    )

    now = utc_now()
    # Case A: client replied at 09:00, manager ALREADY replied at 09:05 (last_from_peer=0, unread_count=0)
    r1 = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, status, current_step,
            sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 4101, 'already_handled', 'Handled', 'in_progress', 1, ?, '2026-11-12T08:00:00', ?, ?)
        """,
        (camp["id"], acc_id, now, now),
    ).lastrowid
    d1 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 4101, 'already_handled', 'Handled', 0, '2026-11-12T09:05:00', 'Спасибо, напишу', 0, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (d1, 1, "out", "Step 1", "2026-11-12T08:00:00", now, now),
            (d1, 2, "in", "Да, интересно", "2026-11-12T09:00:00", now, now),
            (d1, 3, "out", "Спасибо, напишу", "2026-11-12T09:05:00", now, now),
        ],
    )

    # Case B: stale dialog flags (last_from_peer=1, unread_count=1), BUT latest message in messages is 'out'
    r2 = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, status, current_step,
            sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 4102, 'stale_flags', 'Stale', 'in_progress', 1, ?, '2026-11-12T08:00:00', ?, ?)
        """,
        (camp["id"], acc_id, now, now),
    ).lastrowid
    d2 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 4102, 'stale_flags', 'Stale', 1, '2026-11-12T09:00:00', 'Да', 1, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (d2, 10, "out", "Step 1", "2026-11-12T08:00:00", now, now),
            (d2, 11, "in", "Да", "2026-11-12T09:00:00", now, now),
            (d2, 12, "out", "Менеджер ответил", "2026-11-12T09:05:00", now, now),
        ],
    )

    # Case C: already read (last_from_peer=1, unread_count=0)
    r3 = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, status, current_step,
            sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 4103, 'already_read', 'Read', 'in_progress', 1, ?, '2026-11-12T08:00:00', ?, ?)
        """,
        (camp["id"], acc_id, now, now),
    ).lastrowid
    d3 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 4103, 'already_read', 'Read', 1, '2026-11-12T09:00:00', 'Читаю', 0, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (d3, 20, "out", "Step 1", "2026-11-12T08:00:00", now, now),
            (d3, 21, "in", "Читаю", "2026-11-12T09:00:00", now, now),
        ],
    )
    conn.commit()

    synced = _sync_campaign_replies(conn, camp["id"], acc_id)
    assert synced == 3
    for rid in (r1, r2, r3):
        st = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rid,)).fetchone()["status"]
        assert st == "replied"
    assert len(calls) == 0

    # Case D: run_campaign_batch live check also sees in -> out and stops outreach WITHOUT notifying bot
    r4 = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, status, current_step,
            sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 4104, 'batch_handled', 'BatchHandled', 'in_progress', 1, ?, '2026-11-10T08:00:00', ?, ?)
        """,
        (camp["id"], acc_id, now, now),
    ).lastrowid
    d4 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, last_message_preview, unread_count, created_at, updated_at
        ) VALUES (?, 'user', 4104, 'batch_handled', 'BatchHandled', 0, '2026-11-10T08:00:00', 'Step 1', 0, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 100, 'out', 'Step 1', '2026-11-10T08:00:00', ?, ?)
        """,
        (d4, now, now),
    )
    conn.commit()

    gw = FakeGateway(
        history={
            4104: [
                {"telegram_message_id": 100, "direction": "out", "text": "Step 1", "sent_at": "2026-11-10T08:00:00"},
                {"telegram_message_id": 101, "direction": "in", "text": "Ок давайте", "sent_at": "2026-11-10T09:00:00"},
                {"telegram_message_id": 102, "direction": "out", "text": "Отлично!", "sent_at": "2026-11-10T09:05:00"},
            ]
        }
    )
    batch_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10))
    assert batch_res["sent"] == 0
    r4_status = conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (r4,)).fetchone()["status"]
    assert r4_status == "replied"
    assert len(calls) == 0


def test_telethon_watch_private_outgoing_inbox_read_and_mark_dialog_read():
    gw = TelethonGateway()
    me = User(id=1001, first_name="Manager", username="mgr", is_self=True)
    peer = User(id=5001, first_name="Alice", username="alice_client")

    class _FakeClient:
        def __init__(self):
            self.handlers = []
            self.read_acks = []

        def add_event_handler(self, cb, builder):
            self.handlers.append((cb, builder))

        def remove_event_handler(self, cb):
            self.handlers = [(c, b) for c, b in self.handlers if c != cb]

        async def catch_up(self):
            return None

        async def run_until_disconnected(self):
            await asyncio.Future()

        async def get_entity(self, target):
            assert int(target) == 5001
            return peer

        async def send_read_acknowledge(self, entity, message=None, *, max_id=None, **_kwargs):
            self.read_acks.append((entity, max_id))
            return True

    fake_client = _FakeClient()

    async def _run():
        async def _open(_sess):
            return fake_client, False

        gw._open = _open

        incoming_seen = []
        outgoing_seen = []
        read_seen = []

        async def _on_msg(p):
            incoming_seen.append(p)

        async def _on_out(p):
            outgoing_seen.append(p)

        async def _on_read(p):
            read_seen.append(p)

        task = asyncio.create_task(
            gw.watch_private("sess", _on_msg, on_read=_on_read, on_outgoing=_on_out)
        )
        await asyncio.wait_for(listening_event(gw, "sess").wait(), timeout=2)

        raw_cb = None
        out_cb = None
        for cb, builder in fake_client.handlers:
            if builder is events.Raw or isinstance(builder, events.Raw):
                raw_cb = cb
            elif isinstance(builder, events.NewMessage) and getattr(builder, "outgoing", False):
                out_cb = cb

        assert raw_cb is not None
        assert out_cb is not None

        # 1. Outgoing event: get_sender() returns me, get_chat() returns peer
        out_ev = SimpleNamespace(
            is_private=True,
            is_group=False,
            is_channel=False,
            message=SimpleNamespace(
                id=888,
                message="Отправил с телефона",
                date=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc),
                edit_date=None,
                out=True,
                reply_to=None,
                entities=None,
                peer_id=PeerUser(user_id=5001),
            ),
            get_sender=lambda: asyncio.sleep(0, result=me),
            get_chat=lambda: asyncio.sleep(0, result=peer),
        )
        await out_cb(out_ev)
        assert len(incoming_seen) == 0
        assert len(outgoing_seen) == 1
        assert outgoing_seen[0]["direction"] == "out"
        assert outgoing_seen[0]["telegram_user_id"] == 5001
        assert outgoing_seen[0]["username"] == "alice_client"
        assert outgoing_seen[0]["text"] == "Отправил с телефона"

        # 2. UpdateReadHistoryInbox -> on_read with read_inbox_max_id & unread_count=0
        inbox_ev = tl.types.UpdateReadHistoryInbox(
            peer=PeerUser(user_id=5001),
            max_id=120,
            still_unread_count=0,
            pts=10,
            pts_count=1,
        )
        await raw_cb(inbox_ev)
        assert read_seen[-1] == {
            "telegram_user_id": 5001,
            "read_inbox_max_id": 120,
            "unread_count": 0,
        }

        # 3. UpdateReadHistoryOutbox preserved
        outbox_ev = tl.types.UpdateReadHistoryOutbox(
            peer=PeerUser(user_id=5001),
            max_id=125,
            pts=11,
            pts_count=1,
        )
        await raw_cb(outbox_ev)
        assert read_seen[-1] == {
            "telegram_user_id": 5001,
            "read_outbox_max_id": 125,
        }

        # 4. mark_dialog_read
        ack_ok = await gw.mark_dialog_read("sess", 5001, max_id=120)
        assert ack_ok is True
        assert fake_client.read_acks == [(peer, 120)]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_client.handlers == []

    asyncio.run(_run())

    # Base TelegramGateway returns False cleanly
    assert asyncio.run(TelegramGateway.mark_dialog_read(gw, "sess", 5001, max_id=1)) is False


def test_fake_gateway_outgoing_and_mark_dialog_read():
    fgw = FakeGateway(
        dialogs=[
            {
                "telegram_user_id": 5001,
                "username": "alice",
                "name": "Alice",
                "unread_count": 3,
                "last_from_peer": 1,
            }
        ]
    )

    async def _run():
        out_seen = []
        fgw.push_outgoing(
            "sess",
            {
                "telegram_user_id": 5001,
                "telegram_message_id": 77,
                "text": "Привет!",
                "direction": "out",
            },
        )
        task = asyncio.create_task(
            fgw.watch_private("sess", lambda _m: None, on_outgoing=lambda m: out_seen.append(m))
        )
        await asyncio.wait_for(listening_event(fgw, "sess").wait(), timeout=2)
        await asyncio.sleep(0.02)
        assert len(out_seen) == 1
        assert out_seen[0]["direction"] == "out"
        assert out_seen[0]["text"] == "Привет!"

        ok = await fgw.mark_dialog_read("sess", 5001, max_id=77)
        assert ok is True
        assert fgw.read_acks[-1]["peer_id"] == 5001
        assert fgw.read_acks[-1]["max_id"] == 77
        assert fgw.dialogs[0]["unread_count"] == 0

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_whatsapp_gateway_outgoing_subscription(tmp_path):
    wgw = WhatsAppGateway()
    sess = str(tmp_path / "wa.session")

    async def _run():
        in_seen = []
        out_seen = []
        wgw.push_outgoing(
            sess,
            {
                "telegram_user_id": 79001112233,
                "phone": "+79001112233",
                "wa_jid": "79001112233@s.whatsapp.net",
                "telegram_message_id": 9001,
                "text": "С телефона WA",
                "direction": "out",
            },
        )
        task = asyncio.create_task(
            wgw.watch_private(
                sess,
                lambda m: in_seen.append(m),
                on_outgoing=lambda m: out_seen.append(m),
            )
        )
        await asyncio.wait_for(listening_event(wgw, sess).wait(), timeout=2)
        await asyncio.sleep(0.02)
        assert in_seen == []
        assert len(out_seen) == 1
        assert out_seen[0]["direction"] == "out"
        assert out_seen[0]["text"] == "С телефона WA"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


def test_funnel_analytics_in_before_out_not_misclassified_and_null_safe(tmp_path):
    conn = connect(tmp_path / "funnel_edge.db")
    init_db(conn)
    ensure_funnel_columns(conn)
    ensure_funnel_columns(conn)

    now = utc_now()
    acc_id = _seed_account(conn)
    cid = conn.execute(
        """
        INSERT INTO campaigns (name, account_id, account_ids_json, daily_limit, status, created_at, updated_at)
        VALUES ('B2B Funnel Edge', ?, ?, 30, 'running', ?, ?)
        """,
        (acc_id, f"[{acc_id}]", now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at) VALUES (?, 1, 0, 'Hi', ?)",
        (cid, now),
    )

    # Lead 1: incoming BEFORE outgoing ('in' at 08:00, 'out' at 09:00, last_sent_at=09:00) -> sent_unread, NOT replied!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 8101, 'in_then_out', 'InFirst', 'InFirstCo', 'Hi', 1, 'in_progress', '2026-11-12T09:00:00', ?, ?)
        """,
        (cid, now, now),
    )
    d1 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, unread_count, read_outbox_max_id, created_at, updated_at
        ) VALUES (?, 'user', 8101, 'in_then_out', 'InFirst', 0, '2026-11-12T09:00:00', 0, 0, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (d1, 1, "in", "Old inbound before outreach", "2026-11-12T08:00:00", now, now),
            (d1, 2, "out", "Hi", "2026-11-12T09:00:00", now, now),
        ],
    )

    # Lead 2: out (08:00) -> in (09:00) -> out (09:05) -> replied_no_call with preserved client inbound text
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 8102, 'out_in_out', 'OutInOut', 'OutInOutCo', 'Hi', 1, 'replied', '2026-11-12T08:00:00', ?, ?)
        """,
        (cid, now, now),
    )
    d2 = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, last_from_peer,
            last_message_at, unread_count, read_outbox_max_id, created_at, updated_at
        ) VALUES (?, 'user', 8102, 'out_in_out', 'OutInOut', 0, '2026-11-12T09:05:00', 0, 3, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (d2, 1, "out", "Hi", "2026-11-12T08:00:00", now, now),
            (d2, 2, "in", "Расскажите подробнее про условия", "2026-11-12T09:00:00", now, now),
            (d2, 3, "out", "Уже ответил менеджер", "2026-11-12T09:05:00", now, now),
        ],
    )
    conn.commit()

    funnel = get_outreach_funnel(conn, campaign_id=cid)
    by_user = {row["username"]: row for row in funnel["leads"]}
    assert by_user["in_then_out"]["stage"] == "sent_unread"
    assert by_user["in_then_out"]["last_inbound_text"] == ""
    assert funnel["summary"]["replied_total"] == 1
    assert by_user["out_in_out"]["stage"] == "replied_no_call"
    assert by_user["out_in_out"]["last_inbound_text"] == "Расскажите подробнее про условия"

    # Malformed sync_dialog_read_receipts payload never crashes
    assert sync_dialog_read_receipts(conn, acc_id, [{"telegram_user_id": None, "read_outbox_max_id": "bad"}]) == 0



def test_sync_campaign_replies_notifies_when_unread_and_awaiting_reply(tmp_path, monkeypatch):
    conn = connect(tmp_path / "sync_unread_notify.db")
    init_db(conn)
    bot_calls = _install_bot_spy(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "bot-token-test")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")

    now = utc_now()
    acc_id = _seed_account(conn)
    camp = create_campaign(
        conn,
        name="Unread Notify Positive",
        account_id=acc_id,
        status="running",
        steps=[{"delay_hours": 0, "template_text": "Добрый день!"}],
    )
    cid = int(camp["id"])
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 6002, 'unread_lead', 'Борис', 'BetaCo', 1, 'in_progress', ?, '2026-11-12T09:00:00', ?, ?)
        """,
        (cid, acc_id, now, now),
    )
    dlg_id = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title,
            last_message_preview, last_message_at, last_from_peer, unread_count,
            created_at, updated_at
        ) VALUES (?, 'user', 6002, 'unread_lead', 'Борис', 'Да, интересно!', '2026-11-12T09:05:00', 1, 2, ?, ?)
        """,
        (acc_id, now, now),
    ).lastrowid
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (dlg_id, 201, "out", "Добрый день!", "2026-11-12T09:00:00", now, now),
            (dlg_id, 202, "in", "Да, интересно!", "2026-11-12T09:05:00", now, now),
        ],
    )
    conn.commit()

    replied = _sync_campaign_replies(conn, cid, account_id=acc_id)
    assert replied == 1
    rec_row = conn.execute(
        "SELECT status FROM campaign_recipients WHERE campaign_id = ?", (cid,)
    ).fetchone()
    assert rec_row["status"] == "replied"
    assert len(bot_calls) == 1
    assert "@unread_lead" in bot_calls[0]["body"]["text"]


def test_telethon_load_private_extracts_read_inbox_and_last_from_peer():
    gw = TelethonGateway()
    alice = User(id=5001, username="alice", first_name="Alice")
    bob = User(id=5002, username="bob", first_name="Bob")

    class _DlgClient:
        async def iter_dialogs(self):
            yield SimpleNamespace(
                is_user=True,
                entity=alice,
                name="Alice",
                unread_count=3,
                message=SimpleNamespace(
                    id=90,
                    message="Прочитано с телефона",
                    date=datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc),
                    out=False,
                ),
                dialog=SimpleNamespace(
                    read_inbox_max_id=90,
                    read_outbox_max_id=50,
                    unread_mark=False,
                ),
            )
            yield SimpleNamespace(
                is_user=True,
                entity=bob,
                name="Bob",
                unread_count=2,
                message=SimpleNamespace(
                    id=101,
                    message="Новое непрочитанное",
                    date=datetime(2026, 11, 12, 10, 5, tzinfo=timezone.utc),
                    out=False,
                ),
                dialog=SimpleNamespace(
                    read_inbox_max_id=95,
                    read_outbox_max_id=80,
                    unread_mark=False,
                ),
            )

    rows = asyncio.run(gw._load_private(_DlgClient(), 10))
    by_id = {r["telegram_user_id"]: r for r in rows}
    assert by_id[5001]["last_from_peer"] == 1
    assert by_id[5001]["read_inbox_max_id"] == 90
    assert by_id[5001]["read_outbox_max_id"] == 50
    assert by_id[5001]["unread_count"] == 0
    assert by_id[5002]["unread_count"] == 2
    assert by_id[5002]["read_inbox_max_id"] == 95


def test_sync_campaign_replies_respects_newer_followup_and_tz_normalization(tmp_path, monkeypatch):
    from app.outreach import _stop_outreach_from_dialog_sync

    conn = connect(tmp_path / "min_sent_tz.db")
    init_db(conn)
    bot_calls = _install_bot_spy(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "bot-token-test")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")

    now = utc_now()
    acc_id = _seed_account(conn)
    lead_id = int(
        conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, status, created_at, updated_at
            ) VALUES (?, 7101, 'tz_lead', 'Тимур', 'waiting', ?, ?)
            """,
            (acc_id, now, now),
        ).lastrowid
    )
    camp_old = create_campaign(
        conn,
        name="Old Campaign",
        account_id=acc_id,
        status="running",
        steps=[{"delay_hours": 0, "template_text": "Step 1"}],
    )
    camp_new = create_campaign(
        conn,
        name="Newer Followup Campaign",
        account_id=acc_id,
        status="running",
        steps=[{"delay_hours": 0, "template_text": "Follow-up"}],
    )
    rec_old_id = int(
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                current_step, status, sent_account_id, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, 7101, 'tz_lead', 'Тимур', 1, 'in_progress', ?, '2026-11-15T08:00:00Z', ?, ?)
            """,
            (camp_old["id"], lead_id, acc_id, now, now),
        ).lastrowid
    )
    rec_new_id = int(
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                current_step, status, sent_account_id, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, 7101, 'tz_lead', 'Тимур', 1, 'in_progress', ?, '2026-11-15T12:00:00+00:00', ?, ?)
            """,
            (camp_new["id"], lead_id, acc_id, now, now),
        ).lastrowid
    )
    dlg_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, lead_id, peer_type, peer_id, username, title,
                last_message_preview, last_message_at, last_from_peer, unread_count,
                created_at, updated_at
            ) VALUES (?, ?, 'user', 7101, 'tz_lead', 'Тимур', 'Новый фоллоу-ап', '2026-11-15T12:00:00', 0, 0, ?, ?)
            """,
            (acc_id, lead_id, now, now),
        ).lastrowid
    )
    conn.executemany(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (dlg_id, 1, "out", "Step 1", "2026-11-15T08:00:00Z", now, now),
            (dlg_id, 2, "in", "Старый ответ в +03:00", "2026-11-15T12:00:00+03:00", now, now),
            (dlg_id, 3, "out", "Ответили ранее", "2026-11-15T09:05:00+00:00", now, now),
            (dlg_id, 4, "out", "Новый фоллоу-ап", "2026-11-15T12:00:00+00:00", now, now),
        ],
    )
    conn.commit()

    # 12:00+03:00 is 09:00 UTC -> strictly BEFORE rec_new's 12:00 UTC follow-up!
    rec_new_dict = dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE id = ?", (rec_new_id,)).fetchone()
    )
    dlg_dict = dict(conn.execute("SELECT * FROM dialogs WHERE id = ?", (dlg_id,)).fetchone())
    assert _dialog_has_client_reply(conn, dlg_dict, rec_new_dict) is False

    # Even if forced into _stop_outreach_from_dialog_sync with notify=False, newer rec_new MUST stay in_progress
    assert (
        _stop_outreach_from_dialog_sync(
            conn, rec_new_dict, dlg_dict, account_id=acc_id, notify=False
        )
        is False
    )
    assert (
        _sync_campaign_replies(conn, camp_new["id"], account_id=acc_id) == 0
    )
    assert (
        conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec_new_id,)).fetchone()[
            "status"
        ]
        == "in_progress"
    )

    # Syncing the older campaign (08:00Z < 09:00 UTC reply) stops rec_old quietly WITHOUT stopping rec_new or spamming bot
    assert _sync_campaign_replies(conn, camp_old["id"], account_id=acc_id) == 1
    assert (
        conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec_old_id,)).fetchone()[
            "status"
        ]
        == "replied"
    )
    assert (
        conn.execute("SELECT status FROM campaign_recipients WHERE id = ?", (rec_new_id,)).fetchone()[
            "status"
        ]
        == "in_progress"
    )
    assert len(bot_calls) == 0

    # Timezone offset sanity: 11:00+03:00 == 08:00 UTC < 09:00 UTC
    assert (
        _dialog_has_client_reply(
            conn,
            {"id": None, "last_from_peer": 1, "last_message_at": "2026-11-15T11:00:00+03:00"},
            {"current_step": 1, "last_sent_at": "2026-11-15T09:00:00"},
        )
        is False
    )


def test_whatsapp_gateway_synthetic_message_ids_never_collide_at_5001(tmp_path):
    from app.telegram_gateway import GatewayError
    from real_session_helpers import make_protocol_wa_client_factory, make_real_whatsmeow_db

    s1 = tmp_path / "wa1.session"
    s2 = tmp_path / "wa2.session"
    make_real_whatsmeow_db(s1, jid="31611110001:1@s.whatsapp.net")
    make_real_whatsmeow_db(s2, jid="31611110002:1@s.whatsapp.net")

    factory, records = make_protocol_wa_client_factory(registered_phones={"+79001112233"})
    gw1 = WhatsAppGateway(client_factory=factory)
    gw2 = WhatsAppGateway(client_factory=factory)

    async def _run():
        # Unauthenticated session path must fail fast instead of returning fake IDs
        with pytest.raises(GatewayError):
            await gw1.send_message(str(tmp_path / "unauth.session"), "+79001112233", "must fail")

        r1 = await gw1.send_message(str(s1), "+79001112233", "msg 1")
        r2 = await gw2.send_message(str(s2), "+79001112233", "msg 2")
        r3 = await gw1.send_message(str(s1), "+79001112233", "msg 3")
        return r1["telegram_message_id"], r2["telegram_message_id"], r3["telegram_message_id"]

    id1, id2, id3 = asyncio.run(_run())
    assert len(records["sent_messages"]) == 3
    assert id1 >= 1_000_000_000
    assert id2 >= 1_000_000_000
    assert id3 >= 1_000_000_000
    assert id1 != 5001 and id2 != 5001 and id3 != 5001
    assert len({id1, id2, id3}) == 3
    assert id1 < id2 < id3
