#!/usr/bin/env python3
"""Regenerate outreach fields to the UNIFIED standard for ALL TG rows in "Все2".

Standard (user directive):
  - «Первое касание»: greeting -> pain (from their product/niche) -> our presentation
    + case (183.5M views / 90 days, 13-40x cheaper) -> soft CTA. English, 3-4 sentences.
  - «Их угол»: concrete what/for-whom/model (>=40 chars, no fluff).
  - «Наш угол»: which CreatorOS direction + how we help (already OK everywhere).

NO Telegram resolving. Uses existing row data (name, category, their_angle, site).
Rewrites first_touch where it's off-standard (short / missing 183.5M case),
and their_angle where it's too thin. Writes via values.batchUpdate.

Usage:
  python3 scripts/regen_first_touch.py          # dry-run
  python3 scripts/regen_first_touch.py --write
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_ph_leads as Y

SHEET_ID = "1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
TAB = "Все2"


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def sheet_read():
    import requests
    token = Y.gsheets_token()
    r = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{TAB}!A1:L400",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def sheet_write(updates):
    import requests
    token = Y.gsheets_token()
    data = [{"range": f"{TAB}!H{rn}:K{rn}",
             "values": [[v.get("their_angle", ""), v.get("our_angle", ""), "", v.get("first_touch", "")]]}
            for rn, v in updates]
    if not data:
        return
    r = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueInputOption": "RAW", "data": data}, timeout=60)
    r.raise_for_status()
    log(f"sheet updated: {len(data)} rows")


def ft_off_standard(ft):
    ft = ft or ""
    return len(ft) < 200 or "183.5" not in ft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start-row", type=int, default=2,
                    help="first sheet row to inspect (inclusive)")
    ap.add_argument("--end-row", type=int, default=0,
                    help="last sheet row to inspect (inclusive; 0 means end)")
    args = ap.parse_args()

    Y.load_env()
    rows = sheet_read()
    todo = []
    for i, row in enumerate(rows[1:], start=2):
        if i < args.start_row or (args.end_row and i > args.end_row):
            continue
        row = row + [""] * (12 - len(row))
        if not row[4].strip() or "t.me/" not in row[6]:
            continue
        need_ft = ft_off_standard(row[10])
        need_ta = len(row[7]) < 40
        if not (need_ft or need_ta):
            continue
        todo.append({
            "row": i, "name": row[4], "site": row[5].strip(), "category": row[3],
            "their_angle": row[7], "our_angle": row[8], "first_touch": row[10],
            "need_ta": need_ta, "source": "sheet",
            # context for LLM: prefer existing their_angle + site; fall back to fit/name
            "one_liner": row[7] or row[2],
            "site_text": row[7] or row[2],  # reuse existing angle as research context
        })
    if args.limit:
        todo = todo[: args.limit]
    log(f"rows to regen: {len(todo)} (first_touch off-standard or thin angle)")
    if not todo:
        return

    # 1) regenerate thin their_angle (batched)
    thin = [t for t in todo if t["need_ta"]]
    if thin:
        log(f"regen thin their_angle: {len(thin)}")
        try:
            angles = Y.llm_angles_batch(thin)
            for idx, t in enumerate(thin):
                a = angles.get(idx) or {}
                if len((a.get("their_angle") or "").strip()) >= 40:
                    t["their_angle"] = a["their_angle"].strip()
                if (a.get("our_angle") or "").strip():
                    t["our_angle"] = a["our_angle"].strip()
        except Exception as e:
            log(f"angles batch failed: {e}; per-lead")
            for t in thin:
                try:
                    one = Y.llm_angles_batch([t])
                    a = list(one.values())[0] if one else {}
                    if len((a.get("their_angle") or "").strip()) >= 40:
                        t["their_angle"] = a["their_angle"].strip()
                except Exception as e2:
                    log(f"  angle {t['name']}: {e2}")
                time.sleep(2)

    # 2) regenerate first_touch for ALL todo (strict structure)
    log(f"regen first_touch: {len(todo)}")
    for n, t in enumerate(todo, 1):
        try:
            t["first_touch"] = Y.llm_first_touch(t)
        except Exception as e:
            log(f"  ft {t['name']}: {e}")
        time.sleep(2)
        if n % 20 == 0:
            log(f"  progress {n}/{len(todo)}")

    ok = [t for t in todo if not ft_off_standard(t["first_touch"])]
    log(f"regen OK: {len(ok)}/{len(todo)}")

    if args.write and ok:
        updates = [(t["row"], t) for t in ok]
        for i in range(0, len(updates), 25):
            sheet_write(updates[i:i + 25])
            time.sleep(1)
        log("DONE (written)")
    else:
        log("DRY-RUN")
        for t in todo[:3]:
            print("\n━━━", t["name"], "━━━\n", t["first_touch"])


if __name__ == "__main__":
    main()
