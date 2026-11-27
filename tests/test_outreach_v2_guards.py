from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.db import connect, init_db
from app.outreach import (
    assert_campaign_copy_language,
    create_campaign,
    find_vacancy_account_pool_violations,
    get_account_global_sent_today,
    get_campaign,
    import_campaign_csv,
    is_account_eligible_for_outreach,
    preview_outreach_batch,
    promote_due_secondary_contacts,
    resolve_step_delivery_payload,
    resolve_step_image_url,
    run_autopilot_tick,
    run_campaign_batch,
    update_campaign,
)
from leadgen.outreach_copy import VLAD_STEP3_IMAGE_URL, VLAD_STEP3_TEXT


class _FakeTgGateway:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self._mid = 100

    async def send_message(
        self,
        session_path: str,
        target,
        text: str,
        reply_to=None,
        *,
        image_path: str | None = None,
    ) -> dict:
        self._mid += 1
        self.send_calls.append(
            {
                "session_path": session_path,
                "target": target,
                "text": text,
                "reply_to": reply_to,
                "image_path": image_path,
            }
        )
        return {"telegram_message_id": self._mid, "sent_at": "2026-11-22T12:00:00Z"}


class _FakeWaGateway:
    def __init__(self) -> None:
        self.send_calls: list[dict] = []
        self._mid = 5000

    async def send_message(self, session_path: str, phone: str, text: str) -> dict:
        self._mid += 1
        self.send_calls.append(
            {
                "session_path": session_path,
                "phone": phone,
                "text": text,
            }
        )
        return {"telegram_message_id": self._mid, "sent_at": "2026-11-22T12:05:00Z"}


def _seed_account(
    conn,
    *,
    phone: str,
    username: str | None = None,
    name: str | None = None,
    provider: str = "telegram",
    status: str = "connected",
    health: str = "active",
    daily_limit: int = 5,
    sent_today: int = 0,
    sent_date: str | None = "2026-11-22",
    last_error: str | None = None,
    session_path: str | None = None,
) -> int:
    ts = "2026-11-22T08:00:00Z"
    sp = session_path or f"data/sessions/live_{phone.lstrip('+')}.session"
    cur = conn.execute(
        """
        INSERT INTO accounts (
            provider, phone, session_path, telegram_name, telegram_username,
            status, health, daily_limit, sent_today, sent_date, last_error,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            phone,
            sp,
            name or username or phone,
            username,
            status,
            health,
            daily_limit,
            sent_today,
            sent_date,
            last_error,
            ts,
            ts,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_recipient_and_lead(
    conn,
    campaign_id: int,
    *,
    username: str | None = None,
    phone: str | None = None,
    name: str = "Alex Founder",
    company: str = "Acme Apparel",
    website: str = "https://acme.example",
    niche: str = "D2C Skincare",
    segment: str = "b2b",
    language: str = "en",
    pool_usd: float | None = None,
    company_domain: str | None = None,
    secondary_contact: dict | None = None,
    step1_text: str | None = None,
    current_step: int = 0,
    rec_status: str = "pending",
    last_sent_at: str | None = None,
    lead_status: str = "new",
    created_at: str = "2026-11-22T09:00:00Z",
) -> tuple[int, int]:
    sec_json = json.dumps(secondary_contact, ensure_ascii=False) if secondary_contact else None
    cur_lead = conn.execute(
        """
        INSERT INTO leads (
            username, name, phone, status, company, url, niche,
            segment, language, pool_usd, company_domain, secondary_contact_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            username,
            name,
            phone,
            lead_status,
            company,
            website,
            niche,
            segment,
            language,
            pool_usd,
            company_domain,
            sec_json,
            created_at,
            created_at,
        ),
    )
    lead_id = int(cur_lead.lastrowid)
    cur_rec = conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, username, name, phone, company, website, category,
            step1_text, current_step, status, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            lead_id,
            username,
            name,
            phone,
            company,
            website,
            niche,
            step1_text,
            current_step,
            rec_status,
            last_sent_at,
            created_at,
            created_at,
        ),
    )
    conn.commit()
    return lead_id, int(cur_rec.lastrowid)


def test_account_eligibility_blocks_unhealthy_and_never_spills_quota_to_neighbors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    # Direct unit assertions including health='restricted' (not in sqlite CHECK enum yet)
    assert is_account_eligible_for_outreach({"status": "connected", "health": "active"}) == (True, "active")
    assert is_account_eligible_for_outreach({"status": "connected", "health": "frozen"})[0] is False
    assert is_account_eligible_for_outreach({"status": "connected", "health": "banned"})[0] is False
    assert is_account_eligible_for_outreach({"status": "connected", "health": "restricted"})[0] is False
    assert is_account_eligible_for_outreach({"status": "disconnected", "health": "active"})[0] is False
    assert is_account_eligible_for_outreach({"status": "unauthorized", "health": "active"})[0] is False

    conn = connect(tmp_path / "quota_nospill.db")
    init_db(conn)
    try:
        acc_healthy = _seed_account(
            conn, phone="+14155550101", username="sender_healthy", status="connected", health="active", daily_limit=3
        )
        acc_frozen = _seed_account(
            conn, phone="+14155550102", username="sender_frozen", status="connected", health="frozen", daily_limit=12
        )
        acc_banned = _seed_account(
            conn, phone="+14155550103", username="sender_banned", status="connected", health="banned", daily_limit=12
        )
        acc_offline = _seed_account(
            conn, phone="+14155550104", username="sender_off", status="error", health="active", daily_limit=12
        )

        camp = create_campaign(
            conn,
            name="Whale D2C Test",
            account_ids=[acc_healthy, acc_frozen, acc_banned, acc_offline],
            daily_limit=40,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, demo reels for {company}?"}],
        )
        for idx in range(10):
            _seed_recipient_and_lead(
                conn,
                camp["id"],
                username=f"brand_owner_{idx}",
                name=f"Founder {idx}",
                company=f"Brand {idx} Skincare",
                segment="whale",
                language="en",
            )

        gw = _FakeTgGateway()
        res = asyncio.run(
            run_campaign_batch(conn, camp["id"], gateway=gw, max_send=20, respect_delays=False)
        )
        assert res["sent"] == 3
        assert res["sent_by_account"] == {str(acc_healthy): 3}
        assert len(gw.send_calls) == 3
        # Healthy neighbor strictly stays at daily_limit=3; unused limits of frozen/banned/offline NEVER spill over
        assert get_account_global_sent_today(conn, acc_healthy, today="2026-11-22") == 3
        for blocked_id in (acc_frozen, acc_banned, acc_offline):
            assert get_account_global_sent_today(conn, blocked_id, today="2026-11-22") == 0
    finally:
        conn.close()


