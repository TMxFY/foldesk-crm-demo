import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import warnings

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

import app.accounts as accounts_mod
import app.digest as digest_mod
import app.outreach as outreach_mod
from app.accounts import probe_account
from app.db import DEFAULT_DB, connect, init_db
from app.outreach import (
    create_campaign,
    enroll_leads_from_crm,
    get_campaign,
    import_campaign_csv,
    run_autopilot_tick,
    run_campaign_batch,
    update_outreach_schedule,
)
from app.telegram_gateway import FakeGateway, GatewayError, TelegramGateway, TelethonGateway


VALID_B2B_STEP1 = (
    "Hey,\n\nSaw AcmeCloud. Getting steady B2B signups at predictable CAC is tough — "
    "SoftUGC runs an organic short-video growth engine (183.5M views). Open to test?"
)


class _ControlledGateway(TelegramGateway):
    def __init__(
        self,
        *,
        live_messages_by_session_peer: dict[tuple[str, int], list[dict]] | None = None,
        fail_sessions: dict[str, Exception] | None = None,
        invalid_usernames: set[str] | None = None,
        client=None,
    ):
        self.sent: list[dict] = []
        self.send_attempts: list[dict] = []
        self.live_messages_by_session_peer = dict(live_messages_by_session_peer or {})
        self.fail_sessions = dict(fail_sessions or {})
        self.invalid_usernames = {u.lower().lstrip("@") for u in (invalid_usernames or set())}
        self.client = client
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_path: str) -> asyncio.Lock:
        lock = self._locks.get(session_path)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_path] = lock
        return lock

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        clean = (query or "").strip().lstrip("@").lower()
        if clean in self.invalid_usernames:
            return []
        if session_path in self.fail_sessions:
            raise self.fail_sessions[session_path]
        fake_id = 100000 + (sum(ord(ch) for ch in clean) % 700000)
        return [{"telegram_user_id": fake_id, "username": clean, "name": clean.title()}]

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        if session_path in self.fail_sessions:
            raise self.fail_sessions[session_path]
        return list(self.live_messages_by_session_peer.get((session_path, int(peer_id)), []))

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to: int | None = None,
        image_path=None,
    ) -> dict:
        self.send_attempts.append({"session_path": session_path, "peer_id": peer_id, "text": text})
        if session_path in self.fail_sessions:
            raise self.fail_sessions[session_path]
        peer_str = str(peer_id).strip().lstrip("@").lower()
        if peer_str in self.invalid_usernames:
            raise GatewayError(f'No user has "{peer_str}" as username')
        entry = {"session_path": session_path, "peer_id": peer_id, "text": text, "image_path": image_path}
        self.sent.append(entry)
        return {"telegram_message_id": 6000 + len(self.sent), "sent_at": outreach_mod.utc_now()}


def _set_clock(monkeypatch, iso_ts: str) -> None:
    day = iso_ts[:10]
    monkeypatch.setattr(outreach_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(outreach_mod, "utc_today", lambda: day)
    monkeypatch.setattr(accounts_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(accounts_mod, "utc_today", lambda: day)


def _seed_account(
    conn,
    account_id: int,
    *,
    phone: str | None = None,
    session_path: str | None = None,
    health: str = "active",
    daily_limit: int = 20,
    sent_today: int = 0,
    sent_date: str = "2026-11-16",
) -> None:
    now = "2026-11-16T08:00:00"
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, telegram_name,
            daily_limit, sent_today, sent_date, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            phone or f"+1555000{account_id:04d}",
            session_path or f"data/sessions/acc{account_id}.session",
            health,
            f"Acc {account_id}",
            daily_limit,
            sent_today,
            sent_date,
            now,
            now,
        ),
    )
    conn.commit()


