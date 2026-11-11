import asyncio
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import random
import re
import sqlite3
from zoneinfo import ZoneInfo
from typing import Any

from app.accounts import _load as load_account
from app.accounts import _public as public_account
from app.accounts import increment_daily_sent
from app.db import as_dict, utc_now, utc_today
from app.inbox import (
    _cache_messages,
    _is_synthetic_peer_id,
    _record_lead_event_safe,
    stop_outreach_on_reply,
)

CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed")
RECIPIENT_STATUSES = ("pending", "in_progress", "completed", "replied", "failed")

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_RU = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}

DEFAULT_SCHEDULE_DAYS = {
    "mon": {"enabled": True, "start": "09:00", "end": "18:00"},
    "tue": {"enabled": True, "start": "09:00", "end": "18:00"},
    "wed": {"enabled": True, "start": "09:00", "end": "18:00"},
    "thu": {"enabled": True, "start": "09:00", "end": "18:00"},
    "fri": {"enabled": True, "start": "09:00", "end": "18:00"},
    "sat": {"enabled": False, "start": "10:00", "end": "16:00"},
    "sun": {"enabled": False, "start": "10:00", "end": "16:00"},
}

TARGET_CSV_FIELDS = (
    "username",
    "phone",
    "name",
    "company",
    "step1_text",
    "step2_text",
    "step3_text",
    "their_angle",
    "our_angle",
    "category",
    "audience",
    "website",
    "hook",
    "note",
    "telegram_user_id",
)

HEADER_ALIASES = {
    "username": "username",
    "user": "username",
    "tg": "username",
    "telegram": "username",
    "ник": "username",
    "юзернейм": "username",
    "контакт": "username",
    "contact": "username",
    "tg_link": "username",
    "ссылка": "username",
    "name": "name",
    "full_name": "name",
    "fullname": "name",
    "имя": "name",
    "фио": "name",
    "phone": "phone",
    "tel": "phone",
    "телефон": "phone",
    "номер": "phone",
    "whatsapp": "phone",
    "wa": "phone",
    "wa_phone": "phone",
    "company": "company",
    "org": "company",
    "компания": "company",
    "проект": "company",
    "название": "company",
    "сайт": "website",
    "website": "website",
    "url": "website",
    "домен": "website",
    "категория": "category",
    "category": "category",
    "ниша": "category",
    "niche": "category",
    "audience": "audience",
    "target_audience": "audience",
    "целевая аудитория": "audience",
    "целевая_аудитория": "audience",
    "ца": "audience",
    "их угол": "their_angle",
    "their_angle": "their_angle",
    "описание": "their_angle",
    "наш угол": "our_angle",
    "our_angle": "our_angle",
    "оффер": "our_angle",
    "хук": "hook",
    "hook": "hook",
    "первое касание": "step1_text",
    "first_touch": "step1_text",
    "шаг 1": "step1_text",
    "step1": "step1_text",
    "step1_text": "step1_text",
    "фоллоуап 1": "step2_text",
    "followup 1": "step2_text",
    "followup_1": "step2_text",
    "шаг 2": "step2_text",
    "step2": "step2_text",
    "step2_text": "step2_text",
    "фоллоуап 2": "step3_text",
    "followup 2": "step3_text",
    "followup_2": "step3_text",
    "шаг 3": "step3_text",
    "step3": "step3_text",
    "step3_text": "step3_text",
    "note": "note",
    "notes": "note",
    "comment": "note",
    "заметка": "note",
    "комментарий": "note",
    "telegram_user_id": "telegram_user_id",
    "tg_id": "telegram_user_id",
    "user_id": "telegram_user_id",
}


def _parse_account_ids(account_id=None, account_ids=None) -> list[int]:
    raw_list = []
    if isinstance(account_ids, str):
        try:
            parsed = json.loads(account_ids)
            if isinstance(parsed, list):
                raw_list.extend(parsed)
        except Exception:
            pass
    elif isinstance(account_ids, (list, tuple, set)):
        raw_list.extend(account_ids)
    if account_id is not None and not isinstance(account_id, bool):
        raw_list.append(account_id)
    out = []
    seen = set()
    for item in raw_list:
        if isinstance(item, bool) or item is None:
            continue
        try:
            val = int(item)
        except (TypeError, ValueError):
            continue
        if val > 0 and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _clean_handle(raw: str | None) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    val = re.sub(r"^https?://(?:www\.)?t\.me/", "", val, flags=re.IGNORECASE)
    val = re.sub(r"^t\.me/", "", val, flags=re.IGNORECASE)
    val = val.strip().lstrip("@").strip("/")
    return val or None


def _clean_company_title(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"^\d+\.\s+", "", s).strip()
    return s or None


_LEGACY_CREATOROS_RE = re.compile(r"\bCreatorOS\b", re.IGNORECASE)
_LEGACY_STEP1_UPGRADE_RE = re.compile(
    r"(?:\bCreatorOS\b|If your user growth has hit a ceiling|plug-and-play growth arm|Want me to show how\s*$)",
    re.IGNORECASE,
)


def _sanitize_legacy_brand(text: str | None) -> str:
    if not text:
        return ""
    return _LEGACY_CREATOROS_RE.sub("SoftUGC", str(text))


def _needs_legacy_vlad_step1_upgrade(text: str | None) -> bool:
    return bool(text and _LEGACY_STEP1_UPGRADE_RE.search(str(text)))


def _is_b2b_campaign_name(name: str | None) -> bool:
    return not any(k in str(name or "").lower() for k in ("ваканс", "vacanc"))


def _clean_step1_text(
    raw: str | None,
    company: str | None = None,
    *,
    is_b2b: bool = True,
) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    raw_str = str(raw).strip()
    if not raw_str:
        return None, None
    marker = "Actually, here's the rewrite based on what you gave me:"
    if marker in raw_str:
        raw_str = raw_str.split(marker, 1)[1].strip()
    low = raw_str.lower()
    if (
        low.startswith("i can't write this one")
        or low.startswith("i cannot write")
        or "didn't come through" in low
        or "prompt ends at" in low
        or "paste the outreach text" in low
        or "paste the original outreach text" in low
        or bool(re.fullmatch(r"(?:yc\s+[swf]\d{2}|hi,?|hey,?)", low))
    ):
        return None, "Пропущен: в CSV ошибка генерации текста (Cloudflare/пустое описание)"
    if is_b2b and _needs_legacy_vlad_step1_upgrade(raw_str):
        from leadgen.outreach_copy import upgrade_to_vlad_step1

        upgraded = upgrade_to_vlad_step1(raw_str, company=str(company or ""))
        return (upgraded.strip() or None), None
    text = _sanitize_legacy_brand(raw_str).strip()
    return (text or None), None


_OFFLINE_NON_ICP_RE = re.compile(
    r"(?:"
    r"архитектур\w*\s+(?:бюро|визуал|фасад|дизайн|и\s+дизайн)|ведущий\s+архитектор|"
    r"архитектор,\s*исследовател|архитекторы\.рф|арх\.рф|zaharov\s+architect|abd\s+architects|"
    r"archbutik|archivibe|archblago|karst\.studio|architectural\s+(?:bureau|visualization)|"
    r"3d\s+renderings\s+of\s+residential|благоустройств|застройщик|жилых\s+комплекс|новостро|"
    r"брокер\s+по\s+недвижимост|клуб\s+недвижимост|недвижимый\s+подкаст|"
    r"федеральный\s+девелопер|школа\s+девелопера|девелоперы\.рф|девелоперских\s+проектов|"
    r"девелопер\s+создаю|девелопмент\s+и\s+маркетинг|девелопмент\s+на\s+карте|"
    r"городской\s+ландшафт|территор\w*\s+и\s+проект|маркетолог\s+пространств|"
    r"городской\s+среды|общественных\s+пространств|земельных\s+участков|квадратного\s+метра|"
    r"диалоги\s+о\s+городе|проектирую\s+красивые\s+дома|urban\s+mobility|"
    r"дизайн[а-я]*\s+(?:интерьер|ресторан)|дизайн-бюро|текстильн\w*\s+дизайн|decor\s+bonjour|"
    r"механизац\w*\s+декор|стеклофибробетон|квартирный\s+ответ|"
    r"детск\w*\s+(?:уличн|игров|площад|и\s+спортивн)|igratex|silver-town|"
    r"уличн\w*\s+мебел|беседк\w*\s+из|клееного\s+бруса|мебел\w*\s+для\s+ресторан|"
    r"[cс]троительств\w*\s+бассейн|simple-pools|люки\s+и\s+двери|индустриальн\w*\s+парк|"
    r"площадк\w*\s+для\s+собак|жизни\s+с\s+собаками|"
    r"производств\w*\s+брюк|джинс|косметическ\w*\s+бренд|feelnature|эфирн\w*\s+масл|"
    r"психолог|нумеролог|психосоматик|гештальт|бизнес-коуч|аудит\s+коммуникации|"
    r"экскурси|проводник.*пхукет|phuket.*проводник|передержк\w*.*животн|выгул|"
    r"певиц|вокал|говорить\s+или\s+петь|сонграйтинг|музыкальн\w*\s+(?:лейбл|издательств)|"
    r"звукорежиссер|музыка\s+для\s+кино|слушай\s+мои\s+песни|трэк\s+|концерт\w*,\s+спектакл|"
    r"площадок\s+под\s+концерт|медиа\s+о\s+концерт|услуг\w*\s+для\s+артистов|артистов\s+слушали|"
    r"kassy\.ru|организатор\s+мероприятий|elenaetlesgarcons|erphymusic|"
    r"аренда\s+звука|открытые\s+микрофон|dance\s+school|аудиокнига\s+о\s+смысле|"
    r"бухгалтерск\w*\s+компани|бухгалтер\s+пр|налогов\w*\s+помощь|"
    r"внедрени\w*\s+1[cс]|менеджер\s+1[cс]|вокруг\s+1[cс]|"
    r"торговое\s+оборудован|подбор\w*\s+кадр|профессия\s+hr|обучение\s+тм\s+в|"
    r"огэ/егэ|школа\s+искусств|научн\w*\s+клуб|филантропи|общественн\w*\s+деятел|"
    r"вселенной\s+«хронограф»|маркетинг\s*-\s*в\s+бан|"
    r"скачать\s+с\s+яндекс|404\s+ошибка|заблокирован\s+хостинг|"
    r"служб\w*\s+поддержк|техника\s+по\s+выгодным\s+ценам|ивент-агентств\w*\s+с\s+барной|"
    r"co-owner\s+@mol_bar|залов\s+для\s+тренировок|здоровье\s+без\s+аптек|"
    r"i\s+can['’]?t\s+write\s+this\s+one|cloudflare\s+anti-bot\s+interstitial|"
    r"tgcenterapp\.ru|1024ai\.ru|bugbounty\.bi\.zone/profile/|"
    r"запуск\w*\s+контент-завод|усиливать\s+бренд\s+через\s+видеоконтент"
    r")",
    re.I,
)

_OFFLINE_JUNK_USERNAMES = frozenset(
    {
        "valentinsko", "stnslvlbnv", "igorgorbulev", "khasan_ow", "dimosbarbos",
        "skewer74", "radovssky", "katya_volkova16", "damelkin", "zyuk05",
        "valery_leon", "chereshnevaya_masha", "financial_designer", "uncle_kekos",
        "yuliya_yukhta", "super_taco", "jwmw85", "abiiii0707", "vkrupnov",
        "ofmninja", "praamzius", "bychef", "itrecruit", "khibinite",
        "tgsoft_oleg", "tgsoft_roman", "chatgpt985", "pusik34", "igornaumov_ai", "frixeee",
    }
)


def _fix_mojibake(s: str | None) -> str:
    txt = str(s or "")
    if "Ð" in txt or "Ñ" in txt:
        try:
            return txt.encode("latin1").decode("utf-8")
        except Exception:
            return txt
    return txt


def is_offline_non_icp_contact(
    *,
    username: str | None = None,
    name: str | None = None,
    company: str | None = None,
    website: str | None = None,
    their_angle: str | None = None,
) -> bool:
    uname = (username or "").strip().lstrip("@").lower()
    if uname in _OFFLINE_JUNK_USERNAMES:
        return True
    angle_fixed = _fix_mojibake(their_angle)
    blob = " ".join(
        part for part in (_fix_mojibake(name), _fix_mojibake(company), str(website or ""), angle_fixed) if part
    )
    if _OFFLINE_NON_ICP_RE.search(blob):
        return True
    return False


def _extract_extra_fields(recipient: dict) -> dict[str, str]:
    for dict_key in ("extra_fields", "custom_vars"):
        raw_extra = recipient.get(dict_key)
        if isinstance(raw_extra, dict):
            return {str(k): str(v or "") for k, v in raw_extra.items() if str(k).strip()}
    for json_key in ("extra_json", "custom_vars_json"):
        raw_json = recipient.get(json_key)
        if raw_json and isinstance(raw_json, str):
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return {str(k): str(v or "") for k, v in parsed.items() if str(k).strip()}
            except Exception:
                pass
    return {}


_SPINTAX_INNER_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def expand_spintax(text: str, rng: random.Random | None = None) -> str:
    """Раскрывает конструкции Spintax вида {Привет|Добрый день|Здравствуйте}, включая вложенные."""
    out = str(text or "")
    chooser = rng.choice if rng is not None else random.choice
    for _ in range(50):
        if not _SPINTAX_INNER_RE.search(out):
            break
        out = _SPINTAX_INNER_RE.sub(
            lambda m: chooser([opt.strip() for opt in m.group(1).split("|")]),
            out,
        )
    return out


_SOFTWARE_CATEGORIES = frozenset(
    {
        "saas",
        "b2b saas",
        "ai",
        "cex",
        "defi",
        "токен",
        "финтех",
        "fintech",
        "финансы / трейдинг",
        "инфра",
        "проп",
        "web3",
        "crypto",
    }
)

_ML_TRAINING_NOISE_RE = re.compile(
    r"(?:машинн\w*\s+обучен\w*|обучен\w*\s+с\s+подкреплен\w*|дообучен\w*|"
    r"обучен\w*\s+(?:модел\w*|нейросет\w*|llm|ии|агент\w*|робот\w*)|"
    r"обучен\w*,\s*тонк\w*\s+настройк\w*|тонк\w*\s+настройк\w*|"
    r"образовательн\w*\s+(?:клип\w*|контент\w*|ролик\w*|видео|шортс\w*)|"
    r"игров\w*\s+механик\w*|геймификац\w*|gamification|"
    r"\bof\s+course\b|\b(?:ai|ии)[\-\s]?коуч\w*|\bai[\-\s]?coach\w*|"
    r"курс(?:ы|а|ов|ам|ах)?\s+(?:валют\w*|крипт\w*|биткоин\w*|доллар\w*|рубл\w*|акци\w*|обмен\w*)|"
    r"бирж\w*,\s*курс\w*|"
    r"machine\s+learning|reinforcement\s+learning|model\s+training|robot\w*\s+training|training\s+robot\w*|fine[\-\s]?tun\w*)",
    re.IGNORECASE,
)

