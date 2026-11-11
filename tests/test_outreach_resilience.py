import asyncio
from pathlib import Path
import sys

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import init_db, connect, utc_now
from app.outreach import (
    create_campaign,
    get_campaign,
    heal_transient_failed_recipients,
    import_campaign_csv,
    run_autopilot_tick,
    run_campaign_batch,
)
from app.telegram_gateway import TelegramGateway, GatewayError


def _setup_conn(tmp_path):
    db_path = tmp_path / "test_resilience.db"
    conn = connect(db_path)
    init_db(conn)
    return conn


class BrokenThenHealthyGateway(TelegramGateway):
    """Simulates dead session on account 1 (InvokeWithLayerRequest) and entity cache miss on integer peer_id."""

    def __init__(self, broken_sessions: set[str], entity_miss_ids: set[int]):
        self.sent: list[dict] = []
        self.broken_sessions = set(broken_sessions)
        self.entity_miss_ids = set(entity_miss_ids)
        self.resolved_usernames_by_session: dict[str, set[str]] = {}

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        if session_path in self.broken_sessions:
            raise GatewayError(
                "The very first request must always be InvokeWithLayerRequest "
                "(caused by InvokeWithLayerRequest(InitConnectionRequest(GetConfigRequest)))"
            )
        q = (query or "").strip().lstrip("@").lower()
        if q:
            self.resolved_usernames_by_session.setdefault(session_path, set()).add(q)
            return [{"telegram_user_id": 109693549, "username": q, "name": q.title()}]
        return []

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        image_path: str | None = None,
    ) -> dict:
        if session_path in self.broken_sessions:
            raise GatewayError(
                "The very first request must always be InvokeWithLayerRequest "
                "(caused by InvokeWithLayerRequest(InitConnectionRequest(GetConfigRequest)))"
            )
        if isinstance(peer_id, int) and peer_id in self.entity_miss_ids:
            resolved = self.resolved_usernames_by_session.get(session_path, set())
            if not resolved:
                raise GatewayError(
                    f"Could not find the input entity for PeerUser(user_id={peer_id}) (PeerUser). "
                    "Please read https://docs.telethon.dev/en/stable/concepts/entities.html to find out more details."
                )
        self.sent.append(
            {
                "session_path": session_path,
                "peer_id": peer_id,
                "text": text,
                "image_path": image_path,
            }
        )
        return {
            "telegram_message_id": 5000 + len(self.sent),
            "text": text,
            "sent_at": utc_now(),
            "direction": "out",
        }