def _seed_prior_outgoing(
    conn,
    *,
    account_id: int,
    peer_id: int,
    username: str,
    text: str = VALID_B2B_STEP1,
    sent_at: str = "2026-11-15T19:00:00",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title,
            last_message_at, last_message_preview, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, peer_id, username, username, sent_at, text[:80], sent_at, sent_at),
    )
    dlg_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 101, 'out', ?, ?, ?, ?)
        """,
        (dlg_id, text, sent_at, sent_at, sent_at),
    )
    conn.commit()
    return dlg_id


def test_followup_reservation_capped_at_half_daily_limit_prevents_step1_starvation(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_cap50.db")
    init_db(conn)
    _seed_account(conn, 21, daily_limit=20, sent_today=0, sent_date="2026-11-16")
    _set_clock(monkeypatch, "2026-11-16T08:05:00")

    camp = create_campaign(
        conn,
        name="SoftUGC B2B SaaS Cap50",
        account_ids=[21],
        daily_limit=20,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Quick follow-up for {company} — open to test SoftUGC?"},
            {"step_order": 3, "delay_hours": 48, "template_text": "Final note on {company} organic short-video reach."},
        ],
    )

    yest_evening = "2026-11-15T19:00:00"
    for idx in range(1, 21):
        peer_id = 61000 + idx
        uname = f"evening_warm_{idx}"
        _seed_prior_outgoing(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_evening)
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, assigned_account_id, sent_account_id, current_step,
                status, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'B2B SaaS', ?, 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Warm {idx}", f"WarmCo{idx}", VALID_B2B_STEP1, yest_evening, yest_evening, yest_evening),
        )

    for idx in range(1, 16):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'B2B SaaS', ?, 0, 'pending', '2026-11-16T07:00:00', '2026-11-16T07:00:00')
            """,
            (
                camp["id"],
                62000 + idx,
                f"fresh_b2b_{idx}",
                f"Founder {idx}",
                f"CloudSaaS{idx}",
                f"Hey Founder {idx}, saw CloudSaaS{idx}. SoftUGC generated 183.5M organic views without shoots. Worth a quick look?",
            ),
        )
    conn.commit()

    gw = _ControlledGateway()
    res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=20, respect_delays=True))
    # 20 evening follow-ups must NOT starve 100% of daily_limit=20; reserve is capped at 50% (10), allowing 10 Step 1 sends!
    assert res["sent"] == 10
    assert len(gw.sent) == 10


