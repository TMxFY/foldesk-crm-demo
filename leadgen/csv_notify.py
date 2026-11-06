#!/usr/bin/env python3
"""Send complete-format CSV exports for new ``Все2`` leads.

Tracks a watermark (last_sent_row) in .tg_pipeline/csv_export_state.json.
On each run: read the "Все2" sheet, count rows below the watermark; if there are
>= THRESHOLD new leads, attach that new batch with the real header and all columns
A:O.  The attachment intentionally contains no older leads.

Called at the end of yc_ph_leads.py and tg_pipeline.sh (best-effort, never fatal).

  python3 scripts/csv_notify.py            # check + send if >= 50
  python3 scripts/csv_notify.py --force    # send pending new rows (>=1)
  python3 scripts/csv_notify.py --resend-range 233:339  # resend exact rows
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse

import requests

try:
    from followup_agent import generate_followups_for_rows, smart_fallback_followups
    from lead_market import market_of, segment_of
    from outreach_copy import followup_one, followup_two
except ImportError:
    try:
        from leadgen.followup_agent import generate_followups_for_rows, smart_fallback_followups
        from leadgen.lead_market import market_of, segment_of
        from leadgen.outreach_copy import followup_one, followup_two
    except ImportError:
        from scripts.followup_agent import generate_followups_for_rows, smart_fallback_followups
        from scripts.lead_market import market_of, segment_of
        from scripts.outreach_copy import followup_one, followup_two

SHEET_ID = "1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
TAB = "Все2"
TABS = (
    ("Все2", "last_sent_row", "tg"),
    ("Direct", "last_sent_row_direct", "direct"),
    ("X", "last_sent_row_x", "x"),
)
THRESHOLD = int(os.environ.get("CSV_NOTIFY_THRESHOLD", "1"))
_DEFAULT_STATE_DIR = os.environ.get("LEADGEN_STATE_DIR") or (
    "/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
    if os.path.isdir("/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "leadgen_state")
)
STATE = os.path.join(_DEFAULT_STATE_DIR, "csv_export_state.json")
TIM_ASSISTANT_ENV = "/Users/timofei/tim-assistant/.env"
CHAT_ID = "764309033"

COLUMN_COUNT = 19  # A:O plus Рынок, Сегмент, Фоллоуап 1, Фоллоуап 2
_SHEET_WIDTH = 15
_OUTREACH_HEADERS = ("Рынок", "Сегмент", "Фоллоуап 1", "Фоллоуап 2")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def gsheets_token():
    import subprocess
    env = dict(os.environ)
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")
    return subprocess.check_output(["/Users/timofei/.local/bin/gsheets-token"], env=env).decode().strip()


def bot_token():
    try:
        for line in open(TIM_ASSISTANT_ENV):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def read_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"last_sent_row": 1, "last_sent_row_direct": 1, "last_sent_row_x": 1}


def write_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    s["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(s, open(STATE, "w"), indent=1)


def sheet_rows(tab=TAB):
    token = gsheets_token()
    sheet_enc = urllib.parse.quote(tab)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{sheet_enc}!A:O"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def contact_has_telegram(row):
    contact = row[6] if len(row) > 6 else ""
    return "t.me" in str(contact).lower()


def contact_has_x(row):
    contact = str(row[6] if len(row) > 6 else "").lower()
    return "x.com/" in contact or "twitter.com/" in contact


def contact_has_direct(row):
    contact = str(row[6] if len(row) > 6 else "").strip()
    if not contact:
        return False
    lowered = contact.lower()
    if "t.me/" in lowered or "telegram.me/" in lowered:
        return False
    return True


def _sheet_width(row):
    row = list(row)[:_SHEET_WIDTH]
    if len(row) < _SHEET_WIDTH:
        row.extend([""] * (_SHEET_WIDTH - len(row)))
    return row


def _cell(row, index):
    return row[index] if len(row) > index else ""


def _outreach_cells(row, followups=None):
    category = str(_cell(row, 3))
    name = str(_cell(row, 4))
    site = str(_cell(row, 5))
    angle = str(_cell(row, 7))
    our_angle = str(_cell(row, 8))
    first_touch = str(_cell(row, 10))
    if followups is not None:
        f1, f2 = followups
    else:
        f1, f2 = smart_fallback_followups(name, site, category, angle, our_angle, first_touch)
    return [
        "foreign",
        segment_of(" ".join((category, name, angle))),
        f1,
        f2,
    ]


def pack_for_send(header, body, *, use_llm=False):
    """t.me rows outside the RU market, plus market, segment, and follow-ups."""
    bases = []
    for row in body:
        if not contact_has_telegram(row):
            continue
        name = _cell(row, 4)
        angle = _cell(row, 7)
        if market_of(str(_cell(row, 5)), f"{name} {angle}") == "ru":
            continue
        bases.append(_sheet_width(row))
    followups_list = generate_followups_for_rows(bases, use_llm=use_llm) if bases else []
    kept = [
        base + _outreach_cells(base, followups=fpair)
        for base, fpair in zip(bases, followups_list)
    ]
    return [_sheet_width(header) + list(_OUTREACH_HEADERS)] + kept


def pack_tab_for_send(tab, header, body, *, use_llm=False):
    """Pack rows for Все2 (TG), Direct, or X outside the RU market."""
    if tab == "Все2":
        return pack_for_send(header, body, use_llm=use_llm)
    pred = contact_has_x if tab == "X" else contact_has_direct
    bases = []
    for row in body:
        if not pred(row):
            continue
        name = _cell(row, 4)
        angle = _cell(row, 7)
        if market_of(str(_cell(row, 5)), f"{name} {angle}") == "ru":
            continue
        bases.append(_sheet_width(row))
    followups_list = generate_followups_for_rows(bases, use_llm=use_llm) if bases else []
    kept = [
        base + _outreach_cells(base, followups=fpair)
        for base, fpair in zip(bases, followups_list)
    ]
    return [_sheet_width(header) + list(_OUTREACH_HEADERS)] + kept


def export_csv(rows):
    """Return a rectangular UTF-8 CSV with the supplied header and rows."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        row = list(row) + [""] * (COLUMN_COUNT - len(row))
        writer.writerow(row[:COLUMN_COUNT])
    return buf.getvalue()


