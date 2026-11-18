"""CRM database hygiene, Sniper ICP gate, self-healing, and server leadgen readiness SSOT."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any
from urllib.parse import urlparse

from app.db import as_dict, utc_now

try:
    from leadgen.supply_side import (
        PLATFORM_AGGREGATOR_HOSTS as _SS_PLATFORM_HOSTS,
        PLATFORM_GARBAGE_COMPANY_NAMES as _SS_GARBAGE_COMPANIES,
        is_platform_or_social_url as _ss_is_platform_url,
        is_rejected_buyer_category as _ss_is_rejected_cat,
        is_supply_side as _ss_is_supply_side,
        supply_reason as _ss_supply_reason,
    )
except ImportError:  # pragma: no cover
    _SS_PLATFORM_HOSTS = (
        "x.com",
        "twitter.com",
        "t.co",
        "linkedin.com",
        "instagram.com",
        "t.me",
        "telegram.me",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "facebook.com",
        "reddit.com",
        "github.com",
        "gitlab.com",
        "news.ycombinator.com",
        "ycombinator.com",
        "linktr.ee",
        "linktree.com",
        "notion.site",
        "notion.so",
        "calendly.com",
        "medium.com",
        "substack.com",
        "producthunt.com",
        "whop.com",
        "apps.whop.com",
        "gumroad.com",
        "skool.com",
        "patreon.com",
        "stan.store",
        "taplink.cc",
        "clck.ru",
        "cutt.ly",
        "ow.ly",
        "buff.ly",
        "rb.gy",
        "is.gd",
        "shorturl.at",
        "lnkd.in",
        "bento.me",
        "bio.site",
        "campsite.bio",
    )
    _SS_GARBAGE_COMPANIES = frozenset(
        {
            "github", "gitlab", "telegram", "twitter", "linkedin", "instagram", "youtube",
            "notion", "medium", "substack", "producthunt", "whop", "gumroad", "skool",
            "clck", "taplink",
        }
    )

    def _ss_is_platform_url(url: str | None) -> bool:
        raw = str(url or "").strip()
        if not raw:
            return False
        host = (urlparse(raw if "://" in raw else f"https://{raw}").hostname or "").lower().removeprefix("www.")
        return any(host == h or host.endswith("." + h) for h in _SS_PLATFORM_HOSTS)

    def _ss_is_rejected_cat(category: str | None) -> bool:
        return False

    def _ss_is_supply_side(text: str) -> bool:
        return False

    def _ss_supply_reason(text: str) -> str:
        return ""


try:
    from leadgen.supply_side import sniper_reject_reason as _external_sniper_reject_reason
except ImportError:
    _external_sniper_reject_reason = None


_PROTECTED_LEAD_STATUSES = frozenset({"won", "on_hold", "call_scheduled"})
_PROTECTED_AIRTABLE_STATUSES = frozenset({"signed", "on hold", "call scheduled"})
_ACTIVE_COLD_LEAD_STATUSES = frozenset({"new", "talking", "waiting"})

_REJECTED_SNIPER_NICHES = frozenset(
    {
        "агентство",
        "сервис / агентство",
        "сервис/агентство",
        "инфра",
        "инфраструктура",
        "инфраструктура / dev",
        "инфраструктура/dev",
        "низкий приоритет",
        "партнёр, не ца",
        "партнер, не ца",
        "agency",
        "service / agency",
        "infra",
        "infrastructure / dev",
    }
)
_REJECTED_SNIPER_NICHE_SUBSTRINGS = (
    "сервис / агентство",
    "инфраструктура / dev",
    "партнёр, не ца",
    "партнер, не ца",
    "низкий приоритет",
)

_FALLBACK_SUPPLY_OR_AGENCY_RE = re.compile(
    r"(?:"
    r"\bfinpr(?:\s+agency)?\b"
    r"|\bmarketacross\b"
    r"|\byou\s+agency\b"
    r"|\b(?:taboola|meta\s+ads|facebook\s+ads|google\s+ads|tiktok\s+ads|ppc|seo)\s+expert\b"
    r"|\bshopify\s+(?:expert|agency|partner)s?\b"
    r"|\bshopify\s+merchant\s+help\b|\bmerchant\s+help\s+and\s+developer\b|\bmerchant\s+help\b"
    r"|\bkirito\s+bot\s+hosting\b|\bbot\s+hosting\b"
    r"|\bmanaged\s+hosting\b"
    r"|отдел\w*\s+продаж\s*(?:b2b\s*)?под\s+ключ"
    r"|\baimrktops\b"
    r"|\bitmentor\b|\bit[\s\-]*ментор\w*\b"
    r"|\bfull[\s\-]*stack\s+developers?\s+team\b|\boutsource\s+developers?\s+team\b"
    r"|helping\s+(?:projects|brands|online\s+stores|stores|merchants|founders|startups|clients|businesses|companies)\s+"
    r"(?:trend|get\s+consistent(?:\s+sales)?|gain\s+real\s+traction|grow|scale|boost|get\s+more\s+sales)"
    r"|\bpr\s*(?:&|and|\+)\s*growth\s+for\s+(?:crypto|web3|ai|startups|tech|blockchain)\b"
    r"|\bsparks[_\-\s]*merch\b|sparks-merch\.com|крипто[\-\s]*мерч\s+и\s+сувенир"
    r"|\b(?:marketing|growth|pr|smm|seo|web3|kol|shilling|clipping|ugc|video|dev|development|automation)\s+(?:agenc\w*|consultanc\w*)\b"
    r"|\bgtm\s+consultanc\w*\b"
    r"|\bfreelancer\b|\bfreelance\s+(?:video|editor|marketer|dev|developer|designer|writer|creator)\b"
    r"|\[for\s*hire\]|\bfor\s+hire\b|\bopen\s+to\s+work\b"
    r"|партн[её]р,\s*не\s*ца|сервис\s*/\s*агентство"
    r")",
    re.IGNORECASE,
)

_SNIPER_OSS_OR_DEVTOOL_RE = re.compile(
    r"(?:"
    r"\bfree,\s*open[\s\-]*source\b"
    r"|\bopen[\s\-]*source\b"
    r"|\baccount\s+recovery\s+apis?\b|\bethical\s+hacking\b"
    r"|движок\s+графов\s+знаний\s+кодовой\s+базы(?:\s+для\s+разработчиков)?"
    r"|граф\w*\s+знаний\s+кодовой\s+базы\s+для\s+разработчиков"
    r"|синхронизирует\s+ai[\s\-]*кодинг[\s\-]*агентов"
    r"|\bclaude\s+code,\s*codex,\s*cursor\b"
    r"|\bbacklink\s+indexing\s+service\b|\bbacklink\s+indexer\b"
    r"|\b(?:matrixreach|matrixreach_bot|firsay|1win(?:\s+partners)?|1xbet|mostbet)\b"
    r")",
    re.IGNORECASE,
)

_SNIPER_MICRO_VACANCY_RE = re.compile(
    r"(?:"
    r"стори[сз][\s_\-]*мейкер|story[\s_\-]*mak"
    r"|\bmari_vakansii\b"
    r"|подработк\w*\s+(?:за|на)\s*10[\s\-–]*15\s*(?:тыс|к\b|000)"
    r"|(?:оформлени|ведени|монтаж)\w*\s+стори[сз]"
    r"|иллюстра(?:тор|ц)\w*[\s\S]{0,80}детск\w*\s+книг"
    r"|(?:монтаж|нарезк|рилсмейкер)\w*[\s\S]{0,35}\bмем(?:ов|ы|ами|ам)?\b"
    r")",
    re.IGNORECASE,
)

_VACANCY_SOURCE_OR_CAT_RE = re.compile(
    r"(?:ваканс|vacanc|job_monitor|tg[\s_\-]*jobs?\b)",
    re.IGNORECASE,
)
_WARM_MANUAL_SOURCE_RE = re.compile(
    r"(?:\breferral\b|реферал|\binbound\b|входящ|рекомендац|\bmanual\b|ручн|\bother\b|\bairtable\b|\bpartner\b|партн[её]р)",
    re.IGNORECASE,
)
_COLD_OUTREACH_OR_LEADGEN_SOURCE_RE = re.compile(
    r"(?:cold\s+outreach|leadgen|google\s+sheet|product\s+hunt|\byc\b|hacker\s+news|\bhn\b|"
    r"meta|ad\s+spy|\btg\b|channel|social|reddit|whop|tiktok|app_store|app_stores|ats_hiring|ph_detailed|"
    r"d2c_whatsapp|fb_groups|facebook_groups|catalog|linkedin|rea[\s_\-]*video|direct|scrape|github)",
    re.IGNORECASE,
)

_HN_PREFIX_RE = re.compile(r"^(?:show|ask|launch|tell)\s+hn\s*:\s*", re.IGNORECASE)
_NUM_PREFIX_RE = re.compile(r"^\d+[\.\)]\s+")
_RAW_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_BRAND_OR_ORG_TOKEN_RE = re.compile(
    r"\b(?:"
    r"vc|ai|io|xyz|app|apps|bot|labs|lab|studio|studios|agency|hosting|host|merch|group|"
    r"team|tech|cloud|pay|finance|fin|crypto|defi|dao|protocol|network|games|gaming|media|"
    r"digital|ventures|capital|global|pro|soft|crm|saas|ecom|b2b|store|shop|club|hub|"
    r"cargo|logistics|clinic|design|auto|retail|box|bridge|scale|inc|llc|ltd|corp|co|"
    r"metrics|flow|orbit|pulse|deck|sync|stack|base|data|chain|coin|token|wallet|dex|cex|"
    r"школа|бренд|студия|агентство|бюро|сервис|платформа|компания|группа|маркет|отдел|"
    r"клиника|завод|производство|импорт|онлайн|мебельный|саморазвития|инвестиции"
    r")\b",
    re.IGNORECASE,
)

_HUMAN_FIRST_NAMES = frozenset(
    {
        # Latin
        "adam", "aaron", "alex", "alexander", "alexei", "alexey", "alice", "alina", "amanda",
        "amy", "andrea", "andrew", "andrey", "anna", "anthony", "anton", "artem", "arthur",
        "ben", "benjamin", "bob", "boris", "brandon", "brian", "carlos", "charles", "charlie",
        "chloe", "chris", "christian", "claire", "dan", "daniel", "daria", "david", "denis",
        "diego", "dima", "dmitry", "edward", "ekaterina", "elena", "emily", "emma", "eric",
        "erik", "eugene", "evgeny", "filip", "frank", "gabriel", "george", "gleb", "grace",
        "greg", "hannah", "harry", "henry", "hugo", "ian", "igor", "ilya", "irina", "isaac",
        "ivan", "jack", "jacob", "jake", "jakub", "james", "jan", "jane", "jason", "javier",
        "jeff", "jeremy", "jessica", "john", "jonathan", "jordan", "joseph", "josh", "joshua",
        "julia", "justin", "kate", "katherine", "katya", "kevin", "kirill", "konstantin",
        "ksenia", "kyle", "laura", "lauren", "leo", "leon", "leonid", "liam", "lisa", "lorenzo",
        "louis", "luca", "lucas", "lukas", "luke", "marc", "marco", "marek", "maria", "marina",
        "mark", "martin", "mathew", "matt", "matteo", "matthew", "max", "maxim", "maksim",
        "megan", "melissa", "michael", "michal", "michelle", "mike", "mikhail", "misha",
        "natalia", "natalie", "nathan", "nicholas", "nick", "nicole", "nikita", "nikolai",
        "noah", "oleg", "olga", "oliver", "olivia", "olya", "oscar", "patrick", "pasha",
        "paul", "pavel", "peter", "philip", "pierce", "piotr", "polina", "rachel", "raphael",
        "rebecca", "richard", "robert", "romain", "roman", "ruslan", "ryan", "sam", "samantha",
        "samuel", "sarah", "sasha", "scott", "sean", "sergei", "sergey", "simon", "sofia",
        "sophia", "sophie", "stanislav", "stefan", "stephanie", "stephen", "steve", "steven",
        "svetlana", "tatiana", "taylor", "thomas", "tim", "timmy", "timofei", "timofey",
        "timothy", "tomas", "tom", "tyler", "vadim", "valeria", "veronica", "veronika",
        "victor", "victoria", "viktor", "vlad", "vladimir", "vladislav", "will", "william",
        "yulia", "yuri", "zach", "zachary", "zoe",
        # Cyrillic
        "александр", "александра", "алексей", "алина", "алёна", "алена", "анастасия",
        "анатoлий", "анатолий", "андрей", "анна", "антон", "артём", "артем", "артур",
        "борис", "вадим", "валентин", "валентина", "валерий", "валерия", "василий",
        "вероника", "виктор", "виктория", "виталий", "владимир", "владислав", "вячеслав",
        "геннадий", "георгий", "глеб", "григорий", "даниил", "данила", "дарья", "денис",
        "дмитрий", "евгений", "евгения", "егор", "екатерина", "елена", "иван", "игорь",
        "илья", "инна", "ирина", "кирилл", "константин", "ксения", "леонид", "максим",
        "маргарита", "марина", "мария", "матвей", "михаил", "надежда", "наталия", "наталья",
        "никита", "николай", "оксана", "олег", "ольга", "павел", "пётр", "петр", "полина",
        "роман", "руслан", "светлана", "сергей", "софия", "софья", "станислав", "степан",
        "татьяна", "тимофей", "тимур", "фёдор", "федор", "юлия", "юрий", "ярослав",
    }
)

_SURNAME_SUFFIXES_LATIN = (
    "ova", "ina", "eva", "skaya", "sky", "ski", "vich", "ko", "chuk", "yuk",
    "son", "sen", "mann", "berg", "stein", "smith", "jones", "brown", "miller",
    "wilson", "moore", "taylor", "anderson", "thomas", "jackson", "white", "harris",
    "martin", "thompson", "garcia", "martinez", "robinson", "clark", "rodriguez",
    "lewis", "lee", "walker", "hall", "allen", "young", "king", "wright", "scott",
    "green", "baker", "adams", "nelson", "carter", "mitchell", "perez", "roberts",
)
_SURNAME_SUFFIXES_CYRILLIC = (
    "ов", "ова", "ев", "ева", "ёв", "ёва", "ин", "ина", "ын", "ына",
    "ский", "ская", "цкий", "цкая", "ко", "енко", "чук", "юк", "ич", "вич",
    "дзе", "швили", "ян", "янц", "езе", "гезе",
)

_GENERIC_URL_BASES = frozenset(
    {
        "t", "x", "twitter", "linkedin", "itunes", "apps", "play", "github", "gitlab",
        "npmjs", "pypi", "medium", "substack", "youtube", "youtu", "instagram", "facebook",
        "fb", "tiktok", "discord", "notion", "linktr", "linktree", "beacons", "carrd",
        "docs", "drive", "chromewebstore", "lu", "luma", "news", "huggingface", "calendly",
        "ycombinator", "wa", "whatsapp", "vk", "vc", "habr", "reddit", "bit", "tinyurl",
    }
)


def _fix_mojibake(value: str | None) -> str:
    txt = str(value or "")
    if "Ð" in txt or "Ñ" in txt:
        try:
            return txt.encode("latin1").decode("utf-8")
        except Exception:
            return txt
    return txt


def _is_synthetic_tg_id(peer_id: Any) -> bool:
    try:
        val = int(peer_id)
    except (TypeError, ValueError):
        return False
    return 900000 <= val < 1000000


def _has_real_tg_id(peer_id: Any) -> bool:
    if peer_id is None or str(peer_id).strip() == "":
        return False
    try:
        val = int(str(peer_id).strip())
    except (TypeError, ValueError):
        return False
    return val > 0 and not _is_synthetic_tg_id(val)


def _clean_username_token(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        return ""
    val = re.sub(r"^https?://(?:www\.)?(?:t\.me|telegram\.me)/", "", val, flags=re.IGNORECASE)
    val = re.sub(r"^(?:t\.me|telegram\.me)/", "", val, flags=re.IGNORECASE)
    return val.strip().lstrip("@").strip("/")


def _has_messenger_contact(
    *,
    username: Any = None,
    phone: Any = None,
    telegram_user_id: Any = None,
) -> bool:
    if _clean_username_token(username):
        return True
    if str(phone or "").strip():
        return True
    if _has_real_tg_id(telegram_user_id):
        return True
    return False


def _call_external_sniper_reject(
    *,
    blob: str,
    name: str,
    company: str,
    niche: str,
    note: str,
    description: str,
    url: str,
    lead_source: str,
    vacancies_enabled: bool,
) -> str:
    if _external_sniper_reject_reason is None:
        return ""
    row_payload = {
        "name": name,
        "company": company,
        "project": company,
        "category": niche,
        "niche": niche,
        "note": note,
        "description": description,
        "their_angle": description or note,
        "project_url": url,
        "url": url,
        "source": lead_source,
        "text": blob,
    }
    res = _external_sniper_reject_reason(row_payload, category=niche or None)
    return str(res or "").strip()


def sniper_lead_violation_reason(
    *,
    name: str = "",
    company: str = "",
    niche: str = "",
    note: str = "",
    description: str = "",
    url: str = "",
    lead_source: str = "",
    vacancies_enabled: bool = False,
) -> str:
    name_s = _fix_mojibake(name).strip()
    company_s = _fix_mojibake(company).strip()
    niche_s = _fix_mojibake(niche).strip()
    note_s = _fix_mojibake(note).strip()
    desc_s = _fix_mojibake(description).strip()
    url_s = str(url or "").strip()
    src_s = _fix_mojibake(lead_source).strip()

    if niche_s:
        niche_norm = " ".join(niche_s.lower().split())
        if (
            _ss_is_rejected_cat(niche_s)
            or niche_norm in _REJECTED_SNIPER_NICHES
            or any(sub in niche_norm for sub in _REJECTED_SNIPER_NICHE_SUBSTRINGS)
        ):
            return f"нецелевая категория ({niche_s})"

    blob = "\n".join(p for p in (name_s, company_s, niche_s, note_s, desc_s, url_s, src_s) if p)
    if not blob:
        return ""

    ext_reason = _call_external_sniper_reject(
        blob=blob,
        name=name_s,
        company=company_s,
        niche=niche_s,
        note=note_s,
        description=desc_s,
        url=url_s,
        lead_source=src_s,
        vacancies_enabled=vacancies_enabled,
    )
    if ext_reason:
        return ext_reason

    m_agency = _FALLBACK_SUPPLY_OR_AGENCY_RE.search(blob)
    if m_agency:
        return f"агентство / сервисный подрядчик ({m_agency.group(0).strip()})"

    m_oss = _SNIPER_OSS_OR_DEVTOOL_RE.search(blob)
    if m_oss:
        return f"open-source / dev-only утилита ({m_oss.group(0).strip()})"

    sup_reason = _ss_supply_reason(blob) if _ss_is_supply_side(blob) else ""
    if sup_reason:
        return f"supply-side сервис ({sup_reason.strip()})"

    if not vacancies_enabled:
        m_vac = _SNIPER_MICRO_VACANCY_RE.search(blob)
        if m_vac:
            return f"микро-вакансия при выключенных вакансиях ({m_vac.group(0).strip()})"
        if _VACANCY_SOURCE_OR_CAT_RE.search(f"{niche_s} {src_s}"):
            return f"вакансионный источник при выключенных вакансиях ({src_s or niche_s})"

    return ""


def _capitalize_slug_if_lower(raw: str) -> str:
    s = raw.strip()
    if not s:
        return ""
    if s == s.lower() and re.search(r"[a-zа-яё]", s):
        return s[:1].upper() + s[1:]
    return s


def _extract_brand_from_product_url(url: str | None) -> str | None:
    raw = str(url or "").strip()
    if not raw or _ss_is_platform_url(raw):
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host or "." not in host:
        return None
    parts = [p for p in host.split(".") if p]
    if not parts:
        return None
    base = (
        parts[-2]
        if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey", "docs", "join"}
        else parts[0]
    )
    if not base or len(base) < 2 or base in _GENERIC_URL_BASES or base in _SS_GARBAGE_COMPANIES:
        return None
    if "-" in base or "_" in base:
        tokens = [t for t in re.split(r"[\-_]+", base) if t]
        return " ".join(
            t.upper() if t.lower() in {"ai", "io", "ui", "ux", "vc", "db", "os", "api"} else t[:1].upper() + t[1:]
            for t in tokens
        )
    return base[:1].upper() + base[1:]


def _is_human_fio_not_brand(company: str | None, cleaned_name: str = "") -> bool:
    comp = " ".join(str(company or "").strip().split())
    if not comp:
        return False
    if any(ch in comp for ch in ("@", "/", "_", ".", "&", "+", ":", "|")) or re.search(r"\d", comp):
        return False
    if _BRAND_OR_ORG_TOKEN_RE.search(comp):
        return False

    tokens = [t for t in comp.split(" ") if t]
    if not (1 <= len(tokens) <= 3):
        return False

    name_norm = " ".join(str(cleaned_name or "").strip().split())
    same_as_lead_name = bool(name_norm and comp.lower() == name_norm.lower())

    # Single-word company equal to a known human first name (e.g. company="Олег" when name="Олег")
    if len(tokens) == 1:
        tok = tokens[0]
        if same_as_lead_name and tok.lower() in _HUMAN_FIRST_NAMES:
            if re.fullmatch(r"[A-Z][a-z]{1,18}|[А-ЯЁ][а-яё]{1,18}", tok):
                return True
        return False

    # All words must be TitleCase alphabetic (no internal camelCase like AlphaFlow or T3tris)
    all_latin_title = all(
        re.fullmatch(r"[A-Z][a-z]{1,18}(?:-[A-Z][a-z]{1,18})?|[A-Z]\.?", t) for t in tokens
    )
    all_cyr_title = all(
        re.fullmatch(r"[А-ЯЁ][а-яё]{1,18}(?:-[А-ЯЁ][а-яё]{1,18})?|[А-ЯЁ]\.?", t) for t in tokens
    )
    if not (all_latin_title or all_cyr_title):
        return False

    first_low = tokens[0].lower().rstrip(".")
    last_low = tokens[-1].lower().rstrip(".")
    if first_low in _HUMAN_FIRST_NAMES or last_low in _HUMAN_FIRST_NAMES:
        return True
    if all_latin_title and any(last_low.endswith(suf) for suf in _SURNAME_SUFFIXES_LATIN):
        return True
    if all_cyr_title and any(last_low.endswith(suf) for suf in _SURNAME_SUFFIXES_CYRILLIC):
        return True
    if same_as_lead_name and len(tokens) in (2, 3):
        return True
    return False


def _strip_hn_and_title_noise(raw: str) -> tuple[str, bool]:
    """Return (cleaned_string, had_hn_prefix)."""
    s = _fix_mojibake(raw).strip()
    if not s:
        return "", False
    had_hn = bool(_HN_PREFIX_RE.match(s))
    if had_hn:
        s = _HN_PREFIX_RE.sub("", s).strip()
        s = re.split(r"\s+[—–\-:]\s+|\s*:\s+", s, maxsplit=1)[0].strip()
        s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    s = _RAW_URL_IN_TEXT_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s).strip(" \t\r\n-|–—/")
    had_num = bool(_NUM_PREFIX_RE.match(s))
    if had_num:
        s = _NUM_PREFIX_RE.sub("", s).strip()
        if " " not in s:
            s = _capitalize_slug_if_lower(s)
    elif had_hn and " " not in s:
        s = _capitalize_slug_if_lower(s)
    return s, had_hn


def clean_crm_lead_names(
    name: str | None,
    company: str | None,
    url: str | None = None,
) -> tuple[str, str | None]:
    raw_name = _fix_mojibake(name).strip()
    raw_comp = _fix_mojibake(company).strip() if company is not None else ""

    embedded_urls = _RAW_URL_IN_TEXT_RE.findall(f"{raw_name} {raw_comp}")
    effective_url = str(url or "").strip() or (embedded_urls[0] if embedded_urls else "")

    clean_name, name_had_hn = _strip_hn_and_title_noise(raw_name)
    clean_comp_str, comp_had_hn = _strip_hn_and_title_noise(raw_comp)
    clean_comp: str | None = clean_comp_str or None

    if not clean_name and raw_name:
        clean_name = _extract_brand_from_product_url(effective_url) or raw_name

    if name_had_hn and clean_name and (clean_comp is None or comp_had_hn):
        clean_comp = clean_name

    if clean_comp is not None and _is_human_fio_not_brand(clean_comp, clean_name):
        clean_comp = _extract_brand_from_product_url(effective_url)

    return clean_name, clean_comp


def is_protected_crm_deal(lead: dict[str, Any]) -> bool:
    status_low = str(lead.get("status") or "").strip().lower()
    if status_low in _PROTECTED_LEAD_STATUSES:
        return True
    try:
        if float(lead.get("amount") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        if float(lead.get("monthly_value") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    airtable_low = str(lead.get("airtable_status") or "").strip().lower()
    if airtable_low in _PROTECTED_AIRTABLE_STATUSES:
        return True
    return False


def _is_cold_outreach_eligible_lead(
    ld: dict[str, Any],
    *,
    outreach_lead_ids: set[int],
    outreach_usernames: set[str],
) -> bool:
    """Return True ONLY for cold-outreach/leadgen leads; never touch manual, folder-synced, or 'Other' leads."""
    if ld.get("folder_id") is not None:
        return False
    src = str(ld.get("lead_source") or "").strip()
    if src and _WARM_MANUAL_SOURCE_RE.search(src):
        return False
    if src and _COLD_OUTREACH_OR_LEADGEN_SOURCE_RE.search(src):
        return True
    lid = int(ld.get("id") or 0)
    uname = _clean_username_token(ld.get("username")).lower()
    if lid in outreach_lead_ids or (uname and uname in outreach_usernames):
        return True
    if not src:
        # Untouched newborn row without folder/dialog/source (e.g. unattributed raw insert)
        status_low = str(ld.get("status") or "").strip().lower()
        has_dialog = ld.get("dialog_id") is not None and int(ld.get("dialog_id") or 0) > 0
        return status_low == "new" and not has_dialog
    return False


def _is_vacancy_or_qa_campaign(camp_name: str | None, leadgen_sources_json: str | None = None) -> bool:
    low = str(camp_name or "").strip().lower()
    if "[qa" in low or "qa тест" in low or "тест своих" in low:
        return True
    if any(k in low for k in ("ваканс", "vacanc")) or re.search(r"\btg[\s_\-]*jobs?\b", low):
        return True
    if '"vacancies' in str(leadgen_sources_json or "").lower():
        return True
    return False


def _collect_hygiene_findings(conn: sqlite3.Connection) -> dict[str, Any]:
    active_junk_recipients: list[tuple[int, str, dict[str, Any]]] = []
    contactless_leads: list[tuple[int, str, dict[str, Any]]] = []
    active_junk_leads: list[tuple[int, str, dict[str, Any]]] = []
    dirty_title_leads: list[tuple[int, str, str | None, dict[str, Any]]] = []

    outreach_lead_ids: set[int] = set()
    outreach_usernames: set[str] = set()
    for rr_link in conn.execute(
        "SELECT lead_id, username FROM campaign_recipients"
    ).fetchall():
        if rr_link["lead_id"] is not None:
            outreach_lead_ids.add(int(rr_link["lead_id"]))
        u_tok = _clean_username_token(rr_link["username"]).lower()
        if u_tok:
            outreach_usernames.add(u_tok)

    lead_rows = conn.execute("SELECT * FROM leads ORDER BY id ASC").fetchall()
    leads_by_id: dict[int, dict[str, Any]] = {}
    for lr in lead_rows:
        ld = as_dict(lr)
        lid = int(ld["id"])
        leads_by_id[lid] = ld

        if is_protected_crm_deal(ld):
            continue

        status_low = str(ld.get("status") or "").strip().lower()
        lead_source = str(ld.get("lead_source") or "").strip()
        is_cold_src = _is_cold_outreach_eligible_lead(
            ld,
            outreach_lead_ids=outreach_lead_ids,
            outreach_usernames=outreach_usernames,
        )

        if status_low in _ACTIVE_COLD_LEAD_STATUSES and is_cold_src:
            has_contact = _has_messenger_contact(
                username=ld.get("username"),
                phone=ld.get("phone"),
                telegram_user_id=ld.get("telegram_user_id"),
            )
            has_dialog = ld.get("dialog_id") is not None and int(ld.get("dialog_id") or 0) > 0
            if not has_contact and not has_dialog:
                contactless_leads.append(
                    (lid, "нет контакта (username/phone/telegram_user_id/dialog_id)", ld)
                )

            reason = sniper_lead_violation_reason(
                name=str(ld.get("name") or ""),
                company=str(ld.get("company") or ""),
                niche=str(ld.get("niche") or ""),
                note=str(ld.get("note") or ""),
                description=str(ld.get("description") or ""),
                url=str(ld.get("url") or ""),
                lead_source=lead_source,
                vacancies_enabled=False,
            )
            if reason:
                active_junk_leads.append((lid, reason, ld))

        if status_low != "won" and is_cold_src:
            cur_name = str(ld.get("name") or "")
            cur_comp_raw = ld.get("company")
            cur_comp = str(cur_comp_raw).strip() if cur_comp_raw is not None and str(cur_comp_raw).strip() != "" else None
            new_name, new_comp = clean_crm_lead_names(cur_name, cur_comp, ld.get("url"))
            if new_name != cur_name or new_comp != cur_comp:
                dirty_title_leads.append((lid, new_name, new_comp, ld))

    rec_rows = conn.execute(
        """
        SELECT cr.*, c.name AS camp_name, c.leadgen_sources_json AS camp_sources_json
        FROM campaign_recipients cr
        JOIN campaigns c ON c.id = cr.campaign_id
        WHERE cr.status IN ('pending', 'in_progress')
        ORDER BY cr.id ASC
        """
    ).fetchall()
    for rr in rec_rows:
        rd = as_dict(rr)
        rid = int(rd["id"])
        if _is_vacancy_or_qa_campaign(rd.get("camp_name"), rd.get("camp_sources_json")):
            continue
        linked_lead = leads_by_id.get(int(rd["lead_id"])) if rd.get("lead_id") is not None else None
        if linked_lead is not None and is_protected_crm_deal(linked_lead):
            continue

        has_contact = _has_messenger_contact(
            username=rd.get("username"),
            phone=rd.get("phone"),
            telegram_user_id=rd.get("telegram_user_id"),
        )
        if not has_contact:
            active_junk_recipients.append(
                (rid, "нет контакта получателя (username/phone/telegram_user_id)", rd)
            )
            continue

        rec_reason = sniper_lead_violation_reason(
            name=str(rd.get("name") or ""),
            company=str(rd.get("company") or ""),
            niche=str(rd.get("category") or (linked_lead or {}).get("niche") or ""),
            note=f"{rd.get('their_angle') or ''}\n{rd.get('note') or ''}".strip(),
            description=str((linked_lead or {}).get("description") or ""),
            url=str(rd.get("website") or (linked_lead or {}).get("url") or ""),
            lead_source=str((linked_lead or {}).get("lead_source") or ""),
            vacancies_enabled=False,
        )
        if rec_reason:
            active_junk_recipients.append((rid, rec_reason, rd))

    return {
        "active_junk_recipients": active_junk_recipients,
        "contactless_leads": contactless_leads,
        "active_junk_leads": active_junk_leads,
        "dirty_title_leads": dirty_title_leads,
    }


def _collect_fake_session_accounts(conn: sqlite3.Connection) -> list[tuple[int, str, dict[str, Any]]]:
    from app.accounts import _inspect_telethon_session, _is_valid_string_session
    from app.db import resolve_session
    from app.runtime_config import is_vps_runtime
    from app.whatsapp_gateway import _inspect_whatsmeow_db

    flagged: list[tuple[int, str, dict[str, Any]]] = []
    try:
        rows = conn.execute(
            """
            SELECT id, phone, provider, status, session_path, session_string
            FROM accounts
            WHERE status = 'connected'
            ORDER BY id ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return flagged

    vps_mode = is_vps_runtime()
    for raw in rows:
        d = as_dict(raw)
        aid = int(d["id"])
        prov = str(d.get("provider") or "telegram").strip().lower()
        sess_path = str(d.get("session_path") or "").strip()
        sess_str = str(d.get("session_string") or "").strip()
        is_demo = sess_path.startswith("data/sessions/demo")
        if is_demo:
            if vps_mode:
                flagged.append((aid, "демо-аккаунт в продовой БД", d))
            continue
        abs_p = resolve_session(sess_path) if sess_path else None
        disk_exists = bool(abs_p is not None and abs_p.exists() and abs_p.is_file())
        if not vps_mode and not disk_exists and not sess_str:
            continue
        if prov == "whatsapp":
            if (vps_mode or disk_exists) and _inspect_whatsmeow_db(sess_path) is None:
                flagged.append((aid, "отсутствует или подделана БД whatsmeow_device", d))
        else:
            has_real_str = _is_valid_string_session(sess_str)
            has_real_sqlite = _inspect_telethon_session(sess_path) is not None if sess_path else False
            if (vps_mode or disk_exists or bool(sess_str)) and not (has_real_str or has_real_sqlite):
                flagged.append((aid, "отсутствует или подделана Telethon-сессия", d))
    return flagged


