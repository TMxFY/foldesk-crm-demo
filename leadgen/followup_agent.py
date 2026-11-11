#!/usr/bin/env python3
"""LLM Follow-up Agent + Humanizer + Quality Validator for SoftUGC CSV exports.

Read-only copy generator: never sends messages directly and never bypasses CRM outreach guards.

Pipeline for each batch of leads:
1. Draft (`_call_llm_batch`): Generates product-specific `followup_1` (concrete
   video format idea) and `followup_2` (casual check-in on acquisition timing).
2. Humanize (`humanize_followups_batch`): Rewrites both follow-ups so they read
   like a real person typing on Telegram/LinkedIn — strips dashes (`—`, `–`,
   `--`, ` - `), kills repetitive AI phrasing ("sharp hook", "wondering if",
   "curious if", "happy to share a teardown"), and varies openings across leads.
3. Validate (`validate_followup_pair`): Hard quality gate verifying zero dashes,
   zero AI-slop phrases, zero uncleaned numbered names, language consistency,
   and length bounds. Any row that fails validation is repaired or replaced with
   a validated human fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from outreach_copy import clean_brand_name, followup_one, followup_two
except ImportError:
    try:
        from leadgen.outreach_copy import clean_brand_name, followup_one, followup_two
    except ImportError:
        from scripts.outreach_copy import clean_brand_name, followup_one, followup_two

try:
    from relay_route import chain, note_failure
except ImportError:
    try:
        from leadgen.relay_route import chain, note_failure
    except ImportError:
        from scripts.relay_route import chain, note_failure

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_DIR = Path(
    os.environ.get("LEADGEN_STATE_DIR")
    or str(ROOT / "data" / "leadgen_state")
)
CACHE_PATH = _DEFAULT_STATE_DIR / "followups_cache.json"
BATCH_SIZE = 10
TIMEOUT = 90

import sys
if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        env_p = ROOT / ".env"
        if env_p.exists():
            for _line in env_p.read_text(encoding="utf-8", errors="ignore").splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

SYSTEM_PROMPT = """You write short, human Telegram / LinkedIn follow-up DMs (followup_1 on Day 3, followup_2 on Day 7) for a short-form video growth team.

Context:
- We run organic short-form distribution (AI UGC + clipping + a 10+ account network on TikTok/Reels/Shorts, plus paid short-form ads).
- The lead already received our First Touch DM (which mentioned our 183.5M views case study).

Rules for EACH lead in the input JSON array:
1. Clean product name (`clean_name`):
   - Never keep list numbers ("7. Calendarly" -> "Calendarly").
   - Use proper casing ("rescreen" -> "ReScreen", "gielinorsurvivors" -> "Gielinor Survivors").
   - If `name` is a person's name, handle, or tweet snippet, refer to their product naturally ("your app", "your show", "your game").
2. Language:
   - If `first_touch` is in Russian, write 100% in natural conversational Russian (zero English buzzwords).
   - Otherwise write 100% in natural, plain English (zero Cyrillic).
3. `followup_1` (Day 3 — Concrete Video Idea, 1-2 short sentences, 20-42 words):
   - Pitch ONE specific short-form video idea that fits what their product actually does (from `their_angle`):
     * App / SaaS: a 15-sec screen/workflow demo showing the exact user moment (e.g., unlocking a phone to see live Todoist tasks on the wallpaper, or recording a tutorial where auto-zoom tracks the cursor).
     * Game: fast boss-fight / build clips on Shorts and TikTok sending players to the game page.
     * Prop firm / CEX / DeFi / Fintech: short trader breakdown or challenge payout clips driving referral signups.
     * Media / Podcast / YouTube: cutting their episodes/streams into daily clips across 10 accounts.
     * Hiring Content Creator (`linkedin_jobs`) or running ads (`meta_ads`): testing 20+ clip variations a week across our pages instead of relying on one hire or letting ad creatives fatigue.
   - Vary how you start the sentence across leads! Do NOT start every message with "For {Name}, a sharp/strong hook is...". Use natural openings like:
     * "Quick idea for {Name}: ..."
     * "Was looking at {Name} again, ..."
     * "One format that works really well for tools like {Name} is ..."
     * "Thought of a simple clip angle for {Name}: ..."
   - End with a natural, short question ("Want me to send 2 examples?", "Worth testing a cut like that?", "Should I send over a couple examples from your niche?").
