import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import ImportedContact, User

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import as_dict, connect, init_db, utc_now, utc_today  # noqa: E402
from app.hygiene import _has_messenger_contact, audit_crm_hygiene  # noqa: E402
from app.leadgen_settings import (  # noqa: E402
    DEFAULT_LEADGEN_CONFIG,
    _normalize_config,
    push_discovered_now,
    sync_leadgen_campaigns,
)
from app.mcp_server import _handle_ingest_discovered_leads  # noqa: E402
from app.outreach import (  # noqa: E402
    MAX_TG_PHONE_DIRECT_PER_ACCOUNT_DAY,
    create_campaign,
    get_tg_phone_direct_sent_today,
    preview_outreach_batch,
    run_campaign_batch,
)
from app.telegram_gateway import FakeGateway, GatewayError, TelethonGateway  # noqa: E402
from leadgen.language_router import CAMPAIGN_WHALES_EN_TG  # noqa: E402
from leadgen.phone_messenger_resolver import ResolvedMessenger, route_resolved_lead  # noqa: E402


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "wave_a_phone_direct.db"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "leadgen_config.json"

    monkeypatch.setenv("CRMTG_DB_PATH", str(db_path))
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEADGEN_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("CRMTG_FORCE_NO_JITTER", "1")

    conn = connect(db_path)
    init_db(conn)
    try:
        yield conn, state_dir, tmp_path
    finally:
        conn.close()


