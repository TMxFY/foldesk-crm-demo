from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import urllib.request

import pytest

_WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_ROOT))

import app.accounts as accounts_mod
import app.digest as digest_mod
import app.outreach as outreach_mod
import app.telegram_gateway as gw_mod
from app.db import connect, init_db
from app.outreach import (
    create_campaign,
    get_account_cooldown_until,
    run_campaign_batch,
    update_outreach_schedule,
)
from app.telegram_gateway import FakeGateway, TelegramGateway, TelethonGateway, _parse_spambot_reply


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


def _seed_tg_account(
    conn,
    account_id: int,
    *,
    username: str | None = None,
    health: str = "active",
    last_error: str | None = None,
    daily_limit: int = 20,
    sent_today: int = 0,
    sent_date: str = "2026-11-17",
    session_path: str | None = None,
) -> None:
    now = "2026-11-17T08:00:00"
    conn.execute(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, last_error,
            telegram_user_id, telegram_username, telegram_name,
            daily_limit, sent_today, sent_date, provider, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?, ?, ?, 'telegram', ?, ?)
        """,
        (
            account_id,
            f"+1555000{account_id:04d}",
            session_path or f"data/sessions/acc{account_id}.session",
            health,
            last_error,
            900000 + account_id,
            username or f"acc_{account_id}",
            f"Account {account_id}",
            daily_limit,
            sent_today,
            sent_date,
            now,
            now,
        ),
    )
    conn.commit()


class _BaseStubGateway(TelegramGateway):
    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}


# ---------------------------------------------------------------------------
# 1a. Smart @SpamBot date parsing (EN & RU, expired vs future)
# ---------------------------------------------------------------------------
def test_parse_spambot_details_expired_and_future_en_ru_formats():
    parse_spambot_details = getattr(gw_mod, "parse_spambot_details")

    prod_reply_en = (
        "Dear Sergey, your phone number wasasha-reported by some Telegram users. "
        "Unfortunately, our moderators have confirmed the report and your account is now limited "
        "until 17 Nov 2026, 08:11 UTC. While the account is limited, you will not be able "
        "to send messages to people who do not have your number in their phone contacts."
    )
    # Exact prod bug timestamp: 2026-11-17T08:11:34 UTC -> 08:11 UTC has ALREADY PASSED!
    now_after = datetime(2026, 11, 17, 8, 11, 34, tzinfo=timezone.utc)
    d_expired = parse_spambot_details(prod_reply_en, now_utc=now_after)
    assert d_expired["limited_until_utc"] == "2026-11-17T08:11:00Z"
    assert d_expired["is_expired_limit"] is True
    assert d_expired["limited"] is False
    assert _parse_spambot_reply(prod_reply_en, now_utc=now_after) is False

    # 6 minutes earlier (08:05 UTC): still in the future -> limited=True!
    now_before = datetime(2026, 11, 17, 8, 5, 0, tzinfo=timezone.utc)
    d_future = parse_spambot_details(prod_reply_en, now_utc=now_before)
    assert d_future["limited_until_utc"] == "2026-11-17T08:11:00Z"
    assert d_future["is_expired_limit"] is False
    assert d_future["limited"] is True

    # RU mixed & Cyrillic month formats
    ru_en_month = "К сожалению, ваш аккаунт временно ограничен до 17 Nov 2026, 08:11 UTC."
    d_ru_exp = parse_spambot_details(ru_en_month, now_utc=now_after)
    assert d_ru_exp["is_expired_limit"] is True
    assert d_ru_exp["limited"] is False
    assert d_ru_exp["limited_until_utc"] == "2026-11-17T08:11:00Z"

    ru_cyr_month = "Ограничения будут сняты автоматически — до 17 нояб. 2026, 08:11 UTC."
    d_ru_fut = parse_spambot_details(ru_cyr_month, now_utc=now_before)
    assert d_ru_fut["is_expired_limit"] is False
    assert d_ru_fut["limited"] is True
    assert d_ru_fut["limited_until_utc"] == "2026-11-17T08:11:00Z"


# ---------------------------------------------------------------------------
# 1b. Double /start on expired @SpamBot limit & auto-unfreeze in TelethonGateway + FakeGateway + outreach
# ---------------------------------------------------------------------------
def test_probe_spambot_sends_second_start_on_expired_limit_and_unfreezes_account(tmp_path, monkeypatch):
    now_dt = datetime(2026, 11, 17, 8, 11, 34, tzinfo=timezone.utc)
    _set_clock(monkeypatch, "2026-11-17T08:11:34")

    expired_msg = (
        "Dear Sergey, our moderators have confirmed the report and your account is now limited "
        "until 17 Nov 2026, 08:11 UTC. While the account is limited, you can only message contacts."
    )
    clean_msg = "Good news, no limits are currently applied to your account. You’re free as a bird!"

    # A) FakeGateway double /start when 1st reply is expired
    fgw = FakeGateway()
    sess = "data/sessions/make1tlo.session"
    fgw.spambot_replies = {sess: [expired_msg, clean_msg]}
    f_res = asyncio.run(fgw.probe_spambot(sess, now_utc=now_dt))
    assert fgw.spambot_starts == [sess, sess]
    assert f_res["ok"] is True
    assert f_res["limited"] is False
    assert f_res["expired_limit_cleared"] is True
    assert f_res["limited_until_utc"] == "2026-11-17T08:11:00Z"

    # B) TelethonGateway double /start (even if 2nd reply still echoes the expired 08:11 UTC banner!)
    class _FakeMsg:
        def __init__(self, mid: int, text: str, out: bool = False):
            self.id = mid
            self.message = text
            self.out = out

    class _FakeTelethonClient:
        def __init__(self, replies: list[str]):
            self.replies = list(replies)
            self.starts_sent = 0
            self._next_id = 100
            self._inbox: list[_FakeMsg] = []

        def is_connected(self) -> bool:
            return True

        async def send_message(self, entity, text: str):
            assert entity == "SpamBot"
            assert text == "/start"
            self.starts_sent += 1
            self._next_id += 1
            out_m = _FakeMsg(self._next_id, text, out=True)
            reply_txt = self.replies[min(self.starts_sent - 1, len(self.replies) - 1)]
            self._next_id += 1
            self._inbox.insert(0, _FakeMsg(self._next_id, reply_txt, out=False))
            return out_m

        async def get_messages(self, entity, limit: int = 4):
            return list(self._inbox[:limit])

    tgw = TelethonGateway()
    tgw._spambot_poll_sleep = 0.0
    fake_client = _FakeTelethonClient([expired_msg, expired_msg])
    tgw._clients[sess] = fake_client
    t_res = asyncio.run(tgw.probe_spambot(sess, now_utc=now_dt))
    assert fake_client.starts_sent == 2
    assert t_res["ok"] is True
    assert t_res["limited"] is False
    assert t_res["expired_limit_cleared"] is True

    # C) End-to-end unfreeze in run_campaign_batch on frozen @make1tlo
    conn = connect(tmp_path / "unfreeze_make1tlo.db")
    init_db(conn)
    _seed_tg_account(
        conn,
        27,
        username="make1tlo",
        health="frozen",
        last_error=f"@SpamBot: {expired_msg}",
        session_path=sess,
    )
    camp = create_campaign(
        conn,
        name="SoftUGC Unfreeze E2E",
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
        ) VALUES (?, 501001, 'saas_founder', 'Alex', 'AcmeCloud', 'SaaS', ?, 0, 'pending', '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """,
        (camp["id"], VALID_PITCH),
    )
    conn.commit()

    outreach_mod._SPAMBOT_PROBE_CACHE.clear()
    e2e_gw = FakeGateway(dialogs=[{"telegram_user_id": 501001, "username": "saas_founder", "name": "Alex"}])
    e2e_gw._pending[sess] = {"phone": "+15550000027", "state": "connected"}
    e2e_gw.spambot_replies = {sess: [expired_msg, clean_msg]}

    batch_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=e2e_gw, max_send=5, now_dt=now_dt))
    assert batch_res["sent"] == 1
    assert len(e2e_gw.spambot_starts) == 2
    acc_row = conn.execute("SELECT health, last_error FROM accounts WHERE id = 27").fetchone()
    assert acc_row["health"] == "active"
    assert acc_row["last_error"] is None


