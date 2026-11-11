import asyncio
from pathlib import Path
import sys

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import apply_legacy_outreach_cleanup, connect, init_db, utc_now
from app.outreach import (
    create_campaign,
    get_campaign,
    import_campaign_csv,
    render_template,
    run_campaign_batch,
)
from app.telegram_gateway import GatewayError, TelegramGateway


class LiveHistoryGateway(TelegramGateway):
    def __init__(self, broken_sessions: set[str] | None = None, history_by_peer: dict[int, list] | None = None):
        self.sent: list[dict] = []
        self.broken_sessions = set(broken_sessions or set())
        self.history_by_peer = dict(history_by_peer or {})

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        if session_path in self.broken_sessions:
            raise GatewayError("The authorization key (session file) was used under two different IP addresses simultaneously")
        q = (query or "").strip().lstrip("@").lower()
        return [{"telegram_user_id": abs(hash(q)) % 100000 + 10000, "username": q, "name": q.title()}]

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        if session_path in self.broken_sessions:
            raise GatewayError("The authorization key (session file) was used under two different IP addresses simultaneously")
        return list(self.history_by_peer.get(int(peer_id), []))

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        image_path: str | None = None,
    ) -> dict:
        if session_path in self.broken_sessions:
            raise GatewayError("The authorization key (session file) was used under two different IP addresses simultaneously")
        self.sent.append(
            {
                "session_path": session_path,
                "peer_id": peer_id,
                "text": text,
                "image_path": image_path,
            }
        )
        return {
            "telegram_message_id": 7000 + len(self.sent),
            "text": text,
            "sent_at": utc_now(),
            "direction": "out",
        }


def _seed_accounts(conn):
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES
            (27, '+79088819595', 'data/sessions/27.session', 'connected', 'unauthorized', 20, 0, '2026-11-10', ?, ?),
            (30, '+79951210659', 'data/sessions/30.session', 'connected', 'active', 20, 0, '2026-11-10', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.commit()


def test_never_sends_followup_from_different_account_when_original_account_dead(tmp_path):
    conn = connect(tmp_path / "test_followup_guard.db")
    init_db(conn)
    _seed_accounts(conn)

    camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[27, 30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Hey, regarding last message, we could bring you 500K-2M reach"},
            {"step_order": 3, "delay_hours": 24, "template_text": "Step 3"},
        ],
    )
    import_campaign_csv(
        conn,
        camp["id"],
        "username,name,step1_text\n@ly3333,Jeremy | OpenEden,Step 1 pitch\n@fresh_lead,Fresh Founder,We run CreatorOS for your growth.\n",
    )
    recs = {r["username"]: r for r in get_campaign(conn, camp["id"])["recipients"]}
    # @ly3333 was at step 1 on dead account 27
    conn.execute(
        """
        UPDATE campaign_recipients
        SET current_step = 1,
            status = 'in_progress',
            assigned_account_id = 27,
            last_sent_at = '2026-09-10T10:00:00'
        WHERE id = ?
        """,
        (recs["ly3333"]["id"],),
    )
    conn.commit()

    gw = LiveHistoryGateway(broken_sessions={"data/sessions/27.session"})
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=5, respect_delays=True)
    )

    # Account 30 must ONLY send Step 1 to @fresh_lead (with CreatorOS scrubbed to SoftUGC)
    # and MUST NEVER send Step 2 follow-up to @ly3333 in an empty chat!
    assert res["sent"] == 1
    assert len(gw.sent) == 1
    assert "regarding last message" not in gw.sent[0]["text"]
    assert "CreatorOS" not in gw.sent[0]["text"]
    assert "SoftUGC" in gw.sent[0]["text"]

    after = {r["username"]: r for r in get_campaign(conn, camp["id"])["recipients"]}
    assert after["ly3333"]["current_step"] == 1
    assert after["ly3333"]["assigned_account_id"] == 27
    assert after["fresh_lead"]["current_step"] == 1
    assert after["fresh_lead"]["assigned_account_id"] == 30


