from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.accounts import list_accounts
from app.db import connect, init_db, utc_now, utc_today
from app.inbox import list_inbox, list_unified_inbox
from app.main import create_app
from app.telegram_gateway import FakeGateway


def _insert_account(
    conn,
    *,
    phone: str,
    username: str,
    name: str,
    tg_uid: int,
    status: str = "connected",
    health: str | None = "active",
    daily_limit: int = 20,
    sent_today: int = 0,
    sent_date: str | None = None,
    proxy_url: str | None = None,
    provider: str = "telegram",
    session_path: str | None = None,
) -> int:
    now = utc_now()
    spath = session_path or f"data/sessions/demo_{username}_{tg_uid}.session"
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id, telegram_username, telegram_name,
            created_at, updated_at, health, daily_limit, sent_today, sent_date, proxy_url, provider
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            phone,
            spath,
            status,
            tg_uid,
            username,
            name,
            now,
            now,
            health,
            daily_limit,
            sent_today,
            sent_date if sent_date is not None else utc_today(),
            proxy_url,
            provider,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_lead(
    conn,
    *,
    account_id: int | None,
    tg_uid: int | None,
    username: str | None,
    name: str,
    status: str = "talking",
    amount: int = 1500,
    company: str = "Acme Studio",
    niche: str = "SaaS",
    airtable_status: str | None = None,
    airtable_record_id: str | None = None,
    provider: str = "telegram",
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, status, amount,
            company, niche, airtable_status, airtable_record_id, provider,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            tg_uid,
            username,
            name,
            status,
            amount,
            company,
            niche,
            airtable_status,
            airtable_record_id,
            provider,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_dialog(
    conn,
    *,
    account_id: int,
    peer_id: int,
    username: str | None,
    title: str,
    lead_id: int | None = None,
    unread_count: int = 0,
    last_message_at: str = "2026-11-19T10:00:00",
    last_message_preview: str = "Привет!",
    last_from_peer: int = 0,
    muted: int = 0,
    is_group: int = 0,
    peer_type: str = "user",
    provider: str = "telegram",
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider,
            is_group, unread_count, last_message_at, last_message_preview,
            last_from_peer, muted, lead_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            peer_type,
            peer_id,
            username,
            title,
            provider,
            is_group,
            unread_count,
            last_message_at,
            last_message_preview,
            last_from_peer,
            muted,
            lead_id,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_message(
    conn,
    *,
    dialog_id: int,
    tg_msg_id: int,
    direction: str,
    text: str,
    sent_at: str,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (dialog_id, tg_msg_id, direction, text, sent_at, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_list_unified_inbox_cross_account_lead_grouping_and_aggregation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appTestBase999")
    conn = connect(tmp_path / "unified_lead.db")
    init_db(conn)

    acc1 = _insert_account(
        conn,
        phone="+79000000001",
        username="sales_vlad",
        name="Vlad",
        tg_uid=101,
        health="active",
    )
    acc2 = _insert_account(
        conn,
        phone="+79000000002",
        username="sales_tim",
        name="Tim",
        tg_uid=102,
        health="frozen",
    )

    # Lead 1 has two dialogs across acc1 (older, waiting+unread) and acc2 (newer, last_from_peer=0)
    lead1 = _insert_lead(
        conn,
        account_id=acc1,
        tg_uid=7001,
        username="ivan_ceo",
        name="Иван CEO",
        status="talking",
        amount=4200,
        company="UGC Lab",
        niche="B2B SaaS",
        airtable_status="In conversation",
        airtable_record_id="recLead001",
    )
    d1_old = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=7001,
        username="ivan_ceo",
        title="Иван CEO",
        lead_id=lead1,
        unread_count=2,
        last_message_at="2026-11-19T10:00:00",
        last_message_preview="Жду смету",
        last_from_peer=1,
    )
    d1_new = _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=7001,
        username="ivan_ceo",
        title="Иван CEO",
        lead_id=lead1,
        unread_count=3,
        last_message_at="2026-11-19T12:00:00",
        last_message_preview="Отправил кейсы",
        last_from_peer=0,
    )

    # Lead 2 on acc1 only (our_turn, muted)
    lead2 = _insert_lead(
        conn,
        account_id=acc1,
        tg_uid=7002,
        username="maria_brand",
        name="Мария",
        status="waiting",
        amount=0,
        company="Brand X",
        niche="Ecom",
        airtable_status=None,
        airtable_record_id=None,
    )
    d2 = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=7002,
        username="maria_brand",
        title="Мария",
        lead_id=lead2,
        unread_count=0,
        last_message_at="2026-11-19T11:00:00",
        last_message_preview="Пинг по брифу",
        last_from_peer=0,
        muted=1,
    )

    # Lead 3 closed (won), last_from_peer=1 -> MUST NOT be waiting_our_answer
    lead3 = _insert_lead(
        conn,
        account_id=acc2,
        tg_uid=7003,
        username="olga_won",
        name="Ольга Won",
        status="won",
        amount=9000,
        company="WinCo",
        niche="Apps",
        airtable_status="Signed",
        airtable_record_id="recLead003",
    )
    _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=7003,
        username="olga_won",
        title="Ольга Won",
        lead_id=lead3,
        unread_count=0,
        last_message_at="2026-11-19T09:00:00",
        last_message_preview="Спасибо!",
        last_from_peer=1,
    )

    # Legacy list_inbox remains 100% ungrouped (4 raw rows)
    legacy = list_inbox(conn, account_id=0, with_counts=True, crm_only=True)
    assert len(legacy["dialogs"]) == 4
    assert legacy["workspace_total_dialogs"] == 4
    assert "peer_dialogs" not in legacy["dialogs"][0]

    # Unified grouped view collapses d1_old + d1_new into 1 client -> 3 grouped threads
    unified = list_unified_inbox(conn, account_id=0, with_counts=True, crm_only=True)
    assert unified["workspace_total_dialogs"] == 4
    assert unified["total_dialogs"] == 4
    assert unified["grouped_total_leads"] == 3
    assert unified["raw_counts"] == {
        "waiting": 1,
        "our_turn": 2,
        "unread": 2,
        "groups": 0,
        "muted": 1,
    }
    assert unified["counts"] == {
        "waiting": 1,
        "our_turn": 1,
        "unread": 1,
        "groups": 0,
        "muted": 1,
    }
    assert len(unified["dialogs"]) == 3
    assert unified["items"] == unified["dialogs"]

    first = unified["dialogs"][0]
    assert first["id"] == d1_new
    assert first["primary_dialog_id"] == d1_new
    assert first["unread_count"] == 5  # 2 + 3
    assert first["is_waiting_our_answer"] is True
    assert first["lead_status"] == "talking"
    assert first["lead_amount"] == 4200
    assert first["lead_company"] == "UGC Lab"
    assert first["lead_niche"] == "B2B SaaS"
    assert first["lead_airtable_status"] == "In conversation"
    assert first["lead_airtable_url"] == "https://airtable.com/appTestBase999/recLead001"
    assert [p["dialog_id"] for p in first["peer_dialogs"]] == [d1_new, d1_old]
    assert first["peer_dialogs"][0]["account_health"] == "frozen"
    assert first["peer_dialogs"][1]["account_health"] == "active"
    assert first["peer_dialogs"][1]["is_waiting_our_answer"] is True
    assert first["peer_dialogs"][0]["is_waiting_our_answer"] is False

    second = unified["dialogs"][1]
    assert second["id"] == d2
    assert second["lead_airtable_status"] == "Offer sent"
    assert second["lead_airtable_url"] is None

    # Chip filtering: waiting picks Lead 1 even though primary dialog had last_from_peer=0; our_turn skips Lead 1
    waiting_only = list_unified_inbox(conn, chip="waiting", with_counts=True, crm_only=True)
    assert [g["lead_id"] for g in waiting_only["dialogs"]] == [lead1]
    our_turn_only = list_unified_inbox(conn, chip="our_turn", with_counts=True, crm_only=True)
    assert [g["lead_id"] for g in our_turn_only["dialogs"]] == [lead2]

    # Filtering by account_id=acc1 keeps Lead 1 & Lead 2, and still attaches ALL peer_dialogs (including acc2!)
    acc1_only = list_unified_inbox(conn, account_id=acc1, with_counts=True, crm_only=True)
    assert acc1_only["workspace_total_dialogs"] == 4
    assert acc1_only["grouped_total_leads"] == 3
    assert [g["lead_id"] for g in acc1_only["dialogs"]] == [lead1, lead2]
    assert [p["account_id"] for p in acc1_only["dialogs"][0]["peer_dialogs"]] == [acc2, acc1]


