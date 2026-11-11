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
import re
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect, utc_now
from app.outreach import _fix_mojibake, is_offline_non_icp_contact
from leadgen.outreach_copy import (
    REVSHARE_OFFER_TEXT,
    VALID_AUDIENCES,
    VLAD_STEP2_TEMPLATE,
    VLAD_STEP3_IMAGE_URL,
    VLAD_STEP3_TEXT,
    clean_brand_name,
    first_touch,
    followup_one,
    generate_vlad_sonnet_batch,
)

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_NON_ASCII_CLEAN_RE = re.compile(r"[^\x00-\x7F’']")
_DASH_RE = re.compile(r"[—–―‒]")
_PLACEHOLDER_RE = re.compile(
    r"(?:\[[^\]]*\]|\{[^}]*\}|u\+[0-9a-f]{4}|\b(?:hi|hey|hello)\s+there\b)",
    re.I,
)
_CAT_EN_MAP = {
    "агентство": "B2B / Marketing Agency",
    "сервис / агентство": "B2B / Marketing Agency",
    "медиа": "Media / Content / Community",
    "финтех": "Fintech",
    "финансы / трейдинг": "Trading / Finance",
    "проп": "Prop Trading",
}


def _load_env() -> None:
    for p in (
        ROOT / ".env",
        Path("/Users/timofei/dev/Python/new era/crmtg/.env"),
        Path("/Users/timofei/dev/Python/new era/creatoros/.env"),
    ):
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _collect_contacted_sets(conn: sqlite3.Connection) -> tuple[set[str], set[int], set[str], set[int]]:
    """Return (usernames, tg_ids, phones, lead_ids) that already received outbound messages or step>=1."""
    unames: set[str] = set()
    tg_ids: set[int] = set()
    phones: set[str] = set()
    lead_ids: set[int] = set()

    for r in conn.execute(
        """
        SELECT DISTINCT d.peer_id, d.username, d.phone, d.lead_id
        FROM dialogs d
        JOIN messages m ON m.dialog_id = d.id
        WHERE m.direction = 'out'
        """
    ).fetchall():
        u = str(r["username"] or "").strip().lstrip("@").lower()
        if u:
            unames.add(u)
        pid = r["peer_id"]
        if pid is not None and str(pid).isdigit():
            tid = int(pid)
            if tid > 0 and not (900000 <= tid <= 999999):
                tg_ids.add(tid)
        ph = str(r["phone"] or "").strip().lstrip("+")
        if ph:
            phones.add(ph)
        if r["lead_id"] is not None:
            lead_ids.add(int(r["lead_id"]))

    for r in conn.execute(
        """
        SELECT DISTINCT l.id, l.telegram_user_id, l.username, l.phone
        FROM leads l
        JOIN messages m ON m.dialog_id = l.dialog_id
        WHERE m.direction = 'out'
        """
    ).fetchall():
        lead_ids.add(int(r["id"]))
        u = str(r["username"] or "").strip().lstrip("@").lower()
        if u:
            unames.add(u)
        tid = r["telegram_user_id"]
        if tid is not None and int(tid) > 0 and not (900000 <= int(tid) <= 999999):
            tg_ids.add(int(tid))
        ph = str(r["phone"] or "").strip().lstrip("+")
        if ph:
            phones.add(ph)

    for r in conn.execute(
        """
        SELECT telegram_user_id, username, phone, lead_id
        FROM campaign_recipients
        WHERE COALESCE(current_step, 0) >= 1
           OR last_sent_at IS NOT NULL
           OR status IN ('in_progress', 'completed', 'replied')
        """
    ).fetchall():
        u = str(r["username"] or "").strip().lstrip("@").lower()
        if u:
            unames.add(u)
        tid = r["telegram_user_id"]
        if tid is not None and int(tid) > 0 and not (900000 <= int(tid) <= 999999):
            tg_ids.add(int(tid))
        ph = str(r["phone"] or "").strip().lstrip("+")
        if ph:
            phones.add(ph)
        if r["lead_id"] is not None:
            lead_ids.add(int(r["lead_id"]))

    return unames, tg_ids, phones, lead_ids


