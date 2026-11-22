#!/usr/bin/env python3
"""Legitimate public-feed, RSS/Atom, and email-digest monitors for freelance platforms.

Monitors `Profi.ru` (`profi_ru`), `Kwork` (`kwork`), and `Upwork` (`upwork`) for
short-form video / Reels / Shorts / UGC / AI-video orders and prepares
`segment="vacancy_assisted"` rows for the Assisted Job Queue (`manual_action_url`
+ personalized `manual_draft_text`).

Policy:
- Strictly NO CAPTCHA, Cloudflare, or anti-bot bypasses.
- HTTP 403/429 or any CAPTCHA/anti-bot marker returns structured
  `{"status": "blocked_by_antibot", "rows": []}` honestly without retry evasion.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from email import message_from_bytes, message_from_string
from email.header import decode_header
from email.message import Message
import hashlib
import html as html_lib
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import requests

SUPPORTED_SOURCES: tuple[str, ...] = ("profi_ru", "kwork", "upwork")
DEFAULT_SHOWCASE_URL = "https://tmxfy.github.io/"
DEFAULT_USER_AGENT = "SoftUGC-Leadgen/1.0 (+https://softugc.com)"

DEFAULT_FEED_URLS: dict[str, str] = {
    "profi_ru": "https://profi.ru/zakazy/",
    "kwork": "https://kwork.ru/projects",
    "upwork": "https://www.upwork.com/nx/search/jobs/?q=UGC+Reels+AI+video",
}

_SOURCE_ENV_KEYS: dict[str, str] = {
    "profi_ru": "PROFI_RU_FEED_URL",
    "kwork": "KWORK_FEED_URL",
    "upwork": "UPWORK_FEED_URL",
}

_SOURCE_LANGUAGE: dict[str, str] = {
    "profi_ru": "ru",
    "kwork": "ru",
    "upwork": "en",
}

ANTIBOT_MARKERS: tuple[str, ...] = (
    "captcha",
    "smartcaptcha",
    "cloudflare",
    "verify you are human",
    "cf-browser-checking",
    "cf-turnstile",
    "turnstile",
    "g-recaptcha",
    "hcaptcha",
    "ddos-guard",
    "just a moment",
    "checking your browser",
    "подтвердите, что вы не робот",
    "проверка, что вы не робот",
    "введите символы с картинки",
    "bot protection",
)

_RESUME_SELF_PROMO_RE = re.compile(
    r"#резюме|#помогу|#ищу_?работу|ищу\s+работу|ищу\s+заказ\w*|возьму\s+заказ\w*"
    r"|мо[её]\s+портфолио|свободен\s+для\s+заказов|выполню\s+на\s+заказ\s+недорого",
    re.I,
)

_STORYMAKER_JUNK_RE = re.compile(
    r"стори[сз][\s_-]*мейкер|story[\s_-]*mak|(?:оформлени|ведени|монтаж)\w*\s+стори[сз]"
    r"|stories\s+(?:maker|manager|designer)",
    re.I,
)

_GAMING_OR_SPAM_RE = re.compile(
    r"\broblox\b|\bfortnite\b|\bfivem\b|\bmobile\s+games?\b|\bgame\s+studio\b"
    r"|\bcasino\b|\bslots\b|\bgambling\b|\bigaming\b|\bbetting\b|казино"
    r"|иллюстра(?:тор|ц)\w*[\s\S]{0,80}детск\w*\s+книг"
    r"|(?:монтаж|нарезк|рилсмейкер)\w*[\s\S]{0,35}\bмем(?:ов|ы|ами|ам)?\b",
    re.I,
)

_DEV_OR_NON_VIDEO_RE = re.compile(
    r"программист|разработчик|верстальщик|бэкенд|фронтенд|фуллстек"
    r"|\bfull[\s_-]*stack\b|\bback[\s_-]*end\b|\bfront[\s_-]*end\b"
    r"|\bsoftware\s+engineer\b|\bweb\s+developer\b|\bpython\s+developer\b"
    r"|\bios\s+developer\b|\bandroid\s+developer\b|\breact\s+developer\b"
    r"|\bwordpress\b|\btilda\b|тильд[аеу]|в[её]рстка\s+сайт|создать\s+сайт|разработка\s+сайта"
    r"|\b1с\b|битрикс|\bbitrix\b|парсер|скрейпер|\bscraper\b|\bdownloader\b"
    r"|чат[\s-]*бот|телеграм[\s-]*бот|\btelegram\s+bot\b|\btg\s+bot\b"
    r"|\bn8n\b|\bmake\.com\b|\bzapier\b|бухгалтер|юрист|дизайн\s+логотип",
    re.I,
)

_BUILD_SOFTWARE_PHRASE_RE = re.compile(
    r"(?:создать|разработать|написать|сделать|настроить|build|develop|create|code)\s+"
    r"(?:[\w-]+\s+){0,3}(?:бот[ауы]?|bot|парсер|scraper|downloader|сайт|лендинг|crm|n8n|приложение|web\s+app)",
    re.I,
)

_POSITIVE_VIDEO_RE = re.compile(
    r"рилс|рилсмейкер|\breels\b|\bshorts\b|шортс|тикток|\btiktok\b|\bugc\b"
    r"|short[\s-]*form|вертикальн\w*\s+(?:видео|ролик)|коротк\w+\s+(?:видео|ролик)"
    r"|(?:ai|нейро|ии)[\s-]*(?:видео|ролик\w*|креатор\w*|персонаж\w*|аватар\w*|ugc|video|avatar|reels)"
    r"|видеомонтаж|монтаж\w*\s+(?:видео|ролик|рилс|shorts|тикток|подкаст|интервью)"
    r"|видеомейкер|видеокреатор|видео\s+для\s+(?:рекламы|таргета|reels|tiktok|shorts|маркетплейс|wb|ozon|бренда)"
    r"|video\s+(?:editor|editing|creator|producer|ads)"
    r"|\bcapcut\b|\bheygen\b|\bkling\b|\bseedance\b|\bsora\b|\brunway\b|\belevenlabs\b"
    r"|\bfaceless\b|контент[\s-]*завод|нарезк\w*\s+(?:видео|рилс|shorts|подкаст|стрим)"
    r"|\bclipp(?:er|ing)\b|\btalking[\s-]*head\b",
    re.I,
)

_AI_AVATAR_HINT_RE = re.compile(
    r"(?:ai|нейро|ии)[\s-]*(?:персонаж|аватар|видео|ролик|avatar|video)|heygen|kling|seedance|sora|faceless|липсинк|lipsync",
    re.I,
)
_UGC_HINT_RE = re.compile(r"\bugc\b|креатор|creator|tiktok|тикток|маркетплейс|wb|ozon|meta\s+ads|реклам", re.I)

_BUDGET_RE = re.compile(
    r"(?:бюджет|budget|цена|оплата|до|от|rate|hourly)?\s*[:\-–]?\s*"
    r"(?:\$\s*\d[\d,\s.]*(?:\s*[-–]\s*\$?\d[\d,\s.]*)?(?:\s*(?:usd|/\s*hr|per\s+hour))?"
    r"|\d[\d\s]{1,10}\s*(?:₽|руб\.?|р\.|usd|\$))",
    re.I,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.I)

_SOURCE_URL_PATTERNS: dict[str, re.Pattern[str]] = {
    "profi_ru": re.compile(
        r"profi\.ru/(?:(?:backoffice/)?orders?/[^/?#]+|zakazy?/[^/?#]+|.*n\.php\?[^#\s]*\b(?:id|o|order)=)",
        re.I,
    ),
    "kwork": re.compile(r"kwork\.ru/(?:projects|wants)/[^/?#]+", re.I),
    "upwork": re.compile(r"upwork\.com/(?:nx/|ab/)?(?:jobs|freelance-jobs)/[^/?#]+", re.I),
}

_GENERIC_CTA_TEXT_RE = re.compile(
    r"^(?:откликнуться|посмотреть(?:\s+заказ)?|подробнее|перейти|apply(?:\s+now)?|view\s+job|see\s+more|open)$",
    re.I,
)


@dataclass(frozen=True)
class FreelanceFeedItem:
    """Normalized incoming order/job item from a legitimate feed, HTML listing, or email digest."""

    source: str
    title: str
    url: str
    description: str = ""
    budget: str = ""
    client_name: str = ""
    external_id: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class SourceMonitorResult:
    """Structured result for one freelance source run."""

    source: str
    status: str
    rows: list[dict[str, Any]]
    fetched_count: int = 0
    rejected_count: int = 0
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    unescaped = html_lib.unescape(str(value))
    no_tags = _HTML_TAG_RE.sub(" ", unescaped)
    return _WS_RE.sub(" ", no_tags).strip()


def _validate_source(source: str) -> str:
    norm = str(source or "").strip().lower()
    if norm not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported freelance monitor source {source!r}; expected one of {SUPPORTED_SOURCES}")
    return norm


def _is_valid_http_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _row_id(source: str, external_id: str, url: str) -> str:
    key = external_id.strip() or url.strip()
    return hashlib.sha256(f"{source}:{key}".encode("utf-8")).hexdigest()[:24]


def _extract_budget(text: str) -> str:
    cleaned = _clean_text(text)
    if not cleaned:
        return ""
    m = _BUDGET_RE.search(cleaned)
    if not m:
        return ""
    val = m.group(0).strip(" :;-–")
    return val if any(ch.isdigit() for ch in val) else ""


def _scrub_legacy_brand(text: str) -> str:
    return re.sub(r"\bCreator\s*OS\b", "SoftUGC", str(text or ""), flags=re.IGNORECASE)


def detect_antibot_block(
    status_code: int | None,
    body: str = "",
    headers: Mapping[str, str] | None = None,
) -> str | None:
    """Return a structured block reason if the source returned an anti-bot or CAPTCHA gate.

    Never attempts to bypass, solve, or retry past any anti-bot challenge.
    """
    if status_code in (403, 429):
        return f"http_{status_code}"

    if headers:
        lowered_headers = {str(k).casefold(): str(v).casefold() for k, v in headers.items()}
        if "cf-mitigated" in lowered_headers or "cf-chl-bypass" in lowered_headers:
            return "header:cloudflare_challenge"
        server_val = lowered_headers.get("server", "")
        if "ddos-guard" in server_val:
            return "header:ddos-guard"

    folded = str(body or "").casefold()
    if not folded:
        return None
    for marker in ANTIBOT_MARKERS:
        if marker in folded:
            return f"marker:{marker}"
    return None


def classify_freelance_order(title: str, description: str = "") -> tuple[bool, str]:
    """Return `(accepted, reason)` for short-form/Reels/UGC/AI-video freelance orders."""
    clean_title = _clean_text(title)
    clean_desc = _clean_text(description)
    combined = f"{clean_title}\n{clean_desc}".strip()
    if not combined:
        return False, "rejected:empty"

    if _RESUME_SELF_PROMO_RE.search(combined):
        return False, "rejected:resume_self_promo"
    if _STORYMAKER_JUNK_RE.search(combined):
        return False, "rejected:storymaker"
    if _GAMING_OR_SPAM_RE.search(combined):
        return False, "rejected:gaming_or_gambling"

    title_has_video = bool(_POSITIVE_VIDEO_RE.search(clean_title))
    if _DEV_OR_NON_VIDEO_RE.search(clean_title):
        return False, "rejected:dev_or_non_video"
    if not title_has_video and _DEV_OR_NON_VIDEO_RE.search(clean_desc):
        return False, "rejected:dev_or_non_video"
    if _BUILD_SOFTWARE_PHRASE_RE.search(combined) and not _AI_AVATAR_HINT_RE.search(clean_title):
        return False, "rejected:dev_or_non_video"

    if not _POSITIVE_VIDEO_RE.search(combined):
        return False, "rejected:no_video_signal"

    return True, "matched:video_ugc_reels"


def _xml_local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def parse_rss_or_atom_xml(xml_text: str, source: str) -> list[FreelanceFeedItem]:
    """Parse RSS 2.0 or Atom XML feed text into normalized `FreelanceFeedItem` records."""
    src = _validate_source(source)
    raw = str(xml_text or "").strip()
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items: list[FreelanceFeedItem] = []
    seen_urls: set[str] = set()
    root_name = _xml_local_name(root.tag)

    candidates: list[ET.Element] = []
    if root_name == "rss" or root.find(".//item") is not None:
        candidates = list(root.findall(".//item"))
    else:
        for elem in root.iter():
            if _xml_local_name(elem.tag) == "entry":
                candidates.append(elem)

    for node in candidates:
        title = ""
        link = ""
        desc = ""
        guid = ""
        pub = ""
        author = ""
        budget = ""
        for child in node:
            cname = _xml_local_name(child.tag)
            text_val = "".join(child.itertext()).strip()
            if cname == "title" and not title:
                title = _clean_text(text_val)
            elif cname == "link":
                href = (child.attrib.get("href") or "").strip()
                rel = (child.attrib.get("rel") or "alternate").strip().lower()
                cand_link = href or text_val
                if cand_link and (not link or rel == "alternate"):
                    link = cand_link
            elif cname in {"description", "summary", "content", "encoded"}:
                cleaned = _clean_text(text_val)
                if len(cleaned) > len(desc):
                    desc = cleaned
            elif cname in {"guid", "id"} and not guid:
                guid = _clean_text(text_val)
            elif cname in {"pubdate", "published", "updated", "date"} and not pub:
                pub = _clean_text(text_val)
            elif cname in {"author", "creator"} and not author:
                author = _clean_text(text_val)
            elif cname in {"budget", "price"} and not budget:
                budget = _clean_text(text_val)

        if not _is_valid_http_url(link):
            continue
        norm_url = link.strip()
        if norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)
        if not budget:
            budget = _extract_budget(f"{title} {desc}")
        items.append(
            FreelanceFeedItem(
                source=src,
                title=title or norm_url,
                url=norm_url,
                description=desc,
                budget=budget,
                client_name=author,
                external_id=guid or norm_url,
                published_at=pub,
            )
        )
    return items


def _normalize_url_against_base(href: str, base_url: str) -> str:
    raw = str(href or "").strip()
    if not raw or raw.startswith(("#", "javascript:", "mailto:")):
        return ""
    resolved = urljoin(base_url, raw) if base_url else raw
    return resolved if _is_valid_http_url(resolved) else ""


def _pick_card_container(anchor: Any, link_rx: re.Pattern[str]) -> Any:
    curr = anchor.parent
    best = anchor.parent
    for _ in range(5):
        if curr is None or getattr(curr, "name", None) in {"body", "html", "main", "ul", "table"}:
            break
        cls_str = " ".join(curr.get("class", [])).lower() if hasattr(curr, "get") else ""
        matched_links = curr.find_all("a", href=link_rx)
        unique_hrefs = {str(a.get("href") or "").strip() for a in matched_links}
        if len(unique_hrefs) > 2:
            break
        best = curr
        if any(tok in cls_str for tok in ("card", "want", "order", "project", "job", "vacancy", "tile", "item")):
            break
        curr = curr.parent
    return best


def parse_public_html_listings(
    html_text: str,
    source: str,
    base_url: str = "",
) -> list[FreelanceFeedItem]:
    """Parse legitimate public HTML project listings for Profi.ru, Kwork, or Upwork."""
    src = _validate_source(source)
    raw = str(html_text or "")
    if not raw.strip():
        return []
    if detect_antibot_block(200, raw):
        return []

    effective_base = base_url or DEFAULT_FEED_URLS[src]
    link_rx = _SOURCE_URL_PATTERNS[src]
    soup = BeautifulSoup(raw, "html.parser")

    items: list[FreelanceFeedItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        full_url = _normalize_url_against_base(str(anchor["href"]), effective_base)
        if not full_url or not link_rx.search(full_url):
            continue
        if full_url in seen_urls:
            continue

        container = _pick_card_container(anchor, link_rx)
        anchor_text = _clean_text(anchor.get_text(" ", strip=True))
        title = ""
        if anchor_text and not _GENERIC_CTA_TEXT_RE.match(anchor_text):
            title = anchor_text
        if not title and container is not None:
            for heading in container.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
                ht = _clean_text(heading.get_text(" ", strip=True))
                if ht and not _GENERIC_CTA_TEXT_RE.match(ht):
                    title = ht
                    break
        if not title:
            continue

        seen_urls.add(full_url)
        desc = ""
        budget = ""
        client_name = ""
        if container is not None:
            for sel in ("[class*='price']", "[class*='budget']", "[data-test*='budget']"):
                el = container.select_one(sel)
                if el:
                    budget = _clean_text(el.get_text(" ", strip=True))
                    if budget:
                        break
            for sel in ("[class*='author']", "[class*='client']", "[class*='buyer']", "[class*='user']"):
                el = container.select_one(sel)
                if el:
                    client_name = _clean_text(el.get_text(" ", strip=True))
                    if client_name:
                        break
            for sel in ("[class*='desc']", "[class*='text']", "[class*='summary']", "p"):
                el = container.select_one(sel)
                if el:
                    cand = _clean_text(el.get_text(" ", strip=True))
                    if cand and cand != title:
                        desc = cand
                        break
            if not desc:
                full_block = _clean_text(container.get_text(" ", strip=True))
                if full_block and full_block != title:
                    desc = full_block.replace(title, "", 1).strip(" :-–|")
            if not budget:
                budget = _extract_budget(f"{title} {desc}")

        items.append(
            FreelanceFeedItem(
                source=src,
                title=title,
                url=full_url,
                description=desc,
                budget=budget,
                client_name=client_name,
                external_id=full_url,
            )
        )
    return items


def parse_json_feed(payload: Any, source: str) -> list[FreelanceFeedItem]:
    """Normalize JSON API / webhook feed payloads into `FreelanceFeedItem` records."""
    src = _validate_source(source)
    raw_items: list[Any] = []
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        for key in ("items", "rows", "jobs", "projects", "orders", "results", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                raw_items = val
                break
            if isinstance(val, dict):
                for inner_key in ("items", "rows", "jobs", "projects", "orders", "results"):
                    inner = val.get(inner_key)
                    if isinstance(inner, list):
                        raw_items = inner
                        break
                if raw_items:
                    break

    out: list[FreelanceFeedItem] = []
    seen: set[str] = set()
    for entry in raw_items:
        item = _coerce_feed_item(entry, default_source=src)
        if item is None or item.url in seen:
            continue
        seen.add(item.url)
        out.append(item)
    return out


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(chunk))
    return _clean_text("".join(parts))


def _decode_part_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    raw = part.get_payload()
    return str(raw or "")


def _detect_source_from_text(text: str) -> str | None:
    low = text.casefold()
    if "profi.ru" in low:
        return "profi_ru"
    if "kwork.ru" in low:
        return "kwork"
    if "upwork.com" in low:
        return "upwork"
    return None


def parse_email_digest(
    raw_email: str | bytes,
    source: str | None = None,
) -> list[FreelanceFeedItem]:
    """Parse a user's legitimate email alert/digest (`.eml`) into feed items."""
    msg = (
        message_from_bytes(raw_email)
        if isinstance(raw_email, (bytes, bytearray))
        else message_from_string(str(raw_email or ""))
    )
    sender = _decode_mime_header(msg.get("From"))
    subject = _decode_mime_header(msg.get("Subject"))
    date_str = _decode_mime_header(msg.get("Date"))

    html_parts: list[str] = []
    text_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/html":
                html_parts.append(_decode_part_payload(part))
            elif ctype == "text/plain":
                text_parts.append(_decode_part_payload(part))
    else:
        ctype = (msg.get_content_type() or "text/plain").lower()
        body = _decode_part_payload(msg)
        if ctype == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    all_raw_blob = "\n".join([sender, subject, *html_parts, *text_parts])
    detected_src = _detect_source_from_text(all_raw_blob)
    resolved_src = _validate_source(source) if source else detected_src
    if not resolved_src:
        return []
    if source and detected_src and detected_src != resolved_src and not _SOURCE_URL_PATTERNS[resolved_src].search(all_raw_blob):
        return []

    items: list[FreelanceFeedItem] = []
    seen_urls: set[str] = set()

    for html_body in html_parts:
        for parsed in parse_public_html_listings(html_body, resolved_src):
            if parsed.url in seen_urls:
                continue
            seen_urls.add(parsed.url)
            items.append(
                FreelanceFeedItem(
                    source=parsed.source,
                    title=parsed.title,
                    url=parsed.url,
                    description=parsed.description,
                    budget=parsed.budget,
                    client_name=parsed.client_name,
                    external_id=parsed.external_id,
                    published_at=parsed.published_at or date_str,
                )
            )

    if items:
        return items

    link_rx = _SOURCE_URL_PATTERNS[resolved_src]
    for txt in text_parts:
        lines = [_clean_text(ln) for ln in txt.splitlines()]
        lines = [ln for ln in lines if ln]
        for idx, line in enumerate(lines):
            for match in _URL_IN_TEXT_RE.finditer(line):
                url = match.group(0).rstrip(".,;)>")
                if not link_rx.search(url) or url in seen_urls:
                    continue
                seen_urls.add(url)
                same_line_title = _clean_text(line.replace(match.group(0), ""))
                prev_line = lines[idx - 1] if idx > 0 else ""
                next_line = lines[idx + 1] if idx + 1 < len(lines) else ""
                if same_line_title and not _GENERIC_CTA_TEXT_RE.match(same_line_title):
                    title = same_line_title
                elif prev_line and not _URL_IN_TEXT_RE.search(prev_line):
                    title = prev_line
                else:
                    title = subject or url
                desc_parts = [p for p in (prev_line, next_line) if p and p != title and not _URL_IN_TEXT_RE.search(p)]
                desc = " — ".join(desc_parts)
                items.append(
                    FreelanceFeedItem(
                        source=resolved_src,
                        title=title,
                        url=url,
                        description=desc,
                        budget=_extract_budget(f"{title} {desc}"),
                        external_id=url,
                        published_at=date_str,
                    )
                )
    return items


