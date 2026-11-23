#!/usr/bin/env python3
"""Push discovered leads directly into Foldesk CRM via MCP (JSON-RPC 2.0).

Replaces legacy CSV Telegram exports (csv_notify.py) with:
1. 2-step Micro-Ping conversion flow (`TOUCH_MODE=micro_ping` by default):
   - `step1_text`: short 1-line personal question (`micro_ping_first_touch`, <=120 chars, no links)
   - `step2_text` + `our_angle` / `hook`: full offer with the 183.5M views / 90 days / 13-40x case
   - `step3_text`: niche proof / concrete video angle follow-up
2. Watermark-tracked Google Sheet reader (`Все2`, `Direct`, `X` in `crm_mcp_push_state.json`)
   plus direct `--jsonl <path>` ingestion.
3. Dual MCP transport:
   - HTTP MCP (`POST ${CRMTG_MCP_URL}/mcp` with `Authorization: Bearer ${MCP_API_KEY}`)
   - Local in-process MCP (`app.mcp_server.handle_jsonrpc_message`)
   with automatic fallback to `create_or_update_lead` + `import_campaign_csv` if
   `ingest_discovered_leads` is not yet registered on a remote server.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import fcntl
import io
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import requests

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

try:
    from followup_agent import (
        clean_product_name,
        generate_followups_for_rows,
        smart_fallback_followups,
    )
    from lead_market import market_of, segment_of
    from outreach_copy import (
        REVSHARE_OFFER_TEXT,
        VLAD_STEP2_TEMPLATE,
        VLAD_STEP3_IMAGE_URL,
        VLAD_STEP3_TEXT,
        _extract_personal_first_name,
        followup_one,
        followup_two,
        generate_vlad_sonnet_batch,
        infer_lead_audience,
        micro_ping_first_touch,
        upgrade_to_vlad_step1,
    )
    from supply_side import (
        NON_BUYER_ROLES,
        has_insufficient_product_info,
        is_invalid_product_name,
        is_rejected_buyer_category,
        is_supply_side,
        pick_best_product_url,
    )
except ImportError:
    from leadgen.followup_agent import (
        clean_product_name,
        generate_followups_for_rows,
        smart_fallback_followups,
    )
    from leadgen.lead_market import market_of, segment_of
    from leadgen.outreach_copy import (
        REVSHARE_OFFER_TEXT,
        VLAD_STEP2_TEMPLATE,
        VLAD_STEP3_IMAGE_URL,
        VLAD_STEP3_TEXT,
        _extract_personal_first_name,
        followup_one,
        followup_two,
        generate_vlad_sonnet_batch,
        infer_lead_audience,
        micro_ping_first_touch,
        upgrade_to_vlad_step1,
    )
    from leadgen.supply_side import (
        NON_BUYER_ROLES,
        has_insufficient_product_info,
        is_invalid_product_name,
        is_rejected_buyer_category,
        is_supply_side,
        pick_best_product_url,
    )

SHEET_ID = "1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
TAB = "Все2"
TABS = (
    ("Все2", "last_sent_row", "tg"),
    ("Direct", "last_sent_row_direct", "direct"),
    ("X", "last_sent_row_x", "x"),
)
THRESHOLD = int(os.environ.get("CRM_MCP_PUSH_THRESHOLD", os.environ.get("CSV_NOTIFY_THRESHOLD", "1")))

_DEFAULT_STATE_DIR = os.environ.get("LEADGEN_STATE_DIR") or str(WORKTREE_ROOT / "data" / "leadgen_state")
STATE_PATH = os.path.join(_DEFAULT_STATE_DIR, "crm_mcp_push_state.json")
LEGACY_CSV_STATE_PATH = os.path.join(_DEFAULT_STATE_DIR, "csv_export_state.json")
_SHEET_WIDTH = 15


def _load_ui_leadgen_config() -> dict[str, Any]:
    try:
        from app.leadgen_settings import get_leadgen_config

        return get_leadgen_config()
    except Exception:
        pass
    cfg_path = Path(os.environ.get("LEADGEN_CONFIG_PATH") or (WORKTREE_ROOT / "data" / "leadgen_config.json"))
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _resolve_b2b_touch_mode(explicit: str | None = None) -> str:
    if explicit:
        mode = explicit.strip().lower()
    elif os.environ.get("LEADGEN_TOUCH_MODE"):
        mode = os.environ["LEADGEN_TOUCH_MODE"].strip().lower()
    else:
        b2b = (_load_ui_leadgen_config().get("b2b") or {})
        mode = str(b2b.get("touch_mode") or "direct").strip().lower()
    return "micro_ping" if mode == "micro_ping" else "direct"


def log(*args: Any) -> None:
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


def gsheets_token() -> str:
    import subprocess

    env = dict(os.environ)
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")
    return (
        subprocess.check_output(["/Users/timofei/.local/bin/gsheets-token"], env=env)
        .decode()
        .strip()
    )


@contextmanager
def acquire_push_state_lock(state_path: str = STATE_PATH) -> Iterator[None]:
    lock_path = f"{state_path}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def read_state(state_path: str = STATE_PATH) -> dict[str, Any]:
    for candidate in (state_path, LEGACY_CSV_STATE_PATH):
        try:
            with open(candidate, encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            if isinstance(data, dict):
                return {
                    "last_sent_row": int(data.get("last_sent_row", 1)),
                    "last_sent_row_direct": int(data.get("last_sent_row_direct", 1)),
                    "last_sent_row_x": int(data.get("last_sent_row_x", 1)),
                }
        except Exception:
            continue
    return {"last_sent_row": 1, "last_sent_row_direct": 1, "last_sent_row_x": 1}


def write_state(state: dict[str, Any], state_path: str = STATE_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
    payload = dict(state)
    payload["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp_path = f"{state_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(tmp_path, state_path)


def sheet_rows(tab: str = TAB) -> list[list[str]]:
    token = gsheets_token()
    sheet_enc = urllib.parse.quote(tab)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{sheet_enc}!A:S"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("values", [])


def _cell(row: list[Any], index: int) -> str:
    return str(row[index]).strip() if len(row) > index and row[index] is not None else ""


def contact_has_telegram(row: list[Any]) -> bool:
    return "t.me" in _cell(row, 6).lower()


def contact_has_x(row: list[Any]) -> bool:
    contact = _cell(row, 6).lower()
    return "x.com/" in contact or "twitter.com/" in contact


def contact_has_direct(row: list[Any]) -> bool:
    contact = _cell(row, 6)
    if not contact:
        return False
    lowered = contact.lower()
    return "t.me/" not in lowered and "telegram.me/" not in lowered


def _normalize_e164_wa_phone(raw: Any) -> str:
    """Normalize a wa.me link, api.whatsapp.com send link, or E.164 text into +<digits>."""
    text = str(raw or "").strip()
    if not text:
        return ""
    for pat in (
        r"(?:wa\.me/(?:c/)?|\bphone=)(\+?[%\d][%\d\s().\-]{6,18})",
        r"^\s*(\+\d[\d\s().\-]{6,18}\d)\s*$",
        r"(?:whatsapp|wa)\s*(?:/\s*tel[^:\n]{0,18})?\s*[:\-–—]?\s*(\+\d[\d\s().\-]{6,18}\d)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            digits = re.sub(r"\D", "", urllib.parse.unquote(m.group(1)))
            if 8 <= len(digits) <= 15 and len(set(digits)) > 1:
                return f"+{digits}"
    return ""


def _extract_contact_fields(contact_str: str, tg_col: str = "", email_col: str = "") -> dict[str, str]:
    """Parse username, phone, email, provider from sheet/JSONL contact strings."""
    out = {"username": "", "phone": "", "email": (email_col or "").strip(), "provider": "telegram"}
    raw_tg = (tg_col or "").strip()
    raw_contact = (contact_str or "").strip()

    for candidate in (raw_contact, raw_tg):
        if not candidate:
            continue
        m_tg = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{3,32})", candidate, re.I)
        if m_tg:
            out["username"] = m_tg.group(1)
            break
        if candidate.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{3,32}", candidate):
            out["username"] = candidate.lstrip("@")
            break
        if re.fullmatch(r"[A-Za-z0-9_]{4,32}", candidate) and candidate == raw_tg:
            out["username"] = candidate
            break

    norm_wa = _normalize_e164_wa_phone(raw_contact)
    if norm_wa:
        out["phone"] = norm_wa
        if not out["username"]:
            out["provider"] = "whatsapp"

    if not out["email"] and "@" in raw_contact and " " not in raw_contact and not raw_contact.startswith("@"):
        clean_mail = raw_contact.removeprefix("mailto:").strip()
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean_mail):
            out["email"] = clean_mail

    return out


def _has_canonical_vlad_copy(text: str | None) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    low = s.lower()
    if (
        "creatoros" in low
        or "if your user growth has hit a ceiling" in low
        or "plug-and-play" in low
    ):
        return False
    return "SoftUGC" in s and REVSHARE_OFFER_TEXT in s


def _is_vacancy_source_or_cat(source: str | None, category: str | None) -> bool:
    s_low = str(source or "").lower()
    c_low = str(category or "").lower()
    return any(k in s_low for k in ("ваканс", "vacanc", "job_monitor")) or any(
        k in c_low for k in ("ваканс", "vacanc")
    )


def _should_auto_sonnet() -> bool:
    key = (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    if not key:
        return False
    try:
        from app.runtime_config import should_allow_auto_sonnet

        return should_allow_auto_sonnet()
    except ImportError:
        if os.environ.get("CRMTG_AUTO_SONNET") == "1" or os.environ.get("CRMTG_VPS_RUNTIME") == "1":
            return True
        if os.environ.get("CRMTG_EPHEMERAL_RUNTIME") == "1":
            return False
        return True


def _apply_sonnet_batch_to_records(records: list[dict[str, Any]]) -> None:
    """In-place generate first_touch + followup_1 ({audience}) + followup_2 via claude-sonnet-5 for B2B records lacking canonical Vlad copy."""
    if not records or not _should_auto_sonnet():
        return
    need_idx: list[int] = []
    need_inputs: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        src = str(rec.get("lead_source") or rec.get("source") or "")
        cat = str(rec.get("category") or rec.get("niche") or "")
        if _is_vacancy_source_or_cat(src, cat):
            continue
        raw_ft = str(rec.get("first_touch") or rec.get("step1_text") or "").strip()
        if _has_canonical_vlad_copy(raw_ft):
            continue
        need_idx.append(idx)
        need_inputs.append(
            {
                "company": rec.get("company") or rec.get("project") or rec.get("product") or rec.get("name") or "",
                "name": rec.get("founder_name") or rec.get("name") or "",
                "website": rec.get("website") or rec.get("site") or rec.get("project_url") or rec.get("url") or "",
                "category": cat,
                "their_angle": rec.get("their_angle") or rec.get("one_liner") or rec.get("bio") or rec.get("description") or "",
                "source": src,
            }
        )
    if not need_inputs:
        return

    for start in range(0, len(need_inputs), 15):
        chunk_i = need_idx[start : start + 15]
        chunk_in = need_inputs[start : start + 15]
        try:
            gen = generate_vlad_sonnet_batch(chunk_in, model="claude-sonnet-5")
        except Exception:
            continue
        for rec_i, g, inp in zip(chunk_i, gen, chunk_in):
            if not isinstance(g, dict):
                continue
            s1 = str(g.get("step1_text") or "").strip()
            aud = str(g.get("audience") or "").strip().lower()
            if not s1:
                continue
            target = records[rec_i]
            target["first_touch"] = s1
            target["step1_text"] = s1
            if aud:
                target["audience"] = aud
            f1 = followup_one(
                str(inp.get("company") or inp.get("name") or ""),
                str(inp.get("their_angle") or ""),
                audience=aud,
                category=str(inp.get("category") or ""),
                site=str(inp.get("website") or ""),
            )
            target["followup_1"] = f1
            target["step2_text"] = f1
            target["followup_2"] = VLAD_STEP3_TEXT
            target["step3_text"] = VLAD_STEP3_TEXT


def _ensure_full_case_pitch(
    raw_first_touch: str,
    *,
    company: str,
    name: str = "",
    site: str = "",
    category: str,
    their_angle: str,
    source: str = "",
) -> str:
    """Guarantee Vlad's canonical 3-paragraph SoftUGC pitch (183.5M views / 90 days / 13-40x + revshare/CPA CTA)."""
    return upgrade_to_vlad_step1(
        raw_first_touch,
        company=company,
        name=name,
        site=site,
        category=category,
        their_angle=their_angle,
        source=source,
    )


