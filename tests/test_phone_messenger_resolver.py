from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.types import ImportedContact, User

from app.whatsapp_gateway import GatewayError, WhatsAppGateway
from leadgen import phone_messenger_resolver as resolver
from leadgen.phone_messenger_resolver import (
    ResolvedMessenger,
    merge_resolved_messengers,
    normalize_e164,
    resolve_phones_telegram,
    resolve_phones_whatsapp,
    route_resolved_lead,
)


def test_normalize_e164_valid_and_invalid_inputs() -> None:
    assert normalize_e164("+1 (415) 555-0199") == "+14155550199"
    assert normalize_e164("1-415-555-0199") == "+14155550199"
    assert normalize_e164("0044 7911 123456") == "+447911123456"
    assert normalize_e164("1234567") == "+1234567"
    assert normalize_e164("+123456789012345") == "+123456789012345"

    assert normalize_e164("") == ""
    assert normalize_e164("not a phone") == ""
    assert normalize_e164("123456") == ""
    assert normalize_e164("+1234567890123456") == ""
    assert normalize_e164("0012345") == ""


def test_resolve_phones_whatsapp_batches_and_parses_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(resolver.asyncio, "sleep", fake_sleep)

    class StubGateway:
        async def check_numbers(self, session_path: str, batch: list[str]) -> list[dict]:
            calls.append((session_path, list(batch)))
            out = []
            for p in batch:
                if p == "+14155550101":
                    out.append(
                        {
                            "phone": p,
                            "found": True,
                            "wa_jid": "14155550101@s.whatsapp.net",
                            # Gateway stores numeric phone key here for inbox parity;
                            # resolver must NEVER treat it as a real Telegram user ID.
                            "telegram_user_id": 14155550101,
                        }
                    )
                elif p == "+14155550103":
                    out.append({"phone": p, "is_on_whatsapp": True, "jid": ""})
                else:
                    out.append({"phone": p, "found": False, "wa_jid": None})
            return out

    res = asyncio.run(
        resolve_phones_whatsapp(
            StubGateway(),
            "/opt/crmtg/data/wa_main.db",
            [
                "+1 (415) 555-0101",
                "14155550101",  # duplicate after normalization
                "+14155550102",
                "bad",
                "+14155550103",
            ],
            batch_size=2,
            pause_s=3.0,
        )
    )

    assert calls == [
        ("/opt/crmtg/data/wa_main.db", ["+14155550101", "+14155550102"]),
        ("/opt/crmtg/data/wa_main.db", ["+14155550103"]),
    ]
    assert sleeps == [3.0]

    assert res["+14155550101"] == ResolvedMessenger(
        phone_e164="+14155550101",
        whatsapp_exists=True,
        whatsapp_jid="14155550101@s.whatsapp.net",
        telegram_username="",
        telegram_user_id=None,
        wa_check_pending=False,
    )
    assert res["+14155550102"] == ResolvedMessenger(
        phone_e164="+14155550102",
        whatsapp_exists=False,
        whatsapp_jid="",
        wa_check_pending=False,
    )
    assert res["+14155550103"].whatsapp_exists is True
    assert res["+14155550103"].whatsapp_jid == "14155550103@s.whatsapp.net"


def test_resolve_phones_whatsapp_never_fabricates_on_missing_or_error_session(
    tmp_path,
) -> None:
    phones = ["+14155550111", "+447911123456"]

    # 1. Missing gateway / empty session path
    res_no_gw = asyncio.run(resolve_phones_whatsapp(None, "/tmp/wa.db", phones))
    res_no_sess = asyncio.run(resolve_phones_whatsapp(WhatsAppGateway(), "", phones))
    for mapping in (res_no_gw, res_no_sess):
        assert set(mapping.keys()) == set(phones)
        for item in mapping.values():
            assert item.whatsapp_exists is False
            assert item.whatsapp_jid == ""
            assert item.wa_check_pending is True

    # 2. Real WhatsAppGateway raising GatewayError for unauthorized/missing session file
    missing_db = str(tmp_path / "nonexistent_whatsmeow.db")
    res_real_err = asyncio.run(
        resolve_phones_whatsapp(WhatsAppGateway(), missing_db, phones, pause_s=0.0)
    )
    for phone in phones:
        assert res_real_err[phone].whatsapp_exists is False
        assert res_real_err[phone].wa_check_pending is True

    # 3. Mid-batch gateway failure preserves batch 1 and marks remaining pending
    class FlakyGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def check_numbers(self, session_path: str, batch: list[str]) -> list[dict]:
            self.calls += 1
            if self.calls == 1:
                return [{"phone": batch[0], "found": True, "wa_jid": "14155550111@s.whatsapp.net"}]
            raise GatewayError("session disconnected")

    res_partial = asyncio.run(
        resolve_phones_whatsapp(
            FlakyGateway(),
            "/opt/crmtg/data/wa.db",
            ["+14155550111", "+447911123456", "+4915112345678"],
            batch_size=1,
            pause_s=0.0,
        )
    )
    assert res_partial["+14155550111"].whatsapp_exists is True
    assert res_partial["+14155550111"].wa_check_pending is False
    assert res_partial["+447911123456"].whatsapp_exists is False
    assert res_partial["+447911123456"].wa_check_pending is True
    assert res_partial["+4915112345678"].whatsapp_exists is False
    assert res_partial["+4915112345678"].wa_check_pending is True


