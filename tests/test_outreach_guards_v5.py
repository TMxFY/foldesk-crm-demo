import asyncio
import json
from pathlib import Path
import sys

import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import apply_canonical_vlad_outreach_v5, connect, init_db, utc_now
from app.leadgen_settings import (
    get_leadgen_config,
    push_discovered_now,
    resolve_target_b2b_tg_campaign_name,
    sync_leadgen_campaigns,
)
from app.mcp_server import handle_jsonrpc_message
from app.outreach import (
    create_campaign,
    get_campaign,
    import_campaign_csv,
    run_campaign_batch,
    update_campaign,
)
from app.telegram_gateway import GatewayError, TelegramGateway
from leadgen import crm_mcp_push, followup_agent, tg_curate, tg_enrich_leads
from leadgen.outreach_copy import (
    REVSHARE_OFFER_TEXT,
    SOFTUGC_PITCH_TEXT,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_TEXT,
)


class RecordingGateway(TelegramGateway):
    def __init__(
        self,
        *,
        broken_sessions: set[str] | None = None,
        live_messages_by_session_peer: dict[tuple[str, int], list[dict]] | None = None,
        on_after_send=None,
    ):
        self.sent: list[dict] = []
        self.broken_sessions = set(broken_sessions or set())
        self.live_messages_by_session_peer = dict(live_messages_by_session_peer or {})
        self.on_after_send = on_after_send

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        if session_path in self.broken_sessions:
            raise GatewayError("The authorization key (session file) was used under two different IP addresses simultaneously")
        clean = (query or "").strip().lstrip("@").lower()
        fake_id = 100000 + (sum(ord(ch) for ch in clean) % 700000)
        return [{"telegram_user_id": fake_id, "username": clean, "name": clean.title()}]

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        if session_path in self.broken_sessions:
            raise GatewayError("Session revoked / unauthorized")
        key = (session_path, int(peer_id))
        if key in self.live_messages_by_session_peer:
            return list(self.live_messages_by_session_peer[key])
        return []

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to: int | None = None,
        image_path=None,
    ) -> dict:
        if session_path in self.broken_sessions:
            raise GatewayError("The authorization key (session file) was used under two different IP addresses simultaneously")
        entry = {
            "session_path": session_path,
            "peer_id": peer_id,
            "text": text,
            "image_path": image_path,
        }
        self.sent.append(entry)
        if callable(self.on_after_send):
            self.on_after_send(entry)
        return {
            "telegram_message_id": 5000 + len(self.sent),
            "sent_at": utc_now(),
        }