_AUDIENCE_EDTECH_RE = re.compile(
    r"(?:\b(?:edtech|education|educational|course|courses|academy|bootcamp|tutor|tutoring|"
    r"e-learning|coaching|mentorship|masterclass)\b|"
    r"\bобучен|(?<!\w)курс(?:ы|ов|ам|ах|\b)|инфопродукт|онлайн[\-\s]?школ|(?<!\w)школ[аыуе]|"
    r"академи|ментор|(?<!\w)коуч|репетитор|студент|ученик|образоват)",
    re.IGNORECASE,
)

_AUDIENCE_GAMING_NICHE_RE = re.compile(
    r"(?:\b(?:gaming|igaming|gamedev|game|games|esports|casino)\b|"
    r"\bигр|(?<!\w)гейм|казино|гемблинг|беттинг)",
    re.IGNORECASE,
)

_AUDIENCE_GAMING_DESC_RE = re.compile(
    r"(?:\b(?:game|games|gaming|gamedev|igaming|gameplay|multiplayer|shooter|puzzle|"
    r"rpg|mmorpg|roguelike|arcade|casino|slots|betting|esports|roblox)\b|"
    r"\bигр(?:а|ы|у|ой|овой|овые|оков)\b|геймдев|казино|гемблинг|беттинг|шутер|головоломк)",
    re.IGNORECASE,
)

_AUDIENCE_AUDIO_RE = re.compile(
    r"(?:\b(?:podcast|podcasts|podcaster|music|musician|audio|audiobook|radio|"
    r"spotify|soundcloud|playlist|playlists|listeners)\b|"
    r"подкаст|музык|аудио|радио|слушател)",
    re.IGNORECASE,
)

_AUDIENCE_ECOM_NICHE_RE = re.compile(
    r"(?:\b(?:e-com|ecom|ecommerce|e-commerce|d2c|retail|shopify|dropship|dropshipping|"
    r"agency|agencies|beauty|skincare|cosmetics|supplement|supplements|furniture|apparel|clothing|consulting)\b|"
    r"агентств|маркетплейс|дропшип|товар|косметик|пептид|мебел|пятновыводит|импорт|консалтинг)",
    re.IGNORECASE,
)

_AUDIENCE_ECOM_DESC_RE = re.compile(
    r"(?:\b(?:shopify|dropshipping|ecommerce|e-commerce|d2c|skincare|cosmetics|merch)\b|"
    r"интернет[\-\s]?магазин|дропшиппинг|маркетплейс|физическ\w*\s+товар)",
    re.IGNORECASE,
)


def _is_software_niche(cat_low: str, lead_niche_low: str) -> bool:
    if cat_low in _SOFTWARE_CATEGORIES or lead_niche_low in _SOFTWARE_CATEGORIES:
        return True
    if not cat_low and lead_niche_low:
        return bool(re.search(r"\b(?:saas|defi|web3|crypto|fintech|ai|crm)\b", lead_niche_low))
    return False


def infer_recipient_audience(recipient: dict) -> str:
    """Возвращает целевое слово ЦА ({buyers|students|users|players|listeners}) по нише и полям лида."""
    extra_fields = _extract_extra_fields(recipient)
    norm_extra: dict[str, str] = {}
    for k, v in extra_fields.items():
        ck = str(k or "").strip().lstrip("\ufeff").lower()
        if ck:
            norm_extra[ck] = str(v or "").strip()
            norm_extra[re.sub(r"[\s_\-]+", "_", ck)] = str(v or "").strip()

    for override_key in ("audience", "target_audience", "целевая аудитория", "целевая_аудитория", "ца"):
        raw_override = str(
            norm_extra.get(override_key)
            or recipient.get(override_key)
            or ""
        ).strip()
        if raw_override and "/" not in raw_override and "|" not in raw_override:
            return raw_override

    cat = str(
        recipient.get("category")
        or norm_extra.get("category")
        or norm_extra.get("категория")
        or ""
    ).strip()
    cat_low = cat.lower()
    lead_niche = str(recipient.get("lead_niche") or recipient.get("niche") or "").strip()
    lead_niche_low = lead_niche.lower()
    extra_niche = str(norm_extra.get("niche") or norm_extra.get("ниша") or "").strip()
    is_software = _is_software_niche(cat_low, lead_niche_low)

    company = str(recipient.get("company") or "").strip()
    their_angle = str(recipient.get("their_angle") or "").strip()
    raw_note = str(recipient.get("note") or "").strip()
    if "Наш угол:" in raw_note:
        raw_note = raw_note.split("Наш угол:", 1)[0].strip()

    product_text = f"{company} {their_angle}" if their_angle else f"{company} {raw_note}"
    clean_niche_and_desc = _ML_TRAINING_NOISE_RE.sub(
        " ", f"{cat} {lead_niche} {extra_niche} {product_text}"
    )
    clean_desc = _ML_TRAINING_NOISE_RE.sub(" ", product_text)

    if _AUDIENCE_EDTECH_RE.search(clean_niche_and_desc):
        return "students"

    if _AUDIENCE_GAMING_NICHE_RE.search(f"{cat_low} {lead_niche_low}"):
        return "players"
    if _AUDIENCE_GAMING_DESC_RE.search(clean_desc):
        return "players"
    if (
        "gaming" in extra_niche.lower()
        and not is_software
        and cat_low not in {"агентство", "медиа"}
    ):
        return "players"

    if cat_low in {"podcast", "music", "audio", "подкаст", "музыка", "аудио"}:
        return "listeners"
    if not is_software and _AUDIENCE_AUDIO_RE.search(clean_niche_and_desc):
        return "listeners"

    if cat_low in {"агентство", "agency", "agencies", "e-com", "ecom", "ecommerce", "d2c", "retail"}:
        return "buyers"
    if _AUDIENCE_ECOM_NICHE_RE.search(f"{cat_low} {lead_niche_low}"):
        return "buyers"
    if extra_niche.lower().startswith(("e-com", "agencies")) and not is_software:
        return "buyers"
    if not is_software and _AUDIENCE_ECOM_DESC_RE.search(clean_desc):
        return "buyers"

    return "users"


def render_template(
    template_text: str,
    recipient: dict,
    rng: random.Random | None = None,
    *,
    sanitize_brand: bool = True,
) -> str:
    text = str(template_text or "")
    name_val = (
        (recipient.get("name") or "").strip()
        or (recipient.get("company") or "").strip()
        or (recipient.get("username") or "").strip()
        or "коллега"
    )
    username_val = (recipient.get("username") or "").strip()
    company_val = (recipient.get("company") or "").strip()
    note_val = (recipient.get("note") or recipient.get("their_angle") or "").strip()
    phone_val = (recipient.get("phone") or "").strip()
    website_val = (recipient.get("website") or "").strip()
    category_val = (
        (recipient.get("category") or recipient.get("lead_niche") or recipient.get("niche") or "").strip()
    )
    their_angle_val = (recipient.get("their_angle") or "").strip()
    our_angle_val = (recipient.get("our_angle") or "").strip()
    hook_val = (recipient.get("hook") or "").strip()
    step1_val = (recipient.get("step1_text") or "").strip()
    step2_val = (recipient.get("step2_text") or "").strip()
    step3_val = (recipient.get("step3_text") or "").strip()
    audience_val = infer_recipient_audience(recipient)

    extra_fields = _extract_extra_fields(recipient)
    lookup_by_norm: dict[str, str] = {}
    for col_key, col_val in extra_fields.items():
        clean_k = col_key.strip().lstrip("\ufeff")
        if not clean_k:
            continue
        val_str = str(col_val or "").strip()
        lookup_by_norm[clean_k.lower()] = val_str
        lookup_by_norm[re.sub(r"[\s_\-]+", "_", clean_k.lower())] = val_str

    replacements = {
        "{first_touch}": step1_val,
        "{followup_1}": step2_val,
        "{followup_2}": step3_val,
        "{step1_text}": step1_val,
        "{step2_text}": step2_val,
        "{step3_text}": step3_val,
        "{name}": name_val,
        "{username}": username_val,
        "{company}": company_val,
        "{note}": note_val,
        "{phone}": phone_val,
        "{website}": website_val,
        "{category}": category_val,
        "{their_angle}": their_angle_val,
        "{our_angle}": our_angle_val,
        "{hook}": hook_val,
        "{audience}": audience_val,
        "{target_audience}": audience_val,
    }
    for col_key, col_val in extra_fields.items():
        token = f"{{{col_key}}}"
        if token not in replacements:
            replacements[token] = str(col_val or "").strip()

    for token, value in replacements.items():
        text = text.replace(token, value)

    def _replace_dynamic_token(match: re.Match) -> str:
        raw_token = match.group(1)
        clean_token = raw_token.strip().lstrip("\ufeff")
        if not clean_token or "|" in clean_token:
            return match.group(0)
        low = clean_token.lower()
        if low in lookup_by_norm:
            return lookup_by_norm[low]
        norm_underscore = re.sub(r"[\s_\-]+", "_", low)
        if norm_underscore in lookup_by_norm:
            return lookup_by_norm[norm_underscore]
        alias_field = HEADER_ALIASES.get(low)
        if alias_field and f"{{{alias_field}}}" in replacements:
            return replacements[f"{{{alias_field}}}"]
        if clean_token in recipient and recipient[clean_token] is not None:
            return str(recipient[clean_token]).strip()
        return match.group(0)

    text = re.sub(r"\{([^{}|]+)\}", _replace_dynamic_token, text)
    text = expand_spintax(text, rng=rng)

    lines = []
    brand_checked = _sanitize_legacy_brand(text) if sanitize_brand else text
    norm_text = brand_checked.replace("\u2028\n", "\n").replace("\u2028", "\n")
    for line in norm_text.splitlines():
        cleaned = re.sub(r"[ \t]{2,}", " ", line).strip()
        cleaned = re.sub(r"\s+([,!?.:;])", r"\1", cleaned)
        lines.append(cleaned)
    return "\n".join(lines).strip()



def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _add_hours_iso(iso_str: str | None, hours: int) -> str | None:
    dt = _parse_iso_dt(iso_str)
    if dt is None:
        return None
    target = dt + timedelta(hours=max(0, int(hours or 0)))
    return target.strftime("%Y-%m-%dT%H:%M:%S")


def _find_recipient_dialogs(conn, rec: dict, account_id: int | None = None) -> list[dict]:
    acc_ids = set()
    if account_id:
        acc_ids.add(int(account_id))
    if rec.get("assigned_account_id"):
        acc_ids.add(int(rec["assigned_account_id"]))

    match_clauses = []
    match_params = []
    if rec.get("telegram_user_id"):
        match_clauses.append("peer_id = ?")
        match_params.append(int(rec["telegram_user_id"]))
    if rec.get("lead_id"):
        match_clauses.append("lead_id = ?")
        match_params.append(int(rec["lead_id"]))
    if rec.get("username"):
        clean_u = _clean_handle(rec["username"])
        if clean_u:
            match_clauses.append("(username IS NOT NULL AND lower(username) = lower(?))")
            match_params.append(clean_u)
    if rec.get("phone"):
        phone_str = str(rec["phone"]).strip()
        phone_digits = re.sub(r"\D", "", phone_str)
        peer_cand = int(phone_digits[-12:]) if phone_digits and len(phone_digits) >= 7 else -1
        match_clauses.append("(phone = ? OR peer_id = ?)")
        match_params.extend([phone_str, peer_cand])

    if not match_clauses:
        return []

    sql = f"SELECT * FROM dialogs WHERE peer_type = 'user' AND ({' OR '.join(match_clauses)})"
    params = list(match_params)
    if acc_ids:
        ph = ",".join("?" for _ in acc_ids)
        sql += f" AND account_id IN ({ph})"
        params.extend(sorted(acc_ids))
    rows = conn.execute(sql, params).fetchall()
    return [as_dict(r) for r in rows]


def _dialog_has_client_reply(conn, dialog: dict, rec: dict) -> bool:
    current_step = int(rec.get("current_step") or 0)
    last_sent_at = rec.get("last_sent_at")
    if current_step < 1 and not last_sent_at:
        return False
    ref_ts = str(last_sent_at or rec.get("created_at") or "").strip()
    if int(dialog.get("last_from_peer") or 0) == 1:
        dlg_last_at = str(dialog.get("last_message_at") or "").strip()
        if ref_ts and dlg_last_at:
            if dlg_last_at >= ref_ts:
                return True
        else:
            return True
    dlg_id = int(dialog["id"])
    if ref_ts:
        in_row = conn.execute(
            """
            SELECT id FROM messages
            WHERE dialog_id = ? AND direction = 'in' AND sent_at >= ?
            LIMIT 1
            """,
            (dlg_id, ref_ts),
        ).fetchone()
        if in_row is not None:
            return True
    elif current_step >= 1:
        any_in = conn.execute(
            """
            SELECT id FROM messages
            WHERE dialog_id = ? AND direction = 'in'
            LIMIT 1
            """,
            (dlg_id,),
        ).fetchone()
        if any_in is not None:
            return True
    return False


def _sync_campaign_replies(conn, campaign_id: int, account_id: int | None = None) -> int:
    rows = conn.execute(
        """
        SELECT * FROM campaign_recipients
        WHERE campaign_id = ? AND status IN ('pending', 'in_progress', 'completed')
        """,
        (campaign_id,),
    ).fetchall()
    replied_now = 0
    now = utc_now()
    for row in rows:
        rec = as_dict(row)
        if int(rec.get("current_step") or 0) < 1 and not rec.get("last_sent_at"):
            continue
        dialogs = _find_recipient_dialogs(conn, rec, account_id=account_id)
        if not dialogs and account_id is not None:
            # Also check across any account if assigned_account_id or dialog exists elsewhere
            dialogs = _find_recipient_dialogs(conn, rec, account_id=None)
        has_reply = any(_dialog_has_client_reply(conn, d, rec) for d in dialogs)
        if has_reply:
            conn.execute(
                """
                UPDATE campaign_recipients
                SET status = 'replied', updated_at = ?
                WHERE id = ?
                """,
                (now, rec["id"]),
            )
            stop_outreach_on_reply(
                conn,
                account_id=rec.get("assigned_account_id") or account_id,
                peer_id=rec.get("telegram_user_id"),
                username=rec.get("username"),
                phone=rec.get("phone"),
                lead_id=rec.get("lead_id"),
            )
            replied_now += 1
    if replied_now:
        conn.commit()
    return replied_now