def test_unified_grouping_by_peer_id_username_tiebreak_and_provider_isolation(
    tmp_path: Path,
):
    conn = connect(tmp_path / "unified_peer.db")
    init_db(conn)

    acc1 = _insert_account(
        conn, phone="+79001110001", username="a1", name="Acc1", tg_uid=201
    )
    acc2 = _insert_account(
        conn, phone="+79001110002", username="a2", name="Acc2", tg_uid=202
    )
    wa_acc = _insert_account(
        conn,
        phone="+79001110003",
        username="wa1",
        name="WA",
        tg_uid=203,
        provider="whatsapp",
    )

    # Create a campaign + contacted recipients so dialogs without lead_id pass crm_only=True
    now = utc_now()
    camp_id = conn.execute(
        "INSERT INTO campaigns (name, status, account_id, created_at, updated_at) VALUES ('Test', 'running', ?, ?, ?)",
        (acc1, now, now),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, status, current_step, last_sent_at,
            sent_account_id, created_at, updated_at
        ) VALUES (?, 8888, 'peer_client', 'replied', 1, ?, ?, ?, ?)
        """,
        (camp_id, now, acc1, now, now),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, status, current_step, last_sent_at,
            sent_account_id, created_at, updated_at
        ) VALUES (?, 8888, 'peer_client', 'replied', 1, ?, ?, ?, ?)
        """,
        (camp_id, now, acc2, now, now),
    )
    conn.commit()

    # Same peer_id=8888 across acc1 & acc2, NO lead_id, identical last_message_at -> tie-break favors is_waiting_our_answer=1!
    d_wait = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=8888,
        username="peer_client",
        title="Peer Client",
        lead_id=None,
        unread_count=1,
        last_message_at="2026-11-19T14:00:00",
        last_message_preview="Вопрос",
        last_from_peer=1,
    )
    d_not_wait = _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=8888,
        username="peer_client",
        title="Peer Client",
        lead_id=None,
        unread_count=0,
        last_message_at="2026-11-19T14:00:00",
        last_message_preview="Ответ",
        last_from_peer=0,
    )

    # Same peer_id=8888 on WhatsApp MUST NOT merge into Telegram group
    lead_wa = _insert_lead(
        conn,
        account_id=wa_acc,
        tg_uid=None,
        username=None,
        name="WA Client",
        status="talking",
        provider="whatsapp",
    )
    d_wa = _insert_dialog(
        conn,
        account_id=wa_acc,
        peer_id=8888,
        username="peer_client",
        title="WA Client",
        lead_id=lead_wa,
        unread_count=4,
        last_message_at="2026-11-19T13:00:00",
        last_message_preview="WA msg",
        last_from_peer=1,
        provider="whatsapp",
    )

    # Two dialogs with peer_id=0 and no lead_id grouping by normalized username (crm_only=False)
    d_uname_1 = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=0,
        username="@UName_Case",
        title="UName 1",
        lead_id=None,
        unread_count=1,
        last_message_at="2026-11-19T08:00:00",
        last_from_peer=1,
    )
    d_uname_2 = _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=0,
        username="uname_case",
        title="UName 2",
        lead_id=None,
        unread_count=2,
        last_message_at="2026-11-19T08:30:00",
        last_from_peer=0,
    )

    res_crm = list_unified_inbox(conn, with_counts=True, crm_only=True)
    assert len(res_crm["dialogs"]) == 2
    tg_grp = res_crm["dialogs"][0]
    assert tg_grp["primary_dialog_id"] == d_wait  # tie-break: waiting wins over higher id d_not_wait!
    assert tg_grp["unread_count"] == 1
    assert [p["dialog_id"] for p in tg_grp["peer_dialogs"]] == [d_wait, d_not_wait]
    assert res_crm["dialogs"][1]["primary_dialog_id"] == d_wa

    res_all = list_unified_inbox(conn, with_counts=True, crm_only=False)
    uname_grps = [g for g in res_all["dialogs"] if g["primary_dialog_id"] == d_uname_2]
    assert len(uname_grps) == 1
    assert uname_grps[0]["unread_count"] == 3
    assert [p["dialog_id"] for p in uname_grps[0]["peer_dialogs"]] == [d_uname_2, d_uname_1]