def _seed_two_accounts(conn):
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO accounts (
            id, phone, session_path, status, health, telegram_name,
            daily_limit, sent_today, sent_date, created_at, updated_at
        ) VALUES (?, ?, ?, 'connected', 'active', ?, 20, 0, '2026-11-11', ?, ?)
        """,
        [
            (21, "+10000000021", "data/sessions/acc21.session", "Outreach Acc 21", now, now),
            (31, "+10000000031", "data/sessions/acc31.session", "Sergey Glebov", now, now),
        ],
    )
    conn.commit()


def _seed_prior_outgoing_msg(
    conn,
    *,
    account_id: int,
    peer_id: int,
    username: str,
    text: str = "Hey,\n\nInitial SoftUGC touch.",
    sent_at: str = "2026-09-14T10:00:00",
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


# ---------------------------------------------------------------------------
# 1) Pinning sent_account_id & Iron Invariant blocking follow-up in empty chat
# ---------------------------------------------------------------------------
def test_v5_guard1_followup_never_switches_account_and_blocks_empty_chat(tmp_path):
    conn = connect(tmp_path / "guard1.db")
    init_db(conn)
    _seed_two_accounts(conn)

    camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[21, 31],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
            {"step_order": 3, "delay_hours": 72, "template_text": VLAD_STEP3_TEXT},
        ],
    )
    now = "2026-11-11T10:00:00"

    # Case A: Step 0 pins sent_account_id immediately and marks cloud_persistence_dirty_at
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, 200001, 'freshfounder', 'Fresh', 'FreshAI', 'SaaS', ?, 0, 'pending', ?, ?)
        """,
        (
            camp["id"],
            f"Hey,\n\nSaw FreshAI. Steady signups are tough.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}",
            now,
            now,
        ),
    )
    conn.commit()

    gw_step0 = RecordingGateway()
    res0 = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw_step0, max_send=1))
    assert res0["sent"] == 1
    fresh_row = conn.execute(
        "SELECT status, current_step, sent_account_id, assigned_account_id FROM campaign_recipients WHERE username = 'freshfounder'"
    ).fetchone()
    assert fresh_row["status"] == "in_progress"
    assert fresh_row["current_step"] == 1
    assert fresh_row["sent_account_id"] == 21
    assert fresh_row["assigned_account_id"] == 21
    dirty_kv = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'cloud_persistence_dirty_at'"
    ).fetchone()
    assert dirty_kv is not None and dirty_kv["value"]

    # Case B: Joseph Chang (@joseph0201) got Step 1 on 2026-09-14 from account 21 (sent_account_id NULL historically),
    # and now account 21 hits its daily limit (sent_today=20) while account 31 (Sergey Glebov) is wide open!
    # Account 31 has an EMPTY chat with Joseph Chang -> MUST NOT send Step 2 from account 31; must keep in_progress!
    _seed_prior_outgoing_msg(
        conn,
        account_id=21,
        peer_id=300201,
        username="joseph0201",
        text="Hey,\n\nSaw OpenFi.",
        sent_at="2026-09-14T12:00:00",
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            sent_account_id, assigned_account_id, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 300201, 'joseph0201', 'Joseph Chang', 'OpenFi', 'Финтех', NULL, NULL, 1, 'in_progress', '2026-09-14T12:00:00', ?, ?)
        """,
        (camp["id"], now, now),
    )
    today_str = conn.execute("SELECT sent_date FROM accounts WHERE id = 21").fetchone()[0]
    from app.db import utc_today

    conn.execute(
        "UPDATE accounts SET sent_today = 20, daily_limit = 20, sent_date = ? WHERE id = 21",
        (utc_today(),),
    )
    # Case C: Jeremy | OpenEden (@jeremy_openeden) is at current_step=1 in DB, but has ZERO outgoing messages in `messages`!
    # Follow-up MUST be blocked and moved to status='completed' with exact error reason!
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            sent_account_id, assigned_account_id, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 300999, 'jeremy_openeden', 'Jeremy | OpenEden', 'OpenEden', 'DeFi', 31, 31, 1, 'in_progress', '2026-10-01T12:00:00', ?, ?)
        """,
        (camp["id"], now, now),
    )
    conn.commit()

    gw_followup = RecordingGateway(
        live_messages_by_session_peer={
            ("data/sessions/acc21.session", 300201): [
                {"telegram_message_id": 101, "direction": "out", "text": "Hey,\n\nSaw OpenFi.", "sent_at": "2026-09-14T12:00:00"}
            ]
        }
    )
    res_fu = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw_followup, max_send=10, respect_delays=True)
    )
    assert res_fu["sent"] == 0
    assert gw_followup.sent == []

    joseph = conn.execute(
        "SELECT status, current_step, sent_account_id, error_text FROM campaign_recipients WHERE username = 'joseph0201'"
    ).fetchone()
    # Historical sent_account_id=21 was resolved & saved, and because acc 21 is at limit, Joseph stays in_progress (NOT sent via acc 31!)
    assert joseph["sent_account_id"] == 21
    assert joseph["status"] == "in_progress"
    assert joseph["current_step"] == 1

    jeremy = conn.execute(
        "SELECT status, current_step, error_text FROM campaign_recipients WHERE username = 'jeremy_openeden'"
    ).fetchone()
    assert jeremy["status"] == "completed"
    assert jeremy["error_text"] == "Фоллоу-ап заблокирован: нет предыдущего сообщения в чате"

    # Also test: if account 21 has quota, ONLY account 21 sends Step 2 to Joseph; if account 21 session fails mid-batch, it STILL never rotates to account 31!
    conn.execute("UPDATE accounts SET sent_today = 0 WHERE id = 21")
    conn.commit()
    gw_broken_21 = RecordingGateway(broken_sessions={"data/sessions/acc21.session"})
    res_broken = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw_broken_21, max_send=10, respect_delays=True)
    )
    assert res_broken["sent"] == 0
    assert gw_broken_21.sent == []
    joseph_after_err = conn.execute(
        "SELECT status, current_step, sent_account_id FROM campaign_recipients WHERE username = 'joseph0201'"
    ).fetchone()
    assert joseph_after_err["status"] == "in_progress"
    assert joseph_after_err["current_step"] == 1
    assert joseph_after_err["sent_account_id"] == 21


