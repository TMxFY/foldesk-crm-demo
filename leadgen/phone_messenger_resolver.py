"""Layer 2 ($0) phone-to-messenger verification and lead routing.

Verifies normalized E.164 phone numbers against WhatsApp (via WhatsAppGateway)
and Telegram (via a dedicated checker Telethon session using ImportContacts /
DeleteContacts), then routes leads by strict channel priority:
Telegram > WhatsApp > WA pending retry > Manual Social Queue (ICP >= 85).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import inspect
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ResolvedMessenger",
    "build_fallback_contact_draft",
    "merge_resolved_messengers",
    "normalize_e164",
    "resolve_phones_telegram",
    "resolve_phones_whatsapp",
    "route_resolved_lead",
]

_PLACEHOLDER_VALUES = frozenset({"", "none", "null", "n/a", "na", "-", "—"})
_X_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")
_IG_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_.]{1,30}$")
_EMAIL_SIMPLE_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_NOREPLY_EMAIL_PREFIXES = ("noreply@", "no-reply@", "no_reply@", "donotreply@", "do-not-reply@")


@dataclass
class ResolvedMessenger:
    """Verification outcome for a single normalized E.164 phone number."""

    phone_e164: str
    whatsapp_exists: bool = False
    whatsapp_jid: str = ""
    telegram_username: str = ""
    telegram_user_id: int | None = None
    wa_check_pending: bool = False
    tg_check_status: str = ""


def normalize_e164(raw: str) -> str:
    """Normalize a raw phone string to E.164 ('+<7..15 digits>'), else ''."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("00"):
        digits = re.sub(r"\D", "", text[2:])
    else:
        digits = re.sub(r"\D", "", text)
    if 7 <= len(digits) <= 15:
        return f"+{digits}"
    return ""