def test_dead_account_in_pool_is_skipped_and_does_not_fail_recipients(tmp_path):
    conn = _setup_conn(tmp_path)
    now = utc_now()
    # Account 1: broken session (order 0 in pool)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES (21, '+905346380943', 'data/sessions/21.session', 'connected', 'active', 20, 0, '2026-11-10', ?, ?)
        """,
        (now, now),
    )
    # Account 2: healthy session (order 1 in pool)
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES (30, '+79951210659', 'data/sessions/30.session', 'connected', 'active', 20, 0, '2026-11-10', ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    camp = create_campaign(
        conn,
        name="Resilience Pool Test",
        account_ids=[21, 30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "Step 1 for {name}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Followup for {audience}"},
        ],
    )
    import_campaign_csv(
        conn,
        camp["id"],
        "username,name,category\n@lead_one,Lead One,SaaS\n@lead_two,Lead Two,Gaming\n",
    )

    gw = BrokenThenHealthyGateway(
        broken_sessions={"data/sessions/21.session"},
        entity_miss_ids=set(),
    )
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=2, respect_delays=True)
    )
    assert res["sent"] == 2

    # Account 21 must now be marked unauthorized in DB
    acc21 = conn.execute("SELECT health, last_error FROM accounts WHERE id = 21").fetchone()
    assert acc21["health"] == "unauthorized"
    assert "InvokeWithLayerRequest" in (acc21["last_error"] or "")

    # Zero recipients should be marked failed; both should be in_progress on account 30
    updated_camp = get_campaign(conn, camp["id"])
    assert updated_camp["failed_count"] == 0
    assert updated_camp["in_progress_count"] == 2
    for r in updated_camp["recipients"]:
        assert r["status"] == "in_progress"
        assert r["assigned_account_id"] == 30


def test_followup_missing_input_entity_falls_back_to_username(tmp_path):
    conn = _setup_conn(tmp_path)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES (33, '+998336999642', 'data/sessions/33.session', 'connected', 'active', 20, 0, '2026-11-10', ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    camp = create_campaign(
        conn,
        name="Followup Entity Cache Miss Test",
        account_ids=[33],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {
                "step_order": 2,
                "delay_hours": 24,
                "template_text": "Hey, we bring {audience} to your product",
            },
            {"step_order": 3, "delay_hours": 24, "template_text": "Step 3"},
        ],
    )
    import_campaign_csv(
        conn,
        camp["id"],
        "username,name,category,step1_text\n@dariusiv,Darius,Gaming,Initial hello\n",
    )
    # Put recipient at step 1 (waiting for step 2 follow-up) with real telegram_user_id not in Telethon entity cache
    # Seed prior Step 1 outgoing message on account 33 so empty-chat follow-up guard recognizes legitimate prior touch
    cur_d = conn.execute(
        """
        INSERT INTO dialogs (account_id, peer_type, peer_id, username, title, created_at, updated_at)
        VALUES (33, 'user', 109693549, 'dariusiv', 'Darius', '2026-10-01T10:00:00', '2026-10-01T10:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (?, 1, 'out', 'Initial hello', '2026-10-01T10:00:00', '2026-10-01T10:00:00', '2026-10-01T10:00:00')
        """,
        (int(cur_d.lastrowid),),
    )
    conn.execute(
        """
        UPDATE campaign_recipients
        SET current_step = 1,
            status = 'in_progress',
            assigned_account_id = 33,
            sent_account_id = 33,
            telegram_user_id = 109693549,
            last_sent_at = '2026-10-01T10:00:00'
        WHERE campaign_id = ?
        """,
        (camp["id"],),
    )
    conn.commit()

    gw = BrokenThenHealthyGateway(
        broken_sessions=set(),
        entity_miss_ids={109693549},
    )
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=1, respect_delays=True)
    )
    assert res["sent"] == 1
    assert len(gw.sent) == 1
    assert "players" in gw.sent[0]["text"]

    updated_camp = get_campaign(conn, camp["id"])
    assert updated_camp["failed_count"] == 0
    rec = updated_camp["recipients"][0]
    assert rec["status"] == "in_progress"
    assert rec["current_step"] == 2


def test_heal_transient_failed_recipients_restores_pending_and_in_progress(tmp_path):
    conn = _setup_conn(tmp_path)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES (30, '+79951210659', 'data/sessions/30.session', 'connected', 'active', 20, 0, '2026-11-10', ?, ?)
        """,
        (now, now),
    )
    conn.commit()

    camp = create_campaign(
        conn,
        name="Heal Failed Test",
        account_ids=[30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "S1"},
            {"step_order": 2, "delay_hours": 24, "template_text": "S2"},
            {"step_order": 3, "delay_hours": 24, "template_text": "S3"},
        ],
    )
    import_campaign_csv(
        conn,
        camp["id"],
        "username,name\n@u_step0_layer,U1\n@u_step2_entity,U2\n@u_step1_authdup,U3\n@u_real_invalid,U4\n",
    )
    recs = get_campaign(conn, camp["id"])["recipients"]
    conn.execute(
        "UPDATE campaign_recipients SET current_step = 0, status = 'failed', error_text = ? WHERE id = ?",
        (
            "The very first request must always be InvokeWithLayerRequest (caused by InvokeWithLayerRequest(InitConnectionRequest(GetConfigRequest)))",
            recs[0]["id"],
        ),
    )
    conn.execute(
        "UPDATE campaign_recipients SET current_step = 2, status = 'failed', error_text = ? WHERE id = ?",
        (
            "Could not find the input entity for PeerUser(user_id=109693549) (PeerUser).",
            recs[1]["id"],
        ),
    )
    conn.execute(
        "UPDATE campaign_recipients SET current_step = 1, status = 'failed', error_text = ? WHERE id = ?",
        (
            "The authorization key (session file) was used under two different IP addresses simultaneously, and can no longer be used.",
            recs[2]["id"],
        ),
    )
    conn.execute(
        "UPDATE campaign_recipients SET current_step = 0, status = 'failed', error_text = ? WHERE id = ?",
        (
            'No user has "u_real_invalid" as username',
            recs[3]["id"],
        ),
    )
    conn.commit()

    healed = heal_transient_failed_recipients(conn, camp["id"])
    assert healed == 3

    refreshed = {r["username"]: r for r in get_campaign(conn, camp["id"])["recipients"]}
    assert refreshed["u_step0_layer"]["status"] == "pending"
    assert refreshed["u_step0_layer"]["error_text"] is None
    assert refreshed["u_step2_entity"]["status"] == "in_progress"
    assert refreshed["u_step2_entity"]["error_text"] is None
    assert refreshed["u_step1_authdup"]["status"] == "in_progress"
    assert refreshed["u_step1_authdup"]["error_text"] is None
    assert refreshed["u_real_invalid"]["status"] == "failed"
