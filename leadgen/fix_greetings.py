#!/usr/bin/env python3
"""Fix first-touch greetings + regenerate off-script messages across ALL of "Все2".

Two fix classes:
  A) OLD-TEMPLATE messages (prop-firm / clip-network scripts) -> full regen, strict structure.
  B) PLACEHOLDER greetings ([Founder's Name], [Name], U+0048, 'Hi there', brand-as-person)
     -> regen with strict name rule: use a personal name only if it's a normal English
        first name, else just 'Hey,'/'Hi,'.

No Telegram resolving. founder_name is guessed from the row's display name when it
looks like a real person (First Last, latin, no digits/brand words).

  python3 scripts/fix_greetings.py          # dry-run
  python3 scripts/fix_greetings.py --write
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

OLD_TPL = re.compile(
    r"(prop firms live and die|clip networks|10-account|10 account|1000\+ clips|"
    r"checkout - organic|send qualified|cut your episodes|deep content archive|"
    r"partner chat|add-on for your clients)", re.I)
PLACEHOLDER = re.compile(
    r"(\[founder|\[name|\[their|u\+\d{4}|\bhi there\b|\bhey there\b|\bhello there\b)", re.I)

# brand/role words that mean the display name is NOT a person's first name
NON_PERSON = re.compile(
    r"(labs|studio|group|capital|network|exchange|pay|wallet|chain|protocol|finance|"
    r"marketing|partnership|community|media|tech|labs|coin|token|dao|ai|io|xyz|"
    r"founder|ceo|bd|head|lead|dev|growth|kol|web3|crypto|defi|nft|tg|ws)", re.I)


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
    data = [{"range": f"{TAB}!K{rn}", "values": [[v]]} for rn, v in updates]
    if not data:
        return
    r = requests.post(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"valueInputOption": "RAW", "data": data}, timeout=60)
    r.raise_for_status()
    log(f"sheet updated: {len(data)} first-touch cells")


def guess_founder_name(display):
    """Return a clean English first name if display looks like a real person, else ''."""
    d = (display or "").strip()
    if not d or NON_PERSON.search(d):
        return ""
    # strip emoji / non-latin
    d = re.sub(r"[^\x00-\x7F]+", " ", d).strip()
    # take first token that looks like a capitalized latin name (2-15 chars, letters only)
    for tok in re.split(r"[\s|/,&]+", d):
        if re.fullmatch(r"[A-Z][a-z]{1,14}", tok):
            return tok
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    Y.load_env()
    rows = sheet_read()
    todo = []
    for i, row in enumerate(rows[1:], start=2):
        row = row + [""] * (12 - len(row))
        if not row[4].strip() or "t.me/" not in row[6]:
            continue
        ft = row[10]
        is_old = bool(OLD_TPL.search(ft))
        is_ph = bool(PLACEHOLDER.search(ft))
        if not (is_old or is_ph):
            continue
        todo.append({
            "row": i, "name": row[4], "category": row[3], "site": row[5].strip(),
            "their_angle": row[7], "our_angle": row[8], "first_touch": ft,
            "source": "sheet", "one_liner": row[7] or row[2],
            "site_text": row[7] or row[2],
            "founder_name": guess_founder_name(row[4]),
            "kind": "old" if is_old else "placeholder",
        })
    if args.limit:
        todo = todo[: args.limit]
    log(f"rows to fix: {len(todo)} "
        f"(old-template={sum(1 for t in todo if t['kind']=='old')}, "
        f"placeholder={sum(1 for t in todo if t['kind']=='placeholder')})")
    if not todo:
        return

    for n, t in enumerate(todo, 1):
        try:
            t["first_touch"] = Y.llm_first_touch(t)
        except Exception as e:
            log(f"  ft {t['name']}: {e}")
        time.sleep(2)
        if n % 20 == 0:
            log(f"  progress {n}/{len(todo)}")

    # validate: no placeholder, has case, decent length
    def ok(ft):
        return (ft and "183.5" in ft and len(ft) >= 200 and not PLACEHOLDER.search(ft)
                and "[founder" not in ft.lower() and "[name" not in ft.lower())

    good = [t for t in todo if ok(t["first_touch"])]
    log(f"fixed OK: {len(good)}/{len(todo)}")

    if args.write and good:
        updates = [(t["row"], t["first_touch"]) for t in good]
        for i in range(0, len(updates), 25):
            sheet_write(updates[i:i + 25])
            time.sleep(1)
        log("DONE (written)")
    else:
        log("DRY-RUN")
        for t in todo[:5]:
            print(f"\n━━━ [{t['kind']}] {t['name']} (founder={t['founder_name']!r}) ━━━\n{t['first_touch']}")


if __name__ == "__main__":
    main()