def _insert_tg_account(
    conn,
    *,
    phone: str,
    session_path: str,
    tg_uid: int,
    tg_username: str,
    daily_limit: int = 20,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO accounts (
            phone, session_path, status, telegram_user_id,
            telegram_username, telegram_name, created_at,
            health, updated_at, daily_limit, sent_today, provider
        ) VALUES (?, ?, 'connected', ?, ?, ?, ?, 'active', ?, ?, 0, 'telegram')
        """,
        (phone, session_path, tg_uid, tg_username, tg_username, now, now, daily_limit),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_route_resolved_lead_tg_phone_direct_without_username() -> None:
    resolved_map = {
        "+14155550111": ResolvedMessenger(
            phone_e164="+14155550111",
            whatsapp_exists=True,
            whatsapp_jid="14155550111@s.whatsapp.net",
            telegram_username="",
            telegram_user_id=710011,
            tg_check_status="found",
        ),
        "+14155550112": ResolvedMessenger(
            phone_e164="+14155550112",
            whatsapp_exists=True,
            whatsapp_jid="14155550112@s.whatsapp.net",
            telegram_username="",
            telegram_user_id=None,
            tg_check_status="not_found",
        ),
    }

    routed_tg = route_resolved_lead(
        {"phone": "+1 (415) 555-0111", "icp_score": 88, "company": "AcmeAI"},
        resolved_map,
    )
    assert routed_tg["provider"] == "telegram"
    assert routed_tg["telegram_user_id"] == 710011
    assert routed_tg["phone"] == "+14155550111"
    assert routed_tg["tg_phone_direct"] is True
    assert routed_tg["wa_check_pending"] is False
    assert not routed_tg.get("username")

    # Without found telegram_user_id, WhatsApp remains fallback
    routed_wa = route_resolved_lead({"phone": "+14155550112", "icp_score": 88}, resolved_map)
    assert routed_wa["provider"] == "whatsapp"
    assert routed_wa["whatsapp"] == "https://wa.me/14155550112"


def test_telethon_gateway_send_message_by_phone_imports_sends_and_cleans_contacts(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class _MockClient:
        def __init__(self, users, *, fail_send: bool = False):
            self._users = users
            self._fail_send = fail_send

        async def __call__(self, req):
            if isinstance(req, ImportContactsRequest):
                calls.append(
                    (
                        "import",
                        [
                            (c.client_id, c.phone, c.first_name, c.last_name)
                            for c in req.contacts
                        ],
                    )
                )
                real_uids = [int(u.id) for u in self._users if not getattr(u, "bot", False) and not getattr(u, "deleted", False)]
                imported = [ImportedContact(user_id=real_uids[0], client_id=1)] if real_uids else []
                return SimpleNamespace(users=list(self._users), imported=imported)
            if isinstance(req, DeleteContactsRequest):
                calls.append(("delete", [int(u.id) for u in req.id]))
                return True
            raise AssertionError(f"Unexpected TL call: {req!r}")

        async def send_message(self, target_user, body: str):
            calls.append(("send_message", (int(target_user.id), body)))
            if self._fail_send:
                raise RuntimeError("UserPrivacyRestrictedError")
            return SimpleNamespace(
                id=881,
                message=body,
                date=datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc),
            )

        async def send_file(self, target_user, media_path: str, caption: str | None = None):
            calls.append(("send_file", (int(target_user.id), media_path, caption)))
            return SimpleNamespace(
                id=882,
                message=caption or "",
                date=datetime(2026, 11, 23, 12, 1, tzinfo=timezone.utc),
            )

    async def _run():
        gw = TelethonGateway()
        sess = "data/sessions/acc_phone_test.session"
        bot_user = User(id=1001, access_hash=1, bot=True, username="helper_bot")
        deleted_user = User(id=1002, access_hash=2, deleted=True)
        real_user = User(id=505001, access_hash=3, first_name="Sarah", username=None)
        client_ok = _MockClient([bot_user, deleted_user, real_user])

        async def _fake_open(_sp: str):
            return client_ok, False

        gw._open = _fake_open  # type: ignore[method-assign]

        res = await gw.send_message_by_phone(
            sess, "00 1 (415) 555-0142", "Hey Sarah, quick SoftUGC note", first_name="Sarah"
        )
        assert res["telegram_message_id"] == 881
        assert res["telegram_user_id"] == 505001
        assert res["username"] is None
        assert res["text"] == "Hey Sarah, quick SoftUGC note"
        assert res["direction"] == "out"
        assert res["sent_at"] == "2026-11-23T12:00:00"
        assert not gw._session_lock(sess).locked()

        assert calls[0] == ("import", [(1, "+14155550142", "Sarah", "")])
        assert calls[1] == ("send_message", (505001, "Hey Sarah, quick SoftUGC note"))
        assert calls[2] == ("delete", [1001, 1002, 505001])

        # Image path uses send_file and defaults first_name to "Contact"
        img_file = tmp_path / "demo.jpg"
        img_file.write_bytes(b"jpg-bytes")
        calls.clear()
        res_img = await gw.send_message_by_phone(
            sess, "+14155550143", "With attachment", image_path=str(img_file)
        )
        assert res_img["telegram_message_id"] == 882
        assert calls[0][1][0][2] == "Contact"
        assert calls[1][0] == "send_file"
        assert calls[2][0] == "delete"

        # Failure during send_message MUST still execute DeleteContactsRequest in finally
        calls.clear()
        client_fail = _MockClient([real_user], fail_send=True)

        async def _fake_open_fail(_sp: str):
            return client_fail, False

        gw._open = _fake_open_fail  # type: ignore[method-assign]
        with pytest.raises(GatewayError, match="UserPrivacyRestrictedError"):
            await gw.send_message_by_phone(sess, "+14155550142", "Hello")
        assert calls[-1] == ("delete", [505001])
        assert not gw._session_lock(sess).locked()

        # Bot/deleted only -> raises GatewayError AND still deletes imported contacts
        calls.clear()
        client_bot_only = _MockClient([bot_user, deleted_user])

        async def _fake_open_bot(_sp: str):
            return client_bot_only, False

        gw._open = _fake_open_bot  # type: ignore[method-assign]
        with pytest.raises(GatewayError, match="не найден в Telegram"):
            await gw.send_message_by_phone(sess, "+14155550199", "Hello")
        assert calls[-1] == ("delete", [1001, 1002])

    asyncio.run(_run())


def test_fake_gateway_send_message_by_phone() -> None:
    async def _run():
        gw = FakeGateway(
            phone_contacts={
                "+14155550150": {"telegram_user_id": 771122, "username": None},
                "+14155550151": None,
            }
        )
        res = await gw.send_message_by_phone("s.session", "14155550150", "Hi there", first_name="Maya")
        assert res["telegram_user_id"] == 771122
        assert res["username"] is None
        assert res["direction"] == "out"
        assert gw.sent[-1]["via"] == "send_message_by_phone"
        assert gw.sent[-1]["phone"] == "+14155550150"
        assert gw.sent[-1]["first_name"] == "Maya"

        with pytest.raises(GatewayError, match="не найден в Telegram"):
            await gw.send_message_by_phone("s.session", "+14155550151", "Hi")

    asyncio.run(_run())


def test_hygiene_and_mcp_treat_tg_phone_direct_as_valid_telegram_not_wa_only(isolated_env) -> None:
    conn, state_dir, _ = isolated_env
    assert _has_messenger_contact(
        username=None,
        phone="+14155550160",
        telegram_user_id=7700160,
        provider="telegram",
    )

    sync_leadgen_campaigns(conn)
    res = _handle_ingest_discovered_leads(
        conn,
        {
            "enroll_in_campaign": True,
            "campaign_name": CAMPAIGN_WHALES_EN_TG,
            "leads": [
                {
                    "name": "Nina Brooks",
                    "company": "VeloFitness",
                    "provider": "telegram",
                    "telegram_user_id": 7700160,
                    "phone": "+14155550160",
                    "tg_phone_direct": True,
                    "website": "https://velofitness.io",
                    "category": "Fitness App",
                    "their_angle": "Running TikTok UGC sprints for fitness app",
                    "source": "meta_ads",
                    "segment": "whale",
                    "language": "en",
                    "icp_score": 91,
                }
            ],
        },
    )
    assert res["ok"] is True
    assert res["created_leads"] == 1
    assert res["enrolled_in_campaign"] == 1
    assert res["quarantined"] == 0

    lead = as_dict(conn.execute("SELECT * FROM leads WHERE telegram_user_id = 7700160").fetchone())
    assert lead is not None
    assert lead["provider"] == "telegram"
    assert lead["phone"] == "+14155550160"
    assert lead["username"] is None

    rec = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE lead_id = ?", (lead["id"],)).fetchone()
    )
    assert rec is not None
    assert rec["status"] == "pending"
    assert rec["phone"] == "+14155550160"

    hyg = audit_crm_hygiene(conn)
    assert hyg["ok"] is True
    assert hyg["contactless_lead_count"] == 0
    assert hyg["active_junk_recipient_count"] == 0

    # Also verify push_discovered_now handles phone-direct TG leads without username
    (state_dir / "discovered_leads.jsonl").write_text(
        json.dumps(
            {
                "name": "Owen Price",
                "company": "KinetixAI",
                "provider": "telegram",
                "telegram_user_id": 7700161,
                "phone": "+14155550161",
                "tg_phone_direct": True,
                "website": "https://kinetixai.com",
                "category": "AI SaaS",
                "their_angle": "AI video creation for mobile consumer apps",
                "source": "meta_ads",
                "segment": "whale",
                "icp_score": 90,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pushed = push_discovered_now(conn)
    assert pushed["ok"] is True
    assert pushed["ingested"] == 1
    owen_lead = as_dict(conn.execute("SELECT * FROM leads WHERE telegram_user_id = 7700161").fetchone())
    assert owen_lead is not None
    assert owen_lead["provider"] == "telegram"


def test_outreach_phone_direct_send_fallback_cap_rotation_and_identity_sync(isolated_env) -> None:
    conn, _state_dir, _ = isolated_env
    acc1 = _insert_tg_account(
        conn,
        phone="+15550001001",
        session_path="data/sessions/acc_pd_1.session",
        tg_uid=90001,
        tg_username="sender_one",
        daily_limit=30,
    )
    acc2 = _insert_tg_account(
        conn,
        phone="+15550001002",
        session_path="data/sessions/acc_pd_2.session",
        tg_uid=90002,
        tg_username="sender_two",
        daily_limit=30,
    )

    camp = create_campaign(
        conn,
        name=CAMPAIGN_WHALES_EN_TG,
        account_id=acc1,
        account_ids=[acc1, acc2],
        status="running",
        daily_limit=30,
        steps=[
            {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
            {"step_order": 2, "delay_hours": 48, "template_text": "{step2_text}"},
        ],
    )
    cid = int(camp["id"])
    conn.execute(
        "UPDATE campaigns SET segment = 'whale', language = 'en', copy_profile = 'whale_mass' WHERE id = ?",
        (cid,),
    )
    conn.commit()

    # Enroll 6 phone-only TG leads + 1 stale-username lead + 1 normal username lead
    leads_payload = []
    for idx in range(1, 7):
        leads_payload.append(
            {
                "name": f"Phone Lead {idx}",
                "company": f"PhoneBrand{idx}",
                "provider": "telegram",
                "phone": f"+1415555020{idx}",
                "tg_phone_direct": True,
                "website": f"https://phonebrand{idx}.io",
                "category": "AI SaaS",
                "their_angle": f"Scaling consumer reels for PhoneBrand{idx}",
                "source": "meta_ads",
                "segment": "whale",
                "language": "en",
                "step1_text": f"Hey Phone, quick SoftUGC demo reels idea for PhoneBrand{idx}.",
                "step2_text": f"Bumping this for PhoneBrand{idx}.",
            }
        )
    # Lead 7 has stale username + valid phone -> entity resolution fails -> falls back to phone direct
    leads_payload.append(
        {
            "name": "Stale Handle Lead",
            "company": "StaleBrand",
            "provider": "telegram",
            "username": "stale_missing_user",
            "phone": "+14155550299",
            "website": "https://stalebrand.io",
            "category": "AI SaaS",
            "their_angle": "Building consumer creator AI app",
            "source": "meta_ads",
            "segment": "whale",
            "language": "en",
            "step1_text": "Hey Stale, quick SoftUGC demo reels idea for StaleBrand.",
        }
    )
    # Lead 8 has valid username (works even if all accounts hit phone-direct daily cap!)
    leads_payload.append(
        {
            "name": "Handle Lead",
            "company": "HandleBrand",
            "provider": "telegram",
            "username": "valid_tg_handle",
            "website": "https://handlebrand.io",
            "category": "AI SaaS",
            "their_angle": "Building consumer mobile video app",
            "source": "meta_ads",
            "segment": "whale",
            "language": "en",
            "step1_text": "Hey Handle, quick SoftUGC demo reels idea for HandleBrand.",
        }
    )

    ing = _handle_ingest_discovered_leads(
        conn,
        {"campaign_id": cid, "enroll_in_campaign": True, "leads": leads_payload},
    )
    assert ing["enrolled_in_campaign"] == 8

    # Preview check must pass cleanly for phone-direct TG leads without username
    prev = preview_outreach_batch(conn, cid, sample_size=5, respect_delays=False)
    assert prev["ok"] is True
    assert not prev["violations"]

    class _EntityFailFallbackGateway(FakeGateway):
        async def send_message(self, session_path: str, peer_id, text: str, reply_to=None, image_path=None):
            if str(peer_id).lstrip("@").lower() == "stale_missing_user":
                raise GatewayError("Could not find the input entity for PeerUser")
            return await super().send_message(session_path, peer_id, text, reply_to=reply_to, image_path=image_path)

    gw = _EntityFailFallbackGateway(
        dialogs=[
            {"telegram_user_id": 880800, "username": "valid_tg_handle", "name": "Handle Lead"},
        ],
        phone_contacts={
            "+14155550201": {"telegram_user_id": 880201, "username": "discovered_u1"},
            "+14155550202": {"telegram_user_id": 880202, "username": None},
            "+14155550203": {"telegram_user_id": 880203, "username": None},
            "+14155550204": {"telegram_user_id": 880204, "username": None},
            "+14155550205": {"telegram_user_id": 880205, "username": None},
            "+14155550206": {"telegram_user_id": 880206, "username": None},
            "+14155550299": {"telegram_user_id": 880299, "username": "new_resolved_u"},
        },
    )

    # Pin first 5 phone leads to acc1 to fill acc1's 5/day phone-direct cap,
    # and keep acc1 least-loaded or explicitly assign first 5 + stale to acc1.
    rec_rows = [
        as_dict(r)
        for r in conn.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? ORDER BY id ASC",
            (cid,),
        ).fetchall()
    ]
    for r in rec_rows[:5]:
        conn.execute(
            "UPDATE campaign_recipients SET assigned_account_id = ? WHERE id = ?",
            (acc1, r["id"]),
        )
    # Assign lead 6 (phone-only) to acc1 as well -> must rotate to acc2 once acc1 hits 5/5!
    conn.execute(
        "UPDATE campaign_recipients SET assigned_account_id = ? WHERE id = ?",
        (acc1, rec_rows[5]["id"]),
    )
    # Assign stale username lead to acc1 -> entity fail fallback hits 5/5 cap on acc1, rotates to acc2!
    conn.execute(
        "UPDATE campaign_recipients SET assigned_account_id = ? WHERE id = ?",
        (acc1, rec_rows[6]["id"]),
    )
    conn.commit()

    batch_res = asyncio.run(run_campaign_batch(conn, cid, gateway=gw, max_send=20))
    assert batch_res["sent"] == 8

    today = utc_today()
    assert MAX_TG_PHONE_DIRECT_PER_ACCOUNT_DAY == 5
    assert get_tg_phone_direct_sent_today(conn, acc1, today=today) == 5
    assert get_tg_phone_direct_sent_today(conn, acc2, today=today) == 2

    # Check identity sync on lead 1 (discovered real telegram_user_id + username)
    l1 = as_dict(conn.execute("SELECT * FROM leads WHERE phone = '+14155550201'").fetchone())
    assert l1["telegram_user_id"] == 880201
    assert l1["username"] == "discovered_u1"
    d1 = as_dict(conn.execute("SELECT * FROM dialogs WHERE id = ?", (l1["dialog_id"],)).fetchone())
    assert d1["peer_id"] == 880201
    assert d1["username"] == "discovered_u1"

    # Check stale username fallback synced real telegram_user_id and new username on lead 7 & dialog
    l7 = as_dict(conn.execute("SELECT * FROM leads WHERE phone = '+14155550299'").fetchone())
    assert l7["telegram_user_id"] == 880299
    assert l7["username"] == "new_resolved_u"
    r7 = as_dict(conn.execute("SELECT * FROM campaign_recipients WHERE lead_id = ?", (l7["id"],)).fetchone())
    assert r7["sent_account_id"] == acc2
    assert r7["telegram_user_id"] == 880299
    assert r7["username"] == "new_resolved_u"

    # Now saturate acc2 to 5/5 phone-direct too, add another phone-only + another @username lead:
    # Phone-only lead stays 'pending' (NOT 'failed'), while @username lead STILL sends!
    for _ in range(3):
        from app.outreach import _increment_tg_phone_direct_sent_today

        _increment_tg_phone_direct_sent_today(conn, acc2, today=today)
    assert get_tg_phone_direct_sent_today(conn, acc2, today=today) == 5

    ing2 = _handle_ingest_discovered_leads(
        conn,
        {
            "campaign_id": cid,
            "enroll_in_campaign": True,
            "leads": [
                {
                    "name": "Overflow Phone",
                    "company": "OverflowCo",
                    "provider": "telegram",
                    "phone": "+14155550277",
                    "tg_phone_direct": True,
                    "website": "https://overflowco.io",
                    "category": "AI SaaS",
                    "their_angle": "Consumer video analytics app",
                    "source": "meta_ads",
                    "segment": "whale",
                    "language": "en",
                    "step1_text": "Hey Overflow, quick SoftUGC demo reels idea for OverflowCo.",
                },
                {
                    "name": "Normal Username",
                    "company": "NormalCo",
                    "provider": "telegram",
                    "username": "normal_user_ok",
                    "website": "https://normalco.io",
                    "category": "AI SaaS",
                    "their_angle": "Consumer mobile productivity app",
                    "source": "meta_ads",
                    "segment": "whale",
                    "language": "en",
                    "step1_text": "Hey Normal, quick SoftUGC demo reels idea for NormalCo.",
                },
            ],
        },
    )
    assert ing2["enrolled_in_campaign"] == 2
    gw.dialogs.append({"telegram_user_id": 880900, "username": "normal_user_ok", "name": "Normal Username"})

    batch2 = asyncio.run(run_campaign_batch(conn, cid, gateway=gw, max_send=10, respect_delays=True))
    assert batch2["sent"] == 1

    overflow_rec = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE phone = '+14155550277'").fetchone()
    )
    assert overflow_rec["status"] == "pending"
    assert overflow_rec["current_step"] == 0
    assert overflow_rec["error_text"] is None

    normal_rec = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE username = 'normal_user_ok'").fetchone()
    )
    assert normal_rec["status"] == "in_progress"
    assert normal_rec["current_step"] == 1

    # Also verify Lead 1 (which discovered username 'discovered_u1' via phone-direct)
    # can receive Step 2 via @username even while phone-direct quota is 5/5!
    batch3 = asyncio.run(run_campaign_batch(conn, cid, gateway=gw, max_send=10, respect_delays=False))
    # Lead 1 (@discovered_u1), Lead 7 (@new_resolved_u), Lead 8 (@valid_tg_handle), Lead 10 (@normal_user_ok)
    # advance via @username, while phone-only leads (Leads 2-6 & Overflow) stay untouched at 5/5 cap!
    assert batch3["sent"] == 4
    l2_rec = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE phone = '+14155550202'").fetchone()
    )
    assert l2_rec["current_step"] == 1
    assert l2_rec["status"] == "in_progress"
    overflow_rec_after = as_dict(
        conn.execute("SELECT * FROM campaign_recipients WHERE phone = '+14155550277'").fetchone()
    )
    assert overflow_rec_after["status"] == "pending"
    assert overflow_rec_after["current_step"] == 0


def test_wave_a_leadgen_settings_defaults_and_ro_fallback_normalization() -> None:
    assert DEFAULT_LEADGEN_CONFIG["enrichment"]["daily_budget_cap_usd"] == 10.0
    assert DEFAULT_LEADGEN_CONFIG["whales"]["max_dms_per_company"] == 3
    assert DEFAULT_LEADGEN_CONFIG["whales"]["phone_tg_checker_daily_limit"] == 200
    assert DEFAULT_LEADGEN_CONFIG["whales"]["allow_phone_checker_ro_fallback"] is True

    assert _normalize_config({})["whales"]["allow_phone_checker_ro_fallback"] is True
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": False}})["whales"]["allow_phone_checker_ro_fallback"] is False
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": "false"}})["whales"]["allow_phone_checker_ro_fallback"] is False
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": "0"}})["whales"]["allow_phone_checker_ro_fallback"] is False
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": "true"}})["whales"]["allow_phone_checker_ro_fallback"] is True
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": 0}})["whales"]["allow_phone_checker_ro_fallback"] is False
    assert _normalize_config({"whales": {"allow_phone_checker_ro_fallback": 1}})["whales"]["allow_phone_checker_ro_fallback"] is True

    with pytest.raises(ValueError, match="allow_phone_checker_ro_fallback"):
        _normalize_config({"whales": {"allow_phone_checker_ro_fallback": "maybe"}})
