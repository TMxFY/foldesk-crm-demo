#!/usr/bin/env python3
"""Map curated leads to the Google Sheet format.

Column G is Telegram when the lead has one. Otherwise it is WhatsApp
(https://wa.me/…), a work email, a LinkedIn /in/ profile, a LinkedIn job URL,
or https://x.com/{handle}.
Telegram rows go to «Все2», X-only rows to «X», and LinkedIn, email, or WhatsApp
without Telegram to «Direct». Company pages, noreply mail, and noise URLs are not contacts.
"""
import argparse
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse

# category -> sheet "Категория" label
CAT_LABEL = {
    "CEX / биржа": "CEX",
    "Проп трейдинг": "Проп",
    "Медиа / контент": "Медиа",
    "DeFi / DEX": "DeFi",
    "Токен / мемкоин": "Токен",
    "Gaming / AI": "AI",
    "SaaS / AI app": "SaaS",
    "Платёжки / финтех": "Финтех",
    "Инфраструктура / dev": "Инфра",
    "Сервис / агентство": "Агентство",
    "Низкий приоритет": "Web3",
}

GOOD = {"CEX / биржа", "Проп трейдинг", "Медиа / контент", "DeFi / DEX",
        "Токен / мемкоин", "Gaming / AI", "SaaS / AI app", "Платёжки / финтех"}

# sellable roles (per SOP we sell to founders/growth/marketing/community/creators)
SELL_ROLES = {"Founder", "Marketing", "Community", "Creator/KOL"}

# "Наш угол" per category
OUR_ANGLE = {
    "CEX / биржа": "Направление 3 (клиппинг под реф/инсталлы) + 2 (UGC-инфлюенсеры). CEX живёт на acquisition ритейл-трейдеров через реферальные ссылки — наш клип-фарм закрывает CPM/реф дёшево, сигналы ведём в TG-канал и переливаем на биржу.",
    "Проп трейдинг": "Направление 3 (клиппинг под продажи челленджей). Проп-фирма живёт на продажах челленджей — клип-сетка гонит квалифицированных трейдеров прямо на checkout, органика вместо платного трафика.",
    "Медиа / контент": "Направление 1 (клиппинг архива). У них есть подкасты/эфиры/AMA — режем в ежедневный поток шортсов по 10 аккаунтам с первого дня.",
    "DeFi / DEX": "Направление 3 (клиппинг под юзеров/TVL). Их спейсы и AMA не доходят до шортсов — режем в клипы по 10 аккаунтам со ссылкой на продукт, гоним юзеров и TVL.",
    "Токен / мемкоин": "Направление 2 (AI UGC) + 3 (клиппинг). Хайп-окно мемкоина ~2 дня, команда не успевает делать контент — наш продакшн + сетка выстреливает волной в тот же день.",
    "Gaming / AI": "Направление 2 (AI UGC) + 3 (клиппинг). Аудитория клип-нативная, частые запуски — запускаем AI UGC объёмом и рекламируем их оффер.",
    "SaaS / AI app": "Направление 2 (AI UGC) + 3 (клиппинг под инсталлы/подписки). SaaS/AI-приложение живёт на стоимости привлечения юзера — наша органическая сетка гонит инсталлы и подписки дешевле платного трафика, 183.5M просмотров за 90 дней на прошлом проекте.",
    "Платёжки / финтех": "Направление 2 (UGC-демо) + 3 (клиппинг под регистрации). Консюмерский финтех живёт на объёме регистраций — демо-ролики + дешёвый клиппинг гонят сигнапы через реф-ссылку.",
    "Инфраструктура / dev": "Направление 3 (клиппинг). Dev-тулы и инфраструктура находят проекты через короткое видео — клиппинг по 10 аккаунтам.",
    "Сервис / агентство": "Партнёрская программа ($100/клиент). Агентство может перепродавать наш клиппинг/UGC своим клиентам.",
    "Низкий приоритет": "Направление 3 (клиппинг). Общий web3 — клиппинг по 10 аккаунтам.",
}

def first_touch_for(category, project, their_angle="", source="", site=""):
    """Build Vlad's canonical 3-paragraph SoftUGC first touch."""
    try:
        from outreach_copy import first_touch
    except ImportError:
        try:
            from leadgen.outreach_copy import first_touch
        except ImportError:
            from scripts.outreach_copy import first_touch
    return first_touch(
        project or "",
        their_angle or "",
        russian=False,
        source=source,
        category=category or "",
        site=site or "",
    )


def site_of(r):
    return norm_site(r.get("project_url") or "")


def telegram_username(r):
    return str(r.get("username") or r.get("telegram_username") or "").strip().lstrip("@")


_X_NOISE = {"home", "search", "explore", "i", "share", "intent", "hashtag", "compose"}
_X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}
_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