def audit_crm_hygiene(conn: sqlite3.Connection) -> dict[str, Any]:
    findings = _collect_hygiene_findings(conn)
    fake_accs = _collect_fake_session_accounts(conn)
    rec_ids = [rid for rid, _, _ in findings["active_junk_recipients"]]
    junk_lead_ids = [lid for lid, _, _ in findings["active_junk_leads"]]
    contactless_ids = [lid for lid, _, _ in findings["contactless_leads"]]
    dirty_ids = [lid for lid, _, _, _ in findings["dirty_title_leads"]]
    fake_acc_ids = [aid for aid, _, _ in fake_accs]

    samples: list[dict[str, Any]] = []
    for aid, reason, ad in fake_accs[:5]:
        samples.append(
            {
                "kind": "fake_session_account",
                "id": aid,
                "phone": ad.get("phone"),
                "provider": ad.get("provider"),
                "reason": reason,
            }
        )
    for rid, reason, rd in findings["active_junk_recipients"][:5]:
        samples.append(
            {
                "kind": "active_junk_recipient",
                "id": rid,
                "username": rd.get("username"),
                "name": rd.get("name"),
                "company": rd.get("company"),
                "reason": reason,
            }
        )
    for lid, reason, ld in findings["contactless_leads"][:5]:
        samples.append(
            {
                "kind": "contactless_lead",
                "id": lid,
                "name": ld.get("name"),
                "company": ld.get("company"),
                "lead_source": ld.get("lead_source"),
                "reason": reason,
            }
        )
    for lid, reason, ld in findings["active_junk_leads"][:5]:
        samples.append(
            {
                "kind": "active_junk_lead",
                "id": lid,
                "username": ld.get("username"),
                "name": ld.get("name"),
                "company": ld.get("company"),
                "reason": reason,
            }
        )
    for lid, new_name, new_comp, ld in findings["dirty_title_leads"][:5]:
        samples.append(
            {
                "kind": "dirty_title_lead",
                "id": lid,
                "old_name": ld.get("name"),
                "new_name": new_name,
                "old_company": ld.get("company"),
                "new_company": new_comp,
            }
        )

    ok = (
        len(rec_ids) == 0
        and len(junk_lead_ids) == 0
        and len(contactless_ids) == 0
        and len(dirty_ids) == 0
        and len(fake_acc_ids) == 0
    )
    return {
        "ok": ok,
        "active_junk_recipient_count": len(rec_ids),
        "active_junk_recipient_ids": rec_ids,
        "active_junk_lead_count": len(junk_lead_ids),
        "active_junk_lead_ids": junk_lead_ids,
        "contactless_lead_count": len(contactless_ids),
        "contactless_lead_ids": contactless_ids,
        "dirty_title_lead_count": len(dirty_ids),
        "dirty_title_lead_ids": dirty_ids,
        "fake_session_account_count": len(fake_acc_ids),
        "fake_session_account_ids": fake_acc_ids,
        "samples": samples[:15],
    }


