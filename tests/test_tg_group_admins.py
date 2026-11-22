from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    ChatAdminRights,
    User,
    Username,
)
from telethon.tl.types.channels import ChannelParticipants

from leadgen.tg_group_admins import (
    GroupAdminContact,
    extract_brand_tg_admins,
    extract_tg_group_handles_from_row,
    filter_and_rank_admins,
    normalize_tg_group_handle,
)


def _make_tg_user(
    user_id: int,
    *,
    username: str | None = None,
    usernames: list[Username] | None = None,
    first_name: str = "",
    last_name: str = "",
    bot: bool = False,
    deleted: bool = False,
    support: bool = False,
    scam: bool = False,
    fake: bool = False,
) -> User:
    return User(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        usernames=usernames,
        bot=bot,
        deleted=deleted,
        support=support,
        scam=scam,
        fake=fake,
    )


def _make_admin_rights() -> ChatAdminRights:
    return ChatAdminRights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        add_admins=True,
        manage_call=True,
    )


def _make_channel(channel_id: int = 1001, title: str = "Acme Community", username: str = "acme_chat") -> Channel:
    return Channel(
        id=channel_id,
        title=title,
        photo=None,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        megagroup=True,
        username=username,
        access_hash=424242,
    )


class _FakeTelethonClient:
    def __init__(
        self,
        *,
        entity: Any = None,
        entity_error: Exception | None = None,
        response: Any = None,
        call_error: Exception | None = None,
    ) -> None:
        self._entity = entity
        self._entity_error = entity_error
        self._response = response
        self._call_error = call_error
        self.entity_queries: list[Any] = []
        self.requests: list[Any] = []

    async def get_entity(self, target: Any) -> Any:
        self.entity_queries.append(target)
        if self._entity_error is not None:
            raise self._entity_error
        return self._entity

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if self._call_error is not None:
            raise self._call_error
        return self._response


def test_extract_tg_group_handles_scans_all_fields_and_dedups() -> None:
    row = {
        "telegram_group": "@Primary_Group",
        "community_url": "https://t.me/s/News_Channel/42",
        "tg_group": "tg_chat_only",
        "links": [
            "https://t.me/primary_group",
            "https://t.me/acme_community/99",
            "https://telegram.me/@alt_group",
            "https://t.me/+AbCdEf12345_x",
            "https://t.me/joinchat/ZyXwVu98765",
        ],
        "project_url": "https://t.me/project_hub",
        "bio": "Building tools. Join us at t.me/bio_group!",
        "what_they_do": "Official group: https://t.me/what_group",
        "sample": "Check telegram.dog/sample_chat for updates",
    }

    handles = extract_tg_group_handles_from_row(row)
    assert handles == [
        "Primary_Group",
        "News_Channel",
        "tg_chat_only",
        "acme_community",
        "alt_group",
        "https://t.me/+AbCdEf12345_x",
        "https://t.me/joinchat/ZyXwVu98765",
        "project_hub",
        "bio_group",
        "what_group",
        "sample_chat",
    ]


def test_extract_tg_group_handles_filters_noise_bots_and_explicit_dms() -> None:
    row = {
        "telegram_group": "https://t.me/acme_helper_bot",
        "links": [
            "https://t.me/joinchat",
            "https://t.me/share/url?url=https://acme.io",
            "https://t.me/addstickers/CoolPack",
            "https://t.me/s/",
            "https://t.me/c/123456789/12",
            "https://t.me/proxy?server=1.2.3.4",
            "https://t.me/acme_bot",
            "https://t.me/my_support_BOT",
            "https://t.me/+14155551234",
            "https://t.me/alice_dm?text=hello",
            "https://get.me/not_telegram",
            {"type": "dm", "url": "https://t.me/founder_direct"},
            {"type": "community", "url": "https://t.me/real_brand_chat"},
        ],
        "bio": (
            "DM me: https://t.me/bob_personal | "
            "Personal: t.me/carol_private, "
            "or join our group: https://t.me/brand_official. "
            "Direct: https://t.me/dan_founder (DM)"
        ),
        "sample": "DM me or join our community: https://t.me/community_ok; ЛС: t.me/ivan_lichka",
    }

    handles = extract_tg_group_handles_from_row(row)
    assert handles == ["real_brand_chat", "brand_official", "community_ok"]


def test_extract_tg_group_handles_handles_string_and_json_links() -> None:
    assert extract_tg_group_handles_from_row({}) == []
    assert extract_tg_group_handles_from_row(
        {"links": '["https://t.me/one_chat", "https://t.me/two_bot"]'}
    ) == ["one_chat"]
    assert extract_tg_group_handles_from_row(
        {"links": "https://t.me/alpha_group, https://t.me/beta_group"}
    ) == ["alpha_group", "beta_group"]


def test_normalize_tg_group_handle() -> None:
    assert normalize_tg_group_handle("https://t.me/s/acme_group/") == "acme_group"
    assert normalize_tg_group_handle("@acme_group") == "acme_group"
    assert normalize_tg_group_handle("https://t.me/acme_group/55") == "acme_group"
    assert normalize_tg_group_handle("https://t.me/+AbCdEf123") == "https://t.me/+AbCdEf123"
    assert normalize_tg_group_handle("https://t.me/acme_bot") == ""
    assert normalize_tg_group_handle("https://t.me/joinchat") == ""
    assert normalize_tg_group_handle("https://t.me/c/999/1") == ""