def _x_handle(value, *, allow_bare):
    """Profile handle, or empty for noise and non-X URLs."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text and "/" not in text and "." not in text and " " not in text:
        if not allow_bare:
            return ""
        if _X_HANDLE.fullmatch(text) and text.casefold() not in _X_NOISE:
            return text
        return ""
    raw = text if "://" in text else "https://" + text
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host not in _X_HOSTS:
        return ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    if not parts or not _X_HANDLE.fullmatch(parts[0]):
        return ""
    if parts[0].casefold() in _X_NOISE:
        return ""
    return parts[0]


def x_profile_url(r):
    """https://x.com/{handle} from founder fields or a verified X contact."""
    for key in ("founder_x", "founder_twitter", "x_url", "twitter_url"):
        handle = _x_handle(r.get(key), allow_bare=True)
        if handle:
            return f"https://x.com/{handle}"
    for item in r.get("social_verified") or r.get("social_contacts") or []:
        if not isinstance(item, dict) or item.get("platform") != "x":
            continue
        handle = _x_handle(item.get("url") or item.get("handle"), allow_bare=True)
        if handle:
            return f"https://x.com/{handle}"
    return ""


_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_BAD_EMAIL_LOCAL = {"noreply", "no-reply"}
_LI_IN_RE = re.compile(
    r"(?:https?://)?(?:[\w.]+\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)",
    re.I,
)
_LI_JOB_RE = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9-]+\.)*linkedin\.com/jobs/view/"
    r"(?:[A-Za-z0-9\-_%]+-)?(\d+)(?=[/?#\s]|$)",
    re.I,
)
_TCO_RE = re.compile(r"https://t\.co/[A-Za-z0-9]+", re.I)
_TCO_LIMIT = 4
_TCO_TIMEOUT_S = 3
_WA_ME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?wa\.me/([%\d+][%\d\s().\-]*)",
    re.I,
)
_WA_PHONE_RE = re.compile(
    r"https?://(?:www\.)?(?:api\.whatsapp\.com|whatsapp\.com)/send(?:/)?\?"
    r"[^#]*?\bphone=(\+?[%\d][%\d\s().\-]{6,})",
    re.I,
)


def _digits(value):
    return re.sub(r"\D", "", unquote(str(value or "")))


def whatsapp_url(value):
    """https://wa.me/<digits> from wa.me or a WhatsApp send link. Groups are ignored."""
    raw = str(value or "")
    if not raw.strip():
        return ""
    found = []
    for match in _WA_ME_RE.finditer(raw):
        digits = _digits(match.group(1))
        if len(digits) >= 8:
            found.append((match.start(), digits))
    for match in _WA_PHONE_RE.finditer(raw):
        digits = _digits(match.group(1))
        if len(digits) >= 8:
            found.append((match.start(), digits))
    if not found:
        return ""
    found.sort(key=lambda item: item[0])
    return f"https://wa.me/{found[0][1]}"


def email_of_text(value):
    """First ordinary address. noreply, no-reply, and .png/.jpg names are skipped."""
    raw = str(value or "")
    for match in _EMAIL_RE.finditer(raw):
        email = match.group(0).strip("._-")
        local, sep, domain = email.partition("@")
        if not sep or not domain or not local:
            continue
        if local.casefold() in _BAD_EMAIL_LOCAL:
            continue
        tail = raw[match.end():match.end() + 4].lower()
        if domain.casefold().endswith((".png", ".jpg")) or tail.startswith((".png", ".jpg")):
            continue
        return email
    return ""


def linkedin_in_url(value):
    """https://www.linkedin.com/in/<slug> without a query. Company pages are not contacts."""
    match = _LI_IN_RE.search(str(value or ""))
    if not match:
        return ""
    slug = match.group(1).strip("._-")
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def linkedin_job_url(value):
    """https://www.linkedin.com/jobs/view/<id> without a query. Not a company page."""
    match = _LI_JOB_RE.search(str(value or ""))
    if not match or not match.group(1).isdigit():
        return ""
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}"


def linkedin_of(r):
    for key in ("founder_linkedin", "linkedin_url"):
        found = linkedin_in_url(r.get(key) or "")
        if found:
            return found
    for item in r.get("social_verified") or r.get("social_contacts") or []:
        if isinstance(item, dict) and item.get("platform") == "linkedin":
            found = linkedin_in_url(item.get("url") or "")
            if found:
                return found
    return ""


def contact_of(r):
    """Telegram, else WhatsApp, else email, else LinkedIn /in/, else a job URL, else X.

    A catalog_ row with none of those returns its public site, or "" if it has none.
    """
    u = telegram_username(r)
    if u:
        return f"https://t.me/{u}"
    wa = whatsapp_url(r.get("whatsapp") or "")
    if wa:
        return wa
    email = email_of_text(r.get("work_email") or "")
    if email:
        return email
    linkedin = linkedin_of(r)
    if linkedin:
        return linkedin
    job = linkedin_job_of(r)
    if job:
        return job
    x = x_profile_url(r)
    if x:
        return x
    return _catalog_site_url(r)


_HARVEST_KEYS = (
    "bio", "sample", "what_they_do", "web_desc", "about", "links", "project_url",
)
_HARVEST_SKIP = {"whatsapp", "work_email", "founder_linkedin"}