def _infer_pain_angle(category: str, their_angle: str, step1_text: str) -> str:
    cat_low = (category or "").strip().lower()
    blob = f"{cat_low} {(their_angle or '').lower()} {(step1_text or '').lower()}"
    if re.search(r"(?:агентств|agency|agencies|\bbd\b|advisory|consulting|venture\s+studio|kol\s+marketing)", cat_low):
        return "agency_manual_bd"
    if re.search(r"(?:\bweb3\b|\bdefi\b|\bdex\b|\bcex\b|проп|трейдинг|prop\s*trad|crypto|on-?chain|token|exchange)", cat_low):
        return "web3_paid_restrictions"
    if re.search(r"(?:финтех|fintech|payment|neobank|banking)", cat_low):
        return "fintech_trust_cac"
    if re.search(r"(?:медиа|\bmedia\b|podcast|newsletter|community)", cat_low):
        return "media_shortform_scale"
    if re.search(r"(?:\be-?com(?:merce)?\b|\bd2c\b|\bdtc\b|shopify|skincare|supplement|apparel|consumer\s+brand)", blob):
        return "ecom_creative_fatigue"
    if re.search(r"(?:\bgaming\b|gamefi|igaming|roblox|steam\s+game|mobile\s+game|game\s+studio)", blob):
        return "gaming_paid_ua"
    if re.search(r"(?:\bedtech\b|online\s+course|bootcamp|tutoring|language\s+learning|academy)", blob):
        return "edtech_cpl_scale"
    if re.search(r"(?:open-?source|\bsdk\b|\bcli\b|self-?hosted|developer\s+tool|dev-?tool|docker|kubernetes)", blob):
        return "devtool_organic_reach"
    if re.search(r"(?:\bsaas\b|\bai\b|\bcac\b|paid\s+ads|signups)", blob):
        return "saas_cac_scale"
    return "product_organic_reach"


def _normalize_step1(raw_step1: str) -> str:
    s = (raw_step1 or "").replace("\u2028", "\n").replace("\r\n", "\n").strip()
    s = re.sub(r"\bcreatoros\b", "SoftUGC", s, flags=re.I)
    s = re.sub(r"\bsoftugc\b", "SoftUGC", s, flags=re.I)
    s = re.sub(r"\b183\.5m\b", "183.5M", s, flags=re.I)
    s = re.sub(r"\s*\(generated\)\s*", " ", s, flags=re.I)
    s = re.sub(r"\s*[—–―‒]\s*", ", ", s)
    s = re.sub(r"^Hey,\n\n(?:Hey|Hi)\b[,\s]*", "Hey,\n\n", s, flags=re.I)
    if s.count(REVSHARE_OFFER_TEXT) > 1:
        head = s[: -len(REVSHARE_OFFER_TEXT)].replace(REVSHARE_OFFER_TEXT, "").strip()
        s = f"{head}\n\n{REVSHARE_OFFER_TEXT}"
    return s.strip()


def _check_defects(
    *,
    step1_text: str,
    followup1_text: str,
    followup2_text: str,
    audience: str,
    deterministic_fallback: str,
) -> list[str]:
    errs: list[str] = []
    if not step1_text:
        errs.append("empty_step1")
        return errs
    if step1_text.strip() == deterministic_fallback.strip():
        errs.append("deterministic_fallback")
    if "creatoros" in step1_text.lower() or "creatoros" in followup1_text.lower():
        errs.append("has_creatoros")
    if "if your user growth has hit a ceiling" in step1_text.lower():
        errs.append("has_ceiling_cliche")
    if "(generated)" in step1_text.lower() or "(generated)" in followup1_text.lower():
        errs.append("has_generated_tag")
    if _DASH_RE.search(step1_text) or _DASH_RE.search(followup1_text) or _DASH_RE.search(followup2_text):
        errs.append("has_em_or_en_dash")
    if _CYRILLIC_RE.search(step1_text) or _CYRILLIC_RE.search(followup1_text):
        errs.append("has_cyrillic")
    if _NON_ASCII_CLEAN_RE.search(step1_text) or _NON_ASCII_CLEAN_RE.search(followup1_text):
        errs.append("has_non_ascii_glitch")
    if _PLACEHOLDER_RE.search(step1_text):
        errs.append("has_placeholder")
    if "SoftUGC" not in step1_text:
        errs.append("missing_SoftUGC")
    if "183.5M" not in step1_text:
        errs.append("missing_183.5M")
    if not step1_text.endswith(REVSHARE_OFFER_TEXT):
        errs.append("bad_cta_ending")
    if step1_text.count("Want to see 3 example reels") != 1:
        errs.append("duplicate_cta")
    if audience not in VALID_AUDIENCES:
        errs.append("invalid_audience")
    return errs


