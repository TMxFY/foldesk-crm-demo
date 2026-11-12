from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from app.airtable_sync import (
    AirtableRestClient,
    get_airtable_config,
    map_lead_to_airtable_fields,
    parse_airtable_contact,
    pull_from_airtable,
    push_calendar_date_to_airtable,
    push_lead_to_airtable,
    sync_airtable,
)
from app.db import as_dict, connect, init_db
from app.leads import bulk_leads_action, create_lead, get_lead, search_leads, update_lead


class FakeAirtableClient:
    """In-memory fake Airtable client for offline unit/integration tests."""

    def __init__(
        self,
        *,
        leads: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        projects: list[dict[str, Any]] | None = None,
    ) -> None:
        self.table_name = "Leads"
        self.tables: dict[str, list[dict[str, Any]]] = {
            "Leads": [dict(r, fields=dict(r.get("fields") or {})) for r in (leads or [])],
            "Tasks": [dict(r, fields=dict(r.get("fields") or {})) for r in (tasks or [])],
            "Projects": [dict(r, fields=dict(r.get("fields") or {})) for r in (projects or [])],
        }
        self.calls: list[dict[str, Any]] = []
        self._seq = 100

    def list_records(
        self,
        table: str | None = None,
        *,
        filter_formula: str | None = None,
        max_records: int | None = None,
    ) -> list[dict[str, Any]]:
        t = table or self.table_name
        self.calls.append({"op": "list", "table": t, "filter_formula": filter_formula})
        rows = self.tables.get(t, [])
        return [dict(r, fields=dict(r.get("fields") or {})) for r in rows]

    get_records = list_records

    def create_record(
        self,
        table: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        self._seq += 1
        rec_id = f"recFake{self._seq}"
        rec = {"id": rec_id, "fields": dict(fields)}
        self.tables.setdefault(table, []).append(rec)
        self.calls.append(
            {
                "op": "create",
                "table": table,
                "id": rec_id,
                "fields": dict(fields),
                "typecast": typecast,
            }
        )
        return dict(rec, fields=dict(rec["fields"]))

    def update_record(
        self,
        table: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        typecast: bool = True,
    ) -> dict[str, Any]:
        rows = self.tables.setdefault(table, [])
        for r in rows:
            if r["id"] == record_id:
                r["fields"].update(fields)
                self.calls.append(
                    {
                        "op": "update",
                        "table": table,
                        "id": record_id,
                        "fields": dict(fields),
                        "typecast": typecast,
                    }
                )
                return dict(r, fields=dict(r["fields"]))
        rec = {"id": record_id, "fields": dict(fields)}
        rows.append(rec)
        self.calls.append(
            {
                "op": "update",
                "table": table,
                "id": record_id,
                "fields": dict(fields),
                "typecast": typecast,
            }
        )
        return dict(rec, fields=dict(rec["fields"]))


@pytest.fixture
def conn(tmp_path: Path):
    db = connect(tmp_path / "crm.db")
    init_db(db)
    yield db
    db.close()


def test_pull_all_seven_stages_and_fields_from_airtable(conn):
    """1. Pull из Airtable Leads со всеми 7 стадиями, полями SoftUGC Suite и парсингом @username."""
    sample_leads = [
        {
            "id": "recCoinStats01",
            "fields": {
                "Lead Name": "CoinStats",
                "Company": "CoinStats Inc",
                "Status": "In conversation",
                "Value": 4500,
                "Monthly Value": 3000,
                "Contact": "@narekgevorgyan",
                "Website": "https://coinstats.app",
                "Niche": "Crypto Portfolio",
                "Offer Type": ["Performance", "Per-video"],
                "Offer Details": "30 UGC videos/mo + performance bonus",
                "Next Step": "Send sample hooks",
                "Next Follow-up": "2026-11-10",
                "Last Contacted": "2026-11-04",
                "Lead Source": "Cold outreach",
                "Notes": "Interested in TikTok + Reels",
            },
        },
        {
            "id": "recDenaria02",
            "fields": {
                "Lead Name": "Denaria",
                "Company": "Denaria Labs",
                "Status": "Call scheduled",
                "Value": 6000,
                "Monthly Value": 6000,
                "Contact": "https://t.me/denaria_founder, +1 415 555 0199",
                "Website": "https://denaria.io",
                "Niche": "Fintech",
                "Offer Type": ["Retainer"],
                "Offer Details": "Monthly retainer",
                "Next Step": "Zoom demo",
                "Next Follow-up": "2026-11-08",
                "Last Contacted": "2026-11-03",
                "Lead Source": "Referral",
                "Notes": "Warm intro",
            },
        },
        {
            "id": "recAnimus03",
            "fields": {
                "Lead Name": "Animus",
                "Company": "Animus AI",
                "Status": "Offer sent",
                "Value": 2500,
                "Monthly Value": 2500,
                "Contact": "@animus_ceo",
                "Niche": "AI Companion",
                "Offer Type": ["Per-video"],
            },
        },
        {
            "id": "recStageNew04",
            "fields": {
                "Lead Name": "NovaApp",
                "Status": "New",
                "Contact": "@nova_lead",
                "Value": 1200,
            },
        },
        {
            "id": "recStageHold05",
            "fields": {
                "Lead Name": "PauseCorp",
                "Status": "On hold",
                "Contact": "@pause_ceo",
                "Value": 1800,
            },
        },
        {
            "id": "recStageWon06",
            "fields": {
                "Lead Name": "WinStudio",
                "Status": "Signed",
                "Contact": "@win_founder",
                "Value": 9000,
                "Monthly Value": 4500,
            },
        },
        {
            "id": "recStageLost07",
            "fields": {
                "Lead Name": "CancelCo",
                "Status": "Cancelled",
                "Contact": "@cancel_lead",
                "Value": 800,
            },
        },
    ]

    client = FakeAirtableClient(leads=sample_leads)
    res = pull_from_airtable(conn, client=client)

    assert res["ok"] is True
    assert res["pulled_leads"] == 7
    assert res["created_in_crm"] == 7
    assert res["updated_in_crm"] == 0
    assert [s["key"] for s in res["stages"]] == [
        "new",
        "talking",
        "call_scheduled",
        "waiting",
        "on_hold",
        "won",
        "lost",
    ]

    leads = {ld["name"]: ld for ld in search_leads(conn)}
    assert set(leads.keys()) == {
        "CoinStats",
        "Denaria",
        "Animus",
        "NovaApp",
        "PauseCorp",
        "WinStudio",
        "CancelCo",
    }

    cs = leads["CoinStats"]
    assert cs["airtable_record_id"] == "recCoinStats01"
    assert cs["status"] == "talking"
    assert cs["airtable_status"] == "In conversation"
    assert cs["company"] == "CoinStats Inc"
    assert cs["username"] == "narekgevorgyan"
    assert cs["amount"] == 4500
    assert cs["monthly_value"] == 3000
    assert cs["url"] == "https://coinstats.app"
    assert cs["niche"] == "Crypto Portfolio"
    assert cs["offer_type"] == ["Performance", "Per-video"]
    assert cs["offer_details"] == "30 UGC videos/mo + performance bonus"
    assert cs["next_step"] == "Send sample hooks"
    assert cs["next_follow_up"] == "2026-11-10"
    assert cs["last_contacted"] == "2026-11-04"
    assert cs["lead_source"] == "Cold outreach"
    assert cs["note"] == "Interested in TikTok + Reels"

    den = leads["Denaria"]
    assert den["status"] == "call_scheduled"
    assert den["airtable_status"] == "Call scheduled"
    assert den["username"] == "denaria_founder"
    assert den["phone"] == "+1 415 555 0199"

    assert leads["Animus"]["status"] == "waiting"
    assert leads["NovaApp"]["status"] == "new"
    assert leads["PauseCorp"]["status"] == "on_hold"
    assert leads["WinStudio"]["status"] == "won"
    assert leads["CancelCo"]["status"] == "lost"


def test_pull_deduplication_by_record_id_and_username(conn):
    """2. Дедупликация при повторном Pull (по airtable_record_id и по @username)."""
    # Existing local lead without airtable_record_id, matched by @username
    local_lead = create_lead(
        conn,
        name="Narek (TG)",
        username="@narekgevorgyan",
        status="new",
        amount=1000,
    )

    remote_records = [
        {
            "id": "recCoinStats01",
            "fields": {
                "Lead Name": "CoinStats",
                "Company": "CoinStats",
                "Status": "In conversation",
                "Value": 5000,
                "Contact": "@narekgevorgyan",
                "Next Follow-up": "2026-11-12",
            },
        },
        {
            "id": "recDenaria02",
            "fields": {
                "Lead Name": "Denaria",
                "Status": "Call scheduled",
                "Value": 3500,
                "Contact": "@denaria_ceo",
            },
        },
    ]
    client = FakeAirtableClient(leads=remote_records)

    first = pull_from_airtable(conn, client=client)
    assert first["created_in_crm"] == 1  # Denaria created
    assert first["updated_in_crm"] == 1  # CoinStats matched existing @narekgevorgyan

    all_leads = search_leads(conn)
    assert len(all_leads) == 2

    updated_local = get_lead(conn, local_lead["id"])
    assert updated_local["airtable_record_id"] == "recCoinStats01"
    assert updated_local["status"] == "talking"
    assert updated_local["amount"] == 5000
    assert updated_local["next_follow_up"] == "2026-11-12"

    # Second pull with updated remote status on recCoinStats01 -> updates by airtable_record_id, no duplicates
    client.tables["Leads"][0]["fields"]["Status"] = "Signed"
    client.tables["Leads"][0]["fields"]["Value"] = 7500

    second = pull_from_airtable(conn, client=client)
    assert second["created_in_crm"] == 0
    assert second["updated_in_crm"] == 2
    assert len(search_leads(conn)) == 2

    after_second = get_lead(conn, local_lead["id"])
    assert after_second["status"] == "won"
    assert after_second["airtable_status"] == "Signed"
    assert after_second["amount"] == 7500


def test_push_new_and_updated_lead_to_airtable(conn):
    """3. Push нового и изменённого лида из CRM в Airtable (typecast=True, сохранение airtable_record_id, маппинг стадий)."""
    client = FakeAirtableClient()

    lead = create_lead(
        conn,
        name="Hyperion App",
        company="Hyperion Labs",
        username="hyperion_founder",
        phone="+44 7700 900077",
        status="call_scheduled",
        amount=4200,
        monthly_value=2100,
        niche="AI Productivity",
        offer_type=["Performance", "Retainer"],
        offer_details="20 videos + ad spend share",
        next_step="Prepare media kit",
        next_follow_up="2026-11-15",
        last_contacted="2026-11-05",
        url="https://hyperion.ai",
        note="Met on Telegram",
    )

    push_res = push_lead_to_airtable(conn, lead["id"], client=client)
    assert push_res["ok"] is True
    assert push_res["action"] == "created"
    assert push_res["airtable_record_id"].startswith("recFake")

    create_calls = [c for c in client.calls if c["op"] == "create" and c["table"] == "Leads"]
    assert len(create_calls) == 1
    assert create_calls[0]["typecast"] is True
    sent_fields = create_calls[0]["fields"]
    assert sent_fields["Lead Name"] == "Hyperion App"
    assert sent_fields["Company"] == "Hyperion Labs"
    assert sent_fields["Status"] == "Call scheduled"
    assert sent_fields["Value"] == 4200
    assert sent_fields["Monthly Value"] == 2100
    assert sent_fields["Contact"] == "@hyperion_founder, +44 7700 900077"
    assert sent_fields["Offer Type"] == ["Performance", "Retainer"]
    assert sent_fields["Lead Source"] == "Cold outreach"
    assert sent_fields["Next Follow-up"] == "2026-11-15"

    stored = get_lead(conn, lead["id"])
    assert stored["airtable_record_id"] == push_res["airtable_record_id"]
    assert stored["airtable_status"] == "Call scheduled"
    assert stored["airtable_synced_at"] is not None

    # Update lead in CRM and run sync_airtable(direction="both")
    update_lead(conn, lead["id"], status="won", amount=8400, next_step="Onboarding")
    sync_res = sync_airtable(conn, direction="both", client=client)
    assert sync_res["pushed_to_airtable"] == 1

    update_calls = [c for c in client.calls if c["op"] == "update" and c["table"] == "Leads"]
    assert len(update_calls) == 1
    assert update_calls[0]["id"] == push_res["airtable_record_id"]
    assert update_calls[0]["typecast"] is True
    assert update_calls[0]["fields"]["Status"] == "Signed"
    assert update_calls[0]["fields"]["Value"] == 8400
    assert update_calls[0]["fields"]["Next Step"] == "Onboarding"

    cfg = get_airtable_config(conn)
    assert cfg["last_sync_at"] == sync_res["synced_at"]
    assert cfg["last_sync_summary"]["pushed_to_airtable"] == 1


def test_tasks_and_projects_sync_and_calendar_date_push(conn):
    """4. Pull задач Tasks и проектов Projects в calendar_events и обратный пуш даты через push_calendar_date_to_airtable."""
    client = FakeAirtableClient(
        leads=[
            {
                "id": "recLeadCoinStats",
                "fields": {
                    "Lead Name": "CoinStats",
                    "Status": "In conversation",
                    "Contact": "@narekgevorgyan",
                    "Next Follow-up": "2026-11-10",
                },
            }
        ],
        tasks=[
            {
                "id": "recTask01",
                "fields": {
                    "Task Name": "Отправить КП для CoinStats",
                    "Due Date": "2026-11-11",
                    "Status": "In Progress",
                    "Description": "Подготовить расчёт по 30 роликам",
                    "Related Lead": ["recLeadCoinStats"],
                },
            },
            {
                "id": "recTask02",
                "fields": {
                    "Task Name": "Закрытая задача",
                    "Due Date": "2026-11-01",
                    "Status": "Completed",
                },
            },
        ],
        projects=[
            {
                "id": "recProj01",
                "fields": {
                    "Project Name": "SoftUGC Batch #4 — CoinStats",
                    "Start Date": "2026-11-01",
                    "End Date": "2026-11-25",
                    "Status": "In Progress",
                    "Project Description": "Пакет из 30 рилсов",
                },
            }
        ],
    )

    res = pull_from_airtable(conn, client=client)
    assert res["synced_tasks"] == 2
    assert res["synced_projects"] == 1

    lead_row = conn.execute(
        "SELECT id FROM leads WHERE airtable_record_id = 'recLeadCoinStats'"
    ).fetchone()
    assert lead_row is not None
    lead_id = int(lead_row["id"])

    cal_rows = [
        as_dict(r)
        for r in conn.execute("SELECT * FROM calendar_events ORDER BY id ASC").fetchall()
    ]
    assert len(cal_rows) == 3

    task1 = next(r for r in cal_rows if r["external_id"] == "recTask01")
    assert task1["source"] == "airtable_task"
    assert task1["event_type"] == "task_deadline"
    assert task1["start_at"] == "2026-11-11"
    assert task1["all_day"] == 1
    assert task1["lead_id"] == lead_id
    assert task1["status"] == "scheduled"

    task2 = next(r for r in cal_rows if r["external_id"] == "recTask02")
    assert task2["status"] == "completed"

    proj1 = next(r for r in cal_rows if r["external_id"] == "recProj01")
    assert proj1["source"] == "airtable_project"
    assert proj1["event_type"] == "project_deadline"
    assert proj1["start_at"] == "2026-11-25"
    assert proj1["all_day"] == 1

    # Push calendar dates back to Airtable for lead_followup, airtable_task, airtable_project
    r_fu = push_calendar_date_to_airtable(
        conn,
        item_kind="lead_followup",
        item_id=lead_id,
        new_date="2026-11-18T14:00:00",
        client=client,
    )
    assert r_fu["ok"] is True
    assert r_fu["pushed"] is True
    assert r_fu["new_date"] == "2026-11-18"
    assert get_lead(conn, lead_id)["next_follow_up"] == "2026-11-18"
    assert client.tables["Leads"][0]["fields"]["Next Follow-up"] == "2026-11-18"

    r_task = push_calendar_date_to_airtable(
        conn,
        item_kind="airtable_task",
        item_id=task1["id"],
        new_date="2026-11-19",
        client=client,
    )
    assert r_task["ok"] is True
    assert r_task["pushed"] is True
    assert client.tables["Tasks"][0]["fields"]["Due Date"] == "2026-11-19"

    r_proj = push_calendar_date_to_airtable(
        conn,
        item_kind="airtable_project",
        item_id=proj1["id"],
        new_date="2026-11-30",
        client=client,
    )
    assert r_proj["ok"] is True
    assert r_proj["pushed"] is True
    assert client.tables["Projects"][0]["fields"]["End Date"] == "2026-11-30"


def test_crm_delete_never_deletes_in_airtable(conn):
    """5. Защита от удаления: удаление лида в CRM не вызывает DELETE в Airtable."""
    client = FakeAirtableClient(
        leads=[
            {
                "id": "recKeepInAirtable",
                "fields": {
                    "Lead Name": "Protected Lead",
                    "Status": "New",
                    "Contact": "@protected_user",
                },
            }
        ]
    )
    pull_from_airtable(conn, client=client)
    leads = search_leads(conn)
    assert len(leads) == 1

    calls_before = len(client.calls)
    del_res = bulk_leads_action(conn, [leads[0]["id"]], "delete")
    assert del_res["deleted"] == 1
    assert len(search_leads(conn)) == 0

    # No DELETE call was made to Airtable, and the record still exists in Airtable
    assert len(client.calls) == calls_before
    assert len(client.tables["Leads"]) == 1
    assert client.tables["Leads"][0]["id"] == "recKeepInAirtable"
    assert not hasattr(AirtableRestClient, "delete_record")


def test_rest_client_pagination_retry_and_readonly_filtering():
    """Проверка пагинации (offset), retry на 429/5xx и фильтрации read-only полей в AirtableRestClient."""
    requests_seen: list[urllib.request.Request] = []
    attempt_counter = {"post": 0}

    def fake_transport(req: urllib.request.Request, timeout: float = 30):
        requests_seen.append(req)
        url = req.full_url
        method = req.get_method()

        if method == "GET":
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "offset" not in qs:
                payload = {
                    "records": [{"id": "recPage1", "fields": {"Lead Name": "Alpha"}}],
                    "offset": "itrNextPageToken",
                }
            else:
                payload = {
                    "records": [{"id": "recPage2", "fields": {"Lead Name": "Beta"}}],
                }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        if method == "POST":
            attempt_counter["post"] += 1
            if attempt_counter["post"] == 1:
                raise urllib.error.HTTPError(
                    url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "0.01"},
                    io.BytesIO(b'{"error":"rate_limited"}'),
                )
            body = json.loads(req.data.decode("utf-8"))
            return io.BytesIO(
                json.dumps({"id": "recCreated", "fields": body["fields"], "typecast": body["typecast"]}).encode(
                    "utf-8"
                )
            )

        raise AssertionError(f"Unexpected method {method}")

    sleeps: list[float] = []
    rest = AirtableRestClient(
        api_key="patFakeTestToken",
        base_id="appFakeTestBase",
        transport=fake_transport,
        sleep=lambda s: sleeps.append(s),
    )

    records = rest.list_records("Leads")
    assert [r["id"] for r in records] == ["recPage1", "recPage2"]

    created = rest.create_record(
        "Leads",
        {
            "Lead Name": "Gamma",
            "Days Since Last Contacted": 5,
            "Lead Summary (AI)": "Should be stripped",
        },
        typecast=True,
    )
    assert created["id"] == "recCreated"
    assert created["typecast"] is True
    assert "Days Since Last Contacted" not in created["fields"]
    assert "Lead Summary (AI)" not in created["fields"]
    assert attempt_counter["post"] == 2
    assert len(sleeps) >= 1


