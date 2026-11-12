import asyncio
from pathlib import Path
import sys

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

import app.accounts as accounts_mod
import app.outreach as outreach_mod
from app.db import connect, init_db
from app.outreach import create_campaign, get_campaign, run_campaign_batch
from app.telegram_gateway import TelegramGateway


class RecordingGateway(TelegramGateway):
    def __init__(self, *, live_messages_by_session_peer: dict[tuple[str, int], list[dict]] | None = None):
        self.sent: list[dict] = []
        self.live_messages_by_session_peer = dict(live_messages_by_session_peer or {})

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        clean = (query or "").strip().lstrip("@").lower()
        fake_id = 100000 + (sum(ord(ch) for ch in clean) % 700000)
        return [{"telegram_user_id": fake_id, "username": clean, "name": clean.title()}]

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        return list(self.live_messages_by_session_peer.get((session_path, int(peer_id)), []))

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to: int | None = None,
        image_path=None,
    ) -> dict:
        entry = {"session_path": session_path, "peer_id": peer_id, "text": text, "image_path": image_path}
        self.sent.append(entry)
        return {"telegram_message_id": 5000 + len(self.sent), "sent_at": outreach_mod.utc_now()}


def _seed_prior_outgoing_msg(
    conn,
    *,
    account_id: int,
    peer_id: int,
    username: str,
    text: str = "Hey,\n\nInitial SoftUGC touch.",
    sent_at: str = "2026-11-11T10:00:00",
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


def _set_clock(monkeypatch, iso_ts: str) -> None:
    day = iso_ts[:10]
    monkeypatch.setattr(outreach_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(outreach_mod, "utc_today", lambda: day)
    monkeypatch.setattr(accounts_mod, "utc_now", lambda: iso_ts)
    monkeypatch.setattr(accounts_mod, "utc_today", lambda: day)


def _seed_accounts(conn, specs: list[tuple[int, str, str, int]]) -> None:
    now = "2026-11-11T08:00:00"
    conn.executemany(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, telegram_name,
            daily_limit, sent_today, sent_date, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', 'active', ?, ?, 0, '2026-11-11', ?, ?)
        """,
        [
            (aid, phone, sess, f"Acc {aid}", lim, now, now)
            for (aid, phone, sess, lim) in specs
        ],
    )
    conn.commit()


def _three_steps(delay_2: int = 24, delay_3: int = 24) -> list[dict]:
    return [
        {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
        {"step_order": 2, "delay_hours": delay_2, "template_text": "Followup 1 for {name}"},
        {"step_order": 3, "delay_hours": delay_3, "template_text": "Followup 2 for {name}"},
    ]


def test_reserves_daily_quota_for_followups_due_later_today(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res1.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 5)])

    _set_clock(monkeypatch, "2026-11-12T08:05:00")
    camp = create_campaign(
        conn,
        name="[QA] Reserve Quota Single Acc",
        account_ids=[21],
        daily_limit=5,
        status="running",
        steps=_three_steps(24, 24),
    )

    live_msgs = {}
    for idx, yest_ts in enumerate(
        ("2026-11-11T11:00:00", "2026-11-11T12:00:00", "2026-11-11T13:00:00"), start=1
    ):
        peer_id = 70000 + idx
        uname = f"warm_lead_{idx}"
        _seed_prior_outgoing_msg(
            conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_ts
        )
        live_msgs[("data/sessions/acc21.session", peer_id)] = [
            {"telegram_message_id": 100 + idx, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
        ]
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi warm', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Warm {idx}", yest_ts, yest_ts, yest_ts),
        )

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 80000 + idx, f"new_lead_{idx}", f"New {idx}", f"Hello cold {idx}"),
        )
    conn.commit()

    gw = RecordingGateway(live_messages_by_session_peer=live_msgs)

    # At 08:05 UTC, 3 follow-ups are due later today (11:00..13:00 UTC).
    # Account daily_limit=5 -> only 2 slots may be used for new cold leads!
    morning_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert morning_res["sent"] == 2
    pending_left = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'pending'",
        (camp["id"],),
    ).fetchone()[0]
    assert pending_left == 3

    # At 14:00 UTC, the 3 follow-ups are now due and consume the 3 reserved slots (total 5/5 today).
    _set_clock(monkeypatch, "2026-11-12T14:00:00")
    afternoon_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert afternoon_res["sent"] == 3
    step2_count = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND current_step = 2",
        (camp["id"],),
    ).fetchone()[0]
    assert step2_count == 3
    assert get_campaign(conn, camp["id"])["sent_today"] == 5


def test_due_followups_take_priority_over_earlier_id_pending_leads(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res2.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 5)])
    _set_clock(monkeypatch, "2026-11-12T12:00:00")

    camp = create_campaign(
        conn,
        name="[QA] Priority Order",
        account_ids=[21],
        daily_limit=5,
        status="running",
        steps=_three_steps(24, 24),
    )

    # Insert pending lead FIRST (lower id)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 81001, 'early_id_cold', 'Cold Early', 'Cold hello', 0, 'pending', '2026-11-10T08:00:00', '2026-11-10T08:00:00')
        """,
        (camp["id"],),
    )
    # Insert already-due follow-up SECOND (higher id)
    yest_ts = "2026-11-11T09:00:00"
    _seed_prior_outgoing_msg(conn, account_id=21, peer_id=71001, username="later_id_warm", sent_at=yest_ts)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 71001, 'later_id_warm', 'Warm Later', 'Hi warm', 21, 21, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], yest_ts, yest_ts, yest_ts),
    )
    conn.commit()

    gw = RecordingGateway(
        live_messages_by_session_peer={
            ("data/sessions/acc21.session", 71001): [
                {"telegram_message_id": 101, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
            ]
        }
    )
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1, respect_delays=True)
    )
    assert res["sent"] == 1
    assert gw.sent[0]["peer_id"] == 71001


