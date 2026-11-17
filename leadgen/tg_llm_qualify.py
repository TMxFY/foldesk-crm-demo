#!/usr/bin/env python3
"""LLM-qualify enriched TG leads via Relay Fast (batched).

Replaces the dumb regex gating: for each lead the LLM looks at bio/name/username
and decides if this is a real buyer (founder/growth/marketing/BD with a product
that needs traffic), plus re-classifies category/role/project.

Input:  enriched JSONL (from tg_enrich_leads.py)
Output: qualified JSONL — only is_lead=true, with updated
        category/role/roles/project/project_url/what_they_do.

Env (.env in project root):
    LLM_BASE_URL            https://relay.fast/v1
    RELAYFAST_API_KEY       Relay Fast key (LLM_API_KEY is accepted too)
    LEADGEN_LLM_MODEL       primary model for lead-gen (default: claude-sonnet-5;
                            ladder: claude-haiku-5-5, grok, flash models; kimi-k3
                            is not used)
    LLM_CONCURRENCY         parallel first_touch calls (default: 12)
"""
import argparse
import json
import os
import re
import sys

import requests

try:
    from typesafe_leads import qualify_creator_rows, creator_filter
except Exception:
    try:
        from leadgen.typesafe_leads import qualify_creator_rows, creator_filter
    except Exception:
        qualify_creator_rows = creator_filter = None

try:
    from relay_route import chain, note_failure
except ImportError:
    try:
        from leadgen.relay_route import chain, note_failure
    except ImportError:
        from scripts.relay_route import chain, note_failure

try:
    from supply_side import (
        NON_BUYER_ROLES,
        has_insufficient_product_info,
        is_invalid_product_name,
        is_rejected_buyer_category,
        is_truncated_or_bad_pitch,
        supply_reason,
    )
except ImportError:
    try:
        from leadgen.supply_side import (
            NON_BUYER_ROLES,
            has_insufficient_product_info,
            is_invalid_product_name,
            is_rejected_buyer_category,
            is_truncated_or_bad_pitch,
            supply_reason,
        )
    except ImportError:
        from scripts.supply_side import (
            NON_BUYER_ROLES,
            has_insufficient_product_info,
            is_invalid_product_name,
            is_rejected_buyer_category,
            is_truncated_or_bad_pitch,
            supply_reason,
        )

if os.environ.get("CRMTG_SKIP_DOTENV") != "1":
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:
        # minimal .env parse fallback
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE_URL = (os.getenv("LLM_BASE_URL") or "https://relay.fast/v1").rstrip("/")
API_KEY = os.getenv("RELAYFAST_API_KEY") or os.getenv("LLM_API_KEY") or ""

BATCH_SIZE = 12
TIMEOUT = 90

CATEGORIES = ["SaaS / AI app", "CEX / биржа", "Проп трейдинг", "DeFi / DEX",
              "Медиа / контент", "Gaming / AI", "Токен / мемкоин",
              "Платёжки / финтех", "Web3 (общее)", "Сервис / агентство"]
ROLES = ["Founder", "Marketing", "Community", "Creator/KOL", "Dev", "Trader", ""]