def build_lead_payload_from_sheet_row(
    row: list[Any],
    *,
    followups: tuple[str, str] | None = None,
    touch_mode: str | None = None,
    lead_source: str = "Google Sheet",
) -> dict[str, Any]:
    """Convert a 15..19 column Google Sheet row into an `ingest_discovered_leads` item."""
    mode = _resolve_b2b_touch_mode(touch_mode)
    tg_col = _cell(row, 0)
    category = _cell(row, 3)
    raw_name = _cell(row, 4)
    site = _cell(row, 5)
    contact = _cell(row, 6)
    their_angle = _cell(row, 7)
    raw_our_angle = _cell(row, 8)
    raw_hook = _cell(row, 9)
    raw_first_touch = _cell(row, 10)
    email_col = _cell(row, 15)

    clean_comp = clean_product_name(raw_name, site, their_angle)
    personal_first = _extract_personal_first_name(raw_name, clean_comp)
    contact_info = _extract_contact_fields(contact, tg_col=tg_col, email_col=email_col)
    audience = infer_lead_audience(
        category=category,
        company=clean_comp or raw_name,
        their_angle=their_angle,
        site=site,
    )

    if (
        mode != "micro_ping"
        and not _is_vacancy_source_or_cat(lead_source, category)
        and not _has_canonical_vlad_copy(raw_first_touch)
        and _should_auto_sonnet()
    ):
        tmp_rec: dict[str, Any] = {
            "company": clean_comp or raw_name,
            "name": raw_name,
            "website": site,
            "category": category,
            "their_angle": their_angle,
            "lead_source": lead_source,
            "first_touch": raw_first_touch,
        }
        _apply_sonnet_batch_to_records([tmp_rec])
        if _has_canonical_vlad_copy(tmp_rec.get("first_touch")):
            raw_first_touch = str(tmp_rec["first_touch"])
            if tmp_rec.get("audience"):
                audience = str(tmp_rec["audience"])
            followups = (
                str(tmp_rec.get("followup_1") or followup_one(clean_comp or raw_name, their_angle, audience=audience, category=category, site=site)),
                VLAD_STEP3_TEXT,
            )

    full_pitch = _ensure_full_case_pitch(
        raw_first_touch,
        company=clean_comp,
        name=raw_name,
        site=site,
        category=category,
        their_angle=their_angle,
        source=lead_source,
    )
    if followups is not None:
        f1, f2 = followups
    else:
        f1 = followup_one(clean_comp or raw_name, their_angle, audience=audience, category=category, site=site)
        f2 = followup_two(clean_comp or raw_name)

    ping_text = micro_ping_first_touch(
        name=personal_first,
        company=clean_comp or raw_name,
        category=category,
        site=site,
        angle=their_angle,
    )

    if mode == "micro_ping":
        step1_text = ping_text
        step2_text = full_pitch
        step3_text = f1
        hook = full_pitch
        if raw_our_angle and "183.5" not in raw_our_angle:
            our_angle = f"{raw_our_angle}\n\n{full_pitch}"
        else:
            our_angle = raw_our_angle or full_pitch
    else:
        step1_text = full_pitch
        step2_text = f1
        step3_text = f2
        hook = raw_hook or ping_text
        our_angle = raw_our_angle or full_pitch

    note_parts = []
    if contact and not contact_info["username"]:
        note_parts.append(f"Контакт: {contact}")
    if _cell(row, 12):
        note_parts.append(f"Founder X: {_cell(row, 12)}")
    if _cell(row, 14):
        note_parts.append(f"Founder LinkedIn: {_cell(row, 14)}")

    return {
        "username": contact_info["username"] or None,
        "name": raw_name or clean_comp or contact_info["username"] or None,
        "company": clean_comp or raw_name or None,
        "phone": contact_info["phone"] or None,
        "email": contact_info["email"] or None,
        "website": site or None,
        "category": category or None,
        "their_angle": their_angle or None,
        "our_angle": our_angle or None,
        "hook": hook or None,
        "audience": audience,
        "step1_text": step1_text,
        "step2_text": step2_text,
        "step3_text": step3_text,
        "lead_source": lead_source,
        "note": "\n".join(note_parts) if note_parts else None,
        "provider": contact_info["provider"],
    }