def _coerce_feed_item(
    raw: Mapping[str, Any] | FreelanceFeedItem,
    *,
    default_source: str,
) -> FreelanceFeedItem | None:
    if isinstance(raw, FreelanceFeedItem):
        src = _validate_source(raw.source or default_source)
        if not _is_valid_http_url(raw.url):
            return None
        return FreelanceFeedItem(
            source=src,
            title=_clean_text(raw.title) or raw.url,
            url=raw.url.strip(),
            description=_clean_text(raw.description),
            budget=_clean_text(raw.budget) or _extract_budget(f"{raw.title} {raw.description}"),
            client_name=_clean_text(raw.client_name),
            external_id=str(raw.external_id or raw.url).strip(),
            published_at=str(raw.published_at or "").strip(),
        )

    if not isinstance(raw, Mapping):
        return None
    src = _validate_source(str(raw.get("source") or default_source))
    url = str(
        raw.get("manual_action_url")
        or raw.get("url")
        or raw.get("link")
        or raw.get("alternate_url")
        or raw.get("job_url")
        or ""
    ).strip()
    if not _is_valid_http_url(url):
        return None
    title = _clean_text(raw.get("title") or raw.get("name") or raw.get("subject") or raw.get("role"))
    desc = _clean_text(
        raw.get("description")
        or raw.get("summary")
        or raw.get("text")
        or raw.get("body")
        or raw.get("what_they_do")
    )
    if not (title or desc):
        return None
    budget = _clean_text(raw.get("budget") or raw.get("price") or raw.get("pay") or raw.get("compensation"))
    if not budget:
        budget = _extract_budget(f"{title} {desc}")
    client_name = _clean_text(
        raw.get("client_name") or raw.get("author") or raw.get("client") or raw.get("company") or raw.get("employer")
    )
    ext_id = str(raw.get("external_id") or raw.get("id") or raw.get("guid") or url).strip()
    pub = str(raw.get("published_at") or raw.get("pub_date") or raw.get("published") or raw.get("created_at") or "").strip()
    return FreelanceFeedItem(
        source=src,
        title=title or desc[:90],
        url=url,
        description=desc,
        budget=budget,
        client_name=client_name,
        external_id=ext_id,
        published_at=pub,
    )


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def build_assisted_draft(
    item: FreelanceFeedItem | Mapping[str, Any],
    *,
    language: str | None = None,
    showcase_url: str = DEFAULT_SHOWCASE_URL,
    custom_template: str | None = None,
) -> str:
    """Build a personalized manual outreach draft in RU (Profi/Kwork) or EN (Upwork)."""
    if isinstance(item, FreelanceFeedItem):
        src = _validate_source(item.source)
        title = item.title
        desc = item.description
        budget = item.budget
        client_name = item.client_name
    else:
        src = _validate_source(str(item.get("source") or "kwork"))
        title = _clean_text(item.get("title") or item.get("name") or "проекту")
        desc = _clean_text(item.get("description") or item.get("summary") or "")
        budget = _clean_text(item.get("budget") or item.get("price") or "")
        client_name = _clean_text(item.get("client_name") or item.get("company") or "")

    lang = (language or _SOURCE_LANGUAGE[src]).strip().lower()
    if lang not in {"ru", "en"}:
        lang = _SOURCE_LANGUAGE[src]
    clean_showcase = str(showcase_url or DEFAULT_SHOWCASE_URL).strip() or DEFAULT_SHOWCASE_URL
    blob = f"{title} {desc}"

    if custom_template and str(custom_template).strip():
        rendered = str(custom_template).strip().format_map(
            _SafeFormatDict(
                title=title,
                description=desc,
                budget=budget,
                client_name=client_name,
                showcase_url=clean_showcase,
                source=src,
            )
        )
        return _scrub_legacy_brand(rendered)

    if lang == "ru":
        greeting = f"Добрый день, {client_name}!" if client_name and _CYRILLIC_RE.search(client_name) else "Добрый день!"
        if _AI_AVATAR_HINT_RE.search(blob):
            angle = (
                "Специализируюсь на реалистичных AI-персонажах и AI UGC под ключ без съёмок: "
                "держу консистентность лица и стиля из серии в серию (Nano Banana Pro, Kling / Seedance / HeyGen, ElevenLabs)."
            )
        elif _UGC_HINT_RE.search(blob):
            angle = (
                "Предлагаю закрыть задачу через AI UGC + нашу команду креаторов: "
                "собираем продающие Reels / Shorts / TikTok под тесты гипотез и воронку лидгена."
            )
        else:
            angle = (
                "Делаю серийный продакшн и монтаж вертикальных роликов (Reels / Shorts / TikTok) "
                "с сильными хуками и стабильным темпом выпуска под ключ."
            )
        budget_note = f" Ориентир по бюджету ({budget}) учтён." if budget else ""
        draft = (
            f"{greeting} По заказу «{title}».\n\n"
            f"{angle}{budget_note}\n\n"
            "Релевантные цифры:\n"
            "— Faceless-канал на AI-роликах: 183,5 млн просмотров за 90 дней, 241K подписчиков, 94,8% холодный охват\n"
            "— Потоковый продакшн: до 90 роликов в месяц под ключ\n\n"
            f"Примеры и кейсы (SoftUGC): {clean_showcase}\n\n"
            "Если пришлёте 1–2 референса или короткое ТЗ, соберу тестовый фрагмент 10–15 секунд под ваш формат. Сколько роликов планируете в месяц?"
        )
        return _scrub_legacy_brand(draft)

    greeting_en = f"Hi {client_name}," if client_name else "Hi,"
    if _AI_AVATAR_HINT_RE.search(blob):
        angle_en = (
            "We specialize in lifelike AI UGC avatars and short-form video pipelines with consistent "
            "characters across episodes (Kling / Seedance / HeyGen + ElevenLabs)."
        )
    elif _UGC_HINT_RE.search(blob):
        angle_en = (
            "At SoftUGC we build high-volume UGC & AI short-form creative pipelines for Reels, TikTok, "
            "and Shorts so you can test fresh hooks weekly without creator bottlenecks."
        )
    else:
        angle_en = (
            "At SoftUGC we produce retention-focused Reels, TikTok, and YouTube Shorts at scale "
            "with fast iteration on hooks and pacing."
        )
    budget_en = f" Budget note ({budget}) works on our end." if budget else ""
    draft_en = (
        f"{greeting_en} saw your \"{title}\" project.\n\n"
        f"{angle_en}{budget_en}\n\n"
        "Recent benchmarks:\n"
        "- Faceless short-form channel: 183.5M views in 90 days (241K followers, 94.8% cold reach)\n"
        "- Production throughput: up to 90+ videos/month turnkey\n\n"
        f"Portfolio & case studies: {clean_showcase}\n\n"
        "Happy to put together a quick 10-15 sec custom sample from your brief — what monthly volume and niche are you targeting?"
    )
    return _scrub_legacy_brand(draft_en)