SYSTEM_PROMPT = """You qualify Telegram leads for SoftUGC.

SoftUGC sells organic short-form traffic (clipping + AI/UGC, paid short-form ads, physical products, 183.5M views in 90 days, revshare/CPA deals) to founders/growth/marketing of startups.

Target categories: SaaS/AI apps, CEX/exchanges, prop trading, media/content, DeFi/DEX, Gaming/AI, payments/fintech, tokens/memecoins.
Target roles (buyers): Founder, Marketing.
NOT customers: regular users with no project, spammers, pure traders/investors with no product, freelancers/UGC creators/ambassadors with no own product, community moderators/KOL managers/shillers, devs with no product, custom dev/AI/SEO/marketing/GTM agencies or consultancies selling services to clients (Сервис / агентство), open-source libraries/build tools, CSS/coding quizzes, and free fan hobby games without monetization.

For EACH lead in the input array decide:
- is_lead: true ONLY if this person is a founder/CEO/CMO/growth/marketing lead with a REAL product/project that needs user traffic. False for pure traders, investors, job seekers, freelancers, community mods, empty/nonsense bios, spammers, devs without a product, custom dev/AI/SEO/marketing/GTM agencies (Сервис / агентство), open-source tools, coding quizzes, or free hobby games.
- category: exactly one of [SaaS / AI app, CEX / биржа, Проп трейдинг, DeFi / DEX, Медиа / контент, Gaming / AI, Токен / мемкоин, Платёжки / финтех, Web3 (общее), Сервис / агентство]
- role: exactly one of [Founder, Marketing, Community, Creator/KOL, Dev, Trader, ""]
- project: project/company name or ""
- project_url: website URL if present in bio/links (not t.me/twitter/linkedin), else ""
- what_they_do: short description, max 90 chars, language of the bio

Respond with STRICT JSON only: an array of objects in the SAME ORDER as input, one per lead:
[{"is_lead": bool, "category": "...", "role": "...", "project": "...", "project_url": "...", "what_they_do": "..."}, ...]
No markdown, no commentary, just the JSON array. The array length MUST equal the input length."""


def lead_view(i, r):
    return {
        "n": i,
        "name": r.get("name") or "",
        "username": r.get("username") or "",
        "bio": (r.get("bio") or "")[:400],
        "links": [l for l in (r.get("links") or []) if isinstance(l, str)][:5],
    }


def call_llm(batch, model):
    """Send one batch to Relay Fast; return parsed list or raise."""
    api_key = (os.getenv("RELAYFAST_API_KEY") or os.getenv("LLM_API_KEY") or API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("RELAYFAST_API_KEY / LLM_API_KEY not configured")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                [lead_view(i, r) for i, r in enumerate(batch)], ensure_ascii=False)},
        ],
        "temperature": 0,
    }
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # strip markdown fences if the model wraps anyway
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
    # find the JSON array even if there's stray text
    m = re.search(r"\[.*\]", content, flags=re.DOTALL)
    if not m:
        raise ValueError(f"no JSON array in response: {content[:200]}")
    return json.loads(m.group(0))


def call_llm_retry(batch):
    last = None
    models = chain()
    for index, model in enumerate(models):
        try:
            if index:
                print(f"    using model {model}", file=sys.stderr)
            return call_llm(batch, model)
        except requests.HTTPError as exc:
            last = exc
            response = exc.response
            status = response.status_code if response is not None else 0
            body = (response.text or "")[:400] if response is not None else ""
            if note_failure(model, status, body):
                print(f"    {model} unavailable ({status}), next model", file=sys.stderr)
                continue
            raise
        except (requests.Timeout, requests.ConnectionError, ValueError) as exc:
            last = exc
            if note_failure(model, 0, ""):
                print(f"    {model} failed ({exc.__class__.__name__}), next model", file=sys.stderr)
                continue
            raise
    raise last


def norm_result(res):
    """Coerce one LLM result into a safe dict; None if unusable."""
    if not isinstance(res, dict):
        return None
    cat = res.get("category") if res.get("category") in CATEGORIES else "Web3 (общее)"
    role = res.get("role") if res.get("role") in ROLES else ""
    what = str(res.get("what_they_do") or "")[:90]
    url = str(res.get("project_url") or "")
    if url and not url.startswith(("http://", "https://")):
        url = ""
    return {
        "is_lead": bool(res.get("is_lead")),
        "category": cat,
        "role": role,
        "project": str(res.get("project") or "")[:80],
        "project_url": url,
        "what_they_do": what,
    }


_VOLUNTEER_OR_FOR_HIRE_RE = re.compile(
    r"\[for\s*hire\]|\bfor\s+hire\b|\[volunteer|\bvolunteers?\b|\bunpaid\b",
    re.I,
)


