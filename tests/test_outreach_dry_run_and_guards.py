import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db, utc_now  # noqa: E402
from app.main import create_app  # noqa: E402
from app.outreach import (  # noqa: E402
    create_campaign,
    get_campaign,
    import_campaign_csv,
    preview_outreach_batch,
    run_campaign_batch,
    update_campaign,
)
from app.telegram_gateway import TelegramGateway  # noqa: E402


class _SpyGateway(TelegramGateway):
    def __init__(self):
        self.sent: list[dict] = []

    async def request_code(self, phone: str, session_path: str) -> str:
        return "hash"

    async def confirm_code(self, phone: str, code: str, phone_code_hash: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def confirm_password(self, password: str, session_path: str) -> dict:
        return {"status": "connected"}

    async def search_dialogs(self, session_path: str, query: str, limit: int = 30) -> list:
        q = (query or "").strip().lstrip("@").lower()
        return [{"telegram_user_id": abs(hash(q)) % 100000 + 20000, "username": q, "name": q.title()}]

    async def fetch_messages(self, session_path: str, peer_id: int, limit: int = 50) -> list:
        return []

    async def send_message(
        self,
        session_path: str,
        peer_id,
        text: str,
        reply_to=None,
        image_path: str | None = None,
    ) -> dict:
        self.sent.append({"peer_id": peer_id, "text": text, "image_path": image_path})
        return {
            "telegram_message_id": 8000 + len(self.sent),
            "text": text,
            "sent_at": utc_now(),
            "direction": "out",
        }


def _seed_connected_account(conn, account_id: int = 30) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO accounts (id, phone, session_path, status, health, daily_limit, sent_today, sent_date, created_at, updated_at)
        VALUES (?, '+79951210659', 'data/sessions/demo30.session', 'connected', 'active', 20, 0, ?, ?, ?)
        """,
        (account_id, now[:10], now, now),
    )
    conn.commit()


def _seed_fresh_leads_campaign(conn, count: int = 7) -> dict:
    camp = create_campaign(
        conn,
        name="SoftUGC Fresh Outreach",
        account_ids=[30],
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 24, "template_text": "Following up quickly on SoftUGC"},
        ],
    )
    lines = ["username,name,company,step1_text"]
    for i in range(1, count + 1):
        lines.append(
            f"@fresh_user_{i},Founder {i},Acme{i},Hey Founder {i} — SoftUGC scaled Acme{i} reels to 12M views. Open to test?"
        )
    import_campaign_csv(conn, camp["id"], "\n".join(lines) + "\n")
    return camp


def test_preview_outreach_batch_returns_3_to_5_fresh_samples_without_mutating_db(tmp_path):
    conn = connect(tmp_path / "preview_ok.db")
    init_db(conn)
    _seed_connected_account(conn)
    camp = _seed_fresh_leads_campaign(conn, count=7)

    preview = preview_outreach_batch(conn, camp["id"], sample_size=5)
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert preview["sent"] == 0
    assert preview["brand"] == "SoftUGC"
    assert preview["forbidden_pattern_count"] == 0
    assert preview["violations"] == []
    assert 3 <= preview["sample_count"] <= 5
    assert preview["sample_count"] == 5
    assert len(preview["previews"]) == 5

    for item in preview["previews"]:
        assert item["status"] == "pending"
        assert item["current_step"] == 0
        assert item["step_order"] == 1
        assert item["untouched"] is True
        assert item["lead_age_hours"] is not None and item["lead_age_hours"] >= 0
        assert "SoftUGC" in item["message_text"]
        assert "creatoros" not in item["message_text"].lower()
        assert item["forbidden_patterns"] == []

    # Verify DB untouched after dry-run
    after = get_campaign(conn, camp["id"])
    assert after["pending_count"] == 7
    assert after["in_progress_count"] == 0


def test_preview_outreach_batch_rejects_creatoros_in_template_name_or_rendered_output(tmp_path, monkeypatch):
    conn = connect(tmp_path / "preview_brand.db")
    init_db(conn)
    _seed_connected_account(conn)
    camp = _seed_fresh_leads_campaign(conn, count=4)

    # Inject CreatorOS directly into step template in DB (simulating legacy/broken template)
    conn.execute(
        "UPDATE campaign_steps SET template_text = 'Hey {name}, check CreatorOS growth engine' WHERE campaign_id = ? AND step_order = 1",
        (camp["id"],),
    )
    conn.commit()

    res_soft = preview_outreach_batch(conn, camp["id"], strict=False)
    assert res_soft["ok"] is False
    assert res_soft["forbidden_pattern_count"] >= 1
    assert any(v["code"] == "legacy_brand_creatoros" for v in res_soft["violations"])

    with pytest.raises(ValueError, match="CreatorOS"):
        preview_outreach_batch(conn, camp["id"], strict=True)

    # Also block runtime send if rendered output ever produces CreatorOS
    conn.execute(
        "UPDATE campaign_steps SET template_text = '{step1_text}' WHERE campaign_id = ? AND step_order = 1",
        (camp["id"],),
    )
    conn.commit()
    monkeypatch.setattr(
        "app.outreach._render_step_text",
        lambda step, step_idx, rec, campaign_name=None: "We built CreatorOS for your brand",
    )
    gw = _SpyGateway()
    batch_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=2))
    assert batch_res["sent"] == 0
    assert gw.sent == []


def test_step_ordering_and_followup_before_step1_blocked_everywhere(tmp_path):
    conn = connect(tmp_path / "step_order.db")
    init_db(conn)
    _seed_connected_account(conn)

    # 1. Reversed step_order rejected in create_campaign
    with pytest.raises(ValueError, match="Step 1"):
        create_campaign(
            conn,
            name="Bad Sequence",
            account_ids=[30],
            steps=[
                {"step_order": 2, "delay_hours": 24, "template_text": "Followup first"},
                {"step_order": 1, "delay_hours": 0, "template_text": "Step 1 second"},
            ],
        )

    # 2. Follow-up wording or token in Step 1 rejected in create_campaign & update_campaign
    with pytest.raises(ValueError, match="follow-up"):
        create_campaign(
            conn,
            name="Followup As Step1",
            account_ids=[30],
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{followup_1}"}],
        )

    with pytest.raises(ValueError, match="CreatorOS"):
        create_campaign(
            conn,
            name="Brand Leak Template",
            account_ids=[30],
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hi, CreatorOS here"}],
        )

    # 3. Corrupted DB campaign (Step 1 contains follow-up phrase) caught by dry-run AND blocked in live batch
    camp = _seed_fresh_leads_campaign(conn, count=3)
    conn.execute(
        "UPDATE campaign_steps SET template_text = 'Hey, regarding last message — quick nudge' WHERE campaign_id = ? AND step_order = 1",
        (camp["id"],),
    )
    conn.commit()

    bad_preview = preview_outreach_batch(conn, camp["id"], strict=False)
    assert bad_preview["ok"] is False
    assert any(v["code"] == "followup_before_step1" for v in bad_preview["violations"])
    with pytest.raises(ValueError, match="follow-up|Step 1"):
        preview_outreach_batch(conn, camp["id"], strict=True)

    gw = _SpyGateway()
    live_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=3))
    assert live_res["sent"] == 0
    assert gw.sent == []


def test_preview_and_batch_reject_legacy_1300_exhausted_leads(tmp_path):
    conn = connect(tmp_path / "legacy_1300.db")
    init_db(conn)
    _seed_connected_account(conn)
    camp = _seed_fresh_leads_campaign(conn, count=3)

    # Mark all 3 recipients as belonging to the legacy exhausted 1300+ pool
    conn.execute(
        "UPDATE campaign_recipients SET extra_json = ? WHERE campaign_id = ?",
        (json.dumps({"fine_status": "need_step2", "source_base": "legacy_1300"}), camp["id"]),
    )
    conn.commit()

    preview = preview_outreach_batch(conn, camp["id"], strict=False)
    assert preview["ok"] is False
    assert preview["fresh_untouched_count"] == 0
    assert preview["skipped_legacy_count"] == 3
    assert any(v["code"] in ("legacy_1300_base_only", "no_fresh_untouched_leads") for v in preview["violations"])

    with pytest.raises(ValueError):
        preview_outreach_batch(conn, camp["id"], strict=True)

    gw = _SpyGateway()
    live_res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, max_send=3))
    assert live_res["sent"] == 0
    assert gw.sent == []


def test_run_campaign_batch_and_http_api_support_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "0")
    conn = connect(tmp_path / "api_dry_run.db")
    init_db(conn)
    _seed_connected_account(conn)
    camp = _seed_fresh_leads_campaign(conn, count=4)

    gw = _SpyGateway()
    res = asyncio.run(run_campaign_batch(conn, camp["id"], gateway=gw, dry_run=True))
    assert res["dry_run"] is True
    assert res["ok"] is True
    assert res["sent"] == 0
    assert res["sample_count"] == 4
    assert gw.sent == []

    app = create_app(conn=conn)
    client = TestClient(app)
    resp = client.post(f"/api/campaigns/{camp['id']}/run", json={"dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["ok"] is True
    assert body["sent"] == 0
    assert body["sample_count"] == 4
    assert body["forbidden_pattern_count"] == 0
    assert get_campaign(conn, camp["id"])["pending_count"] == 4


def test_cli_dry_run_outputs_preview_and_fails_on_violations(tmp_path):
    db_path = tmp_path / "cli_dry_run.db"
    conn = connect(db_path)
    init_db(conn)
    _seed_connected_account(conn)
    camp = _seed_fresh_leads_campaign(conn, count=4)
    cid = camp["id"]
    conn.close()

    ok_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.outreach",
            "--db",
            str(db_path),
            "--campaign-id",
            str(cid),
            "--dry-run",
            "--sample-size",
            "3",
        ],
        cwd=_WORKTREE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0, ok_proc.stderr
    payload = json.loads(ok_proc.stdout)
    assert payload["dry_run"] is True
    assert payload["ok"] is True
    assert payload["sample_count"] == 3
    assert payload["forbidden_pattern_count"] == 0

    # Inject legacy CreatorOS brand -> CLI --dry-run must exit non-zero
    conn2 = connect(db_path)
    conn2.execute(
        "UPDATE campaign_steps SET template_text = 'CreatorOS outdated copy' WHERE campaign_id = ? AND step_order = 1",
        (cid,),
    )
    conn2.commit()
    conn2.close()

    bad_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.outreach",
            "--db",
            str(db_path),
            "--campaign-id",
            str(cid),
            "--dry-run",
        ],
        cwd=_WORKTREE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode != 0
