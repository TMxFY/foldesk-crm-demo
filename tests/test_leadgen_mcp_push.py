import json
from pathlib import Path
import sys
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)

from app.db import connect, init_db
from app.outreach import get_campaign
from leadgen.crm_mcp_push import (
    build_lead_payload_from_dict,
    build_lead_payload_from_sheet_row,
    load_leads_from_jsonl,
    pack_tab_leads_for_mcp,
    push_leads_via_mcp,
)
from leadgen.outreach_copy import micro_ping_first_touch


@pytest.fixture()
def db_conn(tmp_path):
    conn = connect(tmp_path / "test_leadgen_push.db")
    init_db(conn)
    return conn


def test_micro_ping_first_touch_with_personal_name_and_filters():
    # 1. Valid English first name + company
    ping_with_name = micro_ping_first_touch("Alex Rivera", "ClipForge", category="SaaS")
    assert ping_with_name.startswith("Hey Alex, saw ClipForge —")
    assert "organic IG/TikTok reels" in ping_with_name
    assert len(ping_with_name) <= 120
    assert "\n" not in ping_with_name
    assert "http" not in ping_with_name.lower()
    assert "183.5" not in ping_with_name

    # 2. Empty name -> clean "Hey, saw <Company> — ..."
    ping_no_name = micro_ping_first_touch("", "ClipForge")
    assert ping_no_name.startswith("Hey, saw ClipForge —")
    assert len(ping_no_name) <= 120

    # 3. Brand/role words or placeholders in `name` must NOT be treated as a personal first name
    for non_person in (
        "[Founder's Name]",
        "[Name]",
        "Hi there",
        "AlphaLabs Protocol",
        "ClipForge",
        "@alex_founder",
        "CEO & Founder",
        "U+0048",
    ):
        msg = micro_ping_first_touch(non_person, "ClipForge", site="https://clipforge.ai")
        assert msg.startswith("Hey, saw ClipForge —"), f"Failed to filter non-person {non_person!r}: {msg}"
        assert "[" not in msg and "]" not in msg
        assert len(msg) <= 120


def test_build_lead_payload_micro_ping_vs_full_mode():
    # 15-col sheet row: A..O
    sheet_row = [
        "alex_founder",                       # A: TG
        "High",                               # B: Приоритет
        "Good fit",                           # C: Статус/Фит
        "SaaS / AI app",                      # D: Категория
        "7. ScreenStudio (YC W26)",           # E: Название
        "https://screenstudio.com",           # F: Сайт
        "https://t.me/alex_founder",          # G: Контакт
        "AI screen recorder with auto-zoom for macOS creators",  # H: Их угол
        "Направление 2 (AI UGC) + 3 (клиппинг).",                # I: Наш угол
        "",                                   # J: Хук
        (
            "Hey,\n\nScaling reach for macOS creators is expensive.\n\n"
            "We built SoftUGC as a plug-and-play organic distribution arm: "
            "We drove 183.5M views in 90 days with zero live shoots, at 13-40x lower cost.\n\n"
            "Want me to show how this could work for ScreenStudio?"
        ),                                    # K: Первое касание
        "",                                   # L: Статус
        "https://x.com/alex_founder",         # M: Founder X
        "",                                   # N: Founder PH
        "https://linkedin.com/in/alex",       # O: Founder LinkedIn
    ]

    # Default mode: micro_ping
    payload_ping = build_lead_payload_from_sheet_row(sheet_row, touch_mode="micro_ping")
    assert payload_ping["username"] == "alex_founder"
    assert payload_ping["company"] == "ScreenStudio"
    assert len(payload_ping["step1_text"]) <= 120
    assert payload_ping["step1_text"].startswith("Hey, saw ScreenStudio —")
    assert "183.5M" not in payload_ping["step1_text"]
    # Step 2 + hook + our_angle preserve the full 183.5M case offer
    assert "183.5M" in payload_ping["step2_text"]
    assert "13-40x" in payload_ping["step2_text"]
    assert "183.5M" in payload_ping["hook"]
    assert "183.5M" in payload_ping["our_angle"]
    # Step 3 is the niche proof / video angle follow-up
    assert payload_ping["step3_text"]
    assert "ScreenStudio" in payload_ping["step3_text"]
    assert "183.5M" not in payload_ping["step3_text"]

    # Full mode: step1_text has the 183.5M pitch
    payload_full = build_lead_payload_from_sheet_row(sheet_row, touch_mode="full")
    assert "183.5M" in payload_full["step1_text"]
    assert "ScreenStudio" in payload_full["step2_text"]
    assert payload_full["step3_text"]