def test_mid_batch_frozen_account_skipped_without_spillover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "mid_freeze.db")
    init_db(conn)
    try:
        acc_one = _seed_account(
            conn, phone="+14155550111", username="acc_one", status="connected", health="active", daily_limit=2
        )
        acc_two = _seed_account(
            conn, phone="+14155550112", username="acc_two", status="connected", health="active", daily_limit=5
        )
        camp = create_campaign(
            conn,
            name="Mid-Batch Freeze Guard",
            account_ids=[acc_one, acc_two],
            daily_limit=20,
            language="en",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hello {name}, short UGC question for {company}"}],
        )
        for idx in range(7):
            _seed_recipient_and_lead(
                conn,
                camp["id"],
                username=f"d2c_lead_{idx}",
                name=f"Mark {idx}",
                company=f"Serum {idx}",
                language="en",
            )

        class _MidFreezeGw(_FakeTgGateway):
            async def send_message(self, session_path, target, text, reply_to=None, *, image_path=None):
                res = await super().send_message(session_path, target, text, reply_to, image_path=image_path)
                # Freeze acc_two immediately after the very first send in the batch
                if len(self.send_calls) == 1:
                    conn.execute("UPDATE accounts SET health = 'frozen' WHERE id = ?", (acc_two,))
                    conn.commit()
                return res

        gw = _MidFreezeGw()
        res = asyncio.run(
            run_campaign_batch(conn, camp["id"], gateway=gw, max_send=10, respect_delays=False)
        )
        # acc_one could send at most 2 (never absorbs acc_two's remaining quota)
        assert res["sent_by_account"].get(str(acc_one), 0) <= 2
        assert res["sent_by_account"].get(str(acc_two), 0) <= 1
        assert res["sent"] <= 2
    finally:
        conn.close()


def test_global_daily_limit_enforced_when_subscale_shared_between_ru_vacancies_and_ru_experts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "global_subscale.db")
    init_db(conn)
    try:
        subscale_id = _seed_account(
            conn,
            phone="+79165550101",
            username="subscale",
            name="Subscale RU",
            status="connected",
            health="active",
            daily_limit=4,
        )
        camp_vac = create_campaign(
            conn,
            name="Client Reels & Вакансии RU — TG",
            account_ids=[subscale_id],
            daily_limit=20,
            segment="vacancy",
            language="ru",
            copy_profile="vacancy_ru",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Подберём UGC-креаторов для {company}."}],
        )
        camp_exp = create_campaign(
            conn,
            name="РФ-Эксперты (Продюсирование) — TG",
            account_ids=[subscale_id],
            daily_limit=20,
            segment="ru_expert",
            language="ru",
            copy_profile="ru_expert",
            exclude_from_airtable=True,
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Делаем AI-рилсы под продюсирование экспертов."}],
        )

        # Sharing subscale between RU Vacancies and RU Experts is valid (not B2B mixed)
        assert find_vacancy_account_pool_violations(conn, camp_vac["id"]) == []

        fresh_created_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for idx in range(5):
            _seed_recipient_and_lead(
                conn,
                camp_vac["id"],
                username=f"vac_hr_{idx}",
                name=f"Ирина {idx}",
                company=f"Бренд {idx}",
                niche="UGC маркетинг",
                segment="vacancy",
                language="ru",
                created_at=fresh_created_at,
            )
            _seed_recipient_and_lead(
                conn,
                camp_exp["id"],
                username=f"expert_ru_{idx}",
                name=f"Анна {idx}",
                company=f"Онлайн-школа {idx}",
                niche="Онлайн-образование",
                segment="ru_expert",
                language="ru",
            )

        gw = _FakeTgGateway()
        res1 = asyncio.run(
            run_campaign_batch(conn, camp_vac["id"], gateway=gw, max_send=3, respect_delays=False)
        )
        assert res1["sent"] == 3
        assert get_account_global_sent_today(conn, subscale_id, today="2026-11-22") == 3

        # RU Experts campaign shares subscale -> only 1 global slot remains (4 - 3 = 1)
        res2 = asyncio.run(
            run_campaign_batch(conn, camp_exp["id"], gateway=gw, max_send=5, respect_delays=False)
        )
        assert res2["sent"] == 1
        assert get_account_global_sent_today(conn, subscale_id, today="2026-11-22") == 4
        assert len(gw.send_calls) == 4

        with pytest.raises(ValueError, match="суточный лимит аккаунта"):
            asyncio.run(
                run_campaign_batch(conn, camp_exp["id"], gateway=gw, max_send=1, respect_delays=False)
            )
    finally:
        conn.close()


