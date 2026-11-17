from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

import app.accounts as accounts_mod
import app.inbox as inbox_mod
import app.outreach as outreach_mod
from app.accounts import (
    _is_spambot_restriction_error,
    check_all_accounts,
    purge_dead_accounts,
)
from app.db import connect, init_db
from app.folders import pull_folders, sync_folder
from app.inbox import get_dialog_avatar_with_cache, get_thread, start_direct_dialog
from app.main import create_app
from app.outreach import (
    create_campaign,
    get_campaign,
    list_campaigns,
    run_autopilot_tick,
    run_campaign_batch,
    update_outreach_schedule,
)
from app.telegram_gateway import FakeGateway, TelegramGateway, TelethonGateway
from app.tools import convert_phones_to_tg, parse_telegram_group


VALID_PITCH = (
    "Hey,\n\nSaw AcmeCloud scaling fast. SoftUGC runs an organic short-video growth engine "
    "(183.5M views) on RevShare/CPA. Open to see 3 sample reels?"
)


def _set_clock(monkeypatch: pytest.MonkeyPatch, iso_ts: str) -> None:
    day = iso_ts[:10]
    monkeypatch.setattr(outreach_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(outreach_mod, "utc_today", lambda: day)
    monkeypatch.setattr(accounts_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(accounts_mod, "utc_today", lambda: day)


def _seed_account(
    conn,
    account_id: int,
    *,
    status: str = "connected",
    health: str = "active",
    last_error: str | None = None,
    session_path: str | None = None,
    username: str | None = None,
) -> str:
    sess = session_path or f"data/sessions/acc_{account_id}.session"
    now = "2026-11-17T08:00:00"
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, last_error,
            telegram_user_id, telegram_username, telegram_name,
            daily_limit, sent_today, sent_date, provider, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 20, 0, '2026-11-17', 'telegram', ?, ?)
        """,
        (
            account_id,
            f"+1555100{account_id:04d}",
            sess,
            status,
            health,
            last_error,
            910000 + account_id,
            username or f"acc_{account_id}",
            f"Account {account_id}",
            now,
            now,
        ),
    )
    conn.commit()
    return sess


class _StubGateway(TelegramGateway):
    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(
        self, phone: str, code: str, phone_code_hash: str, session_path: str
    ) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}


# ---------------------------------------------------------------------------
# 1. [P0] Raw @SpamBot / PeerFlood recognition without "spambot" word
# ---------------------------------------------------------------------------
def test_s2_1_spambot_raw_message_recognized_and_preserved_by_purge_and_healed_by_autopilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_en_spambot = (
        "Dear Sergey, your phone number was reported by some Telegram users. "
        "Unfortunately, our moderators have confirmed the report and your account is now limited "
        "until 17 Nov 2026, 08:11 UTC. While the account is limited, you will not be able "
        "to send messages to people who do not have your number in their phone contacts."
    )
    raw_ru_spambot = "К сожалению, ваш аккаунт временно ограничен до 17 Nov 2026, 08:11 UTC."
    raw_peerflood = "PeerFloodError: Too many requests (PEER_FLOOD)"

    assert "spambot" not in raw_en_spambot.casefold()
    assert _is_spambot_restriction_error(raw_en_spambot) is True
    assert _is_spambot_restriction_error(raw_ru_spambot) is True
    assert _is_spambot_restriction_error(raw_peerflood) is True
    assert _is_spambot_restriction_error("FROZEN_METHOD_INVALID") is False
    assert _is_spambot_restriction_error("UserDeactivatedBanError") is False
    assert _is_spambot_restriction_error(None) is False

    conn = connect(tmp_path / "s2_1.db")
    init_db(conn)
    sess11 = _seed_account(conn, 11, health="frozen", last_error=raw_en_spambot)
    sess12 = _seed_account(conn, 12, health="frozen", last_error=raw_peerflood)
    _seed_account(conn, 13, health="unauthorized", last_error="AuthKeyUnregisteredError")
    _seed_account(conn, 14, health="banned", last_error="UserDeactivatedBanError")

    purge_res = purge_dead_accounts(conn)
    assert purge_res["deleted_ids"] == [13, 14]
    remaining_ids = [
        int(r["id"]) for r in conn.execute("SELECT id FROM accounts ORDER BY id").fetchall()
    ]
    assert remaining_ids == [11, 12]

    # Autopilot tick after 08:11 UTC must probe @SpamBot on acc #11 (even without "@SpamBot:" prefix!)
    # and unfreeze it back to active so it sends pending outreach.
    _set_clock(monkeypatch, "2026-11-17T08:12:00")
    now_dt = datetime(2026, 11, 17, 8, 12, 0, tzinfo=timezone.utc)
    update_outreach_schedule(
        conn,
        timezone="UTC",
        days={
            k: {"enabled": True, "start": "00:00", "end": "23:59"}
            for k in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
        autopilot_enabled=True,
    )
    camp = create_campaign(
        conn,
        name="SoftUGC Raw SpamBot Unfreeze",
        account_ids=[11, 12],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 700101, 'founder_sergey_test', 'Founder', 'AcmeCloud', 'SaaS', ?, 0, 'pending', '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """,
        (camp["id"], VALID_PITCH),
    )
    conn.commit()

    outreach_mod._SPAMBOT_PROBE_CACHE.clear()
    outreach_mod._ACCOUNT_COOLDOWN_UNTIL.clear()

    gw = FakeGateway(
        dialogs=[
            {"telegram_user_id": 700101, "username": "founder_sergey_test", "name": "Founder"}
        ]
    )
    gw._pending[sess11] = {"phone": "+15551000011", "state": "connected"}
    gw._pending[sess12] = {"phone": "+15551000012", "state": "connected"}
    gw.spambot_replies = {
        sess11: "Good news, no limits are currently applied to your account. You’re free as a bird!",
        sess12: raw_en_spambot.replace("08:11 UTC", "23:59 UTC"),
    }

    tick_res = asyncio.run(
        run_autopilot_tick(
            conn,
            gateway=gw,
            now_dt=now_dt,
            max_send_per_campaign=1,
            jitter_range=None,
            force=True,
        )
    )
    assert sess11 in gw.spambot_starts
    assert tick_res["sent"] == 1
    acc11_row = conn.execute("SELECT health, last_error FROM accounts WHERE id = 11").fetchone()
    assert acc11_row["health"] == "active"
    assert acc11_row["last_error"] is None


