#!/usr/bin/env python3
"""OFM & B2B Telegram groups + public Facebook groups collector with contact waterfall.

Strict isolation guarantees:
- OFM buyer leads are persisted ONLY via ``app.hygiene.append_ofm_review_lead`` into
  ``<state_dir>/ofm_review_leads.jsonl`` and NEVER enter B2B CRM campaigns or B2B JSONL.
- B2B Telegram group leads (``source="tg_groups"``) enforce strict supply-side, sniper,
  personal ``@username``, and external product domain / explicit buyer-intent gates.
- Telegram sessions are opened read-only without interactive login prompts and derived
  strictly from ``TG_SESSION`` / ``SCRAPER_TG_SESSION`` (or explicit ``--session``).
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import ssl
import sys
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.hygiene import append_ofm_review_lead, sniper_lead_violation_reason
from app.leadgen_settings import _resolve_state_dir
from leadgen.lead_market import market_of
from leadgen.supply_side import (
    NON_BUYER_ROLES,
    is_invalid_product_name,
    is_platform_or_social_url,
    is_supply_side,
    sanitize_person_and_company,
    sniper_reject_reason,
)
from leadgen.tg_enrich_leads import classify_cat, classify_role
from leadgen.treg_client import call as treg_call, check_treg_ready
try:
    from leadgen.vps_autonomous_replenish import _load_dotenv_if_allowed
except ImportError:
    def _load_dotenv_if_allowed(env_path: Path) -> None:
        if os.environ.get("CRMTG_SKIP_DOTENV") == "1" or not env_path.is_file():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                os.environ.setdefault(k, v)

logger = logging.getLogger(__name__)

SEEN_DAYS = 90

DEFAULT_OFM_TG_GROUPS = (
    "FranceOFM",
    "ofmfrancenetwork",
    "networkOFM",
    "mofmnetwork",
    "RedditWorldOFM",
    "aimodelnetwork",
    "AdultDeskAI",
)
DEFAULT_B2B_TG_GROUPS = (
    "solofounderschat",
    "productcircle_chat",
    "saasfounders",
    "producthunt_heroes",
)
DEFAULT_FB_GROUP_DISCOVERY_QUERIES = (
    'site:facebook.com/groups "Public group" ("OFM agency" OR "OnlyFans management" OR "OnlyFans agency")',
    'site:facebook.com/groups "Public group" ("Shopify" OR "DTC" OR "Ecommerce" OR "UGC")',
)
OFM_DISCOVERY_QUERIES = (
    "OFM",
    "OnlyFans agency",
    "Fanvue",
    "OFM network",
)

_TG_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_RESERVED_TG_HANDLES = frozenset(
    {
        "joinchat",
        "share",
        "addstickers",
        "proxy",
        "iv",
        "s",
        "c",
        "boost",
        "socks",
        "grouphelp",
        "rose",
        "ghclone",
        "shieldy",
        "combot",
    }
)
_KNOWN_GROUP_HANDLES = frozenset(
    g.casefold() for g in (*DEFAULT_OFM_TG_GROUPS, *DEFAULT_B2B_TG_GROUPS)
)
_BOT_IDENTITY_TOKENS = ("ghclone", "grouphelp")

_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z][A-Za-z0-9_]{3,31})(?!\s*/\s*\d+)(?=[/?#\s),.;:!?\"']|$)",
    re.I,
)
_EXPLICIT_TG_DM_RE = re.compile(
    r"(?:^|[\s(\[:,;|/])"
    r"(?:dm(?:\s+me)?(?:\s+on\s+(?:tg|telegram))?|"
    r"contact(?:\s+me)?(?:\s+on\s+(?:tg|telegram))?|"
    r"message(?:\s+me)?(?:\s+on\s+(?:tg|telegram))?|"
    r"reach\s+(?:out|me)(?:\s+on\s+(?:tg|telegram))?|"
    r"telegram|tg|mp|"
    r"fondateur|founder|owner|"
    r"в\s*лс|лс|пишите|пиши)"
    r"\s*(?:[:\-–—]|at|on)?\s*@([A-Za-z][A-Za-z0-9_]{3,31})\b",
    re.I,
)
_SOCIAL_PREFIXED_HANDLE_RE = re.compile(
    r"(?:^|[\s(\[:,;|/])(?:ig|insta|instagram|twitter|x|tiktok|tt|snap|snapchat)"
    r"(?:\s*(?:dm|me|on))?\s*(?:[:\-–—]|at|on)?\s*@([A-Za-z0-9_.]{2,30})\b",
    re.I,
)
_X_URL_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?=[/?#\s),.;:!?\"']|$)",
    re.I,
)
_X_EXPLICIT_RE = re.compile(
    r"(?:^|[\s(\[:,;|/])(?:x|twitter)\s*[:\-–—]\s*@([A-Za-z0-9_]{1,15})\b",
    re.I,
)
_X_NOISE = frozenset(
    {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose", "settings", "login", "signup", "tos", "privacy"}
)
_IG_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?instagram\.com/([A-Za-z0-9_.]{1,30})(?=[/?#\s),.;:!?\"']|$)",
    re.I,
)
_IG_EXPLICIT_RE = re.compile(
    r"(?:^|[\s(\[:,;|/])(?:(?:dm|contact|message)\s+(?:me\s+)?on\s+)?(?:ig|insta|instagram)\s*(?:[:\-–—]|at)?\s*@([A-Za-z0-9_.]{2,30})\b",
    re.I,
)
_IG_NOISE = frozenset(
    {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "tv", "tags", "locations", "about", "legal", "s", "share"}
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)
_BAD_EMAIL_LOCAL = frozenset({"noreply", "no-reply", "donotreply"})
_BAD_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
_WA_ME_RE = re.compile(r"(?:https?://)?(?:www\.)?wa\.me/(?:c/)?([%\d+][%\d\s().\-]*)", re.I)
_WA_PHONE_RE = re.compile(
    r"https?://(?:www\.)?(?:api\.whatsapp\.com|whatsapp\.com)/send(?:/)?\?[^#]*?\bphone=(\+?[%\d][%\d\s().\-]{6,})",
    re.I,
)
_WA_TEXT_RE = re.compile(
    r"(?:whatsapp|wa)\s*(?:/\s*tel[^:\n]{0,18})?\s*[:\-–—]?\s*(\+?\d[\d\s().\-]{7,18}\d)",
    re.I,
)
_URL_RE = re.compile(r"(?:https?://[^\s<>()\[\]\"']+|www\.[^\s<>()\[\]\"']+)", re.I)
_CONTACT_SKIP_URL_HOSTS = (
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegram.org",
    "telegra.ph",
    "instagram.com",
    "instagr.am",
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "t.co",
    "facebook.com",
    "fb.com",
    "fb.watch",
    "m.facebook.com",
    "messenger.com",
    "wa.me",
    "whatsapp.com",
    "api.whatsapp.com",
)
_FB_GROUP_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?facebook\.com/groups/([A-Za-z0-9._-]+)",
    re.I,
)
_FB_NOISE_SLUGS = frozenset({"discover", "feed", "create", "search", "categories", "joins", "notifications"})

# --- OFM classification rules ---
_OFM_L4L_RE = re.compile(
    r"\b(?:l4l|sfs|f4f|c4c|like\s*4\s*like|sub\s*4\s*sub|follow\s*4\s*follow)\b|"
    r"drop\s+your\s+link|free\s+(?:promo\s+)?group|gg\s+group|gains\s+group",
    re.I,
)
_OFM_FRAUD_KYC_RE = re.compile(
    r"\blemon\s*cash\b|\bemulator\b|\bkyc\b|qui\s+font\s+du\s+kyc|"
    r"withdraw\s+money|\bskrill\b|\bneteller\b|\bcarding\b|\bfullz\b|\bcvv\b|"
    r"\bbank\s+drop\b|buying\s+verified\s+accounts?|contourn\w*\s+kyc",
    re.I,
)
_OFM_CASINO_RE = re.compile(
    r"\b(?:1win|1xbet|mostbet|melbet|pin-?up|casino|slots|gambling|betting|казино|ставки|букмекер\w*)\b",
    re.I,
)
_OFM_RECRUITER_MIDDLEMAN_RE = re.compile(
    r"\brecruteur(?:s)?\s+(?:de\s+)?chatteur(?:s)?\b|"
    r"\bchatter\s+recruiter\b|\bva\s+recruiter\b|\bi\s+recruit\s+chatters\b|"
    r"\bmodels?\s+marketplace\b|\bconnect(?:ed|ing)?\s+[\d,+]*\s*models\s+with\b|"
    r"\bmodels?\s+available\s+for\s+agenc",
    re.I,
)
_OFM_JOB_SEEKER_RE = re.compile(
    r"\bavailable\b.{0,30}\b(?:virtual\s+assistant|chatter)\b|"
    r"\bavailable\s+(?:as\s+|for\s+)?(?:a\s+)?(?:va\b|instagram|threads|tiktok|reddit|x|chatting)|"
    r"\b(?:virtual\s+assistant|chatter)\s+available\b|"
    r"\blooking\s+for\s+(?:an?\s+)?(?:ofm\s+)?agenc(?:y|ies)\b|"
    r"\blooking\s+for\s+(?:a\s+|an\s+)?(?:long[-\s]?term\s+|full[-\s]?time\s+|part[-\s]?time\s+|remote\s+)?(?:job|work|role|position|opportunity|chatter\s+(?:role|position|job)|va\s+(?:role|position|job)|clients?)\b|"
    r"\bready\s+to\s+(?:promote|manage|grow|scale)\s+your\s+models?\b|"
    r"\bare\s+(?:you\s+)?looking\s+for\s+(?:a\s+|an\s+)?[^?\n]{0,80}\b(?:virtual\s+assistant|va\b|chatter|chatteur|specialist|manager)\b|"
    r"\b(?:do\s+you\s+)?need\s+(?:a\s+|an\s+)?[^?\n]{0,80}\b(?:virtual\s+assistant|va\b|chatter|chatteur|specialist)\b[^?\n]{0,60}\?|"
    r"\bseeking\b.{0,30}\b(?:agency|chatter\s+(?:role|job)|job|position|clients?)\b|"
    r"\b(?:i\s+am|i['’]m|im|je\s+suis)\s+(?:a\s+|an\s+)?(?:experienced\s+|reliable\s+|results[-\s]driven\s+|dedicated\s+)?(?:ofm\s+|reddit\s+|dating\s+app\s+|instagram\s+|threads\s+|tiktok\s+|twitter\s+|social\s+media\s+)?(?:chatter|chatteur|chatteuse|virtual\s+assistant|va\b|specialist)\b|"
    r"\b(?:je\s+)?cherche\b.{0,35}\b(?:agence|poste|travail|mission|boulot|clients?)\b|"
    r"\bdispo(?:nible)?\s+(?:pour|as|for)\s+(?:chatting|chatter|chatteur|va|travail)\b|"
    r"\b(?:i\s+offer|je\s+propose\s+mes\s+services|dm\s+for\s+my\s+services)\b|"
    r"\bfor\s+hire\b|\bopen\s+to\s+work\b|"
    r"\b(?:ищу\s+работу|ищу\s+вакансию|ищу\s+подработку|ищу\s+заказы|в\s+поиске\s+работы)\b",
    re.I,
)
_EXPERIENCED_LABOR_RE = re.compile(
    r"\bexperienced\s+(?:virtual\s+assistant|va\b|ofm\s+chatter|chatter)\b",
    re.I,
)
_HIRING_PREFIX_RE = re.compile(
    r"\b(?:hiring|looking\s+for|need|seeking|recrut\w*|on\s+recrute|cherche\s+des?)\b",
    re.I,
)
_OFM_ACCOUNT_VENDOR_RE = re.compile(
    r"\b(?:selling|for\s+sale|vend(?:s|ons)?|vente\s+de)\s+(?:aged\s+|warmed\s+|verified\s+)?(?:ig|instagram|tiktok|reddit|threads|twitter|x|of|onlyfans|fanvue)\s+accounts?\b",
    re.I,
)

_OFM_TRAFFIC_BUYER_RE = re.compile(
    r"\brecrutement\s+(?:de\s+|des\s+)?vas?\b|"
    r"\bon\s+recrute\s+(?:des\s+)?vas?\b|"
    r"\brecrute\s+(?:des\s+)?vas?\b|"
    r"\b(?:a|à)\s+la\s+recherche\s+de\s+(?:vas?\b|virtual\s+assistants?)|"
    r"\b(?:je\s+)?(?:cherche|recherche)\s+(?:des\s+|de\s+)?vas?\b|"
    r"\bbesoin\s+(?:de|des)\s+vas?\b|"
    r"\bhiring\b.{0,30}\b(?:virtual\s+assistants?|vas?)\b|"
    r"\blooking\s+for\b.{0,30}\b(?:virtual\s+assistants?|vas?)\b|"
    r"\bneed\b.{0,25}\b(?:virtual\s+assistants?|vas?)\b|"
    r"\b(?:recrut\w*|hiring|looking\s+for|recherche|need|want|buying|buy|on\s+cherche|notre\s+agence|our\s+agency)\b.{0,90}"
    r"\b(?:va\s+(?:insta(?:gram)?|threads|tiktok|twitter|x|reddit|posting|r[ée]seaux)|mother[\s/]+child|per\s+subscriber|per\s+sub|par\s+abo|new\s+abo|nouvel\s+abo)\b|"
    r"\b(?:looking\s+for|buying|buy|need|want)\s+(?:ofm\s+|ig\s+|tiktok\s+|reddit\s+|threads\s+)?traffic\b|"
    r"\b(?:cherche|besoin)\s+(?:de\s+|du\s+)?trafic\b|"
    r"\bachat\s+de\s+trafic\b|"
    r"\b(?:need|buying|want|looking\s+for)\s+reels\b",
    re.I | re.S,
)
_OFM_MODEL_OR_PAYOUT_RE = re.compile(
    r"\b(?:fanvue|ai\s+model|ia\s+model|mod[èe]le\s+ia|ai\s+creator|ai\s+influencer)\b",
    re.I,
)
_OFM_PAYOUT_TOKEN_RE = re.compile(
    r"(?:\$\s*\d+(?:\.\d+)?\+?|€\s*\d+|\bper\s+subscriber\b|\bpar\s+abo\b|\bnew\s+abo\b|"
    r"\bpaying\b|\bpaye\b|\br[ée]mun[ée]ration\b|\bbudget\b|\bcpa\b|\bcpm\b|\brevshare\b)",
    re.I,
)
_OFM_AGENCY_OWNER_RE = re.compile(
    r"\bon\s+recrute\b.{0,30}\bchatteurs?\b|"
    r"\brecrute\b.{0,25}\bchatteurs?\b|"
    r"\brecrutement\b.{0,25}\bchatteurs?\b|"
    r"\b(?:cherche|besoin)\b.{0,25}\bchatteurs?\b|"
    r"\bhiring\b.{0,25}\bchatters?\b|"
    r"\blooking\s+for\b.{0,25}\bchatters?\b|"
    r"\bagrandit\s+(?:l['’]|son|notre)\s*[ée]quipe\b|"
    r"\bagrandissons\s+notre\s+[ée]quipe\b|"
    r"\bexpanding\s+(?:our|the)\s+team\b|"
    r"\b(?:ofm\s+)?agency\s+owner\b|"
    r"\bfondateur\b.{0,30}\b(?:agence|ofm)\b|"
    r"\b(?:founder|owner|ceo)\b.{0,30}\b(?:ofm|onlyfans|fanvue)\b|"
    r"\bnotre\s+agence\b.{0,30}\brecrut\w*\b|"
    r"\bour\s+agency\b.{0,30}\bhiring\b|"
    r"\bofm\s+agency\b|\bonlyfans\s+agency\b|\bonlyfans\s+management\b|\bagence\s+ofm\b",
    re.I,
)
_OFM_CONTEXT_RE = re.compile(
    r"\b(?:ofm|onlyfans|only[\s\-]?fans|fanvue|fansly|loyalfans|mym|ai\s+model|mod[èe]le\s+ia|chatteur|chatteurs|chatter)\b"
    r"|\bof\s+(?:model|agency|creator|traffic|page)\b",
    re.I,
)

# Market detection markers
_MARKET_US_SENSITIVE_RE = re.compile(r"\b(?:US|USA)\b")
_MARKET_US_RE = re.compile(
    r"🇺🇸|\b(?:united\s+states|american|miami|nyc|los\s+angeles|us\s+market|us\s+traffic|us\s+models?|march[ée]\s+us)\b",
    re.I,
)
_MARKET_UK_SENSITIVE_RE = re.compile(r"\bUK\b")
_MARKET_UK_RE = re.compile(
    r"🇬🇧|\b(?:united\s+kingdom|london|british|gbp|uk\s+market)\b|£",
    re.I,
)
_MARKET_FR_SENSITIVE_RE = re.compile(r"\bFR\b")
_MARKET_FR_RE = re.compile(
    r"🇫🇷|\b(?:recrute|recrutement|agence|mod[èe]le|cherche|par\s+abo|new\s+abo|chatteur|chatteurs|"
    r"r[ée]mun[ée]ration|francophone|france|fran[çc]ais(?:e|es)?|notre\s+agence|bonsoir|bonjour|salut|"
    r"[ée]quipe|besoin\s+de|trafic)\b",
    re.I,
)
_MARKET_GLOBAL_RE = re.compile(
    r"\b(?:global|worldwide|international|ww)\b",
    re.I,
)

# Channel detection markers
_CHANNEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Instagram", re.compile(r"\b(?:instagram|insta|ig|reels?)\b", re.I)),
    ("Threads", re.compile(r"\bthreads\b", re.I)),
    ("Twitter/X", re.compile(r"\b(?:twitter|x\.com|va\s+x|tweets?)\b", re.I)),
    ("TikTok", re.compile(r"\btiktok\b|\bva\s+tt\b", re.I)),
    ("Reddit", re.compile(r"\b(?:reddit|subreddits?)\b", re.I)),
    ("OnlyFans/Fanvue", re.compile(r"\b(?:onlyfans|only\s*fans|fanvue|ofm|mym)\b|\bof\s+(?:model|agency|creator|traffic|page)\b", re.I)),
)

# B2B buyer intent regex
_B2B_BUYER_INTENT_RE = re.compile(
    r"\b(?:"
    r"looking\s+for\s+(?:ugc|clippers?|creatives?|traffic)|"
    r"hiring\s+(?:clippers?|ugc)|"
    r"need\s+(?:ugc|clippers?|traffic|creatives?|reels|shorts)|"
    r"buying\s+traffic|"
    r"just\s+launched|launching\s+(?:our|my)|launched\s+(?:our|my)|"
    r"ищ[уе]м?\s+(?:трафик|ugc|клипперов|креативы)|"
    r"нужен\s+(?:трафик|ugc)|"
    r"запустил[иа]?\s+(?:сервис|продукт|приложение|стартап|saas)"
    r")\b",
    re.I,
)


def _clean_tg_handle(raw: Any, *, extra_exclude: Iterable[str] = ()) -> str:
    handle = str(raw or "").strip().lstrip("@").strip()
    if not handle or not _TG_HANDLE_RE.match(handle):
        return ""
    low = handle.casefold()
    if low.endswith("bot") or low in _RESERVED_TG_HANDLES or low in _KNOWN_GROUP_HANDLES:
        return ""
    for item in extra_exclude:
        if low == str(item or "").strip().lstrip("@").casefold():
            return ""
    return handle


def _clean_http_url(raw: str) -> str:
    val = str(raw or "").strip().rstrip(".,;:!?)\"'>]")
    if not val:
        return ""
    if val.lower().startswith("www."):
        val = f"https://{val}"
    try:
        parsed = urlparse(val)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host or "." not in host:
        return ""
    return val


def _host_matches(host: str, skip_hosts: tuple[str, ...]) -> bool:
    clean = host.lower().removeprefix("www.").rstrip(".")
    return any(clean == h or clean.endswith("." + h) for h in skip_hosts)


def _extract_email(text: str) -> str:
    raw = str(text or "")
    for m in _EMAIL_RE.finditer(raw):
        email = m.group(0).strip("._-")
        local, sep, domain = email.partition("@")
        if not sep or not local or not domain:
            continue
        if local.casefold() in _BAD_EMAIL_LOCAL:
            continue
        dom_low = domain.casefold()
        tail = raw[m.end() : m.end() + 5].lower()
        if dom_low.endswith(_BAD_EMAIL_SUFFIXES) or tail.startswith(_BAD_EMAIL_SUFFIXES):
            continue
        return email
    return ""


def _extract_whatsapp(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    candidates: list[tuple[int, str]] = []
    for rx in (_WA_ME_RE, _WA_PHONE_RE, _WA_TEXT_RE):
        for m in rx.finditer(raw):
            digits = re.sub(r"\D", "", unquote(m.group(1)))
            if 8 <= len(digits) <= 15:
                candidates.append((m.start(), digits))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return f"https://wa.me/{candidates[0][1]}"


def _extract_x_url(text: str) -> str:
    raw = str(text or "")
    for m in _X_URL_RE.finditer(raw):
        handle = m.group(1).strip()
        if handle and handle.casefold() not in _X_NOISE:
            return f"https://x.com/{handle}"
    for m in _X_EXPLICIT_RE.finditer(raw):
        handle = m.group(1).strip()
        if handle and handle.casefold() not in _X_NOISE:
            return f"https://x.com/{handle}"
    return ""


def _extract_instagram_url(text: str) -> str:
    raw = str(text or "")
    for m in _IG_URL_RE.finditer(raw):
        handle = m.group(1).strip().strip(".")
        if handle and handle.casefold() not in _IG_NOISE:
            return f"https://instagram.com/{handle}"
    for m in _IG_EXPLICIT_RE.finditer(raw):
        handle = m.group(1).strip().strip(".")
        if handle and handle.casefold() not in _IG_NOISE:
            return f"https://instagram.com/{handle}"
    return ""


def extract_post_contacts(
    text: str,
    bio: str = "",
    default_username: str = "",
) -> dict[str, Any]:
    """Extract personal TG handle, X, Instagram, email, WhatsApp, and external URL."""
    combined = "\n".join(part for part in (text or "", bio or "") if part)

    tg_username = _clean_tg_handle(default_username)
    if not tg_username:
        social_handles: set[str] = set()
        for m in _SOCIAL_PREFIXED_HANDLE_RE.finditer(combined):
            social_handles.add(m.group(1).strip().lstrip("@").casefold())

        for m in _TME_LINK_RE.finditer(combined):
            cand = _clean_tg_handle(m.group(1))
            if cand:
                tg_username = cand
                break

        if not tg_username:
            for m in _EXPLICIT_TG_DM_RE.finditer(combined):
                cand = _clean_tg_handle(m.group(1))
                if cand and cand.casefold() not in social_handles:
                    tg_username = cand
                    break

    x_url = _extract_x_url(combined)
    instagram_url = _extract_instagram_url(combined)
    email = _extract_email(combined)
    whatsapp = _extract_whatsapp(combined)

    ext_url = ""
    for raw_url in _URL_RE.findall(combined):
        cleaned = _clean_http_url(raw_url)
        if not cleaned:
            continue
        host = (urlparse(cleaned).hostname or "").lower()
        if _host_matches(host, _CONTACT_SKIP_URL_HOSTS):
            continue
        ext_url = cleaned
        break

    return {
        "username": tg_username,
        "x_url": x_url,
        "instagram_url": instagram_url,
        "email": email,
        "whatsapp": whatsapp,
        "url": ext_url,
    }


def _detect_market(text: str) -> str:
    if _MARKET_US_SENSITIVE_RE.search(text) or _MARKET_US_RE.search(text):
        return "US"
    if _MARKET_UK_SENSITIVE_RE.search(text) or _MARKET_UK_RE.search(text):
        return "UK"
    if _MARKET_FR_SENSITIVE_RE.search(text) or _MARKET_FR_RE.search(text):
        return "FR"
    if _MARKET_GLOBAL_RE.search(text):
        return "GLOBAL"
    ascii_words = re.findall(r"\b[A-Za-z]{3,}\b", text)
    if len(ascii_words) >= 3:
        return "EN"
    return "GLOBAL"


def _detect_channels(text: str, contacts: dict[str, Any] | None = None) -> list[str]:
    channels: list[str] = []
    for label, rx in _CHANNEL_PATTERNS:
        if rx.search(text):
            channels.append(label)
    if contacts:
        if contacts.get("instagram_url") and "Instagram" not in channels:
            channels.append("Instagram")
        if contacts.get("x_url") and "Twitter/X" not in channels:
            channels.append("Twitter/X")
    return channels


def _has_unsolicited_experienced_labor(text: str) -> bool:
    for m in _EXPERIENCED_LABOR_RE.finditer(text):
        prefix = text[max(0, m.start() - 40) : m.start()]
        if not _HIRING_PREFIX_RE.search(prefix):
            return True
    return False


def classify_ofm_actor(
    name: str = "",
    username: str = "",
    bio: str = "",
    text: str = "",
) -> dict[str, Any]:
    """Classify an OFM Telegram/Facebook actor into traffic_buyer, agency_owner, or reject."""
    raw_uname = str(username or "").strip().lstrip("@")
    raw_uname_low = raw_uname.casefold()
    name_low = str(name or "").strip().casefold()

    def _reject(reason: str, *, market: str = "GLOBAL", channels: list[str] | None = None) -> dict[str, Any]:
        return {
            "is_buyer": False,
            "ofm_tier": "",
            "icp_score": 0,
            "market": market,
            "channels": channels or [],
            "signal_summary": f"rejected:{reason}",
            "reject_reason": reason,
        }

    if (
        raw_uname_low.endswith("bot")
        or raw_uname_low in _RESERVED_TG_HANDLES
        or any(tok in raw_uname_low for tok in _BOT_IDENTITY_TOKENS)
        or any(tok in name_low for tok in _BOT_IDENTITY_TOKENS)
    ):
        return _reject("bot")

    contacts = extract_post_contacts(text, bio=bio, default_username=username)
    has_personal_contact = any(
        (
            contacts["username"],
            contacts["x_url"],
            contacts["instagram_url"],
            contacts["email"],
            contacts["whatsapp"],
        )
    )

    blob = "\n".join(part for part in (name, bio, text) if part).strip()
    market = _detect_market(blob)
    channels = _detect_channels(blob, contacts)

    if _OFM_L4L_RE.search(blob):
        return _reject("l4l_promo", market=market, channels=channels)
    if _OFM_FRAUD_KYC_RE.search(blob):
        return _reject("fraud_kyc", market=market, channels=channels)
    if _OFM_CASINO_RE.search(blob):
        return _reject("casino", market=market, channels=channels)
    if _OFM_RECRUITER_MIDDLEMAN_RE.search(blob):
        return _reject("recruiter_middleman", market=market, channels=channels)
    if _OFM_ACCOUNT_VENDOR_RE.search(blob):
        return _reject("account_vendor", market=market, channels=channels)
    if _OFM_JOB_SEEKER_RE.search(blob) or _has_unsolicited_experienced_labor(blob):
        return _reject("job_seeker", market=market, channels=channels)

    if not has_personal_contact:
        return _reject("no_contact", market=market, channels=channels)

    # Tier 1: Active VA / Reels / IG / Threads / TikTok / Reddit traffic buyer
    traffic_match = _OFM_TRAFFIC_BUYER_RE.search(blob)
    model_match = _OFM_MODEL_OR_PAYOUT_RE.search(blob)
    payout_match = _OFM_PAYOUT_TOKEN_RE.search(blob)

    if traffic_match or (model_match and (payout_match or _HIRING_PREFIX_RE.search(blob))):
        score = 90
        if payout_match:
            score += 2
        if len(channels) >= 2:
            score += 2
        if traffic_match and model_match:
            score += 1
        score = max(90, min(95, score))
        matched_tok = (traffic_match or model_match).group(0).strip()
        ch_label = ", ".join(channels) if channels else "Social"
        summary = f"[traffic_buyer] {market} | {ch_label} | {matched_tok}"[:200]
        return {
            "is_buyer": True,
            "ofm_tier": "traffic_buyer",
            "icp_score": score,
            "market": market,
            "channels": channels,
            "signal_summary": summary,
            "reject_reason": "",
        }

    # Tier 2: OFM agency owner / team hiring chatters with real brand
    agency_match = _OFM_AGENCY_OWNER_RE.search(blob)
    if agency_match:
        matched_tok = agency_match.group(0).strip()
        ch_label = ", ".join(channels) if channels else "OnlyFans/Fanvue"
        summary = f"[agency_owner] {market} | {ch_label} | {matched_tok}"[:200]
        return {
            "is_buyer": True,
            "ofm_tier": "agency_owner",
            "icp_score": 84,
            "market": market,
            "channels": channels,
            "signal_summary": summary,
            "reject_reason": "",
        }

    return _reject("no_buyer_signal", market=market, channels=channels)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def _llm_model_chain() -> list[str]:
    try:
        from leadgen.relay_route import chain

        return list(chain())
    except Exception:
        pref = (os.environ.get("LEADGEN_LLM_MODEL") or "claude-sonnet-5").strip()
        ladder = [pref, "claude-haiku-5-5", "grok-4.7", "gemini-3.8-flash"]
        return list(dict.fromkeys(m for m in ladder if m))


def _post_chat_completions(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: int = 20,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 2000,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SoftUGC-Leadgen/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"] or "").strip()


_OFM_LLM_PROMPT = """Verify OFM buyer candidates for a short-form Reels/TikTok/IG traffic service.
Keep (is_buyer=true):
- traffic_buyer (icp_score 90..95): OFM / Fanvue / AI model operators or agencies actively hiring VAs (Insta/Threads/TikTok/Twitter/Reddit), buying traffic, paying per subscriber/new abo, or needing Reels.
- agency_owner (icp_score 82..87): Real OFM agency founders/owners hiring chatters or scaling creators.