def test_resolve_phones_telegram_refuses_non_checker_session() -> None:
    called = False

    async def fake_client(req):
        nonlocal called
        called = True
        return SimpleNamespace(users=[], imported=[])

    # Default is_checker_session=False must fail closed with ValueError
    with pytest.raises(ValueError, match="is_checker_session"):
        asyncio.run(
            resolve_phones_telegram(
                fake_client,
                ["+14155550101"],
            )
        )
    assert called is False

    # Explicit is_checker_session=False must also refuse immediately
    with pytest.raises(ValueError, match="is_checker_session"):
        asyncio.run(
            resolve_phones_telegram(
                fake_client,
                ["+14155550101"],
                is_checker_session=False,
            )
        )
    assert called is False


def test_resolve_phones_telegram_handles_none_client_and_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res_none = asyncio.run(
        resolve_phones_telegram(None, ["+14155550101"], is_checker_session=True)
    )
    assert res_none["+14155550101"].tg_check_status == "no_checker_client"

    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(resolver.asyncio, "sleep", fake_sleep)

    imported_batches: list[list[str]] = []
    deleted_batches: list[list[int]] = []

    async def checker_client(req):
        if isinstance(req, ImportContactsRequest):
            imported_batches.append([c.phone for c in req.contacts])
            users = []
            imported = []
            for c in req.contacts:
                uid = 1000 + c.client_id
                # Omit User.phone on purpose to prove ImportedContact client_id mapping works
                users.append(User(id=uid, access_hash=99, username=f"lead_{uid}", phone=None))
                imported.append(ImportedContact(user_id=uid, client_id=c.client_id))
            return SimpleNamespace(users=users, imported=imported)
        if isinstance(req, DeleteContactsRequest):
            deleted_batches.append([int(u.id) for u in req.id])
            return True
        raise AssertionError(f"Unexpected TL request: {req!r}")

    phones = [f"+1415555010{i}" for i in range(1, 6)]
    res = asyncio.run(
        resolve_phones_telegram(
            checker_client,
            phones,
            batch_size=2,
            daily_limit=3,
            pause_s=10.0,
            is_checker_session=True,
        )
    )

    assert imported_batches == [
        ["+14155550101", "+14155550102"],
        ["+14155550103"],
    ]
    assert len(deleted_batches) == 2
    assert sleeps == [10.0]
    assert res["+14155550101"].telegram_username == "lead_1001"
    assert res["+14155550101"].telegram_user_id == 1001
    assert res["+14155550101"].tg_check_status == "found"
    assert res["+14155550103"].telegram_username == "lead_1001"
    assert res["+14155550104"].tg_check_status == "daily_limit_reached"
    assert res["+14155550105"].tg_check_status == "daily_limit_reached"


def test_resolve_phones_telegram_cleans_contacts_in_finally_even_on_processing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_ids: list[int] = []

    async def checker_client(req):
        if isinstance(req, ImportContactsRequest):
            return SimpleNamespace(
                users=[User(id=555, access_hash=1, username="alice", phone="14155550101")],
                imported=[],
            )
        if isinstance(req, DeleteContactsRequest):
            deleted_ids.extend(int(u.id) for u in req.id)
            return True
        raise AssertionError(f"Unexpected request: {req!r}")

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected mapping failure")

    monkeypatch.setattr(resolver, "_map_imported_users", boom)

    res = asyncio.run(
        resolve_phones_telegram(
            checker_client,
            ["+14155550101"],
            pause_s=0.0,
            is_checker_session=True,
        )
    )
    assert deleted_ids == [555]
    assert res["+14155550101"].tg_check_status == "error"