def _walk_text(value, into):
    if isinstance(value, str):
        if value.strip():
            into.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _walk_text(item, into)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk_text(item, into)


def _lead_blob(lead):
    parts = []
    for key in _HARVEST_KEYS:
        if key in lead:
            _walk_text(lead.get(key), parts)
    for key, value in lead.items():
        if key in _HARVEST_KEYS or key in _HARVEST_SKIP:
            continue
        _walk_text(value, parts)
    return "\n".join(parts)


def linkedin_job_of(r):
    """Job URL from job_url, or from links/bio and the other text harvest already reads."""
    if not isinstance(r, dict):
        return linkedin_job_url(r)
    found = linkedin_job_url(r.get("job_url") or "")
    if found:
        return found
    return linkedin_job_url(_lead_blob(r))


def _fetch_tco(url):
    """Follow one https://t.co/… link. Timeout is short; errors propagate."""
    import urllib.request
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "creatoros-lead-harvest"},
    )
    with urllib.request.urlopen(request, timeout=_TCO_TIMEOUT_S) as response:
        return response.geturl()


def expand_tco(text, fetch=None):
    """Replace up to 4 unique https://t.co/… links. A fetch error keeps that link."""
    raw = str(text or "")
    urls = []
    seen = set()
    for match in _TCO_RE.finditer(raw):
        url = match.group(0)
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= _TCO_LIMIT:
            break
    if not urls:
        return raw
    getter = _fetch_tco if fetch is None else fetch
    replacements = {}
    for url in urls:
        try:
            target = getter(url)
        except Exception:
            continue
        target = str(target or "").strip()
        if not target.lower().startswith(("http://", "https://")):
            continue
        if _TCO_RE.fullmatch(target):
            continue
        replacements[url] = target
    if not replacements:
        return raw

    def substitute(match):
        return replacements.get(match.group(0), match.group(0))

    return _TCO_RE.sub(substitute, raw)


def harvest_direct(lead, fetch=None):
    """Fill empty whatsapp, work_email, founder_linkedin, and job_url from text on the lead.

    Up to four unique https://t.co/… links in that text are expanded first.
    A fetch error leaves the original link unchanged.
    """
    if not isinstance(lead, dict):
        return {}
    out = dict(lead)
    blob = expand_tco(_lead_blob(out), fetch=fetch)
    if not str(out.get("whatsapp") or "").strip():
        wa = whatsapp_url(blob)
        if wa:
            out["whatsapp"] = wa
    if not str(out.get("work_email") or "").strip():
        email = email_of_text(blob)
        if email:
            out["work_email"] = email
    if not str(out.get("founder_linkedin") or "").strip():
        linkedin = linkedin_in_url(blob)
        if linkedin:
            out["founder_linkedin"] = linkedin
    if not str(out.get("job_url") or "").strip():
        job = linkedin_job_url(blob)
        if job:
            out["job_url"] = job
    return out


def hook_of(r):
    """Keep a real hook. A Telegram row with an empty hook stores WhatsApp there."""
    hook = str(r.get("hook") or "")
    if hook.strip():
        return hook
    if telegram_username(r):
        wa = whatsapp_url(r.get("whatsapp") or "")
        if wa:
            return wa
    return hook


def keep_for_sheet(r):
    """True when the row has a G contact and a sellable (or technical) role.

    A catalog_ row with any contact is kept even without a role.
    """
    if not contact_of(r):
        return False
    if str(r.get("source") or "").startswith("catalog_"):
        return True
    roles = set(r.get("roles") or [])
    social_contact = any(r.get(k) for k in (
        "founder_x", "founder_twitter", "x_url", "twitter_url",
        "founder_linkedin", "linkedin_url",
    )) or bool(r.get("social_verified") or r.get("social_contacts"))
    typesafe_fit = float(r.get("typesafe_fit") or 0.0)
    technical_owner = "Dev" in roles and social_contact and typesafe_fit >= 0.45
    return bool(roles & SELL_ROLES) or technical_owner


_TG_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", re.I)
_SITE_SKIP = {
    "t.me", "telegram.me", "x.com", "twitter.com", "linkedin.com", "t.co",
    "bit.ly", "tinyurl.com", "youtu.be",
}


def norm_site(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text).rstrip("/")
    host = text.split("/", 1)[0]
    if not text or host in _SITE_SKIP or host.endswith(".t.co"):
        return ""
    return text


def tg_handles(text):
    return {h.lower() for h in _TG_RE.findall(str(text or ""))}


_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")


def x_handles(text):
    """Lower-case handles from x.com/twitter.com URLs. Bare text is not a handle."""
    found = set()
    for url in _URL_RE.findall(str(text or "")):
        handle = _x_handle(url.rstrip(".,;:)"), allow_bare=False)
        if handle:
            found.add(handle.lower())
    return found


def _row_x_handles(row):
    """X handles from contact (G) and Founder X (M). Short rows are not over-read."""
    found = set()
    if len(row) > 6:
        found |= x_handles(row[6])
    if len(row) > 12:
        found |= x_handles(row[12])
    return found