# ---------------------------------------------------------------------------
# 2. [P0] Fail-closed when 2nd /start in probe_spambot gets empty/stale reply
# ---------------------------------------------------------------------------
def test_s2_2_probe_spambot_fail_closed_on_empty_or_stale_second_start_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_dt = datetime(2026, 11, 17, 8, 12, 0, tzinfo=timezone.utc)
    _set_clock(monkeypatch, "2026-11-17T08:12:00")
    expired_msg = (
        "Dear Sergey, our moderators have confirmed the report and your account is now limited "
        "until 17 Nov 2026, 08:11 UTC. While the account is limited, you can only message contacts."
    )

    # A) FakeGateway: 1st /start returns expired limit, 2nd /start returns empty string
    fgw = FakeGateway()
    sess = "data/sessions/timeout2nd.session"
    fgw.spambot_replies = {sess: [expired_msg, ""]}
    f_res = asyncio.run(fgw.probe_spambot(sess, now_utc=now_dt))
    assert fgw.spambot_starts == [sess, sess]
    assert f_res["ok"] is False
    assert f_res["limited"] is True
    assert f_res["expired_limit_cleared"] is False

    # B) TelethonGateway: 2nd /start gets NO fresh bot reply (only stale message id=202 <= sent2.id=203,
    #    and even if sent_msg.id is missing/0 on 2nd /start, stale message must NOT be re-read!)
    class _Msg:
        def __init__(self, mid: int, text: str, out: bool = False):
            self.id = mid
            self.message = text
            self.out = out

    class _StaleSecondReplyClient:
        def __init__(self, *, zero_second_sent_id: bool = False):
            self.starts_sent = 0
            self.zero_second_sent_id = zero_second_sent_id
            self._inbox: list[_Msg] = []

        def is_connected(self) -> bool:
            return True

        async def send_message(self, entity: str, text: str):
            assert entity == "SpamBot"
            assert text == "/start"
            self.starts_sent += 1
            if self.starts_sent == 1:
                out_m = _Msg(201, text, out=True)
                self._inbox = [_Msg(202, expired_msg, out=False), out_m]
                return out_m
            sent2_id = 0 if self.zero_second_sent_id else 203
            out_m2 = _Msg(sent2_id, text, out=True)
            # Bot has NOT replied to 2nd /start yet — inbox still only has old id=202!
            self._inbox = [out_m2, _Msg(202, expired_msg, out=False)]
            return out_m2

        async def get_messages(self, entity: str, limit: int = 4):
            return list(self._inbox[:limit])

    for zero_id in (False, True):
        tgw = TelethonGateway()
        tgw._spambot_poll_sleep = 0.0
        client = _StaleSecondReplyClient(zero_second_sent_id=zero_id)
        tgw._clients[sess] = client
        t_res = asyncio.run(tgw.probe_spambot(sess, now_utc=now_dt))
        assert client.starts_sent == 2
        assert t_res["ok"] is False
        assert t_res["limited"] is True
        assert t_res["expired_limit_cleared"] is False

    # C) End-to-end: run_campaign_batch must NOT unfreeze frozen account when 2nd /start fails closed
    conn = connect(tmp_path / "s2_2.db")
    init_db(conn)
    _seed_account(
        conn,
        27,
        health="frozen",
        last_error=f"@SpamBot: {expired_msg}",
        session_path=sess,
    )
    camp = create_campaign(
        conn,
        name="SoftUGC FailClosed Unfreeze",
        account_ids=[27],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 700202, 'founder_failclosed', 'Founder', 'AcmeCloud', 'SaaS', ?, 0, 'pending', '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """,
        (camp["id"], VALID_PITCH),
    )
    conn.commit()
    outreach_mod._SPAMBOT_PROBE_CACHE.clear()
    fgw2 = FakeGateway(
        dialogs=[{"telegram_user_id": 700202, "username": "founder_failclosed", "name": "Founder"}]
    )
    fgw2._pending[sess] = {"phone": "+15551000027", "state": "connected"}
    fgw2.spambot_replies = {sess: [expired_msg, ""]}

    batch_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=fgw2, max_send=1, now_dt=now_dt)
    )
    assert batch_res["sent"] == 0
    acc27 = conn.execute("SELECT health FROM accounts WHERE id = 27").fetchone()
    assert acc27["health"] == "frozen"