def _item_to_assisted_row(
    item: FreelanceFeedItem,
    *,
    showcase_url: str,
    custom_template: str | None,
) -> dict[str, Any]:
    lang = _SOURCE_LANGUAGE[item.source]
    draft_text = build_assisted_draft(
        item,
        language=lang,
        showcase_url=showcase_url,
        custom_template=custom_template,
    )
    what_parts = [item.title]
    if item.description:
        what_parts.append(item.description[:280])
    if item.budget:
        what_parts.append(f"Budget: {item.budget}")
    display_name = item.client_name or item.title[:80]
    row: dict[str, Any] = {
        "id": _row_id(item.source, item.external_id, item.url),
        "name": display_name,
        "company": item.client_name or item.source,
        "title": item.title,
        "what_they_do": " — ".join(what_parts),
        "budget": item.budget,
        "source": item.source,
        "lead_source": item.source,
        "segment": "vacancy_assisted",
        "language": lang,
        "manual_action_url": item.url,
        "manual_draft_text": draft_text,
        "job_url": item.url,
        "url": item.url,
        "links": [item.url],
        "published_at": item.published_at,
    }
    return row


def _default_http_get(url: str, *, headers: Mapping[str, str] | None = None, timeout: int = 20) -> requests.Response:
    return requests.get(url, headers=dict(headers or {}), timeout=timeout)