def test_http_unified_endpoints_and_legacy_inbox_parity(tmp_path: Path):
    conn = connect(tmp_path / "http_unified.db")
    init_db(conn)
    acc1 = _insert_account(conn, phone="+79002220001", username="u1", name="U1", tg_uid=301)
    acc2 = _insert_account(conn, phone="+79002220002", username="u2", name="U2", tg_uid=302)
    lead1 = _insert_lead(
        conn,
        account_id=acc1,
        tg_uid=9001,
        username="client_one",
        name="Client One",
        status="talking",
    )
    d1 = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=9001,
        username="client_one",
        title="Client One",
        lead_id=lead1,
        unread_count=1,
        last_message_at="2026-11-19T10:00:00",
        last_from_peer=1,
    )
    d2 = _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=9001,
        username="client_one",
        title="Client One",
        lead_id=lead1,
        unread_count=2,
        last_message_at="2026-11-19T11:00:00",
        last_from_peer=0,
    )

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        # 1. Legacy /api/inbox without group_by_lead stays flat
        r_leg = client.get("/api/inbox", params={"crm_only": 1, "with_counts": 1})
        assert r_leg.status_code == 200
        leg = r_leg.json()
        assert len(leg["dialogs"]) == 2
        assert "grouped_total_leads" not in leg
        assert "peer_dialogs" not in leg["dialogs"][0]

        # 2. /api/inbox?group_by_lead=1&with_counts=1 returns grouped payload
        r_grp_counts = client.get(
            "/api/inbox",
            params={"crm_only": 1, "group_by_lead": 1, "with_counts": 1},
        )
        assert r_grp_counts.status_code == 200
        grp_counts = r_grp_counts.json()

        # 3. /api/inbox/unified matches /api/inbox?group_by_lead=1&with_counts=1
        r_uni = client.get("/api/inbox/unified")
        assert r_uni.status_code == 200
        uni = r_uni.json()
        assert uni == grp_counts
        assert uni["workspace_total_dialogs"] == 2
        assert uni["total_dialogs"] == 2
        assert uni["grouped_total_leads"] == 1
        assert len(uni["dialogs"]) == 1
        assert uni["dialogs"][0]["primary_dialog_id"] == d2
        assert uni["dialogs"][0]["unread_count"] == 3
        assert [p["dialog_id"] for p in uni["dialogs"][0]["peer_dialogs"]] == [d2, d1]

        # 4. /api/inbox?group_by_lead=1 without with_counts returns list
        r_grp_list = client.get(
            "/api/inbox", params={"crm_only": 1, "group_by_lead": 1}
        )
        assert r_grp_list.status_code == 200
        assert isinstance(r_grp_list.json(), list)
        assert len(r_grp_list.json()) == 1
        assert r_grp_list.json()[0]["primary_dialog_id"] == d2