# ---------------------------------------------------------------------------
# 3. [P1] Dead unauthorized/banned sessions never opened in inbox/tools/folders/check_all
# ---------------------------------------------------------------------------
def test_s2_3_dead_unauthorized_and_banned_sessions_isolated_across_inbox_tools_folders_and_check_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inbox_mod, "INBOX_MEDIA_DIR", tmp_path / "media")
    conn = connect(tmp_path / "s2_3.db")
    init_db(conn)

    dead_unauth_sess = _seed_account(
        conn, 1, health="unauthorized", last_error="AuthKeyUnregistered", session_path="data/sessions/dead1.session"
    )
    dead_banned_sess = _seed_account(
        conn, 2, health="banned", last_error="UserDeactivatedBan", session_path="data/sessions/dead2.session"
    )
    live_sess = _seed_account(
        conn, 3, health="active", last_error=None, session_path="data/sessions/live3.session"
    )
    dead_sessions = {dead_unauth_sess, dead_banned_sess}

    cur_dlg = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, provider, is_group,
            unread_count, last_from_peer, muted, created_at, updated_at
        ) VALUES (1, 'user', 777001, 'cached_lead', 'Cached Lead', 'telegram', 0, 0, 1, 0, '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """
    )
    dead_dlg_id = int(cur_dlg.lastrowid)
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 501, 'in', 'Локальное сообщение из SQLite', '2026-11-17T08:01:00', '2026-11-17T08:01:00', '2026-11-17T08:01:00')
        """,
        (dead_dlg_id,),
    )
    cur_folder = conn.execute(
        """
        INSERT INTO folders (
            account_id, telegram_folder_id, title, sync_enabled, blocked_reason, created_at, updated_at
        ) VALUES (1, 10, 'Клиенты', 1, NULL, '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """
    )
    dead_folder_id = int(cur_folder.lastrowid)
    conn.commit()

    touched_sessions: list[tuple[str, str]] = []

    class _StrictDeadSessionSpyGateway(_StubGateway):
        def _guard(self, method: str, session_path: str) -> None:
            touched_sessions.append((method, session_path))
            if session_path in dead_sessions:
                raise AssertionError(f"Dead session opened by {method}: {session_path}")

        async def session_health(self, session_path: str) -> dict:
            self._guard("session_health", session_path)
            return {"health": "active", "last_error": None}

        async def download_profile_photo(self, session_path: str, peer_id, dest_path: str) -> bool:
            self._guard("download_profile_photo", session_path)
            return False

        async def fetch_messages(self, session_path: str, peer_id, limit: int = 50) -> list:
            self._guard("fetch_messages", session_path)
            return []

        async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
            self._guard("search_dialogs", session_path)
            return [{"telegram_user_id": 888111, "username": "live_resolved", "name": "Live Resolved"}]

        async def resolve_phones(self, session_path: str, phones: list[str]) -> list[dict]:
            self._guard("resolve_phones", session_path)
            return [
                {
                    "phone": phones[0],
                    "found": True,
                    "telegram_user_id": 888222,
                    "username": "phone_user",
                    "name": "Phone User",
                }
            ]

        async def get_participants(self, session_path: str, group_link: str, limit: int = 50) -> list[dict]:
            self._guard("get_participants", session_path)
            return [
                {
                    "telegram_user_id": 888333,
                    "username": "group_member",
                    "name": "Group Member",
                    "group_title": "Founders Hub",
                }
            ]

        async def list_folders(self, session_path: str) -> list:
            self._guard("list_folders", session_path)
            return []

        async def folder_peers(self, session_path: str, folder_id: int) -> list:
            self._guard("folder_peers", session_path)
            return []

    spy_gw = _StrictDeadSessionSpyGateway()

    # 3a. Avatar fallback without touching dead session
    touched_sessions.clear()
    avatar_bytes, mime, _cc = asyncio.run(
        get_dialog_avatar_with_cache(conn, dead_dlg_id, gateway=spy_gw)
    )
    assert touched_sessions == []
    assert mime == "image/svg+xml"
    assert b"<svg" in avatar_bytes

    # 3b. Thread returns SQLite messages without touching dead session
    touched_sessions.clear()
    thread_msgs = asyncio.run(get_thread(conn, dead_dlg_id, gateway=spy_gw))
    assert touched_sessions == []
    assert len(thread_msgs) == 1
    assert thread_msgs[0]["text"] == "Локальное сообщение из SQLite"

    # 3c. start_direct_dialog auto-selects live #3, and raises ValueError if dead #1/#2 explicitly requested
    touched_sessions.clear()
    direct_dlg = asyncio.run(
        start_direct_dialog(conn, "@live_resolved", account_id=None, gateway=spy_gw)
    )
    assert touched_sessions == [("search_dialogs", live_sess)]
    assert int(direct_dlg["dialog"]["account_id"]) == 3
    touched_sessions.clear()
    with pytest.raises(ValueError, match="деавторизован|заблокирован|сесси"):
        asyncio.run(start_direct_dialog(conn, "@live_resolved", account_id=1, gateway=spy_gw))
    with pytest.raises(ValueError, match="деавторизован|заблокирован|сесси"):
        asyncio.run(start_direct_dialog(conn, "@live_resolved", account_id=2, gateway=spy_gw))
    assert touched_sessions == []

    # 3d. check_all_accounts skips unauthorized/banned unless recheck_dead=True
    touched_sessions.clear()
    asyncio.run(check_all_accounts(conn, gateway=spy_gw))
    assert touched_sessions == [("session_health", live_sess)]

    recheck_probed: list[str] = []

    class _RecheckGateway(_StubGateway):
        async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
            return []

        async def session_health(self, session_path: str) -> dict:
            recheck_probed.append(session_path)
            if session_path == dead_unauth_sess:
                return {"health": "unauthorized", "last_error": "AuthKeyUnregistered"}
            if session_path == dead_banned_sess:
                return {"health": "banned", "last_error": "UserDeactivatedBan"}
            return {"health": "active", "last_error": None}

    asyncio.run(check_all_accounts(conn, gateway=_RecheckGateway(), recheck_dead=True))
    assert recheck_probed == [dead_unauth_sess, dead_banned_sess, live_sess]

    # 3e. Tools (_pick_account) skip dead accounts #1/#2 and use live account #3
    touched_sessions.clear()
    conv_res = asyncio.run(
        convert_phones_to_tg(conn, spy_gw, {"phones": ["+79991234567"], "add_to_leads": True})
    )
    assert conv_res["found_count"] == 1
    grp_res = asyncio.run(
        parse_telegram_group(conn, spy_gw, {"group_link": "t.me/founders_hub", "limit": 5})
    )
    assert grp_res["participants_count"] == 1
    assert all(sess == live_sess for _, sess in touched_sessions)

    # 3f. Folders (_connected) raise clear ValueError before touching gateway on dead accounts
    touched_sessions.clear()
    with pytest.raises(ValueError, match="Аккаунт деавторизован, переподключите сессию"):
        asyncio.run(pull_folders(conn, 1, spy_gw))
    with pytest.raises(ValueError, match="Аккаунт деавторизован, переподключите сессию"):
        asyncio.run(sync_folder(conn, dead_folder_id, spy_gw))
    assert touched_sessions == []