# ---------------------------------------------------------------------------
# 1c. Future limited_until_utc sets exact cooldown_until and auto-unfreezes on expiry
# ---------------------------------------------------------------------------
def test_future_spambot_limit_sets_exact_cooldown_until_and_auto_unfreezes_at_expiry(tmp_path, monkeypatch):
    conn = connect(tmp_path / "exact_spambot_cd.db")
    init_db(conn)
    sess27 = "data/sessions/acc27.session"
    sess30 = "data/sessions/acc30.session"
    _seed_tg_account(conn, 27, username="make1tlo", health="active", session_path=sess27)
    _seed_tg_account(conn, 30, username="backup_acc", health="active", session_path=sess30)

    camp = create_campaign(
        conn,
        name="SoftUGC Future Timer Unfreeze",
        account_ids=[27, 30],
        daily_limit=20,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    for idx in range(1, 3):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, assigned_account_id, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 27, 0, 'pending', '2026-11-17T08:00:00', '2026-11-17T08:00:00')
            """,
            (camp["id"], 610000 + idx, f"founder_{idx}", f"Founder {idx}", f"App{idx}", VALID_PITCH),
        )
    conn.commit()

    future_reply = (
        "Dear Sergey, your account is now limited until 17 Nov 2026, 08:11 UTC. "
        "While the account is limited, you can only send messages to mutual contacts."
    )
    clean_reply = "Good news, no limits are currently applied to your account. You’re free as a bird!"
    state = {"reply": future_reply, "fail_send_27": True, "probes": 0}

    class _ExactTimerGateway(_BaseStubGateway):
        async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
            uname = query.lstrip("@")
            uid = 610001 if uname == "founder_1" else 610002
            return [{"telegram_user_id": uid, "username": uname, "name": uname}]

        async def probe_spambot(self, session_path: str, *, now_utc: datetime | None = None) -> dict:
            state["probes"] += 1
            fg = FakeGateway()
            fg.spambot_replies = {session_path: state["reply"]}
            if now_utc is not None:
                try:
                    return await fg.probe_spambot(session_path, now_utc=now_utc)
                except TypeError:
                    return await fg.probe_spambot(session_path)
            return await fg.probe_spambot(session_path)

        async def send_message(self, session_path: str, peer_id, text: str, reply_to=None, image_path=None) -> dict:
            if session_path == sess27 and state["fail_send_27"]:
                raise RuntimeError("PeerFloodError: You cannot message non-contacts, check @SpamBot")
            return {"telegram_message_id": 9001, "sent_at": outreach_mod.utc_now()}

    outreach_mod._SPAMBOT_PROBE_CACHE.clear()
    outreach_mod._ACCOUNT_COOLDOWN_UNTIL.clear()
    gw = _ExactTimerGateway()

    # Step 1 at 08:05:00 UTC: acc27 hits @SpamBot limited until 08:11 UTC -> rotated to acc30 for founder_1
    t_0805 = datetime(2026, 11, 17, 8, 5, 0, tzinfo=timezone.utc)
    _set_clock(monkeypatch, "2026-11-17T08:05:00")
    r1 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1, now_dt=t_0805))
    assert r1["sent"] == 1
    expected_until_ts = datetime(2026, 11, 17, 8, 11, 0, tzinfo=timezone.utc).timestamp()
    cd_ts = get_account_cooldown_until(conn, 27, now_dt=t_0805)
    assert cd_ts == pytest.approx(expected_until_ts)
    assert conn.execute("SELECT health FROM accounts WHERE id = 27").fetchone()["health"] == "frozen"

    # Step 2 at 08:11:05 UTC (just 6m05s later, well under old 3600s cache!):
    # Cooldown has expired -> acc27 MUST automatically re-probe @SpamBot, unfreeze to active, and send founder_2!
    t_0811 = datetime(2026, 11, 17, 8, 11, 5, tzinfo=timezone.utc)
    _set_clock(monkeypatch, "2026-11-17T08:11:05")
    state["reply"] = [future_reply, clean_reply]
    state["fail_send_27"] = False
    assert get_account_cooldown_until(conn, 27, now_dt=t_0811) is None

    r2 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1, now_dt=t_0811))
    assert r2["sent"] == 1
    assert r2["sent_by_account"] == {"27": 1}
    acc27_after = conn.execute("SELECT health, last_error FROM accounts WHERE id = 27").fetchone()
    assert acc27_after["health"] == "active"
    assert acc27_after["last_error"] is None


# ---------------------------------------------------------------------------
# 2. Closed-Loop Source Reputation Quarantine
# ---------------------------------------------------------------------------
def test_source_reputation_quarantines_toxic_spambot_and_rejected_sources_and_blocks_pending(tmp_path, monkeypatch):
    record_source_send_outcome = getattr(outreach_mod, "record_source_send_outcome")
    get_quarantined_sources = getattr(outreach_mod, "get_quarantined_sources")

    conn = connect(tmp_path / "source_rep.db")
    init_db(conn)
    _set_clock(monkeypatch, "2026-11-17T10:00:00")
    _seed_tg_account(conn, 21, username="sender_one", daily_limit=50)

    # Source A: 13 sent + 2 spambot_hit (15 attempts, 0 replies, spambot_hits >= 2) -> quarantined!
    rec_spam_src = {"extra_json": json.dumps({"source_chat": "@toxic_spam_dump"})}
    for _ in range(13):
        record_source_send_outcome(conn, rec_spam_src, outcome="sent")
    record_source_send_outcome(conn, rec_spam_src, outcome="spambot_hit")
    assert "@toxic_spam_dump" not in get_quarantined_sources(conn)  # 14 attempts -> not yet
    record_source_send_outcome(conn, rec_spam_src, outcome="spambot_hit")
    assert "@toxic_spam_dump" in get_quarantined_sources(conn)  # 15th attempt -> quarantined!

    # Source B: 6 sent + 9 rejected_or_blocked (15 attempts, 0 replies, 9/15 = 0.60) -> quarantined!
    rec_rej_src = {"extra_json": json.dumps({"group": "dead_privacy_group"})}
    for _ in range(6):
        record_source_send_outcome(conn, rec_rej_src, outcome="sent")
    for _ in range(9):
        record_source_send_outcome(conn, rec_rej_src, outcome="rejected_or_blocked")
    assert "dead_privacy_group" in get_quarantined_sources(conn)

    # Source C: 13 sent + 2 spambot_hit + 1 replied -> NOT quarantined because replies > 0!
    rec_good_src = {"extra_json": json.dumps({"source": "yc_founders_clean"})}
    for _ in range(13):
        record_source_send_outcome(conn, rec_good_src, outcome="sent")
    record_source_send_outcome(conn, rec_good_src, outcome="replied")
    for _ in range(2):
        record_source_send_outcome(conn, rec_good_src, outcome="spambot_hit")
    assert "yc_founders_clean" not in get_quarantined_sources(conn)

    raw_stats = json.loads(
        conn.execute("SELECT value FROM kv_settings WHERE key = 'source_reputation_stats'").fetchone()[0]
    )
    assert raw_stats["@toxic_spam_dump"]["spambot_hits"] == 2
    assert raw_stats["dead_privacy_group"]["rejected_or_blocked"] == 9
    assert raw_stats["yc_founders_clean"]["replied"] == 1

    # Verify run_campaign_batch blocks pending recipients from quarantined sources AND auto-quarantines live!
    camp = create_campaign(
        conn,
        name="SoftUGC Source Quarantine Guard",
        account_ids=[21],
        daily_limit=50,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    # 1 recipient from already quarantined @toxic_spam_dump, 1 from yc_founders_clean,
    # and 16 recipients from @live_bad_source where first 15 fail with UserIsBlocked -> 16th auto-quarantined!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, extra_json, current_step, status, created_at, updated_at
        ) VALUES (?, 700001, 'quarantined_lead', 'Q Lead', 'QCorp', 'SaaS', ?, ?, 0, 'pending', '2026-11-17T09:00:00', '2026-11-17T09:00:00')
        """,
        (camp["id"], VALID_PITCH, json.dumps({"source_chat": "@toxic_spam_dump"})),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, extra_json, current_step, status, created_at, updated_at
        ) VALUES (?, 700002, 'clean_yc_lead', 'YC Lead', 'YCCorp', 'SaaS', ?, ?, 0, 'pending', '2026-11-17T09:00:00', '2026-11-17T09:00:00')
        """,
        (camp["id"], VALID_PITCH, json.dumps({"source": "yc_founders_clean"})),
    )
    for idx in range(1, 17):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, extra_json, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, ?, 0, 'pending', '2026-11-17T09:00:00', '2026-11-17T09:00:00')
            """,
            (
                camp["id"],
                710000 + idx,
                f"bad_src_u{idx}",
                f"Bad {idx}",
                f"BadCo{idx}",
                VALID_PITCH,
                json.dumps({"source_chat": "@live_bad_source"}),
            ),
        )
    conn.commit()

    class _SelectiveBlockGateway(_BaseStubGateway):
        def __init__(self):
            self.attempted_usernames: list[str] = []

        async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
            uname = query.lstrip("@")
            return [{"telegram_user_id": 799999, "username": uname, "name": uname}]

        async def send_message(self, session_path: str, peer_id, text: str, reply_to=None, image_path=None) -> dict:
            if int(peer_id) >= 710001:
                self.attempted_usernames.append(str(peer_id))
                raise RuntimeError("UserPrivacyRestrictedError: Cannot message this user")
            self.attempted_usernames.append(str(peer_id))
            return {"telegram_message_id": 8801, "sent_at": "2026-11-17T10:00:00"}

    gw = _SelectiveBlockGateway()
    res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=30))
    assert res["sent"] == 1

    q_row = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'quarantined_lead'"
    ).fetchone()
    assert q_row["status"] == "completed"
    assert q_row["error_text"] == "Источник поставлен на авто-карантин из-за жалоб/низкого качества"

    # @live_bad_source hit 15 blocked attempts -> auto-quarantined -> 16th recipient skipped without send attempt!
    assert "@live_bad_source" in get_quarantined_sources(conn)
    r16 = conn.execute(
        "SELECT status, error_text FROM campaign_recipients WHERE username = 'bad_src_u16'"
    ).fetchone()
    assert r16["status"] == "completed"
    assert r16["error_text"] == "Источник поставлен на авто-карантин из-за жалоб/низкого качества"