def heal_transient_failed_recipients(conn, campaign_id: int | None = None) -> int:
    now = utc_now()
    where_camp = "AND campaign_id = ?" if campaign_id is not None else ""
    params: tuple = (now, int(campaign_id)) if campaign_id is not None else (now,)
    cur = conn.execute(
        f"""
        UPDATE campaign_recipients
        SET status = CASE WHEN COALESCE(current_step, 0) >= 1 THEN 'in_progress' ELSE 'pending' END,
            error_text = NULL,
            updated_at = ?
        WHERE status = 'failed'
          {where_camp}
          AND (
              error_text LIKE '%InvokeWithLayerRequest%'
              OR error_text LIKE '%ConnectionLayerInvalid%'
              OR error_text LIKE '%authorization key%'
              OR error_text LIKE '%two different IP addresses%'
              OR error_text LIKE '%AuthKey%'
              OR error_text LIKE '%Could not find the input entity%'
              OR error_text LIKE '%PeerUser%'
              OR error_text LIKE '%Сессия %'
              OR error_text LIKE '%Нет связи%'
              OR error_text LIKE '%TimedOut%'
              OR error_text LIKE '%A wait of %'
              OR error_text LIKE '%SpamBot%'
              OR error_text LIKE '%cannot message non-contacts%'
              OR error_text LIKE '%spam-reported%'
          )
        """,
        params,
    )
    healed = int(cur.rowcount or 0)
    if healed > 0:
        conn.execute(
            f"""
            UPDATE campaigns
            SET status = 'running', updated_at = ?
            WHERE status = 'completed'
              {"AND id = ?" if campaign_id is not None else ""}
              AND EXISTS (
                  SELECT 1 FROM campaign_recipients cr
                  WHERE cr.campaign_id = campaigns.id
                    AND cr.status IN ('pending', 'in_progress')
              )
            """,
            params,
        )
        conn.commit()
    return healed


def _classify_outreach_send_error(exc: Exception) -> tuple[str, str]:
    text = str(exc or "").strip()
    blob = f"{exc.__class__.__name__} {text}".casefold()
    if any(
        marker in blob
        for marker in (
            "frozen",
            "spambot",
            "spam-reported",
            "cannot message non-contacts",
            "peerspam",
        )
    ):
        return ("account_dead", "frozen")
    if "userdeactivatedban" in blob or "phonenumberbanned" in blob:
        return ("account_dead", "banned")
    if any(
        marker in blob
        for marker in (
            "invokewithlayer",
            "connectionlayerinvalid",
            "authorization key",
            "two different ip addresses",
            "auth_key",
            "authkey",
            "unauthorized",
            "userdeactivated",
            "сессия не авторизована",
            "сессия сброшена",
        )
    ):
        return ("account_dead", "unauthorized")
    if any(
        marker in blob
        for marker in (
            "floodwait",
            "a wait of ",
            "too many requests",
            "peerflood",
            "нет связи",
            "timedout",
            "timed out",
            "timeout",
            "connectionerror",
            "connection reset",
            "socks",
            "proxy",
        )
    ):
        return ("account_transient", "offline")
    return ("recipient_error", "active")