def test_autopilot_intraday_pacing_spreads_sends_across_schedule_window(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_pacing.db")
    init_db(conn)
    _seed_account(conn, 21, daily_limit=20, sent_today=0, sent_date="2026-11-16")
    update_outreach_schedule(
        conn,
        timezone_name="UTC",
        days={
            "mon": {"enabled": True, "start": "08:00", "end": "18:00"},
            "tue": {"enabled": True, "start": "08:00", "end": "18:00"},
        },
        autopilot_enabled=True,
    )

    camp = create_campaign(
        conn,
        name="SoftUGC Paced Autopilot",
        account_ids=[21],
        daily_limit=20,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Quick follow-up on {company} growth with SoftUGC."},
        ],
    )
    for idx in range(1, 26):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', '2026-11-16T07:00:00', '2026-11-16T07:00:00')
            """,
            (
                camp["id"],
                63000 + idx,
                f"paced_lead_{idx}",
                f"Paced {idx}",
                f"PacedApp{idx}",
                f"Hey Paced {idx}, saw PacedApp{idx}. SoftUGC drives predictable user acquisition via short-form video.",
            ),
        )
    conn.commit()

    gw = _ControlledGateway()
    # 2026-11-16 is Monday. At 08:02 UTC (elapsed=2m of 600m): ceil(20 * (32 / 600)) = 2
    _set_clock(monkeypatch, "2026-11-16T08:02:00")
    t1 = asyncio.run(
        run_autopilot_tick(
            conn,
            gateway=gw,
            now_dt=datetime(2026, 11, 16, 8, 2, tzinfo=timezone.utc),
            max_per_campaign=20,
            jitter_range=(0.0, 0.0),
        )
    )
    assert t1["sent"] == 2

    # 3 minutes later at 08:05 UTC: still capped at 2 -> sends 0
    _set_clock(monkeypatch, "2026-11-16T08:05:00")
    t2 = asyncio.run(
        run_autopilot_tick(
            conn,
            gateway=gw,
            now_dt=datetime(2026, 11, 16, 8, 5, tzinfo=timezone.utc),
            max_per_campaign=20,
            jitter_range=(0.0, 0.0),
        )
    )
    assert t2["sent"] == 0

    # At 13:00 UTC (elapsed=300m of 600m): ceil(20 * (330 / 600)) = 11 -> sends 9 more (total 11)
    _set_clock(monkeypatch, "2026-11-16T13:00:00")
    t3 = asyncio.run(
        run_autopilot_tick(
            conn,
            gateway=gw,
            now_dt=datetime(2026, 11, 16, 13, 0, tzinfo=timezone.utc),
            max_per_campaign=20,
            jitter_range=(0.0, 0.0),
        )
    )
    assert t3["sent"] == 9
    assert len(gw.sent) == 11

    # At 17:35 UTC (elapsed=575m of 600m): allowed_by_now = 20 -> sends remaining 9 (total 20)
    _set_clock(monkeypatch, "2026-11-16T17:35:00")
    t4 = asyncio.run(
        run_autopilot_tick(
            conn,
            gateway=gw,
            now_dt=datetime(2026, 11, 16, 17, 35, tzinfo=timezone.utc),
            max_per_campaign=20,
            jitter_range=(0.0, 0.0),
        )
    )
    assert t4["sent"] == 9
    assert len(gw.sent) == 20


def test_orphaned_followups_from_unauthorized_or_deleted_account_self_heal_and_release_quota(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_orphan.db")
    init_db(conn)
    _seed_account(conn, 21, health="unauthorized", daily_limit=20, sent_today=0, sent_date="2026-11-16")
    _seed_account(conn, 30, health="active", daily_limit=10, sent_today=0, sent_date="2026-11-16")
    _set_clock(monkeypatch, "2026-11-16T10:00:00")

    camp = create_campaign(
        conn,
        name="SoftUGC Orphan Self-Heal",
        account_ids=[21, 30],
        daily_limit=10,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Follow-up on SoftUGC short-video distribution."},
        ],
    )

    old_ts = "2026-11-13T10:00:00"
    for idx in range(1, 13):
        peer_id = 64000 + idx
        uname = f"orphan_21_{idx}"
        _seed_prior_outgoing(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=old_ts)
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, assigned_account_id, sent_account_id, current_step,
                status, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'OrphanCo', 'SaaS', ?, 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Orphan {idx}", VALID_B2B_STEP1, old_ts, old_ts, old_ts),
        )

    # Also 2 orphaned follow-ups: one from delete_account(98), one with missing sent_account_id=99
    _seed_account(conn, 98, health="active", daily_limit=10, sent_today=0, sent_date="2026-11-16")
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, assigned_account_id, sent_account_id, current_step,
            status, last_sent_at, created_at, updated_at
        ) VALUES (?, 64901, 'orphan_deleted_1', 'DeletedAcc 1', 'DeletedAccCo', 'SaaS', ?, 98, 98, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], VALID_B2B_STEP1, old_ts, old_ts, old_ts),
    )
    accounts_mod.delete_account(conn, 98)
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, assigned_account_id, sent_account_id, current_step,
                status, last_sent_at, created_at, updated_at
            ) VALUES (?, 64902, 'orphan_deleted_2', 'DeletedAcc 2', 'DeletedAccCo', 'SaaS', ?, NULL, 99, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], VALID_B2B_STEP1, old_ts, old_ts, old_ts),
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
            """,
            (
                camp["id"],
                65000 + idx,
                f"healthy_cold_{idx}",
                f"Healthy {idx}",
                f"HealthySaaS{idx}",
                VALID_B2B_STEP1,
            ),
        )
    conn.commit()

    gw = _ControlledGateway()
    res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res["sent"] == 5
    assert res["sent_by_account"] == {"30": 5}

    orphans = conn.execute(
        """
        SELECT username, status, error_text
        FROM campaign_recipients
        WHERE campaign_id = ? AND username LIKE 'orphan_%'
        """,
        (camp["id"],),
    ).fetchall()
    assert len(orphans) == 14
    for row in orphans:
        assert row["status"] == "completed"
        assert row["error_text"] == "Цепочка остановлена: исходный аккаунт деавторизован (unauthorized)"