def test_resolve_phones_telegram_stops_early_on_flood_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(resolver.asyncio, "sleep", fake_sleep)

    import_calls = 0

    async def checker_client(req):
        nonlocal import_calls
        if isinstance(req, ImportContactsRequest):
            import_calls += 1
            if import_calls == 1:
                return SimpleNamespace(
                    users=[User(id=77, access_hash=2, username="first_hit", phone="14155550101")],
                    imported=[],
                )
            raise FloodWaitError(request=req, capture=300)
        if isinstance(req, DeleteContactsRequest):
            return True
        raise AssertionError(f"Unexpected request: {req!r}")

    phones = ["+14155550101", "+14155550102", "+14155550103"]
    res = asyncio.run(
        resolve_phones_telegram(
            checker_client,
            phones,
            batch_size=1,
            pause_s=45.0,
            is_checker_session=True,
        )
    )

    assert import_calls == 2
    assert sleeps == [45.0]
    assert res["+14155550101"].telegram_username == "first_hit"
    assert res["+14155550101"].tg_check_status == "found"
    assert res["+14155550102"].tg_check_status == "flood_wait"
    assert res["+14155550103"].tg_check_status == "flood_wait"


def test_resolve_phones_telegram_stops_on_two_consecutive_zero_hit_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr(resolver.asyncio, "sleep", fake_sleep)

    calls: list[list[str]] = []

    async def checker_client(req):
        if isinstance(req, ImportContactsRequest):
            phones_in_batch = [c.phone for c in req.contacts]
            calls.append(phones_in_batch)
            # Batch 1: 0 hits; Batch 2: 1 hit (resets streak); Batch 3: 0 hits; Batch 4: 0 hits -> pause!
            if "+14155550103" in phones_in_batch:
                return SimpleNamespace(
                    users=[User(id=88, access_hash=3, username="mid_hit", phone="14155550103")],
                    imported=[],
                )
            return SimpleNamespace(users=[], imported=[])
        if isinstance(req, DeleteContactsRequest):
            return True
        raise AssertionError(f"Unexpected request: {req!r}")

    phones = [f"+1415555010{i}" for i in range(1, 10)]
    res = asyncio.run(
        resolve_phones_telegram(
            checker_client,
            phones,
            batch_size=2,
            pause_s=5.0,
            is_checker_session=True,
        )
    )

    # Batch 5 (+14155550109) must NOT be called because Batch 3 + Batch 4 were 2 consecutive 0-hit batches
    assert len(calls) == 4
    assert sleeps == [5.0, 5.0, 5.0]
    assert res["+14155550101"].tg_check_status == "not_found"
    assert res["+14155550103"].telegram_username == "mid_hit"
    assert res["+14155550103"].tg_check_status == "found"
    assert res["+14155550105"].tg_check_status == "not_found"
    assert res["+14155550107"].tg_check_status == "zero_hit_paused"
    assert res["+14155550108"].tg_check_status == "zero_hit_paused"
    assert res["+14155550109"].tg_check_status == "zero_hit_paused"


