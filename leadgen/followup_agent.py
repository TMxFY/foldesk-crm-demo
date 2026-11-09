#!/usr/bin/env python3
"""LLM Follow-up Agent + Humanizer + Quality Validator for CreatorOS CSV exports.

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
    from relay_route import chain, note_failure
except ImportError:
    try:
        from leadgen.relay_route import chain, note_failure
    except ImportError:
        from scripts.relay_route import chain, note_failure

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_DIR = Path(
    os.environ.get("LEADGEN_STATE_DIR")
    or (
        "/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
        if os.path.isdir("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
        else str(ROOT / "data" / "leadgen_state")
    )
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
   - Do NOT repeat "183.5M" or "SoftUGC".

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
    name = (raw_name or "").strip()
    name = re.sub(r"^\d+[\.\)]\s*", "", name).strip()
    name = re.sub(r"\s*[\(\|].*$", "", name).strip()

    if name and name == name.lower() and " " not in name:
        angle_head = (their_angle or "").strip()[:80]
        m = re.match(r"^([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+)?)\b", angle_head)
        if m:
            candidate = m.group(1).strip()
            if re.sub(r"[^a-z0-9]", "", candidate.lower()) == re.sub(r"[^a-z0-9]", "", name.lower()):
                return candidate
        if len(name) <= 18:
            return name[:1].upper() + name[1:]

    if not name or name.startswith("@") or len(name) > 45:
        if site:
            host = (urlparse(site if "://" in site else f"https://{site}").hostname or "").lower().removeprefix("www.")
            base = host.split(".")[0] if host else ""
            if base and base not in {"t", "x", "twitter", "linkedin", "itunes", "apps", "play"}:
                return base[:1].upper() + base[1:]
        return ""
    return name


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
    text = re.sub(r"\bSoftUGC\b", "our team", text, flags=re.I)
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
        if "183.5" in txt or "183,5" in txt or "softugc" in txt.casefold():
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

    target = f"for {prod}" if prod else "for your product"
    prod_ref = prod if prod else "your product"

    if "hiring" in (first_touch or "").lower() or ("creator" in angle_l and ("new york" in angle_l or "remote" in angle_l or "united states" in angle_l)):
        f1 = (
            f"Since you're hiring on the content side, we could run 20+ short-form variations a week "
            f"across our account network {target} while you fill the role. Want to see 2 examples from your niche?"
        )
    elif "active meta video ads" in angle_l or "running active video ads" in (first_touch or "").lower():
        f1 = (
            f"Saw the video ads you're running right now. We could spin up fresh AI UGC variations and "
            f"organic clips across 10 pages {target} so your winning angles don't burn out. Open to seeing 2 examples?"
        )
    elif "медиа" in cat_l or "podcast" in angle_l or "youtube" in angle_l or "episode" in angle_l:
        f1 = (
            f"Quick idea {target}: we can take your existing long-form episodes and turn them into a daily "
            f"clip stream across 10 dedicated Shorts and Reels accounts. Want me to cut one sample clip as a demo?"
        )
    elif "проп" in cat_l or "prop" in angle_l or "cex" in cat_l or "defi" in cat_l or "trading" in angle_l or "wallet" in angle_l:
        f1 = (
            f"One format working really well in trading and fintech right now is short breakdown clips "
            f"posted across a 10-account network pointing straight to {prod_ref}. Want to see 2 examples?"
        )
    elif "game" in angle_l or "gaming" in cat_l or "roblox" in angle_l or "fivem" in angle_l or "browser" in angle_l:
        f1 = (
            f"Quick idea {target}: daily gameplay moments and build clips across TikTok and Shorts "
            f"usually bring in the cheapest organic players. Want me to send over 2 reference accounts?"
        )
    else:
        openers = (
            f"Quick idea {target}: 15-second workflow demos posted across 10 niche TikTok and Reels pages pointing straight to {prod_ref}. Want to see 2 examples?",
            f"Was looking at {prod_ref} again. Short problem-to-solution clips across a 10-account network would convert super cleanly here, want me to send 2 sample angles?",
            f"One format that works really well for products like {prod_ref} is short daily screen demos across dedicated Shorts and Reels pages. Worth testing a batch?",
        )
        f1 = openers[seed % len(openers)]

    f2_options = (
        f"Are you guys testing any new acquisition channels {target} this month, or mostly heads-down on product right now?",
        f"Let me know if getting more short-form volume {target} is a priority right now, we can spin up a test batch pretty quickly.",
        f"Open to a quick 10-minute chat this week to see how the account network would work {target}, or bad timing?",
    )
    f2 = f2_options[seed % len(f2_options)]
    return _sanitize_followup(f1, raw_name, prod), _sanitize_followup(f2, raw_name, prod)


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
    """Return validated, humanized [(followup_1, followup_2), ...] for 15-col sheet rows."""
    if not rows:
        return []
    if use_llm is None:
        use_llm = os.environ.get("CSV_NOTIFY_LLM", "1").lower() not in {"0", "false", "no"} and bool(
            os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY")
        )

    cache = _load_cache(cache_path) if use_llm else {}
    results: list[tuple[str, str] | None] = [None] * len(rows)
    uncached_indices: list[int] = []
    uncached_items: list[dict] = []

    for idx, row in enumerate(rows):
        cat = str(row[3] if len(row) > 3 else "")
        name = str(row[4] if len(row) > 4 else "")
        site = str(row[5] if len(row) > 5 else "")
        their_angle = str(row[7] if len(row) > 7 else "")
        our_angle = str(row[8] if len(row) > 8 else "")
        first_touch = str(row[10] if len(row) > 10 else "")
        clean_name = clean_product_name(name, site, their_angle)
        ck = _cache_key(name, site, their_angle, first_touch)
        if use_llm and ck in cache:
            f1 = _sanitize_followup(cache[ck][0], name, clean_name)
            f2 = _sanitize_followup(cache[ck][1], name, clean_name)
            if not validate_followup_pair(f1, f2, raw_name=name, first_touch=first_touch):
                results[idx] = (f1, f2)
                continue
        if use_llm:
            uncached_indices.append(idx)
            uncached_items.append({
                "i": idx,
                "name": name,
                "clean_name": clean_name,
                "site": site,
                "category": cat,
                "their_angle": their_angle[:350],
                "our_angle": our_angle[:250],
                "first_touch": first_touch[:350],
            })
        else:
            results[idx] = smart_fallback_followups(name, site, cat, their_angle, our_angle, first_touch)

    if use_llm and uncached_items:
        batches = [
            uncached_items[start : start + BATCH_SIZE]
            for start in range(0, len(uncached_items), BATCH_SIZE)
        ]
        workers = min(len(batches), 4)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            batch_maps = list(ex.map(_call_llm_batch, batches))
        merged: dict[int, tuple[str, str]] = {}
        for bm in batch_maps:
            merged.update(bm)
        cache_dirty = False
        for idx in uncached_indices:
            row = rows[idx]
            cat = str(row[3] if len(row) > 3 else "")
            name = str(row[4] if len(row) > 4 else "")
            site = str(row[5] if len(row) > 5 else "")
            their_angle = str(row[7] if len(row) > 7 else "")
            our_angle = str(row[8] if len(row) > 8 else "")
            first_touch = str(row[10] if len(row) > 10 else "")
            if idx in merged:
                results[idx] = merged[idx]
                ck = _cache_key(name, site, their_angle, first_touch)
                cache[ck] = [merged[idx][0], merged[idx][1]]
                cache_dirty = True
            else:
                results[idx] = smart_fallback_followups(name, site, cat, their_angle, our_angle, first_touch)
        if cache_dirty:
            _save_cache(cache, cache_path)

    return [
        r if r is not None else ("", "")
        for r in results
    ]