def test_account_health_transition_to_unauthorized_or_frozen_emits_idempotent_single_alert(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_alert.db")
    init_db(conn)
    _seed_account(conn, 21, phone="+79991112233", session_path="data/sessions/acc21.session", health="active")
    conn.executemany(
        "INSERT OR REPLACE INTO kv_settings (key, value, updated_at) VALUES (?, ?, '2026-11-16T08:00:00')",
        [("bot_token", "test-bot-tok"), ("bot_chat_id", "123456")],
    )
    conn.commit()

    sent_alerts: list[dict] = []

    def _fake_post(url: str, payload: dict, opener):
        sent_alerts.append({"url": url, "payload": payload})
        return {"ok": True, "result": {"message_id": len(sent_alerts)}}

    monkeypatch.setattr(digest_mod, "_post", _fake_post)

    gw = FakeGateway(health="unauthorized")
    # 3 consecutive 30s health probes while unauthorized -> must alert ONCE, not spam every 30s!
    for _ in range(3):
        asyncio.run(probe_account(conn, 21, gw))

    assert len(sent_alerts) == 1
    assert "unauthorized" in sent_alerts[0]["payload"]["text"].lower() or "деавториз" in sent_alerts[0]["payload"]["text"].lower()

    # Recover to active, then transition to frozen -> emits 1 new alert (total 2), repeated frozen does not spam
    gw.health = "active"
    asyncio.run(probe_account(conn, 21, gw))
    gw.health = "frozen"
    asyncio.run(probe_account(conn, 21, gw))
    asyncio.run(probe_account(conn, 21, gw))
    assert len(sent_alerts) == 2

    log_row = conn.execute("SELECT value FROM kv_settings WHERE key = 'account_health_alerts_log'").fetchone()
    assert log_row is not None
    events = json.loads(log_row["value"])
    assert len(events) == 2
    assert [e["health"] for e in events] == ["unauthorized", "frozen"]


def test_hung_prewarm_recipient_stories_times_out_cleanly_without_blocking_step1_send(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_prewarm_timeout.db")
    init_db(conn)
    _seed_account(conn, 21, session_path="data/sessions/acc21.session", daily_limit=20)
    _set_clock(monkeypatch, "2026-11-16T10:00:00")

    camp = create_campaign(
        conn,
        name="SoftUGC Prewarm Timeout Guard",
        account_ids=[21],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 66001, 'hung_story_founder', 'Story Founder', 'StorySaaS', 'SaaS', ?, 0, 'pending', '2026-11-16T09:00:00', '2026-11-16T09:00:00')
        """,
        (camp["id"], VALID_B2B_STEP1),
    )
    conn.commit()

    async def _hung_prewarm(client, peer_or_username, reaction_emoticon="🔥"):
        await asyncio.sleep(60)
        return {"prewarmed": False}

    monkeypatch.setattr(outreach_mod, "prewarm_recipient_stories", _hung_prewarm)
    if hasattr(outreach_mod, "PREWARM_STORIES_TIMEOUT_SEC"):
        monkeypatch.setattr(outreach_mod, "PREWARM_STORIES_TIMEOUT_SEC", 0.05)

    gw = _ControlledGateway(client=object())

    async def _run_bounded():
        return await asyncio.wait_for(
            run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1),
            timeout=0.5,
        )

    res = asyncio.run(_run_bounded())
    assert res["sent"] == 1
    assert len(gw.sent) == 1
    assert not gw._session_lock("data/sessions/acc21.session").locked()


def test_floodwait_and_proxy_timeout_set_account_cooldown_and_invalidate_gateway_proxy_cache(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_cooldown.db")
    init_db(conn)
    _seed_account(conn, 21, session_path="data/sessions/acc21.session", daily_limit=20)
    _set_clock(monkeypatch, "2026-11-16T10:00:00")

    camp = create_campaign(
        conn,
        name="SoftUGC FloodWait Cooldown",
        account_ids=[21],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', '2026-11-16T09:00:00', '2026-11-16T09:00:00')
            """,
            (camp["id"], 67000 + idx, f"fw_lead_{idx}", f"FW {idx}", f"FWCo{idx}", VALID_B2B_STEP1),
        )
    conn.commit()

    gw = _ControlledGateway(
        fail_sessions={"data/sessions/acc21.session": GatewayError("A wait of 720 seconds is required (caused by SendMessageRequest)")}
    )
    r1 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1))
    assert r1["sent"] == 0
    assert len(gw.send_attempts) == 1

    # 30 seconds later (autopilot next tick): account 21 is on 720s cooldown -> MUST NOT hit gateway again!
    _set_clock(monkeypatch, "2026-11-16T10:00:30")
    gw.fail_sessions.clear()
    r2 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1))
    assert r2["sent"] == 0
    assert len(gw.send_attempts) == 1

    # After 725 seconds (10:12:05): cooldown has expired -> account 21 sends cleanly!
    _set_clock(monkeypatch, "2026-11-16T10:12:05")
    r3 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1))
    assert r3["sent"] == 1
    assert len(gw.send_attempts) == 2

    # TelethonGateway proxy cache invalidation on network/proxy connect failure + clean disconnect task tracking
    import telethon

    tg_gw = TelethonGateway()
    sess_path = str(tmp_path / "21.session")
    tg_gw.set_proxy(sess_path, "socks5://user:pass@dead-proxy.example.com:1080")
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    class _DeadProxyClient:
        def __init__(self, *args, **kwargs):
            pass

        def is_connected(self):
            return False

        async def connect(self):
            raise ConnectionError("SOCKS5 proxy connection refused")

        async def disconnect(self):
            pass

    monkeypatch.setattr(telethon, "TelegramClient", _DeadProxyClient)

    async def _exercise_gateway():
        with pytest.raises(ConnectionError):
            await tg_gw._open(sess_path)
        assert sess_path not in tg_gw._proxies
        await tg_gw.close_all()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(_exercise_gateway())
    assert not any("Task was destroyed" in str(w.message) for w in caught)


