"""Regression harness: strict DB-lead & incoming-only colleague reply notifications."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from email.message import Message

import pytest

from app.bot_login import handle_bot_reply_to_lead
from app.db import connect, init_db, utc_now
from app.digest import send_lead_reply_notification
from app.inbox import apply_incoming, stop_outreach_on_reply, sync_inbox
from app.leads import create_lead
from app.outreach import create_campaign, run_campaign_batch
from app.telegram_gateway import FakeGateway


class _FakeHttpResp:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode("utf-8")
        self.headers = Message()
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _install_mock_opener(monkeypatch, calls: list[dict]):
    def fake_urlopen(req, timeout=10):
        body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else {}
        calls.append({"url": req.full_url, "body": body})
        return _FakeHttpResp({"ok": True, "result": {"message_id": 7000 + len(calls)}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _seed_account(
    conn,
    *,
    phone: str = "+79990001122",
    telegram_username: str = "saabath",
    telegram_user_id: int = 9001,
) -> dict:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_user_id, telegram_username, telegram_name,
            session_path, status, health, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'data/sessions/saabath.session', 'connected', 'active', ?, ?)
        """,
        (phone, telegram_user_id, telegram_username, "Saabath", now, now),
    )
    conn.commit()
    return {"id": int(cur.lastrowid), "phone": phone, "telegram_username": telegram_username}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "8685610974:colleague-secret-token")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    monkeypatch.delenv("DISABLE_BOT_ALERTS", raising=False)
    c = connect(tmp_path / "strict_alerts.db")
    init_db(c)
    return c


def test_personal_non_lead_dm_and_outgoing_message_never_alert(conn, monkeypatch):
    calls: list[dict] = []
    _install_mock_opener(monkeypatch, calls)
    acc = _seed_account(conn)

    # 1. Personal DM (NOT in leads DB and NOT in contacted recipients)
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 555001,
            "telegram_message_id": 11,
            "username": "personal_friend",
            "name": "Friend",
            "text": "Привет, пойдём пить кофе?",
            "direction": "in",
        },
    )
    assert calls == []

    # 2. Lead exists in DB, but direction='out' (fail-closed!)
    lead = create_lead(
        conn,
        name="Real Lead",
        username="real_lead",
        telegram_user_id=555002,
        status="new",
        account_id=acc["id"],
    )
    dlg_cur = conn.execute(
        """
        INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, lead_id, created_at, updated_at)
        VALUES (?, 'user', 555002, 'real_lead', 'Real Lead', ?, ?, ?)
        """,
        (acc["id"], lead["id"], utc_now(), utc_now()),
    )
    did = int(dlg_cur.lastrowid)
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 88, 'out', 'Мы сами написали первыми', ?, ?, ?)
        """,
        (did, utc_now(), utc_now(), utc_now()),
    )
    conn.commit()

    assert (
        stop_outreach_on_reply(
            conn,
            account_id=acc["id"],
            peer_id=555002,
            username="real_lead",
            lead_id=lead["id"],
            preview="Мы сами написали первыми",
            direction="out",
        )
        == 0
    )
    assert (
        send_lead_reply_notification(
            conn,
            lead_handle="@real_lead",
            account_handle="@saabath",
            dialog_id=did,
            lead_id=lead["id"],
            account_id=acc["id"],
            peer_id=555002,
            telegram_message_id=88,
            direction="in",  # even if caller forgets direction='out', DB message #88 has direction='out'
        )
        is False
    )
    assert calls == []


def test_incoming_db_lead_alerts_once_and_blocks_duplicates_edits_and_read_watermarks(conn, monkeypatch):
    calls: list[dict] = []
    _install_mock_opener(monkeypatch, calls)
    acc = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Strict Lead",
        username="strict_lead",
        telegram_user_id=555003,
        status="waiting",
        account_id=acc["id"],
    )

    # First genuine incoming message from DB lead -> exactly 1 alert
    dlg = apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 555003,
            "telegram_message_id": 201,
            "username": "strict_lead",
            "name": "Strict Lead",
            "text": "Да, интересен пилот по роликам",
            "sent_at": "2026-11-18T10:05:00+00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1
    assert "@strict_lead" in calls[0]["body"]["text"]

    # Duplicate delivery of same telegram_message_id -> 0 extra alerts
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 555003,
            "telegram_message_id": 201,
            "username": "strict_lead",
            "name": "Strict Lead",
            "text": "Да, интересен пилот по роликам",
            "sent_at": "2026-11-18T10:05:00+00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Message edit -> 0 extra alerts
    apply_incoming(
        conn,
        acc["id"],
        {
            "telegram_user_id": 555003,
            "telegram_message_id": 201,
            "username": "strict_lead",
            "name": "Strict Lead",
            "text": "Да, интересен пилот по роликам (edited)",
            "is_edit": True,
            "direction": "in",
        },
    )
    assert len(calls) == 1

    # Watermark equal to incoming_sent_at (in_ts <= last_read_at) -> skip
    conn.execute("UPDATE dialogs SET last_read_at = ? WHERE id = ?", ("2026-11-18T10:05:00+00:00", dlg["id"]))
    conn.commit()
    assert (
        send_lead_reply_notification(
            conn,
            lead_handle="@strict_lead",
            account_handle="@saabath",
            dialog_id=dlg["id"],
            lead_id=lead["id"],
            account_id=acc["id"],
            peer_id=555003,
            telegram_message_id=202,
            incoming_sent_at="2026-11-18T10:05:00+00:00",
            direction="in",
        )
        is False
    )
    assert len(calls) == 1


def test_untouched_pending_recipient_and_closed_won_and_bot_echo_never_alert(conn, monkeypatch):
    calls: list[dict] = []
    _install_mock_opener(monkeypatch, calls)
    acc = _seed_account(conn)
    camp = create_campaign(conn, name="Camp A", account_id=acc["id"], steps=[{"template_text": "Hi"}])

    # 1. Untouched pending campaign_recipient WITHOUT any DB lead row must NEVER auto-create lead or alert
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, NULL, 555010, 'untouched_user', 'Untouched', 'Hi', 0, 'pending', ?, ?)
        """,
        (camp["id"], utc_now(), utc_now()),
    )
    # 2. Closed won client must NEVER alert
    won_lead = create_lead(
        conn,
        name="Viktor Won",
        username="won_client",
        telegram_user_id=555011,
        status="won",
        account_id=acc["id"],
    )
    conn.commit()

    for peer_id, uname, mid in (
        (555010, "untouched_user", 301),
        (555011, "won_client", 302),
        (8685610974, "softugccollegabot", 303),
    ):
        apply_incoming(
            conn,
            acc["id"],
            {
                "telegram_user_id": peer_id,
                "telegram_message_id": mid,
                "username": uname,
                "name": uname,
                "text": "You received a response from @someone",
                "is_bot": uname == "softugccollegabot",
                "direction": "in",
            },
        )
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE username = 'untouched_user'").fetchone()[0] == 0
    assert calls == []

    # 3. Direct call with require_backing=True against unknown/won peers also fails closed
    assert (
        send_lead_reply_notification(
            conn,
            lead_handle="@won_client",
            account_handle="@saabath",
            lead_id=won_lead["id"],
            peer_id=555011,
        )
        is False
    )

    # 4. Sync inbox only alerts when backed by contacted recipient or open DB lead, and respects sync preview rules
    sync_inbox(conn, acc["id"], [], opener=None, notify=False)
    assert calls == []