def build_lead_payload_from_dict(
    record: dict[str, Any],
    *,
    touch_mode: str | None = None,
) -> dict[str, Any]:
    """Convert a JSONL lead dict (from TG pipeline, YC/PH, channel graph, or ad spy) into MCP format."""
    mode = _resolve_b2b_touch_mode(touch_mode)
    raw_name = str(
        record.get("founder_name")
        or record.get("name")
        or record.get("display_name")
        or ""
    ).strip()
    raw_company = str(
        record.get("company")
        or record.get("project")
        or record.get("product")
        or record.get("name")
        or ""
    ).strip()
    site = pick_best_product_url(record)
    category = str(record.get("category") or record.get("niche") or "SaaS").strip()
    their_angle = str(
        record.get("their_angle")
        or record.get("one_liner")
        or record.get("bio")
        or record.get("description")
        or ""
    ).strip()
    raw_our_angle = str(record.get("our_angle") or "").strip()
    raw_hook = str(record.get("hook") or "").strip()
    raw_first_touch = str(record.get("first_touch") or record.get("step1_text") or "").strip()
    raw_source = str(record.get("lead_source") or record.get("source") or "").strip()
    if not raw_source and record.get("group"):
        raw_source = "tg_groups"
    source = raw_source or "leadgen"

    username_raw = str(
        record.get("username")
        or record.get("tg_username")
        or record.get("telegram_username")
        or record.get("contact")
        or ""
    ).strip()
    email_raw = str(record.get("email") or record.get("work_email") or "").strip()
    contact_info = _extract_contact_fields(username_raw, tg_col=username_raw, email_col=email_raw)
    norm_wa_phone = _normalize_e164_wa_phone(record.get("whatsapp") or record.get("phone") or contact_info["phone"])
    raw_plain_phone = str(record.get("phone") or "").strip()
    if raw_plain_phone.lower().startswith(("http://", "https://")) or "wa.me/" in raw_plain_phone.lower():
        raw_plain_phone = ""
    phone = norm_wa_phone or raw_plain_phone or str(contact_info["phone"] or "").strip()
    raw_tg_uid = record.get("telegram_user_id") or record.get("user_id") or record.get("id") or None
    is_tg_phone_direct = bool(record.get("tg_phone_direct")) or (
        str(record.get("provider") or "").strip().lower() == "telegram"
        and not contact_info["username"]
        and bool(phone)
        and raw_tg_uid is not None
    )
    has_wa_signal = (
        False
        if is_tg_phone_direct
        else bool(
            norm_wa_phone
            and (
                record.get("whatsapp")
                or str(record.get("provider") or "").strip().lower() == "whatsapp"
                or str(source).strip().lower() == "d2c_whatsapp"
                or contact_info["provider"] == "whatsapp"
            )
        )
    )

    clean_comp = clean_product_name(raw_company, site, their_angle)
    personal_first = _extract_personal_first_name(raw_name, clean_comp or raw_company)
    explicit_aud = str(record.get("audience") or record.get("target_audience") or "").strip().lower()
    audience = explicit_aud if explicit_aud in {"buyers", "students", "users", "players", "listeners"} else infer_lead_audience(
        category=category,
        company=clean_comp or raw_company,
        their_angle=their_angle,
        site=site,
    )

    if (
        mode != "micro_ping"
        and not _is_vacancy_source_or_cat(source, category)
        and not _has_canonical_vlad_copy(raw_first_touch)
        and _should_auto_sonnet()
    ):
        tmp_rec_d: dict[str, Any] = {
            "company": clean_comp or raw_company,
            "name": raw_name,
            "website": site,
            "category": category,
            "their_angle": their_angle,
            "lead_source": source,
            "first_touch": raw_first_touch,
        }
        _apply_sonnet_batch_to_records([tmp_rec_d])
        if _has_canonical_vlad_copy(tmp_rec_d.get("first_touch")):
            raw_first_touch = str(tmp_rec_d["first_touch"])
            if tmp_rec_d.get("audience"):
                audience = str(tmp_rec_d["audience"])
            record = dict(record)
            record["first_touch"] = raw_first_touch
            record["step1_text"] = raw_first_touch
            record["followup_1"] = tmp_rec_d.get("followup_1")
            record["step2_text"] = tmp_rec_d.get("step2_text")
            record["followup_2"] = VLAD_STEP3_TEXT
            record["step3_text"] = VLAD_STEP3_TEXT

    full_pitch = _ensure_full_case_pitch(
        raw_first_touch,
        company=clean_comp or raw_company,
        name=raw_name,
        site=site,
        category=category,
        their_angle=their_angle,
        source=source,
    )
    f1_default = followup_one(clean_comp or raw_company, their_angle, audience=audience, category=category, site=site)
    f2_default = followup_two(clean_comp or raw_company)
    f1 = str(record.get("followup_1") or record.get("step2_text") or f1_default).strip()
    f2 = str(record.get("followup_2") or record.get("step3_text") or f2_default).strip()

    ping_text = micro_ping_first_touch(
        name=personal_first or raw_name,
        company=clean_comp or raw_company,
        category=category,
        site=site,
        angle=their_angle,
    )

    if mode == "micro_ping":
        step1_text = ping_text
        step2_text = full_pitch
        step3_text = str(record.get("step3_text") or record.get("followup_1") or f1_default).strip()
        hook = full_pitch
        if raw_our_angle and "183.5" not in raw_our_angle:
            our_angle = f"{raw_our_angle}\n\n{full_pitch}"
        else:
            our_angle = raw_our_angle or full_pitch
    else:
        step1_text = full_pitch
        step2_text = f1
        step3_text = f2
        hook = raw_hook or ping_text
        our_angle = raw_our_angle or full_pitch

    payload_out = {
        "username": contact_info["username"] or None,
        "name": raw_name or clean_comp or raw_company or contact_info["username"] or None,
        "company": clean_comp or raw_company or None,
        "phone": phone or None,
        "email": contact_info["email"] or None,
        "website": site or None,
        "category": category or None,
        "their_angle": their_angle or None,
        "our_angle": our_angle or None,
        "hook": hook or None,
        "audience": audience,
        "step1_text": step1_text,
        "step2_text": step2_text,
        "step3_text": step3_text,
        "lead_source": source,
        "note": str(record.get("note") or "").strip() or None,
        "telegram_user_id": raw_tg_uid,
        "provider": "telegram" if (contact_info["username"] or is_tg_phone_direct) else ("whatsapp" if has_wa_signal else (str(record.get("provider") or "").strip().lower() if str(record.get("provider") or "").strip().lower() in ("telegram", "whatsapp") else (contact_info["provider"] or "telegram"))),
    }
    if is_tg_phone_direct:
        payload_out["tg_phone_direct"] = True
    return payload_out


