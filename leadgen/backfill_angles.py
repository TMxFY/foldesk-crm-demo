#!/usr/bin/env python3
"""Backfill «Их угол» / «Наш угол» / «Первое касание» for EXISTING rows in the
"Все2" Google Sheet that have a TG contact but empty/short outreach fields.

Two lead shapes:
  - rows WITH a site  -> research via fetch_site_text (requests/bs4 + firecrawl fallback)
  - rows WITHOUT site -> research via Telegram bio (Telethon get_entity)

Angles are generated in batches; first-touch per lead, strict user structure.
Updates the Sheet in place (columns H, I, K) via values.batchUpdate.

Usage:
  python3 scripts/backfill_angles.py            # dry-run, no sheet writes
  python3 scripts/backfill_angles.py --write    # apply to sheet
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yc_ph_leads as Y  # reuse fetch_site_text, llm_angles_batch, llm_first_touch, gsheets_token

SHEET_ID = "1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
TAB = "Все2"
SESSION = "/Users/timofei/.claude/skill-telegram/tim2.session"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def sheet_read():
    token = Y.gsheets_token()
    import requests
    r = requests.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{TAB}!A1:L400",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def sheet_write(updates):
    """updates: list of (row_number, {col_letter: value}). Batch by row into H:K range writes."""
    import requests
    token = Y.gsheets_token()
    data = []
    for rownum, vals in updates:
        # write H,I,J,K (Их угол, Наш угол, Хук, Первое касание) as one contiguous range
        data.append({
            "range": f"{TAB}!H{rownum}:K{rownum}",
            "values": [[vals.get("their_angle", ""), vals.get("our_angle", ""),
                        "", vals.get("first_touch", "")]],
        })
    if not data:
        return
    r = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueInputOption": "RAW", "data": data}, timeout=60)
    r.raise_for_status()
    log(f"sheet updated: {len(data)} rows")


def tg_username(contact):
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", contact or "")
    return m.group(1) if m else ""


def fetch_bios(usernames):
    """Pull bios for usernames via Telethon. Returns {username_lower: bio}."""
    from telethon import TelegramClient
    from telethon.tl.functions.users import GetFullUserRequest
    out = {}

    async def run():
        client = TelegramClient(SESSION, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("tim2 session not authorized")
        for u in usernames:
            try:
                ent = await client.get_entity(u)
                full = await client(GetFullUserRequest(ent))
                bio = (full.full_user.about or "").strip()
                name = " ".join(x for x in [getattr(ent, "first_name", ""), getattr(ent, "last_name", "")] if x).strip()
                out[u.lower()] = {"bio": bio, "tg_name": name}
            except Exception as e:
                log(f"  bio @{u}: {e}")
                out[u.lower()] = {"bio": "", "tg_name": ""}
            await asyncio.sleep(1.2)
        await client.disconnect()

    asyncio.run(run())
    return out


def needs_enrich(row):
    row = row + [""] * (12 - len(row))
    return ("t.me/" in row[6]) and not (len(row[7]) > 30 and len(row[8]) > 10 and len(row[10]) > 120)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply updates to the sheet")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    Y.load_env()
    rows = sheet_read()
    todo = []
    for i, row in enumerate(rows[1:], start=2):
        if needs_enrich(row):
            row = row + [""] * (12 - len(row))
            todo.append({
                "row": i, "name": row[4], "site": row[5].strip(), "contact": row[6],
                "category": row[3], "source": "sheet",
                "one_liner": row[7] or row[2],  # old short angle / fit as fallback context
            })
    if args.limit:
        todo = todo[: args.limit]
    log(f"rows needing enrich: {len(todo)}")
    if not todo:
        return

    # 1) research: site text for rows with site, TG bio for the rest
    no_site = [t for t in todo if not t["site"]]
    usernames = [tg_username(t["contact"]) for t in no_site]
    usernames = [u for u in usernames if u]
    bios = {}
    if usernames:
        log(f"fetching {len(usernames)} TG bios...")
        bios = fetch_bios(usernames)
        log(f"bios fetched: {sum(1 for v in bios.values() if v['bio'])}/{len(bios)}")

    for t in todo:
        if t["site"]:
            t["site_text"] = Y.fetch_site_text(t["site"])
        else:
            b = bios.get(tg_username(t["contact"]).lower()) or {}
            ctx = b.get("bio") or ""
            if b.get("tg_name"):
                ctx = (b["tg_name"] + " — " + ctx).strip(" —")
            t["site_text"] = ctx  # bio acts as research text
            if not ctx:
                t["site_text"] = f"{t['name']} ({t['category']})"  # minimal context

    # 2) LLM angles (batched) + first touch (per lead)
    Y.enrich_with_llm(todo)

    # 3) report + write
    ok = [t for t in todo if len(t.get("their_angle", "")) >= 30 and t.get("our_angle") and len(t.get("first_touch", "")) >= 120]
    log(f"enriched OK: {len(ok)}/{len(todo)}")
    for t in todo:
        status = "OK " if t in ok else "SKIP"
        log(f"  {status} row {t['row']}: {t['name'][:25]:25} их={len(t.get('their_angle','')):3} касание={len(t.get('first_touch','')):4}")

    if args.write and ok:
        updates = [(t["row"], t) for t in ok]
        # batch in chunks of 25 to stay under API limits
        for i in range(0, len(updates), 25):
            sheet_write(updates[i:i + 25])
            time.sleep(1)
        log("DONE (written)")
    elif not args.write:
        log("DRY-RUN (use --write to apply)")


if __name__ == "__main__":
    main()