Reject (is_buyer=false):
- Job seekers, available VAs/chatters, chatter middleman recruiters ("recruteur de chatteurs"), KYC/fraud/emulator/payment hacks, L4L/SFS/F4F promo spam, account sellers, casino, or bots.

Return STRICT JSON array only, matching the input indices:
[{"index": 0, "is_buyer": true, "ofm_tier": "traffic_buyer", "icp_score": 92, "reason": "max 10 words"}]"""


def refine_ofm_buyers_with_llm(
    candidates: list[dict[str, Any]],
    *,
    api_key: str = "",
    base_url: str = "",
) -> list[dict[str, Any]]:
    """Optional batched RelayFast verification. Deterministic pass-through when no key."""
    if not candidates:
        return []
    eff_key = (
        api_key
        or os.environ.get("RELAYFAST_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or ""
    ).strip()
    if not eff_key:
        return [dict(c) for c in candidates]

    eff_base = (
        base_url
        or os.environ.get("LLM_BASE_URL")
        or "https://relay.fast/v1"
    ).strip().rstrip("/")

    try:
        from leadgen.relay_route import note_failure
    except Exception:
        note_failure = None

    batch_size = 12
    verified: list[dict[str, Any]] = []
    models = _llm_model_chain()[:2]
    deadline = time.monotonic() + 45.0

    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]
        compact = [
            {
                "index": idx,
                "name": str(c.get("name") or "")[:80],
                "username": str(c.get("username") or "")[:64],
                "ofm_tier": str(c.get("ofm_tier") or ""),
                "icp_score": int(c.get("icp_score") or 84),
                "bio": str(c.get("bio") or "")[:300],
                "sample_message": str(c.get("sample_message") or c.get("message") or "")[:400],
            }
            for idx, c in enumerate(chunk)
        ]
        messages = [
            {"role": "system", "content": _OFM_LLM_PROMPT},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ]

        parsed_judgments: dict[int, dict[str, Any]] | None = None
        for model in models:
            rem = deadline - time.monotonic()
            if rem <= 3.0:
                break
            try:
                raw_content = _post_chat_completions(
                    base_url=eff_base,
                    api_key=eff_key,
                    model=model,
                    messages=messages,
                    timeout=max(4, min(20, int(rem))),
                )
                cleaned = re.sub(
                    r"^```(?:json)?\s*|\s*```$", "", raw_content, flags=re.M
                ).strip()
                match = re.search(r"\[.*\]", cleaned, flags=re.S)
                if not match:
                    raise ValueError("LLM response missing JSON array")
                arr = json.loads(match.group(0))
                if not isinstance(arr, list):
                    raise ValueError("LLM JSON is not a list")
                tmp_map: dict[int, dict[str, Any]] = {}
                for pos, item in enumerate(arr):
                    if isinstance(item, dict):
                        j_idx = int(item.get("index", pos))
                        tmp_map[j_idx] = item
                parsed_judgments = tmp_map
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                if note_failure and note_failure(model, exc.code, body):
                    continue
                break
            except Exception:
                if note_failure and note_failure(model, 0, ""):
                    continue
                continue

        if parsed_judgments is None:
            verified.extend(dict(c) for c in chunk)
            continue

        for idx, cand in enumerate(chunk):
            item = parsed_judgments.get(idx)
            if item is None:
                verified.append(dict(cand))
                continue
            if not bool(item.get("is_buyer")):
                continue
            updated = dict(cand)
            tier = str(item.get("ofm_tier") or "").strip()
            if tier in {"traffic_buyer", "agency_owner"}:
                updated["ofm_tier"] = tier
            try:
                score = int(item.get("icp_score"))
                if 70 <= score <= 100:
                    updated["icp_score"] = score
            except (TypeError, ValueError):
                pass
            verified.append(updated)

    return verified


def _fresh_seen(seen: dict[str, str], day: date, keep_days: int = SEEN_DAYS) -> dict[str, str]:
    cutoff = day - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for key, raw in (seen or {}).items():
        try:
            seen_on = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if seen_on >= cutoff:
            kept[str(key)] = seen_on.isoformat()
    return kept


def _load_seen_map(path: Path, today: date) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            inner = raw.get("items") if isinstance(raw.get("items"), dict) else raw
            clean = {str(k): str(v) for k, v in inner.items() if isinstance(v, str)}
            return _fresh_seen(clean, today)
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_seen_map(path: Path, seen: dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"items": seen}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not save seen state %s: %s", path, exc)


def _clean_group_list(groups: Iterable[str] | None, fallback: tuple[str, ...]) -> list[str]:
    source = list(groups) if groups is not None else list(fallback)
    out: list[str] = []
    seen_low: set[str] = set()
    for raw in source:
        g = str(raw or "").strip()
        g = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "", g, flags=re.I)
        g = g.lstrip("@").strip("/ ")
        if g and g.casefold() not in seen_low:
            seen_low.add(g.casefold())
            out.append(g)
    return out


def _default_tg_session(explicit: str = "") -> str:
    return (
        str(explicit or "").strip()
        or (os.environ.get("TG_SESSION") or "").strip()
        or (os.environ.get("SCRAPER_TG_SESSION") or "").strip()
    )


def _session_file_exists(session_path: str) -> bool:
    if not session_path:
        return False
    p = Path(session_path).expanduser()
    if p.is_file():
        return True
    if p.suffix.lower() != ".session" and Path(f"{p}.session").is_file():
        return True
    return False


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _run_coro(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _open_readonly_tg_client(
    *,
    session_path: str,
    api_id: int,
    api_hash: str,
) -> tuple[Any | None, str]:
    eff_session = _default_tg_session(session_path)
    eff_api_id = int(api_id or os.environ.get("TG_API_ID") or 0)
    eff_api_hash = str(api_hash or os.environ.get("TG_API_HASH") or "").strip()
    if not eff_session or not eff_api_id or not eff_api_hash:
        return None, "missing_tg_credentials"
    if not _session_file_exists(eff_session):
        return None, "missing_session_file"
    try:
        from telethon import TelegramClient
    except Exception as exc:
        return None, f"telethon_import_error:{exc}"

    client = TelegramClient(eff_session, eff_api_id, eff_api_hash, flood_sleep_threshold=0)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None, "tg_session_not_authorized"
        return client, ""
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        return None, f"tg_connect_failed:{exc}"


def _is_non_human_tg_sender(msg: Any, sender: Any) -> bool:
    if getattr(msg, "post", False):
        return True
    from_id = getattr(msg, "from_id", None)
    if from_id is not None and "Channel" in type(from_id).__name__:
        return True
    if sender is not None:
        if getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return True
        if (
            getattr(sender, "broadcast", False)
            or getattr(sender, "megagroup", False)
            or "Channel" in type(sender).__name__
            or "Chat" in type(sender).__name__
        ):
            return True
    return False


def _is_flood_wait_error(exc: Exception) -> bool:
    return "FloodWait" in type(exc).__name__


async def _fetch_group_messages(tg_client: Any, group_name: str, msg_limit: int) -> list[Any]:
    entity = await _maybe_await(tg_client.get_entity(group_name))
    if entity is None:
        return []
    if hasattr(tg_client, "get_messages"):
        msgs = await _maybe_await(tg_client.get_messages(entity, limit=msg_limit))
        return list(msgs or [])
    if hasattr(tg_client, "iter_messages"):
        return [m async for m in tg_client.iter_messages(entity, limit=msg_limit)]
    return []


async def _discover_ofm_tg_groups(tg_client: Any, existing: list[str]) -> list[str]:
    extra: list[str] = []
    known = {g.casefold() for g in existing}
    try:
        from telethon.tl.functions.contacts import SearchRequest
    except Exception:
        return extra

    for query in OFM_DISCOVERY_QUERIES:
        try:
            res = await _maybe_await(tg_client(SearchRequest(q=query, limit=10)))
            for chat in getattr(res, "chats", None) or []:
                uname = str(getattr(chat, "username", "") or "").strip().lstrip("@")
                if not uname or uname.casefold() in known:
                    continue
                if getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False):
                    known.add(uname.casefold())
                    extra.append(uname)
                    if len(extra) >= 10:
                        return extra
        except Exception:
            continue
    return extra


async def _resolve_full_user(
    tg_client: Any,
    lookup_target: Any,
    sender_id: Any,
    fallback_username: str,
    fallback_name: str,
) -> dict[str, Any]:
    try:
        from telethon.tl.functions.users import GetFullUserRequest

        req = GetFullUserRequest(lookup_target)
    except Exception:
        req = lookup_target

    full = await _maybe_await(tg_client(req))
    users = getattr(full, "users", None) or []
    user_obj = next(
        (u for u in users if sender_id is not None and getattr(u, "id", None) == sender_id),
        users[0] if users else None,
    )
    bio = str(getattr(getattr(full, "full_user", None), "about", "") or "").strip()
    is_bot = bool(getattr(user_obj, "bot", False)) or bool(getattr(user_obj, "deleted", False))
    real_username = str(getattr(user_obj, "username", "") or fallback_username or "").strip().lstrip("@")
    full_name = " ".join(
        p
        for p in (
            getattr(user_obj, "first_name", "") or "",
            getattr(user_obj, "last_name", "") or "",
        )
        if p
    ).strip() or fallback_name
    return {
        "username": real_username,
        "name": full_name,
        "bio": bio,
        "bot": is_bot,
    }


async def _collect_ofm_tg_groups_async(
    *,
    groups: Iterable[str] | None = None,
    state_dir: str | Path | None = None,
    seen_path: str | Path | None = None,
    session_path: str = "",
    api_id: int = 0,
    api_hash: str = "",
    msg_limit: int = 250,
    limit: int = 40,
    discover_groups: bool = False,
    llm_verify: bool = False,
    delay: float = 0.35,
    max_full_user_lookups: int = 60,
    client: Any = None,
) -> dict[str, Any]:
    resolved_state_dir = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    resolved_seen_path = Path(seen_path) if seen_path is not None else (resolved_state_dir / "ofm_seen.json")
    today = date.today()
    today_iso = today.isoformat()
    seen = _load_seen_map(resolved_seen_path, today)

    own_client = client is None
    tg_client = client
    if own_client:
        tg_client, err = await _open_readonly_tg_client(
            session_path=session_path,
            api_id=api_id,
            api_hash=api_hash,
        )
        if tg_client is None:
            return {
                "ok": False,
                "error": err,
                "groups_scanned": [],
                "messages_scanned": 0,
                "full_user_lookups": 0,
                "candidates": 0,
                "ofm_added": 0,
                "rows": [],
                "state_dir": str(resolved_state_dir),
            }

    group_list = _clean_group_list(groups, DEFAULT_OFM_TG_GROUPS)
    scanned_groups: list[str] = []
    messages_scanned = 0
    full_user_lookups = 0
    flood_wait_hit = False
    profile_cache: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    seen_actor_keys: set[str] = set()

    try:
        if discover_groups:
            extra_groups = await _discover_ofm_tg_groups(tg_client, group_list)
            group_list.extend(extra_groups)

        for grp in group_list:
            try:
                messages = await _fetch_group_messages(tg_client, grp, int(msg_limit))
            except Exception as exc:
                logger.warning("OFM TG group %s failed: %s", grp, exc)
                continue
            scanned_groups.append(grp)

            for msg in messages:
                text = str(getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
                if not text:
                    continue
                messages_scanned += 1

                msg_id = int(getattr(msg, "id", 0) or 0)
                if msg_id > 0:
                    msg_key = f"msg:{grp.casefold()}:{msg_id}"
                else:
                    msg_digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
                    msg_key = f"mhash:{grp.casefold()}:{msg_digest}"
                if msg_key in seen:
                    continue

                sender = getattr(msg, "sender", None)
                sender_id = getattr(msg, "sender_id", None)
                if _is_non_human_tg_sender(msg, sender):
                    seen[msg_key] = today_iso
                    continue

                sender_uname = str(getattr(sender, "username", "") or "").strip().lstrip("@")
                sender_name = " ".join(
                    p
                    for p in (
                        getattr(sender, "first_name", "") or "",
                        getattr(sender, "last_name", "") or "",
                    )
                    if p
                ).strip()

                # Pre-screen BEFORE GetFullUserRequest so spam/bots/job-seekers/off-topic
                # never burn user lookup budget. When sender_id exists without an attached
                # username/contact yet, pass a non-bot placeholder for text pre-screening,
                # then re-run strictly with real username & bio after GetFullUserRequest.
                pre_contacts = extract_post_contacts(text, bio="", default_username=sender_uname)
                has_pre_contact = any(
                    (
                        pre_contacts["username"],
                        pre_contacts["x_url"],
                        pre_contacts["instagram_url"],
                        pre_contacts["email"],
                        pre_contacts["whatsapp"],
                    )
                )
                pre_uname_for_gate = (
                    sender_uname
                    if (sender_uname or has_pre_contact or sender_id is None)
                    else "pending_user_lookup"
                )
                pre_cls = classify_ofm_actor(
                    name=sender_name,
                    username=pre_uname_for_gate,
                    bio="",
                    text=text,
                )
                if not pre_cls["is_buyer"]:
                    seen[msg_key] = today_iso
                    continue

                # Fetch full user profile (bio + verified username) within lookup cap
                real_uname = sender_uname
                full_name = sender_name
                bio = ""
                lookup_target = sender_id if sender_id is not None else (pre_contacts["username"] or None)
                cache_key = str(lookup_target).casefold() if lookup_target is not None else ""
                if cache_key and cache_key in profile_cache:
                    prof = profile_cache[cache_key]
                    if prof.get("bot"):
                        seen[msg_key] = today_iso
                        continue
                    real_uname = prof.get("username") or real_uname
                    full_name = prof.get("name") or full_name
                    bio = prof.get("bio") or ""
                elif (
                    not flood_wait_hit
                    and lookup_target is not None
                    and full_user_lookups < int(max_full_user_lookups)
                ):
                    try:
                        full_user_lookups += 1
                        prof = await _resolve_full_user(
                            tg_client,
                            lookup_target,
                            sender_id,
                            sender_uname,
                            sender_name,
                        )
                        if cache_key:
                            profile_cache[cache_key] = prof
                        if prof.get("bot"):
                            seen[msg_key] = today_iso
                            continue
                        real_uname = prof.get("username") or real_uname
                        full_name = prof.get("name") or full_name
                        bio = prof.get("bio") or ""
                    except Exception as exc:
                        if _is_flood_wait_error(exc):
                            flood_wait_hit = True
                            logger.warning("OFM TG FloodWait during GetFullUserRequest: stopping further lookups (%s)", exc)
                        else:
                            logger.debug("GetFullUserRequest failed for %s: %s", lookup_target, exc)
                    if delay and float(delay) > 0 and not flood_wait_hit:
                        await asyncio.sleep(float(delay))

                # Re-run classification WITH full bio & real username
                final_cls = classify_ofm_actor(
                    name=full_name,
                    username=real_uname,
                    bio=bio,
                    text=text,
                )
                seen[msg_key] = today_iso
                if not final_cls["is_buyer"]:
                    continue

                final_contacts = extract_post_contacts(text, bio=bio, default_username=real_uname)
                actor_key = ""
                for ck_prefix, ck_val in (
                    ("tg", final_contacts["username"]),
                    ("x", final_contacts["x_url"]),
                    ("ig", final_contacts["instagram_url"]),
                    ("email", final_contacts["email"]),
                    ("wa", final_contacts["whatsapp"]),
                ):
                    if ck_val:
                        actor_key = f"{ck_prefix}:{ck_val.casefold()}"
                        break
                if not actor_key or actor_key in seen or actor_key in seen_actor_keys:
                    continue
                seen_actor_keys.add(actor_key)

                candidates.append(
                    {
                        "name": full_name or final_contacts["username"] or "OFM Buyer",
                        "company": full_name or None,
                        "username": final_contacts["username"] or None,
                        "x_url": final_contacts["x_url"] or None,
                        "instagram_url": final_contacts["instagram_url"] or None,
                        "email": final_contacts["email"] or None,
                        "whatsapp": final_contacts["whatsapp"] or None,
                        "url": final_contacts["url"] or None,
                        "source": "ofm_tg",
                        "source_group": grp,
                        "ofm_tier": final_cls["ofm_tier"],
                        "icp_score": final_cls["icp_score"],
                        "market": final_cls["market"],
                        "channels": final_cls["channels"],
                        "signal_summary": final_cls["signal_summary"],
                        "sample_message": text[:400],
                        "bio": bio[:400],
                        "_actor_key": actor_key,
                    }
                )
    finally:
        if own_client and tg_client is not None:
            try:
                await tg_client.disconnect()
            except Exception:
                pass

    # Prioritize traffic_buyer over agency_owner, then higher ICP score
    candidates.sort(
        key=lambda c: (1 if c.get("ofm_tier") == "traffic_buyer" else 0, int(c.get("icp_score") or 0)),
        reverse=True,
    )
    if limit and int(limit) > 0:
        candidates = candidates[: int(limit)]

    if llm_verify and candidates:
        candidates = refine_ofm_buyers_with_llm(candidates)

    persisted_rows: list[dict[str, Any]] = []
    ofm_added = 0
    for cand in candidates:
        row = {k: v for k, v in cand.items() if not k.startswith("_")}
        actor_key = cand.get("_actor_key")
        if actor_key:
            seen[actor_key] = today_iso
        if append_ofm_review_lead(resolved_state_dir, row):
            ofm_added += 1
            persisted_rows.append(row)

    _save_seen_map(resolved_seen_path, seen)

    return {
        "ok": True,
        "groups_scanned": scanned_groups,
        "messages_scanned": messages_scanned,
        "full_user_lookups": full_user_lookups,
        "candidates": len(candidates),
        "ofm_added": ofm_added,
        "rows": persisted_rows,
        "state_dir": str(resolved_state_dir),
    }


def collect_ofm_tg_groups(
    *,
    groups: Iterable[str] | None = None,
    state_dir: str | Path | None = None,
    seen_path: str | Path | None = None,
    session_path: str = "",
    api_id: int = 0,
    api_hash: str = "",
    msg_limit: int = 250,
    limit: int = 40,
    discover_groups: bool = False,
    llm_verify: bool = False,
    delay: float = 0.35,
    max_full_user_lookups: int = 60,
    client: Any = None,
) -> dict[str, Any]:
    """Scan OFM Telegram groups read-only and write verified buyers to ofm_review_leads.jsonl."""
    return _run_coro(
        _collect_ofm_tg_groups_async(
            groups=groups,
            state_dir=state_dir,
            seen_path=seen_path,
            session_path=session_path,
            api_id=api_id,
            api_hash=api_hash,
            msg_limit=msg_limit,
            limit=limit,
            discover_groups=discover_groups,
            llm_verify=llm_verify,
            delay=delay,
            max_full_user_lookups=max_full_user_lookups,
            client=client,
        )
    )


def _is_non_ofm_b2b_group(group_name: str) -> bool:
    low = group_name.casefold()
    if low in {g.casefold() for g in DEFAULT_OFM_TG_GROUPS}:
        return False
    if any(tok in low for tok in ("ofm", "onlyfans", "fanvue")):
        return False
    return True


def _pick_b2b_product_url(text: str, bio: str) -> str:
    combined = "\n".join(p for p in (text or "", bio or "") if p)
    for raw_url in _URL_RE.findall(combined):
        cleaned = _clean_http_url(raw_url)
        if not cleaned or is_platform_or_social_url(cleaned):
            continue
        return cleaned
    return ""


async def _collect_b2b_tg_groups_async(
    *,
    groups: Iterable[str] | None = None,
    state_dir: str | Path | None = None,
    seen_path: str | Path | None = None,
    session_path: str = "",
    api_id: int = 0,
    api_hash: str = "",
    msg_limit: int = 300,
    limit: int = 25,
    delay: float = 0.35,
    max_full_user_lookups: int = 50,
    client: Any = None,
) -> list[dict[str, Any]]:
    persist_seen = seen_path is not None or state_dir is not None or client is None
    resolved_state_dir = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    eff_seen_path = (
        Path(seen_path)
        if seen_path is not None
        else (resolved_state_dir / "b2b_tg_groups_seen.json")
    )
    today = date.today()
    today_iso = today.isoformat()
    seen = _load_seen_map(eff_seen_path, today) if persist_seen else {}

    own_client = client is None
    tg_client = client
    if own_client:
        tg_client, _err = await _open_readonly_tg_client(
            session_path=session_path,
            api_id=api_id,
            api_hash=api_hash,
        )
        if tg_client is None:
            return []

    raw_groups = _clean_group_list(groups, DEFAULT_B2B_TG_GROUPS)
    group_list = [g for g in raw_groups if _is_non_ofm_b2b_group(g)]
    rows: list[dict[str, Any]] = []
    full_user_lookups = 0
    flood_wait_hit = False
    profile_cache: dict[str, dict[str, Any]] = {}
    run_seen_users: set[str] = set()

    try:
        for grp in group_list:
            if len(rows) >= int(limit):
                break
            try:
                messages = await _fetch_group_messages(tg_client, grp, int(msg_limit))
            except Exception as exc:
                logger.warning("B2B TG group %s failed: %s", grp, exc)
                continue

            for msg in messages:
                if len(rows) >= int(limit):
                    break
                text = str(getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
                if not text:
                    continue

                # Pre-filter obvious supply-side pitches or tiny noise without URL
                has_raw_url = bool(_URL_RE.search(text))
                has_intent = bool(_B2B_BUYER_INTENT_RE.search(text))
                if len(text) < 8 and not has_raw_url and not has_intent:
                    continue
                if is_supply_side(text) or _OFM_CONTEXT_RE.search(text):
                    continue

                sender = getattr(msg, "sender", None)
                sender_id = getattr(msg, "sender_id", None)
                if _is_non_human_tg_sender(msg, sender):
                    continue

                sender_uname = str(getattr(sender, "username", "") or "").strip().lstrip("@")
                sender_name = " ".join(
                    p
                    for p in (
                        getattr(sender, "first_name", "") or "",
                        getattr(sender, "last_name", "") or "",
                    )
                    if p
                ).strip()

                real_uname = sender_uname
                full_name = sender_name
                bio = ""
                lookup_target = sender_id if sender_id is not None else (sender_uname or None)
                cache_key = str(lookup_target).casefold() if lookup_target is not None else ""
                if cache_key and cache_key in profile_cache:
                    prof = profile_cache[cache_key]
                    if prof.get("bot"):
                        continue
                    real_uname = prof.get("username") or real_uname
                    full_name = prof.get("name") or full_name
                    bio = prof.get("bio") or ""
                elif (
                    not flood_wait_hit
                    and lookup_target is not None
                    and full_user_lookups < int(max_full_user_lookups)
                ):
                    try:
                        full_user_lookups += 1
                        prof = await _resolve_full_user(
                            tg_client,
                            lookup_target,
                            sender_id,
                            sender_uname,
                            sender_name,
                        )
                        if cache_key:
                            profile_cache[cache_key] = prof
                        if prof.get("bot"):
                            continue
                        real_uname = prof.get("username") or real_uname
                        full_name = prof.get("name") or full_name
                        bio = prof.get("bio") or ""
                    except Exception as exc:
                        if _is_flood_wait_error(exc):
                            flood_wait_hit = True
                            logger.warning("B2B TG FloodWait during GetFullUserRequest: %s", exc)
                        else:
                            logger.debug("B2B GetFullUserRequest failed for %s: %s", lookup_target, exc)
                    if delay and float(delay) > 0 and not flood_wait_hit:
                        await asyncio.sleep(float(delay))

                uname = _clean_tg_handle(real_uname)
                if not uname:
                    continue
                u_key = f"tg:{uname.casefold()}"
                if u_key in run_seen_users or u_key in seen:
                    continue

                combined = "\n".join(p for p in (full_name, bio, text) if p).strip()
                if _OFM_CONTEXT_RE.search(combined):
                    continue
                if is_supply_side(combined):
                    continue

                project_url = _pick_b2b_product_url(text, bio)
                has_buyer_intent = bool(_B2B_BUYER_INTENT_RE.search(combined))
                if not project_url and not has_buyer_intent:
                    continue

                if market_of(project_url, combined) == "ru":
                    continue
                if project_url and re.search(
                    r"(?:^|/)(?:ru|rus)(?:/|$)", urlparse(project_url).path.lower()
                ):
                    continue

                inferred_role = classify_role(bio) or classify_role(text)
                if inferred_role in NON_BUYER_ROLES and not has_buyer_intent:
                    continue
                final_role = inferred_role if inferred_role in {"Founder", "Marketing"} else "Founder"

                category = classify_cat(bio or text) or "SaaS / AI app"
                if category == "Сервис / агентство":
                    continue

                clean_name, clean_comp = sanitize_person_and_company(
                    full_name or uname,
                    "",
                    site=project_url,
                    bio=bio or text,
                )
                company = clean_comp or clean_name or uname
                if is_invalid_product_name(company):
                    company = uname

                what_they_do = (bio or text)[:200].strip()
                if sniper_reject_reason(
                    {
                        "name": clean_name or uname,
                        "username": uname,
                        "company": company,
                        "project": company,
                        "bio": bio,
                        "text": text,
                        "sample": text,
                        "category": category,
                        "role": final_role,
                        "roles": [final_role],
                    }
                ):
                    continue
                if sniper_lead_violation_reason(
                    name=clean_name or uname,
                    company=company,
                    niche=category,
                    note=f"{bio}\n{text}".strip(),
                    description=what_they_do,
                    url=project_url,
                    lead_source="tg_groups",
                    vacancies_enabled=False,
                ):
                    continue

                contacts = extract_post_contacts(text, bio=bio, default_username=uname)
                links = [
                    u
                    for u in (
                        project_url,
                        f"https://t.me/{uname}",
                        contacts["x_url"],
                    )
                    if u
                ]

                run_seen_users.add(u_key)
                seen[u_key] = today_iso
                rows.append(
                    {
                        "id": f"tg_groups:{uname.casefold()}",
                        "name": clean_name or uname,
                        "username": uname,
                        "telegram_username": uname,
                        "tg_url": f"https://t.me/{uname}",
                        "bio": bio[:400],
                        "sample": text[:280],
                        "links": list(dict.fromkeys(links)),
                        "source": "tg_groups",
                        "lead_source": "tg_groups",
                        "source_group": grp,
                        "role": final_role,
                        "roles": [final_role],
                        "category": category,
                        "project": company,
                        "company": company,
                        "project_url": project_url,
                        "url": project_url,
                        "what_they_do": what_they_do,
                        "their_angle": what_they_do,
                        "founder_x": contacts["x_url"],
                        "x_url": contacts["x_url"],
                        "work_email": contacts["email"],
                        "email": contacts["email"],
                        "whatsapp": contacts["whatsapp"],
                        "has_monetization_signal": True,
                        "icp_match": True,
                        "icp_score": 85,
                        "lead_tier": "A" if (project_url and has_buyer_intent) else "B",
                    }
                )
    finally:
        if own_client and tg_client is not None:
            try:
                await tg_client.disconnect()
            except Exception:
                pass

    if persist_seen:
        _save_seen_map(eff_seen_path, seen)

    return rows


def collect_b2b_tg_groups(
    *,
    groups: Iterable[str] | None = None,
    state_dir: str | Path | None = None,
    seen_path: str | Path | None = None,
    session_path: str = "",
    api_id: int = 0,
    api_hash: str = "",
    msg_limit: int = 300,
    limit: int = 25,
    delay: float = 0.35,
    max_full_user_lookups: int = 50,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Scan non-OFM B2B founder/SaaS TG groups and return canonical B2B lead dicts."""
    return _run_coro(
        _collect_b2b_tg_groups_async(
            groups=groups,
            state_dir=state_dir,
            seen_path=seen_path,
            session_path=session_path,
            api_id=api_id,
            api_hash=api_hash,
            msg_limit=msg_limit,
            limit=limit,
            delay=delay,
            max_full_user_lookups=max_full_user_lookups,
            client=client,
        )
    )


