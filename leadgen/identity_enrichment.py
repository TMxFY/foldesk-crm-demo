#!/usr/bin/env python3
"""Conservative public-identity -> Telegram enrichment.

This module only discovers and verifies *public* Telegram contacts.  It does
not scrape arbitrary Telegram users and it never treats a string similarity as
proof of identity.  The intended pipeline is::

    public source row -> extract handles -> rank exact candidates
    -> Telethon exact-username lookup -> Jev/TypeSafe lead qualification

The TypeSafe step belongs to the caller because identity and commercial fit are
separate judgments.  ``candidate_state`` provides a stable, compact state for
that Jev judgment.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit


_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_PLATFORM_HOSTS = {
    "x": {"x.com", "twitter.com"},
    "github": {"github.com"},
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "youtube": {"youtube.com", "www.youtube.com", "youtu.be"},
    "tradingview": {"tradingview.com", "www.tradingview.com"},
    "mql5": {"mql5.com", "www.mql5.com"},
}
_SOCIAL_FIELDS = {
    "x": "x", "twitter": "x", "twitter_url": "x", "founder_twitter": "x",
    "github": "github", "github_url": "github", "founder_github": "github",
    "linkedin": "linkedin", "linkedin_url": "linkedin", "founder_linkedin": "linkedin",
    "youtube": "youtube", "youtube_url": "youtube", "yt": "youtube",
    "tradingview": "tradingview", "tradingview_url": "tradingview",
    "mql5": "mql5", "mql5_url": "mql5", "links": "unknown", "socials": "unknown",
    "telegram": "telegram", "telegram_username": "telegram", "tg": "telegram",
    "tg_username": "telegram", "lichka": "telegram",
}
_NAME_FIELDS = ("founder_name", "author_name", "display_name", "name", "author", "owner")

# These are not evidence of a person.  They are especially dangerous when a
# GitHub/X username happens to exist as a Telegram channel or bot.
_GENERIC_HANDLES = {
    "admin", "administrator", "announcement", "announcements", "bot", "contact",
    "crypto", "cryptocurrency", "forex", "news", "official", "owner", "signal",
    "signals", "support", "team", "trading", "trade", "updates", "help", "info",
    "marketing", "community", "channel", "group", "invest", "investing",
}


def _split(value: str):
    try:
        return urlsplit(value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) else "https://" + value)
    except ValueError:
        return None


def _host(value: str) -> str:
    parsed = _split(value)
    if parsed is None:
        return ""
    return (parsed.hostname or "").lower().removeprefix("www.")


def _path_parts(value: str) -> list[str]:
    parsed = _split(value)
    if parsed is None:
        return []
    return [unquote(x) for x in parsed.path.split("/") if x]


def normalize_handle(value: Any, platform: str | None = None) -> str:
    """Return a Telegram-compatible handle without ``@`` or URL syntax.

    For public-source URLs the path is used only when it is unambiguous for the
    platform.  This avoids accidentally turning a GitHub repository name or a
    LinkedIn company slug into a person candidate.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw or raw.lower().startswith(("t.me/", "x.com/", "twitter.com/", "github.com/", "linkedin.com/", "youtube.com/")):
        parts = _path_parts(raw)
        host = _host(raw)
        if host in _TG_HOSTS:
            raw = parts[0] if parts else ""
        elif platform in {"x", "twitter", "github", "youtube"} or host in _PLATFORM_HOSTS.get(platform or "", set()):
            raw = parts[0] if parts else ""
        elif platform == "linkedin" or host == "linkedin.com":
            raw = parts[1] if len(parts) >= 2 and parts[0].lower() == "in" else ""
        elif platform == "tradingview" or host == "tradingview.com":
            raw = parts[1] if len(parts) >= 2 and parts[0].lower() in {"u", "user"} else ""
        elif platform == "mql5" or host == "mql5.com":
            raw = parts[-1] if parts and parts[-2:-1] == ["users"] else ""
        else:
            raw = ""
    raw = raw.strip().lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/ ")
    # Telegram permits letters, digits and underscores.  Do not silently
    # transliterate arbitrary names into a username.
    return raw.lower() if re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw) else ""


def _source_handle(value: Any, platform: str) -> str:
    """Extract a public-platform slug; unlike Telegram it may contain ``-``/``.``."""
    raw = str(value or "").strip()
    if "://" in raw or "/" in raw:
        parts = _path_parts(raw)
        host = _host(raw)
        if platform == "linkedin" or host == "linkedin.com":
            raw = parts[1] if len(parts) >= 2 and parts[0].lower() == "in" else ""
        elif platform == "tradingview" or host == "tradingview.com":
            raw = parts[1] if len(parts) >= 2 and parts[0].lower() in {"u", "user"} else ""
        elif platform == "mql5" or host == "mql5.com":
            raw = parts[-1] if parts and parts[-2:-1] == ["users"] else ""
        else:
            raw = parts[0] if parts else ""
    raw = raw.strip().lstrip("@").split("?", 1)[0].split("#", 1)[0].strip("/ ").lower()
    return raw if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", raw) else ""


