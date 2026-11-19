import pytest
from fastapi.testclient import TestClient

from app.db import apply_inbox_strict_clients_hygiene_v11, connect, init_db, utc_now
from app.digest import audit_reply_notifications, heal_reply_notifications
from app.funnel_analytics import ensure_funnel_columns
from app.inbox import _person_is_crm_relevant, list_inbox
from app.leads import create_lead
from app.main import _filter_people_to_existing_or_crm, create_app
from app.notes import add_note
from app.outreach import create_campaign
from app.reminders import create_reminder
from app.tasks import create_task
from app.telegram_gateway import FakeGateway


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "strict_inbox_profile_test.db")
    init_db(db)
    return db


def _make_account(
    conn,
    *,
    phone: str = "+79991112233",
    username: str = "sales_sender",
    name: str = "Sales Sender",
    tg_uid: int = 2001,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at, updated_at, health
        ) VALUES (?, ?, 'connected', ?, ?, ?, ?, ?, 'active')
        """,
        (phone, f"data/sessions/{username}.session", tg_uid, username, name, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_dialog(
    conn,
    account_id: int,
    *,
    peer_id: int,
    username: str | None,
    title: str,
    last_message_preview: str | None = "Привет!",
    last_message_at: str | None = None,
    last_from_peer: int = 1,
    lead_id: int | None = None,
    is_bot: int = 0,
    with_message: bool = True,
) -> int:
    now = last_message_at or utc_now()
    cur = conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, is_group,
            unread_count, last_message_at, last_message_preview, last_from_peer,
            muted, lead_id, is_bot, created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, 0, 1, ?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (
            account_id,
            peer_id,
            username,
            title,
            now if last_message_preview else None,
            last_message_preview,
            last_from_peer,
            lead_id,
            is_bot,
            now,
            now,
        ),
    )
    dlg_id = int(cur.lastrowid)
    if with_message and last_message_preview:
        conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, sent_at, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?)
            """,
            (dlg_id, "in" if last_from_peer else "out", last_message_preview, now, now, now),
        )
    conn.commit()
    return dlg_id