def lead_text(row):
    parts = []
    for key in ("name", "username", "bio", "sample", "text", "what_they_do", "their_angle", "project", "company"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _product_reality_reject_reason(row: dict) -> str:
    cat = str(row.get("category") or "").strip()
    if is_rejected_buyer_category(cat):
        return f"rejected_category:{cat}"
    role = str(row.get("role") or "").strip()
    if role and role in NON_BUYER_ROLES:
        return f"non_buyer_role:{role}"
    proj = str(row.get("project") or row.get("company") or "").strip()
    if is_invalid_product_name(proj):
        return f"invalid_product:{proj!r}"
    for angle_key in ("what_they_do", "their_angle", "our_angle"):
        if has_insufficient_product_info(row.get(angle_key)):
            return f"insufficient_product_info:{angle_key}"
    raw_pitch = row.get("first_touch") if row.get("first_touch") is not None else row.get("step1_text")
    if raw_pitch is not None and str(raw_pitch).strip() != "" and is_truncated_or_bad_pitch(str(raw_pitch)):
        return "truncated_or_bad_pitch"
    return ""


def gate_before_model(rows):
    """Drop supply-side, volunteer/[FOR HIRE], and known agency rows across ALL sources."""
    kept, notes = [], []
    for row in rows or []:
        blob = lead_text(row)
        reason = supply_reason(blob)
        if not reason:
            vm = _VOLUNTEER_OR_FOR_HIRE_RE.search(blob)
            if vm:
                reason = vm.group(0)
        if not reason and is_rejected_buyer_category(row.get("category")):
            reason = f"category={row.get('category')}"
        if not reason and has_insufficient_product_info(blob):
            reason = "insufficient_product_info"
        raw_pitch = row.get("first_touch") if row.get("first_touch") is not None else row.get("step1_text")
        if not reason and raw_pitch is not None and str(raw_pitch).strip() != "" and is_truncated_or_bad_pitch(str(raw_pitch)):
            reason = "truncated_pitch"
        if reason:
            notes.append((row, reason))
            continue
        kept.append(row)
    return kept, notes


def _quarantine_row(row: dict, reason: str, quarantine_list: list[dict]) -> None:
    q = dict(row)
    q["qualified"] = False
    q["quarantine_reason"] = reason
    quarantine_list.append(q)


def qualify_loaded(rows, *, batch_size, quarantine_out: list[dict] | None = None):
    original = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    input_total = len(original)
    quarantine: list[dict] = quarantine_out if quarantine_out is not None else []

    kept, notes = gate_before_model(original)
    dropped = 0
    for row, reason in notes:
        dropped += 1
        _quarantine_row(row, f"pre_gate:{reason}", quarantine)
        print(f"    - supply/pre-gate: {row.get('name')} (@{row.get('username') or '-'}) {reason}",
              file=sys.stderr)
    rows = kept
    print(f"Qualifying {input_total} leads in batches of {batch_size} "
          f"(route {' → '.join(chain())})", file=sys.stderr)

    out = []
    fast_path = os.getenv("TYPESAFE_FAST_PATH", "1").lower() in {"1", "true", "yes"}
    ts_results = []
    if rows and qualify_creator_rows and os.getenv("TYPESAFE_ENABLED", "1").lower() not in {"0", "false", "no"}:
        try:
            import asyncio
            ts_results = asyncio.run(qualify_creator_rows(rows))
            print(f"TypeSafe judgments: {sum(x is not None for x in ts_results)}/{len(rows)}", file=sys.stderr)
        except Exception as exc:
            print(f"TypeSafe unavailable; using Relay Fast for all rows ({exc})", file=sys.stderr)
            ts_results = [None] * len(rows)

    routed = []
    for row, ts in zip(rows, ts_results or [None] * len(rows)):
        route = creator_filter(ts) if creator_filter else "uncertain"
        if route == "drop":
            dropped += 1
            _quarantine_row(row, "typesafe_drop", quarantine)
            continue
        row = dict(row)
        if ts and route == "keep" and fast_path:
            ts_cat = str(ts.get("typesafe_category") or row.get("category") or "")
            ts_role = str(ts.get("typesafe_role") or row.get("role") or "")
            if is_rejected_buyer_category(ts_cat) or (ts_role and ts_role in NON_BUYER_ROLES):
                dropped += 1
                _quarantine_row(row, f"fast_path_reject:{ts_cat}:{ts_role}", quarantine)
                continue
            row["typesafe_qualified"] = True
            row["qualified"] = True
            row["typesafe_fit"] = round(float(ts.get("typesafe_fit", 0.0)), 4)
            if ts_cat:
                row["category"] = ts_cat
            if ts_role and ts_role != "Other":
                row["role"] = ts_role
                row["roles"] = [ts_role]
            bad_reason = _product_reality_reject_reason(row)
            if bad_reason:
                routed.append(row)
                continue
            out.append(row)
            continue
        routed.append(row)

    rows = routed
    total = len(rows)
    api_key = (os.getenv("RELAYFAST_API_KEY") or os.getenv("LLM_API_KEY") or API_KEY or "").strip()
    if total and not api_key:
        print("ERROR: RELAYFAST_API_KEY not set; fail-closed quarantining uncertain rows", file=sys.stderr)
        for r in rows:
            dropped += 1
            _quarantine_row(r, "missing_llm_api_key", quarantine)
        rows = []
        total = 0

    print(f"TypeSafe primary: {len(out)} KEEP, {dropped} DROP; Relay Fast recheck {total} UNCERTAIN"
          + (" (fast-path enabled)" if fast_path else " (legacy all-candidate mode)"), file=sys.stderr)
    for start in range(0, total, batch_size):
        batch = rows[start:start + batch_size]
        bno = start // batch_size + 1
        nbatches = (total + batch_size - 1) // batch_size
        try:
            results = call_llm_retry(batch)
        except Exception as e:
            print(f"  batch {bno}/{nbatches} FAILED ({e}); fail-closed quarantining batch",
                  file=sys.stderr)
            for r in batch:
                dropped += 1
                _quarantine_row(r, f"llm_batch_error:{e.__class__.__name__}", quarantine)
            continue

        if not isinstance(results, list) or len(results) != len(batch):
            got_len = len(results) if isinstance(results, list) else -1
            print(f"  batch {bno}: length mismatch {got_len} != {len(batch)}; fail-closed quarantining batch",
                  file=sys.stderr)
            for r in batch:
                dropped += 1
                _quarantine_row(r, "llm_length_mismatch", quarantine)
            continue

        for r, res in zip(batch, results):
            nr = norm_result(res) if res else None
            if nr is None:
                dropped += 1
                _quarantine_row(r, "unparseable_llm_item", quarantine)
                continue
            if not nr["is_lead"] or is_rejected_buyer_category(nr["category"]):
                dropped += 1
                _quarantine_row(r, "not_buyer_or_agency", quarantine)
                print(f"    - drop: {r.get('name')} (@{r.get('username') or '-'})",
                      file=sys.stderr)
                continue
            r2 = dict(r)
            r2.update({
                "category": nr["category"],
                "role": nr["role"],
                "roles": [nr["role"]] if nr["role"] else [],
                "project": nr["project"] or r.get("project") or "",
                "project_url": nr["project_url"] or r.get("project_url") or "",
                "what_they_do": nr["what_they_do"] or r.get("what_they_do") or "",
                "llm_qualified": True,
                "qualified": True,
            })
            bad_reason = _product_reality_reject_reason(r2)
            if bad_reason:
                dropped += 1
                _quarantine_row(r2, bad_reason, quarantine)
                continue
            out.append(r2)
        print(f"  batch {bno}/{nbatches}: kept {len(out)} so far, dropped {dropped}",
              file=sys.stderr)

    state_dir = os.getenv("LEADGEN_STATE_DIR", "").strip()
    if quarantine and state_dir and os.path.isdir(state_dir):
        q_path = os.path.join(state_dir, "quarantined_unqualified.jsonl")
        try:
            with open(q_path, "a", encoding="utf-8") as qfh:
                for qrow in quarantine:
                    qfh.write(json.dumps(qrow, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"# warning: could not write quarantine file {q_path}: {exc}", file=sys.stderr)

    print(f"Qualified {len(out)}/{input_total} leads (dropped {dropped})", file=sys.stderr)
    return out, dropped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--limit", type=int, default=0, help="debug limit")
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    out, _dropped = qualify_loaded(rows, batch_size=args.batch_size)
    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"-> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
