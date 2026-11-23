#!/usr/bin/env python3
"""Layer-0 ($0) Telegram group/channel admin discovery for SoftUGC leadgen.

Extracts public ``t.me/<handle>`` brand group/channel candidates from lead rows,
fetches channel/supergroup admins via Telethon ``GetParticipantsRequest`` +
``ChannelParticipantsAdmins``, filters out bots, deleted accounts, and
support/moderation staff, and ranks the top personal decision-maker contacts.

Convention:
    All ``GroupAdminContact.username`` values are bare Telegram handles without
    a leading ``@`` (for example, ``"alice_founder"``), consistent with
    ``leadgen`` rows, and deduplicated case-insensitively.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

_ROW_GROUP_FIELDS: tuple[str, ...] = (
    "telegram_group",
    "community_url",
    "tg_group",
)
_ROW_TEXT_FIELDS: tuple[str, ...] = (
    "links",
    "project_url",
    "bio",
    "what_they_do",
    "sample",
)

_TG_URL_NOISE: frozenset[str] = frozenset(
    {
        "joinchat",
        "share",
        "addstickers",
        "addemoji",
        "addlist",
        "addtheme",
        "s",
        "c",
        "iv",
        "proxy",
        "socks",
        "login",
        "contact",
        "boost",
        "m",
        "nft",
        "gift",
        "gifts",
        "setlanguage",
        "bg",
        "confirmphone",
        "invoice",
        "auth",
        "k",
        "a",
        "z",
    }
)

_TG_URL_RE = re.compile(
    r"""(?<![A-Za-z0-9.\-])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([^\s"'<>\]\)]+)""",
    re.IGNORECASE,
)
_BARE_TG_HANDLE_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_DIRECT_GROUP_HANDLE_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{2,31})$")
_ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")

_CLAUSE_SPLIT_RE = re.compile(r"[\n|;,]|\.\s+")
_DM_PREFIX_RE = re.compile(
    r"\b(?:dm|dms|pm|pms|personal|private|direct(?:\s+message)?|"
    r"founder\s+tg|author\s+tg|my\s+tg|telegram\s+dm|tg\s+dm|"
    r"лс|личк[ауе]|личный(?:\s+тг)?|в\s+лс)\b",
    re.IGNORECASE,
)
_GROUP_PREFIX_RE = re.compile(
    r"\b(?:group|chat|channel|community|club|forum|official\s+tg|"
    r"tg\s+group|tg\s+chat|telegram\s+group|telegram\s+channel|join|"
    r"групп[ауе]|чат|канал|комьюнити|сообществ[оа])\b",
    re.IGNORECASE,
)
_DM_SUFFIX_RE = re.compile(
    r"^\s*(?:\(\s*(?:dm|pm|personal|private|лс|личный)\s*\)|"
    r"\[\s*(?:dm|pm|personal|private|лс)\s*\]|"
    r"[-–—:]\s*(?:dm|pm|personal|лс)\b)",
    re.IGNORECASE,
)
_DICT_DM_LABEL_RE = re.compile(r"\b(?:dm|pm|personal|private|direct|лс)\b", re.IGNORECASE)

# Support/mod/anti-spam keywords rejected in handles and admin ranks.
_NOISY_EXACT_TOKENS: frozenset[str] = frozenset(
    {
        "support",
        "help",
        "helpdesk",
        "mod",
        "mods",
        "moderator",
        "moderators",
        "moder",
        "guardian",
        "safeguard",
        "combot",
        "shield",
        "verify",
        "verification",
        "care",
        "service",
        "services",
        "antispam",
        "captcha",
        "ticket",
        "tickets",
        "concierge",
        "саппорт",
        "поддержка",
        "модер",
        "модератор",
    }
)
_NOISY_UNAMBIGUOUS_SUBSTRINGS: tuple[str, ...] = (
    "support",
    "helpdesk",
    "moderator",
    "guardian",
    "safeguard",
    "combot",
    "shield",
    "verify",
    "antispam",
    "captcha",
)
_NOISY_COMPOUND_HANDLE_RE = re.compile(
    r"(?:customercare|clientcare|customerservice|clientservice|"
    r"tgservice|acmeservice|chatmod|groupmod|tgmod|tghelp|acmehelp|"
    r"^(?:help|care|service|mod)[_-]|[_-](?:help|care|service|mod)$)",
    re.IGNORECASE,
)
_NOISY_ROSE_RE = re.compile(r"(?:^rose$|missrose|^rose[_-]|[_-]rose$)", re.IGNORECASE)