def test_autopilot_tick_respects_global_shared_account_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "autopilot_global.db")
    init_db(conn)
    try:
        subscale_id = _seed_account(
            conn,
            phone="+79165550102",
            username="subscale",
            status="connected",
            health="active",
            daily_limit=3,
        )
        c1 = create_campaign(
            conn,
            name="Client Reels & Вакансии RU — TG",
            account_ids=[subscale_id],
            daily_limit=15,
            status="running",
            segment="vacancy",
            language="ru",
            copy_profile="vacancy_ru",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Готовы закрыть контент-вакансию для {company}."}],
        )
        c2 = create_campaign(
            conn,
            name="РФ-Эксперты (Продюсирование) — TG",
            account_ids=[subscale_id],
            daily_limit=15,
            status="running",
            segment="ru_expert",
            language="ru",
            copy_profile="ru_expert",
            exclude_from_airtable=True,
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Запускаем UGC-продюсирование для экспертов."}],
        )
        for idx in range(4):
            _seed_recipient_and_lead(
                conn,
                c1["id"],
                username=f"hr_auto_{idx}",
                name=f"Ольга {idx}",
                company=f"Студия {idx}",
                segment="vacancy",
                language="ru",
                created_at="2026-11-20T16:00:00Z",
            )
            _seed_recipient_and_lead(
                conn,
                c2["id"],
                username=f"exp_auto_{idx}",
                name=f"Дарья {idx}",
                company=f"Школа {idx}",
                segment="ru_expert",
                language="ru",
                created_at="2026-11-20T16:00:00Z",
            )

        gw = _FakeTgGateway()
        # Weekday end-of-window UTC so paced_limit equals full daily_limit=3
        tick_dt = datetime(2026, 11, 20, 17, 50, tzinfo=timezone.utc)
        monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-20")
        conn.execute("UPDATE accounts SET sent_date = '2026-11-20' WHERE id = ?", (subscale_id,))
        conn.commit()
        res = asyncio.run(
            run_autopilot_tick(
                conn,
                gateway=gw,
                now_dt=tick_dt,
                max_send_per_campaign=2,
                respect_delays=False,
                force=True,
                jitter_range=None,
            )
        )
        assert res["sent"] == 3
        assert get_account_global_sent_today(conn, subscale_id, today="2026-11-20") == 3
    finally:
        conn.close()


def test_language_lock_rejects_mismatched_copy_in_import_preview_send_and_inline_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    # Inline fallback behaves identically when language_router import is absent
    monkeypatch.setattr("app.outreach._router_assert_copy_matches_language", None)
    ok_ru, _ = assert_campaign_copy_language("Привет, собрали AI-рилсы для бренда", "ru")
    ok_en_cyr_name, _ = assert_campaign_copy_language("Hey Константин, quick intro from SoftUGC", "en")
    bad_en_in_ru, reason_en = assert_campaign_copy_language("Hey, quick UGC reels idea for your store", "ru")
    bad_ru_in_en, reason_ru = assert_campaign_copy_language("Привет, отправляю концепт роликов", "en")
    assert ok_ru is True
    assert ok_en_cyr_name is True
    assert bad_en_in_ru is False and reason_en
    assert bad_ru_in_en is False and reason_ru

    conn = connect(tmp_path / "lang_lock.db")
    init_db(conn)
    try:
        tg_en = _seed_account(conn, phone="+14155550131", username="en_sender", daily_limit=10)
        tg_ru = _seed_account(conn, phone="+79165550132", username="ru_sender", daily_limit=10)

        camp_en = create_campaign(
            conn,
            name="Whale US TG",
            account_ids=[tg_en],
            daily_limit=10,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        )
        camp_ru = create_campaign(
            conn,
            name="РФ-Эксперты — TG",
            account_ids=[tg_ru],
            daily_limit=10,
            segment="ru_expert",
            language="ru",
            copy_profile="ru_expert",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        )

        # CSV import flags mismatched explicit step1_text as failed before sending
        csv_en_bad = "username,name,company,step1_text\n@bad_ru_lead,Иван,Бренд,Привет Иван сделаем вам UGC ролики для магазина\n"
        imp_en = import_campaign_csv(conn, camp_en["id"], csv_en_bad)
        assert imp_en["imported"] == 1
        rec_en_row = conn.execute(
            "SELECT status, error_text FROM campaign_recipients WHERE campaign_id = ?",
            (camp_en["id"],),
        ).fetchone()
        assert rec_en_row["status"] == "failed"
        assert "несоответствие языка кампании (en" in rec_en_row["error_text"].lower()

        # Direct pending mismatch is caught by preview_outreach_batch AND blocked at run_campaign_batch
        _seed_recipient_and_lead(
            conn,
            camp_en["id"],
            username="cyrillic_in_en",
            name="Максим",
            company="SkinBrand",
            step1_text="Привет, подготовили для вас три рилса под ключ",
            language="ru",
        )
        prev_en = preview_outreach_batch(conn, camp_en["id"], strict=False)
        assert any(v.get("code") == "language_mismatch" for v in prev_en["violations"])

        gw = _FakeTgGateway()
        run_en = asyncio.run(
            run_campaign_batch(conn, camp_en["id"], gateway=gw, max_send=5, respect_delays=False)
        )
        assert run_en["sent"] == 0
        assert len(gw.send_calls) == 0
        failed_en = conn.execute(
            "SELECT status, error_text FROM campaign_recipients WHERE username = 'cyrillic_in_en'"
        ).fetchone()
        assert failed_en["status"] == "failed"
        assert "несоответствие языка кампании (en)" in failed_en["error_text"].lower()

        # English text inside RU campaign is also blocked on preview + send
        _seed_recipient_and_lead(
            conn,
            camp_ru["id"],
            username="english_in_ru",
            name="John",
            company="USBrand",
            step1_text="Hey John, happy to record 3 demo reels for your Shopify brand",
            language="en",
        )
        prev_ru = preview_outreach_batch(conn, camp_ru["id"], strict=False)
        assert any(v.get("code") == "language_mismatch" for v in prev_ru["violations"])

        run_ru = asyncio.run(
            run_campaign_batch(conn, camp_ru["id"], gateway=gw, max_send=5, respect_delays=False)
        )
        assert run_ru["sent"] == 0
        assert len(gw.send_calls) == 0
        failed_ru = conn.execute(
            "SELECT status, error_text FROM campaign_recipients WHERE username = 'english_in_ru'"
        ).fetchone()
        assert failed_ru["status"] == "failed"
        assert "несоответствие языка кампании (ru)" in failed_ru["error_text"].lower()
    finally:
        conn.close()