def heal_crm_hygiene(
    conn: sqlite3.Connection,
    *,
    clear_stale_stage_errors: bool = False,
) -> dict[str, Any]:
    findings = _collect_hygiene_findings(conn)
    fake_accs = _collect_fake_session_accounts(conn)
    now_ts = utc_now()

    healed_fake_accounts = 0
    for aid, reason, _ in fake_accs:
        cur_acc = conn.execute(
            """
            UPDATE accounts
            SET status = 'error',
                health = 'unauthorized',
                last_error = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'connected'
            """,
            (f"Карантин сессии: {reason}", now_ts, aid),
        )
        healed_fake_accounts += int(cur_acc.rowcount or 0)

    quarantined_recipients = 0
    for rid, reason, _ in findings["active_junk_recipients"]:
        cur = conn.execute(
            """
            UPDATE campaign_recipients
            SET status = 'completed',
                error_text = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'in_progress')
            """,
            (f"Остановлено Sniper ICP-фильтром: {reason}", now_ts, rid),
        )
        quarantined_recipients += int(cur.rowcount or 0)

    lead_quarantine_reasons: dict[int, str] = {}
    for lid, reason, _ in findings["contactless_leads"]:
        lead_quarantine_reasons[lid] = reason
    for lid, reason, _ in findings["active_junk_leads"]:
        lead_quarantine_reasons[lid] = reason

    quarantined_leads = 0
    for lid, reason in lead_quarantine_reasons.items():
        cur = conn.execute(
            """
            UPDATE leads
            SET status = 'lost',
                airtable_status = 'Cancelled',
                next_step = ?,
                updated_at = ?
            WHERE id = ?
              AND lower(COALESCE(status, '')) NOT IN ('won', 'on_hold', 'call_scheduled')
              AND COALESCE(amount, 0) <= 0
              AND COALESCE(monthly_value, 0) <= 0
              AND lower(COALESCE(airtable_status, '')) NOT IN ('signed', 'on hold', 'call scheduled')
            """,
            (f"Авто-карантин Sniper ICP: {reason}", now_ts, lid),
        )
        quarantined_leads += int(cur.rowcount or 0)

    sanitized_leads = 0
    for lid, new_name, new_comp, ld in findings["dirty_title_leads"]:
        old_comp = str(ld.get("company") or "").strip()
        old_desc = str(ld.get("description") or "").strip()
        if old_comp and old_desc == old_comp:
            cur = conn.execute(
                """
                UPDATE leads
                SET name = ?,
                    company = ?,
                    description = ?,
                    updated_at = ?
                WHERE id = ?
                  AND lower(COALESCE(status, '')) NOT IN ('won', 'on_hold', 'call_scheduled')
                  AND COALESCE(amount, 0) <= 0
                  AND COALESCE(monthly_value, 0) <= 0
                  AND lower(COALESCE(airtable_status, '')) NOT IN ('signed', 'on hold', 'call scheduled')
                """,
                (new_name, new_comp, new_comp, now_ts, lid),
            )
        else:
            cur = conn.execute(
                """
                UPDATE leads
                SET name = ?,
                    company = ?,
                    updated_at = ?
                WHERE id = ?
                  AND lower(COALESCE(status, '')) NOT IN ('won', 'on_hold', 'call_scheduled')
                  AND COALESCE(amount, 0) <= 0
                  AND COALESCE(monthly_value, 0) <= 0
                  AND lower(COALESCE(airtable_status, '')) NOT IN ('signed', 'on hold', 'call scheduled')
                """,
                (new_name, new_comp, now_ts, lid),
            )
        sanitized_leads += int(cur.rowcount or 0)

    cleared_stage_errors = 0
    if clear_stale_stage_errors:
        cur_err = conn.execute(
            "DELETE FROM kv_settings WHERE key = 'leadgen_stage_errors'"
        )
        cleared_stage_errors = int(cur_err.rowcount or 0)

    if (
        healed_fake_accounts
        or quarantined_recipients
        or quarantined_leads
        or sanitized_leads
        or cleared_stage_errors
    ):
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('cloud_persistence_dirty_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (now_ts, now_ts),
        )
        conn.commit()

    res_dict: dict[str, Any] = {
        "quarantined_recipients": quarantined_recipients,
        "quarantined_leads": quarantined_leads,
        "sanitized_leads": sanitized_leads,
    }
    if healed_fake_accounts:
        res_dict["healed_fake_accounts"] = healed_fake_accounts
    if clear_stale_stage_errors:
        res_dict["cleared_stage_errors"] = cleared_stage_errors
    return res_dict