# ---------------------------------------------------------------------------
# 2) Anti-Duplicate Send Guard on Step 0 (cross-campaign, intra-campaign, messages)
# ---------------------------------------------------------------------------
def test_v5_guard2_anti_duplicate_step0_across_campaigns_within_campaign_and_messages(tmp_path):
    conn = connect(tmp_path / "guard2.db")
    init_db(conn)
    _seed_two_accounts(conn)

    old_camp = create_campaign(
        conn,
        name="Leadgen Auto (TG)",
        account_ids=[21],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    new_camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[21, 31],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
        ],
    )
    now = "2026-11-11T10:00:00"
    canonical_blur_text = (
        f"Hey,\n\nSaw HigherLevel. Consistent user growth is hard.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    )

    # 1) Cross-campaign duplicate: @higherlevel already contacted in old_camp (status='completed', current_step=1)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 401001, 'higherlevel', 'Blur', 'HigherLevel', ?, 1, 'completed', '2026-11-10T19:51:00', ?, ?)
        """,
        (old_camp["id"], canonical_blur_text, now, now),
    )
    # Duplicate row in new_camp with '@HigherLevel' casing
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 401001, '@HigherLevel', 'Blur', 'HigherLevel', ?, 0, 'pending', ?, ?)
        """,
        (new_camp["id"], canonical_blur_text, now, now),
    )

    # 2) Intra-campaign duplicate in same batch: two pending rows for 'samebatch_founder'
    batch_pitch = (
        f"Hey,\n\nSaw NovaPay. Paid ads get pricey.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    )
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'Nova', 'NovaPay', ?, 0, 'pending', ?, ?)
        """,
        [
            (new_camp["id"], 402001, "samebatch_founder", batch_pitch, now, now),
            (new_camp["id"], 402002, "@SameBatch_Founder", batch_pitch, now, now),
        ],
    )

    # 3) Crash-before-checkpoint duplicate in `messages`: outgoing message with identical stripped text already in `messages`!
    crash_pitch = (
        f"Hey,\n\nSaw CrashSafe. Scaling organically takes volume.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
    )
    _seed_prior_outgoing_msg(
        conn,
        account_id=31,
        peer_id=403001,
        username="crash_dup_user",
        text=f"  {crash_pitch}\n ",
        sent_at="2026-11-11T09:30:00",
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 403001, 'crash_dup_user', 'CrashUser', 'CrashSafe', ?, 0, 'pending', ?, ?)
        """,
        (new_camp["id"], crash_pitch, now, now),
    )
    conn.commit()

    gw = RecordingGateway()
    res = asyncio.run(run_campaign_batch(conn, new_camp["id"], gateway=gw, max_send=10))
    # ONLY the first 'samebatch_founder' row should actually be sent!
    assert res["sent"] == 1
    assert len(gw.sent) == 1
    assert "NovaPay" in gw.sent[0]["text"]

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, username, status, current_step, sent_account_id, error_text FROM campaign_recipients WHERE campaign_id = ? ORDER BY id ASC",
            (new_camp["id"],),
        ).fetchall()
    ]
    # Row 0: @HigherLevel -> skipped cross-campaign duplicate
    assert rows[0]["status"] == "completed"
    assert rows[0]["current_step"] == 0
    assert rows[0]["error_text"] == "Пропущен дубль: контакт уже получал аутрич"
    # Row 1: first samebatch_founder -> sent Step 0
    assert rows[1]["status"] == "in_progress"
    assert rows[1]["current_step"] == 1
    assert rows[1]["sent_account_id"] in (21, 31)
    # Row 2: second @SameBatch_Founder -> skipped intra-campaign duplicate
    assert rows[2]["status"] == "completed"
    assert rows[2]["current_step"] == 0
    assert rows[2]["error_text"] == "Пропущен дубль: контакт уже получал аутрич"
    # Row 3: crash_dup_user -> synced from `messages` without resending!
    assert rows[3]["status"] == "in_progress"
    assert rows[3]["current_step"] == 1
    assert rows[3]["sent_account_id"] == 31
    assert rows[3]["error_text"] is None