def test_filter_and_rank_admins_rejects_bots_deleted_and_support_mod_handles() -> None:
    participants = [
        {"username": "helper_bot", "is_bot": True, "rank": "CEO"},
        {"username": "ghost_user", "deleted": True, "rank": "Founder"},
        {"username": "GroupGuardBot", "rank": "Admin"},
        {"username": "acme_support", "rank": "Admin"},
        {"username": "help_desk_team", "rank": "Help"},
        {"username": "john_mod", "rank": "Moderator"},
        {"username": "safeguard", "rank": ""},
        {"username": "guardian_tg", "rank": ""},
        {"username": "rose", "first_name": "Rose"},
        {"username": "combot", "rank": ""},
        {"username": "shield_verify", "rank": ""},
        {"username": "customer_care", "rank": ""},
        {"username": "client_service", "rank": ""},
        {"username": "alex_ok", "rank": "Support"},
        {"username": "maria_ok", "rank": "Chat Mod"},
        {"username": "pramod_ceo", "first_name": "Pramod", "rank": "CEO"},
        {"username": "rose_smith", "first_name": "Rose", "last_name": "Smith", "rank": "Founder & CEO"},
        {"username": "modern_cmo", "first_name": "Sam", "rank": "CMO"},
    ]

    ranked = filter_and_rank_admins(participants, "@acme_group", max_results=10)
    assert [c.username for c in ranked] == ["pramod_ceo", "rose_smith", "modern_cmo"]
    assert all(c.group_handle == "acme_group" for c in ranked)


def test_filter_and_rank_admins_enforces_exact_tier_hierarchy_and_max_results() -> None:
    participants = [
        {"username": "@plain_admin", "user_id": 10, "first_name": "Dan", "rank": ""},
        {"username": "silent_creator", "user_id": 11, "first_name": "Eve", "is_creator": True, "rank": ""},
        {"username": "bd_nick", "user_id": 12, "first_name": "Nick", "rank": "Head of Partnerships"},
        {"username": "growth_kate", "user_id": 13, "first_name": "Kate", "rank": "Head of Growth"},
        {"username": "founder_leo", "user_id": 14, "first_name": "Leo", "last_name": "Kim", "rank": "Co-Founder & CEO"},
    ]

    top_all = filter_and_rank_admins(participants, "https://t.me/acme_group", max_results=10)
    assert [c.username for c in top_all] == [
        "founder_leo",
        "growth_kate",
        "bd_nick",
        "silent_creator",
        "plain_admin",
    ]
    assert top_all[0].role_guess == "Founder / CEO / Owner"
    assert top_all[1].role_guess == "CMO / Marketing / Growth / UA"
    assert top_all[2].role_guess == "BD / Partnerships / Head / Lead"
    assert top_all[3].role_guess == "Creator"
    assert top_all[4].role_guess == "Admin"

    top_two = filter_and_rank_admins(participants, "acme_group")
    assert len(top_two) == 2
    assert [c.username for c in top_two] == ["founder_leo", "growth_kate"]


def test_filter_and_rank_admins_telethon_users_and_fragment_usernames() -> None:
    rights = _make_admin_rights()
    creator_user = _make_tg_user(101, username="alice_creator", first_name="Alice", last_name="Vance")
    creator_part = ChannelParticipantCreator(user_id=101, admin_rights=rights, rank="")

    cmo_user = _make_tg_user(
        102,
        username=None,
        usernames=[Username(username="bob_growth", editable=True, active=True)],
        first_name="Bob",
        last_name="Lee",
    )
    cmo_user.participant = ChannelParticipantAdmin(
        user_id=102,
        promoted_by=101,
        date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        admin_rights=rights,
        rank="VP Marketing / UA",
    )

    bot_user = _make_tg_user(103, username="shield_helper", bot=True, first_name="Shield")
    bot_part = ChannelParticipantAdmin(
        user_id=103,
        promoted_by=101,
        date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        admin_rights=rights,
        rank="Founder",
    )

    no_username_user = _make_tg_user(104, username=None, first_name="Hidden")
    no_username_part = ChannelParticipantAdmin(
        user_id=104,
        promoted_by=101,
        date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        admin_rights=rights,
        rank="CEO",
    )

    ranked = filter_and_rank_admins(
        [(creator_user, creator_part), cmo_user, (bot_user, bot_part), (no_username_user, no_username_part)],
        "acme_chat",
        max_results=2,
    )
    assert [c.username for c in ranked] == ["bob_growth", "alice_creator"]
    assert ranked[0] == GroupAdminContact(
        username="bob_growth",
        user_id=102,
        first_name="Bob",
        last_name="Lee",
        admin_rank="VP Marketing / UA",
        role_guess="CMO / Marketing / Growth / UA",
        group_handle="acme_chat",
    )
    assert ranked[1].role_guess == "Creator"


