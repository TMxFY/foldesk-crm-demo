#!/usr/bin/env python3
"""Batch regenerate untouched B2B campaign recipients (Campaigns #5 & #4) via claude-sonnet-5.

Respects hard request cap, never touches already contacted leads, and never unpauses campaigns.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect, utc_now
from app.outreach import _fix_mojibake, is_offline_non_icp_contact
from leadgen.outreach_copy import (
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_IMAGE_URL,
    VLAD_STEP3_TEXT,
    clean_brand_name,
    generate_vlad_sonnet_batch,
)


def _load_env() -> None:
    for p in (ROOT / ".env", Path("/Users/timofei/dev/Python/new era/crmtg/.env"), Path("/Users/timofei/dev/Python/new era/creatoros/.env")):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description="Regenerate untouched B2B outreach drafts via claude-sonnet-5")
    ap.add_argument("--db", default=str(ROOT / "data" / "crm.db"))
    ap.add_argument("--campaigns", default="5,4", help="Comma-separated campaign IDs")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--hard-cap-requests", type=int, default=75, help="Hard cap on LLM API calls (0 retries beyond cap)")
    ap.add_argument("--limit-leads", type=int, default=0, help="Optional max leads for pilot test")
    ap.add_argument("--write", action="store_true", help="Write generated Step 1 + Step 2 audience into SQLite")
    args = ap.parse_args()

    cids = [int(x.strip()) for x in args.campaigns.split(",") if x.strip()]
    conn = connect(Path(args.db))
    ph = ",".join("?" for _ in cids)
    rows = conn.execute(
        f"""
        SELECT id, campaign_id, telegram_user_id, username, phone, name, company,
               website, category, their_angle, note, extra_json
        FROM campaign_recipients
        WHERE campaign_id IN ({ph})
          AND COALESCE(current_step, 0) = 0
          AND last_sent_at IS NULL
          AND status IN ('pending', 'failed')
        ORDER BY campaign_id DESC, id ASC
        """,
        tuple(cids),
    ).fetchall()

    candidates: list[dict] = []
    for r in rows:
        rec = dict(r)
        uname = str(rec.get("username") or "").strip().lstrip("@")
        phone = str(rec.get("phone") or "").strip()
        tg_id = rec.get("telegram_user_id")
        has_handle = bool(uname or phone or (tg_id is not None and not (900000 <= int(tg_id) <= 999999)))
        if not has_handle:
            continue
        angle = _fix_mojibake(rec.get("their_angle") or rec.get("note") or "")
        if is_offline_non_icp_contact(
            username=uname,
            name=rec.get("name"),
            company=rec.get("company"),
            website=rec.get("website"),
            their_angle=angle,
        ):
            continue
        rec["clean_angle"] = angle
        candidates.append(rec)

    if args.limit_leads > 0:
        candidates = candidates[: args.limit_leads]

    batches = [candidates[i : i + args.batch_size] for i in range(0, len(candidates), args.batch_size)]
    if len(batches) > args.hard_cap_requests:
        raise RuntimeError(
            f"Refusing to exceed hard cap: {len(batches)} batches > --hard-cap-requests={args.hard_cap_requests}"
        )

    print(
        f"Targets: {len(candidates)} untouched ICP leads across campaigns {cids} "
        f"-> {len(batches)} claude-sonnet-5 batches (hard cap={args.hard_cap_requests}, write={args.write})"
    )
    if not candidates:
        return 0

    results_by_id: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fut_map = {
            pool.submit(
                generate_vlad_sonnet_batch,
                [
                    {
                        "company": item.get("company") or "",
                        "name": item.get("name") or "",
                        "category": item.get("category") or "",
                        "website": item.get("website") or "",
                        "their_angle": item.get("clean_angle") or "",
                    }
                    for item in batch
                ],
                model="claude-sonnet-5",
            ): batch
            for batch in batches
        }
        done_batches = 0
        for fut in as_completed(fut_map):
            batch = fut_map[fut]
            gen_list = fut.result()
            done_batches += 1
            for item, gen in zip(batch, gen_list):
                results_by_id[int(item["id"])] = gen
            print(f"  [{done_batches}/{len(batches)}] generated ({len(results_by_id)}/{len(candidates)} leads)", flush=True)

    if args.write:
        now = utc_now()
        for item in candidates:
            rid = int(item["id"])
            gen = results_by_id[rid]
            extra: dict = {}
            if item.get("extra_json"):
                try:
                    extra = json.loads(item["extra_json"])
                except Exception:
                    extra = {}
            extra["audience"] = gen["audience"]
            brand = clean_brand_name(
                item.get("company") or item.get("name") or "",
                site=item.get("website") or "",
                their_angle=item.get("clean_angle") or "",
            ) or item.get("company")
            conn.execute(
                """
                UPDATE campaign_recipients
                SET company = ?,
                    their_angle = ?,
                    step1_text = ?,
                    step2_text = NULL,
                    step3_text = NULL,
                    extra_json = ?,
                    status = 'pending',
                    error_text = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    brand,
                    item.get("clean_angle") or None,
                    gen["step1_text"],
                    json.dumps(extra, ensure_ascii=False),
                    now,
                    rid,
                ),
            )
        conn.commit()
        print(f"Saved {len(candidates)} claude-sonnet-5 drafts into {args.db} (campaign pause state untouched).")
    else:
        for item in candidates[:3]:
            gen = results_by_id[int(item["id"])]
            print(f"\n=== @{item.get('username')} ({item.get('company')}) | audience={gen['audience']} ===")
            print("STEP 1:\n" + gen["step1_text"])
            print("\nSTEP 2:\n" + VLAD_STEP2_TEMPLATE.format(audience=gen["audience"]))
            print(f"\nSTEP 3 (+ {VLAD_STEP3_IMAGE_URL}):\n" + VLAD_STEP3_TEXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