def _invoke_http_get(
    http_get_fn: Callable[..., Any],
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: int,
) -> Any:
    try:
        sig = inspect.signature(http_get_fn)
        params = sig.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "headers" in params:
            kwargs["headers"] = dict(headers)
        if accepts_kwargs or "timeout" in params:
            kwargs["timeout"] = timeout
        return http_get_fn(url, **kwargs)
    except (TypeError, ValueError):
        return http_get_fn(url)


def _unpack_http_response(resp: Any) -> tuple[int, str, dict[str, str]]:
    if isinstance(resp, str):
        return 200, resp, {}
    if isinstance(resp, tuple):
        if len(resp) == 2:
            return int(resp[0]), str(resp[1] or ""), {}
        if len(resp) >= 3:
            hdrs = resp[1] if isinstance(resp[1], Mapping) else {}
            return int(resp[0]), str(resp[2] or ""), {str(k): str(v) for k, v in hdrs.items()}
    if isinstance(resp, Mapping):
        status = int(resp.get("status_code") or resp.get("status") or 200)
        body = resp.get("text") if resp.get("text") is not None else resp.get("body")
        if body is None and resp.get("json") is not None:
            body = json.dumps(resp["json"], ensure_ascii=False)
        hdrs = resp.get("headers") if isinstance(resp.get("headers"), Mapping) else {}
        return status, str(body or ""), {str(k): str(v) for k, v in hdrs.items()}
    status = int(getattr(resp, "status_code", getattr(resp, "status", 200)) or 200)
    body = getattr(resp, "text", None)
    if body is None:
        raw_content = getattr(resp, "content", b"")
        body = raw_content.decode("utf-8", errors="replace") if isinstance(raw_content, (bytes, bytearray)) else str(raw_content or "")
    raw_hdrs = getattr(resp, "headers", None)
    hdrs = {str(k): str(v) for k, v in raw_hdrs.items()} if isinstance(raw_hdrs, Mapping) else {}
    return status, str(body or ""), hdrs


