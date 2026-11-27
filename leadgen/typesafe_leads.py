#!/usr/bin/env python3
"""Small, typed TypeSafe judgments shared by the lead pipelines.

This module deliberately does not generate outreach copy.  It only answers
bounded questions (fit, role, category) and leaves filtering, routing and
sheet writes to the caller.  If TypeSafe is unavailable every caller gets a
safe ``None`` result and can keep its existing deterministic/LLM path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

def _ensure_safe_ssl_cert_env() -> None:
    """Populate SSL_CERT_FILE when unset so httpx2 avoids Linux truststore C-layer races."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return
    try:
        import certifi  # type: ignore

        ca_path = certifi.where()
        if ca_path and os.path.isfile(ca_path):
            os.environ["SSL_CERT_FILE"] = ca_path
            return
    except Exception:
        pass
    for cand in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.isfile(cand):
            os.environ["SSL_CERT_FILE"] = cand
            return


_ensure_safe_ssl_cert_env()

for _vendor_cand in ("/opt/crmtg/vendor/typesafe-sdk", "/Users/timofei/.codex/vendor/typesafe-sdk"):
    if os.path.isdir(_vendor_cand) and _vendor_cand not in sys.path:
        sys.path.append(_vendor_cand)

try:
    from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul
except Exception:  # SDK is optional at runtime.
    AsyncTypeSafeClient = Choice = Noul = None  # type: ignore

MODEL = os.getenv("TYPESAFE_MODEL", "jev-latest")
ENABLED = os.getenv("TYPESAFE_ENABLED", "1").lower() not in {"0", "false", "no"}
CONCURRENCY = max(1, min(6, int(os.getenv("TYPESAFE_CONCURRENCY", "4"))))
_DEFAULT_STATE_DIR = os.getenv("LEADGEN_STATE_DIR") or "data/leadgen_state"
CREATOR_CACHE = Path(os.getenv("TYPESAFE_CREATOR_CACHE", os.path.join(_DEFAULT_STATE_DIR, "typesafe_qualify_cache.json")))
FOREX_CACHE = Path(os.getenv("TYPESAFE_FOREX_CACHE", "data/typesafe_channel_cache.json"))
IDENTITY_CACHE = Path(os.getenv("TYPESAFE_IDENTITY_CACHE", os.path.join(_DEFAULT_STATE_DIR, "typesafe_identity_cache.json")))

try:
    from leadgen.supply_side import is_rejected_buyer_category
except ImportError:
    from supply_side import is_rejected_buyer_category  # type: ignore


def _load_key() -> str:
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    # launchd does not source interactive shell files; tolerate the user's
    # existing export without ever logging its value.
    for path in (Path.home() / ".zshrc", Path.home() / ".zprofile"):
        try:
            for line in path.read_text().splitlines():
                m = re.match(r"\s*export\s+TYPESAFE_API_KEY=(['\"]?)([^'\"\s]+)\1", line)
                if m:
                    return m.group(2)
        except OSError:
            pass
    return ""


def _cache_load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, path)
    except OSError:
        pass


def _key(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _answer(response: Any, name: str) -> Any:
    answers = getattr(response, "answers", {}) or {}
    return answers.get(name)


def _prob(answer: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(getattr(answer, "noul"))))
    except (TypeError, ValueError, AttributeError):
        return default


def _choice(answer: Any) -> str:
    value = getattr(answer, "choice", "")
    return str(value or "")


async def _run(states: list[tuple[str, dict[str, Any], dict[str, Any]]], cache_path: Path) -> list[dict[str, Any] | None]:
    """Run independent typed judgments concurrently, preserving input order."""
    if not states or not ENABLED or AsyncTypeSafeClient is None:
        return [None] * len(states)
    api_key = _load_key()
    if not api_key:
        return [None] * len(states)

    cache = _cache_load(cache_path)
    results: list[dict[str, Any] | None] = [None] * len(states)
    missing: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for idx, (cache_key, state, meta) in enumerate(states):
        hit = cache.get(cache_key)
        if isinstance(hit, dict):
            results[idx] = hit
        else:
            missing.append((idx, cache_key, state, meta))
    if not missing:
        return results

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(client: Any, idx: int, cache_key: str, state: dict[str, Any], meta: dict[str, Any]) -> None:
        async with sem:
            try:
                response = await client.system_one(state=state, questions=meta["questions"])
                item = meta["parse"](response)
                if isinstance(item, dict):
                    results[idx] = item
                    cache[cache_key] = item
            except Exception:
                # TypeSafe is an optimization layer, never a reason to lose a lead.
                results[idx] = None

    async with AsyncTypeSafeClient(api_key=api_key, model=MODEL) as client:
        await asyncio.gather(*(one(client, *item) for item in missing))
    _cache_save(cache_path, cache)
    return results