def test_crm_only_strict_blocks_bots_team_ghosts_uncontacted_and_vacancies(conn):
    acc_main = _make_account(conn, phone="+79991112201", username="sales_vlad", name="Vlad", tg_uid=5001)
    acc_other = _make_account(conn, phone="+79991112202", username="sales_roman", name="Roman", tg_uid=5002)
    now = utc_now()

    b2b_camp = create_campaign(
        conn,
        name="SoftUGC B2B Main",
        account_id=acc_main,
        steps=[{"template_text": "Hey {name}"}],
    )
    vac_camp = create_campaign(
        conn,
        name="TG Вакансии",
        account_id=acc_main,
        steps=[{"template_text": "Вакансия"}],
    )

    # 1. Real CRM talking lead (must KEEP)
    real_lead = create_lead(
        conn,
        name="Real B2B Client",
        username="real_b2b_founder",
        telegram_user_id=81001,
        status="talking",
        account_id=acc_main,
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81001,
        username="real_b2b_founder",
        title="Real B2B Client",
        last_message_preview="Send demo reels",
        lead_id=real_lead["id"],
    )

    # 2. Real contacted + replied outreach recipient (must KEEP)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name,
            current_step, status, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 81002, 'replied_saas_ceo', 'Replied CEO', 1, 'replied', ?, ?, ?, ?)
        """,
        (b2b_camp["id"], acc_main, now, now, now),
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81002,
        username="replied_saas_ceo",
        title="Replied CEO",
        last_message_preview="Interesting, let's chat",
    )

    # 3. Bots even if attached to a lead (must BLOCK)
    bot_lead = create_lead(
        conn,
        name="Bot Leak",
        username=None,
        telegram_user_id=81003,
        status="won",
        account_id=acc_main,
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81003,
        username="animusastro_bot",
        title="ANIMUS Bot",
        last_message_preview="Stars ready",
        lead_id=bot_lead["id"],
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81004,
        username="crmchat_crm_bot",
        title="CRMchat Bot",
        last_message_preview="Reminder",
        lead_id=bot_lead["id"],
        is_bot=1,
    )

    # 4. Telegram service account & @Telegram username (must BLOCK)
    _insert_dialog(
        conn,
        acc_main,
        peer_id=777000,
        username="Telegram",
        title="Telegram",
        last_message_preview="Login code",
        lead_id=real_lead["id"],
    )

    # 5. Own connected account + internal team (must BLOCK)
    _insert_dialog(
        conn,
        acc_main,
        peer_id=5002,
        username="sales_roman",
        title="Roman",
        last_message_preview="Hey colleague",
        lead_id=real_lead["id"],
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81005,
        username="korznikov_timofei",
        title="Timofei",
        last_message_preview="Personal team chat",
    )

    # 6. Empty ghost duplicate dialog (empty preview + 0 messages in messages table, even with lead_id -> must BLOCK)
    _insert_dialog(
        conn,
        acc_other,
        peer_id=81001,
        username="real_b2b_founder",
        title="Real B2B Client Ghost",
        last_message_preview="",
        lead_id=real_lead["id"],
        with_message=False,
    )

    # 7. Uncontacted current_step=0 false 'replied' without lead_id (must BLOCK)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name,
            current_step, status, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 81006, 'uncontacted_pal', 'Dan Pal', 0, 'replied', NULL, NULL, ?, ?)
        """,
        (b2b_camp["id"], now, now),
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81006,
        username="uncontacted_pal",
        title="Dan Pal",
        last_message_preview="Thumbs up",
    )

    # 8. Replied recipient on WRONG session account (sent via acc_other, dialog on acc_main -> must BLOCK on acc_main)
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name,
            current_step, status, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, 81007, 'other_session_lead', 'Other Session', 1, 'replied', ?, ?, ?, ?)
        """,
        (b2b_camp["id"], acc_other, now, now, now),
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81007,
        username="other_session_lead",
        title="Other Session Personal",
        last_message_preview="Personal message on acc_main",
    )

    # 9. Vacancy campaign HR dialog (must BLOCK)
    hr_lead = create_lead(
        conn,
        name="HR Candidate",
        username="hr_applicant_1",
        telegram_user_id=81008,
        status="talking",
        account_id=acc_main,
    )
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, telegram_user_id, username, name,
            current_step, status, sent_account_id, last_sent_at, created_at, updated_at
        ) VALUES (?, ?, 81008, 'hr_applicant_1', 'HR Candidate', 1, 'replied', ?, ?, ?, ?)
        """,
        (vac_camp["id"], hr_lead["id"], acc_main, now, now, now),
    )
    _insert_dialog(
        conn,
        acc_main,
        peer_id=81008,
        username="hr_applicant_1",
        title="HR Candidate",
        last_message_preview="Где нашли вакансию?",
        lead_id=hr_lead["id"],
    )
    conn.commit()

    crm_rows = list_inbox(conn, 0, "", crm_only=True)
    kept_usernames = [r["username"] for r in crm_rows]
    assert sorted(kept_usernames) == ["real_b2b_founder", "replied_saas_ceo"]
    assert len(crm_rows) == 2

    # Check _person_is_crm_relevant & _filter_people_to_existing_or_crm reject bots & personal chats too
    assert _person_is_crm_relevant(conn, acc_main, 81003, "animusastro_bot", None, is_bot=True) is False
    assert _person_is_crm_relevant(conn, acc_main, 81009, "random_stranger", None) is False
    assert _person_is_crm_relevant(conn, acc_main, 81008, "hr_applicant_1", None) is False
    assert _person_is_crm_relevant(conn, acc_main, 81001, "real_b2b_founder", None) is True

    filtered_sync = _filter_people_to_existing_or_crm(
        conn,
        acc_main,
        [
            {"telegram_user_id": 81001, "username": "real_b2b_founder", "name": "Real"},
            {"telegram_user_id": 81003, "username": "animusastro_bot", "name": "Bot", "is_bot": True},
            {"telegram_user_id": 999999, "username": "personal_mom", "name": "Mom"},
        ],
    )
    assert [p["username"] for p in filtered_sync] == ["real_b2b_founder"]


