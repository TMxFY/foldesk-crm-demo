#!/usr/bin/env python3
"""Market and segment labels for outreach. No network and no sheet writes.

market_of returns "ru" or "foreign". segment_of returns "large", "classic", or "".
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_RU_HOST_SUFFIXES = (".ru", ".su", ".рф", ".xn--p1ai")
_RU_WORD = re.compile(r"(?<!\w)(?:россия|рф|russia)(?!\w)")
_RU_PHRASES = (
    "russian market",
    "for russia",
    "в россии",
    "для россии",
    "по россии",
)

# Casino / slots / betting / gambling are not the large segment.
_GAMBLE = re.compile(r"casino|slots?|betting|gambling|казино")
_GTA = re.compile(r"(?<![a-z0-9])gta(?:[\s-]*6)?(?![a-z])")
_GTA_RP = re.compile(r"(?<![a-z0-9])gta[\s-]*rp(?![a-z])")
_IGAMING = re.compile(r"(?<![a-z])i-?gaming(?![a-z])|(?<![a-z])i gaming(?![a-z])")
_CLASSIC_PARTS = (
    "supplement",
    "vitamin",
    "cosmetic",
    "skincare",
    "household",
    "добавк",
    "витамин",
    "косметик",
    "бытов",
)
_CLASSIC_APPS = ("fitness app", "health app", "productivity app", "habit app")
_MINI_PRODUCTS = ("ebook", "e-book", "course", "guide", "книга", "гайд", "курс")
_MINI_CONTEXT = ("astrology", "астролог", "fitness", "make money", "заработ")
_ASTROLOGY_WITH = ("fitness", "make money", "астролог")


def _hostname(site: str) -> str:
    raw = (site or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "//" + raw.lstrip("/")
    return (urlparse(raw).hostname or "").casefold().rstrip(".")


def _ru_host(site: str) -> bool:
    host = _hostname(site)
    if not host:
        return False
    names = [host]
    try:
        names.append(host.encode("ascii").decode("idna").casefold())
    except UnicodeError:
        pass
    return any(name.endswith(_RU_HOST_SUFFIXES) for name in names)


def _ru_text(text: str) -> bool:
    low = (text or "").casefold()
    if not low:
        return False
    if any(phrase in low for phrase in _RU_PHRASES):
        return True
    return _RU_WORD.search(low) is not None


def market_of(site: str, text: str) -> str:
    """RU only for a .ru/.su/.рф host or an explicit Russia market marker."""
    if _ru_host(site) or _ru_text(text):
        return "ru"
    return "foreign"


def _large(low: str) -> bool:
    if "roblox" in low or "fivem" in low:
        return True
    if "record label" in low or "music label" in low:
        return True
    if _GTA.search(low) or _GTA_RP.search(low):
        return True
    # iGaming counts only as Roblox-like games, never as casino.
    if _IGAMING.search(low) and _GAMBLE.search(low) is None:
        return True
    return False


def _classic(low: str) -> bool:
    if any(part in low for part in _CLASSIC_PARTS):
        return True
    if any(app in low for app in _CLASSIC_APPS):
        return True
    if any(part in low for part in _MINI_PRODUCTS) and any(part in low for part in _MINI_CONTEXT):
        return True
    # "astrology" alone is not a mini-product; it still needs a second niche.
    return "astrology" in low and any(part in low for part in _ASTROLOGY_WITH)


def segment_of(text: str) -> str:
    """Short label: large beats classic. Casino is neither."""
    low = (text or "").casefold()
    if _large(low):
        return "large"
    if _classic(low):
        return "classic"
    return ""
