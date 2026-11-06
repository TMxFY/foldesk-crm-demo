#!/usr/bin/env python3
"""Fixed outreach templates. No model calls and no site links."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_NON_PERSON_RE = re.compile(
    r"\b(?:"
    r"labs|studio|group|capital|network|exchange|pay|wallet|chain|protocol|finance|"
    r"marketing|partnership|community|media|tech|coin|token|dao|ai|io|xyz|app|bot|soft|crm|"
    r"founder|ceo|cto|cmo|bd|head|lead|dev|growth|kol|web3|crypto|defi|nft|tg|ws|official|support|"
    r"team|agency|global|ventures|digital|club|hub|games|gaming|play|store|shop|cloud|data|"
    r"hi|hey|hello|there|admin|user|owner|manager|support|sales|info|contact|product|company"
    r")\b",
    re.I,
)
_PLACEHOLDER_RE = re.compile(
    r"(?:\[[^\]]*\]|\{[^}]*\}|u\+[0-9a-f]{4}|\b(?:hi|hey|hello)\s+there\b)",
    re.I,
)


def _extract_personal_first_name(raw_name: str, raw_company: str = "") -> str:
    """Return a clean Latin personal first name, or '' if raw_name is a brand/handle/placeholder."""
    text = (raw_name or "").strip()
    if not text or _PLACEHOLDER_RE.search(text):
        return ""
    if text.startswith("@") or "://" in text or "t.me/" in text.lower():
        return ""
    if re.search(r"\d", text) or "." in text or "_" in text:
        return ""
    comp_clean = re.sub(r"[^a-z0-9]+", "", (raw_company or "").lower())
    name_clean = re.sub(r"[^a-z0-9]+", "", text.lower())
    if comp_clean and name_clean and (name_clean == comp_clean or name_clean in comp_clean):
        return ""
    if _NON_PERSON_RE.search(text):
        return ""
    ascii_text = re.sub(r"[^\x00-\x7F]+", " ", text).strip()
    tokens = [t for t in re.split(r"[\s|/,&\-]+", ascii_text) if t]
    if not tokens or len(tokens) > 3:
        return ""
    first = tokens[0]
    if not re.fullmatch(r"[A-Z][a-z]{1,13}", first):
        return ""
    if _NON_PERSON_RE.search(first):
        return ""
    return first


def _clean_company_for_ping(company: str, site: str = "", name: str = "") -> str:
    """Extract a concise, human-looking company/product name without URLs or placeholders."""
    raw = (company or "").strip()
    raw = _PLACEHOLDER_RE.sub("", raw).strip()
    raw = re.sub(r"^\d+[\.\)]\s*", "", raw).strip()
    raw = re.sub(r"\s*[\(\|].*$", "", raw).strip()
    raw = re.sub(r"https?://\S+", "", raw, flags=re.I).strip().strip("/")
    if raw.startswith("@"):
        raw = ""
    if not raw and site:
        s = (site or "").strip()
        host = (urlparse(s if "://" in s else f"https://{s}").hostname or "").lower().removeprefix("www.")
        base = host.split(".")[0] if host else ""
        if base and base not in {"t", "x", "twitter", "linkedin", "itunes", "apps", "play", "github"}:
            raw = base[:1].upper() + base[1:]
    if not raw and name and not _extract_personal_first_name(name, ""):
        cand = re.sub(r"^\d+[\.\)]\s*", "", name.strip())
        cand = re.sub(r"https?://\S+", "", cand, flags=re.I).strip()
        if cand and not cand.startswith("@") and not _PLACEHOLDER_RE.search(cand):
            raw = cand
    if not raw or len(raw) < 2:
        return "your product"
    if len(raw) > 24:
        raw = raw[:24].rstrip()
    return raw


def micro_ping_first_touch(
    name: str,
    company: str,
    category: str = "",
    site: str = "",
    angle: str = "",
) -> str:
    """Short 1-line question (<=120 chars, no links, no case dump) to break Telegram banner blindness."""
    del category, angle
    comp = _clean_company_for_ping(company, site=site, name=name)
    first_name = _extract_personal_first_name(name, comp)
    if first_name:
        msg = (
            f"Hey {first_name}, saw {comp} — are you already running organic "
            f"IG/TikTok reels for it, or mostly focusing on product?"
        )
        if len(msg) > 120:
            msg = f"Hey {first_name}, saw {comp} — running organic IG/TikTok reels yet, or focused on product?"
    else:
        msg = (
            f"Hey, saw {comp} — are you already running organic "
            f"IG/TikTok reels for it, or mostly focusing on product?"
        )
        if len(msg) > 120:
            msg = f"Hey, saw {comp} — running organic IG/TikTok reels yet, or mostly focused on product?"
    return msg[:120].strip()


_EN_BODY = (
    "We built SoftUGC as a plug-and-play organic distribution arm: AI short-form production "
    "plus a network of owned accounts. We drove 183.5M views in 90 days with zero live shoots, "
    "at 13-40x lower cost than standard clipper services."
)
_RU_BODY = (
    "Мы сделали SoftUGC как готовую органическую дистрибуцию: короткое видео через AI "
    "и сетка своих аккаунтов. 183.5M просмотров за 90 дней без живых съёмок, "
    "в 13-40 раз дешевле обычных клипперов."
)
_EN_PAIN = (
    "Reaching qualified buyers consistently is expensive when short-form production "
    "and distribution are handled manually."
)
_RU_PAIN = (
    "Стабильно доставать нужных покупателей дорого, когда короткие ролики и дистрибуция делаются руками."
)


def _product(product: str, russian: bool) -> str:
    clean = (product or "").strip()
    if clean:
        return clean
    return "твой продукт" if russian else "your product"


def _latin_niche(niche: str) -> str:
    text = (niche or "").strip().rstrip(".")
    if not re.search(r"[A-Za-z]", text):
        return ""
    return text[:180]


def dm_allowed(user) -> bool:
    """True only for a Telegram user. Channels and groups are not DMs."""
    return type(user).__name__ == "User"


def first_touch(product: str, niche: str, russian: bool = False, *, source: str = "") -> str:
    name = _product(product, russian)
    if russian:
        return (
            "Привет,\n\n"
            f"{_RU_PAIN}\n\n"
            f"{_RU_BODY}\n\n"
            f"Показать, как это сработает для {name}?"
        )
    context = _latin_niche(niche)
    if source == "linkedin_jobs":
        if context:
            pain = f"Saw {name} is hiring for {context}. Scaling short-form output with one in-house hire is slow and expensive."
        else:
            pain = f"Saw {name} is hiring for content creation. Scaling short-form output with one in-house hire is slow and expensive."
    elif source == "meta_ads":
        pain = f"Saw {name} is running active video ads. Keeping creative volume fresh without creative fatigue gets expensive fast."
    elif context:
        pain = (
            f"Scaling reach for {context} often means expensive initial reach "
            "and inconsistent organic growth."
        )
    else:
        pain = _EN_PAIN
    return (
        "Hey,\n\n"
        f"{pain}\n\n"
        f"{_EN_BODY}\n\n"
        f"Want me to show how this could work for {name}?"
    )


def followup_one(product: str, niche: str, russian: bool = False) -> str:
    # Niche stays in the signature. The follow-up names the product only.
    del niche
    name = _product(product, russian)
    if russian:
        return f"Рассказать, как мы можем получить это для {name}?"
    return f"Want me to walk you through how we can get that for {name}?"


def followup_two(product: str, russian: bool = False) -> str:
    name = _product(product, russian)
    if russian:
        return f"Интересно органическое продвижение {name}?"
    return f"Are you interested in organic promotion of {name}?"


def interest_reply(russian: bool = False) -> str:
    if russian:
        return "15 минут по твоему продукту. Когда удобно?"
    return "15 minutes on your product. What time works?"


def _how_it_works(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return (
            f"Один короткий ролик на аккаунтах, которые ведём. "
            f"Платишь, только если {name} продаёт. 15 минут. Когда удобно?"
        )
    return (
        f"One short cut on accounts we run. "
        f"You pay only if {name} sells. 15 minutes. What time works?"
    )


def _send_cases(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return f"Больше цифр здесь нет. 15 минут по {name}. Когда удобно?"
    return f"No more numbers here. 15 minutes on {name}. What time works?"


def _not_now(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return f"Понял. Про {name} напишу позже."
    return f"Understood. I will write about {name} later."


def _price(product: str, russian: bool) -> str:
    name = _product(product, russian)
    if russian:
        return (
            f"Платишь, только если {name} продаёт. "
            f"Сумму назовём на 15-минутном созвоне."
        )
    return f"You pay only if {name} sells. The amount is set on a 15-minute call."


def scenario_message(scenario: str, product: str, niche: str = "", russian: bool = False) -> str:
    if scenario == "no_reply_1":
        return followup_one(product, niche, russian=russian)
    if scenario == "no_reply_2":
        return followup_two(product, russian=russian)
    if scenario == "interested":
        return interest_reply(russian=russian)
    if scenario == "how_it_works":
        return _how_it_works(product, russian)
    if scenario == "send_cases":
        return _send_cases(product, russian)
    if scenario == "not_now":
        return _not_now(product, russian)
    if scenario == "price":
        return _price(product, russian)
    raise ValueError(f"unknown scenario: {scenario}")


def big_client_offer() -> str:
    """Team note only. Do not paste this into a lead message."""
    return (
        "Текст для команды, не для отправки лиду. В рассылку это не ставить.\n"
        "Для кого: крупные — Roblox-подобные, лейблы, GTA.\n"
        "Что: клиппинг и AI UGC.\n"
        "Оплата за результат.\n"
        "Доказательство: кейс Roblox. Цифры вписывает Влад, миллионы не выдумывать."
    )