# ---------------------------------------------------------------------------
# 3. Least-Loaded Balancer + Jitter on Step 1
# ---------------------------------------------------------------------------
def test_least_loaded_balancer_picks_fresh_account_by_daily_ratio_instead_of_fixed_order(tmp_path, monkeypatch):
    conn = connect(tmp_path / "least_loaded.db")
    init_db(conn)
    _set_clock(monkeypatch, "2026-11-17T11:00:00")

    # Fixed list order puts burned-down accounts FIRST and fresh @tim_korznikov LAST:
    # acc 21: 14/20 (70%), acc 30: 6/10 (60%), acc 35 (@tim_korznikov): 0/20 (0%)
    _seed_tg_account(conn, 21, username="acc_heavy", daily_limit=20, sent_today=14, sent_date="2026-11-17")
    _seed_tg_account(conn, 30, username="acc_mid", daily_limit=10, sent_today=6, sent_date="2026-11-17")
    _seed_tg_account(conn, 35, username="tim_korznikov", daily_limit=20, sent_today=0, sent_date="2026-11-17")

    camp = create_campaign(
        conn,
        name="SoftUGC Least Loaded Pool",
        account_ids=[21, 30, 35],
        daily_limit=50,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    for idx in range(1, 15):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, company, category,
                step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', '2026-11-17T09:00:00', '2026-11-17T09:00:00')
            """,
            (camp["id"], 810000 + idx, f"saas_target_{idx}", f"Target {idx}", f"Cloud{idx}", VALID_PITCH),
        )
    conn.commit()

    class _SimpleSendGw(_BaseStubGateway):
        async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
            uname = query.lstrip("@")
            idx = int(uname.rsplit("_", 1)[-1])
            return [{"telegram_user_id": 810000 + idx, "username": uname, "name": uname}]

        async def send_message(self, session_path: str, peer_id, text: str, reply_to=None, image_path=None) -> dict:
            return {"telegram_message_id": 9100 + int(peer_id) % 1000, "sent_at": "2026-11-17T11:00:00"}

    gw = _SimpleSendGw()
    # First 6 sends MUST all go to fresh @tim_korznikov (id=35: 0/20 -> 6/20 = 30% < 60% < 70%)!
    res1 = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=6, jitter_range=(0.001, 0.002))
    )
    assert res1["sent"] == 6
    assert res1["sent_by_account"] == {"35": 6}
    assert len(res1["jitter_delays_ms"]) == 6

    # Next 8 sends: acc 35 sends 6 more (6/20 -> 12/20 = 60%), then at 60% tie (6/10 vs 12/20):
    # (0.6, 6, 30) < (0.6, 12, 35) -> acc 30 takes 1 (7/10 = 70%), then acc 35 (12/20 = 60%) takes 1 (13/20 = 65%)!
    res2 = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=8, jitter_range=(0.0, 0.0))
    )
    assert res2["sent"] == 8
    assert res2["sent_by_account"] == {"35": 7, "30": 1}


# ---------------------------------------------------------------------------
# 4. SLO Silence & Account-Drop Watchdog (idempotent once/day via app.digest)
# ---------------------------------------------------------------------------
def test_outreach_slo_watchdog_alerts_idempotently_on_silence_and_account_drop(tmp_path, monkeypatch):
    check_outreach_slo_watchdog = getattr(outreach_mod, "check_outreach_slo_watchdog")

    conn = connect(tmp_path / "slo_watchdog.db")
    init_db(conn)
    monkeypatch.setenv("BOT_TOKEN", "slo-watchdog-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "999000111")

    update_outreach_schedule(
        conn,
        timezone_name="UTC",
        days={
            "tue": {"enabled": True, "start": "08:00", "end": "18:00"},
            "wed": {"enabled": True, "start": "08:00", "end": "18:00"},
        },
        autopilot_enabled=True,
    )
    for aid in (21, 27, 30, 31, 35):
        _seed_tg_account(conn, aid, health="active", daily_limit=20, sent_today=0, sent_date="2026-11-17")

    camp = create_campaign(
        conn,
        name="SoftUGC Active Pipeline",
        account_ids=[21, 27, 30, 31, 35],
        daily_limit=50,
        status="running",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 920001, 'waiting_lead', 'Lead', 'LeadCo', 'SaaS', ?, 0, 'pending', '2026-11-17T08:00:00', '2026-11-17T08:00:00')
        """,
        (camp["id"], VALID_PITCH),
    )
    conn.commit()

    sent_payloads: list[dict] = []

    class _DummyHTTPResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 1}}'

    def _capture_opener(req: urllib.request.Request, timeout: float = 15):
        sent_payloads.append(json.loads(req.data.decode("utf-8")))
        return _DummyHTTPResp()

    # 1) 45 min after window start (08:45 UTC < 90 min) & 5/5 healthy accounts -> OK, 0 alerts!
    t_early = datetime(2026, 11, 17, 8, 45, 0, tzinfo=timezone.utc)
    w_early = check_outreach_slo_watchdog(conn, now_dt=t_early, opener=_capture_opener)
    assert w_early["ok"] is True
    assert w_early["alert_triggered"] is False
    assert sent_payloads == []

    # 2) 95 min after window start (09:35 UTC >= 90 min), running campaign has pending > 0, sent_today_total == 0 -> ALERT!
    t_silent = datetime(2026, 11, 17, 9, 35, 0, tzinfo=timezone.utc)
    w_silent = check_outreach_slo_watchdog(conn, now_dt=t_silent, opener=_capture_opener)
    assert w_silent["ok"] is False
    assert w_silent["alert_triggered"] is True
    assert len(w_silent["reasons"]) >= 1
    assert len(sent_payloads) == 1

    # 3) Idempotency: repeat call later same day (10:15 UTC) MUST NOT spam operator bot again!
    t_same_day = datetime(2026, 11, 17, 10, 15, 0, tzinfo=timezone.utc)
    w_dup = check_outreach_slo_watchdog(conn, now_dt=t_same_day, opener=_capture_opener)
    assert w_dup["ok"] is False
    assert w_dup["alert_triggered"] is False
    assert len(sent_payloads) == 1

    # 4) Next day (Wed 2026-11-18 08:20 UTC, < 90 min and sent_today=4 so NO silence),
    #    but 2 of 5 connected accounts drop to frozen/unauthorized (3 active < 5) -> Account-Drop ALERT!
    conn.execute("UPDATE accounts SET sent_today = 4, sent_date = '2026-11-18' WHERE id = 21")
    conn.execute("UPDATE accounts SET health = 'frozen' WHERE id = 27")
    conn.execute("UPDATE accounts SET health = 'unauthorized' WHERE id = 30")
    conn.commit()

    t_next_day = datetime(2026, 11, 18, 8, 20, 0, tzinfo=timezone.utc)
    w_drop = check_outreach_slo_watchdog(conn, now_dt=t_next_day, opener=_capture_opener)
    assert w_drop["ok"] is False
    assert w_drop["alert_triggered"] is True
    assert len(sent_payloads) == 2


