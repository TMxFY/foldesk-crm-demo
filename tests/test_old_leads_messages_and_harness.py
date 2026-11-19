from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from email.message import Message
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.analytics import get_analytics_summary
from app.db import connect, init_db, utc_now, utc_today
from app.funnel_analytics import ensure_funnel_columns, get_outreach_funnel
from app.inbox import (
    _person_is_crm_relevant,
    apply_incoming,
    list_inbox,
    list_messages,
)
from app.leads import create_lead, get_client_full_profile
from app.main import (
    _filter_people_to_existing_or_crm,
    create_app,
    sync_crm_dialog_messages_backfill_once,
)
from app.outreach import _sync_campaign_replies, create_campaign
from app.telegram_gateway import FakeGateway, TelethonGateway

WORKTREE_ROOT = Path(__file__).resolve().parents[1]


def _setup_db(tmp_path: Path, name: str = "old_leads_test.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    ensure_funnel_columns(conn)
    return conn


def _make_account(conn, *, phone: str, username: str, tg_uid: int, health: str = "active") -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, health, telegram_user_id,
            telegram_username, telegram_name, daily_limit, created_at, updated_at
        ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 20, ?, ?)
        """,
        (phone, f"data/sessions/{username}.session", health, tg_uid, username, username, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_completed_and_contacted_failed_late_replies_count_and_v11_hygiene_blocked(tmp_path):
    conn = _setup_db(tmp_path, "completed_failed.db")
    acc = _make_account(conn, phone="+79990010001", username="sender_a", tg_uid=10001)
    camp = create_campaign(conn, name="SoftUGC Outbound", account_id=acc, steps=[{"template_text": "Hey"}])
    now = utc_now()

    # 1. Completed recipient from weeks ago who now replies
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 51001, 'old_completed_ceo', 'Old Completed', 'AcmeSaaS', 3, 'completed', ?, ?, '2026-10-15T10:00:00', ?, ?)
        """,
        (camp["id"], acc, acc, now, now),
    )
    # 2. Contacted non-hygiene failed (step1 sent, failed on step2) who now replies
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, error_text, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 51002, 'step1_failed_then_replied', 'Partial Failed', 'BetaApp', 1, 'failed', 'peer_flood_step2', ?, ?, '2026-10-20T10:00:00', ?, ?)
        """,
        (camp["id"], acc, acc, now, now),
    )
    # 3. v11 personal hygiene failed row — MUST remain excluded
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name,
            current_step, status, error_text, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 51003, 'personal_ozon_v11', 'Personal Ozon', 1, 'failed', 'pre_softugc_personal_dm_hygiene_v11', ?, ?, '2026-06-01T10:00:00', ?, ?)
        """,
        (camp["id"], acc, acc, now, now),
    )
    conn.commit()

    # Ingest / CRM relevance guards must accept completed & non-hygiene failed, reject v11 personal
    assert _person_is_crm_relevant(conn, acc, 51001, "old_completed_ceo", None) is True
    assert _person_is_crm_relevant(conn, acc, 51002, "step1_failed_then_replied", None) is True
    assert _person_is_crm_relevant(conn, acc, 51003, "personal_ozon_v11", None) is False

    kept_people = _filter_people_to_existing_or_crm(
        conn,
        acc,
        [
            {"telegram_user_id": 51001, "username": "old_completed_ceo", "name": "Old Completed"},
            {"telegram_user_id": 51002, "username": "step1_failed_then_replied", "name": "Partial Failed"},
            {"telegram_user_id": 51003, "username": "personal_ozon_v11", "name": "Personal Ozon"},
        ],
    )
    assert [p["username"] for p in kept_people] == ["old_completed_ceo", "step1_failed_then_replied"]

    # Incoming messages from both late responders -> must auto-create leads & flip recipients to 'replied'
    apply_incoming(
        conn,
        acc,
        {
            "telegram_user_id": 51001,
            "username": "old_completed_ceo",
            "name": "Old Completed",
            "telegram_message_id": 201,
            "text": "Hey! Saw your old note, send examples please",
            "sent_at": utc_now(),
        },
    )
    apply_incoming(
        conn,
        acc,
        {
            "telegram_user_id": 51002,
            "username": "step1_failed_then_replied",
            "name": "Partial Failed",
            "telegram_message_id": 202,
            "text": "Yes interested, let's book a call",
            "sent_at": utc_now(),
        },
    )

    rec1 = conn.execute("SELECT status, lead_id FROM campaign_recipients WHERE telegram_user_id = 51001").fetchone()
    rec2 = conn.execute("SELECT status, lead_id FROM campaign_recipients WHERE telegram_user_id = 51002").fetchone()
    rec3 = conn.execute("SELECT status, lead_id FROM campaign_recipients WHERE telegram_user_id = 51003").fetchone()
    assert rec1["status"] == "replied" and rec1["lead_id"] is not None
    assert rec2["status"] == "replied" and rec2["lead_id"] is not None
    assert rec3["status"] == "failed" and rec3["lead_id"] is None

    visible = list_inbox(conn, 0, "", crm_only=True)
    assert {r["username"] for r in visible} == {"old_completed_ceo", "step1_failed_then_replied"}

    summary = get_analytics_summary(conn, period="7d")
    deep_sum = summary["deep_outreach"]["summary"]
    assert deep_sum["replied"] == 2
    assert deep_sum["waiting_our_answer"] == 2
    funnel = get_outreach_funnel(conn, campaign_id=camp["id"])
    assert funnel["summary"]["replied_total"] == 2