def _passes_sheet_buyer_gate(row: list[Any]) -> bool:
    cat = _cell(row, 3)
    name = _cell(row, 4)
    site = _cell(row, 5)
    angle = _cell(row, 7)
    our_angle = _cell(row, 8)
    first_touch = _cell(row, 10)
    if is_rejected_buyer_category(cat):
        return False
    blob = f"{name}\n{angle}\n{our_angle}"
    if is_supply_side(blob) or has_insufficient_product_info(blob) or has_insufficient_product_info(first_touch):
        return False
    clean_comp = clean_product_name(name, site, angle)
    if is_invalid_product_name(clean_comp or name):
        return False
    return True


def _passes_dict_buyer_gate(item: dict[str, Any]) -> bool:
    src = str(item.get("lead_source") or item.get("source") or "")
    cat = str(item.get("category") or item.get("niche") or "")
    if _is_vacancy_source_or_cat(src, cat):
        return True
    if is_rejected_buyer_category(cat):
        return False
    roles = {str(x).strip() for x in (item.get("roles") or []) if str(x or "").strip()}
    if item.get("role"):
        roles.add(str(item["role"]).strip())
    if roles and not (roles & {"Founder", "Marketing"}) and (roles & NON_BUYER_ROLES):
        return False
    name = str(item.get("name") or "")
    comp = str(item.get("company") or item.get("project") or item.get("product") or name)
    site = str(item.get("website") or item.get("site") or item.get("project_url") or "")
    angle = str(item.get("their_angle") or item.get("one_liner") or item.get("bio") or item.get("what_they_do") or "")
    blob = "\n".join(x for x in (name, comp, angle, str(item.get("bio") or ""), str(item.get("our_angle") or "")) if x)
    if is_supply_side(blob) or has_insufficient_product_info(blob):
        return False
    clean_comp = clean_product_name(comp, site, angle)
    if is_invalid_product_name(clean_comp or comp):
        return False
    return True