_ROLE_TIER_FOUNDER = "Founder / CEO / Owner"
_ROLE_TIER_MARKETING = "CMO / Marketing / Growth / UA"
_ROLE_TIER_BD = "BD / Partnerships / Head / Lead"
_ROLE_TIER_CREATOR = "Creator"
_ROLE_TIER_ADMIN = "Admin"

_FOUNDER_PHRASE_RE = re.compile(
    r"\b(?:co[- ]?founder|cofounder|founder|ceo|owner|chief\s+executive|"
    r"основател\w*|соосновател\w*|владел\w*|гендиректор\w*)\b",
    re.IGNORECASE,
)
_MARKETING_PHRASE_RE = re.compile(
    r"\b(?:cmo|marketing|growth|ua|user\s+acquisition|performance|brand|"
    r"smm|маркетинг\w*|маркетолог\w*|трафик\w*|продвижен\w*)\b",
    re.IGNORECASE,
)
_BD_PHRASE_RE = re.compile(
    r"\b(?:bd|bizdev|business\s+development|partnerships?|head|lead|"
    r"director|vp|chief|coo|cpo|cto|руководител\w*|директор\w*|партнерств\w*)\b",
    re.IGNORECASE,
)


@dataclass
class GroupAdminContact:
    """Ranked personal admin/founder contact extracted from a Telegram group.

    ``username`` is always stored as a bare handle without a leading ``@``.
    """

    username: str
    user_id: int | None = None
    first_name: str = ""
    last_name: str = ""
    admin_rank: str = ""
    role_guess: str = ""
    group_handle: str = ""


def _tokenize(text: str) -> list[str]:
    """Split identifiers, CamelCase, underscores, and words into lowercase tokens."""
    if not text:
        return []
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    return [tok.lower() for tok in re.findall(r"[A-Za-zА-Яа-яЁё]+", spaced) if tok]