def test_load_private_preserves_media_only_preview_without_text():
    from telethon.tl.types import DocumentAttributeAudio, MessageMediaDocument, User

    gw = TelethonGateway()
    user = User(id=61001, first_name="Voice", last_name="Client", username="voice_client", bot=False)
    voice_doc = SimpleNamespace(
        mime_type="audio/ogg",
        attributes=[DocumentAttributeAudio(duration=9, voice=True)],
    )
    raw_msg = SimpleNamespace(
        id=909,
        message=None,
        date=datetime.now(timezone.utc),
        out=False,
        voice=True,
        video_note=None,
        photo=None,
        audio=None,
        video=None,
        document=voice_doc,
        media=MessageMediaDocument(document=voice_doc),
    )
    raw_dlg = SimpleNamespace(read_inbox_max_id=10, read_outbox_max_id=5)
    dlg = SimpleNamespace(
        name="Voice Client",
        is_user=True,
        is_group=False,
        is_channel=False,
        entity=user,
        message=raw_msg,
        dialog=raw_dlg,
        unread_count=1,
    )

    class _FakeClient:
        async def iter_dialogs(self):
            yield dlg

    async def _run():
        return await gw._load_private(_FakeClient(), limit=30)

    items = asyncio.run(_run())
    assert len(items) == 1
    assert items[0]["telegram_message_id"] == 909
    assert items[0]["last_message_preview"] == "[Голосовое сообщение]"
    assert items[0]["last_from_peer"] == 1