def test_load_leads_from_jsonl_and_pack_tab_filters_ru(tmp_path):
    jsonl_file = tmp_path / "batch.jsonl"
    records = [
        {
            "founder_name": "Sarah Jenkins",
            "company": "PulseNote AI",
            "username": "@sarah_pulsenote",
            "site": "https://pulsenote.io",
            "category": "SaaS / AI app",
            "their_angle": "AI meeting notes and CRM sync for sales teams",
            "source": "yc_ph",
        },
        {
            "founder_name": "Иван",
            "company": "РуСофт",
            "username": "@ivan_rusoft",
            "site": "https://rusoft.ru",
            "category": "SaaS",
            "their_angle": "Сервис аналитики для российских селлеров",
            "source": "tg",
        },
    ]
    jsonl_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    loaded = load_leads_from_jsonl(jsonl_file, touch_mode="micro_ping")
    assert len(loaded) == 1
    lead = loaded[0]
    assert lead["username"] == "sarah_pulsenote"
    assert lead["company"] == "PulseNote AI"
    assert lead["step1_text"].startswith("Hey Sarah, saw PulseNote AI —")
    assert "183.5M" in lead["step2_text"]
    assert lead["step3_text"]


def test_push_leads_via_mcp_local_and_http_mock_and_fallback(db_conn):
    lead_item = build_lead_payload_from_dict(
        {
            "founder_name": "David Kim",
            "company": "2. HyperClip",
            "username": "https://t.me/david_hyperclip",
            "email": "david@hyperclip.ai",
            "site": "hyperclip.ai",
            "category": "SaaS / AI app",
            "their_angle": "Automated product demo videos for SaaS founders",
            "lead_source": "Product Hunt",
        },
        touch_mode="micro_ping",
    )

    # 1. Local in-process MCP execution
    res_local = push_leads_via_mcp([lead_item], conn=db_conn, campaign_name="Leadgen Autopilot")
    assert res_local["ok"] is True
    assert res_local["processed"] == 1
    assert res_local["created_leads"] == 1
    assert res_local["enrolled_in_campaign"] == 1
    cid = res_local["campaign_id"]

    camp = get_campaign(db_conn, cid)
    assert camp["name"] == "Leadgen Autopilot"
    assert len(camp["recipients"]) == 1
    rec = camp["recipients"][0]
    assert rec["username"] == "david_hyperclip"
    assert rec["company"] == "HyperClip"
    assert rec["step1_text"].startswith("Hey David, saw HyperClip —")
    assert "183.5M" in rec["step2_text"]
    assert rec["step3_text"]

    # 2. HTTP MCP transport mock
    captured_http = {}

    class _DummyResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": False,
                    "structuredContent": {
                        "ok": True,
                        "processed": 1,
                        "created_leads": 1,
                        "updated_leads": 0,
                        "enrolled_in_campaign": 1,
                        "skipped_duplicates": 0,
                        "campaign_id": 77,
                        "lead_ids": [101],
                    },
                },
            }

    class _DummySession:
        def post(self, url, headers=None, json=None, timeout=60):
            captured_http["url"] = url
            captured_http["headers"] = headers
            captured_http["json"] = json
            return _DummyResp()

    res_http = push_leads_via_mcp(
        [lead_item],
        mcp_url="https://crm.foldesk.io",
        api_key="mcp-secret-key",
        session=_DummySession(),
    )
    assert res_http["ok"] is True
    assert res_http["campaign_id"] == 77
    assert captured_http["url"] == "https://crm.foldesk.io/mcp"
    assert captured_http["headers"]["Authorization"] == "Bearer mcp-secret-key"
    assert captured_http["json"]["method"] == "tools/call"
    assert captured_http["json"]["params"]["name"] == "ingest_discovered_leads"
    assert captured_http["json"]["params"]["arguments"]["leads"][0]["username"] == "david_hyperclip"

    # 3. Fallback when remote server does not yet know `ingest_discovered_leads`
    calls_made = []

    class _FallbackSession:
        def post(self, url, headers=None, json=None, timeout=60):
            tool_name = json["params"]["name"]
            calls_made.append(tool_name)

            class _Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    if tool_name == "ingest_discovered_leads":
                        return {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "error": {"code": -32601, "message": "Неизвестный MCP-инструмент: ingest_discovered_leads"},
                        }
                    return {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "isError": False,
                            "structuredContent": {"action": "created", "lead": {"id": 555}},
                        },
                    }

            return _Resp()

    res_fb = push_leads_via_mcp(
        [lead_item],
        mcp_url="https://old-crm.foldesk.io",
        api_key="mcp-secret-key",
        session=_FallbackSession(),
    )
    assert res_fb["ok"] is True
    assert res_fb["fallback"] is True
    assert res_fb["created_leads"] == 1
    assert calls_made == ["ingest_discovered_leads", "create_or_update_lead"]