def _is_email_contact(contact):
    text = str(contact or "").strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].strip()
    email = email_of_text(text)
    return bool(email) and text.casefold() == email.casefold()


def _contact_route(contact):
    """tg, direct, x, or empty. A public https site is direct. twitter.com and noise stay empty."""
    text = str(contact or "").strip()
    low = text.lower()
    if "t.me" in low or "telegram.me" in low:
        return "tg"
    if whatsapp_url(text):
        return "direct"
    if _is_email_contact(text):
        return "direct"
    if linkedin_in_url(text) or linkedin_job_url(text):
        return "direct"
    if "x.com" in low and _x_handle(text, allow_bare=False):
        return "x"
    if low.startswith("https://") and norm_site(text):
        return "direct"
    return ""


def route_rows(values):
    """Split value rows into ``(tg_rows, x_rows, direct_rows)``.

    Telegram wins when G contains ``t.me`` or ``telegram.me``.
    Otherwise ``https://wa.me/…``, an email, ``linkedin.com/in/…``,
    ``linkedin.com/jobs/view/…``, or another public https site go to Direct.
    An ``https://x.com/{handle}`` profile goes to X. ``twitter.com``,
    ``x.com/home``, social hosts, bare text, and company pages go nowhere.
    """
    tg_rows, x_rows, direct_rows = [], [], []
    for row in values or []:
        kind = _contact_route(row[6] if len(row) > 6 else "")
        if kind == "tg":
            tg_rows.append(row)
        elif kind == "direct":
            direct_rows.append(row)
        elif kind == "x":
            x_rows.append(row)
    return tg_rows, x_rows, direct_rows