def test_heal_and_v11_hygiene_quarantine_personal_and_detach_bots(conn):
    acc_id = _make_account(conn, phone="+79991112203", username="heal_acc", tg_uid=5003)
    now = utc_now()
    camp = create_campaign(conn, name="SoftUGC Main", account_id=acc_id, steps=[{"template_text": "Hey"}])

    bot_lead = create_lead(conn, name="Service", username="Telegram", status="talking", account_id=acc_id)
    dlg_bot = _insert_dialog(
        conn,
        acc_id,
        peer_id=82001,
        username="helper_test_bot",
        title="Helper Bot",
        lead_id=bot_lead["id"],
    )

    # Uncontacted step=0 no_lead + Pre-2026-09 personal DM no_lead + Real Nov 2026 B2B no_lead
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, telegram_user_id, username, name,
            current_step, status, last_sent_at, created_at, updated_at
        ) VALUES
            (?, 82002, 'personal_step0', 'Dan', 0, 'replied', NULL, ?, ?),
            (?, 82003, 'oxsatoshik_ozon', 'Ozon Chat', 1, 'replied', '2026-06-01T15:00:00', ?, ?),
            (?, 82004, 'real_prospect_nov', 'Real Nov', 1, 'replied', '2026-11-10T10:00:00', ?, ?)
        """,
        (camp["id"], now, now, camp["id"], now, now, camp["id"], now, now),
    )
    conn.commit()

    rep_before = audit_reply_notifications(conn)
    assert rep_before["bot_username_lead_count"] == 1

    healed = heal_reply_notifications(conn)
    assert healed["scrubbed_bot_leads"] == 1
    assert healed["detached_ineligible_dialogs"] == 1
    assert conn.execute("SELECT lead_id FROM dialogs WHERE id = ?", (dlg_bot,)).fetchone()["lead_id"] is None
    assert conn.execute("SELECT username FROM leads WHERE id = ?", (bot_lead["id"],)).fetchone()["username"] is None

    v11_res = apply_inbox_strict_clients_hygiene_v11(conn, force=True)
    assert v11_res["uncontacted_quarantined"] == 1
    assert v11_res["pre_softugc_quarantined"] == 1

    statuses = {
        r["username"]: (r["status"], r["error_text"])
        for r in conn.execute("SELECT username, status, error_text FROM campaign_recipients").fetchall()
    }
    assert statuses["personal_step0"] == ("failed", "uncontacted_personal_chat_hygiene_v11")
    assert statuses["oxsatoshik_ozon"] == ("failed", "pre_softugc_personal_dm_hygiene_v11")
    assert statuses["real_prospect_nov"][0] == "replied"


def test_full_client_profile_endpoints_live_cache_refresh_and_nolead(conn):
    ensure_funnel_columns(conn)
    acc1 = _make_account(conn, phone="+79991112204", username="acc_one", name="Acc One", tg_uid=5004)
    acc2 = _make_account(conn, phone="+79991112205", username="acc_two", name="Acc Two", tg_uid=5005)
    now = utc_now()

    lead = create_lead(
        conn,
        name="Elena Founder",
        username="elena_saas",
        telegram_user_id=93001,
        phone="+491511234567",
        status="talking",
        amount=4500,
        monthly_value=1500,
        company="CloudCRM",
        niche="B2B SaaS",
        offer_type="Retainer",
        url="https://cloudcrm.example",
        next_step="Созвон во вторник",
        note="Сильный фит под AI UGC",
        account_id=acc1,
    )
    add_note(conn, lead["id"], "Проверили кейсы на созвоне")
    create_task(conn, lead["id"], "Отправить коммерческое предложение")
    create_reminder(conn, lead["id"], "2026-11-20T12:00:00", "Пинг по офферу")

    camp = create_campaign(conn, name="SoftUGC — База Все2", account_id=acc1, steps=[{"template_text": "Hey"}])
    conn.execute(
        """
        INSERT INTO campaign_recipients (
            campaign_id, lead_id, sent_account_id, telegram_user_id, username, name,
            company, website, category, their_angle, our_angle, hook,
            step1_text, current_step, status, last_sent_at, read_at, created_at, updated_at
        ) VALUES (?, ?, ?, 93001, 'elena_saas', 'Elena Founder',
            'CloudCRM', 'https://cloudcrm.example', 'B2B SaaS', 'Outbound bottleneck',
            'AI UGC funnel', 'Saw PH launch', 'Hey Elena, saw CloudCRM!', 1, 'replied', ?, ?, ?, ?)
        """,
        (camp["id"], lead["id"], acc1, now, now, now, now),
    )

    dlg1 = _insert_dialog(
        conn,
        acc1,
        peer_id=93001,
        username="elena_saas",
        title="Elena Founder",
        last_message_preview="Давай созвонимся в 15:00",
        lead_id=lead["id"],
    )
    _insert_dialog(
        conn,
        acc2,
        peer_id=93001,
        username="elena_saas",
        title="Elena Founder",
        last_message_preview="Переписка со второго аккаунта",
        lead_id=lead["id"],
    )

    # Also a dialog WITHOUT a lead
    dlg_nolead = _insert_dialog(
        conn,
        acc1,
        peer_id=93002,
        username="new_inbound_lead",
        title="Alex Inbound",
        last_message_preview="Сколько стоит пакет?",
        lead_id=None,
    )

    gw = FakeGateway(
        profiles={
            "93001": {
                "peer_id": 93001,
                "username": "elena_saas",
                "first_name": "Elena",
                "last_name": "Petrova",
                "phone": "+491511234567",
                "bio": "Founder @CloudCRM | Scaling B2B DevTools",
                "status_text": "В сети",
                "is_premium": True,
                "is_verified": False,
                "is_scam": False,
                "is_fake": False,
                "is_bot": False,
                "common_chats_count": 3,
                "personal_channel": "@cloudcrm_news · CloudCRM Updates",
            },
            "93002": {
                "peer_id": 93002,
                "username": "new_inbound_lead",
                "first_name": "Alex",
                "last_name": "Inbound",
                "bio": "E-com brand owner $2M ARR",
                "status_text": "Был(а) недавно",
                "is_premium": False,
                "common_chats_count": 1,
            },
        }
    )

    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        # 1. First fetch hits live gateway & persists cache
        r1 = client.get(f"/api/dialogs/{dlg1}/profile")
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["telegram_profile"]["bio"] == "Founder @CloudCRM | Scaling B2B DevTools"
        assert d1["telegram_profile"]["source"] == "telegram_live"
        assert d1["telegram_profile"]["is_premium"] is True
        assert d1["telegram_profile"]["personal_channel"] == "@cloudcrm_news · CloudCRM Updates"
        assert d1["telegram_profile"]["tg_url"] == "https://t.me/elena_saas"
        assert d1["stats"]["total_dialogs"] == 2
        assert d1["stats"]["total_messages"] == 2
        assert d1["stats"]["campaigns_count"] == 1
        assert d1["outreach"][0]["their_angle"] == "Outbound bottleneck"
        assert d1["outreach"][0]["our_angle"] == "AI UGC funnel"
        assert len(d1["lead"]["notes"]) >= 1
        assert len(d1["lead"]["tasks"]) == 1
        assert len(d1["reminders"]) == 1

        # 2. Update FakeGateway profile & verify normal GET returns cached bio while ?refresh=1 fetches updated live bio
        gw.profiles["93001"]["bio"] = "Updated Founder Bio @CloudCRM"
        r2 = client.get(f"/api/leads/{lead['id']}/profile")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["telegram_profile"]["source"] == "telegram_cached"
        assert d2["telegram_profile"]["bio"] == "Founder @CloudCRM | Scaling B2B DevTools"

        r3 = client.get(f"/api/leads/{lead['id']}/profile?refresh=1")
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["telegram_profile"]["source"] == "telegram_live"
        assert d3["telegram_profile"]["bio"] == "Updated Founder Bio @CloudCRM"

        # 3. Dialog without a CRM lead returns clean profile too
        r4 = client.get(f"/api/dialogs/{dlg_nolead}/profile")
        assert r4.status_code == 200
        d4 = r4.json()
        assert d4["lead"] is None
        assert d4["primary_dialog_id"] == dlg_nolead
        assert d4["telegram_profile"]["bio"] == "E-com brand owner $2M ARR"

        # 4. Gateway raising on ?refresh=1 gracefully returns cached/CRM data without 500
        async def _boom(*_a, **_kw):
            raise RuntimeError("FloodWaitError")

        gw.fetch_peer_profile = _boom
        r5 = client.get(f"/api/leads/{lead['id']}/profile?refresh=1")
        assert r5.status_code == 200
        d5 = r5.json()
        assert d5["telegram_profile"]["bio"] == "Updated Founder Bio @CloudCRM"
        assert d5["telegram_profile"]["source"] == "telegram_cached"

        # 5. Lead with account_id=None and 0 dialogs still resolves via connected TG account fallback
        gw2 = FakeGateway(
            profiles={
                "@AirtableProspect": {
                    "peer_id": 93099,
                    "username": "airtableprospect",
                    "first_name": "Air",
                    "last_name": "Prospect",
                    "bio": "Airtable founder bio",
                }
            }
        )
        lead_orphan = create_lead(
            conn,
            name="Airtable Prospect",
            username="AirtableProspect",
            status="talking",
            account_id=None,
        )
        app.state.gateway = gw2
        r6 = client.get(f"/api/leads/{lead_orphan['id']}/profile")
        assert r6.status_code == 200
        d6 = r6.json()
        assert d6["telegram_profile"]["bio"] == "Airtable founder bio"
        assert d6["telegram_profile"]["source"] == "telegram_live"


def test_is_bot_without_suffix_and_account_search_crm_only(conn):
    from app.inbox import apply_live_outgoing

    acc = _make_account(conn, phone="+79991112206", username="search_acc", tg_uid=5006)
    lead = create_lead(
        conn,
        name="Weird Bot Flagged",
        username="stealth_helper_svc",
        telegram_user_id=94001,
        status="talking",
        account_id=acc,
    )
    _insert_dialog(
        conn,
        acc,
        peer_id=94001,
        username="stealth_helper_svc",
        title="Helper Without Bot Suffix",
        lead_id=lead["id"],
        is_bot=1,
    )
    # _public MUST preserve is_bot=1 and _filter_crm_only_rows MUST block it
    assert list_inbox(conn, acc, "", crm_only=True) == []

    # Outgoing live message to legitimate CRM lead (with dialogs.lead_id=NULL, resolved by _INBOX_SELECT_SQL)
    real_lead = create_lead(
        conn,
        name="Live Out Lead",
        username="live_out_lead",
        telegram_user_id=94002,
        status="talking",
        account_id=acc,
    )
    pub_out = apply_live_outgoing(
        conn,
        acc,
        {
            "telegram_user_id": 94002,
            "telegram_message_id": 77,
            "username": "live_out_lead",
            "name": "Live Out Lead",
            "text": "Following up!",
            "direction": "out",
        },
    )
    assert pub_out["lead_id"] == real_lead["id"]
    assert pub_out["lead_status"] == "talking"

    # /api/accounts/{id}/dialogs?crm_only=1 must exclude personal non-CRM search hits
    gw = FakeGateway(
        dialogs=[
            {"telegram_user_id": 94002, "username": "live_out_lead", "name": "Live Out Lead"},
        ]
    )
    app = create_app(conn=conn, gateway=gw)
    with TestClient(app) as client:
        gw.dialogs.append(
            {"telegram_user_id": 94099, "username": "random_stranger_q", "name": "Random Q"}
        )
        resp = client.get(f"/api/accounts/{acc}/dialogs", params={"q": "", "crm_only": "1"})
        assert resp.status_code == 200
        assert [d["username"] for d in resp.json()] == ["live_out_lead"]
        assert conn.execute("SELECT id FROM dialogs WHERE peer_id = 94099").fetchone() is None