def test_multi_account_per_account_reservation_does_not_starve_other_account(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res3.db")
    init_db(conn)
    _seed_accounts(
        conn,
        [
            (21, "+10000000021", "data/sessions/acc21.session", 3),
            (31, "+10000000031", "data/sessions/acc31.session", 3),
        ],
    )
    _set_clock(monkeypatch, "2026-11-12T08:10:00")

    camp = create_campaign(
        conn,
        name="[QA] Multi Acc Reserve",
        account_ids=[21, 31],
        daily_limit=6,
        status="running",
        steps=_three_steps(24, 24),
    )

    live_msgs = {}
    # Acc 21 has 3 followups due later today at 12:00 UTC (100% of its 3/day quota).
    for idx in range(1, 4):
        peer_id = 72000 + idx
        uname = f"acc21_warm_{idx}"
        yest_ts = "2026-11-11T12:00:00"
        _seed_prior_outgoing_msg(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_ts)
        live_msgs[("data/sessions/acc21.session", peer_id)] = [
            {"telegram_message_id": 200 + idx, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
        ]
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Warm {idx}", yest_ts, yest_ts, yest_ts),
        )

    # 5 pending cold leads
    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 82000 + idx, f"cold_multi_{idx}", f"Cold {idx}", f"Cold msg {idx}"),
        )
    conn.commit()

    gw = RecordingGateway(live_messages_by_session_peer=live_msgs)

    # Morning: Acc 21 must send 0 new leads (reserves all 3 slots), Acc 31 sends 3 new leads!
    morning_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert morning_res["sent"] == 3
    assert morning_res["sent_by_account"] == {"31": 3}

    # Afternoon: Acc 21 sends its 3 due follow-ups!
    _set_clock(monkeypatch, "2026-11-12T13:00:00")
    afternoon_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert afternoon_res["sent"] == 3
    assert afternoon_res["sent_by_account"] == {"21": 3}