def test_switch_account_existing_and_create_new_and_live_gateway(tmp_path: Path):
    conn = connect(tmp_path / "switch_acc.db")
    init_db(conn)
    acc1 = _insert_account(conn, phone="+79003330001", username="s1", name="S1", tg_uid=401)
    acc2 = _insert_account(conn, phone="+79003330002", username="s2", name="S2", tg_uid=402)
    acc3 = _insert_account(conn, phone="+79003330003", username="s3", name="S3", tg_uid=403)

    lead1 = _insert_lead(
        conn,
        account_id=acc1,
        tg_uid=9501,
        username="switch_client",
        name="Switch Client",
        status="talking",
    )
    d1 = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=9501,
        username="switch_client",
        title="Switch Client",
        lead_id=lead1,
        last_message_at="2026-11-19T10:00:00",
    )
    d2 = _insert_dialog(
        conn,
        account_id=acc2,
        peer_id=9501,
        username="switch_client",
        title="Switch Client",
        lead_id=lead1,
        last_message_at="2026-11-19T11:00:00",
    )
    _insert_message(
        conn,
        dialog_id=d2,
        tg_msg_id=101,
        direction="in",
        text="Сообщение на acc2",
        sent_at="2026-11-19T11:00:00",
    )

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        # 1. Existing dialog on acc2 via dialog_id=d1 -> created=False + messages loaded
        r_exist = client.post(
            "/api/dialogs/switch-account",
            json={"dialog_id": d1, "target_account_id": acc2},
        )
        assert r_exist.status_code == 200
        body1 = r_exist.json()
        assert body1["ok"] is True
        assert body1["created"] is False
        assert body1["dialog"]["id"] == d2
        assert body1["dialog"]["account_id"] == acc2
        assert [m["text"] for m in body1["messages"]] == ["Сообщение на acc2"]

        # 2. New dialog on acc3 (lead_id resolved from d1) -> created=True
        r_new = client.post(
            "/api/dialogs/switch-account",
            json={"dialog_id": d1, "target_account_id": acc3},
        )
        assert r_new.status_code == 200
        body2 = r_new.json()
        assert body2["ok"] is True
        assert body2["created"] is True
        assert body2["dialog"]["account_id"] == acc3
        assert body2["dialog"]["lead_id"] == lead1
        assert isinstance(body2["messages"], list)
        assert all(m.get("is_snapshot_preview") for m in body2["messages"])
        d3 = body2["dialog"]["id"]
        assert d3 not in (d1, d2)

        # 3. Calling switch-account again to acc3 finds existing dialog -> created=False
        r_again = client.post(
            "/api/dialogs/switch-account",
            json={"lead_id": lead1, "target_account_id": acc3},
        )
        assert r_again.status_code == 200
        assert r_again.json()["created"] is False
        assert r_again.json()["dialog"]["id"] == d3

        # 4. Direct username without existing lead creates dialog via start_direct_dialog
        r_uname = client.post(
            "/api/dialogs/switch-account",
            json={"username": "@brand_new_handle", "target_account_id": acc3},
        )
        assert r_uname.status_code == 200
        ubody = r_uname.json()
        assert ubody["ok"] is True
        assert ubody["created"] is True
        assert ubody["dialog"]["account_id"] == acc3
        assert ubody["dialog"]["username"] == "brand_new_handle"

        # 5. Non-existent target_account_id returns 404
        r_missing_acc = client.post(
            "/api/dialogs/switch-account",
            json={"dialog_id": d1, "target_account_id": 99999},
        )
        assert r_missing_acc.status_code == 404