# ---------------------------------------------------------------------------
# 5. Zero PYTEST_CURRENT_TEST in gateway, outreach, accounts
# ---------------------------------------------------------------------------
def test_zero_pytest_current_test_sniffing_in_gateway_outreach_and_accounts(tmp_path, monkeypatch):
    for rel_path in ("app/telegram_gateway.py", "app/outreach.py", "app/accounts.py"):
        src = (_WORKTREE_ROOT / rel_path).read_text(encoding="utf-8")
        assert "PYTEST_CURRENT_TEST" not in src, f"Forbidden PYTEST_CURRENT_TEST found in {rel_path}"

    # Verify explicit config flags work cleanly without PYTEST_CURRENT_TEST
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CRMTG_SKIP_GLOBAL_DB_FALLBACK", "1")
    monkeypatch.delenv("NODEMAVEN_USER", raising=False)
    monkeypatch.delenv("NODEMAVEN_PASS", raising=False)
    monkeypatch.delenv("NODEMAVEN_PASSWORD", raising=False)
    monkeypatch.delenv("NODEMAVEN_API_KEY", raising=False)
    assert gw_mod.resolve_nodemaven_config(None) is None

    # Verify accounts.record_account_health_transition alerts strictly when bot credentials exist
    conn = connect(tmp_path / "no_pytest_env.db")
    init_db(conn)
    _seed_tg_account(conn, 41, username="clean_acc", health="active")
    monkeypatch.setenv("BOT_TOKEN", "explicit-bot-token")
    monkeypatch.setenv("BOT_CHAT_ID", "123456")

    captured: list[dict] = []

    class _OkResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 7}}'

    def _opener(req, timeout=15):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _OkResp()

    accounts_mod.record_account_health_transition(conn, 41, "frozen", "@SpamBot: test", opener=_opener)
    assert len(captured) == 1
    assert "@clean_acc" in captured[0]["text"]