def test_followups_due_tomorrow_do_not_consume_today_reservation(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res4.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 3)])
    _set_clock(monkeypatch, "2026-11-12T10:00:00")

    camp = create_campaign(
        conn,
        name="[QA] Tomorrow Followup",
        account_ids=[21],
        daily_limit=3,
        status="running",
        steps=_three_steps(delay_2=48, delay_3=24),
    )

    # Sent yesterday -> with 48h delay, due on 2026-11-13 (tomorrow), NOT today!
    yest_ts = "2026-11-11T12:00:00"
    _seed_prior_outgoing_msg(conn, account_id=21, peer_id=74001, username="due_tmrw", sent_at=yest_ts)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 74001, 'due_tmrw', 'Tmrw', 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], yest_ts, yest_ts, yest_ts),
    )
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 84000 + idx, f"cold_today_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway()
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert res["sent"] == 3


def test_replied_followup_releases_reserved_slot_in_same_day(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res5.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 2)])
    _set_clock(monkeypatch, "2026-11-12T08:00:00")

    camp = create_campaign(
        conn,
        name="[QA] Reply Releases Slot",
        account_ids=[21],
        daily_limit=2,
        status="running",
        steps=_three_steps(24, 24),
    )

    yest_ts = "2026-11-11T12:00:00"
    dlg_id = _seed_prior_outgoing_msg(conn, account_id=21, peer_id=75001, username="replied_warm", sent_at=yest_ts)
    # Client replied at 07:30 today!
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 555, 'in', 'Sounds interesting, send demos', '2026-11-12T07:30:00', '2026-11-12T07:30:00', '2026-11-12T07:30:00')
        """,
        (dlg_id,),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 75001, 'replied_warm', 'Replied Warm', 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], yest_ts, yest_ts, yest_ts),
    )
    for idx in range(1, 3):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 85000 + idx, f"cold_free_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway()
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    # Because replied_warm already replied, its slot is released -> both 2 cold leads are sent!
    assert res["sent"] == 2


def test_campaign_daily_limit_smaller_than_account_sum_reserves_followups(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res6.db")
    init_db(conn)
    _seed_accounts(
        conn,
        [
            (21, "+10000000021", "data/sessions/acc21.session", 20),
            (31, "+10000000031", "data/sessions/acc31.session", 20),
        ],
    )
    _set_clock(monkeypatch, "2026-11-12T08:15:00")

    camp = create_campaign(
        conn,
        name="[QA] Camp Cap Smaller Than Accounts",
        account_ids=[21, 31],
        daily_limit=5,
        status="running",
        steps=_three_steps(24, 24),
    )

    live_msgs = {}
    for idx in range(1, 4):
        peer_id = 76000 + idx
        uname = f"camp_cap_warm_{idx}"
        yest_ts = "2026-11-11T14:00:00"
        _seed_prior_outgoing_msg(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_ts)
        live_msgs[("data/sessions/acc21.session", peer_id)] = [
            {"telegram_message_id": 300 + idx, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
        ]
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Warm {idx}", yest_ts, yest_ts, yest_ts),
        )

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 86000 + idx, f"camp_cap_cold_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway(live_messages_by_session_peer=live_msgs)
    morning_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert morning_res["sent"] == 2

    _set_clock(monkeypatch, "2026-11-12T15:00:00")
    afternoon_res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert afternoon_res["sent"] == 3
    assert get_campaign(conn, camp["id"])["sent_today"] == 5


def test_other_running_campaign_followups_on_shared_account_are_also_reserved(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res7.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 4)])
    _set_clock(monkeypatch, "2026-11-12T08:15:00")

    camp_a = create_campaign(
        conn,
        name="[QA] Shared Acc Camp A",
        account_ids=[21],
        daily_limit=10,
        status="running",
        steps=_three_steps(24, 24),
    )
    camp_b = create_campaign(
        conn,
        name="[QA] Shared Acc Camp B",
        account_ids=[21],
        daily_limit=10,
        status="running",
        steps=_three_steps(24, 24),
    )

    live_msgs = {}
    for idx in range(1, 3):
        peer_id = 77000 + idx
        uname = f"shared_warm_{idx}"
        yest_ts = "2026-11-11T15:00:00"
        _seed_prior_outgoing_msg(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_ts)
        live_msgs[("data/sessions/acc21.session", peer_id)] = [
            {"telegram_message_id": 400 + idx, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
        ]
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp_a["id"], peer_id, uname, f"Warm {idx}", yest_ts, yest_ts, yest_ts),
        )

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp_b["id"], 87000 + idx, f"shared_cold_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway(live_messages_by_session_peer=live_msgs)
    res_b = asyncio.run(
        run_campaign_batch(conn, camp_b["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    # Acc 21 has daily_limit=4 and 2 follow-ups due later today in Camp A -> Camp B only takes 2 cold leads!
    assert res_b["sent"] == 2


def test_other_campaign_replied_or_capped_followups_do_not_hold_phantom_reservation(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res8.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 5)])
    _set_clock(monkeypatch, "2026-11-12T08:15:00")

    # Camp A has daily_limit=1, plus 1 already-replied lead and 3 other followups due later today.
    camp_a = create_campaign(
        conn,
        name="[QA] Capped Other Camp A",
        account_ids=[21],
        daily_limit=1,
        status="running",
        steps=_three_steps(24, 24),
    )
    camp_b = create_campaign(
        conn,
        name="[QA] Main Camp B",
        account_ids=[21],
        daily_limit=5,
        status="running",
        steps=_three_steps(24, 24),
    )

    yest_ts = "2026-11-11T15:00:00"
    dlg_rep = _seed_prior_outgoing_msg(conn, account_id=21, peer_id=78000, username="other_replied", sent_at=yest_ts)
    conn.execute(
        """
        INSERT INTO messages (
            dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
        ) VALUES (?, 900, 'in', 'Yes interested', '2026-11-12T07:00:00', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
        """,
        (dlg_rep,),
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 78000, 'other_replied', 'Other Rep', 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
        """,
        (camp_a["id"], yest_ts, yest_ts, yest_ts),
    )
    for idx in range(1, 4):
        peer_id = 78000 + idx
        _seed_prior_outgoing_msg(conn, account_id=21, peer_id=peer_id, username=f"other_warm_{idx}", sent_at=yest_ts)
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp_a["id"], peer_id, f"other_warm_{idx}", f"OW {idx}", yest_ts, yest_ts, yest_ts),
        )

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp_b["id"], 88000 + idx, f"b_cold_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    # Camp A can send at most daily_limit=1 today -> reserves only 1 slot on Acc 21, leaving 4 for Camp B!
    gw = RecordingGateway()
    res_b = asyncio.run(
        run_campaign_batch(conn, camp_b["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert res_b["sent"] == 4


def test_mismatched_assigned_account_and_phantom_inprogress_resolve_against_real_history(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res9.db")
    init_db(conn)
    _seed_accounts(
        conn,
        [
            (21, "+10000000021", "data/sessions/acc21.session", 2),
            (31, "+10000000031", "data/sessions/acc31.session", 2),
        ],
    )
    _set_clock(monkeypatch, "2026-11-12T08:15:00")

    camp = create_campaign(
        conn,
        name="[QA] History Verified Reserve",
        account_ids=[21, 31],
        daily_limit=4,
        status="running",
        steps=_three_steps(24, 24),
    )

    yest_ts = "2026-11-11T15:00:00"
    # Row 1: sent_account_id is NULL, assigned_account_id=31, but actual outgoing history is on Acc 21!
    _seed_prior_outgoing_msg(conn, account_id=21, peer_id=79001, username="real_on_21", sent_at=yest_ts)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 79001, 'real_on_21', 'Real 21', 'Hi', 31, NULL, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], yest_ts, yest_ts, yest_ts),
    )
    # Row 2: Phantom in_progress claiming sent_account_id=31, but ZERO messages in DB -> must reserve 0 slots!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, step1_text,
            assigned_account_id, sent_account_id, current_step, status,
            last_sent_at, created_at, updated_at
        ) VALUES (?, 79002, 'phantom_31', 'Phantom', 'Hi', 31, 31, 1, 'in_progress', ?, ?, ?)
        """,
        (camp["id"], yest_ts, yest_ts, yest_ts),
    )
    for idx in range(1, 5):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 89000 + idx, f"hist_cold_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway()
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    # Acc 21 reserves 1 slot (sends 1 cold), Acc 31 reserves 0 slots (sends 2 cold) -> total 3!
    assert res["sent"] == 3
    assert res["sent_by_account"] == {"21": 1, "31": 2}


def test_intraday_short_delay_followup_prevents_same_day_quota_overcommit(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res10.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 3)])
    _set_clock(monkeypatch, "2026-11-12T08:00:00")

    camp = create_campaign(
        conn,
        name="[QA] Intraday 2h Followup",
        account_ids=[21],
        daily_limit=3,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 2, "template_text": "2h followup"},
        ],
    )
    for idx in range(1, 4):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 90000 + idx, f"short_cold_{idx}", f"Short {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway()
    # At 08:00, sending 1 cold lead commits 2 messages today (08:00 + 10:00).
    # With daily_limit=3, a 2nd cold lead would commit 4 messages today -> must stop at 1!
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True)
    )
    assert res["sent"] == 1


def test_morning_sent_followups_plus_evening_reserved_followups_sum_cleanly(tmp_path, monkeypatch):
    conn = connect(tmp_path / "res11.db")
    init_db(conn)
    _seed_accounts(conn, [(21, "+10000000021", "data/sessions/acc21.session", 6)])

    camp = create_campaign(
        conn,
        name="[QA] Morning Sent Plus Evening Reserved",
        account_ids=[21],
        daily_limit=6,
        status="running",
        steps=_three_steps(24, 24),
    )

    live_msgs = {}
    # 2 follow-ups due at 08:00 today, 2 follow-ups due at 16:00 today
    for idx, yest_ts in enumerate(
        ("2026-11-11T08:00:00", "2026-11-11T08:05:00", "2026-11-11T16:00:00", "2026-11-11T16:05:00"),
        start=1,
    ):
        peer_id = 91000 + idx
        uname = f"mix_warm_{idx}"
        _seed_prior_outgoing_msg(conn, account_id=21, peer_id=peer_id, username=uname, sent_at=yest_ts)
        live_msgs[("data/sessions/acc21.session", peer_id)] = [
            {"telegram_message_id": 600 + idx, "direction": "out", "text": "Step 1", "sent_at": yest_ts}
        ]
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                assigned_account_id, sent_account_id, current_step, status,
                last_sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Hi', 21, 21, 1, 'in_progress', ?, ?, ?)
            """,
            (camp["id"], peer_id, uname, f"Mix {idx}", yest_ts, yest_ts, yest_ts),
        )

    for idx in range(1, 6):
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, telegram_user_id, username, name, step1_text,
                current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'pending', '2026-11-12T07:00:00', '2026-11-12T07:00:00')
            """,
            (camp["id"], 92000 + idx, f"mix_cold_{idx}", f"Cold {idx}", f"Hello {idx}"),
        )
    conn.commit()

    gw = RecordingGateway(live_messages_by_session_peer=live_msgs)

    # First tick at 08:10 sends the 2 due follow-ups (max_send=2) -> sent_today becomes 2.
    _set_clock(monkeypatch, "2026-11-12T08:10:00")
    r1 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=2, respect_delays=True))
    assert r1["sent"] == 2

    # Second tick at 08:15: sent_today=2, evening_reserved=2, daily_limit=6 -> sends exactly 2 cold leads!
    _set_clock(monkeypatch, "2026-11-12T08:15:00")
    r2 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert r2["sent"] == 2

    # Third tick at 16:30: evening follow-ups are due -> sends remaining 2 follow-ups (total 6/6)!
    _set_clock(monkeypatch, "2026-11-12T16:30:00")
    r3 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert r3["sent"] == 2
    assert get_campaign(conn, camp["id"])["sent_today"] == 6