def test_lead_notes_mark_dirty_and_push_to_airtable(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    conn = connect(tmp_path / "crm.db")
    init_db(conn)
    fake = FakeAirtableClient(
        leads=[
            {
                "id": "recNote1",
                "fields": {
                    "Lead Name": "Алекс Видео",
                    "Status": "In conversation",
                    "Contact": "@alex_vid",
                    "Notes": "Первый контакт",
                },
            }
        ]
    )
    pull_res = sync_airtable(conn, direction="pull", client=fake)
    assert pull_res["created_in_crm"] == 1
    lead = conn.execute("SELECT * FROM leads WHERE airtable_record_id = 'recNote1'").fetchone()
    lead_id = int(lead["id"])

    app = create_app(conn=conn, gateway=None)
    app.state.airtable_client = fake
    with TestClient(app) as client:
        resp = client.post(
            f"/api/leads/{lead_id}/notes",
            json={"body": "Ему нужны 3 видоса"},
        )
        assert resp.status_code == 200, resp.text

    sync_res = sync_airtable(conn, direction="both", client=fake)
    assert sync_res["pushed_to_airtable"] >= 1
    rec = next(r for r in fake.tables["Leads"] if r["id"] == "recNote1")
    notes_text = rec["fields"].get("Notes") or ""
    assert "Первый контакт" in notes_text
    assert "Ему нужны 3 видоса" in notes_text