def normalize_tg_group_handle(group_url_or_handle: str, *, allow_short: bool = False) -> str:
    """Normalize a Telegram group handle or URL to a bare handle or invite URL."""
    raw = str(group_url_or_handle or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip(".,;:!?)\"'>]")
    m = _TG_URL_RE.search(raw)
    if m:
        parsed, _is_invite = _parse_tme_capture(m.group(1), allow_short=allow_short)
        return parsed
    cleaned = raw.lstrip("@").strip("/")
    if not cleaned or cleaned.lower() in _TG_URL_NOISE or cleaned.lower().endswith("bot"):
        return ""
    pat = _DIRECT_GROUP_HANDLE_RE if allow_short else _BARE_TG_HANDLE_RE
    if pat.fullmatch(cleaned):
        return cleaned
    return ""


def _parse_tme_capture(raw_path: str, *, allow_short: bool = False) -> tuple[str, bool]:
    """Parse path after ``t.me/`` into ``(normalized_handle_or_invite, is_invite)``.

    Returns ``("", False)`` for noise/bot/DM links.
    """
    cleaned = raw_path.strip().rstrip(".,;:!?)\"'>]")
    path_and_query, _, _frag = cleaned.partition("#")
    path_only, _, query_str = path_and_query.partition("?")
    if query_str:
        qs = parse_qs(query_str, keep_blank_values=True)
        if any(k in qs for k in ("text", "start", "startgroup", "startapp")):
            return "", False

    segments = [seg for seg in path_only.strip("/").split("/") if seg]
    if not segments:
        return "", False

    first = segments[0]
    first_low = first.lower()

    if first.startswith("+"):
        invite_hash = first[1:]
        if _INVITE_HASH_RE.fullmatch(invite_hash) and re.search(r"[A-Za-z]", invite_hash):
            return f"https://t.me/+{invite_hash}", True
        return "", False

    if first_low == "joinchat":
        if len(segments) >= 2:
            invite_hash = segments[1]
            if _INVITE_HASH_RE.fullmatch(invite_hash) and re.search(r"[A-Za-z]", invite_hash):
                return f"https://t.me/joinchat/{invite_hash}", True
        return "", False

    if first_low == "s":
        if len(segments) < 2:
            return "", False
        handle = segments[1].lstrip("@")
    elif first_low in _TG_URL_NOISE:
        return "", False
    else:
        handle = first.lstrip("@")

    handle_low = handle.lower()
    if handle_low in _TG_URL_NOISE or handle_low.endswith("bot"):
        return "", False
    pat = _DIRECT_GROUP_HANDLE_RE if allow_short else _BARE_TG_HANDLE_RE
    if not pat.fullmatch(handle):
        return "", False
    return handle, False


def _is_explicitly_marked_personal_dm(text: str, start: int, end: int) -> bool:
    prefix = text[max(0, start - 64) : start]
    clause = _CLAUSE_SPLIT_RE.split(prefix)[-1]

    last_dm = -1
    for m in _DM_PREFIX_RE.finditer(clause):
        last_dm = m.end()
    last_group = -1
    for m in _GROUP_PREFIX_RE.finditer(clause):
        last_group = m.end()

    if last_dm >= 0 and last_dm > last_group:
        return True

    suffix = text[end : end + 28]
    if _DM_SUFFIX_RE.match(suffix):
        return True
    return False


def _iter_field_strings(value: Any, *, allow_bare_handle: bool) -> Iterable[tuple[str, bool]]:
    """Yield ``(text, is_explicit_dm_labeled)`` tuples from flexible row field values."""
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if text.startswith("[") and text.endswith("]"):
            try:
                loaded = json.loads(text)
            except ValueError:
                loaded = None
            if isinstance(loaded, list):
                yield from _iter_field_strings(loaded, allow_bare_handle=allow_bare_handle)
                return
        yield text, False
        return
    if isinstance(value, Mapping):
        label_blob = " ".join(
            str(value.get(k) or "") for k in ("type", "kind", "label", "rel", "role")
        )
        is_dm = bool(_DICT_DM_LABEL_RE.search(label_blob))
        for k in ("url", "href", "link", "handle", "username", "value"):
            sub = value.get(k)
            if isinstance(sub, str) and sub.strip():
                yield sub.strip(), is_dm
        return
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_field_strings(item, allow_bare_handle=allow_bare_handle)


def extract_tg_group_handles_from_row(row: Mapping[str, Any]) -> list[str]:
    """Scan lead row fields for public ``t.me/<handle>`` group/channel candidates.

    Scans ``telegram_group``, ``community_url``, ``tg_group``, ``links``,
    ``project_url``, ``bio``, ``what_they_do``, and ``sample``. Excludes
    explicitly marked personal DMs, Telegram utility/noise paths (``joinchat``,
    ``share``, ``addstickers``, ``s``, ``c``, ``proxy``), phone links, and
    ``*bot`` handles. Invite links (``t.me/+<hash>``, ``t.me/joinchat/<hash>``)
    are preserved as normalized ``https://t.me/...`` URLs; public handles are
    returned as bare handles without ``@``.
    """
    if not row or not isinstance(row, Mapping):
        return []

    found: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        if not candidate:
            return
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            found.append(candidate)

    for field in _ROW_GROUP_FIELDS:
        for text, is_dm_labeled in _iter_field_strings(row.get(field), allow_bare_handle=True):
            if is_dm_labeled:
                continue
            matched_url = False
            for m in _TG_URL_RE.finditer(text):
                matched_url = True
                if _is_explicitly_marked_personal_dm(text, m.start(), m.end()):
                    continue
                cand, _ = _parse_tme_capture(m.group(1))
                _add(cand)
            if not matched_url:
                bare = text.strip().lstrip("@")
                if (
                    bare
                    and bare.lower() not in _TG_URL_NOISE
                    and not bare.lower().endswith("bot")
                    and _BARE_TG_HANDLE_RE.fullmatch(bare)
                ):
                    _add(bare)

    for field in _ROW_TEXT_FIELDS:
        for text, is_dm_labeled in _iter_field_strings(row.get(field), allow_bare_handle=False):
            if is_dm_labeled:
                continue
            for m in _TG_URL_RE.finditer(text):
                if _is_explicitly_marked_personal_dm(text, m.start(), m.end()):
                    continue
                cand, _ = _parse_tme_capture(m.group(1))
                _add(cand)

    return found


def _extract_username_from_user_obj(user: Any) -> str:
    if isinstance(user, Mapping):
        raw = str(user.get("username") or "").strip()
    else:
        raw = str(getattr(user, "username", None) or "").strip()
        if not raw:
            for item in getattr(user, "usernames", None) or []:
                if getattr(item, "active", True) and getattr(item, "username", None):
                    raw = str(item.username).strip()
                    break
    if not raw:
        return ""
    if "t.me/" in raw.lower() or "telegram.me/" in raw.lower():
        m = _TG_URL_RE.search(raw)
        if m:
            cand, is_inv = _parse_tme_capture(m.group(1))
            return "" if is_inv else cand
        return ""
    clean = raw.lstrip("@").strip()
    if not _ADMIN_USERNAME_RE.fullmatch(clean):
        return ""
    return clean


def _coerce_user_id(raw_id: Any) -> int | None:
    if raw_id is None or isinstance(raw_id, bool):
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _is_creator_participant(participant: Any, container: Any) -> bool:
    for obj in (participant, container):
        if obj is None:
            continue
        if isinstance(obj, Mapping):
            if bool(obj.get("is_creator") or obj.get("creator")):
                return True
            ptype = str(obj.get("participant_type") or obj.get("role") or "").strip().lower()
            if ptype == "creator":
                return True
        else:
            if type(obj).__name__ == "ChannelParticipantCreator":
                return True
            if bool(getattr(obj, "is_creator", False)):
                return True
    return False


def _extract_participant_record(item: Any) -> dict[str, Any] | None:
    """Normalize a Telethon User, ``(user, participant)`` tuple, or dict."""
    if item is None:
        return None

    user_obj = item
    part_obj = None

    if isinstance(item, tuple) and len(item) == 2:
        user_obj, part_obj = item
    elif isinstance(item, Mapping):
        if "user" in item and isinstance(item.get("user"), (Mapping, object)) and not isinstance(
            item.get("user"), (str, bytes)
        ):
            user_obj = item["user"]
        part_obj = item.get("participant")
    elif hasattr(item, "user") and not hasattr(item, "username") and not hasattr(item, "first_name"):
        user_obj = getattr(item, "user")
        part_obj = getattr(item, "participant", None)
    else:
        part_obj = getattr(item, "participant", None)

    if user_obj is None:
        return None

    def _get(obj: Any, *keys: str, default: Any = None) -> Any:
        for k in keys:
            if isinstance(obj, Mapping):
                if k in obj and obj[k] is not None:
                    return obj[k]
            else:
                if hasattr(obj, k):
                    val = getattr(obj, k)
                    if val is not None:
                        return val
        return default

    is_bot = bool(_get(user_obj, "is_bot", "bot", default=False)) or bool(
        _get(item, "is_bot", "bot", default=False)
    )
    is_deleted = bool(_get(user_obj, "deleted", "is_deleted", default=False)) or bool(
        _get(item, "deleted", "is_deleted", default=False)
    )
    is_tg_flagged = bool(
        _get(user_obj, "support", default=False)
        or _get(user_obj, "scam", default=False)
        or _get(user_obj, "fake", default=False)
    )

    username = _extract_username_from_user_obj(user_obj)
    if not username and item is not user_obj:
        username = _extract_username_from_user_obj(item)

    user_id = _coerce_user_id(_get(user_obj, "id", "user_id"))
    if user_id is None and item is not user_obj:
        user_id = _coerce_user_id(_get(item, "user_id", "id"))

    first_name = str(_get(user_obj, "first_name", default="") or "").strip()
    last_name = str(_get(user_obj, "last_name", default="") or "").strip()
    if not first_name and not last_name:
        full_name = str(_get(user_obj, "name", "full_name", default="") or "").strip()
        if full_name:
            parts = full_name.split(None, 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ""

    rank_raw = _get(part_obj, "rank", "admin_rank", "title", default="") if part_obj is not None else ""
    if not rank_raw:
        rank_raw = _get(user_obj, "admin_rank", "rank", "title", default="")
    if not rank_raw and item is not user_obj:
        rank_raw = _get(item, "admin_rank", "rank", "title", default="")
    admin_rank = str(rank_raw or "").strip()

    bio = str(_get(user_obj, "bio", "about", default="") or "").strip()
    if not bio and item is not user_obj:
        bio = str(_get(item, "bio", "about", default="") or "").strip()

    is_creator = _is_creator_participant(part_obj, user_obj if item is user_obj else item)

    return {
        "username": username,
        "user_id": user_id,
        "first_name": first_name,
        "last_name": last_name,
        "admin_rank": admin_rank,
        "bio": bio,
        "is_bot": is_bot,
        "is_deleted": is_deleted,
        "is_tg_flagged": is_tg_flagged,
        "is_creator": is_creator,
    }


def _has_exec_role(text: str) -> bool:
    return bool(
        text
        and (
            _FOUNDER_PHRASE_RE.search(text)
            or _MARKETING_PHRASE_RE.search(text)
            or _BD_PHRASE_RE.search(text)
        )
    )


def _is_noisy_support_or_mod(
    username: str,
    admin_rank: str,
    first_name: str,
    last_name: str,
) -> bool:
    uname_low = username.lower()
    if uname_low.endswith("bot"):
        return True

    rank_low = admin_rank.lower()
    uname_tokens = set(_tokenize(username))
    rank_tokens = set(_tokenize(admin_rank))

    if uname_tokens & _NOISY_EXACT_TOKENS or rank_tokens & _NOISY_EXACT_TOKENS:
        return True

    for substr in _NOISY_UNAMBIGUOUS_SUBSTRINGS:
        if substr in uname_low or substr in rank_low:
            return True

    if _NOISY_COMPOUND_HANDLE_RE.search(uname_low) and not _has_exec_role(admin_rank):
        return True
    if "service" in rank_tokens or (
        "service" in uname_low and not _has_exec_role(admin_rank)
    ):
        return True

    if "rose" in rank_tokens:
        return True
    if (_NOISY_ROSE_RE.search(uname_low) or "rose" in uname_tokens) and not _has_exec_role(
        admin_rank
    ):
        return True

    full_name_tokens = set(_tokenize(f"{first_name} {last_name}"))
    if full_name_tokens & {
        "support",
        "helpdesk",
        "moderator",
        "guardian",
        "safeguard",
        "combot",
        "antispam",
        "captcha",
        "bot",
    }:
        return True

    return False


def _classify_role_and_score(
    *,
    admin_rank: str,
    username: str,
    bio: str,
    is_creator: bool,
    first_name: str,
) -> tuple[str, tuple[int, int, int, int]]:
    """Return ``(role_guess, sort_key_desc)`` following strict priority tiers."""
    sources: list[tuple[int, str]] = []
    if admin_rank:
        sources.append((2, admin_rank))
    if bio:
        sources.append((1, bio))
    uname_spaced = " ".join(_tokenize(username))
    if uname_spaced:
        sources.append((0, uname_spaced))

    best_tier = 0
    best_source_weight = -1
    best_role = _ROLE_TIER_ADMIN

    for source_weight, text in sources:
        if _FOUNDER_PHRASE_RE.search(text):
            tier, role = 5, _ROLE_TIER_FOUNDER
        elif _MARKETING_PHRASE_RE.search(text):
            tier, role = 4, _ROLE_TIER_MARKETING
        elif _BD_PHRASE_RE.search(text):
            tier, role = 3, _ROLE_TIER_BD
        else:
            continue
        if (tier, source_weight) > (best_tier, best_source_weight):
            best_tier = tier
            best_source_weight = source_weight
            best_role = role

    if best_tier == 0:
        if is_creator:
            best_tier = 2
            best_source_weight = 1
            best_role = _ROLE_TIER_CREATOR
        else:
            best_tier = 1
            best_source_weight = 0
            best_role = _ROLE_TIER_ADMIN

    sort_key = (
        best_tier,
        best_source_weight,
        1 if is_creator else 0,
        1 if first_name else 0,
    )
    return best_role, sort_key


def filter_and_rank_admins(
    participants: Iterable[Any],
    group_handle: str,
    *,
    max_results: int = 2,
) -> list[GroupAdminContact]:
    """Filter group admin participants and rank the best personal DM contacts.

    Accepts Telethon ``User`` objects (with optional ``.participant`` or
    ``(user, participant)`` tuples) as well as plain dicts used in unit tests.
    Rejects bots, deleted accounts, ``*bot`` handles, and support/mod accounts.

    Ranking order:
        1. ``Founder / Co-founder / CEO / Owner``
        2. ``CMO / Marketing / Growth / UA``
        3. ``BD / Partnerships / Head / Lead``
        4. ``Creator`` without a noisy rank
        5. General ``Admin``

    Returns up to ``max_results`` ``GroupAdminContact`` entries with bare
    ``username`` values (no leading ``@``).
    """
    if max_results <= 0 or participants is None:
        return []

    norm_group = normalize_tg_group_handle(group_handle, allow_short=True) or str(
        group_handle or ""
    ).strip().lstrip("@")
    candidates: list[tuple[tuple[int, int, int, int, int], GroupAdminContact]] = []

    for idx, item in enumerate(participants):
        rec = _extract_participant_record(item)
        if rec is None:
            continue
        if rec["is_bot"] or rec["is_deleted"] or rec["is_tg_flagged"]:
            continue
        username = rec["username"]
        if not username:
            continue
        if _is_noisy_support_or_mod(
            username,
            rec["admin_rank"],
            rec["first_name"],
            rec["last_name"],
        ):
            continue

        role_guess, score_tuple = _classify_role_and_score(
            admin_rank=rec["admin_rank"],
            username=username,
            bio=rec["bio"],
            is_creator=rec["is_creator"],
            first_name=rec["first_name"],
        )
        contact = GroupAdminContact(
            username=username,
            user_id=rec["user_id"],
            first_name=rec["first_name"],
            last_name=rec["last_name"],
            admin_rank=rec["admin_rank"],
            role_guess=role_guess,
            group_handle=norm_group,
        )
        # Negative index ensures deterministic tie-breaking preserving input order.
        candidates.append(((*score_tuple, -idx), contact))

    candidates.sort(key=lambda pair: pair[0], reverse=True)

    out: list[GroupAdminContact] = []
    seen_usernames: set[str] = set()
    for _, contact in candidates:
        key = contact.username.lower()
        if key in seen_usernames:
            continue
        seen_usernames.add(key)
        out.append(contact)
        if len(out) >= max_results:
            break
    return out


async def extract_brand_tg_admins(
    client: Any,
    group_url_or_handle: str,
    *,
    max_admins: int = 10,
    max_results: int = 2,
) -> list[GroupAdminContact]:
    """Fetch and rank top personal admin contacts for a Telegram group/channel.

    Lazily imports Telethon ``GetParticipantsRequest`` and
    ``ChannelParticipantsAdmins`` so importing this module requires no live
    Telegram connection. Handles ``ChatAdminRequiredError``,
    ``ChannelPrivateError``, and ``ValueError`` gracefully by returning ``[]``.
    """
    if client is None or max_admins <= 0 or max_results <= 0:
        return []

    target = normalize_tg_group_handle(group_url_or_handle, allow_short=True)
    if not target:
        return []

    try:
        from telethon.errors import (
            ChannelPrivateError,
            ChatAdminRequiredError,
            FloodWaitError,
            RPCError,
            UsernameInvalidError,
            UsernameNotOccupiedError,
        )
        from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest
        from telethon.tl.types import ChannelParticipantsAdmins, PeerChannel
    except ImportError as exc:
        logger.debug("Telethon unavailable in extract_brand_tg_admins: %s", exc)
        return []

    def _parse_participants_response(resp: Any, group_label: str) -> list[GroupAdminContact]:
        raw_users = getattr(resp, "users", None)
        if raw_users is not None:
            raw_parts = getattr(resp, "participants", None) or []
            parts_by_uid: dict[int, Any] = {}
            for part in raw_parts:
                uid = getattr(part, "user_id", None)
                if uid is not None:
                    parts_by_uid[int(uid)] = part

            paired: list[Any] = []
            for user in raw_users:
                uid = getattr(user, "id", None)
                if uid is not None and int(uid) in parts_by_uid:
                    paired.append((user, parts_by_uid[int(uid)]))
                else:
                    paired.append(user)
        elif isinstance(resp, Iterable) and not isinstance(resp, (str, bytes, Mapping)):
            paired = list(resp)
        else:
            paired = []
        return filter_and_rank_admins(paired, group_label, max_results=max_results)

    async def _extract_from_full_about(entity: Any) -> list[GroupAdminContact]:
        try:
            full = await client(GetFullChannelRequest(channel=entity))
        except (
            ChatAdminRequiredError,
            ChannelPrivateError,
            UsernameInvalidError,
            UsernameNotOccupiedError,
            FloodWaitError,
            RPCError,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            logger.debug("GetFullChannelRequest(%s) unavailable: %s", target, exc)
            return []

        about = str(getattr(getattr(full, "full_chat", None), "about", None) or getattr(full, "about", None) or "").strip()
        about_contacts: list[GroupAdminContact] = []
        if about:
            candidates: list[str] = []
            seen_cands: set[str] = {target.lower()}
            for m in _TG_URL_RE.finditer(about):
                seg = m.group(1).split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].lstrip("@")
                if _ADMIN_USERNAME_RE.fullmatch(seg) and seg.lower() not in seen_cands:
                    seen_cands.add(seg.lower())
                    candidates.append(seg)
            for cand in re.findall(r"(?<![A-Za-z0-9._])@([A-Za-z][A-Za-z0-9_]{3,31})", about):
                if cand.lower() not in seen_cands:
                    seen_cands.add(cand.lower())
                    candidates.append(cand)

            resolved_users: set[str] = set()
            for cand in candidates[: min(6, max(1, int(max_admins)))]:
                if cand.lower() in _TG_URL_NOISE or cand.lower().endswith(("bot", "_bot")):
                    continue
                if _is_noisy_support_or_mod(cand, "", "", ""):
                    continue
                try:
                    ent = await client.get_entity(cand)
                except (
                    FloodWaitError,
                ):
                    break
                except (
                    ChannelPrivateError,
                    UsernameInvalidError,
                    UsernameNotOccupiedError,
                    RPCError,
                    ValueError,
                    TypeError,
                ):
                    continue
                if ent is None or type(ent).__name__ != "User":
                    continue
                if getattr(ent, "bot", False) or getattr(ent, "deleted", False) or getattr(ent, "scam", False) or getattr(ent, "fake", False):
                    continue
                resolved_u = str(getattr(ent, "username", None) or cand).strip().lstrip("@")
                if not resolved_u or _is_noisy_support_or_mod(resolved_u, "", "", ""):
                    continue
                low_u = resolved_u.lower()
                if low_u in resolved_users:
                    continue
                resolved_users.add(low_u)
                about_contacts.append(
                    GroupAdminContact(
                        username=resolved_u,
                        user_id=int(ent.id) if getattr(ent, "id", None) is not None else None,
                        first_name=str(getattr(ent, "first_name", "") or "").strip(),
                        last_name=str(getattr(ent, "last_name", "") or "").strip(),
                        admin_rank="Channel About",
                        role_guess="Channel Contact",
                        group_handle=target,
                    )
                )
                if len(about_contacts) >= max_results:
                    break

        linked_contacts: list[GroupAdminContact] = []
        raw_linked_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
        try:
            linked_chat_id = int(raw_linked_id) if raw_linked_id else 0
        except (TypeError, ValueError):
            linked_chat_id = 0
        if linked_chat_id > 0:
            try:
                linked_entity = await client.get_entity(PeerChannel(linked_chat_id))
                if linked_entity is not None:
                    linked_req = GetParticipantsRequest(
                        channel=linked_entity,
                        filter=ChannelParticipantsAdmins(),
                        offset=0,
                        limit=max(1, int(max_admins)),
                        hash=0,
                    )
                    linked_resp = await client(linked_req)
                    linked_label = str(getattr(linked_entity, "username", None) or target).strip().lstrip("@")
                    linked_contacts = _parse_participants_response(linked_resp, linked_label)
            except (
                ChatAdminRequiredError,
                ChannelPrivateError,
                UsernameInvalidError,
                UsernameNotOccupiedError,
                FloodWaitError,
                RPCError,
                ValueError,
                TypeError,
            ) as exc:
                logger.debug("linked_chat_id(%s) admins unavailable: %s", linked_chat_id, exc)

        if not linked_contacts:
            return about_contacts[:max_results]
        merged: list[GroupAdminContact] = []
        seen_merged: set[str] = set()
        for contact in (*linked_contacts, *about_contacts):
            key = contact.username.lower()
            if key in seen_merged:
                continue
            seen_merged.add(key)
            merged.append(contact)
            if len(merged) >= max_results:
                break
        return merged

    try:
        entity = await client.get_entity(target)
        if entity is None:
            return []
        if type(entity).__name__ == "User":
            return []

        request = GetParticipantsRequest(
            channel=entity,
            filter=ChannelParticipantsAdmins(),
            offset=0,
            limit=max(1, int(max_admins)),
            hash=0,
        )
        try:
            response = await client(request)
        except ChatAdminRequiredError:
            return await _extract_from_full_about(entity)
    except (
        ChatAdminRequiredError,
        ChannelPrivateError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
        FloodWaitError,
        RPCError,
        ValueError,
        TypeError,
    ) as exc:
        logger.debug("extract_brand_tg_admins(%s) unavailable: %s", target, exc)
        return []

    contacts = _parse_participants_response(response, target)
    if not contacts and (getattr(entity, "broadcast", False) or not getattr(entity, "megagroup", False)):
        return await _extract_from_full_about(entity)
    return contacts


__all__ = [
    "GroupAdminContact",
    "extract_brand_tg_admins",
    "extract_tg_group_handles_from_row",
    "filter_and_rank_admins",
    "normalize_tg_group_handle",
]