def test_blocks_followup_if_live_dialog_on_assigned_account_has_zero_prior_outgoing(tmp_path):
    conn = connect(tmp_path / "test_empty_chat_guard.db")
    init_db(conn)
    _seed_accounts(conn)

    camp = create_campaign(
        conn,
        name="Empty Chat Guard",
        account_ids=[30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "Step 1"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Hey, regarding last message"},
        ],
    )
    import_campaign_csv(conn, camp["id"], "username,name\n@orphan_peer,Orphan Peer\n")
    rec_id = get_campaign(conn, camp["id"])["recipients"][0]["id"]
    conn.execute(
        """
        UPDATE campaign_recipients
        SET current_step = 1,
            status = 'in_progress',
            assigned_account_id = 30,
            telegram_user_id = 555777,
            last_sent_at = '2026-10-01T10:00:00'
        WHERE id = ?
        """,
        (rec_id,),
    )
    conn.commit()

    # Even if local SQLite messages had a stale synthetic outgoing row, live empty Telegram chat blocks follow-up
    conn.execute(
        """
        INSERT INTO dialogs (id, account_id, peer_type, peer_id, username, title, provider, is_group, unread_count, last_from_peer, muted, created_at, updated_at)
        VALUES (901, 30, 'user', 555777, 'orphan_peer', 'Orphan Peer', 'telegram', 0, 0, 0, 0, '2026-10-01T10:00:00', '2026-10-01T10:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
        VALUES (901, 1, 'out', 'Stale local-only row', '2026-10-01T10:00:00', '2026-10-01T10:00:00', '2026-10-01T10:00:00')
        """
    )
    conn.commit()

    # Live Telegram chat for 555777 is completely empty (no outgoing Step 1 ever sent on acc 30)
    gw = LiveHistoryGateway(history_by_peer={555777: []})
    res = asyncio.run(
        run_campaign_batch(conn, camp["id"], gateway=gw, max_send=2, respect_delays=True)
    )
    assert res["sent"] == 0
    assert len(gw.sent) == 0
    rec_after = get_campaign(conn, camp["id"])["recipients"][0]
    assert rec_after["status"] == "completed"


def test_legacy_cleanup_closes_old_1300_scrubs_creatoros_and_prioritizes_newest_pending(tmp_path):
    conn = connect(tmp_path / "test_migration.db")
    init_db(conn)
    _seed_accounts(conn)

    camp = create_campaign(
        conn,
        name="SoftUGC / CreatorOS — База Все2 (2 174 лида)",
        account_ids=[27, 30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Followup"},
        ],
    )
    import_campaign_csv(
        conn,
        camp["id"],
        (
            "username,name,step1_text\n"
            "@old_inprog,Old Sent,We solve that with CreatorOS.\n"
            "@mis_step2,Jeremy | OpenEden,Our platform CreatorOS helps.\n"
            "@old_failed,Old Failed Step2,CreatorOS pitch.\n"
            "@old_pending,Sept Pending,We built CreatorOS as a growth arm.\n"
            "@new_pending,Nov Pending,We built SoftUGC for your launch.\n"
        ),
    )
    recs = {r["username"]: r for r in get_campaign(conn, camp["id"])["recipients"]}
    # Simulate pre-existing legacy DB rows that still had CreatorOS in step1_text before sanitizer existed
    conn.execute(
        "UPDATE campaign_recipients SET step1_text = 'We built CreatorOS as a growth arm.', extra_json = '{\"fine_status\":\"need_step2\"}' WHERE id IN (?, ?, ?)",
        (recs["old_inprog"]["id"], recs["mis_step2"]["id"], recs["old_pending"]["id"]),
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'in_progress', current_step = 1, assigned_account_id = 30, last_sent_at = '2026-11-05T12:00:00' WHERE id = ?",
        (recs["old_inprog"]["id"],),
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'in_progress', current_step = 2, assigned_account_id = 30, last_sent_at = '2026-11-10T17:23:20' WHERE id = ?",
        (recs["mis_step2"]["id"],),
    )
    conn.execute(
        "UPDATE campaign_recipients SET status = 'failed', current_step = 2, assigned_account_id = 27, error_text = 'PeerUser cache miss', last_sent_at = '2026-09-19T10:13:45' WHERE id = ?",
        (recs["old_failed"]["id"],),
    )
    conn.commit()

    stats = apply_legacy_outreach_cleanup(conn)
    assert stats["closed_in_progress"] == 3
    assert stats["scrubbed_recipients"] >= 3
    assert stats["reordered_pending"] == 2

    updated = get_campaign(conn, camp["id"])
    assert "CreatorOS" not in updated["name"]
    by_user = {r["username"]: r for r in updated["recipients"]}
    assert by_user["old_inprog"]["status"] == "completed"
    assert by_user["mis_step2"]["status"] == "completed"
    assert by_user["old_failed"]["status"] == "completed"
    assert by_user["old_failed"]["error_text"] is None
    assert by_user["old_inprog"]["extra_fields"].get("fine_status") == "completed"
    assert "CreatorOS" not in (by_user["old_pending"]["step1_text"] or "")
    assert "SoftUGC" in (by_user["old_pending"]["step1_text"] or "")

    # Newest pending (@new_pending) must now have a smaller id than @old_pending so it sends FIRST
    assert by_user["new_pending"]["id"] < by_user["old_pending"]["id"]
    assert "CreatorOS" not in render_template("Try CreatorOS today", {"name": "Test"})