def test_switch_account_uses_live_gateway_thread_best_effort(tmp_path: Path):
    conn = connect(tmp_path / "switch_live.db")
    init_db(conn)
    acc1 = _insert_account(
        conn,
        phone="+79004440001",
        username="live1",
        name="Live1",
        tg_uid=501,
        session_path="data/sessions/live_acc1.session",
    )
    acc2 = _insert_account(
        conn,
        phone="+79004440002",
        username="live2",
        name="Live2",
        tg_uid=502,
        session_path="data/sessions/live_acc2.session",
    )
    lead1 = _insert_lead(
        conn,
        account_id=acc1,
        tg_uid=9601,
        username="live_peer",
        name="Live Peer",
        status="talking",
    )
    d1 = _insert_dialog(
        conn,
        account_id=acc1,
        peer_id=9601,
        username="live_peer",
        title="Live Peer",
        lead_id=lead1,
    )

    gw = FakeGateway(
        history={
            9601: [
                {
                    "telegram_message_id": 555,
                    "direction": "in",
                    "text": "История с сервера TG",
                    "sent_at": "2099-01-01T12:00:00",
                }
            ]
        }
    )
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        res = client.post(
            "/api/dialogs/switch-account",
            json={"dialog_id": d1, "target_account_id": acc2},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["created"] is True
        assert [m["text"] for m in body["messages"]] == ["История с сервера TG"]


    # Best-effort fallback: if gateway.fetch_messages fails on target account, switch-account falls back cleanly to list_messages
    async def _failing_fetch(*args, **kwargs):
        raise RuntimeError("transient TG disconnect")

    gw.fetch_messages = _failing_fetch
    acc3 = _insert_account(
        conn,
        phone="+79004440003",
        username="live3",
        name="Live3",
        tg_uid=503,
        session_path="data/sessions/live_acc3.session",
    )
    with TestClient(app) as client:
        res_fb = client.post(
            "/api/dialogs/switch-account",
            json={"dialog_id": d1, "target_account_id": acc3},
        )
        assert res_fb.status_code == 200
        assert res_fb.json()["ok"] is True
        assert res_fb.json()["created"] is True
        assert isinstance(res_fb.json()["messages"], list)


def test_accounts_quota_fields_remaining_today(tmp_path: Path):
    conn = connect(tmp_path / "accounts_quota.db")
    init_db(conn)

    a_active = _insert_account(
        conn,
        phone="+79005550001",
        username="q_active",
        name="Active",
        tg_uid=601,
        status="connected",
        health="active",
        daily_limit=20,
        sent_today=7,
        sent_date=utc_today(),
    )
    a_stale_date = _insert_account(
        conn,
        phone="+79005550002",
        username="q_stale",
        name="Stale",
        tg_uid=602,
        status="connected",
        health="active",
        daily_limit=25,
        sent_today=19,
        sent_date="2020-01-01",
    )
    a_frozen = _insert_account(
        conn,
        phone="+79005550003",
        username="q_frozen",
        name="Frozen",
        tg_uid=603,
        status="connected",
        health="frozen",
        daily_limit=20,
        sent_today=3,
        sent_date=utc_today(),
        proxy_url="socks5://user:pass@gate.nodemaven.com:1080",
    )
    a_pending = _insert_account(
        conn,
        phone="+79005550004",
        username="q_pending",
        name="Pending",
        tg_uid=604,
        status="pending_code",
        health=None,
        daily_limit=20,
        sent_today=0,
        sent_date=utc_today(),
    )
    a_over = _insert_account(
        conn,
        phone="+79005550005",
        username="q_over",
        name="Over",
        tg_uid=605,
        status="connected",
        health="active",
        daily_limit=15,
        sent_today=22,
        sent_date=utc_today(),
    )

    items = {a["id"]: a for a in list_accounts(conn)}
    assert items[a_active]["sent_today"] == 7
    assert items[a_active]["daily_limit"] == 20
    assert items[a_active]["remaining_today"] == 13
    assert items[a_active]["health"] == "active"
    assert items[a_active]["proxy_mode"] == "direct"

    assert items[a_stale_date]["sent_today"] == 0
    assert items[a_stale_date]["remaining_today"] == 25

    assert items[a_frozen]["remaining_today"] == 0
    assert items[a_frozen]["health"] == "frozen"
    assert items[a_frozen]["proxy_mode"] == "rotating_nodemaven"

    assert items[a_pending]["remaining_today"] == 0
    assert items[a_pending]["health"] is None

    assert items[a_over]["remaining_today"] == 0

    app = create_app(conn=conn, gateway=None)
    with TestClient(app) as client:
        r = client.get("/api/accounts")
        assert r.status_code == 200
        api_map = {a["id"]: a for a in r.json()}
        assert api_map[a_active]["remaining_today"] == 13
        assert api_map[a_frozen]["remaining_today"] == 0


def test_service_worker_route_headers_auth_and_content(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("MCP_API_KEY", "test-sw-mcp-key")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongSwPass#2026")
    conn = connect(tmp_path / "sw_sec.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=None)

    with TestClient(app, follow_redirects=False) as client:
        # Unauthenticated browser fetch (e.g. SW update check) succeeds without redirect
        r_unauth = client.get(
            "/sw-inbox.js",
            headers={"User-Agent": "Mozilla/5.0 Chrome/125.0"},
        )
        assert r_unauth.status_code == 200
        assert r_unauth.headers.get("content-type", "").startswith(
            "application/javascript"
        )
        assert r_unauth.headers.get("service-worker-allowed") == "/"
        assert r_unauth.headers.get("cache-control") == "no-cache"

        # Scraper UA stays blocked
        r_bot = client.get(
            "/sw-inbox.js",
            headers={"User-Agent": "python-requests/2.31.0"},
        )
        assert r_bot.status_code == 403

        # Authenticated session cookie fetch succeeds
        r_login = client.post(
            "/api/auth/login",
            json={"username": "tim", "password": "StrongSwPass#2026"},
            headers={"User-Agent": "Mozilla/5.0 Chrome/125.0"},
        )
        assert r_login.status_code == 200
        r_auth = client.get(
            "/sw-inbox.js",
            headers={"User-Agent": "Mozilla/5.0 Chrome/125.0"},
        )
        assert r_auth.status_code == 200
        body = r_auth.text

    # Content invariants: caches ONLY avatars & media in foldesk-media-v1 (LRU 300), never inbox/auth/leads
    assert "foldesk-media-v1" in body
    assert "300" in body
    assert r"\/api\/dialogs\/\d+\/avatar" in body
    assert r"\/api\/dialogs\/\d+\/messages\/\d+\/media" in body
    assert r"\/media\/inbox\/" in body
    assert "/api/inbox" in body
    assert "/api/auth/" in body
    assert "/api/leads" in body
    assert 'request.method !== "GET"' in body

    node_bin = shutil.which("node")
    if node_bin:
        sw_path = Path(__file__).resolve().parents[1] / "app" / "static" / "sw-inbox.js"
        proc = subprocess.run(
            [node_bin, "--check", str(sw_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