def test_route_resolved_lead_priorities_and_social_manual() -> None:
    # Priority 1: Telegram beats WhatsApp when both exist
    merged = merge_resolved_messengers(
        {
            "+14155550101": ResolvedMessenger(
                phone_e164="+14155550101",
                whatsapp_exists=True,
                whatsapp_jid="14155550101@s.whatsapp.net",
            ),
            "+14155550102": ResolvedMessenger(
                phone_e164="+14155550102",
                whatsapp_exists=True,
                whatsapp_jid="14155550102@s.whatsapp.net",
            ),
            "+14155550103": ResolvedMessenger(
                phone_e164="+14155550103",
                wa_check_pending=True,
            ),
            "+14155550104": ResolvedMessenger(
                phone_e164="+14155550104",
                whatsapp_exists=False,
                wa_check_pending=False,
            ),
        },
        {
            "+14155550101": ResolvedMessenger(
                phone_e164="+14155550101",
                telegram_username="@whale_ceo",
                telegram_user_id=9001,
                tg_check_status="found",
            ),
            # User on Telegram without username -> should fall through to WhatsApp!
            "+14155550102": ResolvedMessenger(
                phone_e164="+14155550102",
                telegram_username="",
                telegram_user_id=9002,
                tg_check_status="found",
            ),
        },
    )

    row1_orig = {"phone": "+1 (415) 555-0101", "icp_score": 95}
    routed1 = route_resolved_lead(row1_orig, merged)
    assert "provider" not in row1_orig  # non-mutating
    assert routed1["provider"] == "telegram"
    assert routed1["username"] == "whale_ceo"
    assert routed1["telegram_user_id"] == 9001
    assert routed1["phone"] == "+14155550101"

    # Priority 2: WhatsApp
    routed2 = route_resolved_lead({"phone": "14155550102", "icp_score": 90}, merged)
    assert routed2["provider"] == "whatsapp"
    assert routed2["phone"] == "+14155550102"
    assert routed2["whatsapp"] == "https://wa.me/14155550102"
    assert routed2["wa_check_pending"] is False

    # Priority 3: WA check pending preserves phone and blocks premature social_manual fallback
    routed3 = route_resolved_lead(
        {
            "phone": "+14155550103",
            "icp_score": 95,
            "founder_x": "https://x.com/pending_lead",
        },
        merged,
    )
    assert routed3["phone"] == "+14155550103"
    assert routed3["wa_check_pending"] is True
    assert routed3.get("segment") != "social_manual"

    # Priority 4: Neither TG nor WA, icp_score >= 85 -> social_manual (X > LinkedIn > IG)
    routed_x = route_resolved_lead(
        {
            "phone": "+14155550104",
            "icp_score": 88,
            "brand_name": "Acme Health",
            "founder_name": "Jordan Lee",
            "founder_x": "@jordan_x",
            "founder_linkedin": "https://linkedin.com/in/jordan",
            "instagram_handle": "jordan_ig",
        },
        merged,
        language="en",
    )
    assert routed_x["segment"] == "social_manual"
    assert routed_x["manual_action_url"] == "https://x.com/jordan_x"
    assert "\n" not in routed_x["manual_draft_text"]
    assert "WhatsApp" in routed_x["manual_draft_text"]
    assert "Telegram" in routed_x["manual_draft_text"]
    assert "SoftUGC" in routed_x["manual_draft_text"]
    assert "Acme Health" in routed_x["manual_draft_text"]

    # LinkedIn beats IG when X is missing; RU bridge DM works
    routed_li = route_resolved_lead(
        {
            "icp_score": 85,
            "company": "Nordic Labs",
            "founder_linkedin": "linkedin.com/in/ivan",
            "instagram_handle": "nordic_ig",
        },
        {},
        language="ru",
    )
    assert routed_li["segment"] == "social_manual"
    assert routed_li["manual_action_url"] == "https://linkedin.com/in/ivan"
    assert "\n" not in routed_li["manual_draft_text"]
    assert "Telegram" in routed_li["manual_draft_text"]
    assert "WhatsApp" in routed_li["manual_draft_text"]
    assert "Nordic Labs" in routed_li["manual_draft_text"]

    # IG fallback when neither X nor LinkedIn exists
    routed_ig = route_resolved_lead(
        {"icp_score": 91, "instagram_handle": "@glow_brand"},
        {},
    )
    assert routed_ig["segment"] == "social_manual"
    assert routed_ig["manual_action_url"] == "https://instagram.com/glow_brand"

    # ICP < 85 does NOT enter social_manual
    routed_low_icp = route_resolved_lead(
        {"icp_score": 84, "founder_x": "https://x.com/small_biz"},
        {},
    )
    assert routed_low_icp.get("segment") != "social_manual"
    assert "manual_action_url" not in routed_low_icp

    # Existing Click-to-WA link stays WhatsApp and query numbers don't corrupt phone
    routed_existing_wa = route_resolved_lead(
        {
            "icp_score": 92,
            "whatsapp": "https://wa.me/14155550199?text=hello123",
            "founder_x": "https://x.com/already_wa",
        },
        {},
    )
    assert routed_existing_wa["provider"] == "whatsapp"
    assert routed_existing_wa["phone"] == "+14155550199"
    assert routed_existing_wa.get("segment") != "social_manual"