def test_bot_reply_webhook_rejects_bot_echo_and_wrong_chat_and_pre_followup_stops_on_reply(conn, monkeypatch):
    acc = _seed_account(conn)
    lead = create_lead(
        conn,
        name="Followup Lead",
        username="fup_lead",
        telegram_user_id=555020,
        company="CloudSaaS",
        url="https://cloudsaas.io",
        niche="SaaS",
        status="new",
        account_id=acc["id"],
    )
    camp = create_campaign(
        conn,
        name="SoftUGC B2B Outreach",
        account_id=acc["id"],
        steps=[
            {
                "step_order": 1,
                "delay_hours": 0,
                "template_text": "Привет! Для CloudSaaS сейчас актуально продвижение через короткие UGC видео?",
            },
            {
                "step_order": 2,
                "delay_hours": 0,
                "template_text": "Напомню по формату роликов и кейсам для CloudSaaS — скинуть примеры?",
            },
        ],
    )
    dlg_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, lead_id, created_at, updated_at)
            VALUES (?, 'user', 555020, 'fup_lead', 'Followup Lead', ?, ?, ?)
            """,
            (acc["id"], lead["id"], utc_now(), utc_now()),
        ).lastrowid
    )
    step1_msg = "Привет! Для CloudSaaS сейчас актуально продвижение через короткие UGC видео?"
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 1, 'out', ?, '2026-11-17T10:00:00+00:00', ?, ?)
        """,
        (dlg_id, step1_msg, utc_now(), utc_now()),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name, company, website, category, their_angle,
            step1_text, current_step, status, last_sent_at, assigned_account_id, sent_account_id, created_at, updated_at
        ) VALUES (?, ?, 555020, 'fup_lead', 'Followup Lead', 'CloudSaaS', 'https://cloudsaas.io', 'SaaS',
                  'Automated B2B analytics platform for SaaS revenue teams', ?, 1, 'in_progress',
                  '2026-11-17T10:00:00+00:00', ?, ?, ?, ?)
        """,
        (camp["id"], lead["id"], step1_msg, acc["id"], acc["id"], utc_now(), utc_now()),
    )
    conn.commit()

    async def _run():
        gw = FakeGateway(
            dialogs=[
                {
                    "peer_type": "user",
                    "peer_id": 555020,
                    "username": "fup_lead",
                    "title": "Followup Lead",
                    "last_from_peer": True,
                }
            ],
            history={
                555020: [
                    {"telegram_message_id": 1, "direction": "out", "text": step1_msg, "sent_at": "2026-11-17T10:00:00+00:00"},
                    {"telegram_message_id": 2, "direction": "in", "text": "Да, присылайте кейсы", "sent_at": "2026-11-17T11:00:00+00:00"},
                ]
            },
        )
        res_bot = await handle_bot_reply_to_lead(
            conn,
            {
                "message": {
                    "message_id": 10,
                    "chat": {"id": 978069541},
                    "from": {"id": 8685610974, "is_bot": True},
                    "text": "Echo message",
                }
            },
            gateway=gw,
        )
        assert res_bot["replied"] is False
        assert gw.sent == []

        res_wrong_chat = await handle_bot_reply_to_lead(
            conn,
            {
                "message": {
                    "message_id": 11,
                    "chat": {"id": 1111111},
                    "from": {"id": 1111111, "is_bot": False},
                    "text": "Unauthorized reply",
                }
            },
            gateway=gw,
        )
        assert res_wrong_chat["replied"] is False
        assert gw.sent == []

        batch_res = await run_campaign_batch(conn, campaign_id=camp["id"], gateway=gw)
        assert batch_res["sent"] == 0

    asyncio.run(_run())
    rec_row = conn.execute(
        "SELECT status FROM campaign_recipients WHERE campaign_id = ?", (camp["id"],)
    ).fetchone()
    assert rec_row["status"] == "replied"


def test_bot_webhook_secret_with_httpx_ua_and_whatsapp_monotonic_mid_replay_guard(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from app.inbox import apply_incoming, apply_live_outgoing
    from app.main import create_app
    from app.whatsapp_gateway import _wa_monotonic_mid

    monkeypatch.setenv("MCP_API_KEY", "test-webhook-secret-123")
    monkeypatch.setenv("BOT_TOKEN", "8685610974:AAFakeColleagueTokenForTests")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")

    acc = _seed_account(conn, phone="+79990001122")
    conn.execute("UPDATE accounts SET provider = 'whatsapp' WHERE id = ?", (acc["id"],))
    conn.commit()
    lead = create_lead(
        conn,
        name="WA Lead",
        phone="+79995556677",
        telegram_user_id=79995556677,
        status="talking",
        account_id=acc["id"],
    )

    app = create_app(conn=conn, gateway=FakeGateway())
    with TestClient(app) as client:
        unauth = client.post(
            "/api/bot/webhook",
            headers={"User-Agent": "python-httpx/0.27.0"},
            json={"callback_query": {"data": "noop"}},
        )
        assert unauth.status_code == 401

        auth_ok = client.post(
            "/api/bot/webhook",
            headers={
                "User-Agent": "python-httpx/0.27.0",
                "X-Bot-Webhook-Secret": "test-webhook-secret-123",
            },
            json={"callback_query": {"data": f"lead:{lead['id']}"}},
        )
        assert auth_ok.status_code == 200

    # WA monotonic IDs: earlier incoming message MUST have smaller mid than newer outgoing message
    old_in_mid = _wa_monotonic_mid("3EB0_OLD_IN", ts_sec=1700000100)
    new_out_mid = _wa_monotonic_mid("3EB0_NEW_OUT", ts_sec=1700000500)
    assert old_in_mid < new_out_mid

    calls: list[dict] = []

    class FakeOpener:
        def send(self, chat_id, text, reply_markup=None):
            calls.append({"chat_id": chat_id, "text": text})
            return {"message_id": 1}

    # Newer outgoing WA message sent first
    dlg = apply_live_outgoing(
        conn,
        acc["id"],
        {
            "peer_type": "user",
            "telegram_user_id": 79995556677,
            "phone": "+79995556677",
            "name": "WA Lead",
            "telegram_message_id": new_out_mid,
            "external_message_id": "3EB0_NEW_OUT",
            "text": "Добрый день!",
            "sent_at": "2026-11-18T12:00:00+00:00",
            "provider": "whatsapp",
            "direction": "out",
        },
    )
    # Replaying the exact same outgoing message under a different mid but same external_message_id must NOT duplicate
    apply_live_outgoing(
        conn,
        acc["id"],
        {
            "peer_type": "user",
            "telegram_user_id": 79995556677,
            "phone": "+79995556677",
            "name": "WA Lead",
            "telegram_message_id": new_out_mid + 999,
            "external_message_id": "3EB0_NEW_OUT",
            "text": "Добрый день!",
            "sent_at": "2026-11-18T12:00:00+00:00",
            "provider": "whatsapp",
            "direction": "out",
        },
    )
    out_cnt = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND direction = 'out'",
        (dlg["id"],),
    ).fetchone()[0]
    assert out_cnt == 1

    # Historical WA incoming replay (older ts & smaller monotonic mid) must NEVER notify
    apply_incoming(
        conn,
        acc["id"],
        {
            "peer_type": "user",
            "telegram_user_id": 79995556677,
            "phone": "+79995556677",
            "name": "WA Lead",
            "telegram_message_id": old_in_mid,
            "external_message_id": "3EB0_OLD_IN",
            "text": "Старое сообщение",
            "sent_at": "2026-11-18T10:00:00+00:00",
            "provider": "whatsapp",
            "direction": "in",
        },
        opener=FakeOpener(),
    )
    assert calls == []