def get_campaign(conn, campaign_id: int) -> dict:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        raise LookupError("campaign")
    heal_transient_failed_recipients(conn, int(campaign_id))
    camp = as_dict(row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    _sync_campaign_replies(conn, campaign_id, account_ids[0] if len(account_ids) == 1 else None)

    accounts_list = []
    acc_name_map = {}
    for aid in account_ids:
        try:
            acc_pub = public_account(load_account(conn, aid))
            accounts_list.append(acc_pub)
            acc_name_map[int(aid)] = (
                acc_pub.get("telegram_name")
                or acc_pub.get("telegram_username")
                or acc_pub.get("phone")
                or f"Аккаунт #{aid}"
            )
        except LookupError:
            continue

    steps_rows = conn.execute(
        "SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order",
        (campaign_id,),
    ).fetchall()
    steps = [as_dict(s) for s in steps_rows]

    rec_rows = conn.execute(
        """
        SELECT cr.*, l.niche AS lead_niche
        FROM campaign_recipients cr
        LEFT JOIN leads l ON l.id = cr.lead_id
        WHERE cr.campaign_id = ?
        ORDER BY cr.id
        """,
        (campaign_id,),
    ).fetchall()
    now_iso = utc_now()
    recipients = []
    seen_csv_headers: list[str] = []
    seen_csv_headers_set: set[str] = set()
    try:
        stored_headers = json.loads(camp.get("csv_headers_json") or "[]")
        if isinstance(stored_headers, list):
            for h in stored_headers:
                hs = str(h or "").strip()
                if hs and hs not in seen_csv_headers_set:
                    seen_csv_headers_set.add(hs)
                    seen_csv_headers.append(hs)
    except Exception:
        pass

    out_by_lead_id: dict[int, str] = {}
    out_by_peer_id: dict[int, str] = {}
    out_by_uname: dict[str, str] = {}
    out_by_phone: dict[str, str] = {}
    for drow in conn.execute(
        """
        SELECT d.lead_id, d.peer_id, lower(d.username) AS uname, d.phone,
               (
                   SELECT m.text FROM messages m
                   WHERE m.dialog_id = d.id AND m.direction = 'out'
                   ORDER BY m.sent_at DESC, m.id DESC
                   LIMIT 1
               ) AS last_out_text
        FROM dialogs d
        WHERE d.peer_type = 'user'
        ORDER BY d.last_message_at DESC, d.id DESC
        """
    ).fetchall():
        txt = drow["last_out_text"]
        if not txt:
            continue
        if drow["lead_id"] is not None:
            out_by_lead_id.setdefault(int(drow["lead_id"]), txt)
        if drow["peer_id"] is not None:
            out_by_peer_id.setdefault(int(drow["peer_id"]), txt)
        if drow["uname"]:
            out_by_uname.setdefault(str(drow["uname"]).strip().lower(), txt)
        if drow["phone"]:
            out_by_phone.setdefault(str(drow["phone"]).strip(), txt)

    for r in rec_rows:
        rd = as_dict(r)
        extra_fields = _extract_extra_fields(rd)
        rd["extra_fields"] = extra_fields
        for hk in extra_fields.keys():
            hks = str(hk or "").strip()
            if hks and hks not in seen_csv_headers_set:
                seen_csv_headers_set.add(hks)
                seen_csv_headers.append(hks)
        assigned_id = rd.get("sent_account_id") or rd.get("assigned_account_id")
        rd["error"] = rd.get("error_text")
        if assigned_id is not None:
            assigned_id_int = int(assigned_id)
            if assigned_id_int not in acc_name_map:
                try:
                    ap = public_account(load_account(conn, assigned_id_int))
                    acc_name_map[assigned_id_int] = (
                        ap.get("telegram_name")
                        or ap.get("telegram_username")
                        or ap.get("phone")
                        or f"Аккаунт #{assigned_id_int}"
                    )
                except LookupError:
                    acc_name_map[assigned_id_int] = None
            rd["assigned_account_name"] = acc_name_map.get(assigned_id_int)
        else:
            rd["assigned_account_name"] = None

        cur_step = int(rd.get("current_step") or 0)
        status = rd.get("status")
        last_sent_at = rd.get("last_sent_at")
        if status in ("pending", "in_progress") and cur_step < len(steps):
            rd["next_step_order"] = cur_step + 1
            next_delay = int(steps[cur_step].get("delay_hours") or 0)
            rd["next_delay_hours"] = next_delay
        else:
            rd["next_step_order"] = None
            rd["next_delay_hours"] = None

        if status == "in_progress" and last_sent_at and cur_step < len(steps):
            next_delay = int(steps[cur_step].get("delay_hours") or 0)
            if next_delay > 0:
                rd["next_followup_at"] = _add_hours_iso(last_sent_at, next_delay)
            else:
                rd["next_followup_at"] = last_sent_at
        else:
            rd["next_followup_at"] = None

        if status == "pending":
            rd["followup_due"] = True
        elif status == "in_progress" and rd.get("next_followup_at"):
            rd["followup_due"] = str(rd["next_followup_at"]) <= now_iso
        else:
            rd["followup_due"] = False

        # Compute last_sent_text from messages or rendered step
        last_sent_text = None
        if rd.get("lead_id") is not None:
            last_sent_text = out_by_lead_id.get(int(rd["lead_id"]))
        if not last_sent_text and rd.get("telegram_user_id") is not None:
            last_sent_text = out_by_peer_id.get(int(rd["telegram_user_id"]))
        if not last_sent_text and rd.get("username"):
            last_sent_text = out_by_uname.get(str(rd["username"]).strip().lower())
        if not last_sent_text and rd.get("phone"):
            last_sent_text = out_by_phone.get(str(rd["phone"]).strip())
        if last_sent_text is None and cur_step >= 1 and cur_step - 1 < len(steps):
            prev_idx = cur_step - 1
            last_sent_text = _render_step_text(
                steps[prev_idx], prev_idx, rd, campaign_name=camp.get("name")
            )
        rd["last_sent_text"] = last_sent_text

        recipients.append(rd)

    today = utc_today()
    pending_count = sum(1 for r in recipients if r["status"] == "pending")
    in_progress_count = sum(1 for r in recipients if r["status"] == "in_progress")
    completed_count = sum(1 for r in recipients if r["status"] == "completed")
    replied_count = sum(1 for r in recipients if r["status"] == "replied")
    failed_count = sum(1 for r in recipients if r["status"] == "failed")
    sent_today = sum(
        1
        for r in recipients
        if (r.get("last_sent_at") or "").startswith(today)
    )

    try:
        raw_sources = json.loads(camp.get("leadgen_sources_json") or "[]")
        leadgen_sources = [str(s).strip() for s in raw_sources if str(s or "").strip()] if isinstance(raw_sources, list) else []
    except Exception:
        leadgen_sources = []

    camp_name_low = str(camp.get("name") or "").lower()
    raw_lg_enabled = camp.get("leadgen_enabled")
    if raw_lg_enabled is not None and int(raw_lg_enabled or 0) != 0:
        leadgen_enabled = True
    elif raw_lg_enabled == 0 and ("leadgen" in camp_name_low or "ваканс" in camp_name_low) and not camp.get("leadgen_sources_json"):
        leadgen_enabled = True
    else:
        leadgen_enabled = bool(int(raw_lg_enabled or 0))

    raw_lg_quota = camp.get("leadgen_daily_quota")
    leadgen_daily_quota = int(raw_lg_quota) if raw_lg_quota is not None else 20

    camp["account_ids"] = account_ids
    camp["accounts"] = accounts_list
    camp["steps"] = steps
    camp["recipients"] = recipients
    camp["csv_headers"] = seen_csv_headers
    camp["total_recipients"] = len(recipients)
    camp["pending_count"] = pending_count
    camp["in_progress_count"] = in_progress_count
    camp["completed_count"] = completed_count
    camp["replied_count"] = replied_count
    camp["failed_count"] = failed_count
    camp["sent_today"] = sent_today
    camp["leadgen_enabled"] = leadgen_enabled
    camp["leadgen_daily_quota"] = leadgen_daily_quota
    camp["leadgen_sources"] = leadgen_sources
    return camp


def list_campaigns(conn) -> list:
    rows = conn.execute("SELECT id FROM campaigns ORDER BY id DESC").fetchall()
    return [get_campaign(conn, row["id"]) for row in rows]


_DEFAULT_BOILERPLATE_TEMPLATES = frozenset(
    {
        "привет, {name}! пишу по поводу сотрудничества.",
        "привет, {name}!",
        "привет, {name}! увидел ваш проект {company}, хотел предложить короткий обмен опытом по росту продаж в telegram.",
        "{name}, возвращаюсь к письму по {company}. будет удобно созвониться на 10 минут на этой неделе?",
        "{name}, пишу финальный раз: дайте знать, если тема актуальна для {company}.",
        "{name}, возвращаюсь к вопросу выше — актуально обсудить?",
    }
)


def _render_step_text(
    step: dict,
    step_idx: int,
    rec: dict,
    *,
    campaign_name: str | None = None,
) -> str:
    tmpl = str(step.get("template_text") or "").strip()
    tmpl_low = tmpl.lower()
    csv_step_val = ""
    if step_idx == 0:
        csv_step_val = (rec.get("step1_text") or "").strip()
        has_csv_token = any(
            tok in tmpl_low
            for tok in ("{step1_text}", "{first_touch}", "{первое касание}", "{первое_касание}")
        )
    elif step_idx == 1:
        csv_step_val = (rec.get("step2_text") or "").strip()
        has_csv_token = any(
            tok in tmpl_low
            for tok in ("{step2_text}", "{followup_1}", "{фоллоуап 1}", "{фоллоуап_1}")
        )
    elif step_idx == 2:
        csv_step_val = (rec.get("step3_text") or "").strip()
        has_csv_token = any(
            tok in tmpl_low
            for tok in ("{step3_text}", "{followup_2}", "{фоллоуап 2}", "{фоллоуап_2}")
        )
    else:
        has_csv_token = False

    is_b2b_step0 = step_idx == 0 and _is_b2b_campaign_name(campaign_name)
    sanitize_now = not is_b2b_step0

    is_default_placeholder = (not tmpl) or (tmpl_low in _DEFAULT_BOILERPLATE_TEMPLATES)
    if csv_step_val and not has_csv_token and is_default_placeholder:
        base = render_template(csv_step_val, rec, sanitize_brand=sanitize_now)
    else:
        base = render_template(tmpl, rec, sanitize_brand=sanitize_now)

    offer_tail = str(step.get("offer_tail") or "").strip()
    if offer_tail:
        tail_rendered = render_template(offer_tail, rec, sanitize_brand=sanitize_now)
        if tail_rendered and tail_rendered not in base:
            base = f"{base}\n\n{tail_rendered}".strip()

    if is_b2b_step0:
        if _needs_legacy_vlad_step1_upgrade(base):
            from leadgen.outreach_copy import upgrade_to_vlad_step1

            return upgrade_to_vlad_step1(
                base,
                company=str(rec.get("company") or ""),
                name=str(rec.get("name") or ""),
                site=str(rec.get("website") or ""),
                category=str(rec.get("category") or rec.get("lead_niche") or ""),
                their_angle=str(rec.get("their_angle") or rec.get("note") or ""),
            )
        return _sanitize_legacy_brand(base)
    return base


def _normalize_steps(steps: list | None) -> list[dict]:
    if not steps:
        return [
            {
                "step_order": 1,
                "delay_hours": 0,
                "template_text": "Привет, {name}! Пишу по поводу сотрудничества.",
                "image_url": None,
            }
        ]
    normalized = []
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError("Некорректный формат шага цепочки")
        text = str(step.get("template_text") or "").strip()
        image_url = str(step.get("image_url") or "").strip() or None
        if not text and not image_url:
            raise ValueError("Укажите текст сообщения для каждого шага")
        delay_raw = step.get("delay_hours", 0 if idx == 1 else 24)
        try:
            delay_hours = max(0, int(delay_raw or 0))
        except (TypeError, ValueError):
            raise ValueError("Задержка шага должна быть числом часов") from None
        normalized.append(
            {
                "step_order": idx,
                "delay_hours": delay_hours,
                "template_text": text,
                "image_url": image_url,
            }
        )
    return normalized


def create_campaign(
    conn,
    name: str,
    account_id: int | None = None,
    daily_limit: int = 20,
    steps: list | None = None,
    account_ids: list[int] | None = None,
    status: str | None = None,
    leadgen_enabled: bool | None = None,
    leadgen_daily_quota: int | None = None,
    leadgen_sources: list[str] | None = None,
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Укажите название рассылки")
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not (1 <= int(daily_limit) <= 500):
        raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")
    initial_status = (status or "draft").strip()
    if initial_status not in CAMPAIGN_STATUSES:
        raise ValueError("Неизвестный статус рассылки")
    resolved_ids = _parse_account_ids(account_id, account_ids)
    for aid in resolved_ids:
        acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (aid,)).fetchone()
        if acc is None:
            raise ValueError("Аккаунт не найден")
    primary_account_id = resolved_ids[0] if resolved_ids else None
    account_ids_json = json.dumps(resolved_ids)

    name_low = clean_name.lower()
    if leadgen_enabled is None:
        lg_enabled_val = 1 if ("leadgen" in name_low or "ваканс" in name_low) else 0
    else:
        lg_enabled_val = 1 if bool(leadgen_enabled) else 0

    if leadgen_daily_quota is None:
        lg_quota_val = int(daily_limit)
    else:
        if isinstance(leadgen_daily_quota, bool) or not isinstance(leadgen_daily_quota, int) or not (0 <= int(leadgen_daily_quota) <= 500):
            raise ValueError("Квота лидгена должна быть от 0 до 500")
        lg_quota_val = int(leadgen_daily_quota)

    if leadgen_sources is None:
        lg_sources_list: list[str] = []
    elif isinstance(leadgen_sources, (list, tuple, set)):
        lg_sources_list = [str(s).strip() for s in leadgen_sources if str(s or "").strip()]
    else:
        raise ValueError("leadgen_sources должен быть списком строк")
    lg_sources_json = json.dumps(lg_sources_list, ensure_ascii=False)

    norm_steps = _normalize_steps(steps)
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO campaigns (
            name, account_id, account_ids_json, daily_limit, status,
            leadgen_enabled, leadgen_daily_quota, leadgen_sources_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean_name,
            primary_account_id,
            account_ids_json,
            int(daily_limit),
            initial_status,
            lg_enabled_val,
            lg_quota_val,
            lg_sources_json,
            now,
            now,
        ),
    )
    campaign_id = cur.lastrowid
    for step in norm_steps:
        conn.execute(
            """
            INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, image_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                step["step_order"],
                step["delay_hours"],
                step["template_text"],
                step.get("image_url"),
                now,
            ),
        )
    conn.commit()
    return get_campaign(conn, campaign_id)


def update_campaign(
    conn,
    campaign_id: int,
    name: str | None = None,
    account_id: int | None = None,
    daily_limit: int | None = None,
    status: str | None = None,
    steps: list | None = None,
    account_ids: list[int] | None = None,
    leadgen_enabled: bool | None = None,
    leadgen_daily_quota: int | None = None,
    leadgen_sources: list[str] | None = None,
) -> dict:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if row is None:
        raise LookupError("campaign")
    current = as_dict(row)
    new_name = current["name"] if name is None else (name or "").strip()
    if not new_name:
        raise ValueError("Укажите название рассылки")
    new_limit = current["daily_limit"] if daily_limit is None else daily_limit
    if isinstance(new_limit, bool) or not isinstance(new_limit, int) or not (1 <= int(new_limit) <= 500):
        raise ValueError("Лимит должен быть от 1 до 500 сообщений в сутки")

    if account_ids is not None:
        resolved_ids = _parse_account_ids(account_id, account_ids)
    elif account_id is not None:
        resolved_ids = _parse_account_ids(account_id, None)
    else:
        resolved_ids = _parse_account_ids(current.get("account_id"), current.get("account_ids_json"))

    for aid in resolved_ids:
        acc = conn.execute("SELECT id FROM accounts WHERE id = ?", (aid,)).fetchone()
        if acc is None:
            raise ValueError("Аккаунт не найден")
    primary_account_id = resolved_ids[0] if resolved_ids else None
    account_ids_json = json.dumps(resolved_ids)

    new_status = current["status"] if status is None else status
    if new_status not in CAMPAIGN_STATUSES:
        raise ValueError("Неизвестный статус рассылки")

    if leadgen_enabled is None:
        new_lg_enabled = int(current.get("leadgen_enabled") or 0)
    else:
        new_lg_enabled = 1 if bool(leadgen_enabled) else 0

    if leadgen_daily_quota is None:
        new_lg_quota = int(current.get("leadgen_daily_quota") if current.get("leadgen_daily_quota") is not None else 20)
    else:
        if isinstance(leadgen_daily_quota, bool) or not isinstance(leadgen_daily_quota, int) or not (0 <= int(leadgen_daily_quota) <= 500):
            raise ValueError("Квота лидгена должна быть от 0 до 500")
        new_lg_quota = int(leadgen_daily_quota)

    if leadgen_sources is None:
        new_lg_sources_json = current.get("leadgen_sources_json") or "[]"
    elif isinstance(leadgen_sources, (list, tuple, set)):
        clean_src = [str(s).strip() for s in leadgen_sources if str(s or "").strip()]
        new_lg_sources_json = json.dumps(clean_src, ensure_ascii=False)
    else:
        raise ValueError("leadgen_sources должен быть списком строк")

    now = utc_now()
    conn.execute(
        """
        UPDATE campaigns
        SET name = ?, account_id = ?, account_ids_json = ?, daily_limit = ?, status = ?,
            leadgen_enabled = ?, leadgen_daily_quota = ?, leadgen_sources_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_name,
            primary_account_id,
            account_ids_json,
            int(new_limit),
            new_status,
            new_lg_enabled,
            new_lg_quota,
            new_lg_sources_json,
            now,
            campaign_id,
        ),
    )
    if steps is not None:
        norm_steps = _normalize_steps(steps)
        conn.execute("DELETE FROM campaign_steps WHERE campaign_id = ?", (campaign_id,))
        for step in norm_steps:
            conn.execute(
                """
                INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, image_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    step["step_order"],
                    step["delay_hours"],
                    step["template_text"],
                    step.get("image_url"),
                    now,
                ),
            )
    conn.commit()
    return get_campaign(conn, campaign_id)


def _parse_hhmm(raw: str | None, default_minutes: int = 540) -> int:
    s = str(raw or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return default_minutes
    hh = min(23, max(0, int(m.group(1))))
    mm = min(59, max(0, int(m.group(2))))
    return hh * 60 + mm


def _get_kv(conn, key: str, default: str | None = None) -> str | None:
    try:
        row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return str(row["value"])
    except Exception:
        pass
    return default


def _set_kv(conn, key: str, value: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), now),
    )


def is_autopilot_enabled(conn) -> bool:
    val = _get_kv(conn, "outreach_autopilot_enabled", "1")
    return str(val).strip().lower() not in ("0", "false", "off", "no")


def evaluate_schedule_window(
    schedule: dict,
    now_dt: datetime | None = None,
    autopilot_enabled: bool = True,
    now: datetime | None = None,
) -> dict:
    effective_now = now_dt if now_dt is not None else now
    tz_name = str(schedule.get("timezone") or "UTC").strip() or "UTC"
    try:
        tz_obj = ZoneInfo(tz_name)
    except Exception:
        tz_obj = timezone.utc

    if effective_now is None:
        now_utc = datetime.now(timezone.utc)
    elif effective_now.tzinfo is None:
        now_utc = effective_now.replace(tzinfo=timezone.utc)
    else:
        now_utc = effective_now.astimezone(timezone.utc)

    local_dt = now_utc.astimezone(tz_obj)
    day_key = WEEKDAY_KEYS[local_dt.weekday()]
    days = schedule.get("days") if isinstance(schedule.get("days"), dict) else DEFAULT_SCHEDULE_DAYS
    today_cfg = days.get(day_key) if isinstance(days.get(day_key), dict) else DEFAULT_SCHEDULE_DAYS[day_key]

    enabled_today = bool(today_cfg.get("enabled", False))
    start_str = str(today_cfg.get("start") or "09:00").strip()
    end_str = str(today_cfg.get("end") or "18:00").strip()
    start_min = _parse_hhmm(start_str, 9 * 60)
    end_min = _parse_hhmm(end_str, 18 * 60)
    cur_min = local_dt.hour * 60 + local_dt.minute

    within_window = False
    if not enabled_today:
        reason = "day_disabled"
    else:
        if start_min < end_min:
            within_window = start_min <= cur_min < end_min
        else:
            within_window = cur_min >= start_min or cur_min <= end_min
        reason = "within_window" if within_window else "outside_hours"

    next_run_at: str | None = None
    if not autopilot_enabled:
        next_run_label = "Автопилот выключен"
    elif within_window:
        next_dt = now_utc + timedelta(seconds=30)
        next_run_at = next_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        next_run_label = (
            f"Активен · окно {WEEKDAY_RU.get(day_key, day_key)} {start_str}–{end_str} {tz_name} · след. проверка через 30 сек"
        )
    else:
        found_label = None
        for offset in range(0, 8):
            cand_local = local_dt + timedelta(days=offset)
            cand_key = WEEKDAY_KEYS[cand_local.weekday()]
            cand_cfg = days.get(cand_key) if isinstance(days.get(cand_key), dict) else DEFAULT_SCHEDULE_DAYS[cand_key]
            if not bool(cand_cfg.get("enabled", False)):
                continue
            cand_start = str(cand_cfg.get("start") or "09:00").strip()
            cand_start_min = _parse_hhmm(cand_start, 9 * 60)
            if offset == 0 and cur_min >= cand_start_min:
                continue
            sh, sm = divmod(cand_start_min, 60)
            target_local = cand_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
            next_run_at = target_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            found_label = f"Ожидание окна · след. запуск: {WEEKDAY_RU.get(cand_key, cand_key)} {cand_start} ({tz_name})"
            break
        next_run_label = found_label or "Нет активных дней в расписании"

    return {
        "within_window": within_window,
        "reason": reason,
        "current_day": day_key,
        "local_time": local_dt.strftime("%H:%M"),
        "timezone": tz_name,
        "window_start": start_str,
        "window_end": end_str,
        "next_run_at": next_run_at,
        "next_run_label": next_run_label,
    }


def is_within_outreach_schedule(
    schedule_or_conn,
    now_dt: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    effective_now = now_dt if now_dt is not None else now
    if isinstance(schedule_or_conn, sqlite3.Connection):
        sched = get_outreach_schedule(schedule_or_conn, now_dt=effective_now)
    else:
        sched = schedule_or_conn
    return bool(evaluate_schedule_window(sched, now_dt=effective_now)["within_window"])


def get_outreach_schedule(conn, now_dt: datetime | None = None) -> dict:
    row = conn.execute("SELECT * FROM outreach_schedule WHERE id = 1").fetchone()
    if row is None:
        now = utc_now()
        days_copy = {k: dict(v) for k, v in DEFAULT_SCHEDULE_DAYS.items()}
        conn.execute(
            """
            INSERT INTO outreach_schedule (id, timezone, days_json, updated_at)
            VALUES (1, 'UTC', ?, ?)
            """,
            (json.dumps(days_copy), now),
        )
        conn.commit()
        tz_val = "UTC"
        days_out = days_copy
        updated_at = now
    else:
        d = as_dict(row)
        try:
            parsed_days = json.loads(d.get("days_json") or "{}")
        except Exception:
            parsed_days = {}
        days_out = {}
        for day_key, default_cfg in DEFAULT_SCHEDULE_DAYS.items():
            raw_day = parsed_days.get(day_key) if isinstance(parsed_days, dict) else None
            if isinstance(raw_day, dict):
                days_out[day_key] = {
                    "enabled": bool(raw_day.get("enabled", default_cfg["enabled"])),
                    "start": str(raw_day.get("start") or default_cfg["start"]).strip(),
                    "end": str(raw_day.get("end") or default_cfg["end"]).strip(),
                }
            else:
                days_out[day_key] = dict(default_cfg)
        tz_val = d.get("timezone") or "UTC"
        updated_at = d.get("updated_at") or utc_now()

    enabled = is_autopilot_enabled(conn)
    base_sched = {"timezone": tz_val, "days": days_out}
    win = evaluate_schedule_window(base_sched, now_dt=now_dt, autopilot_enabled=enabled)
    last_tick = _get_kv(conn, "outreach_autopilot_last_tick", None)
    return {
        "timezone": tz_val,
        "days": days_out,
        "updated_at": updated_at,
        "autopilot_enabled": enabled,
        "within_window": win["within_window"],
        "next_run_at": win["next_run_at"],
        "next_run_label": win["next_run_label"],
        "last_tick_at": last_tick,
    }


def update_outreach_schedule(
    conn,
    timezone: str = "UTC",
    days: dict | None = None,
    autopilot_enabled: bool | None = None,
    timezone_name: str | None = None,
) -> dict:
    clean_tz = (timezone_name or timezone or "UTC").strip() or "UTC"
    if not isinstance(days, dict):
        raise ValueError("Некорректный формат дней расписания")
    current = get_outreach_schedule(conn)["days"]
    merged = {}
    for day_key, default_cfg in DEFAULT_SCHEDULE_DAYS.items():
        base_cfg = current.get(day_key, default_cfg)
        incoming = days.get(day_key)
        if isinstance(incoming, dict):
            merged[day_key] = {
                "enabled": bool(incoming.get("enabled", base_cfg["enabled"])),
                "start": str(incoming.get("start") or base_cfg["start"]).strip(),
                "end": str(incoming.get("end") or base_cfg["end"]).strip(),
            }
        else:
            merged[day_key] = dict(base_cfg)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO outreach_schedule (id, timezone, days_json, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            timezone = excluded.timezone,
            days_json = excluded.days_json,
            updated_at = excluded.updated_at
        """,
        (clean_tz, json.dumps(merged), now),
    )
    if autopilot_enabled is not None:
        _set_kv(conn, "outreach_autopilot_enabled", "1" if autopilot_enabled else "0")
    conn.commit()
    return get_outreach_schedule(conn)


def get_autopilot_status(conn, now_dt: datetime | None = None) -> dict:
    sched = get_outreach_schedule(conn, now_dt=now_dt)
    running_rows = conn.execute(
        "SELECT id, name, daily_limit FROM campaigns WHERE status = 'running' ORDER BY id"
    ).fetchall()
    return {
        "enabled": sched["autopilot_enabled"],
        "autopilot_enabled": sched["autopilot_enabled"],
        "within_window": sched["within_window"],
        "timezone": sched["timezone"],
        "next_run_at": sched["next_run_at"],
        "next_run_label": sched["next_run_label"],
        "last_tick_at": sched["last_tick_at"],
        "running_campaigns_count": len(running_rows),
        "running_campaign_ids": [int(r["id"]) for r in running_rows],
        "schedule": sched,
    }


def set_autopilot_enabled(
    conn,
    enabled: bool,
    activate_drafts: bool = False,
    now_dt: datetime | None = None,
) -> dict:
    _set_kv(conn, "outreach_autopilot_enabled", "1" if enabled else "0")
    if enabled and activate_drafts:
        now = utc_now()
        conn.execute(
            """
            UPDATE campaigns
            SET status = 'running', updated_at = ?
            WHERE status IN ('draft', 'paused')
              AND EXISTS (
                  SELECT 1 FROM campaign_recipients r
                  WHERE r.campaign_id = campaigns.id
                    AND r.status IN ('pending', 'in_progress')
              )
            """,
            (now,),
        )
    conn.commit()
    return get_autopilot_status(conn, now_dt=now_dt)