def _check_treg_ready() -> bool:
    try:
        from leadgen.treg_client import check_treg_ready

        return check_treg_ready()
    except ImportError:
        return False


def _check_monid_ready() -> bool:
    try:
        from leadgen.monid_client import check_monid_ready

        return check_monid_ready()
    except ImportError:
        return False


def _parse_iso_ts_utc(raw: Any):
    from datetime import datetime, timezone

    s = str(raw or "").strip()
    if not s:
        return None
    try:
        norm = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(norm)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


STAGE_ERROR_TTL_SECONDS = 6 * 3600


def _count_stage_timeouts_or_errors(conn: sqlite3.Connection) -> int:
    from datetime import datetime, timezone

    try:
        row = conn.execute(
            "SELECT value, updated_at FROM kv_settings WHERE key = 'leadgen_stage_errors'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    if not row or not row["value"]:
        return 0

    now_dt = datetime.now(timezone.utc)
    row_dt = _parse_iso_ts_utc(row["updated_at"])
    try:
        parsed = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return 0

    if isinstance(parsed, dict):
        if row_dt is not None and (now_dt - row_dt).total_seconds() > STAGE_ERROR_TTL_SECONDS:
            return 0
        cnt = 0
        for v in parsed.values():
            s = str(v or "").lower()
            if "timeout" in s or "error" in s or "fail" in s:
                cnt += 1
        return cnt

    if not isinstance(parsed, list):
        return 0
    cnt = 0
    for item in parsed:
        if not isinstance(item, dict):
            continue
        item_dt = _parse_iso_ts_utc(item.get("at")) or row_dt
        if item_dt is not None and (now_dt - item_dt).total_seconds() > STAGE_ERROR_TTL_SECONDS:
            continue
        has_timeout = bool(item.get("timeout")) or "timeout" in str(item.get("error") or "").lower()
        try:
            rc = int(item.get("returncode", 0) or 0)
        except (TypeError, ValueError):
            rc = -1
        if has_timeout or rc != 0:
            cnt += 1
    return cnt


def audit_server_leadgen_readiness(
    conn: sqlite3.Connection,
    *,
    enforce_vps_checks: bool | None = None,
) -> dict[str, Any]:
    enforced = (
        (os.environ.get("CRMTG_VPS_RUNTIME") == "1")
        if enforce_vps_checks is None
        else bool(enforce_vps_checks)
    )
    from app.leadgen_settings import get_leadgen_config

    cfg = get_leadgen_config(conn)
    leadgen_enabled = bool(cfg.get("enabled", True))
    llm_key_ready = bool(
        (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    )
    typesafe_ready = bool(os.environ.get("TYPESAFE_API_KEY", "").strip())
    treg_ready = _check_treg_ready()
    monid_ready = _check_monid_ready()
    stage_timeout_count = _count_stage_timeouts_or_errors(conn)

    b2b_cfg = cfg.get("b2b") or {}
    b2b_enabled = bool(b2b_cfg.get("enabled", True))
    sources = b2b_cfg.get("sources") or {}
    vac_cfg = cfg.get("vacancies") or {}

    scraper_session_required = bool(
        leadgen_enabled
        and (
            (
                b2b_enabled
                and any(
                    bool(sources.get(k))
                    for k in ("tg_groups", "tg_comments", "channel_graph", "tg_ad_spy")
                )
            )
            or bool(vac_cfg.get("enabled", False))
        )
    )
    treg_only_required = any(
        bool(sources.get(k))
        for k in ("meta_ads", "social_x_reddit", "whop_content_rewards")
    )
    vein_needs_any_cli = any(bool(sources.get(k)) for k in ("tiktok_ads", "tiktok_shop"))
    treg_required = bool(
        leadgen_enabled
        and b2b_enabled
        and (treg_only_required or (vein_needs_any_cli and not monid_ready))
    )

    scraper_session_ready = False
    try:
        from leadgen.vps_autonomous_replenish import (
            _extract_sqlite_auth_key_sha256,
            validate_isolated_scraper_session,
        )

        sess_path = validate_isolated_scraper_session(conn)
        if sess_path is not None:
            resolved_p = Path(sess_path)
            if resolved_p.is_file() and _extract_sqlite_auth_key_sha256(resolved_p) is not None:
                scraper_session_ready = True
    except (ValueError, OSError):
        scraper_session_ready = False

    if not leadgen_enabled:
        return {
            "ok": True,
            "enforced": enforced,
            "leadgen_enabled": False,
            "llm_key_ready": llm_key_ready,
            "scraper_session_required": False,
            "scraper_session_ready": scraper_session_ready,
            "treg_required": False,
            "treg_ready": treg_ready,
            "typesafe_ready": typesafe_ready,
            "stage_timeout_count": stage_timeout_count,
            "missing_components": [],
        }

    missing_components: list[str] = []
    if not llm_key_ready:
        missing_components.append("llm_api_key")
    if scraper_session_required and not scraper_session_ready:
        missing_components.append("isolated_scraper_tg_session")
    if treg_required and not treg_ready:
        missing_components.append("treg_cli")
    if stage_timeout_count > 0:
        missing_components.append("stage_errors")

    if enforced:
        ok = bool(
            llm_key_ready
            and (not scraper_session_required or scraper_session_ready)
            and (not treg_required or treg_ready)
            and stage_timeout_count == 0
        )
    else:
        ok = True

    return {
        "ok": ok,
        "enforced": enforced,
        "leadgen_enabled": True,
        "llm_key_ready": llm_key_ready,
        "scraper_session_required": scraper_session_required,
        "scraper_session_ready": scraper_session_ready,
        "treg_required": treg_required,
        "treg_ready": treg_ready,
        "monid_ready": monid_ready,
        "typesafe_ready": typesafe_ready,
        "stage_timeout_count": stage_timeout_count,
        "missing_components": missing_components,
    }


def _direct_x_canonical_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    em = str(row.get("email") or row.get("work_email") or "").strip().lower()
    if em:
        keys.add(f"email:{em}")
    xu = str(row.get("x_url") or row.get("founder_x") or row.get("founder_twitter") or "").strip().lower().rstrip("/")
    if xu:
        if "://" not in xu and not xu.startswith("x.com/"):
            xu = f"https://x.com/{xu.lstrip('@')}"
        keys.add(f"x:{xu}")
    lu = str(row.get("linkedin_url") or row.get("founder_linkedin") or "").strip().lower().rstrip("/")
    if lu:
        keys.add(f"li:{lu}")
    raw_u = str(row.get("url") or row.get("website") or row.get("project_url") or row.get("site") or "").strip()
    if raw_u and not _ss_is_platform_url(raw_u):
        host = (urlparse(raw_u if "://" in raw_u else f"https://{raw_u}").hostname or "").lower().removeprefix("www.").rstrip(".")
        if host and "." in host:
            keys.add(f"dom:{host}")
    return keys


def _safe_http_url(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    low = text.lower()
    if low.startswith(("http://", "https://")):
        return text
    if "://" in text or ":" in text.split("/", 1)[0]:
        return None
    return f"https://{text.lstrip('/')}"


def _normalize_direct_x_record(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    comp = str(out.get("company") or out.get("project") or "").strip() or None
    email = str(out.get("email") or out.get("work_email") or "").strip() or None
    raw_x = str(out.get("x_url") or out.get("founder_x") or out.get("founder_twitter") or "").strip()
    if raw_x and "://" not in raw_x and ":" not in raw_x.split("/", 1)[0]:
        if raw_x.startswith("@") or "/" not in raw_x:
            raw_x = f"https://x.com/{raw_x.lstrip('@')}"
    x_url = _safe_http_url(raw_x)
    linkedin_url = _safe_http_url(out.get("linkedin_url") or out.get("founder_linkedin") or "")
    url = _safe_http_url(out.get("url") or out.get("website") or out.get("project_url") or out.get("site") or "")
    angle = str(out.get("their_angle") or out.get("what_they_do") or "").strip() or None
    lead_src = str(out.get("lead_source") or out.get("source") or "").strip() or None
    out["company"] = comp
    out["project"] = comp or out.get("project")
    out["email"] = email
    out["work_email"] = email or out.get("work_email")
    out["x_url"] = x_url
    out["founder_x"] = x_url
    out["linkedin_url"] = linkedin_url
    out["founder_linkedin"] = linkedin_url
    out["url"] = url
    out["project_url"] = url
    out["their_angle"] = angle
    out["what_they_do"] = angle or out.get("what_they_do")
    out["lead_source"] = lead_src
    return out


def append_direct_x_lead(state_dir: Path, payload: dict[str, Any]) -> bool:
    """Append a contactless direct/X/email/LinkedIn ICP lead to state_dir/direct_x_leads.jsonl without duplicates."""
    norm = _normalize_direct_x_record(payload)
    if not any((norm.get("email"), norm.get("x_url"), norm.get("linkedin_url"), norm.get("url"))):
        return False

    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "direct_x_leads.jsonl"
    cand_keys = _direct_x_canonical_keys(norm)

    if target.is_file():
        try:
            for raw_line in target.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(existing, dict):
                    continue
                ex_keys = _direct_x_canonical_keys(existing)
                if cand_keys and ex_keys and (cand_keys & ex_keys):
                    return False
        except OSError:
            pass

    norm.setdefault("saved_at", utc_now())
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(norm, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_direct_x_leads(state_dir: Path, *, limit: int = 50, query: str = "") -> dict[str, Any]:
    target = state_dir / "direct_x_leads.jsonl"
    items: list[dict[str, Any]] = []
    if target.is_file():
        try:
            for raw_line in target.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    items.append(_normalize_direct_x_record(obj))
        except OSError:
            pass

    items.reverse()
    q_low = str(query or "").strip().lower()
    if q_low:
        items = [
            it
            for it in items
            if q_low
            in " ".join(
                str(it.get(k) or "")
                for k in (
                    "name",
                    "company",
                    "project",
                    "email",
                    "work_email",
                    "x_url",
                    "founder_x",
                    "linkedin_url",
                    "founder_linkedin",
                    "url",
                    "project_url",
                    "category",
                    "their_angle",
                    "what_they_do",
                )
            ).lower()
        ]

    eff_limit = max(1, min(500, int(limit or 50)))
    sliced = items[:eff_limit]
    return {
        "ok": True,
        "path": str(target),
        "total": len(items),
        "count": len(sliced),
        "leads": sliced,
        "items": sliced,
    }


_OFM_TG_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_OFM_RESERVED_TG = frozenset({"joinchat", "share", "addstickers", "proxy", "iv", "grouphelp", "rose"})


def _clean_personal_tg_username(raw: Any) -> str:
    handle = str(raw or "").strip().lstrip("@").strip()
    if not handle or not _OFM_TG_HANDLE_RE.match(handle):
        return ""
    low = handle.casefold()
    if low.endswith("bot") or low in _OFM_RESERVED_TG:
        return ""
    return handle


def _ofm_review_canonical_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    uname = _clean_personal_tg_username(row.get("username") or row.get("telegram_username"))
    if uname:
        keys.add(f"tg:{uname.casefold()}")
    xu = str(row.get("x_url") or row.get("founder_x") or "").strip().lower().rstrip("/")
    if xu:
        keys.add(f"x:{xu}")
    igu = str(row.get("instagram_url") or "").strip().lower().rstrip("/")
    if igu:
        keys.add(f"ig:{igu}")
    em = str(row.get("email") or row.get("work_email") or "").strip().lower()
    if em:
        keys.add(f"email:{em}")
    wa = str(row.get("whatsapp") or "").strip().lower()
    if wa:
        keys.add(f"wa:{wa}")
    return keys


def _normalize_ofm_review_record(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    uname = _clean_personal_tg_username(
        out.get("username") or out.get("telegram_username") or out.get("tg_username")
    )
    tg_url = f"https://t.me/{uname}" if uname else ""
    raw_x = str(out.get("x_url") or out.get("founder_x") or out.get("founder_twitter") or "").strip()
    if raw_x and "://" not in raw_x and ":" not in raw_x.split("/", 1)[0]:
        if raw_x.startswith("@") or "/" not in raw_x:
            raw_x = f"https://x.com/{raw_x.lstrip('@')}"
    x_url = _safe_http_url(raw_x)
    raw_ig = str(out.get("instagram_url") or out.get("ig_url") or "").strip()
    if raw_ig and "://" not in raw_ig and ":" not in raw_ig.split("/", 1)[0]:
        if raw_ig.startswith("@") or "/" not in raw_ig:
            raw_ig = f"https://instagram.com/{raw_ig.lstrip('@')}"
    instagram_url = _safe_http_url(raw_ig)
    email = str(out.get("email") or out.get("work_email") or "").strip() or None
    whatsapp = str(out.get("whatsapp") or "").strip() or None
    url = _safe_http_url(out.get("url") or out.get("project_url") or out.get("website") or "")
    name = str(out.get("name") or out.get("company") or uname or "OFM Buyer").strip()[:160]
    company = str(out.get("company") or out.get("project") or out.get("name") or "").strip()[:160] or None
    source_group = str(out.get("source_group") or out.get("group") or "").strip().lstrip("@")
    signal_summary = str(out.get("signal_summary") or out.get("what_they_do") or "").strip()[:240]
    sample_msg = str(out.get("sample_message") or out.get("message") or "").strip()[:400]
    out.update(
        {
            "name": name,
            "company": company,
            "project": company,
            "username": uname or None,
            "telegram_username": uname or None,
            "tg_url": tg_url or None,
            "x_url": x_url,
            "founder_x": x_url,
            "instagram_url": instagram_url,
            "email": email,
            "work_email": email,
            "whatsapp": whatsapp,
            "url": url,
            "project_url": url,
            "source": str(out.get("source") or "ofm_tg").strip(),
            "source_group": source_group,
            "ofm_tier": str(out.get("ofm_tier") or "traffic_buyer").strip(),
            "icp_score": int(out.get("icp_score") or 85),
            "market": str(out.get("market") or "").strip(),
            "channels": [str(c) for c in (out.get("channels") or []) if str(c).strip()],
            "signal_summary": signal_summary,
            "sample_message": sample_msg,
        }
    )
    return out


def append_ofm_review_lead(state_dir: Path, payload: dict[str, Any]) -> bool:
    """Append an OFM buyer lead with verified direct contact to isolated ofm_review_leads.jsonl."""
    norm = _normalize_ofm_review_record(payload)
    if not any((norm.get("username"), norm.get("x_url"), norm.get("instagram_url"), norm.get("email"), norm.get("whatsapp"))):
        return False

    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "ofm_review_leads.jsonl"
    cand_keys = _ofm_review_canonical_keys(norm)
    if target.is_file():
        try:
            for raw_line in target.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(existing, dict):
                    ex_keys = _ofm_review_canonical_keys(existing)
                    if cand_keys and ex_keys and (cand_keys & ex_keys):
                        return False
        except OSError:
            pass

    norm.setdefault("saved_at", utc_now())
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(norm, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_ofm_review_leads(state_dir: Path, *, limit: int = 100, query: str = "") -> dict[str, Any]:
    """Read isolated OFM buyer leads (newest first) without touching campaign tables."""
    target = state_dir / "ofm_review_leads.jsonl"
    items: list[dict[str, Any]] = []
    if target.is_file():
        try:
            for raw_line in target.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    items.append(_normalize_ofm_review_record(obj))
        except OSError:
            pass

    items.reverse()
    q_low = str(query or "").strip().lower()
    if q_low:
        items = [
            it
            for it in items
            if q_low
            in " ".join(
                str(it.get(k) or "")
                for k in (
                    "name",
                    "company",
                    "username",
                    "source_group",
                    "signal_summary",
                    "sample_message",
                    "market",
                    "x_url",
                    "instagram_url",
                    "email",
                    "url",
                )
            ).lower()
        ]

    eff_limit = max(1, min(500, int(limit or 100)))
    sliced = items[:eff_limit]
    return {
        "ok": True,
        "path": str(target),
        "total": len(items),
        "count": len(sliced),
        "leads": sliced,
        "items": sliced,
    }
