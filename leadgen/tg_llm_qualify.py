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
        sanitize_person_and_company,
        sniper_reject_reason,
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
            sanitize_person_and_company,
            sniper_reject_reason,
            supply_reason,
        )
    except ImportError:
        from scripts.supply_side import (
            NON_BUYER_ROLES,
            has_insufficient_product_info,
            is_invalid_product_name,
            is_rejected_buyer_category,
            is_truncated_or_bad_pitch,
            sanitize_person_and_company,
            sniper_reject_reason,
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

CATEGORIES = ["SaaS / AI app", "E-com / D2C", "CEX / биржа", "Проп трейдинг", "DeFi / DEX",
              "Медиа / контент", "Gaming / AI", "Токен / мемкоин",
              "Платёжки / финтех", "Web3 (общее)", "Сервис / агентство"]
ROLES = ["Founder", "Marketing", "Community", "Creator/KOL", "Dev", "Trader", ""]
VALID_MONETIZATIONS = ("subscription", "ecom", "iap", "cpa_deposit", "paid_product", "none")
MIN_ICP_SCORE = 80

SYSTEM_PROMPT = """You qualify B2B/consumer product leads for SoftUGC (Sniper ICP Gate >= 80).

SoftUGC sells organic short-form video user acquisition (AI UGC + clipping across owned account networks, 183.5M views in 90 days, revshare/CPA deals) ONLY to founders, CMOs, and marketing/growth leads who own a MONETIZED product with short-form viral potential.

Golden ICP clusters (icp_score 80..100):
1. Consumer / Prosumer SaaS & AI subscription apps (coaching, astrology, language learning, fitness, productivity, video/creator tools).
2. Gaming / Roblox / Fortnite / FiveM / mobile & PC games with IAP, passes, or paid items.
3. E-com / Shopify D2C brands (cosmetics/skincare, supplements/vitamins, household products).
4. Crypto B2C wallets, portfolio trackers, exchanges, fintech cards, and prop trading challenges.

REJECT (is_lead=false, icp_score < 80, or monetization="none"):
- Agencies, PR firms (e.g. FINPR, Marketacross), GTM/marketing/SEO/dev consultancies, outsource studios, recruiters, B2B sales agencies, targetologists, mentors, storymakers ("Сервис / агентство").
- Bot hosting / managed server hosting, pure B2D dev-only CLI/SDKs, open-source repos/tools without paid product monetization, ethical hacking/recovery APIs, coding quizzes, free hobby browser games.
- Forum job seekers, freelancers, 3D modeler / Roblox builder recruitment threads, pure traders/investors, community mods.

For EACH lead in the input array decide:
- is_lead: true ONLY if Founder/Marketing/Growth of an own monetized product needing short-form traffic
- icp_score: integer 0..100 (80+ ONLY for verified monetized products in target clusters)
- monetization: one of ["subscription", "ecom", "iap", "cpa_deposit", "paid_product", "none"]
- short_form_fit: bool (true if product can convert from TikTok/Reels/Shorts)
- category: one of [SaaS / AI app, E-com / D2C, CEX / биржа, Проп трейдинг, DeFi / DEX, Медиа / контент, Gaming / AI, Токен / мемкоин, Платёжки / финтех, Web3 (общее), Сервис / агентство]
- role: one of [Founder, Marketing, Community, Creator/KOL, Dev, Trader, ""]
- project: clean product/brand name (strip "Show HN:", numbering, URLs, human names) or ""
- project_url: product website URL (not t.me/twitter/linkedin), else ""
- what_they_do: short description, max 90 chars, language of the bio

Respond with STRICT JSON only: an array of objects in the SAME ORDER as input:
[{"is_lead": bool, "icp_score": 0, "monetization": "subscription", "short_form_fit": bool, "category": "...", "role": "...", "project": "...", "project_url": "...", "what_they_do": "..."}, ...]
No markdown, no commentary, just the JSON array. The array length MUST equal the input length."""


def lead_view(i, r):
    return {
        "n": i,
        "name": r.get("name") or "",
        "username": r.get("username") or "",
        "bio": (r.get("bio") or "")[:400],
        "project": r.get("project") or r.get("company") or "",
        "project_url": r.get("project_url") or "",
        "what_they_do": (r.get("what_they_do") or "")[:200],
        "has_monetization_signal": bool(r.get("has_monetization_signal")),
        "has_agency_signal": bool(r.get("has_agency_signal")),
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


def _default_monetization_for_category(cat: str, is_lead: bool) -> str:
    if not is_lead:
        return "none"
    if cat == "SaaS / AI app":
        return "subscription"
    if cat == "E-com / D2C":
        return "ecom"
    if cat == "Gaming / AI":
        return "iap"
    if cat in {"Проп трейдинг", "CEX / биржа", "DeFi / DEX", "Платёжки / финтех"}:
        return "cpa_deposit"
    return "paid_product"


_ECOM_CATEGORY_ALIASES = frozenset({"e-com", "ecom", "ecommerce", "e-commerce", "d2c", "dtc", "shopify", "e-com / d2c", "shopify / d2c"})


def norm_result(res):
    """Coerce one LLM result into a safe dict; None if unusable."""
    if not isinstance(res, dict):
        return None
    is_lead = bool(res.get("is_lead"))
    raw_cat = str(res.get("category") or "").strip()
    raw_mon = res.get("monetization")
    mon_norm_preview = str(raw_mon or "").strip().lower()
    if raw_cat.lower() in _ECOM_CATEGORY_ALIASES or (mon_norm_preview == "ecom" and raw_cat in ("", "Web3 (общее)", "Медиа / контент")):
        cat = "E-com / D2C"
    elif raw_cat in CATEGORIES:
        cat = raw_cat
    else:
        cat = "Web3 (общее)"
    role = res.get("role") if res.get("role") in ROLES else ""
    what = str(res.get("what_they_do") or "")[:90]
    url = str(res.get("project_url") or "")
    if url and not url.startswith(("http://", "https://")):
        url = ""

    if "icp_score" not in res or res.get("icp_score") is None:
        icp_score = 85 if is_lead else 0
    else:
        try:
            icp_score = max(0, min(100, int(round(float(res["icp_score"])))))
        except (TypeError, ValueError):
            icp_score = 0

    raw_mon = res.get("monetization")
    if raw_mon is None or str(raw_mon).strip() == "":
        monetization = _default_monetization_for_category(cat, is_lead)
    else:
        mon_norm = str(raw_mon).strip().lower()
        monetization = mon_norm if mon_norm in VALID_MONETIZATIONS else "none"

    if "short_form_fit" not in res or res.get("short_form_fit") is None:
        short_form_fit = bool(is_lead)
    else:
        short_form_fit = bool(res.get("short_form_fit"))

    return {
        "is_lead": is_lead,
        "icp_score": icp_score,
        "monetization": monetization,
        "short_form_fit": short_form_fit,
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
    """Drop supply-side, volunteer/[FOR HIRE], and known agency/dev-OSS rows across ALL sources."""
    kept, notes = [], []
    for row in rows or []:
        blob = lead_text(row)
        reason = sniper_reject_reason(row) or supply_reason(blob)
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


def qualify_loaded(
    rows,
    *,
    batch_size,
    quarantine_out: list[dict] | None = None,
    quarantine_dir: str | None = None,
):
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
    fast_path = os.getenv("TYPESAFE_FAST_PATH", "0").lower() in {"1", "true", "yes"}
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
        ts_fit = float(ts.get("typesafe_fit", 0.0)) if ts else 0.0
        if (
            ts
            and route == "keep"
            and fast_path
            and ts_fit >= 0.80
            and row.get("has_monetization_signal")
            and not row.get("has_agency_signal")
            and not sniper_reject_reason(row)
        ):
            ts_cat = str(ts.get("typesafe_category") or row.get("category") or "")
            ts_role = str(ts.get("typesafe_role") or row.get("role") or "")
            if is_rejected_buyer_category(ts_cat) or (ts_role and ts_role in NON_BUYER_ROLES):
                dropped += 1
                _quarantine_row(row, f"fast_path_reject:{ts_cat}:{ts_role}", quarantine)
                continue
            row["typesafe_qualified"] = True
            row["qualified"] = True
            row["typesafe_fit"] = round(ts_fit, 4)
            row["icp_score"] = max(MIN_ICP_SCORE, min(100, int(round(ts_fit * 100))))
            row.setdefault("monetization", _default_monetization_for_category(ts_cat, True))
            row.setdefault("short_form_fit", True)
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
          + (" (fast-path enabled)" if fast_path else " (sniper LLM gate)"), file=sys.stderr)
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
            r2 = dict(r)
            merged_url = nr["project_url"] or r.get("project_url") or ""
            merged_what = nr["what_they_do"] or r.get("what_they_do") or ""
            raw_proj = nr["project"] or r.get("project") or r.get("company") or ""
            clean_name, clean_proj = sanitize_person_and_company(
                r.get("name"),
                raw_proj,
                site=merged_url,
                bio=r.get("bio") or merged_what,
            )
            r2.update({
                "name": clean_name if r.get("name") is not None else clean_name,
                "category": nr["category"],
                "role": nr["role"] or r.get("role") or "",
                "roles": [nr["role"]] if nr["role"] else (r.get("roles") or []),
                "project": clean_proj,
                "project_url": merged_url,
                "what_they_do": merged_what,
                "icp_score": nr["icp_score"],
                "monetization": nr["monetization"],
                "short_form_fit": nr["short_form_fit"],
            })
            if "company" in r2:
                r2["company"] = clean_proj

            if not nr["is_lead"] or is_rejected_buyer_category(nr["category"]):
                dropped += 1
                _quarantine_row(r2, "not_buyer_or_agency", quarantine)
                print(f"    - drop: {r.get('name')} (@{r.get('username') or '-'})",
                      file=sys.stderr)
                continue

            sniper_err = sniper_reject_reason(r2)
            if sniper_err:
                dropped += 1
                _quarantine_row(r2, f"sniper_reject:{sniper_err}", quarantine)
                continue

            has_explicit_own_product = (
                bool(r2.get("has_monetization_signal"))
                and nr["monetization"] in {"subscription", "ecom", "iap", "cpa_deposit"}
                and not is_invalid_product_name(clean_proj)
            )
            if r2.get("has_agency_signal") and not has_explicit_own_product:
                dropped += 1
                _quarantine_row(r2, "agency_site_signal", quarantine)
                continue

            if nr["monetization"] == "none":
                dropped += 1
                _quarantine_row(r2, "no_monetization", quarantine)
                continue

            if int(nr["icp_score"]) < MIN_ICP_SCORE:
                dropped += 1
                _quarantine_row(r2, f"low_icp_score:{nr['icp_score']}", quarantine)
                continue

            bad_reason = _product_reality_reject_reason(r2)
            if bad_reason:
                dropped += 1
                _quarantine_row(r2, bad_reason, quarantine)
                continue

            r2["llm_qualified"] = True
            r2["qualified"] = True
            out.append(r2)
        print(f"  batch {bno}/{nbatches}: kept {len(out)} so far, dropped {dropped}",
              file=sys.stderr)

    target_q_dir = (os.getenv("LEADGEN_STATE_DIR", "").strip() or (quarantine_dir or "").strip())
    if quarantine and target_q_dir:
        try:
            os.makedirs(target_q_dir, exist_ok=True)
            q_path = os.path.join(target_q_dir, "quarantined_unqualified.jsonl")
            with open(q_path, "a", encoding="utf-8") as qfh:
                for qrow in quarantine:
                    qfh.write(json.dumps(qrow, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"# warning: could not write quarantine file: {exc}", file=sys.stderr)

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
    q_dir = os.getenv("LEADGEN_STATE_DIR", "").strip() or os.path.dirname(os.path.abspath(args.output))
    out, _dropped = qualify_loaded(rows, batch_size=args.batch_size, quarantine_dir=q_dir)
    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"-> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