# ---------------------------------------------------------------------------
# 4. [P1] Read-only GET /api/campaigns & get_campaign/list_campaigns do NOT mutate DB
# ---------------------------------------------------------------------------
def test_s2_4_get_and_list_campaigns_are_strictly_read_only_and_healing_runs_only_on_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISABLE_TG_WATCHES", "1")
    _set_clock(monkeypatch, "2026-11-17T10:00:00")
    conn = connect(tmp_path / "s2_4.db")
    init_db(conn)
    sess = _seed_account(conn, 31, health="active")

    camp = create_campaign(
        conn,
        name="SoftUGC Completed ReadOnly Check",
        account_ids=[31],
        daily_limit=20,
        status="completed",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    cid = int(camp["id"])
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, error_text, created_at, updated_at
        ) VALUES (?, 700404, 'transient_target', 'Target', 'AcmeCloud', 'SaaS', ?, 0, 'failed', 'TimedOut: Нет связи', '2026-11-17T09:00:00', '2026-11-17T09:00:00')
        """,
        (cid, VALID_PITCH),
    )
    conn.execute("UPDATE campaigns SET status = 'completed' WHERE id = ?", (cid,))
    conn.commit()

    gw = FakeGateway(
        dialogs=[{"telegram_user_id": 700404, "username": "transient_target", "name": "Target"}]
    )
    gw._pending[sess] = {"phone": "+15551000031", "state": "connected"}
    app = create_app(conn=conn, gateway=gw)

    with TestClient(app) as client:
        changes_before = conn.total_changes
        conn.execute("PRAGMA query_only = ON")
        try:
            c_direct = get_campaign(conn, cid)
            assert c_direct["status"] == "completed"
            assert c_direct["failed_count"] == 1
            assert c_direct["pending_count"] == 0

            listed_direct = list_campaigns(conn)
            assert listed_direct[0]["status"] == "completed"

            r_list = client.get("/api/campaigns")
            assert r_list.status_code == 200
            assert r_list.json()[0]["status"] == "completed"

            r_one = client.get(f"/api/campaigns/{cid}")
            assert r_one.status_code == 200
            assert r_one.json()["status"] == "completed"
            assert r_one.json()["failed_count"] == 1

            assert conn.total_changes == changes_before
        finally:
            conn.execute("PRAGMA query_only = OFF")

    db_camp_status = conn.execute("SELECT status FROM campaigns WHERE id = ?", (cid,)).fetchone()["status"]
    db_rec_status = conn.execute(
        "SELECT status FROM campaign_recipients WHERE campaign_id = ?", (cid,)
    ).fetchone()["status"]
    assert db_camp_status == "completed"
    assert db_rec_status == "failed"

    # Now on real batch execution, heal_transient_failed_recipients MUST heal and send!
    run_res = asyncio.run(run_campaign_batch(conn, cid, gateway=gw, max_send=1))
    assert run_res["sent"] == 1
    db_rec_after = conn.execute(
        "SELECT status, current_step, error_text FROM campaign_recipients WHERE campaign_id = ?",
        (cid,),
    ).fetchone()
    assert db_rec_after["status"] == "completed"
    assert db_rec_after["current_step"] == 1
    assert db_rec_after["error_text"] is None