def test_tme_username_normalization_matches_dialog_to_lead(tmp_path):
    conn = _setup_db(tmp_path, "tme_norm.db")
    acc = _make_account(conn, phone="+79990010002", username="sender_tme", tg_uid=10002)
    now = utc_now()

    lead = create_lead(
        conn,
        name="Tme URL Lead",
        username="https://t.me/old_tme_client",
        status="waiting",
        account_id=acc,
    )
    assert lead["username"] == "old_tme_client"

    # Simulate legacy unnormalized URL directly stored in DB from Airtable import
    conn.execute("UPDATE leads SET username = 'https://t.me/old_tme_client?start=1' WHERE id = ?", (lead["id"],))
    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            lead_id, created_at, updated_at
        ) VALUES (?, 'user', 62001, 'old_tme_client', 'Old Tme', 0, 1, ?, 'Ping back!', 1, NULL, ?, ?)
        """,
        (acc, now, now, now),
    )
    conn.commit()

    rows = list_inbox(conn, 0, "", crm_only=True)
    assert len(rows) == 1
    assert rows[0]["lead_id"] == lead["id"]
    assert rows[0]["lead_status"] == "waiting"


def test_thread_backfill_on_frozen_account_and_snapshot_fallback_on_unauthorized(tmp_path):
    conn = _setup_db(tmp_path, "backfill_frozen_unauth.db")
    acc_frozen = _make_account(conn, phone="+79990010003", username="sender_frozen", tg_uid=10003, health="frozen")
    acc_unauth = _make_account(conn, phone="+79990010004", username="sender_unauth", tg_uid=10004, health="unauthorized")
    now = utc_now()
    today = utc_today()

    lead_f = create_lead(conn, name="Frozen Client", username="frozen_client", telegram_user_id=63001, status="talking", account_id=acc_frozen)
    lead_u = create_lead(conn, name="Unauth Client", username="unauth_client", telegram_user_id=63002, status="talking", account_id=acc_unauth)

    dlg_f = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, created_at, updated_at
            ) VALUES (?, 'user', 63001, 'frozen_client', 'Frozen Client', 0, 1, ?, 'Fresh live reply on frozen', 1, ?, ?, ?)
            """,
            (acc_frozen, f"{today}T12:00:00", lead_f["id"], now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 1, 'out', 'Old ping', ?, ?, ?)",
        (dlg_f, "2026-10-01T10:00:00", now, now),
    )

    dlg_u = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, created_at, updated_at
            ) VALUES (?, 'user', 63002, 'unauth_client', 'Unauth Client', 0, 1, ?, 'Snapshot preview newer msg', 1, ?, ?, ?)
            """,
            (acc_unauth, f"{today}T13:00:00", lead_u["id"], now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 1, 'out', 'Older unauth msg', ?, ?, ?)",
        (dlg_u, "2026-10-02T10:00:00", now, now),
    )
    conn.commit()

    # Before backfill: snapshot fallback must already expose newest message in list_messages, profile & daily_flow
    pre_msgs_u = list_messages(conn, dlg_u, 50)
    assert pre_msgs_u[-1]["is_snapshot_preview"] is True
    assert pre_msgs_u[-1]["text"] == "Snapshot preview newer msg"

    prof_u = get_client_full_profile(conn, dialog_id=dlg_u)
    assert prof_u["recent_messages"][0]["is_snapshot_preview"] is True
    assert prof_u["recent_messages"][0]["text"] == "Snapshot preview newer msg"
    assert prof_u["stats"]["total_messages"] == 2

    gw = FakeGateway(
        history={
            63001: [
                {"telegram_message_id": 1, "direction": "out", "text": "Old ping", "sent_at": "2026-10-01T10:00:00"},
                {"telegram_message_id": 2, "direction": "in", "text": "Fresh live reply on frozen", "sent_at": f"{today}T12:00:00"},
            ]
        }
    )
    app = create_app(conn=conn, gateway=gw)
    res = asyncio.run(sync_crm_dialog_messages_backfill_once(app, max_dialogs=10))
    assert res["ok"] is True
    assert res["backfilled"] >= 1

    post_msgs_f = list_messages(conn, dlg_f, 50)
    assert any(m["telegram_message_id"] == 2 and m["text"] == "Fresh live reply on frozen" for m in post_msgs_f)

    # Unauthorized account still has snapshot reply counted in today's daily_flow
    analytics = get_analytics_summary(conn, period="7d")
    today_flow = [d for d in analytics["charts"]["daily_flow"] if d["date"] == today]
    assert len(today_flow) == 1
    assert today_flow[0]["replies_received"] >= 2


def test_old_won_lead_unread_counts_in_waiting_without_colleague_alert(tmp_path, monkeypatch):
    conn = _setup_db(tmp_path, "won_unread.db")
    acc = _make_account(conn, phone="+79990010005", username="sender_won", tg_uid=10005)
    now = utc_now()

    sent_alerts: list[dict] = []
    monkeypatch.setenv("BOT_TOKEN", "999999:FakeToken")
    monkeypatch.setenv("BOT_CHAT_ID", "-100999888")
    monkeypatch.delenv("DISABLE_BOT_ALERTS", raising=False)

    class _FakeResp:
        status = 200
        headers = Message()

        def __init__(self, body: dict):
            self._raw = json.dumps(body).encode("utf-8")

        def read(self) -> bytes:
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(req, timeout=10):
        body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else {}
        sent_alerts.append({"url": req.full_url, "body": body})
        return _FakeResp({"ok": True, "result": {"message_id": 8000 + len(sent_alerts)}})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    won_lead = create_lead(
        conn,
        name="Old Won VIP",
        username="old_won_vip",
        telegram_user_id=64001,
        status="won",
        amount=5000,
        account_id=acc,
    )
    apply_incoming(
        conn,
        acc,
        {
            "telegram_user_id": 64001,
            "username": "old_won_vip",
            "name": won_lead["name"],
            "telegram_message_id": 501,
            "text": "Привет! Хотим продлить ещё на 3 месяца!",
            "sent_at": now,
        },
    )
    # Playbook 1: closed won lead must never spam colleague bot alerts
    assert sent_alerts == []

    # Sanity-check mock wiring: an active talking lead DOES fire 1 alert
    talking_lead = create_lead(
        conn,
        name="Active Talking Lead",
        username="active_talking_vip",
        telegram_user_id=64002,
        status="talking",
        account_id=acc,
    )
    apply_incoming(
        conn,
        acc,
        {
            "telegram_user_id": 64002,
            "username": "active_talking_vip",
            "name": talking_lead["name"],
            "telegram_message_id": 502,
            "text": "Готовы обсудить детали",
            "sent_at": now,
        },
    )
    assert len(sent_alerts) == 1

    # Yet stats MUST count the unread message from the won client too!
    summary = get_analytics_summary(conn, period="7d")
    assert summary["deep_outreach"]["summary"]["waiting_our_answer"] == 2
    assert summary["deep_outreach"]["summary"]["direct_client_replies"] >= 2


def test_proactive_audit_selftest_and_git_hook_fail_closed_gate_m(tmp_path):
    python_bin = sys.executable
    selftest_res = subprocess.run(
        [python_bin, str(WORKTREE_ROOT / "scripts" / "proactive_audit.py"), "--self-test"],
        cwd=str(WORKTREE_ROOT),
        env={**os.environ, "PYTHONPATH": str(WORKTREE_ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert selftest_res.returncode == 0, f"stdout={selftest_res.stdout}\nstderr={selftest_res.stderr}"

    # Verify githooks/pre-commit exits 1 when Gate M invariant ('completed') is broken in a temp git repo
    repo = tmp_path / "hook_repo"
    shutil.copytree(
        WORKTREE_ROOT / "app",
        repo / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        WORKTREE_ROOT / "scripts",
        repo / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(WORKTREE_ROOT / "deploy", repo / "deploy")
    shutil.copytree(WORKTREE_ROOT / "githooks", repo / "githooks")
    shutil.copytree(WORKTREE_ROOT / ".claude", repo / ".claude")
    shutil.copytree(WORKTREE_ROOT / "replica", repo / "replica")
    shutil.copytree(WORKTREE_ROOT / "docs", repo / "docs")
    shutil.copytree(
        WORKTREE_ROOT / "leadgen",
        repo / "leadgen",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (repo / "app" / "templates" / "index.html").write_text(
        (WORKTREE_ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "githooks" / "pre-commit").chmod(0o755)
    (repo / "githooks" / "pre-push").chmod(0o755)

    # Break Gate M by stripping is_recipient_contacted_outreach inside _person_is_crm_relevant in app/inbox.py
    inbox_text = (repo / "app" / "inbox.py").read_text(encoding="utf-8")
    marker = "def _person_is_crm_relevant("
    pos = inbox_text.find(marker)
    assert pos >= 0
    head, tail = inbox_text[:pos], inbox_text[pos:]
    assert "is_recipient_contacted_outreach(" in tail
    broken_inbox = head + tail.replace("is_recipient_contacted_outreach(", "bool(", 1)
    assert broken_inbox != inbox_text
    (repo / "app" / "inbox.py").write_text(broken_inbox, encoding="utf-8")

    env = {**os.environ, "PYTHON_BIN": python_bin}
    fail_res = subprocess.run(
        ["bash", str(repo / "githooks" / "pre-commit")],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert fail_res.returncode != 0
    assert "Gate M" in fail_res.stderr


def test_sniper_icp_completed_step0_excluded_everywhere(tmp_path):
    conn = _setup_db(tmp_path, "sniper_icp_completed.db")
    acc = _make_account(conn, phone="+79990010011", username="sender_sniper", tg_uid=10011)
    camp = create_campaign(conn, name="SoftUGC Outbound", account_id=acc, steps=[{"template_text": "Hey"}])
    now = utc_now()

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, error_text, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 71001, 'sniper_junk_biz', 'Junk Biz', 'AgencyXYZ', 0, 'completed', 'Остановлено Sniper ICP-фильтром: нерелевантный сервис', NULL, ?, NULL, ?, ?)
        """,
        (camp["id"], acc, now, now),
    )
    conn.commit()

    assert _person_is_crm_relevant(conn, acc, 71001, "sniper_junk_biz", None) is False
    assert (
        _filter_people_to_existing_or_crm(
            conn, acc, [{"telegram_user_id": 71001, "username": "sniper_junk_biz", "name": "Junk Biz"}]
        )
        == []
    )

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            created_at, updated_at
        ) VALUES (?, 'user', 71001, 'sniper_junk_biz', 'Junk Biz', 0, 1, ?, 'Random chat', 1, ?, ?)
        """,
        (acc, now, now, now),
    )
    conn.commit()

    assert list_inbox(conn, 0, "", crm_only=True) == []
    analytics = get_analytics_summary(conn, period="7d")
    assert analytics["deep_outreach"]["summary"]["contacted"] == 0
    assert analytics["deep_outreach"]["summary"]["waiting_our_answer"] == 0
    funnel = get_outreach_funnel(conn, campaign_id=camp["id"])
    assert funnel["summary"]["contacted_total"] == 0


def test_multiaccount_sync_campaign_replies_finds_sent_account_dialog(tmp_path):
    conn = _setup_db(tmp_path, "multiacc_sync.db")
    acc_assigned = _make_account(conn, phone="+79990010012", username="sender_assigned", tg_uid=10012)
    acc_sent = _make_account(conn, phone="+79990010013", username="sender_actual", tg_uid=10013)
    camp = create_campaign(conn, name="SoftUGC Multi", account_id=acc_assigned, steps=[{"template_text": "Hey"}])
    now = utc_now()

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            current_step, status, sent_account_id, assigned_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 72001, 'split_acc_lead', 'Split Lead', 'SplitApp', 1, 'completed', ?, ?, '2026-11-10T10:00:00', ?, ?)
        """,
        (camp["id"], acc_sent, acc_assigned, now, now),
    )
    # Reply landed on acc_sent (while acc_assigned was originally assigned)
    dlg_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                created_at, updated_at
            ) VALUES (?, 'user', 72001, 'split_acc_lead', 'Split Lead', 0, 1, '2026-11-18T15:00:00', 'Интересно, пришлите условия', 1, ?, ?)
            """,
            (acc_sent, now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 1, 'in', 'Интересно, пришлите условия', '2026-11-18T15:00:00', ?, ?)",
        (dlg_id, now, now),
    )
    conn.commit()

    synced = _sync_campaign_replies(conn, camp["id"], account_id=acc_assigned)
    assert synced == 1
    rec = conn.execute("SELECT status, lead_id FROM campaign_recipients WHERE telegram_user_id = 72001").fetchone()
    assert rec["status"] == "replied" and rec["lead_id"] is not None


def test_repeat_short_snapshot_reply_preserved_in_thread_profile_and_daily_flow(tmp_path):
    conn = _setup_db(tmp_path, "repeat_short_snapshot.db")
    acc = _make_account(conn, phone="+79990010014", username="sender_short", tg_uid=10014, health="unauthorized")
    now = utc_now()
    today = utc_today()

    lead = create_lead(conn, name="Short Repeat", username="short_repeat", telegram_user_id=73001, status="talking", account_id=acc)
    dlg_id = int(
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, created_at, updated_at
            ) VALUES (?, 'user', 73001, 'short_repeat', 'Short Repeat', 0, 1, ?, 'Ок', 1, ?, ?, ?)
            """,
            (acc, f"{today}T10:10:00", lead["id"], now, now),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, 1, 'in', 'Ок', ?, ?, ?)",
        (dlg_id, f"{today}T10:00:00", now, now),
    )
    conn.commit()

    msgs = list_messages(conn, dlg_id, 50)
    assert len(msgs) == 2
    assert msgs[-1]["is_snapshot_preview"] is True
    assert msgs[-1]["text"] == "Ок"

    prof = get_client_full_profile(conn, dialog_id=dlg_id)
    assert prof["stats"]["total_messages"] == 2
    assert prof["recent_messages"][0]["is_snapshot_preview"] is True

    analytics = get_analytics_summary(conn, period="7d")
    today_flow = [d for d in analytics["charts"]["daily_flow"] if d["date"] == today]
    assert len(today_flow) == 1
    assert today_flow[0]["replies_received"] == 2