def _tab_slug(tab):
    for t_name, _key, slug in TABS:
        if t_name == tab:
            return slug
    return "leads"


def send_csv(csv_text, count, first_row, last_row, *, resend=False, tab=TAB):
    token = bot_token()
    if not token:
        log("no bot token — skip send")
        return False
    slug = _tab_slug(tab)
    fname = f"leads_{slug}_{first_row}-{last_row}_{time.strftime('%Y%m%d_%H%M')}.csv"
    if resend:
        caption = (f"📥 Повторная выгрузка {count} лидов из «{tab}» (строки {first_row}–{last_row}).\n"
                   "Во вложении только эта новая партия, с колонками A:O.")
    else:
        caption = (f"📥 {count} новых лидов в «{tab}» (строки {first_row}–{last_row}).\n"
                   "Во вложении только эта новая партия, с колонками A:O.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data={"chat_id": CHAT_ID, "caption": caption},
        files={"document": (fname, csv_text.encode("utf-8-sig"), "text/csv")},
        timeout=60)
    if r.json().get("ok"):
        log(f"CSV sent ({tab}): {fname} ({count} leads)")
        return True
    log(f"send failed ({tab}): {r.text[:200]}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="send even if < threshold")
    ap.add_argument("--tab", default="", help="only process a single tab (Все2, Direct, X)")
    ap.add_argument("--resend-range", metavar="FIRST:LAST",
                    help="resend inclusive sheet rows without advancing the watermark")
    args = ap.parse_args()

    state = read_state()

    if args.resend_range:
        tab = args.tab or TAB
        rows = sheet_rows(tab)
        total = len(rows)
        try:
            first, last_row = (int(value) for value in args.resend_range.split(":", 1))
        except ValueError:
            ap.error("--resend-range must be FIRST:LAST, for example 233:339")
        if first < 2 or last_row < first or last_row > total:
            ap.error(f"range must be between rows 2 and {total}")
        selected = pack_tab_for_send(tab, rows[0], rows[first - 1:last_row], use_llm=True)
        count = len(selected) - 1
        if count <= 0:
            log(f"[{tab}] nothing sendable in range")
            return
        if send_csv(export_csv(selected), count, first, last_row, resend=True, tab=tab):
            log(f"[{tab}] batch CSV resent: rows {first}-{last_row} ({count} leads)")
        return

    targets = [t for t in TABS if not args.tab or t[0] == args.tab]
    for tab, state_key, _slug in targets:
        try:
            rows = sheet_rows(tab)
        except Exception as exc:
            log(f"[{tab}] failed to read sheet: {exc}")
            continue
        total = len(rows)
        last = int(state.get(state_key, 1))
        new_count = total - last
        log(f"[{tab}] sheet rows: {total} (incl header), watermark: {last}, new: {new_count}")
        if new_count <= 0 or total <= 1:
            continue
        selected = pack_tab_for_send(tab, rows[0], rows[last:], use_llm=True)
        sendable = len(selected) - 1
        if sendable <= 0:
            log(f"[{tab}] no foreign sendable rows in new range; advancing watermark to {total}")
            state[state_key] = total
            write_state(state)
            continue
        if sendable < THRESHOLD and not args.force:
            log(f"[{tab}] {sendable} < {THRESHOLD} new, not sending (use --force to send anyway)")
            continue
        csv_text = export_csv(selected)
        if send_csv(csv_text, sendable, last + 1, total, tab=tab):
            state[state_key] = total
            write_state(state)
            log(f"[{tab}] watermark advanced to {total}")


if __name__ == "__main__":
    main()
