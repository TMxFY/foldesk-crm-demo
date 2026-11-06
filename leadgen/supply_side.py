"""Clippers sell labour. Buyers hire it. This runs before any model."""
from __future__ import annotations

import re

_CREATOR_PAYOUT_OR_AGENCY = re.compile(
    r"got accepted into.{0,40}content rewards|my (?:last|next) payout|"
    r"getting paid for (?:my|our) content|revenue sharing program|"
    r"original content rewards|clipster withdrawal|x pays creators|@xcreators|"
    r"\|\s*ugc creator\b|\b(?:male|female|bilingual|middle age)\s+ugc creator\b|"
    r"разрабатываем ии-агентов|создаю автономных ии-агентов|"
    r"seo-аудит,\s*продвижение и создание сайтов|"
    r"\bjava build tool\b|\bold css hack\b",
    re.I,
)
# Hiring language wins, even if the same post also says "portfolio".
_BUYER = re.compile(
    r"\bwe acquire\b|\bwe buy and grow\b|\bacquisition studio\b|"
    r"looking for clippers|hiring clippers|need clippers|\bhiring\b|"
    r"content rewards|paying clippers|pay per view|"
    r"my students|my cohort|my community|my course",
    re.I,
)
# "clipper" alone is not supply: "looking for clippers" is a buyer.
_SUPPLY = re.compile(
    r"\bi clip\b|\bi'm a clipper\b|\bi am a clipper\b|\bwe clip\b|"
    r"\bportfolio\b|no followers needed|\bper gig\b|"
    r"\$\s*\d+\s*(?:per|/)\s*(?:clip|video|gig)|"
    r"looking for (?:a )?job|looking for work|ищу работу|open to work",
    re.I,
)


def supply_reason(text: str) -> str:
    """Matched supply phrase, or empty when this person may be a buyer."""
    blob = text or ""
    early = _CREATOR_PAYOUT_OR_AGENCY.search(blob)
    if early:
        return early.group(0)
    if _BUYER.search(blob):
        return ""
    match = _SUPPLY.search(blob)
    return match.group(0) if match else ""


def is_supply_side(text: str) -> bool:
    """True when the author is selling clipping labour, not buying it."""
    return bool(supply_reason(text))