def _default_treg_caller(
    endpoint: str,
    *,
    method: str | None = None,
    data: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any] | None:
    if not check_treg_ready():
        return None
    return treg_call(endpoint, method=method, data=data, query=query, timeout=timeout)


def _invoke_treg(
    treg_fn: Callable[..., Any],
    endpoint: str,
    *,
    method: str | None = None,
    data: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if method is not None:
        kwargs["method"] = method
    if data is not None:
        kwargs["data"] = data
    if query is not None:
        kwargs["query"] = query

    try:
        sig = inspect.signature(treg_fn)
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if not has_var_kw:
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (TypeError, ValueError):
        pass

    try:
        return treg_fn(endpoint, **kwargs)
    except Exception as exc:
        logger.warning("treg call %s failed: %s", endpoint, exc)
        return None


def _unwrap_search_hits(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "organic_results"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            return [x for x in val if isinstance(x, dict)]
    for sub_key in ("output", "result", "data"):
        sub = payload.get(sub_key)
        if isinstance(sub, (dict, list)):
            hits = _unwrap_search_hits(sub)
            if hits:
                return hits
    return []


def _extract_fb_posts(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("posts", "items", "results", "feed"):
        val = payload.get(key)
        if isinstance(val, list) and val:
            return [x for x in val if isinstance(x, dict)]
    for sub_key in ("output", "data", "result"):
        sub = payload.get(sub_key)
        if isinstance(sub, (dict, list)):
            nested = _extract_fb_posts(sub)
            if nested:
                return nested
    return []


def _normalize_fb_group_url(raw: str) -> str:
    match = _FB_GROUP_URL_RE.search(str(raw or "").strip())
    if not match:
        return ""
    slug = match.group(1).strip("/")
    if not slug or slug.casefold() in _FB_NOISE_SLUGS:
        return ""
    return f"https://www.facebook.com/groups/{slug}"


def _extract_fb_post_fields(post: dict[str, Any]) -> dict[str, str]:
    author_obj = post.get("author") if isinstance(post.get("author"), dict) else {}
    user_obj = post.get("user") if isinstance(post.get("user"), dict) else {}
    from_obj = post.get("from") if isinstance(post.get("from"), dict) else {}
    actor_obj = post.get("actor") if isinstance(post.get("actor"), dict) else {}

    text = ""
    for k in ("text", "message", "body", "content", "post_text", "description", "story"):
        val = post.get(k)
        if isinstance(val, str) and val.strip():
            text = val.strip()
            break

    author_name = ""
    for src in (post, author_obj, user_obj, from_obj, actor_obj):
        for k in ("authorName", "author_name", "userName", "user_name", "name", "title"):
            val = src.get(k)
            if isinstance(val, str) and val.strip():
                author_name = val.strip()
                break
        if author_name:
            break

    author_bio = str(
        post.get("authorBio")
        or post.get("author_bio")
        or post.get("bio")
        or author_obj.get("bio")
        or user_obj.get("bio")
        or actor_obj.get("bio")
        or ""
    ).strip()

    extra_links: list[str] = []
    for k in ("external_url", "externalUrl", "website", "attached_url", "attachedUrl", "link", "url", "post_url", "postUrl", "permalink"):
        val = post.get(k)
        if isinstance(val, str) and val.strip():
            extra_links.append(val.strip())
    for k in ("email", "whatsapp", "phone"):
        val = post.get(k)
        if isinstance(val, str) and val.strip():
            extra_links.append(val.strip())

    post_url = str(post.get("postUrl") or post.get("post_url") or post.get("permalink") or post.get("url") or "").strip()
    return {
        "text": text,
        "author_name": author_name,
        "author_bio": author_bio,
        "extra_blob": " ".join(extra_links),
        "post_url": post_url,
    }


def collect_facebook_groups(
    *,
    group_urls: Iterable[str] | None = None,
    state_dir: str | Path | None = None,
    limit: int = 20,
    max_groups: int = 5,
    treg_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Discover & scrape public Facebook groups via Treg, routing OFM vs B2B leads."""
    resolved_state_dir = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    caller = _default_treg_caller if treg_fn is None else treg_fn

    normalized_urls: list[str] = []
    seen_urls: set[str] = set()
    for raw_u in group_urls or ():
        norm = _normalize_fb_group_url(str(raw_u))
        if norm and norm.casefold() not in seen_urls:
            seen_urls.add(norm.casefold())
            normalized_urls.append(norm)

    if len(normalized_urls) < int(max_groups):
        for q in DEFAULT_FB_GROUP_DISCOVERY_QUERIES:
            if len(normalized_urls) >= int(max_groups):
                break
            search_payload = _invoke_treg(caller, "crawl4ai.web.search", query={"q": q})
            for hit in _unwrap_search_hits(search_payload):
                for field in ("url", "link", "snippet", "description"):
                    val = hit.get(field)
                    if not isinstance(val, str):
                        continue
                    for m in _FB_GROUP_URL_RE.finditer(val):
                        norm = _normalize_fb_group_url(m.group(0))
                        if norm and norm.casefold() not in seen_urls:
                            seen_urls.add(norm.casefold())
                            normalized_urls.append(norm)
                            if len(normalized_urls) >= int(max_groups):
                                break
                    if len(normalized_urls) >= int(max_groups):
                        break
                if len(normalized_urls) >= int(max_groups):
                    break

    b2b_rows: list[dict[str, Any]] = []
    ofm_rows: list[dict[str, Any]] = []
    ofm_added = 0
    seen_b2b_keys: set[str] = set()

    for g_url in normalized_urls[: int(max_groups)]:
        if len(b2b_rows) + len(ofm_rows) >= int(limit):
            break

        payload = _invoke_treg(
            caller,
            "anyapi.facebook.group.posts",
            method="POST",
            data={"url": g_url, "sort": "RECENT_ACTIVITY"},
        )
        posts = _extract_fb_posts(payload)
        if not posts:
            fallback_payload = _invoke_treg(
                caller,
                "scrapecreators.x.v1-facebook-group-posts",
                query={"url": g_url},
            )
            posts = _extract_fb_posts(fallback_payload)

        group_is_ofm = bool(_OFM_CONTEXT_RE.search(g_url))

        for post in posts:
            if len(b2b_rows) + len(ofm_rows) >= int(limit):
                break
            fields = _extract_fb_post_fields(post)
            post_text = fields["text"]
            if not post_text:
                continue

            post_blob = "\n".join(
                p for p in (post_text, fields["extra_blob"]) if p
            ).strip()
            contacts = extract_post_contacts(post_blob, bio=fields["author_bio"])
            has_direct_contact = any(
                (
                    contacts["username"],
                    contacts["x_url"],
                    contacts["instagram_url"],
                    contacts["email"],
                    contacts["whatsapp"],
                )
            )
            if not has_direct_contact and not contacts["url"]:
                continue

            full_blob = "\n".join(
                p for p in (fields["author_name"], fields["author_bio"], post_blob) if p
            ).strip()
            is_ofm_post = bool(_OFM_CONTEXT_RE.search(full_blob)) or group_is_ofm
            ofm_cls = classify_ofm_actor(
                name=fields["author_name"],
                username=contacts["username"],
                bio=fields["author_bio"],
                text=post_blob,
            )

            if is_ofm_post or ofm_cls["is_buyer"]:
                if not ofm_cls["is_buyer"]:
                    continue
                ofm_row = {
                    "name": fields["author_name"] or contacts["username"] or "OFM Buyer",
                    "company": fields["author_name"] or None,
                    "username": contacts["username"] or None,
                    "x_url": contacts["x_url"] or None,
                    "instagram_url": contacts["instagram_url"] or None,
                    "email": contacts["email"] or None,
                    "whatsapp": contacts["whatsapp"] or None,
                    "url": contacts["url"] or None,
                    "source": "fb_groups",
                    "source_group": g_url,
                    "post_url": fields["post_url"],
                    "ofm_tier": ofm_cls["ofm_tier"],
                    "icp_score": ofm_cls["icp_score"],
                    "market": ofm_cls["market"],
                    "channels": ofm_cls["channels"],
                    "signal_summary": ofm_cls["signal_summary"],
                    "sample_message": post_text[:400],
                }
                ofm_rows.append(ofm_row)
                if append_ofm_review_lead(resolved_state_dir, ofm_row):
                    ofm_added += 1
                continue

            # B2B Facebook post path (never accepts OFM or supply-side)
            if not has_direct_contact:
                continue
            if _OFM_CONTEXT_RE.search(full_blob) or is_supply_side(full_blob):
                continue

            project_url = contacts["url"] if (contacts["url"] and not is_platform_or_social_url(contacts["url"])) else ""
            has_intent = bool(_B2B_BUYER_INTENT_RE.search(full_blob))
            if not project_url and not has_intent:
                continue
            if market_of(project_url, full_blob) == "ru":
                continue

            clean_name, clean_comp = sanitize_person_and_company(
                fields["author_name"] or contacts["username"] or contacts["email"],
                "",
                site=project_url,
                bio=post_text,
            )
            company = clean_comp or clean_name or contacts["username"] or contacts["email"] or "FB Lead"
            if is_invalid_product_name(company):
                company = contacts["username"] or "FB Lead"
            category = classify_cat(full_blob) or "SaaS / AI app"
            if category == "Сервис / агентство":
                continue

            if sniper_reject_reason(
                {
                    "name": clean_name or fields["author_name"],
                    "username": contacts["username"],
                    "company": company,
                    "project": company,
                    "bio": fields["author_bio"],
                    "text": post_text,
                    "category": category,
                    "role": "Founder",
                    "roles": ["Founder"],
                }
            ):
                continue
            if sniper_lead_violation_reason(
                name=clean_name or fields["author_name"],
                company=company,
                niche=category,
                note=post_text,
                description=post_text[:200],
                url=project_url,
                lead_source="fb_groups",
                vacancies_enabled=False,
            ):
                continue

            dedup_key = (
                f"tg:{contacts['username'].casefold()}"
                if contacts["username"]
                else f"em:{contacts['email'].casefold()}"
                if contacts["email"]
                else f"x:{contacts['x_url'].casefold()}"
                if contacts["x_url"]
                else f"wa:{contacts['whatsapp']}"
                if contacts["whatsapp"]
                else f"ig:{contacts['instagram_url'].casefold()}"
            )
            if dedup_key in seen_b2b_keys:
                continue
            seen_b2b_keys.add(dedup_key)

            links = [
                u
                for u in (
                    project_url,
                    f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                    contacts["x_url"],
                    contacts["instagram_url"],
                    contacts["whatsapp"],
                )
                if u
            ]
            b2b_rows.append(
                {
                    "id": f"fb_groups:{hashlib.sha1(dedup_key.encode()).hexdigest()[:12]}",
                    "name": clean_name or fields["author_name"] or company,
                    "username": contacts["username"],
                    "telegram_username": contacts["username"],
                    "tg_url": f"https://t.me/{contacts['username']}" if contacts["username"] else "",
                    "x_url": contacts["x_url"],
                    "founder_x": contacts["x_url"],
                    "instagram_url": contacts["instagram_url"],
                    "email": contacts["email"],
                    "work_email": contacts["email"],
                    "whatsapp": contacts["whatsapp"],
                    "url": project_url,
                    "project_url": project_url,
                    "links": list(dict.fromkeys(links)),
                    "source": "fb_groups",
                    "lead_source": "fb_groups",
                    "source_group": g_url,
                    "role": "Founder",
                    "roles": ["Founder"],
                    "category": category,
                    "project": company,
                    "company": company,
                    "what_they_do": post_text[:200],
                    "their_angle": post_text[:200],
                    "has_monetization_signal": True,
                    "icp_match": True,
                    "icp_score": 85,
                    "lead_tier": "A" if (project_url and has_intent) else "B",
                }
            )

    return {
        "b2b_rows": b2b_rows,
        "ofm_rows": ofm_rows,
        "ofm_added": ofm_added,
    }


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_allowed(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="OFM & B2B Telegram groups + public Facebook groups collector"
    )
    parser.add_argument(
        "--mode",
        choices=("ofm_tg", "b2b_tg", "fb_groups", "all"),
        default="all",
    )
    parser.add_argument("--groups", default="", help="Comma-separated Telegram group usernames")
    parser.add_argument("--fb-urls", default="", help="Comma-separated Facebook group URLs")
    parser.add_argument("--max-groups", type=int, default=5, help="Max Facebook groups to scan")
    parser.add_argument("--msg-limit", type=int, default=250)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--output", default="", help="Output JSONL path for B2B rows only")
    parser.add_argument("--ofm-out", default="", help="State dir override or JSONL path for OFM leads")
    parser.add_argument("--seen", default="", help="Optional path to ofm_seen.json")
    parser.add_argument("--discover-groups", action="store_true")
    parser.add_argument("--llm-verify", action="store_true")
    parser.add_argument("--no-llm-verify", action="store_true")
    parser.add_argument("--session", default="", help="Path to read-only Telethon .session file")
    args = parser.parse_args(argv)

    groups_arg = [g.strip() for g in args.groups.split(",") if g.strip()] or None
    fb_urls_arg = [u.strip() for u in args.fb_urls.split(",") if u.strip()] or None

    ofm_custom_jsonl: Path | None = None
    if args.ofm_out:
        ofm_out_path = Path(args.ofm_out)
        if ofm_out_path.suffix.lower() == ".jsonl":
            state_dir = ofm_out_path.parent
            if ofm_out_path.name != "ofm_review_leads.jsonl":
                ofm_custom_jsonl = ofm_out_path
        else:
            state_dir = ofm_out_path
    else:
        state_dir = _resolve_state_dir()

    seen_arg = Path(args.seen) if args.seen else None
    eff_session = _default_tg_session(args.session)
    eff_llm_verify = (
        False
        if args.no_llm_verify
        else bool(
            args.llm_verify
            or (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
        )
    )

    b2b_rows: list[dict[str, Any]] = []
    ofm_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"ok": True, "mode": args.mode}

    if args.mode in {"ofm_tg", "all"}:
        ofm_res = collect_ofm_tg_groups(
            groups=groups_arg,
            state_dir=state_dir,
            seen_path=seen_arg,
            session_path=eff_session,
            msg_limit=args.msg_limit,
            limit=args.limit,
            discover_groups=args.discover_groups,
            llm_verify=eff_llm_verify,
        )
        ofm_rows.extend(ofm_res.get("rows") or [])
        summary["ofm_tg"] = {
            "ok": ofm_res.get("ok"),
            "error": ofm_res.get("error", ""),
            "groups_scanned": len(ofm_res.get("groups_scanned") or []),
            "messages_scanned": ofm_res.get("messages_scanned", 0),
            "full_user_lookups": ofm_res.get("full_user_lookups", 0),
            "ofm_added": ofm_res.get("ofm_added", 0),
        }

    if args.mode in {"b2b_tg", "all"}:
        b2b_tg_rows = collect_b2b_tg_groups(
            groups=groups_arg,
            state_dir=state_dir,
            seen_path=seen_arg if args.mode == "b2b_tg" else None,
            session_path=eff_session,
            msg_limit=args.msg_limit,
            limit=args.limit,
        )
        b2b_rows.extend(b2b_tg_rows)
        summary["b2b_tg"] = {"count": len(b2b_tg_rows)}

    if args.mode in {"fb_groups", "all"}:
        fb_res = collect_facebook_groups(
            group_urls=fb_urls_arg,
            state_dir=state_dir,
            limit=args.limit,
            max_groups=max(1, int(args.max_groups)),
        )
        fb_b2b = fb_res.get("b2b_rows") or []
        fb_ofm = fb_res.get("ofm_rows") or []
        b2b_rows.extend(fb_b2b)
        ofm_rows.extend(fb_ofm)
        summary["fb_groups"] = {
            "b2b_count": len(fb_b2b),
            "ofm_count": len(fb_ofm),
            "ofm_added": fb_res.get("ofm_added", 0),
        }

    if ofm_custom_jsonl is not None and ofm_rows:
        ofm_custom_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with ofm_custom_jsonl.open("a", encoding="utf-8") as fh:
            for row in ofm_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for row in b2b_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary["output"] = str(out_path)
        summary["b2b_written"] = len(b2b_rows)

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