def test_step1_repair_v2_restores_truncated_ceiling_and_skips_llm_stubs(tmp_path):
    from app.db import apply_outreach_step1_repair_v2

    conn = connect(tmp_path / "test_v2_repair.db")
    init_db(conn)
    _seed_accounts(conn)

    camp = create_campaign(
        conn,
        name="SoftUGC — База Все2",
        account_ids=[30],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    now = "2026-11-11T08:00:00"
    conn.executemany(
        """
        INSERT INTO campaign_recipients (campaign_id, username, company, status, current_step, step1_text, last_sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                camp["id"],
                "vaibhavsharda",
                "fabric",
                "pending",
                0,
                "If your user growth has hit a ceiling, it's usually not the product - it's the cost of reach. We built an organic distribution system: in-house short-video production + a network of accounts. 183.5M views in 90 days on our last project, zero live shoots. Saw fabric. Want to see how it's set up for a",
                None,
                now,
                now,
            ),
            (
                camp["id"],
                "gd_on",
                "Steno",
                "pending",
                0,
                "Share your email.\n\nActually, here's the rewrite based on what you gave me:\n\nHey,\n\nWe run SoftUGC for Steno. Interested?",
                None,
                now,
                now,
            ),
            (
                camp["id"],
                "brandon35",
                "4. Cue",
                "pending",
                0,
                "Hey Brandon, we run SoftUGC. Want me to show how",
                None,
                now,
                now,
            ),
            (
                camp["id"],
                "hukid",
                "2. Fina Business",
                "pending",
                0,
                "The message to rewrite didn't come through. Your prompt ends at \"MESSAGE:\"",
                None,
                now,
                now,
            ),
            (
                camp["id"],
                "davidhwang",
                "Grocalo",
                "pending",
                0,
                "YC S26",
                None,
                now,
                now,
            ),
            (
                camp["id"],
                "blockblanc",
                "CryptoCanal",
                "in_progress",
                1,
                "Old CreatorOS step1",
                "2026-11-10T17:21:13",
                now,
                now,
            ),
        ],
    )
    conn.commit()

    stats = apply_outreach_step1_repair_v2(conn)
    assert stats == {"closed_yesterday": 1, "repaired": 3, "skipped_broken": 2}

    by_u = {r["username"]: r for r in get_campaign(conn, camp["id"])["recipients"]}
    assert "If your user growth has hit a ceiling" not in by_u["vaibhavsharda"]["step1_text"]
    assert "saw fabric." in by_u["vaibhavsharda"]["step1_text"].lower()
    assert "SoftUGC" in by_u["vaibhavsharda"]["step1_text"]
    assert by_u["gd_on"]["step1_text"] == "Hey,\n\nWe run SoftUGC for Steno. Interested?"
    assert "saw cue." in by_u["brandon35"]["step1_text"].lower()
    assert "SoftUGC" in by_u["brandon35"]["step1_text"]
    assert not by_u["brandon35"]["step1_text"].endswith("Want me to show how")
    assert by_u["hukid"]["status"] == "completed"
    assert by_u["davidhwang"]["status"] == "completed"
    assert by_u["blockblanc"]["status"] == "completed"


def test_purge_offline_non_icp_v3_deletes_pools_playgrounds_and_keeps_saas(tmp_path):
    from app.db import apply_purge_offline_non_icp_v3

    conn = connect(tmp_path / "test_v3_purge.db")
    init_db(conn)
    _seed_accounts(conn)

    camp = create_campaign(
        conn,
        name="SoftUGC — База Все2 (4 лида)",
        account_ids=[30],
        steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
    )
    now = "2026-11-11T09:00:00"
    conn.executemany(
        """
        INSERT INTO campaign_recipients (campaign_id, username, name, company, website, their_angle, status, current_step, step1_text, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 'Test pitch.', ?, ?)
        """,
        [
            (camp["id"], "loba4eva08", "Simple Pools", "simple-pools", "simple-pools.ru", "Cтроительство бассейнов и spa помещений", now, now),
            (camp["id"], "fkoshelev", "Федор", "silver-town", "silver-town.ru", "Детские уличные игровые комплексы", now, now),
            (camp["id"], "raketa_in_spaces", "Spaces", "spaces", "spaces.expert", "ВЕСM МАРКЕТИНГ - В БАН Сооснователь АБ SPACES", now, now),
            (camp["id"], "mshpakov", "Михаил Шпаков", "Statuser", "", "Руководитель разработки Timeweb Cloud. Основатель Statuser", now, now),
            (camp["id"], "anna23vm", "Анна", "MiniApp", "", "Архитектор Mini App и чат-ботов", now, now),
        ],
    )
    conn.commit()

    res = apply_purge_offline_non_icp_v3(conn)
    assert res["deleted"] == 3
    remaining = [r["username"] for r in get_campaign(conn, camp["id"])["recipients"]]
    assert remaining == ["mshpakov", "anna23vm"]