# ---------------------------------------------------------------------------
# 3) On-the-fly auto-upgrade of CreatorOS / ceiling / plug-and-play growth arm
# ---------------------------------------------------------------------------
def test_v5_guard3_auto_upgrades_creatoros_and_ceiling_on_the_fly_and_scrubs_followup_agent(tmp_path):
    conn = connect(tmp_path / "guard3.db")
    init_db(conn)
    _seed_two_accounts(conn)

    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[21],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    vac_camp = create_campaign(
        conn,
        name="TG Вакансии",
        account_ids=[21],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    now = "2026-11-11T11:00:00"

    # Insert raw legacy texts directly into SQLite (simulating uncleaned DB rows)
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', ?, ?)
        """,
        [
            (
                b2b_camp["id"],
                501001,
                "legacy_ceiling",
                "Alex",
                "LoopMetrics",
                "If your user growth has hit a ceiling, it's usually not the product - it's the cost of reach. Saw LoopMetrics. Want to see how it's set up for a",
                now,
                now,
            ),
            (
                b2b_camp["id"],
                501002,
                "legacy_creatoros",
                "Sam",
                "ChainPulse",
                "Hey Sam, we built CreatorOS as a plug-and-play growth arm for Web3 apps. Open to a quick chat?",
                now,
                now,
            ),
            (
                vac_camp["id"],
                501003,
                "hr_vacancy_lead",
                "HR Anna",
                "SMM Studio",
                "Привет! Откликаюсь на вакансию рилсмейкера, вот портфолио.",
                now,
                now,
            ),
        ],
    )
    conn.commit()

    gw = RecordingGateway()
    res_b2b = asyncio.run(run_campaign_batch(conn, b2b_camp["id"], gateway=gw, max_send=10))
    assert res_b2b["sent"] == 2
    for sent_msg in gw.sent:
        txt = sent_msg["text"]
        assert "creatoros" not in txt.lower()
        assert "if your user growth has hit a ceiling" not in txt.lower()
        assert "plug-and-play growth arm" not in txt.lower()
        assert "SoftUGC" in txt
        assert txt.endswith(REVSHARE_OFFER_TEXT)

    gw_vac = RecordingGateway()
    res_vac = asyncio.run(run_campaign_batch(conn, vac_camp["id"], gateway=gw_vac, max_send=5))
    assert res_vac["sent"] == 1
    assert gw_vac.sent[0]["text"] == "Привет! Откликаюсь на вакансию рилсмейкера, вот портфолио."

    # Verify followup_agent has zero CreatorOS leaks and cannot send outside CRM
    sanitized_fu = followup_agent._sanitize_followup("Checking in from CreatorOS on your reels")
    assert "creatoros" not in sanitized_fu.lower()
    problems = followup_agent.validate_followup_pair("Ping from CreatorOS?", "Second ping?")
    assert "f1_repeats_case_or_brand" in problems
    fu_src = Path(followup_agent.__file__).read_text(encoding="utf-8")
    assert "TelegramClient" not in fu_src
    assert "send_message(" not in fu_src


# ---------------------------------------------------------------------------
# 4) Migration apply_canonical_vlad_outreach_v5
# ---------------------------------------------------------------------------
def test_v5_guard4_migration_freezes_old_in_progress_consolidates_vse2_and_preserves_vacancies(tmp_path):
    conn = connect(tmp_path / "guard4.db")
    init_db(conn)
    _seed_two_accounts(conn)

    camp_auto = create_campaign(
        conn,
        name="Leadgen Auto (TG)",
        account_ids=[21],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    camp_vse2 = create_campaign(
        conn,
        name="SoftUGC — База Все2 (2 лида)",
        account_ids=[21, 31],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    camp_vac = create_campaign(
        conn,
        name="TG Вакансии",
        account_ids=[21],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Фоллоу-ап по вакансии"},
        ],
    )
    # Put both B2B campaigns in running/leadgen_enabled=1 before migration to verify migration pauses & disables both
    conn.execute(
        "UPDATE campaigns SET status = 'running', leadgen_enabled = 1 WHERE id IN (?, ?)",
        (camp_auto["id"], camp_vse2["id"]),
    )
    now = "2026-11-11T10:00:00"

    # Seed historical outgoing message for mantegna on account 21
    _seed_prior_outgoing_msg(
        conn,
        account_id=21,
        peer_id=600101,
        username="mantegna",
        text="Old step 1 to mantegna",
        sent_at="2026-11-10T15:00:00",
    )

    # Seed recipients in SoftUGC — База Все2 (#5):
    # - mantegna & vaibhavsharda at current_step=1, status='in_progress' (MUST be closed to 'completed' + sent_account_id backfilled)
    # - existing_clean at current_step=0, status='pending'
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, category,
            step1_text, extra_json, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'SaaS', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                camp_vse2["id"],
                600101,
                "mantegna",
                "Mantegna",
                "Mantegna Labs",
                "Old step 1",
                json.dumps({"fine_status": "need_step2"}),
                1,
                "in_progress",
                "2026-11-10T15:00:00",
                now,
                now,
            ),
            (
                camp_vse2["id"],
                600102,
                "vaibhavsharda",
                "Vaibhav",
                "Fabric",
                "Old step 1",
                None,
                2,
                "in_progress",
                "2026-11-10T16:00:00",
                now,
                now,
            ),
            (
                camp_vse2["id"],
                600103,
                "existing_clean",
                "Clean",
                "CleanSaaS",
                f"Hey,\n\nSaw CleanSaaS.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}",
                None,
                0,
                "pending",
                None,
                now,
                now,
            ),
        ],
    )

    # Seed recipients in Leadgen Auto (TG) (#4):
    # - clean_from_auto (step=0, pending -> MUST move to #5)
    # - existing_clean duplicate (@Existing_Clean, step=0, pending -> MUST be deleted from #4)
    # - offline_pool_ru (offline RU non-ICP -> MUST NOT move to #5)
    # - old_auto_inprog (step=1, in_progress -> MUST close to 'completed')
    conn.executemany(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, website, category, their_angle,
            step1_text, current_step, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                camp_auto["id"],
                600201,
                "@clean_from_auto",
                "Founder",
                "OrbitAI",
                "https://orbitai.io",
                "AI",
                "AI workflow automation tool",
                "Old CreatorOS draft",
                0,
                "pending",
                now,
                now,
            ),
            (
                camp_auto["id"],
                600202,
                "@Existing_Clean",
                "Clean Dup",
                "CleanSaaS",
                "",
                "SaaS",
                "SaaS app",
                "Dup draft",
                0,
                "pending",
                now,
                now,
            ),
            (
                camp_auto["id"],
                600203,
                "pool_builder_ru",
                "Бассейны РФ",
                "Simple Pools",
                "simple-pools.ru",
                "Строительство",
                "Строительство бассейнов под ключ",
                "Привет",
                0,
                "pending",
                now,
                now,
            ),
            (
                camp_auto["id"],
                600204,
                "old_auto_inprog",
                "OldAuto",
                "OldCo",
                "",
                "SaaS",
                "SaaS",
                "Step 1 sent",
                1,
                "in_progress",
                now,
                now,
            ),
        ],
    )

    # Seed active in_progress recipient in TG Вакансии (Cyrillic 'Вакансии' -> MUST remain untouched!)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, 600301, 'vacancy_hr_active', 'HR', 'Agency', 'Отклик', 1, 'in_progress', '2026-11-10T18:00:00', ?, ?)
        """,
        (camp_vac["id"], now, now),
    )
    # Remove v5 kv marker seeded by empty init_db so we run migration against populated data
    conn.execute("DELETE FROM kv_settings WHERE key = 'canonical_vlad_outreach_v5'")
    conn.commit()

    stats = apply_canonical_vlad_outreach_v5(conn)
    assert stats["already_applied"] is False
    assert stats["closed_in_progress"] == 3  # mantegna, vaibhavsharda, old_auto_inprog
    assert stats["backfilled_sent_account"] == 1  # mantegna -> 21
    assert stats["moved_to_vse2"] == 1  # clean_from_auto
    assert stats["dedup_deleted_from_auto"] == 1  # @Existing_Clean

    # Repeat call must be idempotent
    stats_again = apply_canonical_vlad_outreach_v5(conn)
    assert stats_again["already_applied"] is True

    # Verify mantegna & vaibhavsharda are completed and mantegna has sent_account_id=21 + fine_status='completed'
    mantegna = conn.execute(
        "SELECT status, sent_account_id, extra_json FROM campaign_recipients WHERE username = 'mantegna'"
    ).fetchone()
    assert mantegna["status"] == "completed"
    assert mantegna["sent_account_id"] == 21
    assert json.loads(mantegna["extra_json"])["fine_status"] == "completed"

    vaibhav = conn.execute(
        "SELECT status FROM campaign_recipients WHERE username = 'vaibhavsharda'"
    ).fetchone()
    assert vaibhav["status"] == "completed"

    # Verify clean_from_auto moved to camp_vse2 with upgraded canonical SoftUGC step1_text
    moved = conn.execute(
        "SELECT campaign_id, status, current_step, step1_text FROM campaign_recipients WHERE username = 'clean_from_auto'"
    ).fetchone()
    assert moved["campaign_id"] == camp_vse2["id"]
    assert moved["status"] == "pending"
    assert moved["current_step"] == 0
    assert "SoftUGC" in moved["step1_text"]
    assert moved["step1_text"].endswith(REVSHARE_OFFER_TEXT)

    # Duplicate @Existing_Clean was deleted from camp_auto
    dup_count = conn.execute(
        "SELECT COUNT(*) FROM campaign_recipients WHERE lower(username) = 'existing_clean'"
    ).fetchone()[0]
    assert dup_count == 1

    # Both B2B campaigns are paused and leadgen_enabled = 0
    for cid in (camp_auto["id"], camp_vse2["id"]):
        crow = conn.execute("SELECT status, leadgen_enabled FROM campaigns WHERE id = ?", (cid,)).fetchone()
        assert crow["status"] == "paused"
        assert int(crow["leadgen_enabled"]) == 0

    # TG Вакансии in_progress row was NOT touched!
    vac_rec = conn.execute(
        "SELECT status, current_step FROM campaign_recipients WHERE username = 'vacancy_hr_active'"
    ).fetchone()
    assert vac_rec["status"] == "in_progress"
    assert vac_rec["current_step"] == 1


