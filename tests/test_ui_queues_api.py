from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import (
    connect,
    init_db,
    record_enrichment_ledger_entry,
    utc_now,
    utc_today,
)
from app.main import create_app
from app.outreach import create_campaign
from app.telegram_gateway import FakeGateway


def _make_client(tmp_path: Path, monkeypatch) -> tuple[sqlite3.Connection, TestClient]:
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")
    monkeypatch.setenv("CLOUD_BACKUP_ENABLED", "0")
    monkeypatch.setenv("DISABLE_BOT_ALERTS", "1")
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(tmp_path / "leadgen_config.json"))
    conn = connect(tmp_path / "test_crm.db")
    init_db(conn)
    app = create_app(conn=conn, gateway=FakeGateway())
    return conn, TestClient(app)


def _insert_lead(
    conn: sqlite3.Connection,
    *,
    name: str,
    company: str = "",
    segment: str = "b2b",
    language: str = "en",
    exclude_from_airtable: int = 0,
    company_domain: str | None = None,
    pool_usd: int | None = None,
    manual_action_url: str | None = None,
    manual_draft_text: str | None = None,
    status: str = "new",
    last_contacted: str | None = None,
    created_at: str | None = None,
    account_id: int | None = None,
    telegram_user_id: int | None = None,
    username: str | None = None,
    provider: str = "telegram",
    lead_source: str | None = None,
    niche: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> int:
    ts = created_at or utc_now()
    cur = conn.execute(
        """
        INSERT INTO leads (
            account_id, telegram_user_id, username, name, company, segment, language,
            exclude_from_airtable, company_domain, pool_usd, manual_action_url,
            manual_draft_text, status, last_contacted, provider, lead_source, niche,
            phone, email, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            telegram_user_id,
            username,
            name,
            company,
            segment,
            language,
            exclude_from_airtable,
            company_domain,
            pool_usd,
            manual_action_url,
            manual_draft_text,
            status,
            last_contacted,
            provider,
            lead_source,
            niche,
            phone,
            email,
            ts,
            ts,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_leads_segment_filter_and_v2_fields(tmp_path: Path, monkeypatch) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        whale_id = _insert_lead(
            conn,
            name="Alex Whale",
            company="WhaleApp",
            segment="whale",
            language="en",
            company_domain="whaleapp.io",
            pool_usd=15000,
        )
        _insert_lead(
            conn,
            name="Ivan Vacancy",
            company="Studio RU",
            segment="vacancy",
            language="ru",
        )
        _insert_lead(
            conn,
            name="Reels Buyer",
            company="ReelsCo",
            segment="client_reels",
            language="ru",
        )
        ru_exp_id = _insert_lead(
            conn,
            name="Dr. Petrov",
            company="MedBlog",
            segment="ru_expert",
            language="ru",
            exclude_from_airtable=1,
        )
        social_id = _insert_lead(
            conn,
            name="Sam Social",
            company="BountyCo",
            segment="social_manual",
            language="en",
            pool_usd=8000,
            manual_action_url="https://x.com/samsocial",
            manual_draft_text="Hey Sam — quick 15s demo for BountyCo?",
        )

        with client:
            all_resp = client.get("/api/leads")
            assert all_resp.status_code == 200
            all_rows = all_resp.json()
            assert len(all_rows) == 5
            for row in all_rows:
                for key in (
                    "segment",
                    "language",
                    "exclude_from_airtable",
                    "pool_usd",
                    "manual_action_url",
                    "manual_draft_text",
                ):
                    assert key in row

            whale_rows = client.get("/api/leads?segment=whale").json()
            assert [r["id"] for r in whale_rows] == [whale_id]
            assert whale_rows[0]["pool_usd"] == 15000

            ru_rows = client.get("/api/leads?segment=ru_expert").json()
            assert [r["id"] for r in ru_rows] == [ru_exp_id]
            assert ru_rows[0]["exclude_from_airtable"] == 1
            assert ru_rows[0]["language"] == "ru"

            multi_rows = client.get("/api/leads?segment=vacancy,client_reels").json()
            assert {r["segment"] for r in multi_rows} == {"vacancy", "client_reels"}

            social_single = client.get(f"/api/leads/{social_id}").json()
            assert social_single["segment"] == "social_manual"
            assert social_single["manual_action_url"] == "https://x.com/samsocial"

            assert client.get("/api/leads?segment=all").json() == all_rows
            assert client.get("/api/leads?segment=ofm").json() == []
    finally:
        conn.close()


def test_inbox_and_unified_inbox_segment_filter_and_fields(tmp_path: Path, monkeypatch) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        now = utc_now()
        acc_id = conn.execute(
            """
            INSERT INTO accounts (phone, session_path, status, health, provider, daily_limit, created_at)
            VALUES ('+14155550101', 'sessions/test_inbox_seg', 'connected', 'active', 'telegram', 20, ?)
            """,
            (now,),
        ).lastrowid

        # Insert 1 old ru_expert dialog + 65 newer b2b dialogs + 1 newest whale dialog
        # so default top-60 cap would drop ru_expert if segment filtering happened after slicing.
        ru_lead_id = _insert_lead(
            conn,
            account_id=acc_id,
            telegram_user_id=880001,
            username="ru_expert_doc",
            name="Эксперт РФ",
            segment="ru_expert",
            language="ru",
            exclude_from_airtable=1,
            status="talking",
            created_at="2026-11-01T10:00:00",
        )
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, provider, created_at, updated_at
            ) VALUES (?, 'user', 880001, 'ru_expert_doc', 'Эксперт РФ', 0, 1, '2026-11-01T10:05:00', 'Интересно', 1, ?, 'telegram', '2026-11-01T10:00:00', '2026-11-01T10:05:00')
            """,
            (acc_id, ru_lead_id),
        )

        for idx in range(65):
            uid = 890000 + idx
            lid = _insert_lead(
                conn,
                account_id=acc_id,
                telegram_user_id=uid,
                username=f"b2b_user_{idx}",
                name=f"B2B {idx}",
                segment="b2b",
                status="talking",
                created_at="2026-11-15T10:00:00",
            )
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, is_group,
                    unread_count, last_message_at, last_message_preview, last_from_peer,
                    lead_id, provider, created_at, updated_at
                ) VALUES (?, 'user', ?, ?, ?, 0, 0, ?, 'Ping', 0, ?, 'telegram', ?, ?)
                """,
                (acc_id, uid, f"b2b_user_{idx}", f"B2B {idx}", f"2026-11-15T10:{idx:02d}:00", lid, now, now),
            )

        whale_lead_id = _insert_lead(
            conn,
            account_id=acc_id,
            telegram_user_id=880002,
            username="whale_founder",
            name="Whale Founder",
            segment="whale",
            language="en",
            pool_usd=20000,
            status="talking",
            created_at="2026-11-22T12:00:00",
        )
        conn.execute(
            """
            INSERT INTO dialogs (
                account_id, peer_type, peer_id, username, title, is_group,
                unread_count, last_message_at, last_message_preview, last_from_peer,
                lead_id, provider, created_at, updated_at
            ) VALUES (?, 'user', 880002, 'whale_founder', 'Whale Founder', 0, 1, '2026-11-22T12:05:00', 'Send demo', 1, ?, 'telegram', '2026-11-22T12:00:00', '2026-11-22T12:05:00')
            """,
            (acc_id, whale_lead_id),
        )
        conn.commit()

        with client:
            ru_inbox = client.get("/api/inbox?crm_only=1&segment=ru_expert")
            assert ru_inbox.status_code == 200
            ru_list = ru_inbox.json()
            assert len(ru_list) == 1
            assert ru_list[0]["lead_id"] == ru_lead_id
            assert ru_list[0]["segment"] == "ru_expert"
            assert ru_list[0]["language"] == "ru"
            assert ru_list[0]["exclude_from_airtable"] == 1

            whale_with_counts = client.get(
                "/api/inbox?crm_only=1&with_counts=1&segment=whale"
            ).json()
            assert len(whale_with_counts["dialogs"]) == 1
            assert whale_with_counts["dialogs"][0]["segment"] == "whale"
            assert whale_with_counts["dialogs"][0]["pool_usd"] == 20000
            assert whale_with_counts["counts"]["waiting"] == 1
            assert whale_with_counts["counts"]["unread"] == 1
            assert whale_with_counts["workspace_total_dialogs"] == 67

            unified_ru = client.get("/api/inbox/unified?crm_only=1&segment=ru_expert").json()
            assert len(unified_ru["dialogs"]) == 1
            assert unified_ru["dialogs"][0]["segment"] == "ru_expert"
            assert unified_ru["dialogs"][0]["exclude_from_airtable"] == 1
            assert unified_ru["counts"]["waiting"] == 1
    finally:
        conn.close()


def test_manual_queues_ordering_and_mark_manual_sent_lifecycle(tmp_path: Path, monkeypatch) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        s_low_new = _insert_lead(
            conn,
            name="Founder Low Pool",
            company="SmallBounty",
            segment="social_manual",
            language="en",
            company_domain="smallbounty.io",
            pool_usd=5000,
            manual_action_url="https://wa.me/14155550177",
            manual_draft_text="Want 3 demo reels for SmallBounty? Drop TG/WA.",
            phone="+14155550177",
            email="founder@smallbounty.io",
            status="new",
            created_at="2026-11-22T15:00:00",
        )
        s_high_older = _insert_lead(
            conn,
            name="Founder Big Pool",
            company="BigBounty",
            segment="social_manual",
            language="en",
            company_domain="bigbounty.io",
            pool_usd=30000,
            manual_action_url="https://linkedin.com/in/bigbounty",
            manual_draft_text="Saw your $30K pool — want 15s custom demo?",
            status="new",
            created_at="2026-11-21T10:00:00",
        )
        s_no_pool_newest = _insert_lead(
            conn,
            name="Founder No Pool New",
            company="BrandX",
            segment="social_manual",
            language="en",
            company_domain="brandx.ai",
            pool_usd=None,
            manual_action_url="https://instagram.com/brandx",
            manual_draft_text="Quick DM for BrandX",
            status="pending",
            created_at="2026-11-22T17:00:00",
        )
        # Already contacted / closed -> excluded from open queue
        _insert_lead(
            conn,
            name="Already Sent",
            company="DoneCo",
            segment="social_manual",
            pool_usd=50000,
            status="waiting",
            last_contacted="2026-11-22T09:00:00",
        )
        _insert_lead(
            conn,
            name="Won Lead",
            company="WonCo",
            segment="social_manual",
            pool_usd=90000,
            status="won",
        )

        j_older = _insert_lead(
            conn,
            name="HR Old",
            company="OldStudio",
            segment="vacancy_assisted",
            language="ru",
            company_domain="oldstudio.ru",
            manual_action_url="https://hh.ru/vacancy/111",
            manual_draft_text="Добрый день! Отклик по вакансии рилсмейкера.",
            status="new",
            created_at="2026-11-21T08:00:00",
            lead_source="hh_ru",
        )
        j_newest = _insert_lead(
            conn,
            name="Kwork Client",
            company="VideoProject",
            segment="vacancy_assisted",
            language="ru",
            company_domain="kwork.ru",
            manual_action_url="https://kwork.ru/projects/999",
            manual_draft_text="Добрый день! Сделаем серию AI Reels под ключ.",
            status="new",
            created_at="2026-11-22T16:30:00",
            lead_source="kwork",
        )
        j_en = _insert_lead(
            conn,
            name="Upwork Buyer",
            company="GlobalApp",
            segment="vacancy_assisted",
            language="en",
            manual_action_url="https://upwork.com/jobs/777",
            manual_draft_text="Hey! We run 1,500+ AI UGC reels/mo.",
            status="new",
            created_at="2026-11-22T14:00:00",
            lead_source="upwork",
        )

        with client:
            ms_resp = client.get("/api/queues/manual-social")
            assert ms_resp.status_code == 200
            ms_data = ms_resp.json()
            assert ms_data["ok"] is True
            assert ms_data["segment"] == "social_manual"
            assert ms_data["count"] == 3
            assert [it["id"] for it in ms_data["items"]] == [
                s_high_older,
                s_low_new,
                s_no_pool_newest,
            ]
            required_keys = {
                "id",
                "name",
                "company",
                "segment",
                "language",
                "company_domain",
                "pool_usd",
                "manual_action_url",
                "manual_draft_text",
                "phone",
                "email",
                "status",
                "created_at",
            }
            assert required_keys.issubset(ms_data["items"][0].keys())
            assert ms_data["items"][1]["phone"] == "+14155550177"
            assert ms_data["items"][1]["email"] == "founder@smallbounty.io"

            aj_resp = client.get("/api/queues/assisted-jobs")
            assert aj_resp.status_code == 200
            aj_data = aj_resp.json()
            assert [it["id"] for it in aj_data["items"]] == [j_newest, j_en, j_older]

            aj_ru = client.get("/api/queues/assisted-jobs?language=ru").json()
            assert [it["id"] for it in aj_ru["items"]] == [j_newest, j_older]

            # Mark social lead sent with default payload -> status becomes 'waiting', last_contacted set, event recorded
            mark_resp = client.post(f"/api/leads/{s_high_older}/mark-manual-sent")
            assert mark_resp.status_code == 200
            marked = mark_resp.json()
            assert marked["id"] == s_high_older
            assert marked["status"] == "waiting"
            assert marked["last_contacted"]
            ev_types = [ev["event_type"] for ev in marked["events"]]
            assert "manual_queue_sent" in ev_types

            # Disappears from manual-social open queue
            ms_after = client.get("/api/queues/manual-social").json()
            assert [it["id"] for it in ms_after["items"]] == [s_low_new, s_no_pool_newest]

            # Mark assisted job sent with explicit status 'contacted' -> normalized to 'talking'
            mark_job = client.post(
                f"/api/leads/{j_newest}/mark-manual-sent",
                json={"status": "contacted", "note": "Откликнулся на Kwork"},
            )
            assert mark_job.status_code == 200
            job_data = mark_job.json()
            assert job_data["status"] == "talking"
            assert job_data["note"] == "Откликнулся на Kwork"
            assert any(ev["event_type"] == "manual_queue_sent" for ev in job_data["events"])

            aj_after = client.get("/api/queues/assisted-jobs").json()
            assert [it["id"] for it in aj_after["items"]] == [j_en, j_older]

            # Validation: invalid status -> 400; missing lead -> 404
            bad_status = client.post(
                f"/api/leads/{s_low_new}/mark-manual-sent",
                json={"status": "unknown_status_xyz"},
            )
            assert bad_status.status_code == 400
            assert client.post("/api/leads/999999/mark-manual-sent").status_code == 404
    finally:
        conn.close()


def test_enrichment_budget_endpoint_spend_cache_and_cli_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    import leadgen.budget_guard as bg

    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        today = utc_today()
        record_enrichment_ledger_entry(
            conn,
            day=today,
            provider="treg",
            endpoint="people.phone.find",
            lead_key="acme.com",
            cost_usd=1.25,
            hit=1,
        )
        record_enrichment_ledger_entry(
            conn,
            day=today,
            provider="monid",
            endpoint="anyapi/facebook.page_contact",
            lead_key="acme-fb",
            cost_usd=0.35,
            hit=1,
        )
        # Historical day must NOT pollute today's UTC summary
        record_enrichment_ledger_entry(
            conn,
            day="2020-01-01",
            provider="treg",
            endpoint="people.phone.find",
            lead_key="old.com",
            cost_usd=99.0,
            hit=1,
        )

        # 1. Unavailable CLI must NOT fail endpoint
        def _missing_cli(*_a, **_kw):
            raise FileNotFoundError("treg missing")

        monkeypatch.setattr(bg, "fetch_live_balances", _missing_cli)
        with client:
            r1 = client.get("/api/leadgen/enrichment-budget")
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1["ok"] is True
            assert d1["day"] == today
            assert d1["spent_usd"] == 1.6
            assert d1["total_usd"] == 1.6
            assert d1["daily_cap_usd"] == 5.0
            assert d1["remaining_usd"] == 3.4
            assert d1["treg_balance_usd"] is None
            assert d1["monid_balance_usd"] is None
            assert d1["paid_allowed"] is True
            assert d1["by_provider"]["treg"]["total_usd"] == 1.25
            assert d1["by_provider"]["monid"]["total_usd"] == 0.35

            # 2. Healthy + warn balance -> cached on second call
            calls = {"n": 0}

            def _healthy_cli() -> tuple[float, float]:
                calls["n"] += 1
                return (10.94, 1.18)

            monkeypatch.setattr(bg, "fetch_live_balances", _healthy_cli)
            r2 = client.get("/api/leadgen/enrichment-budget")
            assert r2.status_code == 200
            d2 = r2.json()
            assert d2["treg_balance_usd"] == 10.94
            assert d2["monid_balance_usd"] == 1.18
            assert d2["paid_allowed"] is True
            assert d2["low_balance_warning"] is True
            assert d2["cached"] is False
            assert calls["n"] == 1

            r2_cached = client.get("/api/leadgen/enrichment-budget")
            assert r2_cached.status_code == 200
            assert r2_cached.json()["cached"] is True
            assert calls["n"] == 1

            # 3. ?refresh=1 picks up low balance < $0.50 -> paid_allowed=False
            monkeypatch.setattr(bg, "fetch_live_balances", lambda: (10.94, 0.35))
            r3 = client.get("/api/leadgen/enrichment-budget?refresh=1")
            assert r3.status_code == 200
            d3 = r3.json()
            assert d3["monid_balance_usd"] == 0.35
            assert d3["paid_allowed"] is False
            assert "monid=" in d3["stop_reason"]

            # 4. Daily cap reached ($5.00) -> paid_allowed=False even with healthy balances
            record_enrichment_ledger_entry(
                conn,
                day=today,
                provider="treg",
                endpoint="people.phone.find",
                lead_key="whale2.io",
                cost_usd=3.40,
                hit=1,
            )
            monkeypatch.setattr(bg, "fetch_live_balances", lambda: (25.0, 25.0))
            r4 = client.get("/api/leadgen/enrichment-budget?refresh=1")
            assert r4.status_code == 200
            d4 = r4.json()
            assert d4["spent_usd"] == 5.0
            assert d4["remaining_usd"] == 0.0
            assert d4["paid_allowed"] is False
            assert "daily_cap=" in d4["stop_reason"]

            # 5. Invalid day rejected with HTTP 400
            assert client.get("/api/leadgen/enrichment-budget?day=bad-date").status_code == 400
    finally:
        conn.close()


def test_campaign_templates_put_validation_and_creatoros_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        camp = create_campaign(conn, name="Client Reels & Вакансии RU — TG", daily_limit=20)
        cid = int(camp["id"])
        legacy_brand = "Creator" + "OS"

        with client:
            valid_payload = {
                "step1": "Добрый день! По задаче {role} — система AI UGC для {company}. Витрина: {showcase_url}",
                "step2": "Можем сделать 10-сек тестовый фрагмент под {company}.",
                "step3": "Подскажите, задача ещё открыта?",
                "system_prompt": "Пиши коротко и деловым тоном без воды.",
                "showcase_url": "https://tmxfy.github.io/",
            }
            put_ok = client.put(f"/api/campaigns/{cid}/templates", json=valid_payload)
            assert put_ok.status_code == 200
            res = put_ok.json()
            assert res == {
                "ok": True,
                "campaign_id": cid,
                "templates": valid_payload,
            }

            row = conn.execute(
                "SELECT custom_step_templates_json FROM campaigns WHERE id = ?", (cid,)
            ).fetchone()
            assert json.loads(row["custom_step_templates_json"]) == valid_payload

            get_tpl = client.get(f"/api/campaigns/{cid}/templates").json()
            assert get_tpl == res

            camp_detail = client.get(f"/api/campaigns/{cid}").json()
            assert camp_detail["custom_step_templates"] == valid_payload

            # Reject legacy brand mention in ANY field with HTTP 400 without clobbering saved templates
            for field in ("step1", "step2", "step3", "system_prompt", "showcase_url"):
                bad_payload = dict(valid_payload)
                bad_payload[field] = f"https://{legacy_brand.lower()}.io/" if field == "showcase_url" else f"Привет от {legacy_brand}!"
                bad_resp = client.put(f"/api/campaigns/{cid}/templates", json=bad_payload)
                assert bad_resp.status_code == 400

            row_after = conn.execute(
                "SELECT custom_step_templates_json FROM campaigns WHERE id = ?", (cid,)
            ).fetchone()
            assert json.loads(row_after["custom_step_templates_json"]) == valid_payload

            # Reject invalid showcase_url scheme, unknown key, non-string value, and missing campaign
            assert (
                client.put(
                    f"/api/campaigns/{cid}/templates",
                    json={"showcase_url": "javascript:alert(1)"},
                ).status_code
                == 400
            )
            assert (
                client.put(
                    f"/api/campaigns/{cid}/templates",
                    json={"unknown_key": "abc"},
                ).status_code
                == 400
            )
            assert (
                client.put(
                    f"/api/campaigns/{cid}/templates",
                    json={"step1": 12345},
                ).status_code
                == 400
            )
            assert (
                client.put(
                    "/api/campaigns/999999/templates",
                    json={"step1": "Hello"},
                ).status_code
                == 404
            )
    finally:
        conn.close()


def test_enrichment_budget_uses_kv_settings_cache_and_aliases_templates(
    tmp_path: Path, monkeypatch
) -> None:
    import leadgen.budget_guard as bg

    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        # Warm kv_settings cache via budget_guard.check_balances_and_alert
        bg.check_balances_and_alert(
            conn,
            treg_balance_fn=lambda: 14.5,
            monid_balance_fn=lambda: 3.25,
            notify_fn=lambda _msg: True,
        )
        # CLI should NOT even be called when kv_settings cache is fresh (<300s)
        monkeypatch.setattr(
            bg,
            "fetch_live_balances",
            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("CLI should not be invoked")),
        )
        camp = create_campaign(conn, name="Alias Camp", daily_limit=20)
        cid = int(camp["id"])

        with client:
            budget_res = client.get("/api/leadgen/enrichment-budget").json()
            assert budget_res["cached"] is True
            assert budget_res["treg_balance_usd"] == 14.5
            assert budget_res["monid_balance_usd"] == 3.25
            assert budget_res["paid_allowed"] is True

            # Wrapped payload + alias normalization + empty-string omission
            put_alias = client.put(
                f"/api/campaigns/{cid}/templates",
                json={
                    "templates": {
                        "step1_text": "  Custom Step 1  ",
                        "step2_text": "",
                        "editable_prompt_override": "Short & direct",
                        "showcase_url": None,
                    }
                },
            )
            assert put_alias.status_code == 200
            assert put_alias.json()["templates"] == {
                "step1": "Custom Step 1",
                "system_prompt": "Short & direct",
            }
    finally:
        conn.close()


def test_ui_queues_auth_enforced_blocks_unauthenticated_and_allows_crm_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CRM_REQUIRE_AUTH", "1")
    monkeypatch.setenv("CRM_ADMIN_PASSWORD", "StrongQueuePass#2026")
    conn, client = _make_client(tmp_path, monkeypatch)
    try:
        lead_id = _insert_lead(
            conn,
            name="Auth Whale",
            segment="social_manual",
            manual_action_url="https://x.com/authwhale",
        )
        camp = create_campaign(conn, name="Auth Camp", daily_limit=20)
        cid = int(camp["id"])
        ua_headers = {"User-Agent": "Mozilla/5.0 Chrome/125.0"}

        with client:
            assert client.get("/api/queues/manual-social", headers=ua_headers).status_code == 401
            assert client.get("/api/queues/assisted-jobs", headers=ua_headers).status_code == 401
            assert client.get("/api/leadgen/enrichment-budget", headers=ua_headers).status_code == 401
            assert (
                client.post(f"/api/leads/{lead_id}/mark-manual-sent", headers=ua_headers).status_code
                == 401
            )
            assert (
                client.put(
                    f"/api/campaigns/{cid}/templates",
                    json={"step1": "Hi"},
                    headers=ua_headers,
                ).status_code
                == 401
            )

            login_resp = client.post(
                "/api/auth/login",
                json={"username": "tim", "password": "StrongQueuePass#2026"},
                headers=ua_headers,
            )
            assert login_resp.status_code == 200

            assert client.get("/api/queues/manual-social", headers=ua_headers).status_code == 200
            assert client.get("/api/queues/assisted-jobs", headers=ua_headers).status_code == 200
            assert (
                client.post(f"/api/leads/{lead_id}/mark-manual-sent", headers=ua_headers).status_code
                == 200
            )
            assert (
                client.put(
                    f"/api/campaigns/{cid}/templates",
                    json={"step1": "Authenticated template"},
                    headers=ua_headers,
                ).status_code
                == 200
            )
    finally:
        conn.close()