def _dedupe_normalized_phones(phones: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in phones or ():
        e164 = normalize_e164(str(raw or ""))
        if e164 and e164 not in seen:
            seen.add(e164)
            ordered.append(e164)
    return ordered


def _mark_wa_pending(
    resolved: dict[str, ResolvedMessenger],
    phones: list[str],
) -> None:
    for e164 in phones:
        resolved[e164] = ResolvedMessenger(
            phone_e164=e164,
            whatsapp_exists=False,
            whatsapp_jid="",
            wa_check_pending=True,
        )


async def resolve_phones_whatsapp(
    wa_gateway: Any,
    wa_session_path: str,
    phones: list[str],
    *,
    batch_size: int = 20,
    pause_s: float = 3.0,
) -> dict[str, ResolvedMessenger]:
    """Check whether normalized E.164 numbers exist on WhatsApp via wa_gateway.

    Never fabricates `whatsapp_exists=True` on missing session or gateway error;
    instead marks unchecked numbers with `wa_check_pending=True` for later retry.
    """
    unique_phones = _dedupe_normalized_phones(phones)
    if not unique_phones:
        return {}

    resolved: dict[str, ResolvedMessenger] = {}
    session_str = str(wa_session_path or "").strip()
    if wa_gateway is None or not session_str or not hasattr(wa_gateway, "check_numbers"):
        _mark_wa_pending(resolved, unique_phones)
        return resolved

    step = max(1, int(batch_size or 20))
    delay = max(0.0, float(pause_s or 0.0))

    for start in range(0, len(unique_phones), step):
        batch = unique_phones[start : start + step]
        try:
            call_res = wa_gateway.check_numbers(session_str, batch)
            if inspect.isawaitable(call_res):
                items = await call_res
            else:
                items = call_res
        except Exception as exc:
            logger.warning("WhatsApp check_numbers failed (%s); marking batch pending", exc)
            _mark_wa_pending(resolved, unique_phones[start:])
            break

        by_e164: dict[str, Mapping[str, Any]] = {}
        for item in items or ():
            if not isinstance(item, Mapping):
                continue
            item_phone = normalize_e164(
                str(item.get("phone") or item.get("query") or "")
            )
            if item_phone:
                by_e164[item_phone] = item

        for e164 in batch:
            row = by_e164.get(e164)
            if row is None:
                resolved[e164] = ResolvedMessenger(
                    phone_e164=e164,
                    whatsapp_exists=False,
                    whatsapp_jid="",
                    wa_check_pending=False,
                )
                continue

            if "found" in row:
                exists = bool(row.get("found"))
            elif "is_on_whatsapp" in row:
                exists = bool(row.get("is_on_whatsapp"))
            else:
                exists = bool(row.get("exists", False))

            raw_jid = str(row.get("wa_jid") or row.get("jid") or "").strip()
            jid = (raw_jid or f"{e164.lstrip('+')}@s.whatsapp.net") if exists else ""

            resolved[e164] = ResolvedMessenger(
                phone_e164=e164,
                whatsapp_exists=exists,
                whatsapp_jid=jid,
                wa_check_pending=False,
            )

        if start + step < len(unique_phones) and delay > 0:
            await asyncio.sleep(delay)

    return resolved


def _extract_tg_username(user: Any) -> str:
    direct = str(getattr(user, "username", "") or "").strip().lstrip("@")
    if direct:
        return direct
    for extra in getattr(user, "usernames", None) or ():
        if getattr(extra, "active", True):
            handle = str(getattr(extra, "username", "") or "").strip().lstrip("@")
            if handle:
                return handle
    return ""


def _map_imported_users(
    res: Any,
    batch: list[str],
    client_id_to_phone: dict[int, str],
) -> dict[str, tuple[str, int | None]]:
    """Map Telethon ImportedContacts response back to batch E.164 phones."""
    user_id_to_phone: dict[int, str] = {}
    for imp in getattr(res, "imported", None) or ():
        uid = getattr(imp, "user_id", None)
        cid = getattr(imp, "client_id", None)
        if uid is not None and cid is not None:
            cid_int = int(cid)
            if cid_int in client_id_to_phone:
                user_id_to_phone[int(uid)] = client_id_to_phone[cid_int]

    users = [
        u
        for u in (getattr(res, "users", None) or ())
        if not getattr(u, "bot", False) and not getattr(u, "deleted", False)
    ]

    matches: dict[str, tuple[str, int | None]] = {}
    for user in users:
        raw_uid = getattr(user, "id", None)
        uid = int(raw_uid) if raw_uid is not None else None

        phone_e164 = user_id_to_phone.get(uid, "") if uid is not None else ""
        if not phone_e164:
            phone_e164 = normalize_e164(str(getattr(user, "phone", "") or ""))
        if not phone_e164 and len(batch) == 1 and len(users) == 1:
            phone_e164 = batch[0]
        if not phone_e164 or phone_e164 not in batch:
            continue

        username = _extract_tg_username(user)
        matches[phone_e164] = (username, uid)

    return matches


def _set_tg_status_for_phones(
    resolved: dict[str, ResolvedMessenger],
    phones: list[str],
    status: str,
) -> None:
    for e164 in phones:
        resolved[e164] = ResolvedMessenger(
            phone_e164=e164,
            tg_check_status=status,
        )


async def resolve_phones_telegram(
    checker_client: Any,
    phones: list[str],
    *,
    batch_size: int = 15,
    daily_limit: int = 120,
    pause_s: float = 45.0,
    is_checker_session: bool = False,
) -> dict[str, ResolvedMessenger]:
    """Resolve E.164 phone numbers to Telegram accounts via a dedicated checker.

    Refuses immediately if `not is_checker_session` so `ImportContactsRequest`
    never touches warm outreach sessions. Always cleans up imported contacts
    via `DeleteContactsRequest` in `finally`, and stops early on FloodWaitError
    or two consecutive 0-hit batches.
    """
    if not is_checker_session:
        raise ValueError(
            "resolve_phones_telegram requires a dedicated checker session "
            "(is_checker_session=True); refusing ImportContactsRequest on outreach session"
        )

    unique_phones = _dedupe_normalized_phones(phones)
    if not unique_phones:
        return {}

    resolved: dict[str, ResolvedMessenger] = {}
    if checker_client is None:
        _set_tg_status_for_phones(resolved, unique_phones, "no_checker_client")
        return resolved

    cap = max(0, int(daily_limit))
    if cap == 0:
        _set_tg_status_for_phones(resolved, unique_phones, "daily_limit_reached")
        return resolved

    phones_to_check = unique_phones[:cap]
    overflow_phones = unique_phones[cap:]
    if overflow_phones:
        _set_tg_status_for_phones(resolved, overflow_phones, "daily_limit_reached")

    from telethon.errors import FloodWaitError
    from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
    from telethon.tl.types import InputPhoneContact

    step = max(1, int(batch_size or 15))
    delay = max(0.0, float(pause_s or 0.0))
    consecutive_zero_hits = 0

    for start in range(0, len(phones_to_check), step):
        batch = phones_to_check[start : start + step]
        client_id_to_phone = {idx: phone for idx, phone in enumerate(batch, start=1)}
        contacts = [
            InputPhoneContact(
                client_id=idx,
                phone=phone,
                first_name=f"Contact{idx}",
                last_name="",
            )
            for idx, phone in client_id_to_phone.items()
        ]

        users_to_delete: list[Any] = []
        stop_early = False
        try:
            call_res = checker_client(ImportContactsRequest(contacts))
            res = await call_res if inspect.isawaitable(call_res) else call_res
            users_to_delete = list(getattr(res, "users", None) or ())
            matches = _map_imported_users(res, batch, client_id_to_phone)

            batch_hits = len(matches)
            if batch_hits == 0:
                consecutive_zero_hits += 1
            else:
                consecutive_zero_hits = 0

            if consecutive_zero_hits >= 2:
                _set_tg_status_for_phones(
                    resolved, phones_to_check[start:], "zero_hit_paused"
                )
                stop_early = True
            else:
                for e164 in batch:
                    if e164 in matches:
                        tg_user, tg_uid = matches[e164]
                        resolved[e164] = ResolvedMessenger(
                            phone_e164=e164,
                            telegram_username=tg_user,
                            telegram_user_id=tg_uid,
                            tg_check_status="found",
                        )
                    else:
                        resolved[e164] = ResolvedMessenger(
                            phone_e164=e164,
                            tg_check_status="not_found",
                        )
        except Exception as exc:
            is_flood = isinstance(exc, FloodWaitError) or (
                exc.__class__.__name__ == "FloodWaitError"
            )
            status = "flood_wait" if is_flood else "error"
            logger.warning("Telegram phone checker stopped (%s): %s", status, exc)
            _set_tg_status_for_phones(resolved, phones_to_check[start:], status)
            stop_early = True
        finally:
            if users_to_delete:
                try:
                    del_res = checker_client(DeleteContactsRequest(id=users_to_delete))
                    if inspect.isawaitable(del_res):
                        await del_res
                except Exception as del_exc:
                    logger.warning(
                        "DeleteContactsRequest cleanup failed: %s", del_exc
                    )

        if stop_early:
            break
        if start + step < len(phones_to_check) and delay > 0:
            await asyncio.sleep(delay)

    return resolved


def merge_resolved_messengers(
    wa_results: Mapping[str, ResolvedMessenger],
    tg_results: Mapping[str, ResolvedMessenger],
) -> dict[str, ResolvedMessenger]:
    """Combine separate WhatsApp and Telegram resolution maps by normalized E.164."""
    merged: dict[str, ResolvedMessenger] = {}
    for source_map in (wa_results, tg_results):
        for raw_key, item in source_map.items():
            e164 = normalize_e164(item.phone_e164 or raw_key)
            if not e164:
                continue
            cur = merged.get(e164)
            if cur is None:
                merged[e164] = ResolvedMessenger(
                    phone_e164=e164,
                    whatsapp_exists=item.whatsapp_exists,
                    whatsapp_jid=item.whatsapp_jid,
                    telegram_username=item.telegram_username,
                    telegram_user_id=item.telegram_user_id,
                    wa_check_pending=item.wa_check_pending,
                    tg_check_status=item.tg_check_status,
                )
                continue
            cur.whatsapp_exists = cur.whatsapp_exists or item.whatsapp_exists
            if item.whatsapp_jid:
                cur.whatsapp_jid = item.whatsapp_jid
            if item.telegram_username:
                cur.telegram_username = item.telegram_username
            if item.telegram_user_id is not None:
                cur.telegram_user_id = item.telegram_user_id
            cur.wa_check_pending = (
                False if cur.whatsapp_exists else (cur.wa_check_pending or item.wa_check_pending)
            )
            if item.tg_check_status:
                cur.tg_check_status = item.tg_check_status
    return merged


def _clean_field(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold() in _PLACEHOLDER_VALUES:
        return ""
    return text


def _normalize_social_url(raw: Any, kind: str) -> str:
    text = _clean_field(raw)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return text
    if kind == "x":
        if "x.com/" in lowered or "twitter.com/" in lowered:
            return f"https://{text.lstrip('/')}"
        if _X_HANDLE_RE.fullmatch(text):
            return f"https://x.com/{text.lstrip('@')}"
        return ""
    if kind == "linkedin":
        if "linkedin.com/" in lowered:
            return f"https://{text.lstrip('/')}"
        return ""
    if kind == "instagram":
        if "instagram.com/" in lowered:
            return f"https://{text.lstrip('/')}"
        if _IG_HANDLE_RE.fullmatch(text):
            return f"https://instagram.com/{text.lstrip('@')}"
        return ""
    return ""


def _pick_manual_social_url(row: Mapping[str, Any]) -> str:
    for key in ("founder_x", "x_url", "founder_twitter", "twitter", "twitter_url"):
        url = _normalize_social_url(row.get(key), "x")
        if url:
            return url
    for key in ("founder_linkedin", "linkedin_url", "linkedin", "li_url"):
        url = _normalize_social_url(row.get(key), "linkedin")
        if url:
            return url
    for key in (
        "instagram_url",
        "instagram_handle",
        "instagram",
        "ig_url",
        "ig_handle",
    ):
        url = _normalize_social_url(row.get(key), "instagram")
        if url:
            return url
    return ""


def _parse_icp_score(raw: Any) -> float:
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _extract_wa_url_e164(raw_val: str) -> str:
    lowered = raw_val.lower()
    if "wa.me/" in lowered or "whatsapp.com/" in lowered:
        wa_m = re.search(r"(?:wa\.me/(?:c/)?|phone=)\+?(\d{7,15})", raw_val, re.I)
        if wa_m:
            return f"+{wa_m.group(1)}"
        return ""
    return normalize_e164(raw_val)


def _lookup_resolved(
    row: Mapping[str, Any],
    resolved_by_phone: Mapping[str, ResolvedMessenger],
) -> ResolvedMessenger | None:
    if not resolved_by_phone:
        return None

    normalized_index: dict[str, ResolvedMessenger] | None = None
    for key in ("phone", "mobile_phone", "whatsapp"):
        raw_val = _clean_field(row.get(key))
        if not raw_val:
            continue
        if raw_val in resolved_by_phone:
            return resolved_by_phone[raw_val]
        e164 = _extract_wa_url_e164(raw_val) if key == "whatsapp" else normalize_e164(raw_val)
        if not e164:
            continue
        if e164 in resolved_by_phone:
            return resolved_by_phone[e164]
        if normalized_index is None:
            normalized_index = {}
            for k, v in resolved_by_phone.items():
                nk = normalize_e164(v.phone_e164 or str(k))
                if nk and nk not in normalized_index:
                    normalized_index[nk] = v
        if e164 in normalized_index:
            return normalized_index[e164]
    return None


def _extract_first_name(row: Mapping[str, Any]) -> str:
    for key in ("first_name", "founder_name", "contact_name", "name"):
        raw = _clean_field(row.get(key))
        if raw and not raw.startswith(("@", "http")):
            first = raw.split()[0].strip(",.:;!?")
            if first and len(first) <= 32:
                return first
    return ""


def _extract_brand_name(row: Mapping[str, Any]) -> str:
    for key in ("brand_name", "company", "company_name", "project"):
        raw = _clean_field(row.get(key))
        if raw:
            return raw
    return ""


def _build_bridge_dm(row: Mapping[str, Any], language: str) -> str:
    first_name = _extract_first_name(row)
    brand = _extract_brand_name(row)
    lang = (language or "en").strip().lower()

    if lang.startswith("ru"):
        greet = f"Привет, {first_name}!" if first_name else "Привет!"
        target = f"под {brand}" if brand else "под ваш бренд"
        text = (
            f"{greet} Пишу из SoftUGC — подскажите ваш Telegram или WhatsApp, "
            f"скину 3 демо-рилса и кейсы {target}."
        )
    else:
        greet = f"Hey {first_name}, " if first_name else "Hey, "
        target = f"for {brand}" if brand else "for your brand"
        text = (
            f"{greet}quick note from SoftUGC — mind sharing your WhatsApp or Telegram "
            f"so I can send 3 custom demo reels {target}?"
        )
    return " ".join(text.split())


def _pick_fallback_contact_url(row: Mapping[str, Any]) -> tuple[str, str]:
    cform = _clean_field(row.get("contact_form_url"))
    if cform.lower().startswith(("http://", "https://")):
        return cform, "contact_form"
    for key in ("work_email", "email"):
        raw_em = _clean_field(row.get(key)).lower()
        if not raw_em:
            continue
        if raw_em.startswith("mailto:"):
            raw_em = raw_em[7:].split("?", 1)[0].strip()
        if any(raw_em.startswith(p) for p in _NOREPLY_EMAIL_PREFIXES):
            continue
        if _EMAIL_SIMPLE_RE.fullmatch(raw_em):
            return f"mailto:{raw_em}", "email"
    return "", ""


def build_fallback_contact_draft(row: Mapping[str, Any], language: str, channel: str) -> str:
    first_name = _extract_first_name(row)
    brand = _extract_brand_name(row)
    lang = (language or "en").strip().lower()
    ch = (channel or "").strip().lower()

    if ch == "whatsapp_hlr":
        if lang.startswith("ru"):
            greet = f"Привет, {first_name}!" if first_name else "Привет!"
            target = f"под {brand}" if brand else "под ваш бренд"
            text = (
                f"{greet} Пишу из SoftUGC — могу скинуть здесь 3 коротких "
                f"демо-рилса и кейсы {target}?"
            )
        else:
            greet = f"Hey {first_name}, " if first_name else "Hey, "
            target = f"for {brand}" if brand else "for your brand"
            text = (
                f"{greet}quick note from SoftUGC — mind if I send 3 short "
                f"custom demo reels {target} here?"
            )
        return " ".join(text.split())

    if lang.startswith("ru"):
        greet = f"Привет, {first_name}!" if first_name else "Привет!"
        target = f"под {brand}" if brand else "под ваш бренд"
        return (
            f"{greet} Мы в SoftUGC делаем AI UGC-рилсы под ключ (1,500+ роликов/мес).\n"
            f"Подскажите ваш WhatsApp или Telegram — скину 3 демо-ролика {target}?"
        )
    greet = f"Hey {first_name}, " if first_name else "Hey, "
    target = f"for {brand}" if brand else "for your brand"
    return (
        f"{greet}we produce performance AI UGC reels at SoftUGC (1,500+ videos/mo).\n"
        f"What's the best WhatsApp or Telegram to send 3 custom demo reels {target}?"
    )


def route_resolved_lead(
    row: Mapping[str, Any],
    resolved_by_phone: Mapping[str, ResolvedMessenger],
    *,
    language: str = "en",
) -> dict[str, Any]:
    """Route a lead record using resolved messenger verification outcomes.

    Priority order:
      1. Telegram username or verified TG user ID found -> provider='telegram', telegram_user_id
         (with tg_phone_direct=True when username is empty).
      2. WhatsApp verified -> provider='whatsapp', phone=<e164>, whatsapp='https://wa.me/<digits>'.
      3. WhatsApp check pending -> preserve phone, wa_check_pending=True (+ click-to-WA URL/draft).
      4. Neither TG nor WA, icp_score >= 85 ->
         segment='social_manual' via X > LinkedIn > IG, else Contact Form / Email fallback.
    """
    out = dict(row)
    eff_lang = (language or _clean_field(out.get("language")) or "en").strip().lower()
    resolved = _lookup_resolved(out, resolved_by_phone)

    tg_username = (
        resolved.telegram_username.strip().lstrip("@")
        if resolved and resolved.telegram_username
        else _clean_field(out.get("username") or out.get("telegram_username")).lstrip("@")
    )
    if tg_username:
        out["provider"] = "telegram"
        out["username"] = tg_username
        if resolved and resolved.telegram_user_id is not None:
            out["telegram_user_id"] = int(resolved.telegram_user_id)
        if resolved and resolved.phone_e164:
            out["phone"] = resolved.phone_e164
        out["wa_check_pending"] = False
        return out

    if resolved and resolved.tg_check_status == "found" and resolved.telegram_user_id is not None:
        out["provider"] = "telegram"
        out["telegram_user_id"] = int(resolved.telegram_user_id)
        out["phone"] = resolved.phone_e164 or normalize_e164(str(out.get("phone") or ""))
        out["tg_phone_direct"] = True
        out["wa_check_pending"] = False
        return out

    if resolved and resolved.whatsapp_exists:
        e164 = resolved.phone_e164 or normalize_e164(str(out.get("phone") or ""))
        digits = e164.lstrip("+")
        out["provider"] = "whatsapp"
        if e164:
            out["phone"] = e164
        if digits:
            out["whatsapp"] = f"https://wa.me/{digits}"
        out["wa_check_pending"] = False
        return out

    if resolved is None and not bool(out.get("wa_check_pending")):
        existing_wa = _clean_field(out.get("whatsapp"))
        if existing_wa and ("wa.me/" in existing_wa.lower() or "whatsapp.com/" in existing_wa.lower()):
            e164 = _extract_wa_url_e164(existing_wa) or normalize_e164(str(out.get("phone") or ""))
            out["provider"] = "whatsapp"
            if e164:
                out["phone"] = e164
            out["wa_check_pending"] = False
            return out

    if (resolved and resolved.wa_check_pending) or (
        resolved is None and bool(out.get("wa_check_pending"))
    ):
        e164 = (
            resolved.phone_e164
            if resolved and resolved.phone_e164
            else normalize_e164(str(out.get("phone") or ""))
        )
        if e164:
            out["phone"] = e164
            out["wa_click_url"] = f"https://wa.me/{e164.lstrip('+')}"
            out["wa_manual_draft_text"] = build_fallback_contact_draft(out, eff_lang, "whatsapp_hlr")
        out.pop("whatsapp", None)
        if str(out.get("provider") or "").strip().lower() == "whatsapp":
            out.pop("provider", None)
        out["wa_check_pending"] = True
        return out

    icp_score = _parse_icp_score(out.get("icp_score"))
    if icp_score >= 85:
        social_url = _pick_manual_social_url(out)
        if social_url:
            out["segment"] = "social_manual"
            out["manual_action_url"] = social_url
            out["manual_draft_text"] = _build_bridge_dm(out, eff_lang)
            return out
        fb_url, fb_kind = _pick_fallback_contact_url(out)
        if fb_url:
            out["segment"] = "social_manual"
            out["manual_action_url"] = fb_url
            out["manual_draft_text"] = build_fallback_contact_draft(out, eff_lang, fb_kind)
            out["manual_channel"] = fb_kind
            return out

    return out