def _sanitize_brand_input(raw: str) -> str:
    s = (raw or "").strip()
    if sum(1 for ch in s if ord(ch) > 127) >= 3 and not _CYRILLIC_RE.search(s):
        return ""
    return re.sub(r"[^\x00-\x7F]+", " ", s).strip()


def _lead_payload_for_llm(item: dict) -> dict:
    raw_cat = str(item.get("category") or "").strip()
    en_cat = _CAT_EN_MAP.get(raw_cat.lower(), raw_cat)
    return {
        "company": _sanitize_brand_input(item.get("company") or ""),
        "name": _sanitize_brand_input(item.get("name") or ""),
        "category": en_cat,
        "website": item.get("website") or "",
        "their_angle": item.get("clean_angle") or "",
    }


def _det_fallback_for_item(item: dict) -> str:
    brand = clean_brand_name(
        item.get("company") or item.get("name") or "",
        site=item.get("website") or "",
        their_angle=item.get("clean_angle") or "",
    )
    return first_touch(
        brand or item.get("company") or item.get("name") or "",
        item.get("clean_angle") or "",
        russian=False,
        category=item.get("category") or "",
        site=item.get("website") or "",
    )


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description="Regenerate untouched B2B outreach drafts via claude-sonnet-5")
    ap.add_argument("--db", default=str(ROOT / "data" / "crm.db"))
    ap.add_argument("--campaigns", default="5,4", help="Comma-separated campaign IDs")
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--hard-cap-requests", type=int, default=75, help="Hard cap on LLM API calls (0 retries beyond cap)")
    ap.add_argument("--limit-leads", type=int, default=0, help="Optional max leads for pilot test")
    ap.add_argument("--out-json", default=str(ROOT / "data" / "sonnet5_vlad_batch.json"))
    ap.add_argument("--write", action="store_true", help="Write generated Step 1 + Step 2 audience into SQLite")
    args = ap.parse_args()

    req_cids = [int(x.strip()) for x in args.campaigns.split(",") if x.strip()]
    conn = connect(Path(args.db))

    camp_rows = conn.execute("SELECT id, name FROM campaigns").fetchall()
    valid_b2b_cids = {
        int(r["id"])
        for r in camp_rows
        if not any(k in str(r["name"] or "").lower() for k in ("ваканс", "vacanc", "job"))
    }
    cids = [cid for cid in req_cids if cid in valid_b2b_cids]
    if not cids:
        print("No valid B2B campaigns selected.")
        return 0

    contacted_unames, contacted_tids, contacted_phones, contacted_lids = _collect_contacted_sets(conn)

    ph = ",".join("?" for _ in cids)
    rows = conn.execute(
        f"""
        SELECT id, campaign_id, lead_id, telegram_user_id, username, phone, name, company,
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
    skipped_contacted = 0
    for r in rows:
        rec = dict(r)
        uname = str(rec.get("username") or "").strip().lstrip("@")
        uname_low = uname.lower()
        phone = str(rec.get("phone") or "").strip()
        phone_clean = phone.lstrip("+")
        tg_id = rec.get("telegram_user_id")
        real_tid = int(tg_id) if (tg_id is not None and int(tg_id) > 0 and not (900000 <= int(tg_id) <= 999999)) else None
        lid = int(rec["lead_id"]) if rec.get("lead_id") is not None else None

        has_handle = bool(uname or phone or real_tid is not None)
        if not has_handle:
            continue
        if (
            (uname_low and uname_low in contacted_unames)
            or (real_tid is not None and real_tid in contacted_tids)
            or (phone_clean and phone_clean in contacted_phones)
            or (lid is not None and lid in contacted_lids)
        ):
            skipped_contacted += 1
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

        rec["clean_username"] = uname
        rec["clean_angle"] = angle
        rec["handle_key"] = uname_low or (f"tid:{real_tid}" if real_tid is not None else f"ph:{phone_clean}")
        candidates.append(rec)

    if args.limit_leads > 0:
        candidates = candidates[: args.limit_leads]

    unique_by_key: dict[str, dict] = {}
    for item in candidates:
        unique_by_key.setdefault(item["handle_key"], item)
    unique_leads = list(unique_by_key.values())

    batches = [unique_leads[i : i + args.batch_size] for i in range(0, len(unique_leads), args.batch_size)]
    if len(batches) > args.hard_cap_requests:
        raise RuntimeError(
            f"Refusing to exceed hard cap: {len(batches)} batches > --hard-cap-requests={args.hard_cap_requests}"
        )

    print(
        f"Targets: {len(candidates)} untouched ICP rows ({len(unique_leads)} unique handles, "
        f"skipped_contacted={skipped_contacted}) across campaigns {cids} "
        f"-> {len(batches)} claude-sonnet-5 batches (hard cap={args.hard_cap_requests}, write={args.write})",
        flush=True,
    )
    if not candidates:
        return 0

    req_lock = threading.Lock()
    requests_used = 0

    def _call_batch_counted(batch_items: list[dict]) -> list[dict[str, str]] | None:
        nonlocal requests_used
        with req_lock:
            if requests_used >= args.hard_cap_requests:
                return None
            requests_used += 1
            req_num = requests_used
        try:
            return generate_vlad_sonnet_batch(
                [_lead_payload_for_llm(it) for it in batch_items],
                model="claude-sonnet-5",
                timeout=120,
            )
        except Exception as exc:
            print(f"  [WARN req #{req_num}] batch({len(batch_items)}) error: {type(exc).__name__}: {exc}", flush=True)
            return None

    results_by_key: dict[str, dict[str, str]] = {}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fut_map = {pool.submit(_call_batch_counted, batch): batch for batch in batches}
        done_batches = 0
        for fut in as_completed(fut_map):
            batch = fut_map[fut]
            gen_list = fut.result()
            done_batches += 1
            if gen_list and len(gen_list) == len(batch):
                for item, gen in zip(batch, gen_list):
                    results_by_key[item["handle_key"]] = {
                        "step1_text": _normalize_step1(gen["step1_text"]),
                        "audience": str(gen["audience"]).strip().lower(),
                    }
            print(
                f"  [{done_batches}/{len(batches)}] pass-1 ({len(results_by_key)}/{len(unique_leads)} unique leads, "
                f"requests_used={requests_used}/{args.hard_cap_requests})",
                flush=True,
            )

    # Quality verification + targeted retry for any fallback or defective leads within hard cap
    for retry_round in (1, 2):
        bad_items: list[dict] = []
        for item in unique_leads:
            hk = item["handle_key"]
            gen = results_by_key.get(hk)
            if not gen:
                bad_items.append(item)
                continue
            aud = gen["audience"]
            f1 = followup_one(
                item.get("company") or "",
                item.get("clean_angle") or "",
                audience=aud,
                category=item.get("category") or "",
                site=item.get("website") or "",
            )
            errs = _check_defects(
                step1_text=gen["step1_text"],
                followup1_text=f1,
                followup2_text=VLAD_STEP3_TEXT,
                audience=aud,
                deterministic_fallback=_det_fallback_for_item(item),
            )
            if errs:
                bad_items.append(item)

        if not bad_items or requests_used >= args.hard_cap_requests:
            break

        retry_chunk_size = min(5, max(1, args.batch_size))
        retry_batches = [bad_items[i : i + retry_chunk_size] for i in range(0, len(bad_items), retry_chunk_size)]
        print(
            f"Retry round {retry_round}: {len(bad_items)} leads need clean Sonnet-5 regeneration "
            f"({len(retry_batches)} batches, budget left={args.hard_cap_requests - requests_used})",
            flush=True,
        )
        for rb in retry_batches:
            if requests_used >= args.hard_cap_requests:
                break
            gen_list = _call_batch_counted(rb)
            if gen_list and len(gen_list) == len(rb):
                for item, gen in zip(rb, gen_list):
                    results_by_key[item["handle_key"]] = {
                        "step1_text": _normalize_step1(gen["step1_text"]),
                        "audience": str(gen["audience"]).strip().lower(),
                    }

    # Build final items & run hard quality gate across 100% of items
    now = utc_now()
    items_out: list[dict] = []
    defect_summary: list[tuple[int, str, list[str]]] = []

    for item in candidates:
        rid = int(item["id"])
        cid = int(item["campaign_id"])
        hk = item["handle_key"]
        gen = results_by_key.get(hk) or {"step1_text": "", "audience": ""}
        step1_text = gen["step1_text"]
        aud = gen["audience"]
        f1_text = followup_one({"category": item.get("category") or ""}, audience=aud)
        f2_text = VLAD_STEP3_TEXT
        brand = clean_brand_name(
            item.get("company") or item.get("name") or "",
            site=item.get("website") or "",
            their_angle=item.get("clean_angle") or "",
        ) or str(item.get("company") or "").strip()
        pain_angle = _infer_pain_angle(item.get("category") or "", item.get("clean_angle") or "", step1_text)

        errs = _check_defects(
            step1_text=step1_text,
            followup1_text=f1_text,
            followup2_text=f2_text,
            audience=aud,
            deterministic_fallback=_det_fallback_for_item(item),
        )
        if errs:
            defect_summary.append((rid, item.get("clean_username") or "", errs))

        items_out.append(
            {
                "recipient_id": rid,
                "campaign_id": cid,
                "username": item.get("clean_username") or "",
                "telegram_user_id": int(item["telegram_user_id"]) if item.get("telegram_user_id") is not None else None,
                "full_name": str(item.get("name") or "").strip(),
                "company": brand,
                "website": str(item.get("website") or "").strip(),
                "category": str(item.get("category") or "").strip(),
                "their_angle": str(item.get("clean_angle") or "").strip(),
                "step1_text": step1_text,
                "followup1_text": f1_text,
                "followup2_text": f2_text,
                "audience": aud,
                "variant_key": "sonnet5_vlad_v1",
                "pain_angle": pain_angle,
            }
        )

    out_payload = {
        "meta": {
            "generated_at": now,
            "model": "claude-sonnet-5",
            "total_leads": len(items_out),
            "requests_used": requests_used,
            "campaigns": cids,
        },
        "items": items_out,
    }

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote JSON artifact: {out_path} ({len(items_out)} items, requests_used={requests_used})", flush=True)

    if defect_summary:
        print(f"WARNING: {len(defect_summary)} items still have defects: {defect_summary[:10]}", flush=True)
        raise RuntimeError(f"Quality gate failed on {len(defect_summary)} items: {defect_summary[:5]}")

    if args.write:
        by_rid = {obj["recipient_id"]: obj for obj in items_out}
        for item in candidates:
            rid = int(item["id"])
            obj = by_rid[rid]
            extra: dict = {}
            if item.get("extra_json"):
                try:
                    extra = json.loads(item["extra_json"])
                except Exception:
                    extra = {}
            extra["audience"] = obj["audience"]
            extra["variant_key"] = obj["variant_key"]
            extra["pain_angle"] = obj["pain_angle"]
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
                    obj["company"] or item.get("company"),
                    obj["their_angle"] or None,
                    obj["step1_text"],
                    json.dumps(extra, ensure_ascii=False),
                    now,
                    rid,
                ),
            )
        conn.commit()
        print(f"Saved {len(items_out)} claude-sonnet-5 drafts into {args.db} (campaign pause state untouched).")
    else:
        for obj in items_out[:2]:
            print(f"\n=== @{obj['username']} ({obj['company']}) | audience={obj['audience']} | pain={obj['pain_angle']} ===")
            print("STEP 1:\n" + obj["step1_text"])
            print("\nSTEP 2:\n" + obj["followup1_text"])
            print(f"\nSTEP 3 (+ {VLAD_STEP3_IMAGE_URL}):\n" + obj["followup2_text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