# ---------------------------------------------------------------------------
# 5) Auto-routing of new B2B leads to SoftUGC — База Все2 & claude-sonnet-5
# ---------------------------------------------------------------------------
def test_v5_guard5_mcp_and_leadgen_route_b2b_to_softugc_vse2_vacancies_separate_and_sonnet5(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "guard5.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_two_accounts(conn)

    # Clean DB fallback (before SoftUGC campaign exists): keeps 'Leadgen Auto (TG)' for legacy unit tests
    assert resolve_target_b2b_tg_campaign_name(conn, "") == "Leadgen Auto (TG)"

    # Create SoftUGC — База Все2 (paused, leadgen_enabled=0) and Leadgen Auto (TG)
    camp_auto = create_campaign(
        conn,
        name="Leadgen Auto (TG)",
        account_ids=[21],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    camp_vse2 = create_campaign(
        conn,
        name="SoftUGC — База Все2 (10 лидов)",
        account_ids=[21, 31],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    update_campaign(conn, camp_vse2["id"], status="paused", leadgen_enabled=False)
    conn.close()

    # Track calls to generate_vlad_sonnet_batch(..., model="claude-sonnet-5")
    sonnet_calls: list[dict] = []

    def _fake_sonnet_batch(leads, *, api_key=None, base_url=None, model="claude-sonnet-5", timeout=90):
        sonnet_calls.append({"count": len(leads), "model": model, "leads": list(leads)})
        out = []
        for ld in leads:
            comp = ld.get("company") or ld.get("name") or "Product"
            out.append(
                {
                    "audience": (
                        "players"
                        if any(k in str(ld.get("category") or "").lower() for k in ("game", "gaming"))
                        else "users"
                    ),
                    "step1_text": (
                        f"Hey,\n\nSaw {comp}. Custom Sonnet pain paragraph for {comp}.\n\n"
                        f"{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
                    ),
                }
            )
        return out

    monkeypatch.setenv("RELAYFAST_API_KEY", "test-relayfast-key")
    monkeypatch.setenv("FORCE_AUTO_SONNET_IN_TEST", "1")
    monkeypatch.setattr("leadgen.outreach_copy.generate_vlad_sonnet_batch", _fake_sonnet_batch)
    monkeypatch.setattr(crm_mcp_push, "generate_vlad_sonnet_batch", _fake_sonnet_batch)

    # A) MCP ingest_discovered_leads with mixed B2B + Vacancy items and default campaign_name
    rpc_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "ingest_discovered_leads",
            "arguments": {
                "campaign_name": "Leadgen Auto (TG)",
                "activate_campaign": False,
                "leads": [
                    {
                        "username": "@pixelquest_founder",
                        "name": "PixelQuest",
                        "company": "PixelQuest",
                        "category": "Gaming",
                        "their_angle": "Multiplayer browser RPG",
                        "source": "yc_ph",
                    },
                    {
                        "username": "@hr_reels_job",
                        "name": "Ищем рилсмейкера",
                        "company": "BrandStudio",
                        "category": "Вакансия",
                        "source": "tg_jobs",
                        "step1_text": "Привет! Пишу по вакансии рилсмейкера.",
                    },
                ],
            },
        },
    }
    conn = connect(db_path)
    resp = handle_jsonrpc_message(conn, rpc_req)
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["enrolled_in_campaign"] == 2
    # B2B lead landed in SoftUGC — База Все2 (#5), NOT in Leadgen Auto (TG) (#4)!
    vse2_rec = conn.execute(
        "SELECT campaign_id, step1_text, step2_text, step3_text, extra_json FROM campaign_recipients WHERE username = 'pixelquest_founder'"
    ).fetchone()
    assert vse2_rec is not None
    assert vse2_rec["campaign_id"] == camp_vse2["id"]
    assert "Custom Sonnet pain paragraph for PixelQuest" in vse2_rec["step1_text"]
    assert "(which equals highly qualified players)" in vse2_rec["step2_text"]
    assert vse2_rec["step3_text"] == VLAD_STEP3_TEXT
    assert json.loads(vse2_rec["extra_json"])["audience"] == "players"

    # Vacancy lead landed in 'TG Вакансии', NOT in SoftUGC — База Все2!
    vac_rec = conn.execute(
        """
        SELECT cr.step1_text, c.name AS camp_name
        FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE cr.username = 'hr_reels_job'
        """
    ).fetchone()
    assert vac_rec is not None
    assert vac_rec["camp_name"] == "TG Вакансии"
    assert vac_rec["step1_text"] == "Привет! Пишу по вакансии рилсмейкера."

    # SoftUGC — База Все2 stayed paused with leadgen_enabled=0 even after get_leadgen_config / sync_leadgen_campaigns
    cfg = get_leadgen_config(conn)
    assert cfg["b2b"]["target_campaign_tg"].startswith("SoftUGC — База Все2")
    sync_leadgen_campaigns(conn, cfg)
    vse2_camp_row = conn.execute(
        "SELECT status, leadgen_enabled FROM campaigns WHERE id = ?", (camp_vse2["id"],)
    ).fetchone()
    assert vse2_camp_row["status"] == "paused"
    assert int(vse2_camp_row["leadgen_enabled"]) == 0
    conn.close()

    # B) crm_mcp_push direct DB mode also routes default 'Leadgen Autopilot' to SoftUGC — База Все2 + claude-sonnet-5
    push_items = [
        {
            "username": "directpush_saas",
            "name": "FlowDesk",
            "company": "FlowDesk",
            "category": "SaaS",
            "their_angle": "AI support desk for startups",
            "source": "tg_ad_spy",
        }
    ]
    crm_mcp_push._apply_sonnet_batch_to_records(push_items)
    push_res = crm_mcp_push.push_leads_via_mcp(
        push_items,
        campaign_name="Leadgen Autopilot",
        db_path=str(db_path),
    )
    assert push_res["campaign_id"] == camp_vse2["id"]
    assert "Custom Sonnet pain paragraph for FlowDesk" in push_items[0]["step1_text"]

    # C) tg_enrich_leads and tg_curate also invoke claude-sonnet-5 and populate followup_1 / followup_2
    enrich_rows = [{"username": "enrich_ai", "project": "SynthVoice", "category": "AI", "what_they_do": "Voice AI SaaS"}]
    tg_enrich_leads.apply_vlad_sonnet_or_fallback(enrich_rows)
    assert "Custom Sonnet pain paragraph for SynthVoice" in enrich_rows[0]["first_touch"]
    assert enrich_rows[0]["followup_2"] == VLAD_STEP3_TEXT

    curate_rows = [{"username": "curate_ai", "project": "ClipRadar", "category": "SaaS", "what_they_do": "Analytics tool"}]
    curated = tg_curate.curate_rows(curate_rows)
    assert "Custom Sonnet pain paragraph for ClipRadar" in curated[0]["first_touch"]
    assert curated[0]["followup_2"] == VLAD_STEP3_TEXT

    assert len(sonnet_calls) >= 4
    assert all(c["model"] == "claude-sonnet-5" for c in sonnet_calls)


def test_qa_ring_campaign_self_send_guard_and_2h_delays(tmp_path, monkeypatch):
    conn = connect(tmp_path / "qa_ring.db")
    init_db(conn)
    now_holder = {"val": "2026-11-11T12:00:00"}
    import sys
    import app.outreach as outreach_mod

    monkeypatch.setattr(outreach_mod, "utc_now", lambda: now_holder["val"])
    monkeypatch.setattr(sys.modules[__name__], "utc_now", lambda: now_holder["val"])

    ring_accounts = [
        (21, "tim_cros", 5680938518),
        (30, "subscale", 6181048768),
        (27, "vladwilhelm", 978069541),
        (31, "make1tlo", 6805655381),
    ]
    for aid, uname, tid in ring_accounts:
        conn.execute(
            """
            INSERT INTO accounts (
                id, phone, session_path, status, telegram_user_id, telegram_username,
                telegram_name, created_at, health, updated_at, daily_limit, sent_today, provider
            ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, 'active', ?, 20, 0, 'telegram')
            """,
            (aid, f"+1000000{aid}", f"data/sessions/acc{aid}.session", tid, uname, uname, now_holder["val"], now_holder["val"]),
        )

    # Seed old completed row for make1tlo in B2B campaign #5 — QA campaign must NOT be blocked by it
    b2b_camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[21, 30, 27, 31],
        status="paused",
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name, company, step1_text,
            current_step, status, created_at, updated_at
        ) VALUES (?, 6805655381, 'make1tlo', 'Sergey', 'SoftUGC', 'old pitch', 3, 'completed', ?, ?)
        """,
        (b2b_camp["id"], now_holder["val"], now_holder["val"]),
    )

    qa_camp = create_campaign(
        conn,
        name="[QA ТЕСТ] SoftUGC — Свои аккаунты (интервал 2ч)",
        account_ids=[21, 30, 27, 31],
        daily_limit=50,
        status="running",
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 2, "template_text": VLAD_STEP2_TEMPLATE},
            {
                "step_order": 3,
                "delay_hours": 2,
                "template_text": VLAD_STEP3_TEXT,
                "image_url": "/media/outreach/4fa85f083f9e2911.jpg",
            },
        ],
    )

    # Deliberately set assigned_account_id=21 on recipient tim_cros (id=21) to prove self-send guard rotates to non-self!
    for idx, (aid, uname, tid) in enumerate(ring_accounts):
        sender_id = 21 if aid == 21 else ring_accounts[(idx - 1) % len(ring_accounts)][0]
        pitch = f"Hey,\n\nSaw {uname} Studio.\n\n{SOFTUGC_PITCH_TEXT}\n\n{REVSHARE_OFFER_TEXT}"
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, assigned_account_id, telegram_user_id, username, name, company,
                category, step1_text, current_step, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'SaaS', ?, 0, 'pending', ?, ?)
            """,
            (qa_camp["id"], sender_id, tid, uname, uname, f"{uname} Studio", pitch, now_holder["val"], now_holder["val"]),
        )
    conn.commit()

    def _record_live(entry: dict) -> None:
        pid = entry["peer_id"]
        if isinstance(pid, int):
            gw.live_messages_by_session_peer.setdefault((entry["session_path"], pid), []).append(
                {
                    "telegram_message_id": len(gw.sent) + 100,
                    "direction": "out",
                    "text": entry["text"],
                    "sent_at": now_holder["val"],
                }
            )

    gw = RecordingGateway(on_after_send=_record_live)
    # T+0h: Step 1 sends to all 4, and tim_cros NEVER receives from itself (acc21)!
    res1 = asyncio.run(run_campaign_batch(conn, qa_camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res1["sent"] == 4
    for s in gw.sent:
        peer_val = s.get("peer_id") or s.get("peer")
        if peer_val == 5680938518 or peer_val == "@tim_cros":
            assert s.get("session_path", s.get("session")) != "data/sessions/acc21.session"

    pinned_by_uname = {
        r["username"]: int(r["sent_account_id"])
        for r in conn.execute(
            "SELECT username, sent_account_id FROM campaign_recipients WHERE campaign_id = ?",
            (qa_camp["id"],),
        ).fetchall()
    }
    assert pinned_by_uname["tim_cros"] != 21

    # T+1h30m (< 2 hours): 0 follow-ups sent!
    now_holder["val"] = "2026-11-11T13:30:00"
    prev_len = len(gw.sent)
    res_early = asyncio.run(run_campaign_batch(conn, qa_camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res_early["sent"] == 0
    assert len(gw.sent) == prev_len

    # T+2h05m: Step 2 sends strictly from each recipient's pinned sent_account_id!
    now_holder["val"] = "2026-11-11T14:05:00"
    prev_len = len(gw.sent)
    res2 = asyncio.run(run_campaign_batch(conn, qa_camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res2["sent"] == 4
    for s in gw.sent[prev_len:]:
        assert "500K-2M reach" in s["text"]

    # T+4h10m: Step 3 sends Kermit + 'You’re doing me dirty mate... :(((' strictly from pinned sent_account_id!
    now_holder["val"] = "2026-11-11T16:10:00"
    prev_len = len(gw.sent)
    res3 = asyncio.run(run_campaign_batch(conn, qa_camp["id"], gateway=gw, max_send=10, respect_delays=True))
    assert res3["sent"] == 4
    for s in gw.sent[prev_len:]:
        assert s["text"] == VLAD_STEP3_TEXT
        assert s["image_path"] == "/media/outreach/4fa85f083f9e2911.jpg"

    # B2B campaign #5 remained paused with 0 sends!
    c5_state = conn.execute("SELECT status FROM campaigns WHERE id = ?", (b2b_camp["id"],)).fetchone()
    assert c5_state["status"] == "paused"
