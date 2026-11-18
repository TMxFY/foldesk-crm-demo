"""Clippers, freelancers, agencies, and service shops sell labour. Buyers own a product."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urlparse

REJECTED_BUYER_CATEGORIES = frozenset(
    {
        "агентство",
        "сервис / агентство",
        "партнёр, не ца",
        "партнер, не ца",
        "низкий приоритет",
        "инфра",
        "инфраструктура / dev",
    }
)

NON_BUYER_ROLES = frozenset(
    {
        "Community",
        "Creator/KOL",
        "Dev",
        "Trader",
        "Investor",
        "Other",
    }
)

PLATFORM_AGGREGATOR_HOSTS = (
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "t.co",
    "linkedin.com",
    "instagram.com",
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegram.org",
    "telegra.ph",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "facebook.com",
    "fb.com",
    "reddit.com",
    "pinterest.com",
    "threads.net",
    "vk.com",
    "vc.ru",
    "habr.com",
    "discord.gg",
    "discord.com",
    "wa.me",
    "whatsapp.com",
    "api.whatsapp.com",
    "gmail.com",
    "bit.ly",
    "tinyurl.com",
    "beacons.ai",
    "solo.to",
    "carrd.co",
    "github.com",
    "gitlab.com",
    "lu.ma",
    "luma.com",
    "news.ycombinator.com",
    "ycombinator.com",
    "huggingface.co",
    "itunes.apple.com",
    "apps.apple.com",
    "play.google.com",
    "chromewebstore.google.com",
    "linktr.ee",
    "linktree.com",
    "notion.site",
    "notion.so",
    "calendly.com",
    "medium.com",
    "substack.com",
    "docs.google.com",
    "drive.google.com",
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

PLATFORM_GARBAGE_COMPANY_NAMES = frozenset(
    {
        "lu",
        "luma",
        "news",
        "huggingface",
        "itunes",
        "apps",
        "play",
        "linktr",
        "linktree",
        "notion",
        "calendly",
        "medium",
        "substack",
        "docs",
        "drive",
        "github",
        "gitlab",
        "ycombinator",
        "telegram",
        "twitter",
        "linkedin",
        "instagram",
        "youtube",
        "discord",
        "producthunt",
        "whop",
        "gumroad",
        "skool",
        "clck",
        "taplink",
    }
)

# Unconditional vendor / agency / freelancer / spammer / dev-only / forum-collab signals.
# These ALWAYS win, even if the bio also says "Founder", "CEO", "hiring", or "my community".
_UNCONDITIONAL_SUPPLY_OR_AGENCY = re.compile(
    r"got accepted into.{0,40}content rewards|my (?:last|next) payout|"
    r"getting paid for (?:my|our) content|revenue sharing program|"
    r"original content rewards|clipster withdrawal|x pays creators|@xcreators|"
    r"\bfreelancer\b|\bfreelance\s+(?:video|editor|marketer|dev|developer|designer|writer|creator)\b|"
    r"\bvideo\s+creator\b|\bambassador\b|"
    r"\bshiller\b|\bhype\s+builder\b|\bkol\s+manager\b|"
    r"\bcommunity\s+moderator\b|\bcommunity\s+manager\b|"
    r"\bgtm\s+consultanc\w*\b|"
    r"\bfinpr\b|\bmarketacross\b|\bpr\s*&\s*growth\b|"
    r"trusted\s+by\s+top\s+web3\s+names|case\s+studies\s+show\s+how\s+great\s+pr|"
    r"\btalent\s+recruitment\b|\brecruiting\s+agenc\w*\b|"
    r"отдел\w*\s+продаж\s+(?:b2b|под\s+ключ)|"
    r"\btaboola\s+expert\b|\bshopify\s+expert\b|\becommerce\s+helper\b|"
    r"\bbot\s+hosting\b|\bmanaged\s+hosting\b|"
    r"\b(?:it|ит)[\s-]+ментор\w*\b|\bсторисмейкер\w*\b|\bтаргетолог\w*\b|"
    r"\b(?:marketing|growth|pr|smm|seo|web3|kol|shilling|clipping|ugc|video|dev|development|automation|crypto|digital|creative|recruiting)\s+(?:agenc\w*|consultanc\w*)\b|"
    r"\bconsultanc\w*\s+for\s+(?:ai|web3|crypto|saas|startups|brands|blockchain|tech|b2b|ecom\w*)\b|"
    r"helping\s+(?:projects|brands|online\s+stores|stores|merchants|founders|startups|clients|businesses|companies)\s+"
    r"(?:trend|get\s+consistent|gain\s+real\s+traction|grow|scale|boost|get\s+more\s+sales)|"
    r"\bshopify\s+merchant\s+help\b|\bmerchant\s+help\s+and\s+developer\b|"
    r"разрабатываем\s+ии-агентов|создаю\s+автономных\s+ии-агентов|"
    r"сервис\s+информационной\s+безопасности|разработк\w*\s+(?:ботов|телеграм-ботов|tg-ботов|сайтов)|"
    r"seo-аудит,\s*продвижение\s+и\s+создание\s+сайтов|"
    r"free,?\s+open[- ]source|\bopen[- ]source\s+(?:mac\s+app|app|cli|tool|library|framework|engine|слой)\b|"
    r"\bmit\s+licens\w*\b|\bapache[\s-]+2\.0\b|\bclaude\s+skills?\b|\bvibecoded\b|"
    r"\bngrok\s+alternative\b|\bcontainer\s+runtime\b|\bcodebase\s+knowledge\s+graph\b|"
    r"движок\s+графов\s+знаний(?:\s+кодовой\s+базы)?|"
    r"синхронизирует\s+ai[- ]кодинг[- ]агентов|\bкодинг[- ]агентов\b|"
    r"\baccount\s+recovery\s+apis?\b|\bethical\s+hacking\b|\bbacklink\s+indexing\b|"
    r"\btelegram\s+bot\s+setup\b|"
    r"\bjava\s+build\s+tool\b|\bold\s+css\s+hack\b|"
    r"\[open\]|\[(?:builder|modeler|scripter|animator)s?\s+needed\]|"
    r"looking\s+for\s+(?:a\s+|an\s+)?(?:new\s+)?(?:experienced\s+)?(?:3d\s+modeler|modeler|builder|scripter)\b|"
    r"design\s+an\s+asym\s+map|"
    r"\bi\s+clip\b|\bi['’]?m\s+a\s+clipper\b|\bi\s+am\s+a\s+clipper\b|\bwe\s+clip\b|"
    r"\$\s*\d+\s*(?:per|/)\s*(?:clip|video|gig)\b|\bper\s+gig\b|"
    r"\[for\s*hire\]|\bfor\s+hire\b|\bopen\s+to\s+work\b|"
    r"looking\s+for\s+(?:a\s+)?job|looking\s+for\s+work|ищу\s+работу|ищу\s+заказы|"
    r"партн[её]р,\s*не\s*ца|сервис\s*/\s*агентство",
    re.I,
)

_UGC_CREATOR_RE = re.compile(r"\bugc\s+creator\b", re.I)
_BUYING_UGC_CREATOR_RE = re.compile(
    r"\b(?:looking\s+for|hiring|need|seeking|hire|find)\s+(?:a\s+|an\s+|some\s+)?ugc\s+creators?\b",
    re.I,
)

# Explicit clipper-recruitment phrases override soft "portfolio" mentions only.
_BUYER = re.compile(
    r"\bwe\s+acquire\b|\bwe\s+buy\s+and\s+grow\b|\bacquisition\s+studio\b|"
    r"looking\s+for\s+clippers|hiring\s+clippers|need\s+clippers|"
    r"paying\s+clippers|pay\s+per\s+view|"
    r"my\s+students|my\s+cohort|my\s+course",
    re.I,
)
_SUPPLY = re.compile(
    r"\bportfolio\b|no\s+followers\s+needed",
    re.I,
)

_RAW_COHORT_RE = re.compile(
    r"^(?:yc\s*)?[swfx]\d{2}$|^yc\s+(?:winter|summer|fall|spring)\s+20\d{2}$",
    re.I,
)
_INSUFFICIENT_PRODUCT_INFO_RE = re.compile(
    r"(?:недостаточно\s+(?:информации|данных)(?:\s+о\s+продукте)?|"
    r"insufficient\s+information\s+about\s+(?:the\s+)?product|"
    r"not\s+enough\s+information\s+about\s+(?:the\s+)?product)",
    re.I,
)


def supply_reason(text: str) -> str:
    """Matched supply/agency phrase, or empty when this person may be a product buyer."""
    blob = text or ""
    early = _UNCONDITIONAL_SUPPLY_OR_AGENCY.search(blob)
    if early:
        return early.group(0)
    ugc_m = _UGC_CREATOR_RE.search(blob)
    if ugc_m and not _BUYING_UGC_CREATOR_RE.search(blob):
        return ugc_m.group(0)
    if _BUYER.search(blob):
        return ""
    match = _SUPPLY.search(blob)
    return match.group(0) if match else ""


def is_supply_side(text: str) -> bool:
    """True when the author is selling labour/agency services rather than buying traffic for own product."""
    return bool(supply_reason(text))


def is_rejected_buyer_category(category: str | None) -> bool:
    cat = " ".join(str(category or "").strip().lower().split())
    if not cat:
        return False
    if cat in REJECTED_BUYER_CATEGORIES:
        return True
    return any(
        tok in cat
        for tok in (
            "сервис / агентство",
            "партнёр, не ца",
            "партнер, не ца",
            "низкий приоритет",
            "инфраструктура / dev",
        )
    )


def is_raw_cohort_tag(value: str | None) -> bool:
    cleaned = " ".join(str(value or "").strip().split())
    return bool(cleaned and _RAW_COHORT_RE.fullmatch(cleaned))


def has_insufficient_product_info(text: str | None) -> bool:
    return bool(text and _INSUFFICIENT_PRODUCT_INFO_RE.search(str(text)))


def is_platform_or_social_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in PLATFORM_AGGREGATOR_HOSTS)


def pick_best_product_url(record: Mapping[str, Any]) -> str:
    """Choose the first real product URL over aggregator/shortener URLs across project_url/website/site/url."""
    candidates = [
        str(record.get(key) or "").strip()
        for key in ("project_url", "website", "site", "url")
    ]
    non_empty = [u for u in candidates if u]
    for candidate in non_empty:
        if not is_platform_or_social_url(candidate):
            return candidate
    return non_empty[0] if non_empty else ""


def is_invalid_product_name(name: str | None) -> bool:
    cleaned = " ".join(str(name or "").strip().split())
    if not cleaned:
        return True
    low = cleaned.lower().strip(".-_@/ ")
    if not low or len(low) < 2:
        return True
    if low in {"none", "null", "n/a", "na", "unknown", "your product", "stealth"}:
        return True
    if low in PLATFORM_GARBAGE_COMPANY_NAMES:
        return True
    if is_raw_cohort_tag(cleaned):
        return True
    if has_insufficient_product_info(cleaned):
        return True
    return False


def is_truncated_or_bad_pitch(pitch: str | None, *, min_len: int = 40) -> bool:
    cleaned = str(pitch or "").strip()
    if len(cleaned) < min_len:
        return True
    if is_raw_cohort_tag(cleaned):
        return True
    if has_insufficient_product_info(cleaned):
        return True
    return False


_HN_LAUNCH_PREFIX_RE = re.compile(r"^(?:show|ask|launch|tell)\s+hn\s*:\s*", re.I)
_LEADING_BRACKET_RE = re.compile(r"^(?:\[[^\]]*\]\s*)+")
_LEADING_NUMBER_RE = re.compile(r"^\d+[\.\)]\s*")
_URL_IN_TEXT_RE = re.compile(r"(?:https?://\S+|www\.\S+|t\.me/\S+)", re.I)
_PAREN_HANDLE_RE = re.compile(r"\s*\(@?[A-Za-z0-9_.\-]{2,64}\)\s*")
_CYRILLIC_CHAR_RE = re.compile(r"[А-Яа-яЁё]")
_CYRILLIC_BUSINESS_RE = re.compile(
    r"\b(?:студия|агентство|группа|проект|сервис|биржа|банк|канал|школа|клуб|маркет|лаб|тех|софт|плюс|про)\b",
    re.I,
)
_BRAND_WORD_RE = re.compile(
    r"\b(?:"
    r"labs?|studios?|group|capital|network|exchange|pay|wallet|chain|protocol|finance|fintech|stable|"
    r"marketing|partnership|community|media|tech|coin|token|dao|ai|io|xyz|app|apps|bot|soft|crm|"
    r"team|agency|global|ventures|digital|club|hub|game|games|gaming|sports|play|store|shop|cloud|data|"
    r"science|agents?|analytics|pulse|board|flow|orbit|radar|clip|clips|video|voice|note|notes|fit|fitness|"
    r"skin|glow|serum|health|care|quest|swap|dex|defi|trade|trading|funded|peak|astro|coach|learn|"
    r"sync|stack|grid|node|core|box|base|link|web|net|dev|code|pixel|cyber|meta|hyper|super|ultra|"
    r"smart|fast|open|free|true|real|live|next|omega|alpha|beta|infinity|shiba|piggy|whales"
    r")\b",
    re.I,
)
_COMMON_PERSON_FIRST_NAMES = frozenset(
    {
        "aaron", "adam", "aditya", "alan", "albert", "alex", "alexander", "alexey", "alice",
        "alim", "amanda", "amy", "andrea", "andrew", "andrey", "angela", "ann", "anna",
        "anne", "anthony", "anton", "artem", "arthur", "ashley", "austin", "barbara", "ben",
        "benjamin", "betty", "billy", "bob", "bobby", "brandon", "brenda", "brian", "bruce",
        "bryan", "carl", "carlos", "carol", "caroline", "catherine", "charles", "charlie",
        "chloe", "chris", "christian", "christina", "christine", "christopher", "claire",
        "cynthia", "dan", "daniel", "danny", "david", "deborah", "debra", "denis", "dennis",
        "diana", "diane", "dima", "dmitry", "donald", "donna", "dorothy", "dylan", "edward",
        "elena", "elijah", "elizabeth", "emily", "emma", "eric", "ethan", "eugene", "eva",
        "evgeny", "felix", "frank", "gabriel", "gary", "george", "gerald", "grace", "greg",
        "gregory", "hannah", "harold", "harry", "heather", "helen", "henry", "hugo", "igor",
        "ilya", "irene", "ivan", "jack", "jacob", "jake", "james", "jamie", "jan", "jane",
        "janet", "jason", "jean", "jeff", "jeffrey", "jennifer", "jeremy", "jerry", "jesse",
        "jessica", "jim", "jimmy", "joan", "joe", "john", "johnny", "jonathan", "jordan",
        "jose", "joseph", "josh", "joshua", "joyce", "juan", "judith", "judy", "julia",
        "julie", "justin", "karen", "kate", "katherine", "kathleen", "kathryn", "katya",
        "kayla", "keith", "kelly", "ken", "kenneth", "kevin", "kim", "kimberly", "kirill",
        "kyle", "larry", "laura", "lauren", "lawrence", "leah", "leo", "liam", "linda",
        "lisa", "logan", "lori", "louis", "luca", "lucas", "lucy", "luke", "madison",
        "maksim", "manish", "marco", "margaret", "maria", "marie", "marilyn", "mark",
        "martha", "martin", "mary", "mason", "mateo", "matt", "matthew", "max", "maxim",
        "megan", "melissa", "michael", "michelle", "mike", "misha", "molly", "nadav",
        "nancy", "natalia", "natalie", "nathan", "nicholas", "nick", "nicole", "nikita",
        "nina", "noah", "oleg", "olga", "olivia", "olya", "pamela", "pasha", "patricia",
        "patrick", "paul", "pavel", "peter", "philip", "purav", "rachel", "rahul", "ralph",
        "randy", "raymond", "rebecca", "richard", "rob", "robert", "roger", "roman",
        "ronald", "rose", "roy", "russell", "ruth", "ryan", "sam", "samantha", "sammy",
        "samuel", "sandra", "sara", "sarah", "sasha", "scott", "sean", "sergey", "sharon",
        "shirley", "shubham", "sid", "simon", "sofia", "sophia", "stanislav", "stephanie",
        "stephen", "steve", "steven", "susan", "teresa", "terry", "theo", "theresa",
        "thomas", "tiffany", "tim", "timmy", "timofei", "timothy", "tom", "tommy", "tyler",
        "valeria", "victor", "victoria", "vincent", "virginia", "vishal", "vlad", "vladimir",
        "walter", "wanda", "wayne", "will", "william", "willie", "yuri", "zachary", "zoe",
    }
)
_COMMON_PERSON_LAST_NAMES = frozenset(
    {
        "adams", "allen", "anderson", "baker", "bozina", "brown", "carter", "chen", "clark",
        "cohen", "collins", "cook", "cooper", "cox", "davis", "diaz", "edwards", "evans",
        "flores", "foster", "garcia", "gonzalez", "gray", "green", "gupta", "hall", "harris",
        "hernandez", "hill", "howard", "hughes", "jackson", "james", "jenkins", "johnson",
        "jones", "kelly", "kim", "king", "kumar", "lee", "lewis", "li", "liu", "lopez",
        "martin", "martinez", "miller", "mitchell", "moore", "morales", "morgan", "morris",
        "murphy", "myers", "nelson", "nguyen", "ortiz", "park", "parker", "patel", "perez",
        "perry", "peterson", "phillips", "powell", "price", "ramirez", "reed", "reyes",
        "richardson", "rivera", "roberts", "robinson", "rodriguez", "rogers", "ross",
        "russell", "sanchez", "sanders", "scott", "shah", "sharma", "singh", "smith",
        "sterling", "stewart", "stone", "taylor", "thomas", "thompson", "torres", "turner",
        "vance", "walker", "wang", "ward", "watson", "white", "williams", "wilson", "wood",
        "wright", "yang", "young", "zhang",
    }
)


def sniper_reject_reason(
    text_or_row: str | Mapping[str, Any] | None,
    *,
    category: str | None = None,
) -> str:
    """Return a non-empty reject reason for agencies, PR, dev-only OSS, hosting, or modeler threads."""
    if isinstance(text_or_row, Mapping):
        cat = category if category is not None else str(
            text_or_row.get("category") or text_or_row.get("niche") or ""
        )
        if is_rejected_buyer_category(cat):
            return f"rejected_category:{cat}"
        role_val = str(text_or_row.get("role") or "").strip()
        if role_val and role_val in NON_BUYER_ROLES:
            return f"non_buyer_role:{role_val}"
        raw_roles = text_or_row.get("roles")
        if isinstance(raw_roles, (list, tuple, set)):
            roles_set = {str(x).strip() for x in raw_roles if str(x or "").strip()}
            if roles_set and not (roles_set & {"Founder", "Marketing"}) and (roles_set & NON_BUYER_ROLES):
                return f"non_buyer_role:{sorted(roles_set & NON_BUYER_ROLES)[0]}"
        parts: list[str] = []
        for key in (
            "name",
            "username",
            "company",
            "project",
            "bio",
            "sample",
            "text",
            "title",
            "excerpt",
            "what_they_do",
            "their_angle",
            "our_angle",
        ):
            val = text_or_row.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        blob = "\n".join(parts)
    else:
        if category is not None and is_rejected_buyer_category(category):
            return f"rejected_category:{category}"
        blob = str(text_or_row or "")

    sr = supply_reason(blob)
    if sr:
        return sr
    return ""


def _is_human_person_name(value: str, *, person_hint: str = "") -> bool:
    s = " ".join((value or "").split()).strip()
    if not s:
        return False
    if any(ch.isdigit() for ch in s) or any(ch in s for ch in ("/", "@", "_", ".com", ".io", ".ai", ".app", ".dev")):
        return False
    if _CYRILLIC_CHAR_RE.search(s):
        if _CYRILLIC_BUSINESS_RE.search(s):
            return False
        words_ru = s.split()
        if 1 <= len(words_ru) <= 3 and all(re.fullmatch(r"[А-ЯЁа-яё\-]{2,24}", w) for w in words_ru):
            return True
        return False
    if _BRAND_WORD_RE.search(s):
        return False
    words = s.split()
    # CamelCase or ALLCAPS inside token -> likely a brand (e.g., PulseBoard, MADFRIGHT)
    for w in words:
        clean_w = w.rstrip(".")
        if len(clean_w) >= 2 and clean_w[1:] != clean_w[1:].lower():
            return False
    if len(words) == 1:
        return words[0].lower() in _COMMON_PERSON_FIRST_NAMES
    if len(words) == 2 and all(re.fullmatch(r"[A-Z][a-z]{1,16}\.?", w) for w in words):
        first_low = words[0].rstrip(".").lower()
        last_low = words[1].rstrip(".").lower()
        if (
            first_low in _COMMON_PERSON_FIRST_NAMES
            or last_low in _COMMON_PERSON_LAST_NAMES
            or len(last_low) == 1
            or (person_hint and s.casefold() == person_hint.casefold())
        ):
            return True
    if (
        len(words) == 3
        and re.fullmatch(r"[A-Z][a-z]{1,14}", words[0])
        and re.fullmatch(r"[A-Z]\.?", words[1])
        and re.fullmatch(r"[A-Z][a-z]{1,16}", words[2])
    ):
        return True
    return False


def _clean_domain_brand(site: str | None, bio: str | None = None) -> str:
    raw = str(site or "").strip()
    if not raw or is_platform_or_social_url(raw):
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")
    if not host or "." not in host or host.endswith((".ru", ".рф", ".su", ".xn--p1ai")):
        return ""
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    base = (
        parts[-2]
        if len(parts) >= 3 and parts[0] in {"app", "get", "try", "use", "go", "my", "hey", "docs", "join"}
        else parts[0]
    )
    if not base or len(base) < 2 or base in PLATFORM_GARBAGE_COMPANY_NAMES:
        return ""
    norm = re.sub(r"[^a-z0-9]", "", base.lower())
    if bio and norm:
        for m in re.finditer(r"\b([A-Z0-9][A-Za-z0-9\.\-]{1,30})\b", str(bio)[:300]):
            cand = m.group(1).strip(".-")
            if re.sub(r"[^a-z0-9]", "", cand.lower()) == norm:
                return cand
    if "-" in base or "_" in base:
        tokens = [t for t in re.split(r"[\-_]+", base) if t]
        return " ".join(t.upper() if t in {"ai", "io", "ui", "ux", "os", "api"} else t.capitalize() for t in tokens)
    return base[:1].upper() + base[1:]


def _strip_noise_tokens(raw: str | None, *, strip_subtitle: bool) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    had_hn = bool(_HN_LAUNCH_PREFIX_RE.search(s))
    had_bracket = bool(_LEADING_BRACKET_RE.search(s))
    s = _LEADING_BRACKET_RE.sub("", s).strip()
    s = _HN_LAUNCH_PREFIX_RE.sub("", s).strip()
    s = _LEADING_NUMBER_RE.sub("", s).strip()
    s = _URL_IN_TEXT_RE.sub(" ", s)
    s = _PAREN_HANDLE_RE.sub(" ", s)
    s = re.sub(r"(?:^|\s)@[A-Za-z0-9_]{2,64}\b", " ", s)
    if strip_subtitle or had_hn or had_bracket:
        s = re.split(r"\s+[—–:]\s+|\s+-\s+", s, maxsplit=1)[0].strip()
    s = " ".join(s.split()).strip(" -–—|/,;:")
    return s


def sanitize_person_and_company(
    name: str | None,
    company: str | None,
    site: str | None = None,
    bio: str | None = None,
) -> tuple[str, str]:
    """Clean HN/numbered/[OPEN]/URL/(@handle) noise and replace person-as-company with domain brand or ''."""
    raw_name_had_num = bool(_LEADING_NUMBER_RE.search(str(name or "").strip()))
    clean_name = _strip_noise_tokens(name, strip_subtitle=False)
    if raw_name_had_num and clean_name and clean_name == clean_name.lower() and " " not in clean_name:
        clean_name = clean_name[:1].upper() + clean_name[1:]

    clean_comp = _strip_noise_tokens(company, strip_subtitle=True)
    if clean_comp and clean_comp == clean_comp.lower() and " " not in clean_comp:
        norm = re.sub(r"[^a-z0-9]", "", clean_comp)
        matched_case = ""
        if bio and norm:
            for m in re.finditer(r"\b([A-Z0-9][A-Za-z0-9\.\-]{1,30})\b", str(bio)[:300]):
                cand = m.group(1).strip(".-")
                if re.sub(r"[^a-z0-9]", "", cand.lower()) == norm:
                    matched_case = cand
                    break
        clean_comp = matched_case or (clean_comp[:1].upper() + clean_comp[1:])

    if (
        not clean_comp
        or is_invalid_product_name(clean_comp)
        or _is_human_person_name(clean_comp, person_hint=clean_name)
    ):
        clean_comp = _clean_domain_brand(site, bio=bio)

    return clean_name, clean_comp