4. `followup_2` (Day 7 — Casual Check-in, 1-2 short sentences, 14-30 words):
   - Sound like a real person checking in, NOT an AI sales bot.
   - Vary openings across leads! NEVER use "Wondering if...", "Curious if...", "Happy to do a quick teardown", or "on your roadmap".
   - Good human examples:
     * "Are you guys testing any new channels for {Name} this month, or mostly heads-down on product right now?"
     * "Let me know if getting more short-form volume for {Name} is a priority right now, we can spin up a test batch pretty fast."
     * "Still think {Name} would crush on Shorts and Reels. Open to a quick 10-min chat this week, or bad timing?"
5. STRICT BANS:
   - NO dashes of any kind: no em-dash (—), no en-dash (–), no double dash (--), no spaced hyphen ( - ). Use commas or periods only.
   - NO AI clichés: ban "sharp hook", "strong hook", "tight demo", "high-retention", "concrete video angles", "wondering if", "curious if", "happy to", "teardown", "on your roadmap", "plug into", "walk you through how we can get that", "organic promotion of".
   - Do NOT repeat "183.5M", "SoftUGC", or legacy "CreatorOS".

Respond with STRICT JSON only:
[{"i": 0, "followup_1": "...", "followup_2": "..."}, ...]"""

HUMANIZER_PROMPT = """You are a strict human editor cleaning up outbound Telegram/LinkedIn follow-up DMs so they read 100% like a real human typed them on a phone, with zero AI smell.

Rewrite each `followup_1` and `followup_2` in the input JSON array following these hard rules:
1. Keep the exact product name and the specific video idea/angle in `followup_1`, and the check-in intent in `followup_2`. Keep the same language (Russian stays Russian, English stays English).
2. REMOVE all dashes: no `—`, no `–`, no `--`, no ` - `. Replace them with a comma or a period.
3. REMOVE all AI-sounding phrases and cadence:
   - Never use: "sharp hook", "strong hook", "tight demo", "high-retention", "concrete video angles", "wondering if", "curious if", "happy to", "teardown", "on your roadmap", "plug into", "land well", "circling back", "bumping this", "walk you through", "organic promotion".
   - Do not start every `followup_1` with "For [Product], ...". Mix up the sentence structure across items so each row feels handwritten.
   - Do not end every `followup_1` with the same question. Vary between "Want to see 2 examples?", "Worth a quick test?", "Want me to send a similar example?", "Make sense to test this?".
4. Keep sentences short, plain, and conversational (1-2 sentences per follow-up). No quotes around slogans. No "183.5M" or "SoftUGC".

