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
    from supply_side import supply_reason
except ImportError:
    try:
        from leadgen.supply_side import supply_reason
    except ImportError:
        from scripts.supply_side import supply_reason

if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
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

SYSTEM_PROMPT = """You qualify Telegram leads for CreatorOS.

CreatorOS sells organic short-form traffic (clipping + AI/UGC, paid short-form ads, physical products, 183.5M views in 90 days, revshare/CPA deals) to founders/growth/marketing of startups.

Target categories: SaaS/AI apps, CEX/exchanges, prop trading, media/content, DeFi/DEX, Gaming/AI, payments/fintech, tokens/memecoins.
Target roles (buyers): Founder, Marketing, Community, Creator/KOL.
NOT customers: regular users with no project, spammers, pure traders/investors with no product, freelancers with no own product, devs with no product, custom dev/AI/SEO/marketing agencies selling services to clients (Сервис / агентство), open-source libraries/build tools, CSS/coding quizzes, and free fan hobby games without monetization.

For EACH lead in the input array decide:
- is_lead: true ONLY if this person is a founder/CEO/growth/marketing/BD/community lead with a REAL product/project that needs user traffic. False for pure traders, investors, job seekers, empty/nonsense bios, spammers, devs without a product, custom dev/AI/SEO/marketing agencies (Сервис / агентство), open-source tools, coding quizzes, or free hobby games.
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
        headers={"Authorization": f"Bearer {API_KEY}",
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


def lead_text(row):
    parts = []
    for key in ("name", "username", "bio", "sample", "what_they_do", "project"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def gate_before_model(rows):
    """Drop supply-side authors before TypeSafe or Relay Fast see them."""
    kept, notes = [], []
    for row in rows or []:
        reason = supply_reason(lead_text(row))
        if reason:
            notes.append((row, reason))
            continue
        kept.append(row)
    return kept, notes


def qualify_loaded(rows, *, batch_size):
    catalog_rows = []
    pending = []
    for row in rows:
        source = str(row.get("source") or "")
        if source.startswith("catalog_") or source in {"linkedin_jobs", "meta_ads"}:
            catalog_rows.append(row)
        else:
            pending.append(row)
    original = list(pending)
    kept, notes = gate_before_model(original)
    dropped = 0
    for row, reason in notes:
        dropped += 1
        print(f"    - supply: {row.get('name')} (@{row.get('username') or '-'}) {reason}",
              file=sys.stderr)
    rows = kept
    input_total = len(original) + len(catalog_rows)
    print(f"Qualifying {input_total} leads in batches of {batch_size} "
          f"(route {' → '.join(chain())})", file=sys.stderr)

    out = list(catalog_rows)
    dropped = 0
    # TypeSafe is the primary typed classifier.  KEEP/DROP are final routing
    # decisions; only UNCERTAIN rows go through the slower Relay Fast check.
    # Set TYPESAFE_FAST_PATH=0 to temporarily restore Relay Fast as the gate
    # for every non-dropped row while validating a new prompt/threshold.
    fast_path = os.getenv("TYPESAFE_FAST_PATH", "1").lower() in {"1", "true", "yes"}
    ts_results = []
    if qualify_creator_rows and os.getenv("TYPESAFE_ENABLED", "1").lower() not in {"0", "false", "no"}:
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
            continue
        row = dict(row)
        if ts and route == "keep" and fast_path:
            row["typesafe_qualified"] = True
            row["typesafe_fit"] = round(float(ts.get("typesafe_fit", 0.0)), 4)
            if ts.get("typesafe_category"):
                row["category"] = ts["typesafe_category"]
            if ts.get("typesafe_role") and ts["typesafe_role"] != "Other":
                row["role"] = ts["typesafe_role"]
                row["roles"] = [ts["typesafe_role"]]
            out.append(row)
            continue
        routed.append(row)

    if routed:
        rows_for_openrouter = routed
    else:
        rows_for_openrouter = []
    # Keep the existing batched OpenRouter flow for uncertain cases only.
    rows = rows_for_openrouter
    total = len(rows)
    if total and not API_KEY:
        print("ERROR: RELAYFAST_API_KEY not set (.env); keeping uncertain rows with existing enrichment", file=sys.stderr)
        out.extend(rows)
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
            # batch failed hard: keep leads with their regex classification
            # rather than losing them silently
            print(f"  batch {bno}/{nbatches} FAILED ({e}); keeping batch unfiltered",
                  file=sys.stderr)
            results = [None] * len(batch)

        if len(results) != len(batch):
            print(f"  batch {bno}: length mismatch {len(results)} != {len(batch)}; "
                  f"aligning conservatively", file=sys.stderr)
            results = (list(results) + [None] * len(batch))[:len(batch)]

        for r, res in zip(batch, results):
            nr = norm_result(res) if res else None
            if nr is None:
                # unparseable -> keep lead as-is (don't lose data)
                out.append(r)
                continue
            if not nr["is_lead"] or nr["category"] == "Сервис / агентство":
                dropped += 1
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
            })
            out.append(r2)
        print(f"  batch {bno}/{nbatches}: kept {len(out)} so far, dropped {dropped}",
              file=sys.stderr)

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