def handle_key(value: Any) -> str:
    """Comparison key for handles where punctuation differs across platforms."""
    value = str(value or "").strip().lstrip("@")
    return re.sub(r"[^a-z0-9]", "", normalize_handle(value) or value.lower())


def _is_generic(handle: str) -> bool:
    key = handle_key(handle)
    return not key or key in _GENERIC_HANDLES or key.endswith("bot") or key.isdigit()


def _name_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return {x for x in re.findall(r"[^\W_]+", text, flags=re.UNICODE) if len(x) >= 2}


def name_similarity(left: Any, right: Any) -> float:
    """Cheap, explainable name score used after Telethon resolves a profile."""
    a, b = _name_tokens(left), _name_tokens(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    compact_a, compact_b = "".join(sorted(a)), "".join(sorted(b))
    return max(overlap, SequenceMatcher(None, compact_a, compact_b).ratio() * 0.65)


@dataclass(frozen=True)
class PublicHandle:
    platform: str
    handle: str
    url: str = ""
    field: str = ""
    explicit_telegram: bool = False


@dataclass
class TelegramCandidate:
    username: str
    score: float
    match_type: str
    evidence: list[str] = field(default_factory=list)
    source_platforms: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    requires_name_check: bool = False
    verified: bool = False
    verification_status: str = "unverified"
    verification_reason: str = ""
    telegram_id: int | None = None
    telegram_username: str = ""
    telegram_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_values(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_values(child)


def extract_public_handles(row: Mapping[str, Any]) -> list[PublicHandle]:
    """Extract handles only from explicit social/contact fields and URLs."""
    out: list[PublicHandle] = []
    seen: set[tuple[str, str]] = set()
    for raw_field, raw_value in row.items():
        field_name = str(raw_field).lower()
        platform = _SOCIAL_FIELDS.get(field_name, "")
        for value in _iter_values(raw_value):
            text = value.strip()
            if not text:
                continue
            host = _host(text) if ("://" in text or "/" in text) else ""
            found: list[tuple[str, str, bool]] = []
            if host in _TG_HOSTS:
                h = normalize_handle(text, "telegram")
                if h:
                    found.append(("telegram", h, True))
            elif host:
                for p, hosts in _PLATFORM_HOSTS.items():
                    if host in hosts:
                        h = _source_handle(text, p)
                        if h:
                            found.append((p, h, False))
            elif platform in _SOCIAL_FIELDS.values() and platform != "unknown":
                h = _source_handle(text, platform)
                if h:
                    found.append((platform, h, platform == "telegram"))
            for p, handle, direct in found:
                key = (p, handle_key(handle))
                if key in seen:
                    continue
                seen.add(key)
                out.append(PublicHandle(p, handle, text if "://" in text else "", field_name, direct))
    return out


def _display_name(row: Mapping[str, Any]) -> str:
    for key in _NAME_FIELDS:
        if row.get(key):
            return str(row[key]).strip()
    return ""


def _person_name(row: Mapping[str, Any]) -> str:
    """Only explicit person-name fields may seed a Telegram name fallback."""
    for key in ("founder_name", "author_name", "owner"):
        if row.get(key):
            return str(row[key]).strip()
    return ""


def _name_candidate(name: str) -> str:
    tokens = [re.sub(r"[^a-z0-9]", "", x.lower()) for x in re.split(r"\s+", name) if x]
    return "".join(tokens) if len(tokens) >= 2 and all(tokens) else ""


def generate_telegram_candidates(row: Mapping[str, Any], max_candidates: int = 8) -> list[TelegramCandidate]:
    """Generate ranked, conservative usernames; no Telegram network call."""
    handles = extract_public_handles(row)
    by_key: dict[str, TelegramCandidate] = {}
    for item in handles:
        if item.platform == "telegram":
            key = handle_key(item.handle)
            c = by_key.setdefault(key, TelegramCandidate(item.handle, 1.0, "explicit_telegram"))
            c.score = max(c.score, 1.0)
            c.evidence.append(f"explicit Telegram link in {item.field}")
            c.source_platforms.append("telegram")
            if item.handle not in c.aliases:
                c.aliases.append(item.handle)
            c.requires_name_check = False
            continue
        key = handle_key(item.handle)
        if not key or _is_generic(item.handle):
            continue
        # Preserve the Telegram spelling where it is already legal.  A compact
        # spelling is only a candidate if the source itself has punctuation.
        username = normalize_handle(item.handle)
        if not username:
            compact = re.sub(r"[^a-zA-Z0-9]", "", item.handle)
            username = normalize_handle(compact)
        if not username:
            continue
        c = by_key.setdefault(key, TelegramCandidate(username, 0.72, "exact_public_handle"))
        c.score = max(c.score, 0.80)
        c.evidence.append(f"same public handle on {item.platform}")
        c.source_platforms.append(item.platform)
        if username not in c.aliases:
            c.aliases.append(username)
        c.requires_name_check = True
    # Cross-platform repetition is strong evidence, but not proof; this is why
    # the verifier still checks the resolved Telegram profile and display name.
    for c in by_key.values():
        platforms = set(c.source_platforms)
        if c.match_type == "exact_public_handle":
            c.score = min(0.94, c.score + max(0, len(platforms) - 1) * 0.07)
            if len(platforms) >= 2:
                c.evidence.append("handle repeated across multiple platforms")
    # Full-name concatenation is intentionally low priority and must pass a
    # display-name check.  It catches founders whose public accounts use the
    # same name but have no linked Telegram URL, without guessing first names.
    name_cand = _name_candidate(_person_name(row))
    name_key = handle_key(name_cand)
    if name_cand and not _is_generic(name_cand) and name_key not in by_key:
        by_key[name_key] = TelegramCandidate(
            name_cand, 0.44, "full_name_fallback",
            evidence=["full-name fallback; requires Telegram display-name match"],
            requires_name_check=True,
            aliases=[name_cand],
        )
    result = sorted(by_key.values(), key=lambda x: (-x.score, x.username))
    return result[:max(1, max_candidates)]


def candidate_state(row: Mapping[str, Any], candidate: TelegramCandidate) -> dict[str, Any]:
    """Compact state intended for an independent Jev identity judgment."""
    return {
        "lead_name": _display_name(row),
        "candidate_username": candidate.username,
        "match_type": candidate.match_type,
        "score": round(candidate.score, 3),
        "evidence": list(dict.fromkeys(candidate.evidence)),
        "source_platforms": sorted(set(candidate.source_platforms)),
        "telegram_name": candidate.telegram_name,
        "telegram_username": candidate.telegram_username,
        "name_similarity": round(name_similarity(_display_name(row), candidate.telegram_name), 3),
    }


async def verify_telegram_candidates(
    client: Any,
    row: Mapping[str, Any],
    candidates: Iterable[TelegramCandidate] | None = None,
) -> list[TelegramCandidate]:
    """Resolve exact usernames through Telethon and classify identity safely.

    ``client`` must already be an authenticated Telethon client.  FloodWait is
    not swallowed: the candidate is marked ``retry`` and the remaining lookup
    continues, allowing the caller to persist a retryable result.
    """
    candidates = list(candidates or generate_telegram_candidates(row))
    display_name = _display_name(row)
    for candidate in candidates:
        entity = None
        last_exc: Exception | None = None
        for username in list(dict.fromkeys(candidate.aliases or [candidate.username])):
            try:
                entity = await client.get_entity(username)
                candidate.username = username
                break
            except Exception as exc:
                last_exc = exc
                if exc.__class__.__name__ == "FloodWaitError":
                    break
        if entity is None:
            if last_exc is not None and last_exc.__class__.__name__ == "FloodWaitError":
                candidate.verification_status = "retry"
                candidate.verification_reason = "Telegram FloodWait; retry later"
            else:
                candidate.verification_status = "not_found"
                candidate.verification_reason = "none of the exact public-handle variants resolved"
            continue
        actual = normalize_handle(getattr(entity, "username", ""))
        if not actual or handle_key(actual) != handle_key(candidate.username):
            candidate.verification_status = "rejected"
            candidate.verification_reason = "Telegram resolved to a different or hidden username"
            continue
        tg_name = " ".join(x for x in (getattr(entity, "first_name", ""), getattr(entity, "last_name", "")) if x).strip()
        candidate.telegram_id = getattr(entity, "id", None)
        candidate.telegram_username = actual
        candidate.telegram_name = tg_name
        similarity = name_similarity(display_name, tg_name)
        if candidate.match_type == "explicit_telegram":
            candidate.verified = True
            candidate.verification_status = "verified_direct"
            candidate.verification_reason = "explicit public Telegram link resolved"
        elif similarity >= 0.45 or len(set(candidate.source_platforms)) >= 2:
            candidate.verified = True
            candidate.verification_status = "verified_identity"
            candidate.verification_reason = (
                "Telegram display name matches public identity"
                if similarity >= 0.45 else "same non-generic handle appears on multiple public platforms"
            )
        else:
            candidate.verification_status = "needs_review"
            candidate.verification_reason = "single shared handle without sufficient identity evidence"
    return candidates


def verify_telegram_candidates_sync(client: Any, row: Mapping[str, Any], candidates: Iterable[TelegramCandidate] | None = None) -> list[TelegramCandidate]:
    return asyncio.run(verify_telegram_candidates(client, row, candidates))


def dedupe_verified_candidates(candidates: Iterable[TelegramCandidate]) -> list[TelegramCandidate]:
    """Keep the strongest result per Telegram id/username, preserving order."""
    best: dict[str, TelegramCandidate] = {}
    for item in candidates:
        key = str(item.telegram_id) if item.telegram_id is not None else handle_key(item.telegram_username or item.username)
        old = best.get(key)
        if old is None or (item.verified, item.score) > (old.verified, old.score):
            best[key] = item
    return sorted(best.values(), key=lambda x: (-int(x.verified), -x.score, x.username))


__all__ = [
    "PublicHandle", "TelegramCandidate", "candidate_state", "dedupe_verified_candidates",
    "extract_public_handles", "generate_telegram_candidates", "handle_key", "name_similarity",
    "normalize_handle", "verify_telegram_candidates", "verify_telegram_candidates_sync",
]
