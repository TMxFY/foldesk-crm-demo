from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telethon.tl.types import User

from app.db import connect, init_db, utc_now
from app.digest import (
    audit_reply_notifications,
    extract_bot_user_id_from_token,
    heal_reply_notifications,
    is_ineligible_reply_notify_peer,
    send_lead_reply_notification,
)
from app.inbox import apply_incoming, stop_outreach_on_reply, sync_inbox
from app.leads import create_lead
from app.main import create_app
from app.outreach import create_campaign
from app.telegram_gateway import TelethonGateway

WORKTREE_ROOT = Path(__file__).resolve().parents[1]


class _FakeBotResp:
    def __init__(self, msg_id: int = 100):
        self.status = 200
        self._body = json.dumps({"ok": True, "result": {"message_id": msg_id}}).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_colleague_bot_spy(monkeypatch: pytest.MonkeyPatch):
    # Realistic Colleague bot token whose numeric prefix is @softugccollegabot's user_id (8685610974)
    monkeypatch.setenv("BOT_TOKEN", "8685610974:AAFakeColleagueTokenForTests")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    calls: list[dict] = []
    seq = {"id": 500}

    def fake_urlopen(req, timeout=None, context=None):
        seq["id"] += 1
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append({"url": req.full_url, "body": body})
        return _FakeBotResp(msg_id=seq["id"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _seed_account(
    conn,
    *,
    phone: str,
    telegram_user_id: int,
    username: str,
    name: str,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_user_id, telegram_username, telegram_name,
            session_path, status, health, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'connected', 'active', ?, ?)
        """,
        (phone, telegram_user_id, username, name, f"data/sessions/{username}.session", now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_colleague_bot_self_echo_never_triggers_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _install_colleague_bot_spy(monkeypatch)
    conn = connect(tmp_path / "echo.db")
    init_db(conn)
    vlad_acc_id = _seed_account(
        conn,
        phone="+79088819595",
        telegram_user_id=978069541,
        username="vladwilhelm",
        name="Vladislav Wilhelm",
    )

    assert extract_bot_user_id_from_token("8685610974:AAFakeColleagueTokenForTests") == 8685610974

    # Even if @softugccollegabot were mistakenly inserted as a lead, self-echo is blocked
    create_lead(
        conn,
        name="Коллега",
        username="softugccollegabot",
        telegram_user_id=8685610974,
        status="talking",
        account_id=vlad_acc_id,
    )

    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 8685610974,
            "telegram_message_id": 339,
            "username": "softugccollegabot",
            "name": "Коллега",
            "text": "You received a response from @yarlclear\n\nAccount: @saabath",
            "sent_at": "2026-11-17T11:33:25",
            "direction": "in",
        },
    )
    assert calls == []

    # Direct boundary call is also blocked
    assert (
        send_lead_reply_notification(
            conn,
            lead_handle="@softugccollegabot",
            account_handle="@vladwilhelm",
            account_id=vlad_acc_id,
            peer_id=8685610974,
        )
        is False
    )
    assert calls == []


def test_saved_messages_and_internal_team_accounts_never_trigger_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = _install_colleague_bot_spy(monkeypatch)
    conn = connect(tmp_path / "team.db")
    init_db(conn)
    vlad_acc_id = _seed_account(
        conn,
        phone="+79088819595",
        telegram_user_id=978069541,
        username="vladwilhelm",
        name="Vladislav Wilhelm",
    )
    roman_acc_id = _seed_account(
        conn,
        phone="+79026275254",
        telegram_user_id=816020082,
        username="saburowe",
        name="Roman Saburov",
    )

    # 1. Saved Messages (@vladwilhelm -> @vladwilhelm)
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 978069541,
            "telegram_message_id": 174954,
            "username": "vladwilhelm",
            "name": "Vladislav Wilhelm",
            "text": "Заметка себе",
            "sent_at": "2026-11-17T11:00:00",
            "direction": "in",
        },
    )
    # 2. Vlad messaging Roman (@vladwilhelm -> @saburowe)
    apply_incoming(
        conn,
        roman_acc_id,
        {
            "telegram_user_id": 978069541,
            "telegram_message_id": 564573,
            "username": "vladwilhelm",
            "name": "Vladislav Wilhelm",
            "text": "Ром, чекни прокси",
            "sent_at": "2026-11-17T11:01:00",
            "direction": "in",
        },
    )
    # 3. Roman messaging Vlad (@saburowe -> @vladwilhelm)
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 816020082,
            "telegram_message_id": 564580,
            "username": "saburowe",
            "name": "Roman Saburov",
            "text": "Готово!",
            "sent_at": "2026-11-17T11:02:00",
            "direction": "in",
        },
    )
    # 4. Timofei (@korznikov_timofei) messaging Vlad in DM
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 777111222,
            "telegram_message_id": 564590,
            "username": "korznikov_timofei",
            "name": "Tim Korznikov",
            "text": "угар",
            "sent_at": "2026-11-17T11:03:00",
            "direction": "in",
        },
    )
    assert calls == []


def test_non_lead_personal_dms_and_bots_never_notify_while_real_open_leads_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = _install_colleague_bot_spy(monkeypatch)
    conn = connect(tmp_path / "personal_vs_lead.db")
    init_db(conn)
    vlad_acc_id = _seed_account(
        conn,
        phone="+79088819595",
        telegram_user_id=978069541,
        username="vladwilhelm",
        name="Vladislav Wilhelm",
    )
    roman_acc_id = _seed_account(
        conn,
        phone="+79026275254",
        telegram_user_id=816020082,
        username="saburowe",
        name="Roman Saburov",
    )

    # Personal non-lead contact (@gmiroooyu -> @saburowe)
    apply_incoming(
        conn,
        roman_acc_id,
        {
            "telegram_user_id": 1378546224,
            "telegram_message_id": 101,
            "username": "gmiroooyu",
            "name": "мирослава",
            "text": "Привет!",
            "sent_at": "2026-11-17T11:10:00",
            "direction": "in",
        },
    )
    sync_inbox(
        conn,
        roman_acc_id,
        [
            {
                "telegram_user_id": 1378546224,
                "username": "gmiroooyu",
                "name": "мирослава",
                "last_from_peer": 1,
                "last_message_preview": "Привет!",
                "last_message_at": "2026-11-17T11:10:00",
                "unread_count": 1,
            }
        ],
    )
    stop_outreach_on_reply(
        conn,
        account_id=roman_acc_id,
        peer_id=1378546224,
        username="gmiroooyu",
        force_event=True,
    )

    # External Telegram bots (@palienko_futurology_bot, @soctugcaccbot, is_bot=True)
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 6605199840,
            "telegram_message_id": 102,
            "username": "palienko_futurology_bot",
            "name": "Futurology Bot",
            "text": "Дайджест",
            "sent_at": "2026-11-17T11:11:00",
            "direction": "in",
            "is_bot": True,
        },
    )
    assert calls == []

    # Already signed / won client (Viktor @Dangerousmictor3000, status='won') -> MUST NOT notify!
    viktor = create_lead(
        conn,
        name="Viktor Stoliarou",
        username="Dangerousmictor3000",
        telegram_user_id=1029830708,
        status="won",
        account_id=vlad_acc_id,
    )
    camp = create_campaign(
        conn,
        name="Outreach Camp",
        account_id=vlad_acc_id,
        steps=[{"template_text": "Step 1"}],
    )
    now = utc_now()
    # Even if Viktor had a historical campaign_recipients row, he is now 'won' (Signed client)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            status, current_step, sent_account_id, created_at, updated_at
        ) VALUES (?, ?, 1029830708, 'Dangerousmictor3000', 'Viktor', 'replied', 1, ?, ?, ?)
        """,
        (camp["id"], viktor["id"], vlad_acc_id, now, now),
    )
    conn.commit()

    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 1029830708,
            "telegram_message_id": 201,
            "username": "Dangerousmictor3000",
            "name": "Victor Berezovsky",
            "text": "Влад, скинул новые правки по ролику",
            "sent_at": "2026-11-17T11:15:00",
            "direction": "in",
        },
    )
    assert calls == []

    # Cancelled / lost lead without active outreach -> MUST NOT notify!
    create_lead(
        conn,
        name="Lost Lead",
        username="cancelled_client",
        telegram_user_id=2020999,
        status="lost",
        account_id=vlad_acc_id,
    )
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 2020999,
            "telegram_message_id": 202,
            "username": "cancelled_client",
            "name": "Lost Lead",
            "text": "Ок",
            "sent_at": "2026-11-17T11:16:00",
            "direction": "in",
        },
    )
    assert calls == []

    # Real open lead (@Taboolaexpert, status='talking') -> MUST notify cleanly!
    create_lead(
        conn,
        name="Taboola expert",
        username="Taboolaexpert",
        telegram_user_id=6200282197,
        status="talking",
        account_id=vlad_acc_id,
    )
    apply_incoming(
        conn,
        vlad_acc_id,
        {
            "telegram_user_id": 6200282197,
            "telegram_message_id": 301,
            "username": "Taboolaexpert",
            "name": "Taboola expert",
            "text": "Привет! Давайте созвонимся сегодня в 16:00",
            "sent_at": "2026-11-17T11:20:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1
    assert calls[0]["body"]["text"] == "You received a response from @Taboolaexpert\n\nAccount: @vladwilhelm"


def test_telethon_gateway_filters_bots_and_self_on_incoming_and_load_private():
    gw = TelethonGateway()
    self_user = User(id=978069541, username="vladwilhelm", first_name="Vlad", is_self=True, bot=False)
    colleague_bot = User(id=8685610974, username="softugccollegabot", first_name="Коллега", is_self=False, bot=True)
    real_lead = User(id=6200282197, username="Taboolaexpert", first_name="Taboola", is_self=False, bot=False)

    class _DlgClient:
        async def iter_dialogs(self):
            for idx, ent in enumerate((self_user, colleague_bot, real_lead), start=1):
                yield SimpleNamespace(
                    is_user=True,
                    entity=ent,
                    name=ent.first_name,
                    unread_count=1,
                    message=SimpleNamespace(
                        id=idx * 10,
                        message="Test",
                        date=datetime(2026, 11, 17, 11, idx, tzinfo=timezone.utc),
                        out=False,
                    ),
                    dialog=SimpleNamespace(read_inbox_max_id=0, read_outbox_max_id=0, unread_mark=False),
                )

    loaded = asyncio.run(gw._load_private(_DlgClient(), 10))
    assert [r["telegram_user_id"] for r in loaded] == [6200282197]

    # Incoming _private_payload for self_user and bot_user MUST return None
    def _mk_event(peer_obj):
        msg = SimpleNamespace(
            id=77,
            out=False,
            message="Ping",
            date=datetime(2026, 11, 17, 11, 30, tzinfo=timezone.utc),
            edit_date=None,
            media=None,
            entities=None,
            reply_to=None,
        )
        return SimpleNamespace(
            is_private=True,
            is_group=False,
            is_channel=False,
            message=msg,
            get_chat=lambda: asyncio.sleep(0, result=peer_obj),
            get_sender=lambda: asyncio.sleep(0, result=peer_obj),
        )

    assert asyncio.run(gw._private_payload(_mk_event(self_user))) is None
    assert asyncio.run(gw._private_payload(_mk_event(colleague_bot))) is None
    lead_payload = asyncio.run(gw._private_payload(_mk_event(real_lead)))
    assert lead_payload is not None
    assert lead_payload["telegram_user_id"] == 6200282197


def test_self_healing_audit_and_proactive_audit_cli_catch_and_heal_anomalies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BOT_TOKEN", "8685610974:AAFakeColleagueTokenForTests")
    monkeypatch.setenv("BOT_CHAT_ID", "978069541")
    db_path = tmp_path / "dirty_prod_clone.db"
    conn = connect(db_path)
    init_db(conn)

    vlad_acc_id = _seed_account(
        conn,
        phone="+79088819595",
        telegram_user_id=978069541,
        username="vladwilhelm",
        name="Vladislav Wilhelm",
    )
    roman_acc_id = _seed_account(
        conn,
        phone="+79026275254",
        telegram_user_id=816020082,
        username="saburowe",
        name="Roman Saburov",
    )
    won_viktor = create_lead(
        conn,
        name="Viktor Stoliarou",
        username="Dangerousmictor3000",
        telegram_user_id=1029830708,
        status="won",
        account_id=vlad_acc_id,
    )
    bot_lead = create_lead(
        conn,
        name="Animus",
        username="animusastro_bot",
        status="won",
    )
    good_lead = create_lead(
        conn,
        name="Taboola expert",
        username="Taboolaexpert",
        telegram_user_id=6200282197,
        status="talking",
        account_id=vlad_acc_id,
    )
    now = utc_now()
    # Inject historical false notifications + 1 valid notification directly into SQLite
    conn.executemany(
        """
        INSERT INTO bot_reply_notifications (
            bot_message_id, chat_id, dialog_id, lead_id, account_id, peer_id,
            lead_handle, account_handle, created_at
        ) VALUES (?, '978069541', NULL, ?, ?, ?, ?, ?, ?)
        """,
        [
            (339, None, vlad_acc_id, 8685610974, "@softugccollegabot", "@vladwilhelm", now),
            (338, None, roman_acc_id, 1378546224, "@gmiroooyu", "@saburowe", now),
            (330, won_viktor["id"], vlad_acc_id, 1029830708, "@Dangerousmictor3000", "@vladwilhelm", now),
            (313, None, roman_acc_id, 978069541, "@vladwilhelm", "@saburowe", now),
            (306, good_lead["id"], vlad_acc_id, 6200282197, "@Taboolaexpert", "@vladwilhelm", now),
        ],
    )
    conn.commit()

    inelig, _ = is_ineligible_reply_notify_peer(conn, peer_id=8685610974, username="softugccollegabot")
    assert inelig is True

    report_before = audit_reply_notifications(conn)
    assert report_before["ok"] is False
    assert report_before["false_notification_count"] == 4
    assert report_before["bot_username_lead_count"] == 1

    # CLI proactive_audit.py MUST fail (exit 1) on unhealed DB
    cli_fail = subprocess.run(
        [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--db", str(db_path)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "BOT_TOKEN": "8685610974:AAFakeColleagueTokenForTests",
            "BOT_CHAT_ID": "978069541",
        },
    )
    assert cli_fail.returncode == 1, cli_fail.stdout + cli_fail.stderr

    # Heal repairs DB while preserving legitimate @Taboolaexpert alert (bot_message_id=306)
    healed = heal_reply_notifications(conn)
    assert healed["purged_notifications"] == 4
    assert healed["scrubbed_bot_leads"] == 1

    remaining = conn.execute("SELECT bot_message_id, lead_handle FROM bot_reply_notifications").fetchall()
    assert [(r["bot_message_id"], r["lead_handle"]) for r in remaining] == [(306, "@Taboolaexpert")]
    scrubbed_row = conn.execute("SELECT username FROM leads WHERE id = ?", (bot_lead["id"],)).fetchone()
    assert scrubbed_row["username"] is None

    report_after = audit_reply_notifications(conn)
    assert report_after["ok"] is True
    assert report_after["false_notification_count"] == 0
    assert report_after["bot_username_lead_count"] == 0

    # CLI proactive_audit.py now exits 0
    cli_ok = subprocess.run(
        [sys.executable, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--db", str(db_path)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "BOT_TOKEN": "8685610974:AAFakeColleagueTokenForTests",
            "BOT_CHAT_ID": "978069541",
        },
    )
    assert cli_ok.returncode == 0, cli_ok.stdout + cli_ok.stderr

    # /api/health?deep=1 surfaces reply_alert_guard_ok=True and false_reply_alerts_24h=0
    app = create_app(conn=conn)
    with TestClient(app) as client:
        resp = client.get("/api/health?deep=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply_alert_guard_ok"] is True
        assert body["false_reply_alerts_24h"] == 0
    conn.close()


def test_verify_sh_gate_c_blocks_dlg_row_regression(tmp_path: Path):
    sandbox = tmp_path / "gate_c_sandbox"
    for sub in ("scripts", "app", "leadgen", "tests", "deploy/systemd"):
        (sandbox / sub).mkdir(parents=True)
    verify_src = (WORKTREE_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    sb_verify = sandbox / "scripts" / "verify.sh"
    sb_verify.write_text(verify_src, encoding="utf-8")
    sb_verify.chmod(0o755)
    (sandbox / "leadgen" / "vps_autonomous_replenish.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox / "tests" / "test_x.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    (sandbox / "deploy" / "systemd" / "crmtg.service").write_text(
        '[Service]\nEnvironment="CRMTG_VPS_RUNTIME=1"\nExecStart=/opt/crmtg/.venv/bin/python -m leadgen.vps_autonomous_replenish\n',
        encoding="utf-8",
    )
    (sandbox / "app" / "inbox.py").write_text(
        "base_notify_trigger = (force_event and (bool(matched_lead_ids) or (dlg_row is not None)))\n",
        encoding="utf-8",
    )
    res = subprocess.run([str(sb_verify), "--fast-checks-only"], cwd=str(sandbox), capture_output=True, text=True)
    assert res.returncode == 1
    assert "Reply-Alert Gate violation" in res.stderr


def test_skeptic_edge_cases_display_name_airtable_signed_and_generic_bot_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = _install_colleague_bot_spy(monkeypatch)
    conn = connect(tmp_path / "skeptic.db")
    init_db(conn)
    now = utc_now()

    # 1) Lead named "Alex" (no @username) on an account also named "Alex" (no @username) MUST still notify!
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, telegram_user_id, telegram_username, telegram_name,
            session_path, status, health, created_at, updated_at
        ) VALUES ('+79990007788', 500500, NULL, 'Alex', 'data/sessions/alex.session', 'connected', 'active', ?, ?)
        """,
        (now, now),
    )
    alex_acc_id = int(cur.lastrowid)
    # Even when an unrelated won lead @alex exists, open lead "Alex" without @username is NOT muted!
    create_lead(conn, name="Other Won Alex", username="alex", status="won")
    create_lead(
        conn,
        name="Alex",
        username=None,
        telegram_user_id=700701,
        status="waiting",
        account_id=alex_acc_id,
    )
    apply_incoming(
        conn,
        alex_acc_id,
        {
            "telegram_user_id": 700701,
            "telegram_message_id": 10,
            "username": None,
            "name": "Alex",
            "text": "Привет, готов обсудить проект!",
            "sent_at": "2026-11-17T12:00:00",
            "direction": "in",
        },
    )
    assert len(calls) == 1
    assert calls[0]["body"]["text"] == "You received a response from Alex\n\nAccount: Alex"

    # 2) Lead with status='new' BUT airtable_status='Signed' MUST NOT be overwritten to 'talking' or notify!
    signed_id = int(
        conn.execute(
            """
            INSERT INTO leads (
                account_id, telegram_user_id, username, name, status, airtable_status, created_at, updated_at
            ) VALUES (?, 700702, 'signed_airtable_client', 'Signed Client', 'waiting', 'Signed', ?, ?)
            """,
            (alex_acc_id, now, now),
        ).lastrowid
    )
    conn.commit()
    apply_incoming(
        conn,
        alex_acc_id,
        {
            "telegram_user_id": 700702,
            "telegram_message_id": 11,
            "username": "signed_airtable_client",
            "name": "Signed Client",
            "text": "Скинул материалы",
            "sent_at": "2026-11-17T12:01:00",
            "direction": "in",
        },
    )
    lrow = conn.execute("SELECT status, airtable_status FROM leads WHERE id = ?", (signed_id,)).fetchone()
    assert lrow["airtable_status"] == "Signed"
    assert len(calls) == 1  # Still 1!

    # 3) Generic *Bot suffix without underscore (@CryptoBot, @SendBot) + direct peer_id call without backing -> blocked!
    apply_incoming(
        conn,
        alex_acc_id,
        {
            "telegram_user_id": 700703,
            "telegram_message_id": 12,
            "username": "CryptoBot",
            "name": "Crypto Bot",
            "text": "Invoice paid",
            "sent_at": "2026-11-17T12:02:00",
            "direction": "in",
        },
    )
    assert (
        send_lead_reply_notification(
            conn,
            lead_handle="@random_stranger",
            account_handle="Alex",
            account_id=alex_acc_id,
            peer_id=700799,
        )
        is False
    )
    assert len(calls) == 1