def _pack_tab_leads_with_offsets(
    tab: str,
    body: list[list[Any]],
    *,
    use_llm: bool = False,
    touch_mode: str | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    if tab == "Все2":
        pred = contact_has_telegram
    elif tab == "X":
        pred = contact_has_x
    else:
        pred = contact_has_direct

    bases: list[list[str]] = []
    consumed_offsets: list[int] = []
    for offset_idx, row in enumerate(body, start=1):
        if not pred(row):
            continue
        name = _cell(row, 4)
        angle = _cell(row, 7)
        if market_of(_cell(row, 5), f"{name} {angle}") == "ru":
            continue
        if not _passes_sheet_buyer_gate(row):
            continue
        padded = [str(c) for c in list(row)[:_SHEET_WIDTH]]
        if len(padded) < _SHEET_WIDTH:
            padded.extend([""] * (_SHEET_WIDTH - len(padded)))
        bases.append(padded)
        consumed_offsets.append(offset_idx)

    if not bases:
        return [], []

    lead_source = f"Google Sheet ({tab})"
    mode_resolved = _resolve_b2b_touch_mode(touch_mode)
    if mode_resolved != "micro_ping" and _should_auto_sonnet():
        sheet_sonnet_recs: list[dict[str, Any]] = []
        sheet_sonnet_pos: list[int] = []
        for idx_b, b in enumerate(bases):
            raw_ft_b = _cell(b, 10)
            if not _has_canonical_vlad_copy(raw_ft_b):
                sheet_sonnet_pos.append(idx_b)
                sheet_sonnet_recs.append(
                    {
                        "company": clean_product_name(_cell(b, 4), _cell(b, 5), _cell(b, 7)) or _cell(b, 4),
                        "name": _cell(b, 4),
                        "website": _cell(b, 5),
                        "category": _cell(b, 3),
                        "their_angle": _cell(b, 7),
                        "lead_source": lead_source,
                        "first_touch": raw_ft_b,
                    }
                )
        if sheet_sonnet_recs:
            _apply_sonnet_batch_to_records(sheet_sonnet_recs)
            followups_list = generate_followups_for_rows(bases, use_llm=use_llm)
            sonnet_by_pos = dict(zip(sheet_sonnet_pos, sheet_sonnet_recs))
            for pos_i, srec in sonnet_by_pos.items():
                if _has_canonical_vlad_copy(srec.get("first_touch")):
                    bases[pos_i][10] = str(srec["first_touch"])
                    followups_list[pos_i] = (
                        str(srec.get("followup_1") or followups_list[pos_i][0]),
                        VLAD_STEP3_TEXT,
                    )
            return (
                [
                    build_lead_payload_from_sheet_row(
                        base,
                        followups=fpair,
                        touch_mode=touch_mode,
                        lead_source=lead_source,
                    )
                    for base, fpair in zip(bases, followups_list)
                ],
                consumed_offsets,
            )

    followups_list = generate_followups_for_rows(bases, use_llm=use_llm)
    return (
        [
            build_lead_payload_from_sheet_row(
                base,
                followups=fpair,
                touch_mode=touch_mode,
                lead_source=lead_source,
            )
            for base, fpair in zip(bases, followups_list)
        ],
        consumed_offsets,
    )


def pack_tab_leads_for_mcp(
    tab: str,
    body: list[list[Any]],
    *,
    use_llm: bool = False,
    touch_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Filter non-RU sendable Product-Buyer rows for a Google Sheet tab and pack into MCP lead dicts."""
    leads, _offsets = _pack_tab_leads_with_offsets(
        tab,
        body,
        use_llm=use_llm,
        touch_mode=touch_mode,
    )
    return leads


def load_leads_from_jsonl(
    jsonl_path: str | Path,
    *,
    touch_mode: str | None = None,
    use_llm: bool = False,
) -> list[dict[str, Any]]:
    """Load leads from a JSONL file (supports both dict records and list sheet rows)."""
    path = Path(jsonl_path)
    if not path.exists():
        return []
    sheet_like_rows: list[list[Any]] = []
    raw_dict_items: list[dict[str, Any]] = []
    dict_leads: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, list):
            sheet_like_rows.append(item)
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("company") or item.get("project") or "")
            angle = str(item.get("their_angle") or item.get("bio") or "")
            site = str(item.get("website") or item.get("site") or item.get("project_url") or "")
            if market_of(site, f"{name} {angle}") == "ru":
                continue
            if not _passes_dict_buyer_gate(item):
                continue
            raw_dict_items.append(dict(item))

    if raw_dict_items and _resolve_b2b_touch_mode(touch_mode) != "micro_ping":
        _apply_sonnet_batch_to_records(raw_dict_items)

    for item in raw_dict_items:
        built = build_lead_payload_from_dict(item, touch_mode=touch_mode)
        if any(built.get(k) for k in ("username", "name", "company", "phone", "telegram_user_id")):
            dict_leads.append(built)

    if sheet_like_rows:
        dict_leads.extend(
            pack_tab_leads_for_mcp("Все2", sheet_like_rows, use_llm=use_llm, touch_mode=touch_mode)
        )
    return dict_leads


def _call_mcp_jsonrpc(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    mcp_url: str | None = None,
    api_key: str | None = None,
    conn: Any = None,
    db_path: str | Path | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Execute an MCP `tools/call` request over HTTP or local in-process handler."""
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    resolved_url = (mcp_url if mcp_url is not None else os.environ.get("CRMTG_MCP_URL", "")).strip()
    resolved_key = (api_key if api_key is not None else os.environ.get("MCP_API_KEY", "")).strip()

    if resolved_url and conn is None:
        endpoint = resolved_url.rstrip("/")
        if not endpoint.endswith("/mcp"):
            endpoint = f"{endpoint}/mcp"
        headers = {"Content-Type": "application/json"}
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        http_client = session or requests
        resp = http_client.post(endpoint, headers=headers, json=rpc_payload, timeout=60)
        resp.raise_for_status()
        return resp.json()

    from app.db import connect, init_db
    from app.mcp_server import handle_jsonrpc_message

    owns_conn = False
    if conn is None:
        conn = connect(db_path)
        init_db(conn)
        owns_conn = True
    try:
        result = handle_jsonrpc_message(conn, rpc_payload)
        return result if isinstance(result, dict) else {"jsonrpc": "2.0", "id": 1, "error": {"message": "Empty MCP response"}}
    finally:
        if owns_conn:
            conn.close()


def _leads_to_campaign_csv(leads: list[dict[str, Any]]) -> str:
    headers = [
        "username",
        "name",
        "company",
        "phone",
        "website",
        "category",
        "their_angle",
        "our_angle",
        "hook",
        "step1_text",
        "step2_text",
        "step3_text",
        "note",
        "telegram_user_id",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for item in leads:
        writer.writerow([str(item.get(h) or "") for h in headers])
    return buf.getvalue()


def push_leads_via_mcp(
    leads: list[dict[str, Any]],
    *,
    enroll_in_campaign: bool = True,
    campaign_id: int | None = None,
    campaign_name: str = "Leadgen Autopilot",
    mcp_url: str | None = None,
    api_key: str | None = None,
    conn: Any = None,
    db_path: str | Path | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Push a batch of leads to CRM via MCP `ingest_discovered_leads` with fallback."""
    if not leads:
        return {
            "ok": True,
            "processed": 0,
            "created_leads": 0,
            "updated_leads": 0,
            "enrolled_in_campaign": 0,
            "skipped_duplicates": 0,
            "campaign_id": campaign_id,
            "lead_ids": [],
        }

    resolved_camp_name = str(campaign_name or "").strip() or "Leadgen Auto (TG)"
    if (
        campaign_id is None
        and conn is not None
        and resolved_camp_name.lower() in ("leadgen auto (tg)", "leadgen autopilot")
    ):
        try:
            from app.leadgen_settings import resolve_target_b2b_tg_campaign_name

            resolved_camp_name = resolve_target_b2b_tg_campaign_name(
                conn,
                resolved_camp_name,
                fallback_default=resolved_camp_name,
            )
        except Exception:
            pass

    resp = _call_mcp_jsonrpc(
        "ingest_discovered_leads",
        {
            "leads": leads,
            "enroll_in_campaign": enroll_in_campaign,
            "campaign_id": campaign_id,
            "campaign_name": resolved_camp_name,
        },
        mcp_url=mcp_url,
        api_key=api_key,
        conn=conn,
        db_path=db_path,
        session=session,
    )

    if "result" in resp and not resp["result"].get("isError"):
        structured = resp["result"].get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content_items = resp["result"].get("content") or []
        if content_items and isinstance(content_items[0], dict):
            return json.loads(content_items[0].get("text") or "{}")

    # Fallback if `ingest_discovered_leads` is not registered on an older server
    err_msg = ""
    if "error" in resp:
        err_msg = str((resp.get("error") or {}).get("message") or "")
    elif "result" in resp and resp["result"].get("isError"):
        err_msg = str((resp["result"].get("structuredContent") or {}).get("error") or "")

    if "ingest_discovered_leads" not in err_msg and "Method not found" not in err_msg and "Неизвестный MCP" not in err_msg:
        raise RuntimeError(f"MCP ingest_discovered_leads failed: {err_msg or resp}")

    created_leads = 0
    updated_leads = 0
    lead_ids: list[int] = []
    for item in leads:
        lead_args = {
            "name": item.get("name"),
            "username": item.get("username"),
            "phone": item.get("phone"),
            "company": item.get("company"),
            "niche": item.get("category"),
            "offer_details": item.get("our_angle"),
            "lead_source": item.get("lead_source"),
            "note": item.get("note") or item.get("their_angle"),
            "url": item.get("website"),
            "email": item.get("email"),
            "provider": item.get("provider") or "telegram",
        }
        sub = _call_mcp_jsonrpc(
            "create_or_update_lead",
            lead_args,
            mcp_url=mcp_url,
            api_key=api_key,
            conn=conn,
            db_path=db_path,
            session=session,
        )
        structured = (sub.get("result") or {}).get("structuredContent") or {}
        if structured.get("action") == "created":
            created_leads += 1
        elif structured.get("action") == "updated":
            updated_leads += 1
        lid = (structured.get("lead") or {}).get("id")
        if lid is not None:
            lead_ids.append(int(lid))

    enrolled = 0
    if enroll_in_campaign and conn is not None:
        from app.outreach import create_campaign, import_campaign_csv

        all_camps = conn.execute("SELECT id, name FROM campaigns ORDER BY id ASC").fetchall()
        matched_crow = next(
            (r for r in all_camps if str(r["name"] or "").strip().lower() == resolved_camp_name.strip().lower()),
            None,
        )
        if matched_crow is not None:
            cid = int(matched_crow["id"])
        else:
            is_vac = any(k in resolved_camp_name.lower() for k in ("ваканс", "vacanc", "job"))
            fallback_steps = (
                [
                    {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                    {"step_order": 2, "delay_hours": 24, "template_text": "{followup_1}"},
                    {"step_order": 3, "delay_hours": 72, "template_text": "{followup_2}"},
                ]
                if is_vac
                else [
                    {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
                    {"step_order": 2, "delay_hours": 24, "template_text": VLAD_STEP2_TEMPLATE},
                    {"step_order": 3, "delay_hours": 24, "template_text": VLAD_STEP3_TEXT, "image_url": VLAD_STEP3_IMAGE_URL},
                ]
            )
            created_c = create_campaign(
                conn,
                name=resolved_camp_name,
                status="draft",
                steps=fallback_steps,
            )
            cid = int(created_c["id"])
        sendable_leads = [
            l for l in leads
            if str(l.get("username") or "").strip() or str(l.get("phone") or "").strip()
        ]
        imp = (
            import_campaign_csv(conn, cid, _leads_to_campaign_csv(sendable_leads))
            if sendable_leads
            else {"imported": 0}
        )
        enrolled = int(imp.get("imported", 0))
        campaign_id = cid

    return {
        "ok": True,
        "processed": len(leads),
        "created_leads": created_leads,
        "updated_leads": updated_leads,
        "enrolled_in_campaign": enrolled,
        "skipped_duplicates": max(0, len(leads) - enrolled) if enroll_in_campaign else 0,
        "campaign_id": campaign_id,
        "lead_ids": lead_ids,
        "fallback": True,
    }


def main() -> int:
    ui_cfg = _load_ui_leadgen_config()
    b2b_cfg = ui_cfg.get("b2b") or {}
    default_touch = os.environ.get("LEADGEN_TOUCH_MODE") or "direct"
    default_camp_tg = os.environ.get("LEADGEN_CAMPAIGN_NAME") or str(
        b2b_cfg.get("target_campaign_tg") or "Leadgen Auto (TG)"
    )
    default_camp_wa = str(b2b_cfg.get("target_campaign_wa") or "Leadgen Auto (WA)")
    total_daily_quota = int(ui_cfg.get("total_daily_quota") or 50)
    b2b_daily_limit = int(b2b_cfg.get("campaign_daily_limit") or 30)
    effective_quota = min(total_daily_quota, b2b_daily_limit) if total_daily_quota > 0 else b2b_daily_limit

    ap = argparse.ArgumentParser(description="Push discovered leads into Foldesk CRM via MCP")
    ap.add_argument("--jsonl", default="", help="Ingest leads directly from a local JSONL file")
    ap.add_argument("--tab", default="", help="Only process a single Google Sheet tab (Все2, Direct, X)")
    ap.add_argument("--force", action="store_true", help="Push even if below threshold")
    ap.add_argument(
        "--touch-mode",
        choices=("micro_ping", "full", "direct"),
        default=default_touch,
        help="Step 1 message mode: micro_ping (default, short question) or direct/full (case study in step 1)",
    )
    ap.add_argument("--campaign-name", default=default_camp_tg)
    ap.add_argument("--campaign-name-wa", default=default_camp_wa)
    ap.add_argument("--campaign-id", type=int, default=None)
    ap.add_argument("--no-campaign", action="store_true", help="Only create/update CRM leads without campaign enrollment")
    ap.add_argument("--mcp-url", default=os.environ.get("CRMTG_MCP_URL", ""), help="Remote HTTP MCP base URL")
    ap.add_argument("--use-llm", action="store_true", help="Enable LLM follow-up generation when reading sheet rows")
    args = ap.parse_args()

    if not ui_cfg.get("enabled", True) or not b2b_cfg.get("enabled", True):
        log("[crm_mcp_push] B2B leadgen disabled in UI settings; skipping")
        return 0

    enroll = bool(ui_cfg.get("auto_push_to_crm", True)) and not args.no_campaign

    def _push_tg_wa_split(batch_items: list[dict[str, Any]], *, camp_id: int | None) -> dict[str, Any]:
        wa_items = [l for l in batch_items if l.get("provider") == "whatsapp" and l.get("phone") and not l.get("username")]
        tg_items = [l for l in batch_items if not (l.get("provider") == "whatsapp" and l.get("phone") and not l.get("username"))]
        agg = {"processed": 0, "created_leads": 0, "updated_leads": 0, "enrolled_in_campaign": 0, "campaign_id": None}
        if tg_items:
            r_tg = push_leads_via_mcp(
                tg_items,
                enroll_in_campaign=enroll,
                campaign_id=camp_id,
                campaign_name=args.campaign_name,
                mcp_url=args.mcp_url or None,
            )
            for k in ("processed", "created_leads", "updated_leads", "enrolled_in_campaign"):
                agg[k] += int(r_tg.get(k, 0))
            agg["campaign_id"] = r_tg.get("campaign_id")
        if wa_items:
            r_wa = push_leads_via_mcp(
                wa_items,
                enroll_in_campaign=enroll,
                campaign_id=None,
                campaign_name=args.campaign_name_wa,
                mcp_url=args.mcp_url or None,
            )
            for k in ("processed", "created_leads", "updated_leads", "enrolled_in_campaign"):
                agg[k] += int(r_wa.get(k, 0))
            if agg["campaign_id"] is None:
                agg["campaign_id"] = r_wa.get("campaign_id")
        return agg

    if args.jsonl:
        leads = load_leads_from_jsonl(args.jsonl, touch_mode=args.touch_mode, use_llm=args.use_llm)
        if effective_quota > 0:
            leads = leads[:effective_quota]
        if not leads:
            log(f"[jsonl] no sendable leads found in {args.jsonl}")
            return 0
        res = _push_tg_wa_split(leads, camp_id=args.campaign_id)
        log(
            f"[jsonl] CRM MCP push complete ({args.touch_mode}): "
            f"processed={res.get('processed', 0)}, "
            f"created={res.get('created_leads', 0)}, "
            f"updated={res.get('updated_leads', 0)}, "
            f"enrolled={res.get('enrolled_in_campaign', 0)}, "
            f"campaign_id={res.get('campaign_id')}"
        )
        return 0

    with acquire_push_state_lock(STATE_PATH):
        state = read_state(STATE_PATH)
        targets = [t for t in TABS if not args.tab or t[0] == args.tab]
        total_created = 0
        total_updated = 0
        total_enrolled = 0
        remaining_quota = effective_quota if effective_quota > 0 else 999999

        for tab, state_key, _slug in targets:
            if remaining_quota <= 0:
                break
            try:
                rows = sheet_rows(tab)
            except Exception as exc:
                log(f"[{tab}] failed to read Google Sheet: {exc}")
                continue
            total = len(rows)
            start_idx = int(state.get(state_key, 1))
            new_count = total - start_idx
            log(f"[{tab}] sheet rows: {total} (incl header), watermark: {start_idx}, new: {new_count}")
            if new_count <= 0 or total <= 1:
                continue

            leads, consumed_offsets = _pack_tab_leads_with_offsets(
                tab,
                rows[start_idx:],
                use_llm=args.use_llm,
                touch_mode=args.touch_mode,
            )
            sendable = len(leads)
            if sendable <= 0:
                log(f"[{tab}] no foreign sendable leads in new range; advancing watermark to {total}")
                state[state_key] = total
                write_state(state, STATE_PATH)
                continue
            if sendable < THRESHOLD and not args.force:
                log(f"[{tab}] {sendable} < {THRESHOLD} new leads, skipping (use --force to push anyway)")
                continue

            batch = leads[:remaining_quota]
            if tab == "X":
                res = push_leads_via_mcp(
                    batch,
                    enroll_in_campaign=False,
                    campaign_id=None,
                    campaign_name=args.campaign_name,
                    mcp_url=args.mcp_url or None,
                )
            elif tab == "Direct":
                wa_batch = [l for l in batch if l.get("provider") == "whatsapp" and l.get("phone")]
                other_batch = [l for l in batch if not (l.get("provider") == "whatsapp" and l.get("phone"))]
                res = {"created_leads": 0, "updated_leads": 0, "enrolled_in_campaign": 0}
                if wa_batch:
                    r_wa = push_leads_via_mcp(
                        wa_batch,
                        enroll_in_campaign=enroll,
                        campaign_id=args.campaign_id,
                        campaign_name=args.campaign_name_wa,
                        mcp_url=args.mcp_url or None,
                    )
                    for k in ("created_leads", "updated_leads", "enrolled_in_campaign"):
                        res[k] += int(r_wa.get(k, 0))
                if other_batch:
                    r_oth = push_leads_via_mcp(
                        other_batch,
                        enroll_in_campaign=False,
                        campaign_id=None,
                        campaign_name=args.campaign_name,
                        mcp_url=args.mcp_url or None,
                    )
                    for k in ("created_leads", "updated_leads"):
                        res[k] += int(r_oth.get(k, 0))
            else:
                res = _push_tg_wa_split(batch, camp_id=args.campaign_id)
            if len(batch) < len(leads):
                # Quota capped this batch -> advance ONLY to exact last processed row (start_idx + offset)!
                new_watermark = start_idx + (consumed_offsets[len(batch) - 1] if consumed_offsets else len(batch))
            else:
                new_watermark = total
            state[state_key] = new_watermark
            write_state(state, STATE_PATH)
            remaining_quota = max(0, remaining_quota - len(batch))
            total_created += int(res.get("created_leads", 0))
            total_updated += int(res.get("updated_leads", 0))
            total_enrolled += int(res.get("enrolled_in_campaign", 0))
            log(
                f"[{tab}] CRM MCP push complete ({args.touch_mode}): "
                f"rows {start_idx + 1}..{new_watermark} -> "
                f"created={res.get('created_leads', 0)}, "
                f"updated={res.get('updated_leads', 0)}, "
                f"enrolled={res.get('enrolled_in_campaign', 0)}, "
                f"watermark={new_watermark}"
            )

    log(
        f"CRM MCP summary: created_leads={total_created}, "
        f"updated_leads={total_updated}, enrolled_in_campaign={total_enrolled}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