def test_manual_inbox_replies_do_not_burn_campaign_daily_limit_and_quarantine_skips_do_not_abort_batch(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_manual_and_batch.db")
    init_db(conn)
    _seed_account(conn, 21, daily_limit=20, sent_today=0, sent_date="2026-11-16")
    _set_clock(monkeypatch, "2026-11-16T12:00:00")

    camp = create_campaign(
        conn,
        name="SoftUGC Accurate Daily Limit",
        account_ids=[21],
        daily_limit=3,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Checking back on SoftUGC organic short-video pilot."},
        ],
    )

    # 3 recipients got Step 1 two days ago; today operator manually sent custom chat replies in inbox
    step1_sent_at = "2026-11-14T10:00:00"
    manual_today_at = "2026-11-16T11:30:00"
    for idx in range(1, 4):
        peer_id = 68000 + idx
        uname = f"manual_chat_{idx}"
        dlg_id = _seed_prior_outgoing(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=step1_sent_at)
        conn.execute(
            """
            INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
            VALUES (?, ?, 'out', 'Sure! Here is our custom Notion case study link', ?, ?, ?)
            """,
            (dlg_id, 900 + idx, manual_today_at, manual_today_at, manual_today_at),
        )
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, assigned_account_id, sent_account_id, current_step,
                status, last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'ManualCo', 'SaaS', ?, 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Manual {idx}", VALID_B2B_STEP1, step1_sent_at, step1_sent_at, step1_sent_at),
        )

    # Insert 6 invalid/quarantine rows BEFORE 2 valid fresh leads to verify failed_in_batch does NOT abort on 5th skip!
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'BadBrandCo', 'SaaS', 'Hey, regarding my last message on SoftUGC', 0, 'pending', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
            """,
            (camp["id"], 68100 + idx, f"bad_fu_step1_{idx}", f"BadFU {idx}"),
        )
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'GhostCo', 'SaaS', ?, 0, 'pending', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
            """,
            (camp["id"], 910000 + idx, f"nonexistent_ghost_{idx}", f"Ghost {idx}", VALID_B2B_STEP1),
        )

    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 68201, 'clean_after_skips', 'Clean Founder', 'CleanSaaS', 'SaaS', ?, 0, 'pending', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
        """,
        (camp["id"], VALID_B2B_STEP1),
    )
    conn.commit()

    gw = _ControlledGateway(
        invalid_usernames={"nonexistent_ghost_1", "nonexistent_ghost_2", "nonexistent_ghost_3"}
    )
    # First batch with max_send=1: manual messages from 30m ago delay follow-ups, 6 bad rows are quarantined without hitting max_failed_in_batch, and clean_after_skips is sent!
    res1 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1, respect_delays=True))
    assert res1["sent"] == 1
    assert gw.sent[0]["text"] == VALID_B2B_STEP1

    # Verify camp["sent_today"] is strictly 1 ( ONLY the 1 real campaign step sent today, NOT 1 + 3 manual replies = 4!)
    camp_after = get_campaign(conn, camp["id"])
    assert camp_after["sent_today"] == 1


def test_last_mile_icp_guard_blocks_broken_copy_and_non_icp_on_step1_and_followups(tmp_path, monkeypatch):
    conn = connect(tmp_path / "af_icp_guard.db")
    init_db(conn)
    _seed_account(conn, 21, daily_limit=20, sent_today=0, sent_date="2026-11-16")
    _set_clock(monkeypatch, "2026-11-16T10:00:00")

    camp = create_campaign(
        conn,
        name="SoftUGC Last-Mile ICP Guard",
        account_ids=[21],
        daily_limit=20,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Quick follow-up on scaling user acquisition with SoftUGC."},
        ],
    )

    old_ts = "2026-11-14T10:00:00"
    # Step 2 (in_progress) junk rows that slipped in earlier — MUST also be quarantined before sending follow-up!
    _seed_prior_outgoing(conn, account_id=21, peer_id=69001, username="step2_no_product", sent_at=old_ts)
    _seed_prior_outgoing(conn, account_id=21, peer_id=69002, username="step2_ugc_creator", sent_at=old_ts)
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category, note, their_angle,
            step1_text, assigned_account_id, sent_account_id, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 21, 21, 1, 'in_progress', ?, ?, ?)
        """,
        [
            (
                camp["id"],
                69001,
                "step2_no_product",
                "Unknown",
                "Mystery",
                "SaaS",
                "Недостаточно информации о продукте",
                "",
                VALID_B2B_STEP1,
                old_ts,
                old_ts,
                old_ts,
            ),
            (
                camp["id"],
                69002,
                "step2_ugc_creator",
                "Mia | Video creator | Freelancer",
                "Freelance",
                "UGC Creator",
                "Creates UGC videos for brands",
                "",
                VALID_B2B_STEP1,
                old_ts,
                old_ts,
                old_ts,
            ),
        ],
    )

    # Step 1 (pending) junk/broken rows
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category, note, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
        """,
        [
            (camp["id"], 69101, "short_hi", "Bob", "BobCo", "SaaS", "", "", "Hi,"),
            (camp["id"], 69102, "short_yc", "Sam", "SamAI", "AI", "", "", "YC S26"),
            (camp["id"], 69103, "shiller_guy", "Crypto Max", "ShillDAO", "Web3", "Web3 Shiller", "Web3 Shiller", VALID_B2B_STEP1),
            (camp["id"], 69104, "MatrixReach_bot", "MatrixReach Bot", "MatrixReach", "SaaS", "", "", VALID_B2B_STEP1),
            (camp["id"], 69105, "real_saas_ceo", "Elena", "LinearFlow", "B2B SaaS", "Workflow SaaS for engineering teams", "B2B SaaS", VALID_B2B_STEP1),
        ],
    )
    conn.commit()

    gw = _ControlledGateway(
        live_messages_by_session_peer={
            ("data/sessions/acc21.session", 69001): [{"telegram_message_id": 1, "direction": "out", "text": VALID_B2B_STEP1, "sent_at": old_ts}],
            ("data/sessions/acc21.session", 69002): [{"telegram_message_id": 2, "direction": "out", "text": VALID_B2B_STEP1, "sent_at": old_ts}],
        }
    )
    res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res["sent"] == 1
    assert len(gw.sent) == 1
    assert gw.sent[0]["peer_id"] == 69105

    quarantined = conn.execute(
        """
        SELECT username, status, error_text
        FROM campaign_recipients
        WHERE campaign_id = ? AND username != 'real_saas_ceo'
        """,
        (camp["id"],),
    ).fetchall()
    assert len(quarantined) == 6
    for q in quarantined:
        assert q["status"] == "completed"
        assert q["error_text"] == "Остановлено ICP-фильтром: нерелевантный профиль или пустой продукт"


def test_cli_default_db_path_and_completed_campaign_auto_wakeup_on_import_and_enroll(tmp_path, monkeypatch):
    monkeypatch.delenv("CRMTG_DB", raising=False)
    captured_db: list[str] = []

    def _spy_connect(db_path):
        captured_db.append(str(db_path))
        c = connect(tmp_path / "cli_check.db")
        init_db(c)
        return c

    monkeypatch.setattr("app.db.connect", _spy_connect)
    # Campaign 999 won't exist in empty tmp db so preview raises LookupError after connect(args.db)
    with pytest.raises(LookupError):
        outreach_mod.main(["--campaign-id", "999", "--dry-run"])
    assert captured_db == [str(DEFAULT_DB)]
    assert captured_db[0].endswith("data/crm.db")

    conn = connect(tmp_path / "af_wakeup.db")
    init_db(conn)
    _seed_account(conn, 21, daily_limit=20)
    camp = create_campaign(
        conn,
        name="SoftUGC Auto Wakeup",
        account_ids=[21],
        daily_limit=20,
        status="completed",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    assert camp["status"] == "completed"

    imp_res = import_campaign_csv(
        conn,
        camp["id"],
        f"username,name,company,step1_text\n@wake_csv_1,Wake One,WakeSaaS,\"{VALID_B2B_STEP1.replace(chr(10), ' ')}\"\n",
    )
    assert imp_res["campaign"]["status"] == "running"

    conn.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (camp["id"],))
    conn.execute(
        """
        INSERT INTO leads (account_id, telegram_user_id, username, name, company, niche, status, created_at, updated_at)
        VALUES (21, 777888, 'wake_crm_2', 'Wake Two', 'WakeCRM', 'SaaS', 'new', '2026-11-16T08:00:00', '2026-11-16T08:00:00')
        """
    )
    conn.commit()

    en_res = enroll_leads_from_crm(conn, camp["id"], status="new")
    assert en_res["imported_count"] == 1
    assert en_res["campaign"]["status"] == "running"


def test_vacancy_campaign_allows_employer_hiring_ugc_creator_role(tmp_path):
    """Live Canary repro: in 'TG Вакансии', company='UGC-креатор для AI-проекта' is the employer's open job role,
    NOT a freelancer bio — must send cleanly while still blocking UGC creators in B2B campaigns."""
    import asyncio
    from app.outreach import run_campaign_batch
    from app.telegram_gateway import FakeGateway

    conn = connect(tmp_path / "vac_icp.db")
    init_db(conn)
    _seed_account(conn, 27, daily_limit=20)
    vac_camp = create_campaign(
        conn,
        name="TG Вакансии",
        account_ids=[27],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    pitch = "Добрый день! По UGC-креатору в AI-проект. Предлагаю внедрить AI UGC с реалистичными ИИ-персонажами под ключ."
    import_campaign_csv(
        conn,
        vac_camp["id"],
        f"username,name,company,step1_text\n@wqwww90,UGC-креатор для AI-проекта (@wqwww90),UGC-креатор для AI-проекта,\"{pitch}\"\n",
    )
    gw = FakeGateway()
    res = asyncio.run(run_campaign_batch(conn, vac_camp["id"], gateway=gw, max_send=1, respect_delays=False))
    assert res["sent"] == 1
    row = conn.execute("SELECT status, current_step, error_text FROM campaign_recipients WHERE username = 'wqwww90'").fetchone()
    assert row["status"] == "completed" and row["current_step"] == 1 and not row["error_text"]
    conn.close()