def _detect_delimiter(sample: str) -> str:
    first_line = ""
    for line in sample.splitlines():
        if line.strip():
            first_line = line
            break
    if "\t" in first_line:
        return "\t"
    if ";" in first_line and first_line.count(";") >= first_line.count(","):
        return ";"
    return ","


def _build_col_map(
    headers: list[str], column_mapping: dict | None = None
) -> dict[int, str]:
    header_lower = [c.lower().strip().lstrip("\ufeff") for c in headers]
    col_map: dict[int, str] = {}
    for idx, col in enumerate(header_lower):
        alias = HEADER_ALIASES.get(col)
        if alias:
            col_map[idx] = alias

    if isinstance(column_mapping, dict) and column_mapping:
        header_by_lower = {c.lower().strip().lstrip("\ufeff"): idx for idx, c in enumerate(headers)}
        for k, v in column_mapping.items():
            k_str = str(k or "").strip()
            v_str = str(v or "").strip()
            if not k_str or not v_str:
                continue
            if k_str in TARGET_CSV_FIELDS:
                target_field = k_str
                col_ref = v_str
            elif v_str in TARGET_CSV_FIELDS:
                target_field = v_str
                col_ref = k_str
            else:
                continue

            matched_idx = header_by_lower.get(col_ref.lower().lstrip("\ufeff"))
            if matched_idx is None and col_ref.isdigit():
                idx_candidate = int(col_ref)
                if 0 <= idx_candidate < len(headers):
                    matched_idx = idx_candidate
            if matched_idx is not None:
                for old_idx, old_field in list(col_map.items()):
                    if old_field == target_field:
                        col_map.pop(old_idx, None)
                col_map[matched_idx] = target_field
    return col_map


def _looks_like_data_cell(val: str) -> bool:
    v = (val or "").strip()
    if not v:
        return False
    if v.startswith("@") or "t.me/" in v.lower():
        return True
    if re.fullmatch(r"\+?\d[\d\s\-()]{6,18}", v):
        return True
    return False


def preview_campaign_csv(csv_text: str) -> dict:
    raw = (csv_text or "").strip()
    if not raw:
        raise ValueError("Вставьте строки CSV или выберите файл с контактами")
    delimiter = _detect_delimiter(raw)
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("В таблице CSV нет строк с контактами")

    first_clean = [c.strip().lstrip("\ufeff") for c in rows[0]]
    first_lower = [c.lower() for c in first_clean]
    is_header = any(col in HEADER_ALIASES for col in first_lower) or (
        len(rows) >= 2 and not any(_looks_like_data_cell(c) for c in first_clean)
    )

    if is_header:
        headers = first_clean
        data_rows = rows[1:]
        col_map = _build_col_map(headers)
    else:
        max_cols = max(len(r) for r in rows)
        headers = [f"Колонка {i + 1}" for i in range(max_cols)]
        data_rows = rows
        default_fields = ["name", "username", "phone", "company", "note"]
        col_map = {i: default_fields[i] for i in range(min(len(headers), len(default_fields)))}

    detected_mapping: dict[str, str] = {}
    for idx, field in col_map.items():
        if idx < len(headers) and field not in detected_mapping:
            detected_mapping[field] = headers[idx]

    sample_rows = []
    for r in data_rows[:5]:
        row_dict = {}
        for idx, h in enumerate(headers):
            row_dict[h] = r[idx] if idx < len(r) else ""
        sample_rows.append(row_dict)

    return {
        "headers": headers,
        "sample_rows": sample_rows,
        "detected_mapping": detected_mapping,
        "total_rows": len(data_rows),
    }


def _parse_csv_rows(csv_text: str, column_mapping: dict | None = None) -> list[dict]:
    raw = (csv_text or "").strip()
    if not raw:
        raise ValueError("Вставьте строки CSV или выберите файл с контактами")
    delimiter = _detect_delimiter(raw)
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("В таблице CSV нет строк с контактами")

    first_clean = [c.strip().lstrip("\ufeff") for c in rows[0]]
    first_lower = [c.lower() for c in first_clean]
    has_explicit_mapping = bool(isinstance(column_mapping, dict) and column_mapping)
    is_header = (
        any(col in HEADER_ALIASES for col in first_lower)
        or has_explicit_mapping
        or (len(rows) >= 2 and not any(_looks_like_data_cell(c) for c in first_clean))
    )

    parsed = []
    if is_header:
        headers = first_clean
        col_map = _build_col_map(rows[0], column_mapping=column_mapping)
        data_rows = rows[1:]
        for r in data_rows:
            item: dict = {}
            extra_fields: dict[str, str] = {}
            for idx, h_name in enumerate(headers):
                val = r[idx] if idx < len(r) else ""
                if h_name:
                    extra_fields[h_name] = val
                field = col_map.get(idx)
                if field and val:
                    item[field] = val
            if not any(item.get(k) for k in ("username", "name", "phone", "company")):
                # Fallback auto-detection when CSV uses custom column names
                for idx, val in enumerate(r):
                    v = (val or "").strip()
                    if not v:
                        continue
                    if (v.startswith("@") or "t.me/" in v.lower()) and not item.get("username"):
                        item["username"] = v
                    elif re.fullmatch(r"\+?\d[\d\s\-()]{6,18}", v) and not item.get("phone"):
                        item["phone"] = v
                    elif not item.get("name") and idx <= 2:
                        item["name"] = v
            if item or any(extra_fields.values()):
                item["extra_fields"] = extra_fields
                item["_headers"] = headers
                parsed.append(item)
    else:
        for r in rows:
            c0 = r[0] if len(r) > 0 else ""
            c1 = r[1] if len(r) > 1 else ""
            c2 = r[2] if len(r) > 2 else ""
            c3 = r[3] if len(r) > 3 else ""
            c4 = r[4] if len(r) > 4 else ""
            if c0.startswith("@") or "t.me/" in c0.lower() or (
                re.fullmatch(r"[A-Za-z0-9_]{3,32}", c0) and (" " in c1 or re.search(r"[А-Яа-яЁё]", c1))
            ):
                username, name = c0, c1
            else:
                name, username = c0, c1
            parsed.append(
                {
                    "name": name,
                    "username": username,
                    "phone": c2,
                    "company": c3,
                    "note": c4,
                }
            )
    return parsed


def _account_provider(conn, account_id: int | None) -> str:
    if account_id is None:
        return "telegram"
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
    if row is None:
        return "telegram"
    prov = (as_dict(row).get("provider") or "telegram").strip().lower()
    return prov if prov in ("telegram", "whatsapp") else "telegram"


def _resolve_or_create_lead(conn, account_id: int | None, item: dict, now: str) -> tuple[int, int]:
    provider = str(item.get("provider") or _account_provider(conn, account_id) or "telegram").strip().lower()
    if provider not in ("telegram", "whatsapp"):
        provider = "telegram"
    username = _clean_handle(item.get("username"))
    company = _clean_company_title(item.get("company"))
    name = (item.get("name") or "").strip() or None
    phone = (item.get("phone") or "").strip() or None
    note = (item.get("note") or "").strip() or None
    their_angle = (item.get("their_angle") or "").strip() or None
    website_raw = (item.get("website") or "").strip()
    url_val = None
    if website_raw:
        if re.match(r"^https?://", website_raw, flags=re.IGNORECASE):
            url_val = website_raw
        else:
            url_val = f"https://{website_raw}"

    tg_id_raw = item.get("telegram_user_id")
    tg_id = None
    if tg_id_raw is not None and str(tg_id_raw).strip():
        try:
            tg_id = int(str(tg_id_raw).strip())
        except ValueError:
            tg_id = None
    if tg_id is None and provider == "whatsapp" and phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 7:
            tg_id = int(digits[-12:])

    lead_row = None
    if tg_id is not None:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE telegram_user_id = ? ORDER BY id LIMIT 1",
            (tg_id,),
        ).fetchone()
    if lead_row is None and username:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE lower(username) = lower(?) ORDER BY id LIMIT 1",
            (username,),
        ).fetchone()
    if lead_row is None and phone:
        lead_row = conn.execute(
            "SELECT * FROM leads WHERE phone = ? ORDER BY id LIMIT 1",
            (phone,),
        ).fetchone()

    combined_note = note or their_angle or (f"Компания: {company}" if company else None)
    display_name = name or company or (f"{username} · {company}" if username and company else username) or phone

    if lead_row is not None:
        lead_id = int(lead_row["id"])
        existing_tg_id = lead_row["telegram_user_id"]
        if existing_tg_id is None:
            existing_tg_id = tg_id if tg_id is not None else (900000 + lead_id)
            try:
                conn.execute(
                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                    (existing_tg_id, now, lead_id),
                )
            except sqlite3.IntegrityError:
                pass
        if company:
            conn.execute(
                "UPDATE leads SET company = COALESCE(company, ?), description = COALESCE(description, ?), updated_at = ? WHERE id = ?",
                (company, company, now, lead_id),
            )
        if phone and not lead_row["phone"]:
            conn.execute("UPDATE leads SET phone = ?, updated_at = ? WHERE id = ?", (phone, now, lead_id))
        if url_val and not lead_row["url"]:
            conn.execute("UPDATE leads SET url = ?, updated_at = ? WHERE id = ?", (url_val, now, lead_id))
        if combined_note and not lead_row["note"]:
            conn.execute("UPDATE leads SET note = ?, updated_at = ? WHERE id = ?", (combined_note, now, lead_id))
        return lead_id, int(existing_tg_id)

    if tg_id is None:
        import zlib

        seed_str = str(username or phone or display_name or now).lower()
        tg_id = 900000 + (zlib.crc32(seed_str.encode("utf-8")) % 89999) + 10000
    return None, int(tg_id)