def write_routed_payloads(payload_path, dest_dir):
    """Write ``tg.json`` / ``x.json`` / ``direct.json`` only when that side is non-empty."""
    with open(payload_path, encoding="utf-8") as handle:
        data = json.load(handle)
    values = data.get("values") if isinstance(data, dict) else data
    tg_rows, x_rows, direct_rows = route_rows(values or [])
    os.makedirs(dest_dir, exist_ok=True)
    written = []
    for name, rows in (("tg", tg_rows), ("x", x_rows), ("direct", direct_rows)):
        if not rows:
            continue
        path = os.path.join(dest_dir, f"{name}.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump({"values": rows}, out, ensure_ascii=False)
        written.append(name)
    return written


def _linkedin_slugs(text):
    found = set()
    for match in _LI_IN_RE.finditer(str(text or "")):
        slug = match.group(1).strip("._-")
        if slug:
            found.add(slug.casefold())
    return found


def _row_direct_keys(row):
    """LinkedIn slugs from G or O, emails from G or P, and a wa.me number from G."""
    g = row[6] if len(row) > 6 else ""
    o = row[14] if len(row) > 14 else ""
    p = row[15] if len(row) > 15 else ""
    slugs = _linkedin_slugs(g) | _linkedin_slugs(o)
    emails = set()
    for text in (g, p):
        email = email_of_text(text)
        if email:
            emails.add(email.casefold())
    numbers = set()
    wa = whatsapp_url(g)
    if wa:
        numbers.add(wa.rsplit("/", 1)[-1])
    return slugs, emails, numbers


def drop_already_listed(values, existing_values, x_sheet_values=None, direct_sheet_values=None):
    """Drop sheet rows whose Telegram, X, site, or name is already listed.

    Names shorter than 3 characters are ignored so a broken label like ``t``
    does not hide a later real lead, and does not count as a match.
    X handles come from columns G and M. A handle match drops another X-only
    row, including handles that exist only on the X sheet. It does not drop a
    row whose contact contains ``t.me``. The X sheet contributes handles only,
    not names or sites, so a future Telegram lead is not blocked by it.
    Direct keys are a LinkedIn slug (G or column O), an email (G or column P),
    or a wa.me number. They drop an incoming direct row, including keys that
    exist only on the Direct sheet. A ``t.me`` row is not dropped for those
    keys. The Direct sheet does not contribute names or sites.
    """
    names, sites, handles, xseen = set(), set(), set(), set()
    d_slugs, d_emails, d_numbers = set(), set(), set()

    def take_direct(row):
        slugs, emails, numbers = _row_direct_keys(row)
        d_slugs.update(slugs)
        d_emails.update(emails)
        d_numbers.update(numbers)

    def absorb(row):
        name = (row[4] if len(row) > 4 else "").strip().lower()
        site = norm_site(row[5] if len(row) > 5 else "")
        found = tg_handles(row[6] if len(row) > 6 else "")
        if len(name) >= 3:
            names.add(name)
        if site:
            sites.add(site)
        handles.update(found)
        xseen.update(_row_x_handles(row))
        take_direct(row)

    existing = list(existing_values or [])
    if existing:
        head_name = (existing[0][4] if len(existing[0]) > 4 else "").strip().lower()
        head_contact = (existing[0][6] if len(existing[0]) > 6 else "").strip().lower()
        if head_name == "название" or head_contact == "контакт":
            existing = existing[1:]
    for row in existing:
        absorb(row)
    for row in x_sheet_values or []:
        xseen.update(_row_x_handles(row))
    for row in direct_sheet_values or []:
        take_direct(row)
    kept = []
    for row in values:
        name = (row[4] if len(row) > 4 else "").strip().lower()
        site = norm_site(row[5] if len(row) > 5 else "")
        contact = row[6] if len(row) > 6 else ""
        found = tg_handles(contact)
        has_tg = "t.me" in str(contact).lower() or "telegram.me" in str(contact).lower()
        x_hit = bool(_row_x_handles(row) & xseen) and not has_tg
        direct_hit = False
        if not has_tg and _contact_route(str(contact)) == "direct":
            slugs, emails, numbers = _row_direct_keys(row)
            direct_hit = bool(
                (slugs & d_slugs) or (emails & d_emails) or (numbers & d_numbers)
            )
        if ((found & handles) or x_hit or direct_hit
                or (site and site in sites) or (len(name) >= 3 and name in names)):
            continue
        kept.append(row)
        absorb(row)
    return kept


def fetch_sheet_rows(tab="Все2"):
    """Rows A:S for one tab, including the header. Used only for append dedup."""
    import subprocess
    import urllib.parse

    import requests

    token = subprocess.check_output(
        ["/Users/timofei/.local/bin/gsheets-token"], text=True,
    ).strip()
    sheet = urllib.parse.quote(tab)
    url = ("https://sheets.googleapis.com/v4/spreadsheets/"
           f"1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ/values/{sheet}!A:S")
    response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    response.raise_for_status()
    return response.json().get("values") or []


def channel_of(r):
    channels = []
    if r.get("username") or r.get("telegram_username"):
        channels.append("TG")
    if any(r.get(k) for k in ("founder_x", "founder_twitter", "x_url", "twitter_url")):
        channels.append("X")
    if any(r.get(k) for k in ("founder_linkedin", "linkedin_url")):
        channels.append("LinkedIn")
    for item in r.get("social_verified") or r.get("social_contacts") or []:
        p = item.get("platform")
        label = "X" if p == "x" else "LinkedIn" if p == "linkedin" else ""
        if label and label not in channels:
            channels.append(label)
    if whatsapp_url(r.get("whatsapp") or "") and "WA" not in channels:
        channels.append("WA")
    if email_of_text(r.get("work_email") or "") and "Email" not in channels:
        channels.append("Email")
    return " + ".join(channels) or "Web"


def _job_url_on_row(out):
    found = linkedin_job_url(out.get("job_url") or "")
    if found:
        return found
    for item in out.get("links") or []:
        found = linkedin_job_url(item)
        if found:
            return found
    return ""


def _catalog_site_url(out):
    """catalog_*, meta_ads, or tg_ad_spy project_url/site when it is a normal http(s) site, not a social host."""
    source = str(out.get("source") or "")
    if not (source.startswith("catalog_") or source in ("meta_ads", "tg_ad_spy", "tg_channel_graph")):
        return ""
    try:
        from buyer_catalogs import is_public_site
    except ImportError:
        try:
            from leadgen.buyer_catalogs import is_public_site
        except ImportError:
            from scripts.buyer_catalogs import is_public_site
    return is_public_site(str(out.get("project_url") or out.get("site") or ""))


def _admit_direct_row(out, social_rows):
    """LinkedIn /in/, a job URL, work email, WhatsApp, or a catalog site.

    No invented x.com or Telegram. A catalog row with only a public site is
    kept: contact_url is that site and contact_channels includes site.
    """
    wa = whatsapp_url(out.get("whatsapp") or "")
    email = email_of_text(str(out.get("work_email") or ""))
    linkedin = linkedin_in_url(out.get("founder_linkedin") or "") or linkedin_in_url(
        out.get("linkedin_url") or ""
    )
    if not linkedin:
        for social in social_rows or []:
            if isinstance(social, dict) and social.get("platform") == "linkedin":
                linkedin = linkedin_in_url(social.get("url") or "")
                if linkedin:
                    break
    job = _job_url_on_row(out)
    if not (wa or email or linkedin or job):
        site = _catalog_site_url(out)
        if not site:
            return None
        out.pop("telegram_url", None)
        out.pop("telegram_username", None)
        out.pop("founder_x", None)
        out.pop("founder_twitter", None)
        out["username"] = ""
        links = [
            item for item in (out.get("links") or [])
            if "t.me/" not in str(item) and "telegram.me/" not in str(item)
            and "://x.com/" not in str(item).lower()
            and "://twitter.com/" not in str(item).lower()
        ]
        links.append(site)
        out["links"] = list(dict.fromkeys(links))
        out["project_url"] = site
        out["contact_url"] = site
        out["contact_channels"] = ["site"]
        out.pop("social_verified", None)
        out["source"] = out.get("source") or "catalog"
        out["icp_match"] = True
        return out
    out.pop("telegram_url", None)
    out.pop("telegram_username", None)
    out.pop("founder_x", None)
    out.pop("founder_twitter", None)
    out["username"] = ""
    links = [
        item for item in (out.get("links") or [])
        if "t.me/" not in str(item) and "telegram.me/" not in str(item)
    ]
    channels = []
    if wa:
        out["whatsapp"] = wa
        links.append(wa)
        channels.append("whatsapp")
    if email:
        out["work_email"] = email
        channels.append("email")
    if linkedin:
        out["founder_linkedin"] = linkedin
        links.append(linkedin)
        channels.append("linkedin")
    if job:
        out["job_url"] = job
        links.append(job)
        if "linkedin" not in channels:
            channels.append("linkedin")
    out["links"] = list(dict.fromkeys(links))
    out["contact_url"] = wa or email or linkedin or job
    out["contact_channels"] = channels
    kept_social = []
    for social in social_rows or []:
        if isinstance(social, dict) and social.get("platform") == "linkedin" and linkedin_in_url(
            social.get("url") or ""
        ):
            kept_social.append(social)
    if kept_social:
        out["social_verified"] = kept_social
    else:
        out.pop("social_verified", None)
    out["source"] = out.get("source") or "creator_external"
    out["icp_match"] = True
    return out


def admit_discovered_row(row):
    """Admit external/social discovery. Telegram wins; else X; else a direct contact.

    username / telegram_username / telegram_url are set only for a real Telegram
    nick. An X-only lead keeps those fields empty and stores https://x.com/{handle}.
    LinkedIn /in/, a LinkedIn job URL, a work email, or WhatsApp with no X is
    returned without an invented x.com profile and without telegram_username.
    """
    if not isinstance(row, dict):
        return None
    out = dict(row)
    verified = out.get("telegram_verified") or []
    contact = verified[0] if verified and isinstance(verified[0], dict) else {}
    username = str(contact.get("username") or "").strip().lstrip("@")
    if not username:
        bare = str(out.get("username") or out.get("telegram_username") or "").strip().lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{4,32}", bare):
            username = bare
    social_rows = [
        item for item in (out.get("social_verified") or out.get("social_contacts") or [])
        if isinstance(item, dict) and item.get("platform") in {"x", "linkedin"} and item.get("url")
    ]
    if username:
        out["username"] = username
        out["telegram_username"] = username
        out["telegram_url"] = contact.get("url") or f"https://t.me/{username}"
        links = list(out.get("links") or [])
        links.append(out["telegram_url"])
        for social in social_rows:
            key = "founder_x" if social["platform"] == "x" else "founder_linkedin"
            out[key] = social["url"]
            out["social_verified"] = social_rows
            links.append(social["url"])
        out["links"] = list(dict.fromkeys(links))
        out["contact_url"] = out["telegram_url"]
        out["contact_channels"] = list(dict.fromkeys(
            ["telegram"] + [item.get("platform") for item in social_rows if item.get("platform")]
        ))
        out["source"] = out.get("source") or "creator_external"
        out["icp_match"] = True
        return out

    profile = x_profile_url(out)
    if not profile:
        return _admit_direct_row(out, social_rows)
    out.pop("telegram_url", None)
    out.pop("telegram_username", None)
    out["username"] = ""
    links = [
        item for item in (out.get("links") or [])
        if "t.me/" not in str(item) and "telegram.me/" not in str(item)
    ]
    links.append(profile)
    for social in social_rows:
        if social.get("platform") == "linkedin":
            out["founder_linkedin"] = social["url"]
            links.append(social["url"])
    out["links"] = list(dict.fromkeys(links))
    out["founder_x"] = profile
    out["founder_twitter"] = profile
    out["contact_url"] = profile
    channels = []
    for social in social_rows:
        platform = social.get("platform")
        if platform and platform != "telegram" and platform not in channels:
            channels.append(platform)
    if "x" not in channels:
        channels.insert(0, "x")
    out["contact_channels"] = channels
    if social_rows:
        out["social_verified"] = social_rows
    out["source"] = out.get("source") or "creator_external"
    out["icp_match"] = True
    return out


def _row_has_telegram(row):
    if str(row.get("username") or "").strip().lstrip("@"):
        return True
    if str(row.get("telegram_username") or "").strip().lstrip("@"):
        return True
    blobs = [row.get("telegram_url") or ""]
    blobs.extend(row.get("links") or [])
    verified = row.get("telegram_verified") or []
    if isinstance(verified, list):
        for item in verified:
            if isinstance(item, dict):
                blobs.append(item.get("username") or "")
                blobs.append(item.get("url") or "")
    for blob in blobs:
        text = str(blob or "").strip()
        if text and _TG_RE.search(text if "/" in text else "https://t.me/" + text.lstrip("@")):
            return True
    return False


def _row_has_x(row):
    for key in ("founder_x", "founder_twitter", "x_url", "twitter_url"):
        if str(row.get(key) or "").strip():
            return True
    for item in list(row.get("social_verified") or []) + list(row.get("social_contacts") or []):
        if isinstance(item, dict) and item.get("platform") == "x" and (item.get("url") or item.get("handle")):
            return True
    return False


def is_x_only_row(row):
    """X profile and no Telegram. Must not enter master_ids."""
    return isinstance(row, dict) and _row_has_x(row) and not _row_has_telegram(row)


def is_direct_only_row(row):
    """LinkedIn /in/, a job URL, email, WhatsApp, or a catalog site, and neither Telegram nor X.

    Same master rule as X-only: the row stays in raw.new and must not fill
    seen id, site, or contacts. job_url or the contact column G would show
    (a jobs/view URL) counts. Email and WhatsApp are not dedup keys.
    """
    if not isinstance(row, dict) or _row_has_telegram(row) or _row_has_x(row):
        return False
    if linkedin_of(row):
        return True
    if linkedin_job_url(row.get("job_url") or ""):
        return True
    if linkedin_job_url(contact_of(row)):
        return True
    if email_of_text(row.get("work_email") or ""):
        return True
    if whatsapp_url(row.get("whatsapp") or ""):
        return True
    if _catalog_site_url(row):
        return True
    return False


def _skips_master(row):
    return is_x_only_row(row) or is_direct_only_row(row)


def _contact_values(row):
    found = set()
    for key in ("telegram_url", "founder_x", "founder_twitter", "founder_linkedin"):
        value = str(row.get(key) or "").rstrip("/").lower()
        if value:
            found.add(value)
    return found


def merge_master_rows(master_rows, raw_rows, legacy_ids=()):
    """Return ``(master_new, downstream)``.

    ``master_new`` is appended to master_ids. X-only and direct-only never
    enter it. Their id, site, and contacts do not fill the seen sets, so a
    later t.me with the same site or LinkedIn is kept. Email and WhatsApp
    are not dedup keys. ``downstream`` is what ``raw.new`` feeds to enrich:
    X-only and direct-only stay, and so does every Telegram row that passed
    the master dedup.
    """
    seen = set(legacy_ids or ())
    seen_usernames = set()
    seen_projects = set()
    seen_contacts = set()
    for old in master_rows or []:
        if not isinstance(old, dict) or _skips_master(old):
            continue
        seen.add(old.get("id"))
        username = str(old.get("username") or "").lstrip("@").lower()
        if username:
            seen_usernames.add(username)
        project = str(old.get("project_url") or "").rstrip("/").lower()
        if project:
            seen_projects.add(project)
        seen_contacts.update(_contact_values(old))
    master_new = []
    downstream = []
    for row in raw_rows or []:
        if not isinstance(row, dict):
            continue
        if "_update_sent" in row or "_flood_wait_seconds" in row:
            continue
        if _skips_master(row):
            downstream.append(row)
            continue
        username = str(row.get("username") or "").lstrip("@").lower()
        project = str(row.get("project_url") or "").rstrip("/").lower()
        contacts = _contact_values(row)
        if (row.get("id") in seen or (username and username in seen_usernames)
                or (project and project in seen_projects) or contacts & seen_contacts):
            continue
        seen.add(row.get("id"))
        master_new.append(row)
        downstream.append(row)
        if username:
            seen_usernames.add(username)
        if project:
            seen_projects.add(project)
        seen_contacts.update(contacts)
    return master_new, downstream


def apply_master_merge(raw_path, master_path):
    """Append Telegram rows to master_ids. Write those plus X-only and direct-only to raw.new."""
    master_rows = []
    legacy_ids = set()
    try:
        master_file = open(master_path)
    except FileNotFoundError:
        master_file = None
    if master_file is not None:
        with master_file as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    old = json.loads(line)
                except json.JSONDecodeError:
                    try:
                        legacy_ids.add(int(line))
                    except ValueError:
                        pass
                    continue
                if isinstance(old, dict):
                    master_rows.append(old)
                else:
                    try:
                        legacy_ids.add(int(old))
                    except (TypeError, ValueError):
                        pass
    raw_rows = []
    with open(raw_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw_rows.append(json.loads(line))
    master_new, downstream = merge_master_rows(master_rows, raw_rows, legacy_ids=legacy_ids)
    with open(master_path, "a") as handle:
        for row in master_new:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(raw_path + ".new", "w") as handle:
        for row in downstream:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(downstream)


def collect_x_without_tg(paths):
    """Admit JSONL rows that have X and no Telegram. One row per handle."""
    kept = []
    seen = set()
    for path in paths:
        try:
            lines = open(path)
        except FileNotFoundError:
            continue
        with lines as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                admitted = admit_discovered_row(raw)
                if not admitted or str(admitted.get("username") or "").strip():
                    continue
                if admitted.get("telegram_url") or admitted.get("telegram_username"):
                    continue
                profile = x_profile_url(admitted)
                if not profile:
                    continue
                key = profile.rsplit("/", 1)[-1].casefold()
                if not key or key in seen:
                    continue
                # Unverified guesses are not a Telegram contact and must not
                # leave a t.me URL on an X-only row.
                admitted.pop("telegram_candidates", None)
                if "https://t.me/" in json.dumps(admitted):
                    continue
                seen.add(key)
                kept.append(admitted)
    return kept


def social_url(r, *keys):
    for key in keys:
        value = str(r.get(key) or "").strip()
        if value:
            return value if value.startswith("http") else f"https://{value}"
    for item in r.get("social_verified") or r.get("social_contacts") or []:
        if item.get("platform") == ("x" if "x" in keys or "twitter" in " ".join(keys) else "linkedin"):
            return str(item.get("url") or "")
    return ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True, help="JSON with 'values' 2D array")
    p.add_argument("--no-header", action="store_true")
    p.add_argument("--dedup-sheet", action="store_true",
                   help="drop rows already in Все2; X-sheet handles drop X-only rows only")
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]

    header = ["", "Приоритет", "Статус/Фит", "Категория", "Название", "Сайт",
              "Контакт", "Их угол", "Наш угол", "Хук", "Первое касание", "Статус",
              "Founder X", "Founder PH", "Founder LinkedIn",
              "Почта", "Проверка", "Спрос", "Реклама"]
    values = [] if args.no_header else [header]
    tasks = []  # (row_index, lead-for-llm, category, project)

    for r in rows:
        r = harvest_direct(r)
        cat = r.get("category") or "Низкий приоритет"
        roles = set(r.get("roles") or [])
        # G is Telegram, else WhatsApp, email, LinkedIn /in/ or job, else X.
        if not keep_for_sheet(r):
            continue
        social_contact = any(r.get(k) for k in (
            "founder_x", "founder_twitter", "x_url", "twitter_url",
            "founder_linkedin", "linkedin_url",
        )) or bool(r.get("social_verified") or r.get("social_contacts"))
        typesafe_fit = float(r.get("typesafe_fit") or 0.0)
        technical_owner = "Dev" in roles and social_contact and typesafe_fit >= 0.45
        # 🟢 = founder/marketing in a target category; 🟡 = sellable role, generic/web3 category
        hot = bool(roles & {"Founder", "Marketing"}) or technical_owner
        prio = "🟢" if (hot and cat in GOOD) else "🟡"
        tier = r.get("lead_tier")
        fit = f"{prio} Tier {tier}" if tier in {"A", "B"} else prio
        proj = r.get("project") or r.get("name") or ""
        what = r.get("what_they_do") or r.get("web_desc") or ""
        hook = hook_of(r)
        status = "1 касание отправлено" if r.get("message_sent") else ""
        values.append([
            channel_of(r),
            prio,
            fit,
            CAT_LABEL.get(cat, "Web3"),
            proj,
            site_of(r),
            contact_of(r),
            what,
            OUR_ANGLE.get(cat, OUR_ANGLE["Низкий приоритет"]),
            hook,
            "",
            status,
            x_profile_url(r),
            str(r.get("founder_ph") or ""),
            social_url(r, "founder_linkedin", "linkedin_url"),
            str(r.get("work_email") or ""),
            str(r.get("email_status") or ""),
            str(r.get("treg_demand") or ""),
            str(r.get("treg_ads") or ""),
        ])
        tasks.append((len(values) - 1, {
            "name": proj, "source": r.get("source") or "sheet", "founder_name": "",
            "category": cat, "site": r.get("project_url") or "",
            "one_liner": what or r.get("fit") or "",
            "site_text": what or r.get("fit") or "",
        }, cat, proj))

    if tasks:
        import yc_ph_leads as Y
        Y.load_env()
        texts = Y.parallel_first_touch([t[1] for t in tasks])
        templated = 0
        for (row_index, _lead, cat, proj), text in zip(tasks, texts):
            if text:
                values[row_index][10] = text
            else:
                templated += 1
                values[row_index][10] = first_touch_for(
                    cat,
                    proj,
                    _lead.get("one_liner") or "",
                    source=_lead.get("source") or "",
                    site=_lead.get("site") or "",
                )
        if templated:
            print(f"first_touch: {templated}/{len(tasks)} fell back to template "
                  f"after retries", file=sys.stderr)

    if args.dedup_sheet and len(values) > (0 if args.no_header else 1):
        before = len(values) - (0 if args.no_header else 1)
        body = values if args.no_header else values[1:]
        try:
            existing = fetch_sheet_rows()
            try:
                x_existing = fetch_sheet_rows("X")
            except Exception as exc:
                print(f"x sheet dedup skipped ({exc})", file=sys.stderr)
                x_existing = []
            try:
                direct_existing = fetch_sheet_rows("Direct")
            except Exception as exc:
                print(f"direct sheet dedup skipped ({exc})", file=sys.stderr)
                direct_existing = []
            body = drop_already_listed(body, existing, x_existing, direct_existing)
        except Exception as exc:
            print(f"sheet dedup skipped ({exc})", file=sys.stderr)
        else:
            values = body if args.no_header else values[:1] + body
            print(f"sheet dedup: {before} -> {len(body)}", file=sys.stderr)

    with open(args.output, "w") as f:
        json.dump({"values": values}, f, ensure_ascii=False)
    mapped = len(values) - (0 if args.no_header else 1)
    print(f"Mapped {mapped} leads -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