def _resolve_feed_url(source: str, feed_url: str | None, cfg: Mapping[str, Any] | None) -> str:
    if feed_url and str(feed_url).strip():
        return str(feed_url).strip()
    if cfg:
        vac = cfg.get("vacancies") if isinstance(cfg.get("vacancies"), Mapping) else {}
        feeds = vac.get("freelance_feeds") if isinstance(vac.get("freelance_feeds"), Mapping) else {}
        if feeds.get(source):
            return str(feeds[source]).strip()
    env_key = _SOURCE_ENV_KEYS[source]
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return env_val
    return DEFAULT_FEED_URLS[source]


def _resolve_showcase_and_template(
    source: str,
    cfg: Mapping[str, Any] | None,
    showcase_url: str | None,
    custom_template: str | None,
) -> tuple[str, str | None]:
    vac = cfg.get("vacancies") if (cfg and isinstance(cfg.get("vacancies"), Mapping)) else {}
    resolved_showcase = str(showcase_url or vac.get("showcase_url") or DEFAULT_SHOWCASE_URL).strip() or DEFAULT_SHOWCASE_URL
    resolved_tmpl = custom_template
    if not resolved_tmpl and vac:
        overrides = vac.get("freelance_templates") if isinstance(vac.get("freelance_templates"), Mapping) else {}
        if overrides.get(source):
            resolved_tmpl = str(overrides[source])
        elif vac.get("editable_prompt_override") and _SOURCE_LANGUAGE[source] == "ru":
            resolved_tmpl = str(vac["editable_prompt_override"])
    return resolved_showcase, resolved_tmpl