def import_campaign_csv(
    conn,
    campaign_id: int,
    csv_text: str,
    column_mapping: dict | None = None,
) -> dict:
    camp_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if camp_row is None:
        raise LookupError("campaign")
    camp = as_dict(camp_row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    account_id = account_ids[0] if account_ids else camp.get("account_id")

    contacts = _parse_csv_rows(csv_text, column_mapping=column_mapping)
    existing_rows = conn.execute(
        "SELECT username, phone, lead_id, telegram_user_id FROM campaign_recipients WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    seen_usernames = {
        str(r["username"]).lower() for r in existing_rows if r["username"]
    }
    seen_phones = {str(r["phone"]) for r in existing_rows if r["phone"]}
    seen_leads = {int(r["lead_id"]) for r in existing_rows if r["lead_id"]}

    now = utc_now()
    imported = 0
    has_step2 = any((r.get("step2_text") or "").strip() for r in contacts)
    has_step3 = any((r.get("step3_text") or "").strip() for r in contacts)
    detected_headers: list[str] = []
    for item in contacts:
        if not detected_headers and item.get("_headers"):
            detected_headers = [str(h) for h in item["_headers"] if str(h).strip()]
        username = _clean_handle(item.get("username"))
        company = _clean_company_title(item.get("company"))
        explicit_name = (item.get("name") or "").strip() or None
        name = explicit_name or company or username
        phone = (item.get("phone") or "").strip() or None
        website = (item.get("website") or "").strip() or None
        category = (item.get("category") or "").strip() or None
        their_angle = (item.get("their_angle") or "").strip() or None
        our_angle = (item.get("our_angle") or "").strip() or None
        hook = (item.get("hook") or "").strip() or None
        note = (item.get("note") or "").strip() or their_angle
        step1_text, error_reason = _clean_step1_text(
            item.get("step1_text"),
            company,
            is_b2b=_is_b2b_campaign_name(camp.get("name")),
        )
        step2_text = _sanitize_legacy_brand(item.get("step2_text")).strip() or None
        step3_text = _sanitize_legacy_brand(item.get("step3_text")).strip() or None
        if step2_text:
            has_step2 = True
        if step3_text:
            has_step3 = True

        if not any((username, name, phone, item.get("telegram_user_id"))):
            continue
        if username and username.lower() in seen_usernames:
            continue
        if phone and phone in seen_phones:
            continue

        lead_item = dict(item)
        lead_item["company"] = company
        lead_item["name"] = name
        lead_id, tg_id = _resolve_or_create_lead(conn, account_id, lead_item, now)
        if lead_id is not None and lead_id in seen_leads:
            continue

        raw_extra = item.get("extra_fields")
        extra_fields = dict(raw_extra) if isinstance(raw_extra, dict) else {}
        mapped_audience = (item.get("audience") or "").strip()
        if mapped_audience:
            extra_fields["audience"] = mapped_audience
        extra_json = (
            json.dumps(extra_fields, ensure_ascii=False)
            if extra_fields
            else None
        )
        rec_status = "failed" if error_reason else "pending"
        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                phone, company, note, website, category, their_angle,
                our_angle, hook, step1_text, step2_text, step3_text,
                extra_json, current_step, status, last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?)
            """,
            (
                campaign_id,
                lead_id,
                tg_id,
                username,
                name or username or phone,
                phone,
                company,
                note,
                website,
                category,
                their_angle,
                our_angle,
                hook,
                step1_text,
                step2_text,
                step3_text,
                extra_json,
                rec_status,
                error_reason,
                now,
                now,
            ),
        )
        if username:
            seen_usernames.add(username.lower())
        if phone:
            seen_phones.add(phone)
        if lead_id is not None:
            seen_leads.add(lead_id)
        imported += 1

    if has_step2 or has_step3:
        existing_steps = conn.execute(
            "SELECT step_order FROM campaign_steps WHERE campaign_id = ? ORDER BY step_order",
            (campaign_id,),
        ).fetchall()
        existing_orders = {int(s["step_order"]) for s in existing_steps}
        if existing_orders:
            if 2 not in existing_orders:
                conn.execute(
                    """
                    INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
                    VALUES (?, 2, 24, '{followup_1}', ?)
                    """,
                    (campaign_id, now),
                )
            if has_step3 and 3 not in existing_orders:
                conn.execute(
                    """
                    INSERT INTO campaign_steps (campaign_id, step_order, delay_hours, template_text, created_at)
                    VALUES (?, 3, 72, '{followup_2}', ?)
                    """,
                    (campaign_id, now),
                )

    if detected_headers:
        conn.execute(
            "UPDATE campaigns SET csv_headers_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(detected_headers, ensure_ascii=False), now, campaign_id),
        )
    else:
        conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, campaign_id))
    conn.commit()
    updated_campaign = get_campaign(conn, campaign_id)
    return {
        "imported": imported,
        "total_recipients": updated_campaign["total_recipients"],
        "campaign": updated_campaign,
    }


def enroll_leads_from_crm(
    conn,
    campaign_id: int,
    lead_ids: list[int] | None = None,
    status: str | None = None,
    folder_id: int | None = None,
) -> dict:
    camp_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if camp_row is None:
        raise LookupError("campaign")
    camp = as_dict(camp_row)
    account_ids = _parse_account_ids(camp.get("account_id"), camp.get("account_ids_json"))
    camp_providers = {_account_provider(conn, aid) for aid in account_ids if aid}
    primary_provider = next(iter(camp_providers)) if len(camp_providers) == 1 else None

    sql = "SELECT * FROM leads"
    clauses = []
    params: list = []
    if lead_ids is not None and len(lead_ids) > 0:
        clean_ids = [int(x) for x in lead_ids if x is not None and not isinstance(x, bool)]
        if not clean_ids:
            raise ValueError("Выберите хотя бы одного лида")
        ph = ",".join("?" for _ in clean_ids)
        clauses.append(f"id IN ({ph})")
        params.extend(clean_ids)
    else:
        if status and str(status).strip():
            clauses.append("status = ?")
            params.append(str(status).strip())
        if folder_id is not None:
            clauses.append("folder_id = ?")
            params.append(int(folder_id))
        if primary_provider is not None:
            clauses.append("COALESCE(provider, 'telegram') = ?")
            params.append(primary_provider)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"

    lead_rows = [as_dict(r) for r in conn.execute(sql, params).fetchall()]

    existing_rows = conn.execute(
        "SELECT username, phone, lead_id, telegram_user_id FROM campaign_recipients WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchall()
    seen_usernames = {str(r["username"]).lower() for r in existing_rows if r["username"]}
    seen_phones = {str(r["phone"]) for r in existing_rows if r["phone"]}
    seen_leads = {int(r["lead_id"]) for r in existing_rows if r["lead_id"]}
    seen_tg_ids = {int(r["telegram_user_id"]) for r in existing_rows if r["telegram_user_id"]}

    now = utc_now()
    imported_count = 0
    skipped_count = 0

    for lead in lead_rows:
        lid = int(lead["id"])
        lead_provider = (lead.get("provider") or primary_provider or "telegram").strip().lower()
        username = _clean_handle(lead.get("username"))
        phone = (lead.get("phone") or "").strip() or None
        tg_id = lead.get("telegram_user_id")
        name = (lead.get("name") or "").strip() or None

        if not username or not tg_id or not phone:
            dlg = conn.execute(
                "SELECT * FROM dialogs WHERE lead_id = ? ORDER BY id LIMIT 1",
                (lid,),
            ).fetchone()
            if dlg is not None:
                dlg_dict = as_dict(dlg)
                if not username and dlg_dict.get("username"):
                    username = _clean_handle(dlg_dict["username"])
                if not tg_id and dlg_dict.get("peer_id"):
                    tg_id = int(dlg_dict["peer_id"])
                if not phone and dlg_dict.get("phone"):
                    phone = str(dlg_dict["phone"]).strip()
                if not name and dlg_dict.get("title"):
                    name = str(dlg_dict["title"]).strip()

        if tg_id is None and lead_provider == "whatsapp" and phone:
            digits = re.sub(r"\D", "", phone)
            if len(digits) >= 7:
                tg_id = int(digits[-12:])

        if tg_id is None:
            tg_id = 900000 + lid
            try:
                conn.execute(
                    "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                    (tg_id, now, lid),
                )
            except sqlite3.IntegrityError:
                pass

        if (
            lid in seen_leads
            or (tg_id and int(tg_id) in seen_tg_ids)
            or (username and username.lower() in seen_usernames)
            or (phone and phone in seen_phones)
        ):
            skipped_count += 1
            continue

        company = (lead.get("company") or lead.get("description") or "").strip() or None
        if name and "·" in name and not company:
            parts = [p.strip() for p in name.split("·", 1)]
            if len(parts) == 2 and parts[1]:
                company = parts[1]
        if company and (not lead.get("company") or not lead.get("description")):
            conn.execute(
                "UPDATE leads SET company = COALESCE(company, ?), description = COALESCE(description, ?), updated_at = ? WHERE id = ?",
                (company, company, now, lid),
            )
        note = (lead.get("note") or "").strip() or None
        website = (lead.get("url") or "").strip() or None
        category = (lead.get("niche") or "").strip() or None

        conn.execute(
            """
            INSERT INTO campaign_recipients (
                campaign_id, lead_id, telegram_user_id, username, name,
                phone, company, note, website, category, current_step, status,
                last_sent_at, error_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', NULL, NULL, ?, ?)
            """,
            (
                campaign_id,
                lid,
                int(tg_id),
                username,
                name or username or phone or f"Лид #{lid}",
                phone,
                company,
                note,
                website,
                category,
                now,
                now,
            ),
        )
        seen_leads.add(lid)
        if tg_id:
            seen_tg_ids.add(int(tg_id))
        if username:
            seen_usernames.add(username.lower())
        if phone:
            seen_phones.add(phone)
        imported_count += 1

    conn.execute("UPDATE campaigns SET updated_at = ? WHERE id = ?", (now, campaign_id))
    conn.commit()
    updated_campaign = get_campaign(conn, campaign_id)
    return {
        "imported_count": imported_count,
        "imported": imported_count,
        "skipped_count": skipped_count,
        "total_recipients": updated_campaign["total_recipients"],
        "campaign": updated_campaign,
    }


def _ensure_recipient_dialog(
    conn, account_id: int, rec: dict, now: str, provider: str | None = None
) -> dict:
    resolved_provider = (provider or _account_provider(conn, account_id) or "telegram").strip().lower()
    phone = (rec.get("phone") or "").strip() or None
    phone_digits = re.sub(r"\D", "", phone or "")
    if resolved_provider == "whatsapp":
        if phone_digits and len(phone_digits) >= 7:
            peer_id = int(phone_digits[-12:])
            if not phone.startswith("+"):
                phone = f"+{phone_digits}"
        elif rec.get("telegram_user_id"):
            peer_id = int(rec["telegram_user_id"])
        else:
            peer_id = 900000 + int(rec.get("lead_id") or rec["id"])
    else:
        peer_id = int(rec.get("telegram_user_id") or (900000 + int(rec.get("lead_id") or rec["id"])))

    username = _clean_handle(rec.get("username"))
    title = (rec.get("name") or "").strip()
    if rec.get("company") and rec["company"] not in title:
        title = f"{title} · {rec['company']}" if title else rec["company"]
    if not title:
        title = username or phone or str(peer_id)
    lead_id = rec.get("lead_id")

    conn.execute(
        """
        INSERT INTO dialogs (
            account_id, peer_type, peer_id, username, title, phone, provider,
            is_group, unread_count, last_from_peer, muted, lead_id,
            created_at, updated_at
        ) VALUES (?, 'user', ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?, ?)
        ON CONFLICT(account_id, peer_type, peer_id) DO UPDATE SET
            username = COALESCE(excluded.username, dialogs.username),
            title = CASE WHEN dialogs.title IS NULL OR dialogs.title = '' THEN excluded.title ELSE dialogs.title END,
            phone = COALESCE(excluded.phone, dialogs.phone),
            provider = COALESCE(excluded.provider, dialogs.provider),
            lead_id = COALESCE(dialogs.lead_id, excluded.lead_id),
            updated_at = excluded.updated_at
        """,
        (account_id, peer_id, username, title, phone, resolved_provider, lead_id, now, now),
    )
    row = conn.execute(
        "SELECT * FROM dialogs WHERE account_id = ? AND peer_type = 'user' AND peer_id = ?",
        (account_id, peer_id),
    ).fetchone()
    dlg = as_dict(row)
    if lead_id and dlg:
        conn.execute(
            "UPDATE leads SET dialog_id = ? WHERE id = ? AND dialog_id IS NULL",
            (dlg["id"], lead_id),
        )
    return dlg


async def prewarm_recipient_stories(
    client, peer_or_username: Any, reaction_emoticon: str = "🔥"
) -> dict:
    if client is None:
        return {"prewarmed": False, "stories_count": 0, "error": "client is None"}
    try:
        from telethon.tl import functions, types

        target_peer = peer_or_username
        if hasattr(client, "get_input_entity"):
            try:
                resolved = client.get_input_entity(peer_or_username)
                if asyncio.iscoroutine(resolved):
                    resolved = await resolved
                if resolved is not None:
                    target_peer = resolved
            except Exception:
                target_peer = peer_or_username

        res = await client(functions.stories.GetPeerStoriesRequest(peer=target_peer))
        stories_obj = getattr(res, "stories", None)
        if isinstance(stories_obj, (list, tuple)):
            stories_list = list(stories_obj)
        elif stories_obj is not None and hasattr(stories_obj, "stories"):
            stories_list = list(getattr(stories_obj, "stories") or [])
        elif isinstance(res, dict):
            raw_s = res.get("stories")
            stories_list = list(raw_s.get("stories") if isinstance(raw_s, dict) else (raw_s or []))
        else:
            stories_list = []

        valid_ids = []
        for s in stories_list:
            sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", None)
            if sid is not None:
                try:
                    valid_ids.append(int(sid))
                except (TypeError, ValueError):
                    pass

        if not valid_ids:
            return {"prewarmed": False, "stories_count": 0, "error": "no_active_stories"}

        last_id = max(valid_ids)
        await client(functions.stories.ReadStoriesRequest(peer=target_peer, max_id=last_id))
        await client(
            functions.stories.SendReactionRequest(
                peer=target_peer,
                story_id=last_id,
                reaction=types.ReactionEmoji(emoticon=reaction_emoticon),
            )
        )
        return {
            "prewarmed": True,
            "stories_count": len(valid_ids),
            "story_id": last_id,
        }
    except Exception as e:
        return {"prewarmed": False, "stories_count": 0, "error": str(e)}


async def _resolve_telethon_client_for_prewarm(gateway, session_path: str):
    if gateway is None:
        return None
    if hasattr(gateway, "client") and gateway.client is not None:
        return gateway.client
    if hasattr(gateway, "_clients") and isinstance(gateway._clients, dict):
        cached = gateway._clients.get(session_path)
        if cached is not None:
            return cached
    if hasattr(gateway, "_open") and callable(gateway._open):
        try:
            opened = await gateway._open(session_path)
            if isinstance(opened, tuple) and len(opened) >= 1:
                return opened[0]
            return opened
        except Exception:
            return None
    if callable(gateway):
        return gateway
    return None


_CLOUD_BACKUP_BG_TASK: asyncio.Task | None = None
_CLOUD_BACKUP_RERUN: bool = False


def _mark_cloud_persistence_dirty(conn, now_iso: str | None = None) -> None:
    """Commit checkpoint timestamp to kv_settings and schedule non-blocking cloud backup if R2 is configured."""
    global _CLOUD_BACKUP_BG_TASK, _CLOUD_BACKUP_RERUN
    ts = now_iso or utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('cloud_persistence_dirty_at', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (ts, ts),
    )
    conn.commit()
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("BOT_TOKEN") == "e2e-not-a-secret"
    ):
        return
    try:
        from app.cloud_persistence import DEFAULT_DB, _resolve_r2_config, backup_to_cloud

        if not _resolve_r2_config(conn):
            return
        db_file = DEFAULT_DB
        try:
            db_rows = conn.execute("PRAGMA database_list").fetchall()
            for r in db_rows:
                if str(r[1] or "") == "main" and str(r[2] or "").strip():
                    db_file = Path(str(r[2]))
                    break
        except Exception:
            pass

        loop = asyncio.get_running_loop()
        if _CLOUD_BACKUP_BG_TASK is not None and not _CLOUD_BACKUP_BG_TASK.done():
            _CLOUD_BACKUP_RERUN = True
            return

        async def _flush_worker(target_db: Path) -> None:
            global _CLOUD_BACKUP_RERUN
            while True:
                _CLOUD_BACKUP_RERUN = False
                await asyncio.sleep(0.5)
                try:
                    await asyncio.to_thread(backup_to_cloud, None, target_db)
                except Exception:
                    pass
                if not _CLOUD_BACKUP_RERUN:
                    break

        _CLOUD_BACKUP_BG_TASK = loop.create_task(_flush_worker(db_file))
    except Exception:
        pass


def _lookup_prior_outgoing_account(
    conn,
    *,
    clean_uname: str,
    real_peer_id: int | None,
    clean_phone: str,
    lead_id_val: int | None,
    required_account_id: int | None = None,
) -> int | None:
    """Return d.account_id of the most recent outgoing message to this recipient (or verify required_account_id)."""
    peer_str = str(real_peer_id) if real_peer_id is not None else ""
    if not (clean_uname or peer_str or clean_phone or lead_id_val is not None):
        return None
    if required_account_id is not None:
        row = conn.execute(
            """
            SELECT d.account_id
            FROM dialogs d
            JOIN messages m ON m.dialog_id = d.id
            WHERE d.account_id = ?
              AND m.direction = 'out'
              AND (
                  (? != '' AND lower(trim(coalesce(d.username, ''), '@')) = lower(?))
                  OR (? != '' AND d.peer_id = ?)
                  OR (? != '' AND coalesce(d.phone, '') = ?)
                  OR (? IS NOT NULL AND d.lead_id = ?)
              )
            ORDER BY m.sent_at DESC, m.id DESC
            LIMIT 1
            """,
            (
                int(required_account_id),
                clean_uname,
                clean_uname,
                peer_str,
                real_peer_id,
                clean_phone,
                clean_phone,
                lead_id_val,
                lead_id_val,
            ),
        ).fetchone()
        return int(row["account_id"]) if row is not None and row["account_id"] is not None else None

    row = conn.execute(
        """
        SELECT d.account_id
        FROM dialogs d
        JOIN messages m ON m.dialog_id = d.id
        WHERE m.direction = 'out'
          AND (
              (? != '' AND lower(trim(coalesce(d.username, ''), '@')) = lower(?))
              OR (? != '' AND d.peer_id = ?)
              OR (? != '' AND coalesce(d.phone, '') = ?)
              OR (? IS NOT NULL AND d.lead_id = ?)
          )
        ORDER BY m.sent_at DESC, m.id DESC
        LIMIT 1
        """,
        (
            clean_uname,
            clean_uname,
            peer_str,
            real_peer_id,
            clean_phone,
            clean_phone,
            lead_id_val,
            lead_id_val,
        ),
    ).fetchone()
    return int(row["account_id"]) if row is not None and row["account_id"] is not None else None


async def run_campaign_batch(
    conn,
    campaign_id: int,
    gateway=None,
    max_send: int | None = None,
    respect_delays: bool = False,
    wa_gateway=None,
    jitter_range: tuple[float, float] | None = None,
) -> dict:
    camp = get_campaign(conn, campaign_id)
    account_ids = camp.get("account_ids") or []
    if not account_ids:
        raise ValueError("Выберите хотя бы один подключённый аккаунт для рассылки")

    connected_states = []
    for order_idx, aid in enumerate(account_ids):
        try:
            acc_raw = load_account(conn, int(aid))
        except LookupError:
            continue
        if acc_raw.get("status") != "connected":
            continue
        health_val = str(acc_raw.get("health") or "active")
        is_spambot_warm_only = (
            health_val == "frozen"
            and "@spambot" in str(acc_raw.get("last_error") or "").lower()
        )
        if health_val in ("unauthorized", "banned") or (
            health_val == "frozen" and not is_spambot_warm_only
        ):
            continue
        acc_pub = public_account(acc_raw)
        connected_states.append(
            {
                "id": int(acc_raw["id"]),
                "order": order_idx,
                "raw": acc_raw,
                "daily_limit": int(acc_pub["daily_limit"]),
                "sent_today": int(acc_pub["sent_today"]),
                "sent_in_batch": 0,
                "disabled": False,
                "spambot_warm_only": is_spambot_warm_only,
            }
        )

    if not connected_states:
        raise ValueError("Выберите хотя бы один подключённый аккаунт для рассылки")

    steps = camp.get("steps") or []
    if not steps:
        raise ValueError("Добавьте хотя бы один шаг сообщения в рассылку")

    total_account_remaining = sum(
        max(0, st["daily_limit"] - st["sent_today"]) for st in connected_states
    )
    if total_account_remaining <= 0:
        first_limit = connected_states[0]["daily_limit"]
        raise ValueError(
            f"Достигнут суточный лимит аккаунта ({first_limit} сообщ./сутки)"
        )

    campaign_daily_limit = int(camp.get("daily_limit") or 20)
    campaign_sent_today = int(camp.get("sent_today") or 0)
    campaign_remaining = max(0, campaign_daily_limit - campaign_sent_today)
    if campaign_remaining <= 0:
        raise ValueError(
            f"Достигнут суточный лимит рассылки ({campaign_daily_limit} сообщ./сутки)"
        )

    cap = int(max_send) if max_send is not None and int(max_send) > 0 else 500
    effective_quota = min(total_account_remaining, campaign_remaining, cap)
    max_failed_in_batch = max(5, effective_quota * 3)

    eligible_rows = conn.execute(
        """
        SELECT cr.*, l.niche AS lead_niche
        FROM campaign_recipients cr
        LEFT JOIN leads l ON l.id = cr.lead_id
        WHERE cr.campaign_id = ?
          AND cr.status IN ('pending', 'in_progress')
          AND cr.current_step < ?
        ORDER BY cr.id
        """,
        (campaign_id, len(steps)),
    ).fetchall()

    sent_count = 0
    failed_in_batch = 0
    sent_by_account: dict[str, int] = {}
    jitter_delays_ms: list[int] = []
    state_by_id = {st["id"]: st for st in connected_states}

    async def _tg_send_with_username_fallback(
        active_gw,
        session_path: str,
        peer_target,
        rendered_text: str,
        step_image_url: str | None,
        username: str | None,
    ) -> dict:
        async def _do_send(target_val):
            if step_image_url:
                try:
                    return await active_gw.send_message(
                        session_path,
                        target_val,
                        rendered_text,
                        None,
                        image_path=step_image_url,
                    )
                except TypeError:
                    return await active_gw.send_message(
                        session_path, target_val, rendered_text, None
                    )
            return await active_gw.send_message(
                session_path, target_val, rendered_text, None
            )

        try:
            return await _do_send(peer_target)
        except Exception as send_exc:
            err_blob = str(send_exc or "").casefold()
            clean_uname = str(username or "").strip().lstrip("@")
            if (
                clean_uname
                and ("could not find the input entity" in err_blob or "peeruser" in err_blob)
            ):
                if hasattr(active_gw, "search_dialogs"):
                    try:
                        await active_gw.search_dialogs(session_path, f"@{clean_uname}", limit=1)
                    except Exception:
                        pass
                return await _do_send(f"@{clean_uname}")
            raise

    is_b2b_camp = _is_b2b_campaign_name(camp.get("name"))
    if is_b2b_camp:
        dedup_scope_cids = [
            int(r["id"])
            for r in conn.execute("SELECT id, name FROM campaigns").fetchall()
            if _is_b2b_campaign_name(r["name"])
        ]
    else:
        dedup_scope_cids = [int(campaign_id)]

    for row in eligible_rows:
        if sent_count >= effective_quota or failed_in_batch >= max_failed_in_batch:
            break
        rec = as_dict(row)
        step_idx = int(rec.get("current_step") or 0)
        if step_idx >= len(steps):
            continue

        step = steps[step_idx]
        now = utc_now()
        if respect_delays and step_idx >= 1 and rec.get("last_sent_at"):
            delay_hours = int(step.get("delay_hours") or 0)
            if delay_hours > 0:
                due_iso = _add_hours_iso(rec["last_sent_at"], delay_hours)
                if due_iso and due_iso > now:
                    continue

        clean_rec_uname = (_clean_handle(rec.get("username")) or "").lower()
        clean_rec_phone = str(rec.get("phone") or "").strip()
        raw_rec_tg_id = rec.get("telegram_user_id")
        real_rec_peer_id = (
            int(raw_rec_tg_id)
            if raw_rec_tg_id is not None and not _is_synthetic_peer_id(raw_rec_tg_id)
            else None
        )
        rec_peer_str = str(real_rec_peer_id) if real_rec_peer_id is not None else ""
        rec_lead_id = int(rec["lead_id"]) if rec.get("lead_id") is not None else None

        rendered_text = _render_step_text(
            step, step_idx, rec, campaign_name=camp.get("name")
        )
        step_image_url = (step.get("image_url") or "").strip() or None

        if step_idx == 0:
            # Guard 3a: Anti-duplicate across campaign_recipients (username, real telegram_user_id, lead_id, or normalized phone)
            has_already_contacted_dup = False
            norm_rec_phone_digits = re.sub(r"\D+", "", clean_rec_phone)
            if dedup_scope_cids and (
                clean_rec_uname
                or norm_rec_phone_digits
                or real_rec_peer_id is not None
                or rec_lead_id is not None
            ):
                ph_scope = ",".join("?" for _ in dedup_scope_cids)
                cand_rows = conn.execute(
                    f"""
                    SELECT id, username, phone, telegram_user_id, lead_id
                    FROM campaign_recipients
                    WHERE id != ?
                      AND campaign_id IN ({ph_scope})
                      AND (
                          COALESCE(current_step, 0) >= 1
                          OR status IN ('in_progress', 'completed', 'replied')
                      )
                    """,
                    (
                        int(rec["id"]),
                        *dedup_scope_cids,
                    ),
                ).fetchall()
                for crow in cand_rows:
                    other_u = (_clean_handle(crow["username"]) or "").lower()
                    other_p_digits = re.sub(r"\D+", "", str(crow["phone"] or ""))
                    other_tid = crow["telegram_user_id"]
                    other_real_tid = (
                        int(other_tid)
                        if other_tid is not None and not _is_synthetic_peer_id(other_tid)
                        else None
                    )
                    other_lid = int(crow["lead_id"]) if crow["lead_id"] is not None else None
                    if (
                        (clean_rec_uname and other_u == clean_rec_uname)
                        or (len(norm_rec_phone_digits) >= 7 and other_p_digits == norm_rec_phone_digits)
                        or (real_rec_peer_id is not None and other_real_tid == real_rec_peer_id)
                        or (rec_lead_id is not None and other_lid == rec_lead_id)
                    ):
                        has_already_contacted_dup = True
                        break
            if has_already_contacted_dup:
                conn.execute(
                    """
                    UPDATE campaign_recipients
                    SET status = 'completed',
                        error_text = 'Пропущен дубль: контакт уже получал аутрич',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, rec["id"]),
                )
                conn.commit()
                continue

            # Guard 3b: Anti-duplicate outgoing outreach message already in messages
            stripped_rendered = rendered_text.strip()
            if stripped_rendered and (clean_rec_uname or rec_peer_str or norm_rec_phone_digits):
                cand_msgs = conn.execute(
                    """
                    SELECT d.account_id, d.phone, m.sent_at, m.text
                    FROM dialogs d
                    JOIN messages m ON m.dialog_id = d.id
                    WHERE m.direction = 'out'
                      AND (
                          (? != '' AND lower(trim(coalesce(d.username, ''), '@')) = lower(?))
                          OR (? != '' AND d.peer_id = ?)
                          OR (? != '' AND replace(replace(replace(coalesce(d.phone, ''), '+', ''), ' ', ''), '-', '') = ?)
                      )
                    ORDER BY m.sent_at DESC, m.id DESC
                    """,
                    (
                        clean_rec_uname,
                        clean_rec_uname,
                        rec_peer_str,
                        real_rec_peer_id,
                        norm_rec_phone_digits,
                        norm_rec_phone_digits,
                    ),
                ).fetchall()
                camp_low = str(camp.get("name") or "").lower()
                is_vac_camp = "ваканс" in camp_low or "vacanc" in camp_low
                dup_msg = next(
                    (
                        m
                        for m in cand_msgs
                        if str(m["text"] or "").strip() == stripped_rendered
                        or (
                            not is_vac_camp
                            and any(
                                marker in str(m["text"] or "").lower()
                                for marker in ("softugc", "183.5m", "creatoros", "revshare or cpa")
                            )
                        )
                    ),
                    None,
                )
                if dup_msg is not None:
                    dup_acc = (
                        int(dup_msg["account_id"])
                        if dup_msg["account_id"] is not None
                        else rec.get("assigned_account_id")
                    )
                    dup_sent_at = str(dup_msg["sent_at"] or now)
                    synced_step = max(int(rec.get("current_step") or 0), 1)
                    synced_status = "completed" if synced_step >= len(steps) else "in_progress"
                    conn.execute(
                        """
                        UPDATE campaign_recipients
                        SET current_step = ?,
                            status = ?,
                            sent_account_id = COALESCE(sent_account_id, ?),
                            assigned_account_id = COALESCE(assigned_account_id, ?),
                            last_sent_at = COALESCE(last_sent_at, ?),
                            error_text = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            synced_step,
                            synced_status,
                            dup_acc,
                            dup_acc,
                            dup_sent_at,
                            now,
                            rec["id"],
                        ),
                    )
                    conn.commit()
                    continue

        pinned_followup_acc_id: int | None = None
        if step_idx >= 1:
            # Guard 2a & 2b: Resolve sent_account_id strictly and require real prior outgoing message in messages
            raw_sent_acc = rec.get("sent_account_id")
            if raw_sent_acc is not None:
                verified_acc = _lookup_prior_outgoing_account(
                    conn,
                    clean_uname=clean_rec_uname,
                    real_peer_id=real_rec_peer_id,
                    clean_phone=clean_rec_phone,
                    lead_id_val=rec_lead_id,
                    required_account_id=int(raw_sent_acc),
                )
                if verified_acc is not None:
                    pinned_followup_acc_id = verified_acc
            if pinned_followup_acc_id is None:
                found_hist_acc = _lookup_prior_outgoing_account(
                    conn,
                    clean_uname=clean_rec_uname,
                    real_peer_id=real_rec_peer_id,
                    clean_phone=clean_rec_phone,
                    lead_id_val=rec_lead_id,
                )
                if found_hist_acc is not None:
                    pinned_followup_acc_id = found_hist_acc
                    rec["sent_account_id"] = found_hist_acc
                    conn.execute(
                        """
                        UPDATE campaign_recipients
                        SET sent_account_id = ?,
                            assigned_account_id = COALESCE(assigned_account_id, ?),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (found_hist_acc, found_hist_acc, now, rec["id"]),
                    )
                    conn.commit()
            if pinned_followup_acc_id is None:
                conn.execute(
                    """
                    UPDATE campaign_recipients
                    SET status = 'completed',
                        error_text = 'Фоллоу-ап заблокирован: нет предыдущего сообщения в чате',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, rec["id"]),
                )
                conn.commit()
                continue

        sent_this_recipient = False
        no_accounts_left = False
        jitter_applied = False
        replied_skipped = False
        rotated_after_failure = False
        while True:
            available = [
                st
                for st in connected_states
                if not st.get("disabled")
                and st["sent_today"] < st["daily_limit"]
                and (step_idx >= 1 or not st.get("spambot_warm_only"))
            ]
            if not available:
                no_accounts_left = True
                break

            assigned_id = rec.get("assigned_account_id")
            if step_idx >= 1:
                # Guard 2c: Strictly use sent_account_id ONLY — NEVER rotate follow-up to another account
                if pinned_followup_acc_id is None or int(pinned_followup_acc_id) not in state_by_id:
                    chosen_state = None
                else:
                    pinned_state = state_by_id[int(pinned_followup_acc_id)]
                    if (
                        not pinned_state.get("disabled")
                        and not pinned_state.get("dead")
                        and pinned_state["sent_today"] < pinned_state["daily_limit"]
                    ):
                        chosen_state = pinned_state
                    else:
                        chosen_state = None
            elif assigned_id is not None and int(assigned_id) in state_by_id:
                pinned_state = state_by_id[int(assigned_id)]
                if (
                    not pinned_state.get("disabled")
                    and not pinned_state.get("spambot_warm_only")
                    and pinned_state["sent_today"] < pinned_state["daily_limit"]
                ):
                    chosen_state = pinned_state
                else:
                    chosen_state = min(
                        available,
                        key=lambda st: (st["sent_in_batch"], st["sent_today"], st["order"]),
                    )
            else:
                chosen_state = min(
                    available,
                    key=lambda st: (st["sent_in_batch"], st["sent_today"], st["order"]),
                )

            if chosen_state is None:
                break

            chosen_account_id = chosen_state["id"]
            account_raw = chosen_state["raw"]
            acc_provider = (account_raw.get("provider") or "telegram").strip().lower()
            session_path = str(account_raw.get("session_path") or "")
            is_demo_session = session_path.startswith("data/sessions/demo")
            active_gw = (wa_gateway if wa_gateway is not None else gateway) if acc_provider == "whatsapp" else gateway
            if not is_demo_session and active_gw is None:
                chosen_state["disabled"] = True
                continue
            use_gateway = active_gw is not None and not is_demo_session

            if not jitter_applied and jitter_range is not None and len(jitter_range) == 2:
                jitter_applied = True
                j_min = max(0.0, float(jitter_range[0]))
                j_max = max(j_min, float(jitter_range[1]))
                j_sec = random.uniform(j_min, j_max)
                jitter_delays_ms.append(int(round(j_sec * 1000)))
                if j_sec > 0:
                    await asyncio.sleep(j_sec)

            replied_skipped = False
            dialog = None
            try:
                if acc_provider == "whatsapp":
                    dialog = _ensure_recipient_dialog(
                        conn, chosen_account_id, rec, now, provider="whatsapp"
                    )
                else:
                    need_username_resolve = (
                        not rec.get("telegram_user_id")
                        or _is_synthetic_peer_id(rec["telegram_user_id"])
                        or rotated_after_failure
                        or (
                            rec.get("assigned_account_id") is not None
                            and int(rec["assigned_account_id"]) != chosen_account_id
                        )
                    )
                    if (
                        use_gateway
                        and rec.get("username")
                        and need_username_resolve
                        and hasattr(active_gw, "search_dialogs")
                    ):
                        found = await active_gw.search_dialogs(session_path, f"@{rec['username']}", limit=1)
                        if found and found[0].get("telegram_user_id"):
                            real_tg_id = int(found[0]["telegram_user_id"])
                            rec["telegram_user_id"] = real_tg_id
                            conn.execute(
                                "UPDATE campaign_recipients SET telegram_user_id = ? WHERE id = ?",
                                (real_tg_id, rec["id"]),
                            )
                            if rec.get("lead_id"):
                                try:
                                    conn.execute(
                                        "UPDATE leads SET telegram_user_id = ?, updated_at = ? WHERE id = ?",
                                        (real_tg_id, now, rec["lead_id"]),
                                    )
                                except sqlite3.IntegrityError:
                                    pass

                    dialog = _ensure_recipient_dialog(
                        conn, chosen_account_id, rec, now, provider="telegram"
                    )

                # Before sending follow-up (step_idx >= 1), check live and cached messages for reply
                if step_idx >= 1 and dialog:
                    live_history_checked = False
                    live_has_out = False
                    if use_gateway and hasattr(active_gw, "fetch_messages"):
                        try:
                            fetched_msgs = await active_gw.fetch_messages(
                                session_path, dialog["peer_id"], 5
                            )
                            if isinstance(fetched_msgs, list):
                                live_history_checked = True
                                live_has_out = any(
                                    str(m.get("direction") or "") == "out"
                                    for m in fetched_msgs
                                    if isinstance(m, dict)
                                )
                                if fetched_msgs:
                                    _cache_messages(conn, int(dialog["id"]), fetched_msgs)
                        except Exception as fetch_exc:
                            f_kind, _ = _classify_outreach_send_error(fetch_exc)
                            if f_kind in ("account_dead", "account_transient"):
                                raise
                            f_blob = str(fetch_exc or "").casefold()
                            clean_un = str(rec.get("username") or "").strip().lstrip("@")
                            if clean_un and ("could not find the input entity" in f_blob or "peeruser" in f_blob):
                                try:
                                    await active_gw.search_dialogs(session_path, f"@{clean_un}", limit=1)
                                    fetched_msgs = await active_gw.fetch_messages(
                                        session_path, dialog["peer_id"], 5
                                    )
                                    if isinstance(fetched_msgs, list):
                                        live_history_checked = True
                                        live_has_out = any(
                                            str(m.get("direction") or "") == "out"
                                            for m in fetched_msgs
                                            if isinstance(m, dict)
                                        )
                                        if fetched_msgs:
                                            _cache_messages(conn, int(dialog["id"]), fetched_msgs)
                                except Exception as inner_exc:
                                    if _classify_outreach_send_error(inner_exc)[0] in ("account_dead", "account_transient"):
                                        raise
                    refreshed_dlg_row = conn.execute(
                        "SELECT * FROM dialogs WHERE id = ?", (dialog["id"],)
                    ).fetchone()
                    if refreshed_dlg_row is not None:
                        dialog = as_dict(refreshed_dlg_row)
                    if _dialog_has_client_reply(conn, dialog, rec):
                        conn.execute(
                            "UPDATE campaign_recipients SET status = 'replied', updated_at = ? WHERE id = ?",
                            (now, rec["id"]),
                        )
                        stop_outreach_on_reply(
                            conn,
                            account_id=chosen_account_id,
                            peer_id=dialog.get("peer_id") or rec.get("telegram_user_id"),
                            username=dialog.get("username") or rec.get("username"),
                            phone=dialog.get("phone") or rec.get("phone"),
                            lead_id=dialog.get("lead_id") or rec.get("lead_id"),
                        )
                        conn.commit()
                        replied_skipped = True
                        break
                    if live_history_checked and not live_has_out:
                        conn.execute(
                            """
                            UPDATE campaign_recipients
                            SET status = 'completed',
                                error_text = 'Фоллоу-ап заблокирован: нет предыдущего сообщения в чате',
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (now, rec["id"]),
                        )
                        conn.commit()
                        replied_skipped = True
                        break

                if acc_provider == "whatsapp":
                    phone_digits = re.sub(
                        r"\D", "", str(rec.get("phone") or dialog.get("phone") or "")
                    )
                    peer_target = phone_digits or rec.get("telegram_user_id") or dialog["peer_id"]
                    if use_gateway:
                        sent = await active_gw.send_message(
                            session_path, peer_target, rendered_text
                        )
                        tg_mid = int(sent["telegram_message_id"])
                        sent_at = str(sent.get("sent_at") or now)
                    else:
                        next_row = conn.execute(
                            "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
                            (dialog["id"],),
                        ).fetchone()
                        tg_mid = int(next_row["next_id"])
                        sent_at = now
                else:
                    next_step_order = step_idx + 1
                    if use_gateway:
                        peer_target = (
                            f"@{rec['username']}"
                            if (_is_synthetic_peer_id(dialog["peer_id"]) and rec.get("username"))
                            else dialog["peer_id"]
                        )
                        if next_step_order == 1:
                            target_entity = peer_target
                            lock_fn = getattr(active_gw, "_session_lock", None)
                            if callable(lock_fn):
                                async with lock_fn(session_path):
                                    client = await _resolve_telethon_client_for_prewarm(
                                        active_gw, session_path
                                    )
                                    if client is not None:
                                        await prewarm_recipient_stories(client, target_entity)
                            else:
                                client = await _resolve_telethon_client_for_prewarm(
                                    active_gw, session_path
                                )
                                if client is not None:
                                    await prewarm_recipient_stories(client, target_entity)
                        sent = await _tg_send_with_username_fallback(
                            active_gw,
                            session_path,
                            peer_target,
                            rendered_text,
                            step_image_url,
                            rec.get("username"),
                        )
                        tg_mid = int(sent["telegram_message_id"])
                        sent_at = str(sent.get("sent_at") or now)
                    else:
                        next_row = conn.execute(
                            "SELECT COALESCE(MAX(telegram_message_id), 9000) + 1 AS next_id FROM messages WHERE dialog_id = ?",
                            (dialog["id"],),
                        ).fetchone()
                        tg_mid = int(next_row["next_id"])
                        sent_at = now
                sent_this_recipient = True
                break
            except Exception as exc:
                err_kind, err_health = _classify_outreach_send_error(exc)
                if err_kind in ("account_dead", "account_transient"):
                    chosen_state["disabled"] = True
                    rotated_after_failure = True
                    if step_idx == 0 and dialog is not None and not dialog.get("last_message_at"):
                        msg_cnt = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE dialog_id = ?",
                            (dialog["id"],),
                        ).fetchone()[0]
                        if int(msg_cnt or 0) == 0:
                            conn.execute("DELETE FROM dialogs WHERE id = ?", (dialog["id"],))
                    if err_kind == "account_dead":
                        chosen_state["dead"] = True
                        err_str = str(exc)[:300]
                        conn.execute(
                            "UPDATE accounts SET health = ?, last_error = ?, updated_at = ? WHERE id = ?",
                            (err_health, err_str, now, chosen_account_id),
                        )
                        conn.commit()
                        is_spambot_only = any(
                            m in err_str.casefold()
                            for m in ("spambot", "cannot message non-contacts", "peerspam", "spam-reported")
                        )
                        if not is_spambot_only and active_gw is not None and hasattr(active_gw, "_close"):
                            try:
                                await active_gw._close(session_path)
                            except Exception:
                                pass
                    if step_idx >= 1:
                        break
                    continue

                conn.execute(
                    """
                    UPDATE campaign_recipients
                    SET status = 'failed', error_text = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:300], now, rec["id"]),
                )
                failed_in_batch += 1
                break

        if no_accounts_left:
            break
        if replied_skipped or not sent_this_recipient:
            continue

        stored_media_url = None if acc_provider == "whatsapp" else step_image_url
        conn.execute(
            """
            INSERT INTO messages (
                dialog_id, telegram_message_id, direction, text, media_url, sent_at,
                reply_to_telegram_message_id, created_at, updated_at
            ) VALUES (?, ?, 'out', ?, ?, ?, NULL, ?, ?)
            """,
            (dialog["id"], tg_mid, rendered_text, stored_media_url, sent_at, now, now),
        )
        conn.execute(
            """
            UPDATE dialogs
            SET last_message_at = ?, last_message_preview = ?, last_from_peer = 0, updated_at = ?
            WHERE id = ?
            """,
            (sent_at, rendered_text[:120], now, dialog["id"]),
        )
        increment_daily_sent(conn, chosen_account_id)
        chosen_state["sent_today"] += 1
        chosen_state["sent_in_batch"] += 1
        sent_by_account[str(chosen_account_id)] = sent_by_account.get(str(chosen_account_id), 0) + 1

        next_step = step_idx + 1
        next_status = "completed" if next_step >= len(steps) else "in_progress"
        pinned_sent_acc = (
            chosen_account_id
            if step_idx == 0
            else int(rec.get("sent_account_id") or chosen_account_id)
        )
        conn.execute(
            """
            UPDATE campaign_recipients
            SET current_step = ?,
                status = ?,
                assigned_account_id = ?,
                sent_account_id = ?,
                last_sent_at = ?,
                error_text = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_step,
                next_status,
                chosen_account_id,
                pinned_sent_acc,
                sent_at,
                now,
                rec["id"],
            ),
        )
        # Immediate commit & cloud persistence dirty marker right after Step 0 (and every step) before any next-tick/jitter sleep
        _mark_cloud_persistence_dirty(conn, now)
        if rec.get("lead_id"):
            if next_step < len(steps):
                next_delay_h = int(steps[next_step].get("delay_hours") or 0)
                lead_next_step = f"Отправлен шаг {next_step}/{len(steps)} · след. фоллоу-ап через {next_delay_h}ч"
            else:
                lead_next_step = f"Отправлены все {len(steps)} шага рассылки"
            try:
                conn.execute(
                    """
                    UPDATE leads
                    SET account_id = ?,
                        dialog_id = CASE WHEN account_id IS NULL OR account_id != ? THEN ? ELSE COALESCE(dialog_id, ?) END,
                        status = CASE WHEN status = 'new' THEN 'waiting' ELSE status END,
                        next_step = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        chosen_account_id,
                        chosen_account_id,
                        dialog["id"],
                        dialog["id"],
                        lead_next_step,
                        now,
                        int(rec["lead_id"]),
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE leads
                    SET dialog_id = COALESCE(dialog_id, ?),
                        status = CASE WHEN status = 'new' THEN 'waiting' ELSE status END,
                        next_step = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (dialog["id"], lead_next_step, now, int(rec["lead_id"])),
                )
            _record_lead_event_safe(
                conn,
                lead_id=int(rec["lead_id"]),
                event_type="outreach_sent",
                title=f"Отправлен шаг {next_step}/{len(steps)} кампании «{camp['name']}»",
                meta={
                    "campaign_id": campaign_id,
                    "campaign_name": camp["name"],
                    "step": next_step,
                    "total_steps": len(steps),
                    "account_id": chosen_account_id,
                    "preview": rendered_text[:120],
                },
                now=now,
            )
        sent_count += 1

    remaining_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM campaign_recipients
        WHERE campaign_id = ?
          AND status IN ('pending', 'in_progress')
          AND current_step < ?
        """,
        (campaign_id, len(steps)),
    ).fetchone()
    remaining_active = int(remaining_row["cnt"] or 0)
    new_camp_status = "completed" if remaining_active == 0 and camp["total_recipients"] > 0 else "running"
    conn.execute(
        "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
        (new_camp_status, utc_now(), campaign_id),
    )
    conn.commit()

    primary_account_id = connected_states[0]["id"]
    updated_campaign = get_campaign(conn, campaign_id)
    updated_account = public_account(load_account(conn, primary_account_id))
    return {
        "sent": sent_count,
        "sent_by_account": sent_by_account,
        "jitter_delays_ms": jitter_delays_ms,
        "limit_reached": sent_count > 0 and sent_count == effective_quota,
        "remaining": remaining_active,
        "campaign": updated_campaign,
        "account": updated_account,
    }


