import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db, utc_now
from app.mcp_server import handle_jsonrpc_message
from app.outreach import get_campaign, prewarm_recipient_stories, run_campaign_batch
from app.telegram_gateway import FakeGateway


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "test_leadgen_mcp.db")
    init_db(conn)
    return conn


def _call_mcp(conn, name: str, arguments: dict) -> dict:
    resp = handle_jsonrpc_message(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert "result" in resp, f"MCP error: {resp}"
    res = resp["result"]
    assert res["isError"] is False, f"Tool {name} failed: {res['content'][0]['text']}"
    return res["structuredContent"]


def test_mcp_ingest_discovered_leads_creates_leads_campaign_and_deduplicates(db_conn):
    # Check tool is registered in tools/list
    list_resp = handle_jsonrpc_message(
        db_conn,
        {"jsonrpc": "2.0", "id": 100, "method": "tools/list"},
    )
    tool_names = {t["name"] for t in list_resp["result"]["tools"]}
    assert "ingest_discovered_leads" in tool_names

    leads_payload = [
        {
            "username": "@founder_alpha",
            "name": "Alice Smith",
            "company": "AlphaFlow AI",
            "phone": "+15551002001",
            "email": "alice@alphaflow.ai",
            "website": "alphaflow.ai",
            "category": "AI SaaS",
            "their_angle": "Building workflow copilot for ops teams",
            "our_angle": "Short-form AI UGC videos to scale paid acquisition",
            "hook": "Saw AlphaFlow launch on Product Hunt!",
            "step1_text": "Hey Alice,\n\nSaw AlphaFlow AI on PH — congrats on the launch!",
            "step2_text": "Hey Alice, bumping this in case you missed it.",
            "step3_text": "Last follow-up from my side, Alice — happy to share 3 sample hooks.",
            "lead_source": "Product Hunt",
            "note": "Top 3 of the day",
        },
        {
            "username": "https://t.me/bob_beta",
            "name": "Bob Jones",
            "company": "2. BetaMetrics",
            "url": "https://betametrics.io",
            "niche": "Analytics",
            "their_angle": "Self-hosted product analytics",
            "our_angle": "Founder-led organic reels engine",
            "step1_text": "Hey Bob,\n\nLove what you're doing with BetaMetrics.",
            "step2_text": "Quick follow up on BetaMetrics.",
            "step3_text": "Closing the loop, Bob!",
            "lead_source": "YC Directory",
        },
    ]

    # 1. First call creates leads, auto-creates "Leadgen Autopilot" campaign, and enrolls both
    res1 = _call_mcp(
        db_conn,
        "ingest_discovered_leads",
        {"leads": leads_payload, "enroll_in_campaign": True},
    )
    assert res1["ok"] is True
    assert res1["processed"] == 2
    assert res1["created_leads"] == 2
    assert res1["updated_leads"] == 0
    assert res1["enrolled_in_campaign"] == 2
    assert res1["skipped_duplicates"] == 0
    assert res1["campaign_id"] is not None
    assert len(res1["lead_ids"]) == 2

    cid = res1["campaign_id"]
    camp = get_campaign(db_conn, cid)
    assert camp["name"] == "Leadgen Autopilot"
    assert camp["status"] == "draft"
    assert len(camp["steps"]) == 3
    assert [s["template_text"] for s in camp["steps"]] == [
        "{step1_text}",
        "{followup_1}",
        "{followup_2}",
    ]
    assert [s["delay_hours"] for s in camp["steps"]] == [0, 24, 72]

    recs = {r["username"]: r for r in camp["recipients"]}
    assert set(recs.keys()) == {"founder_alpha", "bob_beta"}
    r_alpha = recs["founder_alpha"]
    assert r_alpha["company"] == "AlphaFlow AI"
    assert r_alpha["category"] == "AI SaaS"
    assert r_alpha["website"] == "alphaflow.ai"
    assert r_alpha["their_angle"] == "Building workflow copilot for ops teams"
    assert r_alpha["our_angle"] == "Short-form AI UGC videos to scale paid acquisition"
    assert r_alpha["hook"] == "Saw AlphaFlow launch on Product Hunt!"
    assert "Saw AlphaFlow AI on PH" in r_alpha["step1_text"]
    assert "bumping this" in r_alpha["step2_text"]
    assert "Last follow-up" in r_alpha["step3_text"]

    # Verify lead fields in `leads` table
    lead_row = db_conn.execute(
        "SELECT * FROM leads WHERE username = 'founder_alpha'"
    ).fetchone()
    assert lead_row is not None
    assert lead_row["company"] == "AlphaFlow AI"
    assert lead_row["niche"] == "AI SaaS"
    assert lead_row["url"] == "https://alphaflow.ai"
    assert lead_row["lead_source"] == "Product Hunt"
    assert lead_row["email"] == "alice@alphaflow.ai"
    assert "Top 3 of the day" in (lead_row["note"] or "")
    assert "Building workflow copilot" in (lead_row["note"] or "")
    assert lead_row["offer_details"] == "Short-form AI UGC videos to scale paid acquisition"

    note_before = lead_row["note"]

    # 2. Second call with same leads + 1 new lead -> deduplicates existing in campaign and doesn't duplicate notes
    res2 = _call_mcp(
        db_conn,
        "ingest_discovered_leads",
        {
            "leads": leads_payload
            + [
                {
                    "username": "@charlie_gamma",
                    "name": "Charlie",
                    "company": "GammaDev",
                    "step1_text": "Hey Charlie!",
                }
            ],
            "enroll_in_campaign": True,
            "campaign_name": "leadgen autopilot",  # case-insensitive match
        },
    )
    assert res2["ok"] is True
    assert res2["campaign_id"] == cid
    assert res2["processed"] == 3
    assert res2["created_leads"] == 1
    assert res2["updated_leads"] == 2
    assert res2["enrolled_in_campaign"] == 1
    assert res2["skipped_duplicates"] == 2

    lead_row_after = db_conn.execute(
        "SELECT * FROM leads WHERE username = 'founder_alpha'"
    ).fetchone()
    assert lead_row_after["note"] == note_before


class _MockTelethonClient:
    def __init__(self, stories=None, fail_on=None):
        self.stories = stories if stories is not None else []
        self.fail_on = fail_on
        self.requests = []

    async def get_input_entity(self, peer):
        if self.fail_on == "get_input_entity":
            raise RuntimeError("Cannot resolve peer")
        return f"input_peer:{peer}"

    async def __call__(self, request):
        req_name = request.__class__.__name__
        self.requests.append(request)
        if self.fail_on == req_name or self.fail_on == "all":
            raise RuntimeError(f"Simulated Telethon error in {req_name}")
        if req_name == "GetPeerStoriesRequest":
            return SimpleNamespace(stories=SimpleNamespace(stories=self.stories))
        return SimpleNamespace(ok=True)


def test_prewarm_recipient_stories_with_active_stories():
    mock_stories = [SimpleNamespace(id=10), SimpleNamespace(id=25), SimpleNamespace(id=18)]
    client = _MockTelethonClient(stories=mock_stories)

    res = asyncio.run(prewarm_recipient_stories(client, "@alice_founder", reaction_emoticon="🔥"))
    assert res == {
        "prewarmed": True,
        "stories_count": 3,
        "story_id": 25,
    }
    req_names = [r.__class__.__name__ for r in client.requests]
    assert req_names == [
        "GetPeerStoriesRequest",
        "ReadStoriesRequest",
        "SendReactionRequest",
    ]
    read_req = client.requests[1]
    react_req = client.requests[2]
    assert read_req.max_id == 25
    assert react_req.story_id == 25
    assert getattr(react_req.reaction, "emoticon", None) == "🔥"


def test_prewarm_recipient_stories_empty_or_error_safe():
    # Empty stories
    client_empty = _MockTelethonClient(stories=[])
    res_empty = asyncio.run(prewarm_recipient_stories(client_empty, "@no_stories"))
    assert res_empty["prewarmed"] is False
    assert res_empty["stories_count"] == 0
    assert [r.__class__.__name__ for r in client_empty.requests] == ["GetPeerStoriesRequest"]

    # Error on GetPeerStoriesRequest
    client_err = _MockTelethonClient(stories=[SimpleNamespace(id=5)], fail_on="GetPeerStoriesRequest")
    res_err = asyncio.run(prewarm_recipient_stories(client_err, "@error_user"))
    assert res_err["prewarmed"] is False
    assert res_err["stories_count"] == 0
    assert "Simulated Telethon error" in res_err["error"]


def test_run_campaign_batch_calls_prewarm_before_step1_and_survives_prewarm_errors(db_conn):
    now = utc_now()
    cur = db_conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES ('+15550009999', 'data/sessions/real_acc.session', 'connected', 9001, 'outreach_acc', 'Outreach', ?, 'active', ?, 20, 0, 'telegram')
        """,
        (now, now),
    )
    acc_id = int(cur.lastrowid)
    db_conn.commit()

    ingested = _call_mcp(
        db_conn,
        "ingest_discovered_leads",
        {
            "leads": [
                {
                    "username": "@story_lead1",
                    "name": "Story Lead 1",
                    "company": "StoryCo",
                    "step1_text": "Hey Story Lead 1! First touch.",
                    "step2_text": "Followup 1 for Story Lead 1.",
                },
                {
                    "username": "@story_lead2",
                    "name": "Story Lead 2",
                    "company": "StoryCo 2",
                    "step1_text": "Hey Story Lead 2! First touch.",
                },
            ],
            "enroll_in_campaign": True,
        },
    )
    cid = ingested["campaign_id"]
    db_conn.execute(
        "UPDATE campaigns SET account_id = ?, account_ids_json = ?, status = 'running' WHERE id = ?",
        (acc_id, f"[{acc_id}]", cid),
    )
    db_conn.commit()

    gateway = FakeGateway(
        dialogs=[
            {"telegram_user_id": 7701, "username": "story_lead1", "name": "Story Lead 1"},
            {"telegram_user_id": 7702, "username": "story_lead2", "name": "Story Lead 2"},
        ]
    )
    # Attach mock telethon client to gateway
    mock_client = _MockTelethonClient(stories=[SimpleNamespace(id=42)])
    gateway.client = mock_client

    # Run step 1 for lead 1 (with active stories)
    batch1 = asyncio.run(run_campaign_batch(db_conn, cid, gateway=gateway, max_send=1))
    assert batch1["sent"] == 1
    assert len(gateway.sent) == 1
    assert [r.__class__.__name__ for r in mock_client.requests] == [
        "GetPeerStoriesRequest",
        "ReadStoriesRequest",
        "SendReactionRequest",
    ]

    # Now simulate Telethon error on stories for lead 2 — step 1 message must still be sent!
    failing_client = _MockTelethonClient(fail_on="all")
    gateway.client = failing_client
    batch2 = asyncio.run(
        run_campaign_batch(db_conn, cid, gateway=gateway, max_send=1, respect_delays=True)
    )
    assert batch2["sent"] == 1
    assert len(gateway.sent) == 2
    assert gateway.sent[1]["text"] == "Hey Story Lead 2! First touch."
    assert len(failing_client.requests) == 1
    assert failing_client.requests[0].__class__.__name__ == "GetPeerStoriesRequest"