async def qualify_creator_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    categories = ["SaaS / AI app", "E-com / D2C", "CEX / биржа", "Проп трейдинг", "DeFi / DEX",
                  "Медиа / контент", "Gaming / AI", "Токен / мемкоин",
                  "Платёжки / финтех", "Web3 (общее)", "Сервис / агентство"]
    roles = ["Founder", "Marketing", "Community", "Creator/KOL", "Dev", "Trader", "Other"]
    states = []
    for row in rows:
        state = {k: row.get(k, "") for k in ("name", "username", "bio", "sample", "links", "project", "project_url", "what_they_do", "source", "github_handle", "founder_x", "founder_linkedin")}
        if not state.get("source"):
            state["source"] = row.get("group") or ""
        states.append((_key(state), state, {"questions": {
            "fit": Noul(instructions="Is this a buyer for short-form user acquisition: a founder, owner, marketing/growth lead, or a real monetized B2C/prosumer product discovered via ads/stores/launches whose decision-maker will be contacted? High for an AI SaaS, consumer subscription app (language learning, note taker, astrology, fitness, productivity, flashcards), Shopify/Salla/Zid/D2C e-commerce brand (skincare, cosmetics, perfume, supplements), fintech, prop firm, mobile/gaming product, or large aesthetic, dental, dermatology, telehealth, lab-testing, and multi-location medical clinics / health-tech brands (especially in GCC/MENA with active ads or brand scale) naturally advertised with TikTok/Reels/Shorts — including active Meta Ads / App Store / YC / Whop product rows that do not yet carry a personal Telegram username. The source chat and sample message are context, not proof. Return low for agencies, consultancies, pure B2D developer CLIs/SDKs, robotics/industrial hardware, universities, non-profits, pure retail traders, signal callers, investors, job seekers, UGC freelancers with no product, visa/relocation chatter, local classifieds, small solo local salon/barbershop, OTC, spammer, or empty profile."),
            "category": Choice(instructions="Which category best describes the person's actual product/project?", criteria={x: None for x in categories}),
            "role": Choice(instructions="Which role best describes this person in relation to the product/project? Treat the public owner/maintainer of a shipped product with a direct social profile, or an official brand/Meta Ads/App Store/Shopify/YC product entry without a separate personal bio, as Founder or Marketing when no stronger evidence says otherwise; do not label them Dev or Other only because the project is technical or brand-facing.", criteria={x: None for x in roles}),
        }, "parse": lambda r: {
            "typesafe_fit": _prob(_answer(r, "fit")),
            "typesafe_category": _choice(_answer(r, "category")),
            "typesafe_role": _choice(_answer(r, "role")),
        }}))
    return await _run(states, CREATOR_CACHE)


async def qualify_forex_channels(rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    states = []
    for row in rows:
        state = {k: row.get(k, "") for k in ("channel", "name", "about", "subs", "youtube", "lichka")}
        states.append((_key(state), state, {"questions": {
            "fit": Noul(instructions="Does this Telegram channel clearly reach an audience of forex, crypto, trading, investing, or finance users and look useful for a broker partnership or influencer deal? Low for a generic service, signal-only spam, exchange listing, job board, or unrelated channel."),
            "creator": Noul(instructions="Is there evidence this is a creator, analyst, educator, media channel, or owner with a real audience rather than a generic service account?"),
        }, "parse": lambda r: {
            "typesafe_fit": _prob(_answer(r, "fit")),
            "typesafe_creator": _prob(_answer(r, "creator")),
        }}))
    return await _run(states, FOREX_CACHE)


async def qualify_identity_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Use Jev as a second, semantic identity gate after exact Telegram lookup.

    ``rows`` should contain the public lead fields plus the resolved Telegram
    candidate fields (preferably produced by ``identity_enrichment.candidate_state``).
    This intentionally does not resolve usernames itself: network identity
    checks stay deterministic in Telethon, while Jev judges the evidence and
    prevents a coincidental shared handle from becoming a contact.
    """
    states = []
    for row in rows:
        state = {
            "lead_name": row.get("lead_name", row.get("name", "")),
            "candidate_username": row.get("candidate_username", row.get("telegram_username", "")),
            "match_type": row.get("match_type", ""),
            "evidence": row.get("evidence", []),
            "source_platforms": row.get("source_platforms", []),
            "telegram_name": row.get("telegram_name", ""),
            "telegram_username": row.get("telegram_username", ""),
            "name_similarity": row.get("name_similarity", 0),
            "verification_status": row.get("verification_status", ""),
        }
        states.append((_key(state), state, {"questions": {
            "same_person": Noul(instructions=(
                "Is the resolved Telegram profile likely the same person or creator as the public lead, "
                "using only the supplied evidence? Exact shared handles are not enough when the handle is "
                "generic; explicit Telegram links and matching display names are strong evidence."
            )),
            "safe_contact": Noul(instructions=(
                "Is this a safe direct outreach contact for the lead rather than a generic bot, support account, "
                "news channel, exchange brand, or unrelated profile?"
            )),
        }, "parse": lambda r: {
            "typesafe_same_person": _prob(_answer(r, "same_person")),
            "typesafe_safe_contact": _prob(_answer(r, "safe_contact")),
        }}))
    return await _run(states, IDENTITY_CACHE)


def identity_filter(result: dict[str, Any] | None) -> str:
    """Route Jev's identity judgment without dropping uncertain contacts."""
    if not result:
        return "uncertain"
    same = float(result.get("typesafe_same_person", 0.5))
    safe = float(result.get("typesafe_safe_contact", 0.5))
    if same <= 0.18 or safe <= 0.18:
        return "drop"
    if same >= 0.68 and safe >= 0.60:
        return "keep"
    return "uncertain"


def creator_filter(result: dict[str, Any] | None, *, has_product_url: bool = False) -> str:
    """Return ``keep``, ``drop`` or ``uncertain`` for strict Product-Buyer routing."""
    if not result:
        return "uncertain"
    category = str(result.get("typesafe_category", ""))
    if is_rejected_buyer_category(category):
        return "drop"
    fit = float(result.get("typesafe_fit", 0.5))
    if has_product_url:
        if fit <= 0.15:
            return "drop"
    elif fit <= float(os.getenv("TYPESAFE_CREATOR_DROP", "0.30")):
        return "drop"
    role = str(result.get("typesafe_role", ""))
    if role in {"Community", "Creator/KOL", "Dev", "Trader", "Other"}:
        if has_product_url:
            return "uncertain"
        return "drop" if fit < 0.45 else "uncertain"
    target_role = role in {"Founder", "Marketing"}
    if fit >= float(os.getenv("TYPESAFE_CREATOR_KEEP", "0.60")) and target_role:
        return "keep"
    return "uncertain"