def test_extract_brand_tg_admins_happy_path_with_telethon_types() -> None:
    rights = _make_admin_rights()
    channel = _make_channel()
    u_founder = _make_tg_user(1, username="max_owner", first_name="Max", last_name="Stone")
    u_bd = _make_tg_user(2, username="lena_bd", first_name="Lena", last_name="Kerr")
    u_mod = _make_tg_user(3, username="chat_mod", first_name="Mod")
    u_bot = _make_tg_user(4, username="safeguard_bot", bot=True, first_name="Safeguard")

    response = ChannelParticipants(
        count=4,
        participants=[
            ChannelParticipantCreator(user_id=1, admin_rights=rights, rank="Owner / Founder"),
            ChannelParticipantAdmin(
                user_id=2,
                promoted_by=1,
                date=datetime(2026, 3, 1, tzinfo=timezone.utc),
                admin_rights=rights,
                rank="BD Lead",
            ),
            ChannelParticipantAdmin(
                user_id=3,
                promoted_by=1,
                date=datetime(2026, 3, 1, tzinfo=timezone.utc),
                admin_rights=rights,
                rank="Moderator",
            ),
            ChannelParticipantAdmin(
                user_id=4,
                promoted_by=1,
                date=datetime(2026, 3, 1, tzinfo=timezone.utc),
                admin_rights=rights,
                rank="Verify",
            ),
        ],
        chats=[channel],
        users=[u_mod, u_bot, u_bd, u_founder],
    )

    client = _FakeTelethonClient(entity=channel, response=response)
    contacts = asyncio.run(
        extract_brand_tg_admins(
            client,
            "https://t.me/acme_chat",
            max_admins=12,
            max_results=2,
        )
    )

    assert client.entity_queries == ["acme_chat"]
    assert len(client.requests) == 1
    req = client.requests[0]
    assert isinstance(req, GetParticipantsRequest)
    assert isinstance(req.filter, ChannelParticipantsAdmins)
    assert req.limit == 12
    assert [c.username for c in contacts] == ["max_owner", "lena_bd"]
    assert contacts[0].admin_rank == "Owner / Founder"
    assert contacts[0].role_guess == "Founder / CEO / Owner"
    assert contacts[1].role_guess == "BD / Partnerships / Head / Lead"


@pytest.mark.parametrize(
    "exc",
    [
        ChatAdminRequiredError(request=None),
        ChannelPrivateError(request=None),
        ValueError("Cannot find any entity corresponding to 'unknown_group'"),
        UsernameNotOccupiedError(request=None),
        FloodWaitError(request=None, capture=15),
    ],
)
def test_extract_brand_tg_admins_handles_telethon_and_value_errors_gracefully(
    exc: Exception,
) -> None:
    channel = _make_channel()
    client_on_call = _FakeTelethonClient(entity=channel, call_error=exc)
    assert asyncio.run(extract_brand_tg_admins(client_on_call, "acme_chat")) == []

    client_on_entity = _FakeTelethonClient(entity_error=exc)
    assert asyncio.run(extract_brand_tg_admins(client_on_entity, "acme_chat")) == []


def test_extract_brand_tg_admins_guards_empty_user_and_noise_inputs() -> None:
    assert asyncio.run(extract_brand_tg_admins(None, "acme_chat")) == []
    client = _FakeTelethonClient(entity=_make_tg_user(99, username="alice"))
    assert asyncio.run(extract_brand_tg_admins(client, "alice")) == []
    assert client.requests == []

    assert asyncio.run(extract_brand_tg_admins(client, "https://t.me/some_bot")) == []
    assert asyncio.run(extract_brand_tg_admins(client, "")) == []
    assert asyncio.run(extract_brand_tg_admins(client, "acme_chat", max_admins=0)) == []
    assert asyncio.run(extract_brand_tg_admins(client, "acme_chat", max_results=0)) == []

    # Handles plain object response gracefully without crashing
    plain_client = _FakeTelethonClient(entity=SimpleNamespace(id=55, title="Test"), response=[])
    assert asyncio.run(extract_brand_tg_admins(plain_client, "test_group")) == []


def test_tg_group_admins_lazy_telethon_import(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure module works cleanly for pure Layer-0 helpers even if telethon is unavailable.
    monkeypatch.setitem(sys.modules, "telethon", None)
    monkeypatch.setitem(sys.modules, "telethon.errors", None)
    monkeypatch.setitem(sys.modules, "telethon.tl", None)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions", None)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions.channels", None)
    monkeypatch.setitem(sys.modules, "telethon.tl.types", None)

    assert extract_tg_group_handles_from_row({"links": ["https://t.me/acme_group"]}) == ["acme_group"]
    assert [
        c.username
        for c in filter_and_rank_admins(
            [{"username": "jane_ceo", "rank": "CEO"}],
            "acme_group",
        )
    ] == ["jane_ceo"]
    client = _FakeTelethonClient(entity=SimpleNamespace(id=1, title="Group"), response=[])
    assert asyncio.run(extract_brand_tg_admins(client, "acme_group")) == []