def _parse_body_for_source(body: str, source: str, base_url: str) -> list[FreelanceFeedItem]:
    stripped = str(body or "").lstrip()
    if not stripped:
        return []
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
            return parse_json_feed(payload, source)
        except json.JSONDecodeError:
            pass
    if stripped.startswith("<") and ("<rss" in stripped[:400].lower() or "<feed" in stripped[:400].lower() or "<?xml" in stripped[:200].lower()):
        xml_items = parse_rss_or_atom_xml(stripped, source)
        if xml_items:
            return xml_items
    return parse_public_html_listings(stripped, source, base_url=base_url)


def monitor_source(
    source: str,
    *,
    feed_url: str | None = None,
    feed_items: Iterable[Mapping[str, Any] | FreelanceFeedItem] | None = None,
    email_digests: Iterable[str | bytes] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
    showcase_url: str | None = None,
    custom_template: str | None = None,
    max_items: int = 50,
    timeout: int = 20,
) -> dict[str, Any]:
    """Monitor one legitimate freelance platform source and return structured status + rows."""
    src = _validate_source(source)
    resolved_showcase, resolved_tmpl = _resolve_showcase_and_template(src, cfg, showcase_url, custom_template)

    candidates: list[FreelanceFeedItem] = []
    if feed_items is not None:
        for raw in feed_items:
            coerced = _coerce_feed_item(raw, default_source=src)
            if coerced is not None:
                candidates.append(coerced)
    elif email_digests is not None:
        for raw_eml in email_digests:
            candidates.extend(parse_email_digest(raw_eml, source=src))
    else:
        target_url = _resolve_feed_url(src, feed_url, cfg)
        if not target_url:
            return SourceMonitorResult(source=src, status="not_configured", rows=[]).to_dict()
        getter = http_get_fn or _default_http_get
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/html;q=0.9, */*;q=0.8",
        }
        try:
            resp = _invoke_http_get(getter, target_url, headers=headers, timeout=timeout)
        except Exception as exc:
            err_resp = getattr(exc, "response", None)
            if err_resp is not None:
                st, body, hdrs = _unpack_http_response(err_resp)
                block_reason = detect_antibot_block(st, body, hdrs)
                if block_reason:
                    return SourceMonitorResult(
                        source=src,
                        status="blocked_by_antibot",
                        rows=[],
                        reason=block_reason,
                    ).to_dict()
                if st == 401:
                    return SourceMonitorResult(
                        source=src,
                        status="auth_required",
                        rows=[],
                        reason="http_401",
                    ).to_dict()
            return SourceMonitorResult(
                source=src,
                status="error",
                rows=[],
                error=f"{type(exc).__name__}: {exc}",
            ).to_dict()

        status_code, body_text, resp_headers = _unpack_http_response(resp)
        block_reason = detect_antibot_block(status_code, body_text, resp_headers)
        if block_reason:
            return SourceMonitorResult(
                source=src,
                status="blocked_by_antibot",
                rows=[],
                reason=block_reason,
            ).to_dict()
        if status_code == 401:
            return SourceMonitorResult(
                source=src,
                status="auth_required",
                rows=[],
                reason="http_401",
            ).to_dict()
        if status_code >= 400:
            return SourceMonitorResult(
                source=src,
                status="error",
                rows=[],
                reason=f"http_{status_code}",
                error=f"HTTP {status_code}",
            ).to_dict()
        candidates = _parse_body_for_source(body_text, src, base_url=target_url)

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    rejected_count = 0
    for item in candidates:
        accepted, _why = classify_freelance_order(item.title, item.description)
        if not accepted:
            rejected_count += 1
            continue
        row = _item_to_assisted_row(
            item,
            showcase_url=resolved_showcase,
            custom_template=resolved_tmpl,
        )
        norm_url = row["manual_action_url"].rstrip("/")
        if row["id"] in seen_ids or norm_url in seen_urls:
            continue
        seen_ids.add(row["id"])
        seen_urls.add(norm_url)
        rows.append(row)
        if max_items > 0 and len(rows) >= max_items:
            break

    return SourceMonitorResult(
        source=src,
        status="ok",
        rows=rows,
        fetched_count=len(candidates),
        rejected_count=rejected_count,
    ).to_dict()


def monitor_all_sources(
    *,
    sources: Iterable[str] = SUPPORTED_SOURCES,
    feed_urls: Mapping[str, str] | None = None,
    feed_items_by_source: Mapping[str, Iterable[Mapping[str, Any] | FreelanceFeedItem]] | None = None,
    email_digests: Iterable[str | bytes] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
    showcase_url: str | None = None,
    custom_template_by_source: Mapping[str, str] | None = None,
    max_items_per_source: int = 50,
    timeout: int = 20,
) -> dict[str, Any]:
    """Run all requested freelance monitors independently without letting one block crash others."""
    vac_sources_cfg = (
        cfg.get("vacancies", {}).get("sources", {})
        if cfg and isinstance(cfg.get("vacancies"), Mapping) and isinstance(cfg.get("vacancies", {}).get("sources"), Mapping)
        else {}
    )
    digest_list = list(email_digests) if email_digests is not None else None
    all_rows: list[dict[str, Any]] = []
    per_source: dict[str, dict[str, Any]] = {}

    for raw_src in sources:
        src = _validate_source(raw_src)
        if src in vac_sources_cfg and not bool(vac_sources_cfg[src]):
            per_source[src] = SourceMonitorResult(
                source=src,
                status="disabled",
                rows=[],
                reason="disabled_in_config",
            ).to_dict()
            continue

        src_items = feed_items_by_source.get(src) if feed_items_by_source is not None else None
        src_url = feed_urls.get(src) if feed_urls is not None else None
        src_tmpl = custom_template_by_source.get(src) if custom_template_by_source is not None else None
        res = monitor_source(
            src,
            feed_url=src_url,
            feed_items=src_items,
            email_digests=digest_list if src_items is None and src_url is None and digest_list is not None else None,
            http_get_fn=http_get_fn,
            cfg=cfg,
            showcase_url=showcase_url,
            custom_template=src_tmpl,
            max_items=max_items_per_source,
            timeout=timeout,
        )
        per_source[src] = res
        all_rows.extend(res.get("rows") or [])

    active_statuses = [r["status"] for r in per_source.values() if r["status"] != "disabled"]
    if not active_statuses or all(st == "ok" for st in active_statuses):
        overall = "ok"
    elif all(st == "blocked_by_antibot" for st in active_statuses):
        overall = "blocked_by_antibot"
    elif any(st == "ok" for st in active_statuses):
        overall = "partial"
    elif any(st == "blocked_by_antibot" for st in active_statuses):
        overall = "blocked_by_antibot"
    else:
        overall = "error"

    return {
        "status": overall,
        "rows": all_rows,
        "sources": per_source,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor Profi.ru / Kwork / Upwork public feeds for Reels/UGC orders.")
    parser.add_argument("--source", default="all", choices=("all", *SUPPORTED_SOURCES))
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--feed-url", default="", help="Optional feed/RSS URL when running a single source")
    parser.add_argument("--eml", action="append", default=[], help="Optional .eml email digest path(s)")
    args = parser.parse_args(argv)

    digests = [Path(p).read_bytes() for p in args.eml if Path(p).is_file()] or None
    if args.source == "all":
        summary = monitor_all_sources(email_digests=digests)
        rows = summary["rows"]
    else:
        summary = monitor_source(
            args.source,
            feed_url=args.feed_url or None,
            email_digests=digests,
        )
        rows = summary["rows"]

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps({k: v for k, v in summary.items() if k != "rows"} | {"row_count": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