def test_whatsapp_step3_bans_meme_image_and_kermit_caption_while_telegram_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "wa_no_meme.db")
    init_db(conn)
    try:
        wa_acc = _seed_account(
            conn,
            phone="+14155550141",
            name="WhatsApp Whale US",
            provider="whatsapp",
            daily_limit=10,
            session_path="data/whatsapp/wa_whale_live.db",
        )
        tg_acc = _seed_account(
            conn,
            phone="+14155550142",
            username="tg_whale_sender",
            provider="telegram",
            daily_limit=10,
        )

        custom_with_step3_img = json.dumps(
            {
                "step3_image_url": VLAD_STEP3_IMAGE_URL,
                "3": {"image_url": VLAD_STEP3_IMAGE_URL},
            }
        )
        camp_wa = create_campaign(
            conn,
            name="Whale US — WhatsApp",
            account_ids=[wa_acc],
            daily_limit=10,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            custom_step_templates_json=custom_with_step3_img,
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, quick video idea for {company}"},
                {"step_order": 2, "delay_hours": 48, "template_text": "Quick bump on those 3 sample clips for {company}."},
                {
                    "step_order": 3,
                    "delay_hours": 96,
                    "template_text": VLAD_STEP3_TEXT,
                    "image_url": VLAD_STEP3_IMAGE_URL,
                },
            ],
        )
        # Even when stored directly on step/custom JSON, get_campaign & helpers strip WA Step 3 image
        loaded_wa = get_campaign(conn, camp_wa["id"])
        assert loaded_wa["steps"][2]["image_url"] is None
        conn.execute(
            "UPDATE campaign_steps SET image_url = ? WHERE campaign_id = ? AND step_order = 3",
            (VLAD_STEP3_IMAGE_URL, camp_wa["id"]),
        )
        conn.commit()
        reloaded_wa = get_campaign(conn, camp_wa["id"])
        assert reloaded_wa["steps"][2]["image_url"] is None

        wa_txt, wa_img = resolve_step_delivery_payload(
            {"step_order": 3, "template_text": VLAD_STEP3_TEXT, "image_url": VLAD_STEP3_IMAGE_URL},
            2,
            {"name": "Chris", "company": "GlowLabs"},
            camp=reloaded_wa,
            provider="whatsapp",
        )
        assert wa_img is None
        assert "doing me dirty" not in wa_txt.lower()
        assert "GlowLabs" in wa_txt

        assert (
            resolve_step_image_url(
                {"step_order": 3, "image_url": VLAD_STEP3_IMAGE_URL},
                2,
                camp=reloaded_wa,
                provider="telegram",
            )
            == VLAD_STEP3_IMAGE_URL
        )

        # Advance WhatsApp recipient to Step 2 completed -> ready to send Step 3
        _seed_recipient_and_lead(
            conn,
            camp_wa["id"],
            phone="+14155550188",
            name="Chris",
            company="GlowLabs",
            segment="whale",
            language="en",
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-18T10:00:00Z",
        )
        # Ensure recipient dialog + prior 'out' messages exist so Step 3 follow-up check passes
        dlg_wa_id = int(
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, phone, title, last_from_peer,
                    last_message_at, unread_count, created_at, updated_at
                ) VALUES (?, 'user', 14155550188, '+14155550188', 'Chris', 0,
                          '2026-11-18T10:00:00Z', 0, '2026-11-16T10:00:00Z', '2026-11-18T10:00:00Z')
                """,
                (wa_acc,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
            VALUES (?, 1, 'out', 'Hey Chris, quick video idea for GlowLabs', '2026-11-16T10:00:00Z', '2026-11-16T10:00:00Z', '2026-11-16T10:00:00Z'),
                   (?, 2, 'out', 'Quick bump on those 3 sample clips for GlowLabs.', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z')
            """,
            (dlg_wa_id, dlg_wa_id),
        )
        conn.execute(
            "UPDATE campaign_recipients SET sent_account_id = ?, assigned_account_id = ? WHERE campaign_id = ?",
            (wa_acc, wa_acc, camp_wa["id"]),
        )
        conn.commit()

        wa_gw = _FakeWaGateway()
        res_wa = asyncio.run(
            run_campaign_batch(conn, camp_wa["id"], wa_gateway=wa_gw, max_send=5, respect_delays=False)
        )
        assert res_wa["sent"] == 1
        assert len(wa_gw.send_calls) == 1
        assert "doing me dirty" not in wa_gw.send_calls[0]["text"].lower()
        assert "GlowLabs" in wa_gw.send_calls[0]["text"]
        stored_wa_msg = conn.execute(
            "SELECT text, media_url, media_type FROM messages WHERE dialog_id = ? ORDER BY id DESC LIMIT 1",
            (dlg_wa_id,),
        ).fetchone()
        assert stored_wa_msg["media_url"] is None
        assert stored_wa_msg["media_type"] is None

        # Telegram Step 3 MUST keep image attachment
        camp_tg = create_campaign(
            conn,
            name="Whale US — TG",
            account_ids=[tg_acc],
            daily_limit=10,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, video reels for {company}"},
                {"step_order": 2, "delay_hours": 48, "template_text": "Quick bump for {company}"},
                {"step_order": 3, "delay_hours": 96, "template_text": VLAD_STEP3_TEXT, "image_url": VLAD_STEP3_IMAGE_URL},
            ],
        )
        _seed_recipient_and_lead(
            conn,
            camp_tg["id"],
            username="tg_brand_chris",
            name="Chris",
            company="GlowLabs",
            segment="whale",
            language="en",
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-18T10:00:00Z",
        )
        dlg_tg_id = int(
            conn.execute(
                """
                INSERT INTO dialogs (
                    account_id, peer_type, peer_id, username, title, last_from_peer,
                    last_message_at, unread_count, created_at, updated_at
                ) VALUES (?, 'user', 99001122, 'tg_brand_chris', 'Chris', 0,
                          '2026-11-18T10:00:00Z', 0, '2026-11-16T10:00:00Z', '2026-11-18T10:00:00Z')
                """,
                (tg_acc,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO messages (dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at)
            VALUES (?, 1, 'out', 'Hey Chris, video reels for GlowLabs', '2026-11-16T10:00:00Z', '2026-11-16T10:00:00Z', '2026-11-16T10:00:00Z'),
                   (?, 2, 'out', 'Quick bump for GlowLabs', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z')
            """,
            (dlg_tg_id, dlg_tg_id),
        )
        conn.execute(
            "UPDATE campaign_recipients SET telegram_user_id = 99001122, sent_account_id = ?, assigned_account_id = ? WHERE campaign_id = ?",
            (tg_acc, tg_acc, camp_tg["id"]),
        )
        conn.commit()

        tg_gw = _FakeTgGateway()
        res_tg = asyncio.run(
            run_campaign_batch(conn, camp_tg["id"], gateway=tg_gw, max_send=5, respect_delays=False)
        )
        assert res_tg["sent"] == 1
        assert tg_gw.send_calls[0]["image_path"] == VLAD_STEP3_IMAGE_URL
    finally:
        conn.close()


def test_custom_step_templates_json_and_copy_profile_and_language_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "custom_templates.db")
    init_db(conn)
    try:
        subscale_id = _seed_account(conn, phone="+79165550151", username="subscale", daily_limit=10)
        tg_en = _seed_account(conn, phone="+14155550152", username="whale_en", daily_limit=10)

        # 1. RU Vacancy with custom_step_templates_json showcase_url + step1 override
        vac_custom = json.dumps(
            {
                "showcase_url": "https://tmxfy.github.io/reels-showcase",
                "1": {
                    "template_text": "Привет, {name}! Вот наше портфолио AI-рилсов: {showcase_url} — сделаем тест для {company}."
                },
            },
            ensure_ascii=False,
        )
        camp_vac = create_campaign(
            conn,
            name="Client Reels & Вакансии RU — TG",
            account_ids=[subscale_id],
            daily_limit=10,
            segment="vacancy",
            language="ru",
            copy_profile="vacancy_ru",
            custom_step_templates_json=vac_custom,
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        )
        fresh_created_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_recipient_and_lead(
            conn,
            camp_vac["id"],
            username="vac_lead_1",
            name="Алина",
            company="SkinLab",
            niche="Beauty D2C",
            segment="vacancy",
            language="ru",
            created_at=fresh_created_at,
        )
        prev_vac = preview_outreach_batch(conn, camp_vac["id"], strict=True)
        assert prev_vac["ok"] is True
        msg_vac = prev_vac["previews"][0]["message_text"]
        assert "https://tmxfy.github.io/reels-showcase" in msg_vac
        assert "SkinLab" in msg_vac

        # update_campaign preserves & updates custom_step_templates_json + copy_profile + language
        updated_vac = update_campaign(
            conn,
            camp_vac["id"],
            custom_step_templates_json=json.dumps(
                {
                    "showcase_url": "https://tmxfy.github.io/v2",
                    "step1": "Привет, {name}! Новые примеры для {company}: {showcase_url}",
                },
                ensure_ascii=False,
            ),
        )
        prev_vac2 = preview_outreach_batch(conn, updated_vac["id"], strict=True)
        assert "https://tmxfy.github.io/v2" in prev_vac2["previews"][0]["message_text"]

        # 2. Whale campaign using whale_mass copy_profile + pool_usd enrichment
        camp_whale = create_campaign(
            conn,
            name="Whale Mass US — TG",
            account_ids=[tg_en],
            daily_limit=10,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
        )
        _seed_recipient_and_lead(
            conn,
            camp_whale["id"],
            username="whale_lead_1",
            name="Jordan",
            company="HyperPulse",
            niche="Supplements",
            segment="whale",
            language="en",
            pool_usd=25000.0,
        )
        prev_whale = preview_outreach_batch(conn, camp_whale["id"], strict=True)
        assert prev_whale["ok"] is True
        whale_txt = prev_whale["previews"][0]["message_text"]
        assert "HyperPulse" in whale_txt
        assert assert_campaign_copy_language(whale_txt, "en")[0] is True
    finally:
        conn.close()


def test_promote_due_secondary_contacts_promotes_once_per_domain_after_6_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "secondary_dm.db")
    init_db(conn)
    try:
        tg_acc = _seed_account(conn, phone="+14155550161", username="whale_tg", provider="telegram", daily_limit=10)
        wa_acc = _seed_account(
            conn,
            phone="+14155550162",
            name="Whale WA",
            provider="whatsapp",
            daily_limit=10,
            session_path="data/whatsapp/whale_sec.db",
        )

        camp_whale_tg = create_campaign(
            conn,
            name="Whale US — TG",
            account_ids=[tg_acc],
            daily_limit=20,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, demo reels for {company}?"},
                {"step_order": 2, "delay_hours": 48, "template_text": "Bump on those clips for {company}."},
                {"step_order": 3, "delay_hours": 96, "template_text": VLAD_STEP3_TEXT, "image_url": VLAD_STEP3_IMAGE_URL},
            ],
        )
        camp_whale_wa = create_campaign(
            conn,
            name="Whale US — WhatsApp",
            account_ids=[wa_acc],
            daily_limit=20,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, demo reels for {company}?"},
                {"step_order": 2, "delay_hours": 48, "template_text": "Bump on those clips for {company}."},
                {"step_order": 3, "delay_hours": 96, "template_text": "Hey, bumping this once in case it got buried for {company}."},
            ],
        )

        # 1. Eligible whale lead (>=6 days, current_step=2, replied=0, TG secondary DM)
        due_lead_id, _ = _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="primary_acme",
            name="Sam CEO",
            company="Acme Nutrition",
            website="https://acmenutrition.io",
            segment="whale",
            language="en",
            pool_usd=18000.0,
            company_domain="https://www.AcmeNutrition.io/shop",
            secondary_contact={
                "name": "Alex CMO",
                "role": "CMO",
                "telegram_username": "@alex_cmo_acme",
                "fallback_after_days": 6,
            },
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-15T10:00:00Z",
        )
        # 2. Second lead at SAME company_domain -> MUST NOT promote twice for same company
        _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="other_acme_exec",
            name="Taylor Ops",
            company="Acme Nutrition",
            website="https://acmenutrition.io",
            segment="whale",
            language="en",
            company_domain="acmenutrition.io",
            secondary_contact={"name": "Jordan VP", "telegram_username": "jordan_vp_acme"},
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-14T10:00:00Z",
        )
        # 3. Too early (<6 days) at 2026-11-22 -> blocked initially, eligible later
        early_lead_id, _ = _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="early_founder",
            name="Nina CEO",
            company="EarlyGlow",
            website="https://earlyglow.com",
            segment="whale",
            language="en",
            company_domain="earlyglow.com",
            secondary_contact={"name": "Maya Head Growth", "telegram_username": "maya_earlyglow"},
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-19T10:00:00Z",
        )
        # 4. Replied lead -> MUST NOT promote
        _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="replied_founder",
            name="Eli",
            company="RepliedCo",
            segment="whale",
            language="en",
            company_domain="replied.co",
            secondary_contact={"name": "Second Eli", "telegram_username": "second_replied_co"},
            current_step=2,
            rec_status="replied",
            lead_status="replied",
            last_sent_at="2026-11-10T10:00:00Z",
        )
        # 5. Step 1 only (current_step < 2) -> MUST NOT promote
        _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="step1_only_lead",
            name="Pat",
            company="StepOneCo",
            segment="whale",
            language="en",
            company_domain="stepone.co",
            secondary_contact={"name": "Second Pat", "telegram_username": "second_stepone"},
            current_step=1,
            rec_status="in_progress",
            last_sent_at="2026-11-10T10:00:00Z",
        )
        # 6. Non-whale segment -> MUST NOT promote
        _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="b2b_small",
            name="Dan",
            company="SmallB2B",
            segment="b2b",
            language="en",
            company_domain="smallb2b.io",
            secondary_contact={"name": "Second Dan", "telegram_username": "second_smallb2b"},
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-10T10:00:00Z",
        )
        # 7. Eligible WhatsApp secondary DM with custom fallback_after_days=5 -> routes to Whale WA campaign
        wa_due_lead_id, _ = _seed_recipient_and_lead(
            conn,
            camp_whale_tg["id"],
            username="primary_waco",
            name="Ben CEO",
            company="WaHydrate",
            website="https://wahydrate.com",
            segment="whale",
            language="en",
            company_domain="wahydrate.com",
            secondary_contact={
                "name": "Dana Brand",
                "role": "Brand Director",
                "whatsapp": "+1 (415) 555-0177",
                "fallback_after_days": 5,
            },
            current_step=2,
            rec_status="in_progress",
            last_sent_at="2026-11-16T10:00:00Z",
        )

        # First promotion pass on 2026-11-22: promotes only Acme (TG) and WaHydrate (WA) = 2
        promoted_first = promote_due_secondary_contacts(conn, now="2026-11-22T12:00:00Z")
        assert promoted_first == 2

        sec_tg_rec = conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND username = 'alex_cmo_acme'",
            (camp_whale_tg["id"],),
        ).fetchone()
        assert sec_tg_rec is not None
        assert sec_tg_rec["current_step"] == 0
        assert sec_tg_rec["status"] == "pending"

        sec_wa_rec = conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND phone = '+14155550177'",
            (camp_whale_wa["id"],),
        ).fetchone()
        assert sec_wa_rec is not None
        assert sec_wa_rec["current_step"] == 0
        assert sec_wa_rec["status"] == "pending"

        # Same company second lead, replied, step1-only, non-whale, and early lead were NOT promoted
        for skipped_u in ("jordan_vp_acme", "maya_earlyglow", "second_replied_co", "second_stepone", "second_smallb2b"):
            assert (
                conn.execute(
                    "SELECT 1 FROM campaign_recipients WHERE username = ?", (skipped_u,)
                ).fetchone()
                is None
            )

        ev_rows = conn.execute(
            "SELECT lead_id, event_type, meta_json FROM lead_events WHERE event_type = 'secondary_dm_promoted' ORDER BY id"
        ).fetchall()
        assert [int(r["lead_id"]) for r in ev_rows] == [due_lead_id, wa_due_lead_id]

        # Idempotent on same day -> 0
        assert promote_due_secondary_contacts(conn, now="2026-11-22T18:00:00Z") == 0

        # 5 days later -> only earlyglow becomes due (>=6 days since 2026-11-19), Acme/WaHydrate never re-fire
        promoted_later = promote_due_secondary_contacts(conn, now="2026-11-26T12:00:00Z")
        assert promoted_later == 1
        assert (
            conn.execute(
                "SELECT 1 FROM campaign_recipients WHERE campaign_id = ? AND username = 'maya_earlyglow'",
                (camp_whale_tg["id"],),
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS cnt FROM lead_events WHERE event_type = 'secondary_dm_promoted' AND lead_id = ?",
                (early_lead_id,),
            ).fetchone()["cnt"]
            == 1
        )
    finally:
        conn.close()


def test_en_campaign_allows_cyrillic_recipient_name_and_whale_icp_scoped_strictly_to_whale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    conn = connect(tmp_path / "en_name_whale_scope.db")
    init_db(conn)
    try:
        tg_acc = _seed_account(conn, phone="+14155550191", username="en_scope_sender", daily_limit=10)
        camp_en = create_campaign(
            conn,
            name="B2B Short Greeting Test",
            account_ids=[tg_acc],
            daily_limit=10,
            segment="b2b",
            language="en",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}"}],
        )
        _seed_recipient_and_lead(
            conn,
            camp_en["id"],
            username="konstantin_b2b",
            name="Константин",
            company="SaaS Tools",
            niche="B2B SaaS",
            segment="b2b",
            language="en",
        )
        prev_en = preview_outreach_batch(conn, camp_en["id"], strict=False)
        assert not any(v.get("code") == "language_mismatch" for v in prev_en["violations"])

        gw = _FakeTgGateway()
        run_en = asyncio.run(
            run_campaign_batch(conn, camp_en["id"], gateway=gw, max_send=5, respect_delays=False)
        )
        assert run_en["sent"] == 1
        assert gw.send_calls[0]["text"] == "Hey Константин"

        # Whale campaign enforces whale_icp_reject_reason on Gaming/Dropshipping while keeping D2C Skincare
        camp_whale = create_campaign(
            conn,
            name="Whale ICP Guard Check",
            account_ids=[tg_acc],
            daily_limit=10,
            segment="whale",
            language="en",
            copy_profile="whale_mass",
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, reels for {company}?"},
                {"step_order": 2, "delay_hours": 48, "template_text": "Quick bump on {company} clips"},
            ],
        )
        _seed_recipient_and_lead(
            conn,
            camp_whale["id"],
            username="whale_bad_dropship",
            name="Bob",
            company="QuickDrop Store",
            niche="Dropshipping",
            segment="whale",
            language="en",
        )
        _seed_recipient_and_lead(
            conn,
            camp_whale["id"],
            username="whale_good_d2c",
            name="Alice",
            company="GlowBeauty Co",
            niche="D2C Skincare",
            segment="whale",
            language="en",
        )
        res_w = asyncio.run(
            run_campaign_batch(conn, camp_whale["id"], gateway=gw, max_send=5, respect_delays=False)
        )
        assert res_w["sent"] == 1
        bad_row = conn.execute(
            "SELECT status, current_step, error_text FROM campaign_recipients WHERE username = 'whale_bad_dropship'"
        ).fetchone()
        good_row = conn.execute(
            "SELECT status, current_step, error_text FROM campaign_recipients WHERE username = 'whale_good_d2c'"
        ).fetchone()
        assert bad_row["status"] == "completed"
        assert bad_row["current_step"] == 0 and bad_row["error_text"]
        assert good_row["status"] == "in_progress"
        assert good_row["current_step"] == 1 and good_row["error_text"] is None
    finally:
        conn.close()


def test_blocker5_mixed_cyrillic_en_rejection_and_write_time_template_enforcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient
    from app.main import create_app

    # 1. Mixed Cyrillic leaks inside EN prose must fail even when English letters outnumber Cyrillic
    monkeypatch.setattr("app.outreach._router_assert_copy_matches_language", None)
    assert assert_campaign_copy_language("Hey Константин, quick intro from SoftUGC", "en") == (True, "")
    for bad_mixed in (
        "Привет! Check out SoftUGC reels",
        "Hey Alex! We run SoftUGC — сделаем 3 демо ролика под ваш бренд.",
        "Hey Привет, check out SoftUGC reels",
    ):
        ok, reason = assert_campaign_copy_language(bad_mixed, "en")
        assert ok is False and reason

    conn = connect(tmp_path / "write_time_lang.db")
    init_db(conn)
    try:
        acc_id = _seed_account(conn, phone="+14155550195", username="tpl_guard_acc", daily_limit=10)

        # 2. create_campaign & update_campaign reject mismatched language templates cleanly
        with pytest.raises(ValueError, match="Несоответствие языка"):
            create_campaign(
                conn,
                name="EN Reject Cyrillic Step",
                account_ids=[acc_id],
                daily_limit=10,
                language="en",
                steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет! Сделаем ролики"}],
            )
        with pytest.raises(ValueError, match="Несоответствие языка"):
            create_campaign(
                conn,
                name="RU Reject English Step",
                account_ids=[acc_id],
                daily_limit=10,
                language="ru",
                steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey there, let's shoot videos"}],
            )
        with pytest.raises(ValueError, match="Несоответствие языка"):
            create_campaign(
                conn,
                name="EN Reject Custom Template",
                account_ids=[acc_id],
                daily_limit=10,
                language="en",
                custom_step_templates_json={"1": "Привет! Делаем UGC ролики"},
                steps=[{"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"}],
            )

        # Placeholder-only templates pass without false positive; PUT /api/campaigns/{id}/templates enforces lock
        camp_en = create_campaign(
            conn,
            name="EN Placeholder Valid",
            account_ids=[acc_id],
            daily_limit=10,
            language="en",
            steps=[
                {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
            ],
        )
        with pytest.raises(ValueError, match="Несоответствие языка"):
            update_campaign(
                conn,
                camp_en["id"],
                steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}!"}],
            )

        app = create_app(conn=conn, gateway=None)
        with TestClient(app) as client:
            bad_put = client.put(
                f"/api/campaigns/{camp_en['id']}/templates",
                json={"step1": "Привет, {name}! Сделаем рилсы"},
            )
            assert bad_put.status_code == 400
            assert "языка" in bad_put.json()["detail"].lower()

            ok_put = client.put(
                f"/api/campaigns/{camp_en['id']}/templates",
                json={"step1": "Hey {name}, happy to send 3 demo reels for {company}"},
            )
            assert ok_put.status_code == 200
    finally:
        conn.close()


def test_blocker6_daily_limit_zero_and_concurrent_runs_cap_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")
    monkeypatch.setattr("app.outreach.utc_today", lambda: "2026-11-22")

    from app.accounts import _load as load_account, _public as public_account, check_daily_limit

    conn = connect(tmp_path / "dl0_and_race.db")
    init_db(conn)
    try:
        # 1. daily_limit = 0 must NOT silently fall back to 20
        acc_zero = _seed_account(
            conn, phone="+14155550196", username="zero_cap", daily_limit=0, sent_today=0
        )
        assert public_account(load_account(conn, acc_zero))["daily_limit"] == 0
        with pytest.raises(ValueError, match="суточный лимит аккаунта \\(0"):
            check_daily_limit(conn, acc_zero)

        camp_zero = create_campaign(
            conn,
            name="Zero Cap Guard",
            account_ids=[acc_zero],
            daily_limit=10,
            language="en",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, reels for {company}?"}],
        )
        _seed_recipient_and_lead(
            conn,
            camp_zero["id"],
            username="zero_target_1",
            name="Sam",
            company="ZeroTestCo",
            language="en",
        )
        gw_zero = _FakeTgGateway()
        with pytest.raises(ValueError, match="0 сообщ"):
            asyncio.run(run_campaign_batch(conn, camp_zero["id"], gateway=gw_zero, max_send=5))
        assert len(gw_zero.send_calls) == 0

        # Mid-batch daily_limit -> 0 stops immediately without fallback to 20
        acc_drop = _seed_account(
            conn, phone="+14155550197", username="drop_to_zero", daily_limit=5, sent_today=0
        )
        camp_drop = create_campaign(
            conn,
            name="Mid-Batch Zero Drop",
            account_ids=[acc_drop],
            daily_limit=10,
            language="en",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, quick clip for {company}"}],
        )
        for idx in range(4):
            _seed_recipient_and_lead(
                conn,
                camp_drop["id"],
                username=f"drop_lead_{idx}",
                name=f"Founder {idx}",
                company=f"DropBrand {idx}",
                language="en",
            )

        class _DropToZeroGw(_FakeTgGateway):
            async def send_message(self, session_path, target, text, reply_to=None, *, image_path=None):
                out = await super().send_message(session_path, target, text, reply_to, image_path=image_path)
                conn.execute("UPDATE accounts SET daily_limit = 0 WHERE id = ?", (acc_drop,))
                conn.commit()
                return out

        gw_drop = _DropToZeroGw()
        res_drop = asyncio.run(
            run_campaign_batch(conn, camp_drop["id"], gateway=gw_drop, max_send=5, respect_delays=False)
        )
        assert res_drop["sent"] == 1
        assert len(gw_drop.send_calls) == 1

        # 2. Concurrent run_campaign_batch sharing subscale strictly enforces global daily_limit=3
        subscale_id = _seed_account(
            conn, phone="+79165550198", username="subscale", daily_limit=3, sent_today=0
        )
        camp_v = create_campaign(
            conn,
            name="Client Reels & Вакансии RU — TG",
            account_ids=[subscale_id],
            daily_limit=20,
            segment="vacancy",
            language="ru",
            copy_profile="vacancy_ru",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Делаем AI-рилсы для {company}."}],
        )
        camp_e = create_campaign(
            conn,
            name="РФ-Эксперты (Продюсирование) — TG",
            account_ids=[subscale_id],
            daily_limit=20,
            segment="ru_expert",
            language="ru",
            copy_profile="ru_expert",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Привет, {name}! Запускаем продюсирование рилсов."}],
        )
        for idx in range(4):
            _seed_recipient_and_lead(
                conn,
                camp_v["id"],
                username=f"conc_vac_{idx}",
                name=f"Ольга {idx}",
                company=f"Бренд {idx}",
                segment="vacancy",
                language="ru",
            )
            _seed_recipient_and_lead(
                conn,
                camp_e["id"],
                username=f"conc_exp_{idx}",
                name=f"Анна {idx}",
                company=f"Школа {idx}",
                segment="ru_expert",
                language="ru",
            )

        class _SlowRaceGw(_FakeTgGateway):
            async def send_message(self, session_path, target, text, reply_to=None, *, image_path=None):
                await asyncio.sleep(0.02)
                return await super().send_message(session_path, target, text, reply_to, image_path=image_path)

        gw_race = _SlowRaceGw()

        async def _run_parallel() -> tuple[dict, dict]:
            t1 = asyncio.create_task(
                run_campaign_batch(conn, camp_v["id"], gateway=gw_race, max_send=5, respect_delays=False)
            )
            t2 = asyncio.create_task(
                run_campaign_batch(conn, camp_e["id"], gateway=gw_race, max_send=5, respect_delays=False)
            )
            r1 = await t1
            try:
                r2 = await t2
            except ValueError as exc:
                assert "лимит" in str(exc).lower()
                r2 = {"sent": 0}
            return r1, r2

        r1, r2 = asyncio.run(_run_parallel())
        assert r1["sent"] + r2["sent"] == 3
        assert len(gw_race.send_calls) == 3
        assert get_account_global_sent_today(conn, subscale_id, today="2026-11-22") == 3

        # 3. Failed send releases reserved slot cleanly without leaking sent_today
        acc_fail = _seed_account(
            conn, phone="+14155550199", username="fail_rollback", daily_limit=1, sent_today=0
        )
        camp_fail = create_campaign(
            conn,
            name="Rollback Guard",
            account_ids=[acc_fail],
            daily_limit=10,
            language="en",
            steps=[{"step_order": 1, "delay_hours": 0, "template_text": "Hey {name}, reels for {company}?"}],
        )
        _seed_recipient_and_lead(
            conn,
            camp_fail["id"],
            username="fail_target_1",
            name="Leo",
            company="FailBrand",
            language="en",
        )

        class _FailingGw(_FakeTgGateway):
            async def send_message(self, session_path, target, text, reply_to=None, *, image_path=None):
                raise RuntimeError("PeerFloodError synthetic transient failure")

        res_fail = asyncio.run(
            run_campaign_batch(conn, camp_fail["id"], gateway=_FailingGw(), max_send=1)
        )
        assert res_fail["sent"] == 0
        assert get_account_global_sent_today(conn, acc_fail, today="2026-11-22") == 0
    finally:
        conn.close()