async def run_autopilot_tick(
    conn,
    gateway=None,
    wa_gateway=None,
    now_dt: datetime | None = None,
    max_send_per_campaign: int = 1,
    respect_delays: bool = True,
    jitter_range: tuple[float, float] | None = (0.01, 0.05),
    force: bool = False,
    now: datetime | None = None,
    max_per_campaign: int | None = None,
) -> dict:
    effective_now = now_dt if now_dt is not None else now
    effective_max = int(max_per_campaign) if max_per_campaign is not None else int(max_send_per_campaign)
    tick_iso = (
        effective_now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(effective_now, datetime) and effective_now.tzinfo is not None
        else (
            effective_now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(effective_now, datetime)
            else utc_now()
        )
    )
    _set_kv(conn, "outreach_autopilot_last_tick", tick_iso)
    conn.commit()

    sched = get_outreach_schedule(conn, now_dt=effective_now)
    enabled = bool(sched["autopilot_enabled"])
    within_window = bool(sched["within_window"])

    if not enabled and not force:
        return {
            "ok": True,
            "ran": False,
            "enabled": False,
            "within_schedule": within_window,
            "reason": "autopilot_disabled",
            "campaigns_checked": 0,
            "sent": 0,
            "results": [],
            "campaigns": [],
            "status": get_autopilot_status(conn, now_dt=effective_now),
        }

    if not within_window and not force:
        return {
            "ok": True,
            "ran": False,
            "enabled": enabled,
            "within_schedule": False,
            "reason": "outside_schedule",
            "campaigns_checked": 0,
            "sent": 0,
            "results": [],
            "campaigns": [],
            "status": get_autopilot_status(conn, now_dt=effective_now),
        }

    heal_transient_failed_recipients(conn)
    running_rows = conn.execute(
        "SELECT id, name FROM campaigns WHERE status = 'running' ORDER BY id"
    ).fetchall()

    total_sent = 0
    results = []
    for crow in running_rows:
        cid = int(crow["id"])
        try:
            batch_res = await run_campaign_batch(
                conn,
                cid,
                gateway=gateway,
                max_send=effective_max,
                respect_delays=respect_delays,
                wa_gateway=wa_gateway,
                jitter_range=jitter_range,
            )
            sent_now = int(batch_res.get("sent") or 0)
            total_sent += sent_now
            results.append(
                {
                    "campaign_id": cid,
                    "campaign_name": crow["name"],
                    "sent": sent_now,
                    "sent_by_account": batch_res.get("sent_by_account") or {},
                    "jitter_delays_ms": batch_res.get("jitter_delays_ms") or [],
                    "remaining": batch_res.get("remaining"),
                    "limit_reached": batch_res.get("limit_reached", False),
                }
            )
        except ValueError as exc:
            results.append(
                {
                    "campaign_id": cid,
                    "campaign_name": crow["name"],
                    "sent": 0,
                    "limit_reached": "лимит" in str(exc).lower(),
                    "skipped_reason": str(exc),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "campaign_id": cid,
                    "campaign_name": crow["name"],
                    "sent": 0,
                    "skipped_reason": str(exc),
                }
            )

    return {
        "ok": True,
        "ran": True,
        "enabled": enabled,
        "within_schedule": within_window,
        "reason": "completed",
        "campaigns_checked": len(running_rows),
        "sent": total_sent,
        "results": results,
        "campaigns": results,
        "status": get_autopilot_status(conn, now_dt=effective_now),
    }