Respond with STRICT JSON only, same order:
[{"i": 0, "followup_1": "...", "followup_2": "..."}, ...]"""

_AI_SLOP_RE = re.compile(
    r"\b(?:"
    r"sharp hook|strong hook|tight demo|high-retention|concrete video angles|"
    r"wondering if|curious if|happy to share|happy to do a quick|quick teardown|"
    r"on your roadmap|plug into|walk you through how we can get that|"
    r"are you interested in organic promotion|circling back|just bumping|"
    r"in today's|delve|seamlessly|game-changer|land well"
    r")\b",
    re.I,
)
_DASH_RE = re.compile(r"(?:[—–]|--|\s-\s)")
_NUMBERED_PREFIX_RE = re.compile(r"\b\d+\.\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9]+")


def clean_product_name(raw_name: str, site: str = "", their_angle: str = "") -> str:
    """Turn raw sheet 'Название' ('7. Calendarly', 'gielinorsurvivors', '@user') into a clean product name."""
    return clean_brand_name(raw_name, site=site, their_angle=their_angle)


def _is_russian_lead(name: str, first_touch: str) -> bool:
    if first_touch and re.search(r"[А-Яа-яЁё]{3,}", first_touch):
        return True
    if not first_touch and re.search(r"[А-Яа-яЁё]{2,}", name or ""):
        return True
    return False


def _ru_share(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ("А" <= c <= "я") or c in "Ёё") / len(letters)


def _sanitize_followup(text: str, raw_name: str = "", clean_name: str = "") -> str:
    text = (text or "").strip().strip('"').strip("'")
    # Strip any numbered prefix if the model echoed raw_name
    if raw_name and clean_name and raw_name != clean_name:
        text = text.replace(raw_name, clean_name)
    text = re.sub(r"\b(\d+)\.\s+([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9]+)", r"\2", text)
    # Remove all em/en/double/spaced dashes
    text = re.sub(r"\s*(?:[—–]|--)\s*", ", ", text)
    text = re.sub(r"\s+-\s+", ", ", text)
    # Remove banned case / brand repeats
    text = re.sub(r"\b(?:SoftUGC|CreatorOS)\b", "our team", text, flags=re.I)
    text = re.sub(r"183[.,]5\s*M\+?", "high-volume", text, flags=re.I)
    # Clean up double punctuation and extra whitespace
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"([.!?]),", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def validate_followup_pair(
    f1: str,
    f2: str,
    *,
    raw_name: str = "",
    first_touch: str = "",
) -> list[str]:
    """Validate humanized follow-ups. Returns a list of problem strings ([] if valid)."""
    problems: list[str] = []
    for label, txt, min_len, max_len in (
        ("f1", f1 or "", 30, 300),
        ("f2", f2 or "", 20, 240),
    ):
        if len(txt) < min_len:
            problems.append(f"{label}_too_short")
        if len(txt) > max_len:
            problems.append(f"{label}_too_long")
        if _DASH_RE.search(txt):
            problems.append(f"{label}_has_dash")
        if _AI_SLOP_RE.search(txt):
            problems.append(f"{label}_has_ai_slop:{_AI_SLOP_RE.search(txt).group(0)}")
        if (
            "183.5" in txt
            or "183,5" in txt
            or "softugc" in txt.casefold()
            or "creatoros" in txt.casefold()
        ):
            problems.append(f"{label}_repeats_case_or_brand")
        if re.search(r"\[(?:name|product|founder|company)\]", txt, re.I):
            problems.append(f"{label}_has_placeholder")
        if _NUMBERED_PREFIX_RE.search(txt):
            problems.append(f"{label}_has_numbered_name")

    russian = _is_russian_lead(raw_name, first_touch)
    if russian:
        if _ru_share(f1) < 0.5 or _ru_share(f2) < 0.5:
            problems.append("not_russian")
    else:
        if re.search(r"[А-Яа-яЁё]", (f1 or "") + (f2 or "")):
            problems.append("unexpected_cyrillic")
    return problems


def smart_fallback_followups(
    raw_name: str,
    site: str = "",
    category: str = "",
    their_angle: str = "",
    our_angle: str = "",
    first_touch: str = "",
) -> tuple[str, str]:
    """Human-written, context-aware fallback that always passes validate_followup_pair."""
    russian = _is_russian_lead(raw_name, first_touch)
    prod = clean_product_name(raw_name, site, their_angle)
    cat_l = (category or "").lower()
    angle_l = f"{their_angle} {our_angle}".lower()
    seed = sum(ord(c) for c in (prod or raw_name or site or "a"))

    if russian:
        target = f"для {prod}" if prod else "под ваш продукт"
        if "медиа" in cat_l or "content" in angle_l or "youtube" in angle_l or "архив" in angle_l:
            f1 = (
                f"Есть идея {target}: взять ваши текущие выпуски и нарезать их в ежедневные короткие ролики "
                f"на 10 аккаунтов в Reels, Shorts и TikTok. Скинуть пару примеров таких нарезок?"
            )
        elif "проп" in cat_l or "cex" in cat_l or "бирж" in cat_l or "defi" in cat_l or "трейд" in angle_l:
            f1 = (
                f"В вашей нише лучше всего заходят короткие разборы сделок и выплат, которые ведут трейдеров "
                f"прямиком по реф-ссылке {target}. Показать 2 примера рабочих аккаунтов?"
            )
        elif "gaming" in cat_l or "игр" in angle_l or "roblox" in angle_l or "fivem" in angle_l:
            f1 = (
                f"Подумал про формат {target}: серия коротких геймплейных моментов и челленджей по сетке "
                f"аккаунтов в TikTok и Shorts дает самый дешевый поток игроков. Скинуть пару референсов?"
            )
        else:
            f1 = (
                f"Прикинул пару сценариев коротких роликов {target} с быстрым показом продукта в действии "
                f"для TikTok и Reels. Скинуть 2 примера того, как мы это снимаем?"
            )
        f2 = (
            f"Вы в этом месяце тестируете новые каналы трафика {target}, или сейчас фокус только на разработке? "
            f"Можем быстро созвониться на 10 минут и показать цифры по нише."
        )
        return _sanitize_followup(f1, raw_name, prod), _sanitize_followup(f2, raw_name, prod)

    return (
        followup_one(prod or raw_name, their_angle, russian=False, category=category, site=site),
        followup_two(prod or raw_name, russian=False),
    )


def _cache_key(name: str, site: str, their_angle: str, first_touch: str) -> str:
    raw = f"v2|{(name or '').strip()}|{(site or '').strip()}|{(their_angle or '')[:160]}|{(first_touch or '')[:160]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_cache(path: Path = CACHE_PATH) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): [str(v[0]), str(v[1])]
                for k, v in data.items()
                if isinstance(v, list) and len(v) == 2 and v[0] and v[1]
            }
    except Exception:
        pass
    return {}


def _save_cache(cache: dict[str, list[str]], path: Path = CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _post_relay_json(system_prompt: str, user_payload: list[dict], temperature: float = 0.4) -> list[dict]:
    base = (os.environ.get("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
    key = os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    if not key or not user_payload:
        return []
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    for model in chain():
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": temperature, "max_tokens": 2500},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            if note_failure(model, 0, ""):
                continue
            break
        if resp.status_code >= 400:
            if note_failure(model, resp.status_code, (resp.text or "")[:300]):
                continue
            break
        content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        if not isinstance(content, str) or not content.strip():
            if note_failure(model, 0, ""):
                continue
            break
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\[.*\]", cleaned, flags=re.DOTALL)
        if not match:
            if note_failure(model, 0, ""):
                continue
            break
        try:
            arr = json.loads(match.group(0))
            if isinstance(arr, list) and arr:
                return arr
        except ValueError:
            if note_failure(model, 0, ""):
                continue
            break
    return []


def humanize_followups_batch(drafts: list[dict]) -> dict[int, tuple[str, str]]:
    """Run the LLM humanizer pass over a batch of drafted follow-ups."""
    if not drafts:
        return {}
    arr = _post_relay_json(HUMANIZER_PROMPT, drafts, temperature=0.45)
    out: dict[int, tuple[str, str]] = {}
    for obj in arr:
        if not isinstance(obj, dict):
            continue
        try:
            idx = int(obj.get("i"))
        except (TypeError, ValueError):
            continue
        f1 = str(obj.get("followup_1") or "").strip()
        f2 = str(obj.get("followup_2") or "").strip()
        if f1 and f2:
            out[idx] = (f1, f2)
    return out


def _call_llm_batch(items: list[dict]) -> dict[int, tuple[str, str]]:
    """Stage 1 (Draft) + Stage 2 (Humanizer) + Stage 3 (Sanitize & Validate)."""
    arr = _post_relay_json(SYSTEM_PROMPT, items, temperature=0.4)
    if not arr:
        return {}
    by_i_meta = {int(it["i"]): it for it in items}
    drafts_for_humanizer: list[dict] = []
    raw_drafts: dict[int, tuple[str, str]] = {}

    for obj in arr:
        if not isinstance(obj, dict):
            continue
        try:
            idx = int(obj.get("i"))
        except (TypeError, ValueError):
            continue
        meta = by_i_meta.get(idx) or {}
        f1 = _sanitize_followup(str(obj.get("followup_1") or ""), meta.get("name", ""), meta.get("clean_name", ""))
        f2 = _sanitize_followup(str(obj.get("followup_2") or ""), meta.get("name", ""), meta.get("clean_name", ""))
        if len(f1) >= 25 and len(f2) >= 18:
            raw_drafts[idx] = (f1, f2)
            drafts_for_humanizer.append({
                "i": idx,
                "clean_name": meta.get("clean_name") or meta.get("name") or "",
                "followup_1": f1,
                "followup_2": f2,
            })

    humanized = humanize_followups_batch(drafts_for_humanizer) if drafts_for_humanizer else {}
    final_out: dict[int, tuple[str, str]] = {}

    for idx, (draft_f1, draft_f2) in raw_drafts.items():
        meta = by_i_meta.get(idx) or {}
        raw_name = meta.get("name", "")
        clean_name = meta.get("clean_name", "")
        first_touch = meta.get("first_touch", "")
        if idx in humanized:
            h1 = _sanitize_followup(humanized[idx][0], raw_name, clean_name)
            h2 = _sanitize_followup(humanized[idx][1], raw_name, clean_name)
            if not validate_followup_pair(h1, h2, raw_name=raw_name, first_touch=first_touch):
                final_out[idx] = (h1, h2)
                continue
        # Check if sanitized draft already passes validation
        if not validate_followup_pair(draft_f1, draft_f2, raw_name=raw_name, first_touch=first_touch):
            final_out[idx] = (draft_f1, draft_f2)

    return final_out


def generate_followups_for_rows(
    rows: list[list[str]],
    *,
    use_llm: bool | None = None,
    cache_path: Path = CACHE_PATH,
) -> list[tuple[str, str]]:
    """Return canonical Vlad B2B follow-ups [(followup_1, followup_2), ...] for 15-col sheet rows."""
    del use_llm, cache_path
    if not rows:
        return []
    out: list[tuple[str, str]] = []
    for row in rows:
        cat = str(row[3] if len(row) > 3 else "")
        name = str(row[4] if len(row) > 4 else "")
        site = str(row[5] if len(row) > 5 else "")
        their_angle = str(row[7] if len(row) > 7 else "")
        our_angle = str(row[8] if len(row) > 8 else "")
        first_touch = str(row[10] if len(row) > 10 else "")
        out.append(smart_fallback_followups(name, site, cat, their_angle, our_angle, first_touch))
    return out