def test_analytics_summary_chunks_over_1050_eligible_crm_dialogs(tmp_path):
    conn = _setup_db(tmp_path, "chunk_over_1050.db")
    acc = _make_account(conn, phone="+79990010015", username="sender_scale", tg_uid=10015)
    now = utc_now()
    today = utc_today()

    for idx in range(1055):
        peer = 800000 + idx
        uname = f"scale_lead_{idx}"
        lid = int(
            conn.execute(
                """
                INSERT INTO leads (name, username, telegram_user_id, status, account_id, created_at, updated_at)
                VALUES (?, ?, ?, 'waiting', ?, ?, ?)
                """,
                (f"Scale Lead {idx}", uname, peer, acc, now, now),
            ).lastrowid
        )
        did = int(
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, is_group,
                    unread_count, last_message_at, last_message_preview, last_from_peer,
                    lead_id, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 0, 0, ?, 'Ping', 1, ?, ?, ?)
                """,
                (acc, peer, uname, f"Scale Lead {idx}", f"{today}T09:00:00", lid, now, now),
            ).lastrowid
        )
        if idx < 100:
            conn.execute(
                "INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at) VALUES (?, ?, 'in', 'Ping', ?, ?, ?)",
                (did, idx + 1, f"{today}T09:00:00", now, now),
            )
    conn.commit()

    analytics = get_analytics_summary(conn, period="7d")
    today_flow = [d for d in analytics["charts"]["daily_flow"] if d["date"] == today]
    assert len(today_flow) == 1
    assert today_flow[0]["replies_received"] >= 100
