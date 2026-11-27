from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Mapping
import concurrent.futures
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import urllib.parse

logger = logging.getLogger(__name__)


class _SigtermInterrupt(BaseException):
    """Raised on SIGTERM so inner except-Exception handlers cannot swallow shutdown."""

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

import app.digest as app_digest
from app.db import ROOT, as_dict, connect, init_db, resolve_session, utc_now
from app.leadgen_settings import (
    _clean_chat_list,
    _resolve_state_dir,
    ensure_whales_tg_campaign_ready,
    find_existing_softugc_tg_campaign,
    get_leadgen_config,
    push_discovered_now,
    resolve_effective_market_config,
    resolve_target_b2b_tg_campaign_name,
)
from app.leads import create_lead as crm_create_lead
from app.outreach import create_campaign, preview_outreach_batch
from leadgen.budget_guard import (
    EnrichmentBudgetState,
    check_balances_and_alert,
    check_and_reserve_budget,
    record_enrichment_spend,
)
from leadgen.contact_waterfall import enrich_lead_contacts
from leadgen.freelance_monitors import monitor_all_sources
from leadgen.hh_vacancies import collect_hh_vacancies
from leadgen.language_router import (
    CAMPAIGN_OFM_TG,
    CAMPAIGN_RU_EXPERTS_TG,
    CAMPAIGN_VACANCY_EN_TG,
    CAMPAIGN_VACANCY_RU_TG,
    CAMPAIGN_VACANCY_RU_WA,
    CAMPAIGN_WHALES_EN_TG,
    CAMPAIGN_WHALES_EN_WA,
    QUEUE_ASSISTED_JOB,
    QUEUE_MANUAL_SOCIAL,
    detect_lead_language,
    detect_lead_segment,
    resolve_target_campaign,
    route_lead,
)
from leadgen.linkedin_jobs import collect as collect_linkedin_jobs
from leadgen.outreach_copy import build_outreach_sequence, build_reverse_bridge_draft, resolve_copy_profile
from leadgen.phone_messenger_resolver import (
    ResolvedMessenger,
    _build_bridge_dm,
    _normalize_social_url,
    _parse_icp_score,
    build_fallback_contact_draft,
    merge_resolved_messengers,
    normalize_e164,
    resolve_phones_telegram,
    resolve_phones_whatsapp,
    route_resolved_lead,
)
from leadgen.ru_experts import discover_ru_experts
from leadgen.supply_side import sanitize_person_and_company, sniper_reject_reason, whale_icp_reject_reason
from leadgen.tg_group_admins import extract_brand_tg_admins, extract_tg_group_handles_from_row
from leadgen.tg_identity_bridge import bridge_lead_to_telegram

_ORIG_BRIDGE_LEAD_TO_TELEGRAM = bridge_lead_to_telegram
from leadgen.tg_to_gsheet import (
    admit_discovered_row,
    apply_master_merge,
    email_of_text,
    linkedin_of,
    whatsapp_url,
    x_profile_url,
)
from leadgen.monid_client import check_monid_ready as _client_check_monid_ready, monid_bin
from leadgen.treg_client import check_treg_ready as _client_check_treg_ready, treg_bin

try:
    from leadgen import tg_web_client
except ImportError:
    tg_web_client = None  # type: ignore

try:
    from leadgen.tg_llm_qualify import MIN_ICP_SCORE
except ImportError:
    MIN_ICP_SCORE = 70

DEFAULT_DB_PATH = "/opt/crmtg/data/crm.db"
DEFAULT_SCRAPER_SESSIONS_DIR = "/opt/crmtg/data/scraper_sessions"
COOLDOWN_ELIGIBLE_SOURCES = frozenset({"buyer_public", "facebook_groups"})
COOLDOWN_ZERO_STREAK = 3
COOLDOWN_TTL_SEC = 48 * 3600
_GENERIC_PLATFORM_DOMAINS = frozenset({
    "t.me",
    "telegram.me",
    "telegram.dog",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "github.com",
    "whop.com",
    "producthunt.com",
    "ycombinator.com",
    "apps.apple.com",
    "play.google.com",
    "linktr.ee",
    "beacons.ai",
    "stan.store",
})
_NOISE_TG_HANDLES = frozenset({"joinchat", "share", "addstickers", "proxy", "iv", "s", "c", "boost", "socks"})
_NON_PERSONAL_TG_SUFFIXES = (
    "_official",
    "official",
    "_channel",
    "channel",
    "_news",
    "_announcements",
    "_announcement",
    "_community",
    "_group",
    "_chat",
    "_support",
    "_helpdesk",
    "_updates",
    "_alerts",
    "bot",
    "_bot",
)


def check_treg_ready() -> bool:
    return _client_check_treg_ready(binary_fn=treg_bin)


def check_monid_ready() -> bool:
    return _client_check_monid_ready(binary_fn=monid_bin)


def _count_jsonl_lines(p: Path) -> int:
    if not p.is_file() or p.stat().st_size == 0:
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    out: list[dict[str, Any]] = []
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    return out


def _lead_retry_keys(row: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for id_field in ("id", "telegram_user_id"):
        rid = row.get(id_field)
        if rid not in (None, "", 0):
            keys.add(f"{id_field}:{rid}")
    uname = (
        str(
            row.get("username")
            or row.get("telegram_username")
            or row.get("tg_username")
            or ""
        )
        .strip()
        .lstrip("@")
        .lower()
    )
    if uname:
        keys.add(f"u:{uname}")
    purl = (
        str(
            row.get("project_url")
            or row.get("website")
            or row.get("site")
            or row.get("url")
            or ""
        )
        .strip()
        .rstrip("/")
        .lower()
    )
    if purl:
        keys.add(f"p:{purl}")
    fx = x_profile_url(row)
    if fx:
        keys.add(f"x:{fx.lower()}")
    for extra in ("email", "phone"):
        val = str(row.get(extra) or "").strip().lower()
        if val:
            keys.add(f"{extra}:{val}")
    return keys


def _write_pending_qualify_queue(queue_path: Path, rows: list[dict[str, Any]]) -> None:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rkeys = _lead_retry_keys(row)
        if rkeys and (rkeys & seen_keys):
            continue
        seen_keys.update(rkeys)
        deduped.append(row)
    if not deduped:
        queue_path.unlink(missing_ok=True)
        return
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = queue_path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in deduped) + "\n",
        encoding="utf-8",
    )
    tmp.replace(queue_path)


def _is_tg_phone_direct_row(row: Mapping[str, Any]) -> bool:
    if bool(row.get("tg_phone_direct")):
        return True
    if str(row.get("provider") or "").strip().lower() != "telegram":
        return False
    phone = str(row.get("phone") or "").strip()
    raw_uid = row.get("telegram_user_id")
    try:
        uid = int(raw_uid) if raw_uid is not None and str(raw_uid).strip() else 0
    except (TypeError, ValueError):
        uid = 0
    return bool(phone and uid > 0)


def _count_personal_tg_rows(jsonl_path: Path) -> int:
    if not jsonl_path.is_file():
        return 0
    cnt = 0
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        cand = obj.get("username") or obj.get("telegram_username") or obj.get("tg_username")
        if _is_real_personal_tg_handle(cand) or _is_tg_phone_direct_row(obj):
            cnt += 1
    return cnt


def _is_real_personal_tg_handle(raw_username: Any) -> bool:
    u = str(raw_username or "").strip().lstrip("@")
    if not u or len(u) < 4 or len(u) > 32:
        return False
    low = u.lower()
    if low in _NOISE_TG_HANDLES or any(low.endswith(sfx) for sfx in _NON_PERSONAL_TG_SUFFIXES):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", u))


def _strip_tg_handle_from_row(row: dict[str, Any], handle: str, *, move_to_group: bool) -> None:
    row["username"] = ""
    if "telegram_username" in row:
        row["telegram_username"] = ""
    if "tg_username" in row:
        row["tg_username"] = ""
    if "telegram_url" in row:
        row["telegram_url"] = ""
    if str(row.get("provider") or "").strip().lower() == "telegram":
        row["provider"] = ""
    if move_to_group and handle:
        grp_url = f"https://t.me/{handle}"
        if not str(row.get("telegram_group") or "").strip():
            row["telegram_group"] = grp_url
        links = list(row.get("links") or []) if isinstance(row.get("links"), list) else []
        if grp_url not in links:
            links.append(grp_url)
        row["links"] = links


def _direct_x_dedup_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    em = email_of_text(row.get("work_email") or row.get("email") or "")
    if em:
        keys.append(f"email:{em.casefold()}")
    fx = x_profile_url(row)
    if fx:
        keys.append(f"x:{fx.casefold()}")
    fli = linkedin_of(row)
    if fli:
        keys.append(f"li:{fli.casefold()}")
    cform = str(row.get("contact_form_url") or "").strip()
    if cform:
        keys.append(f"cform:{cform.casefold()}")
    purl = str(row.get("project_url") or row.get("website") or row.get("site") or "").strip()
    if purl:
        host = (urllib.parse.urlparse(purl if "://" in purl else f"https://{purl}").hostname or "").lower().removeprefix("www.")
        if host and "." in host:
            keys.append(f"dom:{host}")
    return keys


def split_tg_and_direct_x_leads(
    final_jsonl: Path,
    direct_x_jsonl: Path,
    manual_social_jsonl: Path | None = None,
    *,
    reverse_bridge_tg_handle: str = "",
) -> dict[str, int]:
    """Keep TG/WA leads (icp_score >= 80) in final_jsonl; preserve manual social & Direct/X queues."""
    if not final_jsonl.is_file() or final_jsonl.stat().st_size == 0:
        if final_jsonl.is_file():
            final_jsonl.unlink(missing_ok=True)
        empty_res = {"tg_wa_kept": 0, "direct_x_added": 0, "dropped": 0}
        if manual_social_jsonl is not None:
            empty_res["social_manual_added"] = 0
        return empty_res

    existing_dx_keys: set[str] = set()
    if direct_x_jsonl.is_file():
        for line in direct_x_jsonl.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                prev_obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(prev_obj, dict):
                existing_dx_keys.update(_direct_x_dedup_keys(prev_obj))

    tg_wa_rows: list[dict[str, Any]] = []
    new_direct_x_rows: list[dict[str, Any]] = []
    manual_social_rows: list[dict[str, Any]] = []
    dropped = 0

    for raw_line in final_jsonl.read_text(encoding="utf-8").splitlines():
        s = raw_line.strip()
        if not s:
            continue
        try:
            item = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        row = dict(item)
        raw_score = row.get("icp_score")
        try:
            score = 85 if raw_score is None else int(round(float(raw_score)))
        except (TypeError, ValueError):
            score = 0
        if score < MIN_ICP_SCORE or str(row.get("monetization") or "").strip().lower() == "none":
            dropped += 1
            continue
        if sniper_reject_reason(row):
            dropped += 1
            continue

        site_val = str(row.get("project_url") or row.get("website") or row.get("site") or "").strip()
        clean_n, clean_p = sanitize_person_and_company(
            row.get("name"),
            row.get("project") or row.get("company"),
            site=site_val,
            bio=row.get("bio") or row.get("what_they_do") or row.get("their_angle"),
        )
        if row.get("name") is not None and clean_n:
            row["name"] = clean_n
        if clean_p:
            row["project"] = clean_p
            if "company" in row:
                row["company"] = clean_p
        row["icp_score"] = score

        raw_tg = row.get("username") or row.get("telegram_username") or row.get("tg_username") or ""
        has_real_tg = _is_real_personal_tg_handle(raw_tg)
        is_tg_direct = _is_tg_phone_direct_row(row)
        is_wa_pending = bool(row.get("wa_check_pending"))
        wa = (
            ""
            if is_wa_pending
            else (
                whatsapp_url(row.get("whatsapp") or "")
                or (
                    whatsapp_url(row.get("phone") or "")
                    if str(row.get("provider") or "").lower() == "whatsapp"
                    else ""
                )
            )
        )
        if has_real_tg or is_tg_direct or wa:
            if has_real_tg:
                row["username"] = str(raw_tg).strip().lstrip("@")
                row["provider"] = "telegram"
            elif is_tg_direct:
                row["provider"] = "telegram"
                row["tg_phone_direct"] = True
            elif wa:
                wa_digits = re.sub(r"\D", "", wa)
                row["whatsapp"] = f"https://wa.me/{wa_digits}"
                row["phone"] = f"+{wa_digits}"
                row["provider"] = "whatsapp"
            tg_wa_rows.append(row)
            continue

        # Preserve Manual Social Queue rows routed by phone_messenger_resolver (X/LinkedIn/IG/ContactForm/Email ICP >= 85)
        if (
            manual_social_jsonl is not None
            and str(row.get("segment") or "").strip().lower() == "social_manual"
            and str(row.get("manual_action_url") or "").strip()
        ):
            manual_social_rows.append(row)
            continue

        # Preserve verified HLR/pending WA phone numbers into Manual Social Queue as click-to-WA + phone outreach
        pending_e164 = normalize_e164(str(row.get("phone") or ""))
        if manual_social_jsonl is not None and is_wa_pending and pending_e164:
            lang = str(row.get("language") or "").strip().lower() or detect_lead_language(row)
            row["phone"] = pending_e164
            row.pop("whatsapp", None)
            if str(row.get("provider") or "").strip().lower() == "whatsapp":
                row.pop("provider", None)
            row["segment"] = "social_manual"
            row["manual_action_url"] = (
                str(row.get("wa_click_url") or "").strip()
                or f"https://wa.me/{pending_e164.lstrip('+')}"
            )
            row["manual_draft_text"] = (
                str(row.get("wa_manual_draft_text") or "").strip()
                or build_fallback_contact_draft(row, lang, "whatsapp_hlr")
            )
            row["manual_channel"] = "whatsapp_hlr"
            manual_social_rows.append(row)
            continue

        # No personal TG and no WhatsApp -> check Direct/X channels
        row["username"] = ""
        row.pop("telegram_username", None)
        row.pop("tg_username", None)
        em = email_of_text(row.get("work_email") or row.get("email") or "")
        fx = x_profile_url(row)
        fli = linkedin_of(row)
        cform = str(row.get("contact_form_url") or "").strip()
        if not cform.lower().startswith(("http://", "https://")):
            cform = ""
        if em or fx or fli or cform:
            comp_val = str(row.get("company") or row.get("project") or "").strip()
            angle_val = str(row.get("their_angle") or row.get("what_they_do") or "").strip()
            if comp_val:
                row["company"] = comp_val
                row["project"] = comp_val
            if site_val:
                row["url"] = site_val
                row["project_url"] = site_val
            if angle_val:
                row["their_angle"] = angle_val
                row["what_they_do"] = angle_val
            if em:
                row["work_email"] = em
                row["email"] = em
            if fx:
                row["founder_x"] = fx
                row["x_url"] = fx
            if fli:
                row["founder_linkedin"] = fli
                row["linkedin_url"] = fli
            if cform:
                row["contact_form_url"] = cform
            has_wa_or_phone = bool(
                whatsapp_url(row.get("whatsapp") or "")
                or normalize_e164(str(row.get("phone") or row.get("mobile_phone") or ""))
            )
            if em or has_wa_or_phone:
                rb_draft = build_reverse_bridge_draft(row, tg_handle=reverse_bridge_tg_handle)
                if rb_draft.get("whatsapp_draft"):
                    row.setdefault("reverse_bridge_wa_draft", rb_draft["whatsapp_draft"])
                if rb_draft.get("email_subject"):
                    row.setdefault("reverse_bridge_email_subject", rb_draft["email_subject"])
                if rb_draft.get("email_body"):
                    row.setdefault("reverse_bridge_email_body", rb_draft["email_body"])
                if rb_draft.get("tg_link"):
                    row.setdefault("reverse_bridge_tg_link", rb_draft["tg_link"])
            row["route_channel"] = "x" if (fx and not em and not fli and not cform) else "direct"
            d_keys = _direct_x_dedup_keys(row)
            if d_keys and any(k in existing_dx_keys for k in d_keys):
                continue
            existing_dx_keys.update(d_keys)
            new_direct_x_rows.append(row)
        else:
            dropped += 1

    if new_direct_x_rows:
        direct_x_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with direct_x_jsonl.open("a", encoding="utf-8") as dxfh:
            for dx_row in new_direct_x_rows:
                dxfh.write(json.dumps(dx_row, ensure_ascii=False) + "\n")

    if manual_social_rows and manual_social_jsonl is not None:
        manual_social_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with manual_social_jsonl.open("a", encoding="utf-8") as msfh:
            for ms_row in manual_social_rows:
                msfh.write(json.dumps(ms_row, ensure_ascii=False) + "\n")

    if tg_wa_rows:
        final_jsonl.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in tg_wa_rows) + "\n",
            encoding="utf-8",
        )
    else:
        final_jsonl.unlink(missing_ok=True)

    split_out = {
        "tg_wa_kept": len(tg_wa_rows),
        "direct_x_added": len(new_direct_x_rows),
        "dropped": dropped,
    }
    if manual_social_jsonl is not None:
        split_out["social_manual_added"] = len(manual_social_rows)
    return split_out


def _norm_quarantine_token(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        return ""
    for prefix in ("tg_groups:", "vacancies:", "chat:", "tg:", "group:", "channel:"):
        if val.lower().startswith(prefix):
            val = val[len(prefix) :].strip()
    cleaned = _clean_chat_list([val])
    if cleaned:
        return cleaned[0].lower()
    return val.lstrip("@").strip("/").lower()


def load_quarantined_sources(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None:
        return set()
    try:
        row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'quarantined_leadgen_sources'"
        ).fetchone()
    except sqlite3.Error:
        return set()
    if not row or not row["value"]:
        return set()

    raw_val = str(row["value"]).strip()
    if not raw_val:
        return set()

    extracted: list[Any] = []
    try:
        parsed = json.loads(raw_val)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    for k in ("source", "chat", "group", "name", "username", "id"):
                        if item.get(k):
                            extracted.append(item[k])
                elif item is not None:
                    extracted.append(item)
        elif isinstance(parsed, dict):
            if isinstance(parsed.get("sources"), list):
                extracted.extend(parsed["sources"])
            elif isinstance(parsed.get("items"), list):
                extracted.extend(parsed["items"])
            else:
                for k, v in parsed.items():
                    if v:
                        extracted.append(k)
        elif isinstance(parsed, str):
            extracted.extend(re.split(r"[\n,;]+", parsed))
    except json.JSONDecodeError:
        extracted.extend(re.split(r"[\n,;]+", raw_val))

    out: set[str] = set()
    for item in extracted:
        tok = _norm_quarantine_token(item)
        if tok:
            out.add(tok)
    return out


def apply_quarantined_sources(
    cfg: dict[str, Any], quarantined: set[str]
) -> dict[str, Any]:
    effective = copy.deepcopy(cfg)
    if not quarantined:
        return effective

    b2b = effective.setdefault("b2b", {})
    for list_key in ("tg_groups_old", "tg_groups_new", "tg_comment_channels"):
        raw_list = b2b.get(list_key) or []
        b2b[list_key] = [
            g for g in raw_list if _norm_quarantine_token(g) not in quarantined
        ]

    vac = effective.setdefault("vacancies", {})
    raw_extra = vac.get("extra_chats") or []
    vac["extra_chats"] = [
        g for g in raw_extra if _norm_quarantine_token(g) not in quarantined
    ]
    return effective


def _session_path_variants(raw_path: str | Path) -> tuple[set[str], set[str]]:
    raw_str = str(raw_path or "").strip()
    if not raw_str:
        return set(), set()

    expanded = Path(raw_str).expanduser()
    resolved_base = resolve_session(str(expanded))
    with_ext = (
        resolved_base
        if resolved_base.suffix.lower() == ".session"
        else Path(f"{resolved_base}.session")
    )
    without_ext = (
        resolved_base.with_suffix("")
        if resolved_base.suffix.lower() == ".session"
        else resolved_base
    )

    path_tokens: set[str] = set()
    name_tokens: set[str] = set()
    for p in (expanded, resolved_base, with_ext, without_ext):
        abs_s = os.path.abspath(str(p))
        real_s = os.path.realpath(str(p))
        path_tokens.add(abs_s)
        path_tokens.add(real_s)
        if p.name:
            name_tokens.add(p.name.lower())
        stem = p.stem if p.suffix.lower() == ".session" else p.name
        if stem:
            name_tokens.add(stem.lower())
            name_tokens.add(f"{stem.lower()}.session")
    return path_tokens, name_tokens


def _extract_sqlite_auth_key_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size < 100:
            return None
        with path.open("rb") as fh:
            magic = fh.read(16)
        if not magic.startswith(b"SQLite format 3\x00"):
            return None
        uri = f"file:{urllib.parse.quote(str(path.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as sconn:
            row = sconn.execute(
                "SELECT auth_key FROM sessions WHERE auth_key IS NOT NULL LIMIT 1"
            ).fetchone()
        if row and row[0]:
            key_bytes = bytes(row[0])
            if len(key_bytes) == 256:
                return hashlib.sha256(key_bytes).hexdigest()
    except Exception:
        return None
    return None


def _extract_blob_auth_key_sha256(blob: Any) -> str | None:
    if not blob:
        return None
    try:
        raw = bytes(blob)
    except Exception:
        return None
    if len(raw) < 100 or not raw.startswith(b"SQLite format 3\x00"):
        return None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as tf:
            tf.write(raw)
            tmp_path = Path(tf.name)
        return _extract_sqlite_auth_key_sha256(tmp_path)
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _extract_string_session_auth_key_sha256(session_string: Any) -> str | None:
    s = str(session_string or "").strip()
    if len(s) < 300:
        return None
    try:
        from telethon.sessions import StringSession

        sess = StringSession(s)
        ak = getattr(sess, "auth_key", None)
        key = getattr(ak, "key", None) if ak is not None else None
        if key and len(bytes(key)) == 256:
            return hashlib.sha256(bytes(key)).hexdigest()
    except Exception:
        pass
    # Telethon StringSession stores a 1-char version header ('1') followed by base64
    # ending with the 256-byte MTProto auth_key (>B4sH256s or >B16sH256s).
    for payload in (s[1:], s):
        try:
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(padded)
            if len(decoded) >= 256:
                key_bytes = decoded[-256:]
                if len(key_bytes) == 256:
                    return hashlib.sha256(key_bytes).hexdigest()
        except Exception:
            continue
    return None


def _collect_outreach_session_dirs(conn: sqlite3.Connection) -> list[Path]:
    dirs: list[Path] = [ROOT / "data" / "sessions"]
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            db_file = str(row[2] or "").strip()
            if db_file:
                db_parent = Path(db_file).resolve().parent
                dirs.append(db_parent / "sessions")
                dirs.append(db_parent / "data" / "sessions")
    except Exception:
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        key = os.path.realpath(str(d))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def validate_isolated_scraper_session(
    conn: sqlite3.Connection,
    session_path: str | Path | None = None,
    *,
    scraper_sessions_dir: str | Path | None = None,
) -> Path | None:
    candidate_raw = str(session_path or "").strip()
    if not candidate_raw:
        candidate_raw = os.environ.get("SCRAPER_TG_SESSION", "").strip()

    if not candidate_raw:
        sess_dir = Path(
            str(
                scraper_sessions_dir
                or os.environ.get("SCRAPER_SESSIONS_DIR")
                or DEFAULT_SCRAPER_SESSIONS_DIR
            ).strip()
        ).expanduser()
        if sess_dir.is_dir():
            found = sorted(sess_dir.glob("*.session"))
            non_checker = [p for p in found if not p.name.startswith("phone_checker_")]
            signals_first = [p for p in non_checker if p.name.startswith("signals_ro")]
            chosen_list = signals_first or non_checker
            if chosen_list:
                candidate_raw = str(chosen_list[0])

    if not candidate_raw:
        return None

    cand_paths, cand_names = _session_path_variants(candidate_raw)
    acc_rows = conn.execute(
        "SELECT id, phone, session_path, telegram_username, session_blob, session_string FROM accounts"
    ).fetchall()
    for row in acc_rows:
        acc_sess = str(row["session_path"] or "").strip()
        if not acc_sess:
            continue
        acc_paths, acc_names = _session_path_variants(acc_sess)
        if (cand_paths & acc_paths) or (cand_names & acc_names):
            raise ValueError(
                f"SCRAPER_TG_SESSION ({candidate_raw}) совпадает с боевой сессией рассылки "
                f"(account #{row['id']}: {acc_sess}). Использование боевого аккаунта запрещено!"
            )

    cand_resolved = resolve_session(str(Path(candidate_raw).expanduser()))
    if cand_resolved.suffix.lower() != ".session":
        with_suffix = Path(f"{cand_resolved}.session")
        if with_suffix.exists():
            cand_resolved = with_suffix

    # Fingerprint guard: inspect 256-byte MTProto auth_key in read-only mode and compare SHA-256
    # against (a) accounts.session_blob, (b) accounts.session_string, and (c) data/sessions/*.session on disk.
    cand_auth_sha = _extract_sqlite_auth_key_sha256(cand_resolved)
    if cand_auth_sha is not None:
        live_auth_shas: set[str] = set()
        for row in acc_rows:
            blob_sha = _extract_blob_auth_key_sha256(row["session_blob"])
            if blob_sha:
                live_auth_shas.add(blob_sha)
            str_sha = _extract_string_session_auth_key_sha256(row["session_string"])
            if str_sha:
                live_auth_shas.add(str_sha)
            acc_sess = str(row["session_path"] or "").strip()
            if acc_sess:
                acc_p = resolve_session(str(Path(acc_sess).expanduser()))
                if acc_p.suffix.lower() != ".session":
                    acc_p_ext = Path(f"{acc_p}.session")
                    if acc_p_ext.is_file():
                        acc_p = acc_p_ext
                file_sha = _extract_sqlite_auth_key_sha256(acc_p)
                if file_sha:
                    live_auth_shas.add(file_sha)

        for sess_dir in _collect_outreach_session_dirs(conn):
            if not sess_dir.is_dir():
                continue
            for disk_sess in sorted(sess_dir.glob("*.session")):
                disk_sha = _extract_sqlite_auth_key_sha256(disk_sess)
                if disk_sha:
                    live_auth_shas.add(disk_sha)

        if cand_auth_sha in live_auth_shas:
            raise ValueError("Scraper session shares MTProto auth_key with outreach account")

    return cand_resolved


def _find_campaign_by_name(
    conn: sqlite3.Connection, name: str
) -> dict[str, Any] | None:
    clean = str(name or "").strip().lower()
    if not clean:
        return None
    rows = conn.execute("SELECT * FROM campaigns ORDER BY id ASC").fetchall()
    for r in rows:
        d = as_dict(r)
        if str(d.get("name") or "").strip().lower() == clean:
            return d
    return None


def _count_pending(conn: sqlite3.Connection, campaign_id: int | None) -> int:
    if campaign_id is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) FROM campaign_recipients
        WHERE campaign_id = ?
          AND COALESCE(current_step, 0) = 0
          AND status = 'pending'
        """,
        (int(campaign_id),),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _parse_iso_utc(raw: str | None) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        norm = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except ValueError:
        return None


def _emit_critical_low_water_alert(
    conn: sqlite3.Connection,
    *,
    campaign: dict[str, Any],
    pending_count: int,
    critical_low_mark: int,
    has_scraper_session: bool,
    has_llm_key: bool,
    missing_treg_cli: bool = False,
    digest_opener: Any = None,
) -> dict[str, Any]:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S")

    reasons: list[str] = []
    reason_labels: list[str] = []
    if not has_scraper_session:
        reasons.append("missing_scraper_tg_session")
        reason_labels.append("нет SCRAPER_TG_SESSION")
    if not has_llm_key:
        reasons.append("missing_relayfast_api_key")
        reason_labels.append("нет RELAYFAST_API_KEY")
    if missing_treg_cli:
        reasons.append("missing_treg_cli")
        reason_labels.append("нет treg CLI / TREG_API_KEY")
    if not reasons:
        reasons.append("sources_exhausted")
        reason_labels.append("источники исчерпаны")

    reason_text = ", ".join(reason_labels)
    prev_sent_at = ""
    try:
        prev_row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'leadgen_low_water_alert'"
        ).fetchone()
        if prev_row and prev_row["value"]:
            prev_obj = json.loads(prev_row["value"])
            if isinstance(prev_obj, dict):
                prev_sent_at = str(prev_obj.get("last_alert_sent_at") or "").strip()
    except Exception:
        prev_sent_at = ""

    prev_dt = _parse_iso_utc(prev_sent_at)
    within_24h = (
        prev_dt is not None and (now_dt - prev_dt).total_seconds() < 86400
    )

    alert_sent = False
    alert_error: str | None = None
    last_alert_sent_at = prev_sent_at

    if not within_24h:
        alert_body = (
            f"⚠️ Leadgen Low-Water Alert: кампания «{campaign['name']}» (#{campaign['id']})\n"
            f"Остаток pending (Step 0): {pending_count} (порог critical_low_mark: {critical_low_mark}).\n"
            f"Причина: {reason_text}."
        )
        try:
            token, chat_id = app_digest._operator(conn)
            payload = {"chat_id": str(chat_id).strip(), "text": alert_body}
            app_digest._post(
                app_digest.SEND_URL.format(token=token), payload, digest_opener
            )
            alert_sent = True
            last_alert_sent_at = now_iso
        except Exception as exc:
            alert_error = str(exc)

    diagnostic = {
        "campaign_id": int(campaign["id"]),
        "campaign_name": str(campaign["name"]),
        "pending": int(pending_count),
        "critical_low_mark": int(critical_low_mark),
        "reason": reasons[0],
        "reasons": reasons,
        "reason_text": reason_text,
        "alert_sent": alert_sent,
        "alert_skipped": "already_sent_within_24h" if within_24h else None,
        "alert_error": alert_error,
        "last_alert_sent_at": last_alert_sent_at or None,
        "updated_at": now_iso,
    }
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_low_water_alert', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(diagnostic, ensure_ascii=False), now_iso),
    )
    conn.commit()
    return diagnostic


def _merge_discovered_jsonl_into_raw(
    source_files: list[Path], raw_jsonl: Path
) -> int:
    added = 0
    raw_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not raw_jsonl.exists():
        raw_jsonl.write_text("", encoding="utf-8")
    with raw_jsonl.open("a", encoding="utf-8") as out_fh:
        for path in source_files:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    row = dict(row)
                    if not row.get("telegram_username") and (
                        row.get("tg_username") or row.get("site_tg")
                    ):
                        row["telegram_username"] = row.get("tg_username") or row.get("site_tg")
                    if not row.get("project_url") and row.get("site"):
                        row["project_url"] = row.get("site")
                    if not row.get("project") and row.get("company"):
                        row["project"] = row.get("company")
                    orig_src = str(row.get("lead_source") or row.get("source") or "").strip()
                    if not orig_src and row.get("group"):
                        orig_src = "tg_groups"
                else:
                    orig_src = ""
                admitted = admit_discovered_row(row)
                if not admitted:
                    continue
                if orig_src:
                    if not str(admitted.get("source") or "").strip():
                        admitted["source"] = orig_src
                    if not str(admitted.get("lead_source") or "").strip():
                        admitted["lead_source"] = str(admitted.get("source") or orig_src)
                admitted.setdefault("icp_match", True)
                out_fh.write(json.dumps(admitted, ensure_ascii=False) + "\n")
                added += 1
    return added


_PROTECTED_JSONL_NAMES = frozenset(
    {
        "master_ids.jsonl",
        "direct_x_leads.jsonl",
        "ofm_review_leads.jsonl",
        "quarantined_unqualified.jsonl",
        "pending_qualify_queue.jsonl",
        "pending_tg_verify_queue.jsonl",
        "leadgen_run_history.jsonl",
    }
)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def prune_leadgen_state_jsonl(
    state_dir: Path,
    *,
    max_age_days: int = 14,
    max_total_bytes: int = 20 * 1024 * 1024,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Enforce retention (<=14d ephemeral runs, <=20MB total *.jsonl) without wiping active SSOT files."""
    if not state_dir.is_dir():
        return {"deleted_files": [], "trimmed_files": [], "total_bytes_after": 0}
    now_sec = float(time.time() if now_ts is None else now_ts)
    cutoff_sec = now_sec - max(1, int(max_age_days)) * 86400.0
    deleted: list[str] = []
    trimmed: list[str] = []

    def _all_jsonl_stats() -> list[tuple[float, int, Path]]:
        items: list[tuple[float, int, Path]] = []
        for p in state_dir.glob("*.jsonl*"):
            if not p.is_file() or ".jsonl" not in p.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((float(st.st_mtime), int(st.st_size), p))
        return items

    # 1) Remove ephemeral staging/run .jsonl files older than max_age_days
    for mtime, _size, p in _all_jsonl_stats():
        if p.name in _PROTECTED_JSONL_NAMES:
            continue
        if mtime < cutoff_sec:
            try:
                p.unlink(missing_ok=True)
                deleted.append(p.name)
            except OSError:
                pass

    # 2) Enforce total <= max_total_bytes by evicting oldest ephemeral artifacts first
    stats = _all_jsonl_stats()
    total_bytes = sum(sz for _, sz, _ in stats)
    if total_bytes > max_total_bytes:
        ephemeral = sorted(
            [(mt, sz, p) for mt, sz, p in stats if p.name not in _PROTECTED_JSONL_NAMES],
            key=lambda x: x[0],
        )
        for _mt, sz, p in ephemeral:
            if total_bytes <= max_total_bytes:
                break
            try:
                p.unlink(missing_ok=True)
                deleted.append(p.name)
                total_bytes = max(0, total_bytes - sz)
            except OSError:
                pass

    # 3) If protected quarantine/pending logs still push over budget, trim oldest lines only
    if total_bytes > max_total_bytes:
        for trim_name in ("quarantined_unqualified.jsonl", "pending_qualify_queue.jsonl", "leadgen_run_history.jsonl"):
            if total_bytes <= max_total_bytes:
                break
            tp = state_dir / trim_name
            if not tp.is_file():
                continue
            try:
                old_sz = tp.stat().st_size
                lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
                # Keep newest half (or newest lines within leftover budget)
                keep_from = max(len(lines) // 2, len(lines) - 200)
                kept = lines[keep_from:]
                new_text = "\n".join(kept) + ("\n" if kept else "")
                tmp = tp.with_suffix(".jsonl.trim_tmp")
                tmp.write_text(new_text, encoding="utf-8")
                tmp.replace(tp)
                new_sz = tp.stat().st_size
                total_bytes = max(0, total_bytes - max(0, old_sz - new_sz))
                trimmed.append(trim_name)
            except OSError:
                pass

    final_total = sum(sz for _, sz, _ in _all_jsonl_stats())
    return {"deleted_files": deleted, "trimmed_files": trimmed, "total_bytes_after": final_total}


def _update_run_history_and_detect(
    state_dir: Path,
    run_entry: dict[str, Any],
    *,
    max_history: int = 30,
) -> tuple[int, list[dict[str, Any]]]:
    hist_path = state_dir / "leadgen_run_history.jsonl"
    prev_entries: list[dict[str, Any]] = []
    if hist_path.is_file():
        for ln in hist_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    prev_entries.append(obj)
            except json.JSONDecodeError:
                continue

    entries = (prev_entries + [run_entry])[-max_history:]
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = hist_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )
        tmp.replace(hist_path)
    except OSError:
        pass

    # Count trailing streak of runs with tg_bridged_count == 0
    zero_streak = 0
    for item in reversed(entries):
        try:
            cnt = int(item.get("tg_bridged_count") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt == 0:
            zero_streak += 1
        else:
            break

    # Detect non-empty per-source JSONLs whose SHA-256 hash matches the previous run
    repeats: list[dict[str, Any]] = []
    cur_hashes = run_entry.get("source_hashes") or {}
    if isinstance(cur_hashes, dict) and len(entries) >= 2:
        for src, sha in cur_hashes.items():
            if not sha:
                continue
            streak = 1
            for prev in reversed(entries[:-1]):
                prev_hashes = prev.get("source_hashes") or {}
                if isinstance(prev_hashes, dict) and prev_hashes.get(src) == sha:
                    streak += 1
                else:
                    break
            if streak >= 2:
                repeats.append({"source": src, "sha256": sha, "repeat_runs": streak})

    return zero_streak, repeats


def _enforce_rule5_quarantine_on_new_recipients(
    conn: sqlite3.Connection,
    *,
    campaign: dict[str, Any] | None,
    pre_existing_ids: set[int],
    stage_errors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[int]]:
    if campaign is None:
        return None, []
    camp_id = int(campaign["id"])
    camp_name = str(campaign["name"])
    current_ids = {
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM campaign_recipients WHERE campaign_id = ?", (camp_id,)
        ).fetchall()
    }
    newly_inserted_ids = current_ids - pre_existing_ids
    if not newly_inserted_ids:
        return None, []

    preview = preview_outreach_batch(conn, camp_id, strict=False)
    violations = list(preview.get("violations") or [])
    bad_new_ids: set[int] = set()
    has_campaign_level_violation = False

    if not preview.get("ok", True) or int(preview.get("forbidden_pattern_count") or 0) > 0:
        for v in violations:
            rid = v.get("recipient_id")
            if rid is not None:
                rid_int = int(rid)
                if rid_int in newly_inserted_ids:
                    bad_new_ids.add(rid_int)
            elif v.get("code") not in ("no_fresh_untouched_leads", "legacy_1300_base_only"):
                has_campaign_level_violation = True
        if has_campaign_level_violation:
            bad_new_ids |= newly_inserted_ids

    # Also inspect raw persisted copy on newly inserted rows in case render-time scrubbing masked legacy CreatorOS
    legacy_brand_re = re.compile(r"\bCreator\s*OS\b|creatoros", re.IGNORECASE)
    placeholders = ",".join("?" for _ in newly_inserted_ids)
    raw_new_rows = conn.execute(
        f"SELECT id, step1_text, step2_text, step3_text, hook FROM campaign_recipients WHERE id IN ({placeholders})",
        tuple(sorted(newly_inserted_ids)),
    ).fetchall()
    for r in raw_new_rows:
        rid_int = int(r["id"])
        combined_raw = "\n".join(
            str(r[col] or "") for col in ("step1_text", "step2_text", "step3_text", "hook")
        )
        if legacy_brand_re.search(combined_raw):
            bad_new_ids.add(rid_int)
            violations.append(
                {
                    "code": "legacy_brand_creatoros",
                    "recipient_id": rid_int,
                    "message": f"В сыром тексте получателя #{rid_int} обнаружен устаревший бренд CreatorOS",
                }
            )

    if bad_new_ids:
        quarantined_ids = sorted(bad_new_ids)
        now_q = utc_now()
        conn.executemany(
            """
            UPDATE campaign_recipients
            SET status = 'completed',
                error_text = 'Карантин Rule 5 Dry-Run',
                updated_at = ?
            WHERE id = ?
            """,
            [(now_q, rid) for rid in quarantined_ids],
        )
        conn.commit()
        err_msg = "; ".join(str(v.get("message") or v.get("code") or "") for v in violations)
        logger.error(
            "Rule 5 Dry-Run quarantined %d newly inserted recipient(s) in campaign %s (#%d): %s",
            len(quarantined_ids),
            camp_name,
            camp_id,
            err_msg,
        )
        stage_errors.append(
            {
                "stage": "rule5_dry_run_quarantine",
                "campaign_id": camp_id,
                "campaign_name": camp_name,
                "quarantined_recipient_ids": quarantined_ids,
                "error": err_msg or "rule5_violation",
                "at": now_q,
            }
        )
        if _count_pending(conn, camp_id) > 0:
            preview = preview_outreach_batch(conn, camp_id, strict=False)
        return preview, quarantined_ids

    return preview, []


DEFAULT_REPLENISH_LOCK_PATH = Path("/run/crmtg-leadgen.lock")
FALLBACK_REPLENISH_LOCK_PATH = Path("/tmp/crmtg-leadgen.lock")


def _acquire_replenish_lock(lock_path: Path | None = None):
    """Acquire non-blocking single-instance flock for leadgen replenishment.

    Raises BlockingIOError if another process already holds the lock.
    """
    candidates = (
        [Path(lock_path)]
        if lock_path is not None
        else [DEFAULT_REPLENISH_LOCK_PATH, FALLBACK_REPLENISH_LOCK_PATH]
    )
    for idx, cand in enumerate(candidates):
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            fh = cand.open("a+", encoding="utf-8")
        except OSError:
            if idx + 1 < len(candidates):
                continue
            return None
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate(0)
            fh.write(f"pid={os.getpid()} started_at={utc_now()}\n")
            fh.flush()
            return fh
        except BlockingIOError:
            fh.close()
            raise
        except OSError:
            fh.close()
            if idx + 1 < len(candidates):
                continue
            return None
    return None


def _release_replenish_lock(lock_fh: Any | None) -> None:
    if lock_fh is None:
        return
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        lock_fh.close()
    except OSError:
        pass


def _run_coro_sync(coro: Any) -> Any:
    """Execute a coroutine from synchronous code safely even inside an active loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _call_with_supported_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke ``fn`` passing only keyword arguments it explicitly accepts unless ``**kwargs`` is present."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if accepts_var_kw:
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **filtered)


def _with_isolated_tg_client(
    session_path: Path | None,
    work_coro_factory: Callable[[Any], Any],
) -> Any:
    """Run ``work_coro_factory(client)`` using a headless non-interactive Telethon session."""
    if session_path is None or not session_path.is_file():
        return None
    raw_api_id = str(os.environ.get("TG_API_ID") or "").strip()
    api_hash = str(os.environ.get("TG_API_HASH") or "").strip()
    if not raw_api_id.isdigit() or not api_hash:
        return None
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        logger.debug("Telethon unavailable for isolated session %s: %s", session_path, exc)
        return None

    async def _runner() -> Any:
        client = TelegramClient(str(session_path), int(raw_api_id), api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                logger.warning("Isolated Telethon session not authorized: %s", session_path)
                return None
            return await work_coro_factory(client)
        finally:
            try:
                await client.disconnect()
            except Exception as exc:
                logger.debug("Telethon disconnect cleanup warning (%s): %s", session_path, exc)

    return _run_coro_sync(_runner())


def _make_budget_callbacks(
    conn: sqlite3.Connection,
    cfg: Mapping[str, Any] | None,
    state: EnrichmentBudgetState | None,
) -> tuple[Callable[..., bool], Callable[..., None]]:
    """Build shared budget-reservation and spend-ledger callbacks."""

    def _guard_fn(provider: str = "treg", *rest: Any, estimated_cost: float = 0.0, **kwargs: Any) -> bool:
        if state is not None and not state.paid_allowed:
            return False
        if len(rest) == 1 and isinstance(rest[0], (int, float)):
            est_cost = float(rest[0])
        elif len(rest) >= 2 and isinstance(rest[1], (int, float)):
            est_cost = float(rest[1])
        else:
            est_cost = float(estimated_cost or 0.0)
        ep_name = str(kwargs.get("endpoint") or (rest[0] if len(rest) >= 2 and isinstance(rest[0], str) else "")).strip()
        if ep_name == "contactout.people.contact.phone" and isinstance(cfg, Mapping):
            enr_sub = cfg.get("enrichment") if isinstance(cfg.get("enrichment"), Mapping) else {}
            raw_sub_cap = enr_sub.get("enrichment_sub_budget_usd")
            if raw_sub_cap is not None and conn is not None:
                try:
                    sub_cap = float(raw_sub_cap)
                    row_spent = conn.execute(
                        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM enrichment_ledger WHERE day = date('now') AND endpoint IN ('contactout.people.contact.phone','treg.people.phone.find','crawl4ai.people.phone.find','treg.people.email.find')"
                    ).fetchone()
                    spent_sub = float(row_spent[0] or 0.0) if row_spent else 0.0
                    if spent_sub + est_cost > sub_cap + 1e-9:
                        return False
                except Exception:
                    pass
        ok, _reason = check_and_reserve_budget(
            conn,
            provider=provider,
            estimated_cost=est_cost,
            cfg=cfg,
        )
        return ok

    def _cost_fn(
        *,
        provider: str,
        endpoint: str = "",
        lead_key: str = "",
        cost_usd: float = 0.0,
        hit: bool = False,
    ) -> None:
        record_enrichment_spend(
            conn,
            provider=provider,
            endpoint=endpoint,
            lead_key=lead_key,
            cost_usd=cost_usd,
            hit=hit,
        )

    return _guard_fn, _cost_fn


def run_contact_waterfall_with_budget(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    cfg: Mapping[str, Any] | None = None,
    budget_state: EnrichmentBudgetState | None = None,
    treg_balance_fn: Callable[[], Any] | None = None,
    monid_balance_fn: Callable[[], Any] | None = None,
    notify_fn: Callable[[str], Any] | None = None,
    enrich_fn: Callable[..., dict[str, Any]] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    dm_finder_fn: Callable[[str, str], dict[str, Any]] | None = None,
    dm_multi_finder_fn: Callable[..., list[dict[str, Any]]] | None = None,
    tg_bridge_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], EnrichmentBudgetState]:
    """Check balances/daily cap first, then run contact_waterfall with budget_guard_fn and ledger spend tracking."""
    state = (
        budget_state
        if budget_state is not None
        else check_balances_and_alert(
            conn,
            cfg=cfg,
            treg_balance_fn=treg_balance_fn,
            monid_balance_fn=monid_balance_fn,
            notify_fn=notify_fn,
        )
    )
    if not rows:
        return [], state

    _guard_fn, _cost_fn = _make_budget_callbacks(conn, cfg, state)

    active_enrich = enrich_lead_contacts if enrich_fn is None else enrich_fn
    eff_treg_fn = treg_fn if state.paid_allowed else (lambda *args, **kwargs: {})
    eff_monid_fn = monid_fn if state.paid_allowed else (lambda *args, **kwargs: {})

    def _budgeted_tg_bridge(row_arg: dict[str, Any]) -> dict[str, Any]:
        return _call_with_supported_kwargs(
            bridge_lead_to_telegram,
            row_arg,
            allow_paid=bool(state.paid_allowed),
            budget_guard_fn=_guard_fn,
            cost_record_fn=_cost_fn,
            treg_fn=eff_treg_fn,
            monid_fn=eff_monid_fn,
        )

    eff_tg_bridge_fn = tg_bridge_fn if tg_bridge_fn is not None else _budgeted_tg_bridge
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        enriched = _call_with_supported_kwargs(
            active_enrich,
            item,
            allow_paid_tier2=bool(state.paid_allowed),
            budget_guard_fn=_guard_fn,
            cost_record_fn=_cost_fn,
            cfg=cfg,
            http_get_fn=http_get_fn,
            treg_fn=eff_treg_fn,
            monid_fn=eff_monid_fn,
            dm_finder_fn=dm_finder_fn,
            dm_multi_finder_fn=dm_multi_finder_fn,
            tg_bridge_fn=eff_tg_bridge_fn,
        )
        out_rows.append(dict(enriched) if isinstance(enriched, Mapping) else item)
    return out_rows, state


def _is_guessed_tg_row(row: Mapping[str, Any]) -> bool:
    if bool(row.get("tg_handle_is_guessed")):
        return True
    match_tag = str(row.get("tg_bridge_match") or "").strip().lower()
    return match_tag.startswith(("guessed", "heuristic", "stage3b", "stage4"))


def _append_pending_tg_verify_queue(queue_path: Path, deferred_rows: list[dict[str, Any]]) -> None:
    if not deferred_rows:
        return
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_jsonl_dicts(queue_path) if queue_path.is_file() else []
    seen_keys: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in [*existing, *deferred_rows]:
        if not isinstance(item, Mapping):
            continue
        item_dict = dict(item)
        handle = str(
            item_dict.get("username")
            or item_dict.get("telegram_username")
            or item_dict.get("tg_username")
            or item_dict.get("deferred_tg_handle")
            or ""
        ).strip().lstrip("@").lower()
        comp = str(item_dict.get("company") or item_dict.get("project") or item_dict.get("project_url") or "").strip().lower()
        key = f"{handle}|{comp}" if handle or comp else json.dumps(item_dict, sort_keys=True, ensure_ascii=False)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(item_dict)
    tmp = queue_path.with_name(f".{queue_path.name}.tmp.{os.getpid()}")
    tmp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in merged) + ("\n" if merged else ""),
        encoding="utf-8",
    )
    tmp.replace(queue_path)


def verify_personal_tg_handles_live(
    rows: list[dict[str, Any]],
    *,
    client: Any = None,
    scraper_session: Path | None = None,
    scraper_sessions: list[Path] | None = None,
    entity_resolver_fn: Callable[[Any, str], Any] | None = None,
    web_probe_fn: Callable[[str], Mapping[str, Any] | None] | None = None,
    web_batch_fn: Callable[[list[str]], Mapping[str, Any] | None] | None = None,
    use_web_first: bool = False,
    tme_cache_path: Path | None = None,
    pending_verify_queue_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Strip channel/group/bot handles from personal username fields, resolving PeerType live (Web-first + RO pool)."""
    stats = {
        "checked": 0,
        "verified_users": 0,
        "heuristic_stripped": 0,
        "channels_stripped": 0,
        "bots_or_invalid_stripped": 0,
        "web_resolved": 0,
        "flood_waits": 0,
        "sessions_exhausted": 0,
        "pending_verify_deferred": 0,
    }
    if not rows:
        return [], stats

    out_rows = [dict(r) for r in rows]
    pending_handles: list[str] = []
    seen_handles: set[str] = set()

    for row in out_rows:
        raw_h = str(
            row.get("username") or row.get("telegram_username") or row.get("tg_username") or ""
        ).strip().lstrip("@")
        if not raw_h:
            continue
        if not _is_real_personal_tg_handle(raw_h):
            is_bot_like = raw_h.lower().endswith(("bot", "_bot"))
            _strip_tg_handle_from_row(row, raw_h, move_to_group=not is_bot_like)
            stats["heuristic_stripped"] += 1
            continue
        if row.get("telegram_user_id"):
            continue
        low = raw_h.lower()
        if low not in seen_handles:
            seen_handles.add(low)
            pending_handles.append(raw_h)

    sessions_to_try: list[Path] = []
    seen_sess_paths: set[str] = set()
    for raw_s in [*(scraper_sessions or []), scraper_session]:
        if raw_s is None:
            continue
        sp = Path(raw_s)
        if not sp.is_file():
            continue
        try:
            rkey = str(sp.resolve())
        except OSError:
            rkey = str(sp)
        if rkey not in seen_sess_paths:
            seen_sess_paths.add(rkey)
            sessions_to_try.append(sp)

    web_enabled = (
        entity_resolver_fn is None
        and client is None
        and (
            use_web_first
            or web_probe_fn is not None
            or web_batch_fn is not None
        )
        and (web_probe_fn is not None or web_batch_fn is not None or tg_web_client is not None)
    )
    has_live_resolver = (
        entity_resolver_fn is not None
        or client is not None
        or bool(sessions_to_try)
        or web_enabled
    )
    if not pending_handles or not has_live_resolver:
        return out_rows, stats

    verdicts: dict[str, tuple[str, int | None]] = {}
    saw_session_failure = False

    def _map_web_kind(info: Mapping[str, Any] | None) -> tuple[str, int | None] | None:
        if not isinstance(info, Mapping):
            return None
        kind = str(info.get("kind") or "").strip().lower()
        if kind == "user":
            raw_uid = info.get("user_id")
            try:
                uid = int(raw_uid) if raw_uid is not None else None
            except (TypeError, ValueError):
                uid = None
            return ("user", uid)
        if kind == "channel":
            return ("channel", None)
        if kind in {"bot", "missing", "invalid"}:
            return ("drop", None)
        return None

    async def _resolve_all(active_client: Any, handles_batch: list[str]) -> tuple[dict[str, tuple[str, int | None]], bool]:
        cache: dict[str, tuple[str, int | None]] = {}
        hit_flood = False
        for handle in handles_batch:
            stats["checked"] += 1
            try:
                if entity_resolver_fn is not None:
                    res = entity_resolver_fn(active_client, handle)
                    ent = await res if inspect.isawaitable(res) else res
                else:
                    ent = await active_client.get_entity(handle)
            except Exception as exc:
                cls_exc = exc.__class__.__name__
                if cls_exc == "FloodWaitError":
                    logger.warning("verify_personal_tg_handles_live hit FloodWaitError; pausing current session")
                    stats["flood_waits"] += 1
                    hit_flood = True
                    break
                if cls_exc in {"UsernameNotOccupiedError", "UsernameInvalidError"} or isinstance(exc, ValueError):
                    cache[handle.lower()] = ("drop", None)
                else:
                    logger.debug("verify_personal_tg_handles_live(%s) warning: %s", handle, exc)
                continue

            if ent is None:
                cache[handle.lower()] = ("drop", None)
                continue
            cls_name = type(ent).__name__
            if (
                cls_name in {"Channel", "Chat"}
                or getattr(ent, "broadcast", False)
                or getattr(ent, "megagroup", False)
            ):
                cache[handle.lower()] = ("channel", None)
            elif (
                getattr(ent, "bot", False)
                or getattr(ent, "deleted", False)
                or getattr(ent, "scam", False)
                or getattr(ent, "fake", False)
            ):
                cache[handle.lower()] = ("drop", None)
            elif cls_name == "User" or getattr(ent, "id", None) is not None:
                raw_uid = getattr(ent, "id", None)
                cache[handle.lower()] = ("user", int(raw_uid) if raw_uid is not None else None)
            else:
                cache[handle.lower()] = ("drop", None)
        return cache, hit_flood

    if entity_resolver_fn is not None or client is not None:
        batch_verdicts, hit_flood = _run_coro_sync(_resolve_all(client, pending_handles)) or ({}, False)
        verdicts.update(batch_verdicts)
        if hit_flood:
            saw_session_failure = True
    else:
        if web_enabled:
            if web_batch_fn is not None:
                try:
                    batch_map = web_batch_fn(pending_handles) or {}
                    if isinstance(batch_map, Mapping):
                        for h in pending_handles:
                            mapped = _map_web_kind(batch_map.get(h) or batch_map.get(h.lower()))
                            if mapped is not None:
                                verdicts[h.lower()] = mapped
                                stats["web_resolved"] += 1
                                stats["checked"] += 1
                except Exception as exc:
                    logger.debug("verify_personal_tg_handles_live web_batch_fn failed: %s", exc)
            for h in pending_handles:
                if h.lower() in verdicts:
                    continue
                try:
                    if web_probe_fn is not None:
                        info = web_probe_fn(h)
                    elif tg_web_client is not None:
                        info = _call_with_supported_kwargs(
                            tg_web_client.probe_tme_handle_web,
                            h,
                            cache_path=tme_cache_path,
                        )
                    else:
                        info = None
                    mapped = _map_web_kind(info)
                    if mapped is not None:
                        verdicts[h.lower()] = mapped
                        stats["web_resolved"] += 1
                        stats["checked"] += 1
                except Exception as exc:
                    logger.debug("verify_personal_tg_handles_live web_probe(%s) warning: %s", h, exc)

        rem_handles = [h for h in pending_handles if h.lower() not in verdicts]
        if rem_handles and sessions_to_try:
            for sess in sessions_to_try:
                rem_handles = [h for h in pending_handles if h.lower() not in verdicts]
                if not rem_handles:
                    break
                try:
                    sess_res = _with_isolated_tg_client(
                        sess,
                        lambda active_client, rem=rem_handles: _resolve_all(active_client, rem),
                    )
                    if sess_res:
                        batch_verdicts, hit_flood = sess_res
                        verdicts.update(batch_verdicts)
                        if hit_flood:
                            saw_session_failure = True
                except Exception as exc:
                    logger.warning("verify_personal_tg_handles_live session %s failed (%s)", sess.name, exc)
                    saw_session_failure = True

        rem_after = [h for h in pending_handles if h.lower() not in verdicts]
        if rem_after and saw_session_failure:
            stats["sessions_exhausted"] += 1

    deferred_rows: list[dict[str, Any]] = []
    pending_handle_set = {h.lower() for h in pending_handles}

    for row in out_rows:
        raw_h = str(
            row.get("username") or row.get("telegram_username") or row.get("tg_username") or ""
        ).strip().lstrip("@")
        if not raw_h:
            continue
        low_h = raw_h.lower()
        outcome = verdicts.get(low_h)
        if outcome is None:
            if low_h in pending_handle_set and _is_guessed_tg_row(row) and (saw_session_failure or (web_enabled and not sessions_to_try)):
                deferred_copy = dict(row)
                deferred_copy["deferred_tg_handle"] = raw_h
                deferred_copy["tg_verify_deferred"] = True
                deferred_copy["tg_verify_deferred_at"] = utc_now()
                deferred_rows.append(deferred_copy)
                _strip_tg_handle_from_row(row, raw_h, move_to_group=False)
                row["tg_verify_deferred"] = True
                stats["pending_verify_deferred"] += 1
            continue
        verdict, uid = outcome
        if verdict == "user":
            if uid is not None:
                row["telegram_user_id"] = uid
            stats["verified_users"] += 1
        elif verdict == "channel":
            _strip_tg_handle_from_row(row, raw_h, move_to_group=True)
            stats["channels_stripped"] += 1
        elif verdict == "drop":
            _strip_tg_handle_from_row(row, raw_h, move_to_group=False)
            stats["bots_or_invalid_stripped"] += 1

    if deferred_rows and pending_verify_queue_path is not None:
        _append_pending_tg_verify_queue(pending_verify_queue_path, deferred_rows)

    return out_rows, stats


def enrich_whales_with_tg_group_admins(
    rows: list[dict[str, Any]],
    *,
    client: Any = None,
    scraper_session: Path | None = None,
    scraper_sessions: list[Path] | None = None,
    admin_extractor_fn: Callable[..., Any] | None = None,
    max_admins: int = 10,
    max_results: int = 2,
    max_groups: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Layer 0 ($0): Resolve personal TG admins for whale rows linking TG communities (with RO session pool)."""
    stats = {"groups_checked": 0, "admins_resolved": 0, "leads_promoted": 0}
    if not rows:
        return [], stats

    targets: list[tuple[int, list[str]]] = []
    for idx, row in enumerate(rows):
        if _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username")):
            continue
        handles = extract_tg_group_handles_from_row(row)
        if handles:
            targets.append((idx, handles))

    if not targets:
        return [dict(r) for r in rows], stats

    out_rows = [dict(r) for r in rows]

    async def _resolve_with_client(
        active_client: Any,
        cache: dict[str, list[Any]],
    ) -> dict[str, list[Any]]:
        for _idx, handles in targets:
            for handle in handles:
                key = handle.lower()
                if key in cache:
                    continue
                if len(cache) >= max_groups:
                    break
                stats["groups_checked"] += 1
                try:
                    if admin_extractor_fn is not None:
                        res = admin_extractor_fn(active_client, handle)
                        admins = await res if inspect.isawaitable(res) else res
                    else:
                        admins = await extract_brand_tg_admins(
                            active_client,
                            handle,
                            max_admins=max_admins,
                            max_results=max_results,
                        )
                    cache[key] = list(admins or [])
                except Exception as exc:
                    if exc.__class__.__name__ == "FloodWaitError":
                        logger.warning("extract_brand_tg_admins hit FloodWaitError for %s; switching session", handle)
                        return cache
                    logger.debug("extract_brand_tg_admins failed for %s: %s", handle, exc)
                    cache[key] = []
        return cache

    resolved_by_group: dict[str, list[Any]] = {}
    if admin_extractor_fn is not None or client is not None:
        resolved_by_group = _run_coro_sync(_resolve_with_client(client, resolved_by_group)) or {}
    else:
        sessions_to_try: list[Path] = []
        seen_sess_paths: set[str] = set()
        for raw_s in [*(scraper_sessions or []), scraper_session]:
            if raw_s is None:
                continue
            sp = Path(raw_s)
            if not sp.is_file():
                continue
            try:
                rkey = str(sp.resolve())
            except OSError:
                rkey = str(sp)
            if rkey not in seen_sess_paths:
                seen_sess_paths.add(rkey)
                sessions_to_try.append(sp)
        for sess in sessions_to_try:
            if len(resolved_by_group) >= max_groups:
                break
            pending_any = any(
                h.lower() not in resolved_by_group
                for _idx, hs in targets
                for h in hs
            )
            if not pending_any:
                break
            try:
                _with_isolated_tg_client(
                    sess,
                    lambda active_client, cache=resolved_by_group: _resolve_with_client(active_client, cache),
                )
            except Exception as exc:
                logger.warning("enrich_whales_with_tg_group_admins session %s failed (%s)", sess.name, exc)

    for idx, handles in targets:
        row = out_rows[idx]
        if _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username")):
            continue
        for handle in handles:
            admins = resolved_by_group.get(handle.lower()) or []
            if not admins:
                continue
            top = admins[0]
            top_u = str(getattr(top, "username", "") or (top.get("username") if isinstance(top, Mapping) else "")).strip().lstrip("@")
            if not top_u:
                continue
            stats["admins_resolved"] += len(admins)
            stats["leads_promoted"] += 1
            row["username"] = top_u
            row["telegram_username"] = top_u
            top_uid = getattr(top, "user_id", None) if not isinstance(top, Mapping) else top.get("user_id")
            if top_uid is not None:
                row["telegram_user_id"] = int(top_uid)
            row["provider"] = "telegram"
            top_role = getattr(top, "role_guess", "") if not isinstance(top, Mapping) else top.get("role_guess", "")
            if top_role:
                row["tg_admin_role"] = str(top_role)
            row["tg_admin_group"] = str(handle)
            row["tg_bridge_match"] = row.get("tg_bridge_match") or "tg_group_admins"
            if len(admins) > 1 and not row.get("secondary_contact_json"):
                sec = admins[1]
                sec_u = str(getattr(sec, "username", "") or (sec.get("username") if isinstance(sec, Mapping) else "")).strip().lstrip("@")
                if sec_u and sec_u.lower() != top_u.lower():
                    sec_role = getattr(sec, "role_guess", "") if not isinstance(sec, Mapping) else sec.get("role_guess", "")
                    row["secondary_contact_json"] = json.dumps(
                        {"username": sec_u, "role": str(sec_role or "Admin"), "provider": "telegram"},
                        ensure_ascii=False,
                    )
            break
    return out_rows, stats


def find_connected_wa_session(conn: sqlite3.Connection) -> str:
    """Return session_path for a healthy connected WhatsApp account in DB, else ''."""
    rows = conn.execute(
        """
        SELECT session_path, status, health
        FROM accounts
        WHERE lower(COALESCE(provider, 'telegram')) = 'whatsapp'
          AND lower(COALESCE(status, '')) = 'connected'
          AND lower(COALESCE(health, 'active')) NOT IN ('offline', 'unauthorized', 'disconnected', 'dead', 'banned', 'frozen', 'flood_wait')
        ORDER BY id ASC
        """
    ).fetchall()
    for r in rows:
        raw_path = str(r["session_path"] or "").strip()
        if not raw_path:
            continue
        return resolve_session(raw_path)
    return ""


_TG_TERMINAL_CHECK_STATUSES = frozenset({"found", "not_found"})


def find_phone_checker_sessions(
    conn: sqlite3.Connection,
    sessions_dir: Path | str | None = None,
) -> list[Path]:
    """Return all dedicated isolated ``phone_checker_*.session`` paths sorted deterministically."""
    base_dir = Path(sessions_dir or DEFAULT_SCRAPER_SESSIONS_DIR)
    if not base_dir.is_dir():
        return []
    found: list[Path] = []
    for cand in sorted(base_dir.glob("phone_checker_*.session")):
        if not cand.is_file():
            continue
        try:
            validated = validate_isolated_scraper_session(conn, cand)
            if validated is not None and validated.is_file():
                found.append(validated)
        except ValueError as exc:
            logger.warning("Rejected non-isolated phone_checker session %s: %s", cand, exc)
            continue
    return found


def find_phone_checker_session(
    conn: sqlite3.Connection,
    sessions_dir: Path | str | None = None,
) -> Path | None:
    """Strictly locate the first dedicated isolated ``phone_checker_*.session`` file; never use outreach sessions."""
    sessions = find_phone_checker_sessions(conn, sessions_dir)
    return sessions[0] if sessions else None


def find_ro_scraper_sessions(
    conn: sqlite3.Connection,
    sessions_dir: Path | str | None = None,
    *,
    primary_session: str | Path | None = None,
    allow_phone_checker_ro_fallback: bool = True,
) -> list[Path]:
    """Return ordered RO Telegram scraper session pool (primary -> signals_ro*.session -> phone_checker_2+.session)."""
    pool: list[Path] = []
    seen_resolved: set[str] = set()

    def _add_candidate(cand: Path | None) -> None:
        if cand is None or not cand.is_file():
            return
        try:
            key = str(cand.resolve())
        except OSError:
            key = str(cand)
        if key not in seen_resolved:
            seen_resolved.add(key)
            pool.append(cand)

    if primary_session is not None and str(primary_session).strip():
        validated_prim = validate_isolated_scraper_session(
            conn,
            primary_session,
            scraper_sessions_dir=sessions_dir,
        )
        if (
            validated_prim is not None
            and validated_prim.is_file()
            and validated_prim.name != "phone_checker_1.session"
        ):
            _add_candidate(validated_prim)

    raw_dir = str(
        sessions_dir
        or os.environ.get("SCRAPER_SESSIONS_DIR")
        or DEFAULT_SCRAPER_SESSIONS_DIR
    ).strip()
    base_dir = Path(os.path.expanduser(raw_dir))
    if base_dir.is_dir():
        for cand in sorted(base_dir.glob("signals_ro*.session")):
            if not cand.is_file():
                continue
            try:
                validated = validate_isolated_scraper_session(
                    conn,
                    cand,
                    scraper_sessions_dir=base_dir,
                )
            except ValueError as exc:
                logger.warning("Rejected non-isolated signals_ro session %s: %s", cand, exc)
                continue
            if validated is not None and validated.is_file():
                _add_candidate(validated)

    if allow_phone_checker_ro_fallback:
        for checker_sess in find_phone_checker_sessions(conn, base_dir):
            match_num = re.search(r"phone_checker_(\d+)\.session$", checker_sess.name)
            if not match_num:
                continue
            if int(match_num.group(1)) >= 2:
                _add_candidate(checker_sess)

    return pool


def _load_source_yield_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "source_yield_state.json"
    if not path.is_file():
        return {"sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return {"sources": {}}


def _save_source_yield_state(state_dir: Path, state: Mapping[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "source_yield_state.json"
    tmp = state_dir / f".source_yield_state.json.tmp.{os.getpid()}"
    tmp.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _is_source_in_cooldown(
    state: Mapping[str, Any],
    source_name: str,
    *,
    now_ts: float | None = None,
) -> bool:
    sources = state.get("sources")
    if not isinstance(sources, Mapping):
        return False
    entry = sources.get(source_name)
    if not isinstance(entry, Mapping):
        return False
    try:
        until = float(entry.get("cooldown_until") or 0.0)
    except (TypeError, ValueError):
        return False
    cur_ts = time.time() if now_ts is None else float(now_ts)
    return until > cur_ts


def _record_source_yield(
    state: dict[str, Any],
    source_name: str,
    rows_count: int,
    *,
    now_ts: float | None = None,
) -> bool:
    """Update consecutive 0-yield streak and put empty eligible sources into 48h cooldown after 3 zeros."""
    cur_ts = time.time() if now_ts is None else float(now_ts)
    sources = state.setdefault("sources", {})
    entry = dict(sources.get(source_name) or {})
    cnt = max(0, int(rows_count or 0))
    entered_cooldown = False
    if cnt > 0:
        entry["zero_streak"] = 0
        entry["cooldown_until"] = 0.0
        entry["last_nonzero_at"] = cur_ts
        entry["last_yield"] = cnt
    else:
        streak = int(entry.get("zero_streak") or 0) + 1
        entry["zero_streak"] = streak
        entry["last_yield"] = 0
        if source_name in COOLDOWN_ELIGIBLE_SOURCES and streak >= COOLDOWN_ZERO_STREAK:
            entry["cooldown_until"] = cur_ts + COOLDOWN_TTL_SEC
            entered_cooldown = True
    entry["updated_at"] = cur_ts
    sources[source_name] = entry
    return entered_cooldown


def _extract_root_domain(raw_val: Any) -> str:
    s = str(raw_val or "").strip()
    if not s:
        return ""
    if "://" not in s and "." not in s:
        return ""
    parsed = urllib.parse.urlparse(s if "://" in s else f"https://{s}")
    host = (parsed.hostname or "").strip().lower().removeprefix("www.")
    if not host or "." not in host:
        return ""
    return host


def _normalize_phone_digits(raw_val: Any) -> str:
    digits = re.sub(r"\D", "", str(raw_val or ""))
    return digits if len(digits) >= 8 else ""


def _filter_existing_crm_leads(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop rows whose username, non-platform root domain, or phone already exists in CRM leads before paid waterfall."""
    if not rows:
        return [], 0
    existing_users: set[str] = set()
    existing_domains: set[str] = set()
    existing_phones: set[str] = set()
    for db_row in conn.execute("SELECT username, url, company_domain, phone FROM leads").fetchall():
        u = str(db_row["username"] or "").strip().lstrip("@").lower()
        if u:
            existing_users.add(u)
        for dom_cand in (db_row["url"], db_row["company_domain"]):
            dom = _extract_root_domain(dom_cand)
            if dom and dom not in _GENERIC_PLATFORM_DOMAINS:
                existing_domains.add(dom)
        ph = _normalize_phone_digits(db_row["phone"])
        if ph:
            existing_phones.add(ph)

    for rec_row in conn.execute("SELECT username, website FROM campaign_recipients").fetchall():
        u = str(rec_row["username"] or "").strip().lstrip("@").lower()
        if u:
            existing_users.add(u)
        dom = _extract_root_domain(rec_row["website"])
        if dom and dom not in _GENERIC_PLATFORM_DOMAINS:
            existing_domains.add(dom)

    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        raw_u = str(
            item.get("username") or item.get("telegram_username") or item.get("tg_username") or ""
        ).strip().lstrip("@").lower()
        has_real_u = bool(raw_u and _is_real_personal_tg_handle(raw_u))
        if raw_u and raw_u in existing_users:
            dropped += 1
            continue
        ph = _normalize_phone_digits(item.get("phone"))
        if ph and ph in existing_phones:
            dropped += 1
            continue
        dom = _extract_root_domain(
            item.get("project_url") or item.get("website") or item.get("site") or item.get("domain")
        )
        if dom and dom not in _GENERIC_PLATFORM_DOMAINS and dom in existing_domains and not has_real_u:
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def _pick_comment_channels_batch(
    state_dir: Path,
    *,
    excluded: set[str],
    batch_size: int = 10,
    include_repo_seed: bool = False,
) -> list[str]:
    state_file = state_dir / "discovered_comment_channels.txt"
    repo_file = ROOT / "leadgen" / "discovered_comment_channels.txt"
    src_file = state_file if state_file.is_file() else (repo_file if include_repo_seed and repo_file.is_file() else None)
    if src_file is None:
        return []
    raw_tokens: list[str] = []
    try:
        for raw_ln in src_file.read_text(encoding="utf-8", errors="replace").splitlines():
            clean_ln = raw_ln.split("#", 1)[0].strip()
            if clean_ln:
                raw_tokens.append(clean_ln)
    except OSError:
        return []
    candidates = [
        ch
        for ch in _clean_chat_list(raw_tokens)
        if ch.lower() not in excluded
    ]
    if not candidates:
        return []
    cursor_path = state_dir / "comment_chats_cursor.json"
    offset = 0
    if cursor_path.is_file():
        try:
            cur_obj = json.loads(cursor_path.read_text(encoding="utf-8"))
            if isinstance(cur_obj, dict):
                offset = max(0, int(cur_obj.get("offset") or 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            offset = 0
    take = min(max(1, int(batch_size)), len(candidates))
    start = offset % len(candidates)
    selected = [candidates[(start + idx) % len(candidates)] for idx in range(take)]
    next_offset = (start + take) % len(candidates)
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_cur = cursor_path.with_name(f".{cursor_path.name}.tmp.{os.getpid()}")
    tmp_cur.write_text(
        json.dumps({"offset": next_offset, "total": len(candidates)}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_cur.replace(cursor_path)
    return selected


def _retry_pending_tg_verify_queue(
    conn: sqlite3.Connection,
    *,
    state_dir: Path,
    cfg: Mapping[str, Any],
    ro_sessions: list[Path],
    primary_ro_session: Path | None,
    use_web_first: bool,
    is_real_subprocess_runner: bool,
    tg_client: Any = None,
    tg_entity_resolver_fn: Callable[..., Any] | None = None,
    tg_admin_extractor_fn: Callable[..., Any] | None = None,
    run_l2_phone_resolver: bool = False,
    wa_gateway: Any = None,
    tg_checker_client: Any = None,
    scraper_sessions_dir: Path | str | None = None,
    tg_verify_stats: dict[str, int] | None = None,
    tg_admin_stats: dict[str, int] | None = None,
    max_age_days: int = 14,
) -> dict[str, int]:
    """Re-verify guessed TG handles deferred by FloodWait and promote resolved leads into CRM."""
    res_stats = {"retried": 0, "verified": 0, "remaining": 0, "ingested": 0, "direct_x_routed": 0}
    queue_path = state_dir / "pending_tg_verify_queue.jsonl"
    if not queue_path.is_file() or queue_path.stat().st_size == 0:
        return res_stats
    can_verify = bool(ro_sessions or use_web_first or tg_client is not None or tg_entity_resolver_fn is not None)
    if not can_verify:
        return res_stats

    queued_rows = _read_jsonl_dicts(queue_path)
    if not queued_rows:
        queue_path.unlink(missing_ok=True)
        return res_stats

    now_dt = datetime.now(timezone.utc)
    cutoff_dt = now_dt - timedelta(days=max(1, int(max_age_days)))
    retry_candidates: list[dict[str, Any]] = []
    for raw_item in queued_rows:
        item = dict(raw_item)
        def_at = _parse_iso_utc(str(item.get("tg_verify_deferred_at") or ""))
        if def_at is not None and def_at < cutoff_dt:
            continue
        deferred_handle = str(item.get("deferred_tg_handle") or "").strip().lstrip("@")
        if deferred_handle and not str(item.get("username") or "").strip():
            item["username"] = deferred_handle
            item["telegram_username"] = deferred_handle
        item.pop("tg_verify_deferred", None)
        retry_candidates.append(item)

    if not retry_candidates:
        queue_path.unlink(missing_ok=True)
        return res_stats

    res_stats["retried"] = len(retry_candidates)
    verified_rows, v_st = verify_personal_tg_handles_live(
        retry_candidates,
        client=tg_client,
        scraper_session=primary_ro_session,
        scraper_sessions=ro_sessions if (is_real_subprocess_runner and tg_client is None and tg_entity_resolver_fn is None) else None,
        entity_resolver_fn=tg_entity_resolver_fn,
        use_web_first=use_web_first,
        tme_cache_path=state_dir / "tme_probe_cache.json",
        pending_verify_queue_path=None,
    )
    if tg_verify_stats is not None:
        for k, val in v_st.items():
            tg_verify_stats[k] = tg_verify_stats.get(k, 0) + int(val)

    if tg_client is not None or tg_admin_extractor_fn is not None or ro_sessions:
        need_admin = any(
            not _is_real_personal_tg_handle(r.get("username") or r.get("telegram_username"))
            and extract_tg_group_handles_from_row(r)
            for r in verified_rows
            if not r.get("tg_verify_deferred")
        )
        if need_admin:
            verified_rows, a_st = enrich_whales_with_tg_group_admins(
                verified_rows,
                client=tg_client,
                scraper_session=primary_ro_session,
                scraper_sessions=ro_sessions if (is_real_subprocess_runner and tg_client is None and tg_admin_extractor_fn is None) else None,
                admin_extractor_fn=tg_admin_extractor_fn,
            )
            if tg_admin_stats is not None:
                for k, val in a_st.items():
                    tg_admin_stats[k] = tg_admin_stats.get(k, 0) + int(val)

    still_deferred: list[dict[str, Any]] = []
    ready_for_pipeline: list[dict[str, Any]] = []
    for row in verified_rows:
        if row.get("tg_verify_deferred"):
            row_copy = dict(row)
            if not row_copy.get("deferred_tg_handle"):
                h = str(row_copy.get("username") or row_copy.get("telegram_username") or "").strip().lstrip("@")
                if h:
                    row_copy["deferred_tg_handle"] = h
            still_deferred.append(row_copy)
            continue
        if _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username") or row.get("tg_username")):
            res_stats["verified"] += 1
        ready_for_pipeline.append(row)

    res_stats["remaining"] = len(still_deferred)
    if still_deferred:
        tmp_q = queue_path.with_name(f".{queue_path.name}.tmp.{os.getpid()}")
        tmp_q.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in still_deferred) + "\n",
            encoding="utf-8",
        )
        tmp_q.replace(queue_path)
    else:
        queue_path.unlink(missing_ok=True)

    if ready_for_pipeline:
        if run_l2_phone_resolver:
            ready_for_pipeline, _ = resolve_and_route_phone_leads(
                conn,
                ready_for_pipeline,
                cfg=cfg,
                wa_gateway=wa_gateway,
                tg_checker_client=tg_checker_client,
                scraper_sessions_dir=scraper_sessions_dir,
            )
        ts_retry = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        retry_final = state_dir / f"raw_{ts_retry}_verify_retry.final.jsonl"
        retry_final.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in ready_for_pipeline) + "\n",
            encoding="utf-8",
        )
        rb_handle_retry = str(((cfg or {}).get("b2b") or {}).get("reverse_bridge_tg_handle") or "").strip()
        sp = split_tg_and_direct_x_leads(
            retry_final,
            state_dir / "direct_x_leads.jsonl",
            reverse_bridge_tg_handle=rb_handle_retry,
        )
        res_stats["direct_x_routed"] += int(sp.get("direct_x_added") or 0)
        if retry_final.is_file() and retry_final.stat().st_size > 0:
            stamp_rows = _read_jsonl_dicts(retry_final)
            p_res = push_discovered_now(conn)
            res_stats["ingested"] += int(p_res.get("ingested") or 0)
            if stamp_rows:
                _stamp_new_lead_v2_fields(conn, stamp_rows)
        else:
            retry_final.unlink(missing_ok=True)

    return res_stats


def emit_leadgen_daily_digest(
    conn: sqlite3.Connection,
    *,
    summary: Mapping[str, Any],
    digest_opener: Any = None,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Send daily 4-line Wave A summary (1x/day UTC) + instant critical alert (6h dedup)."""
    cur_dt = now_dt if now_dt is not None else datetime.now(timezone.utc)
    if cur_dt.tzinfo is None:
        cur_dt = cur_dt.replace(tzinfo=timezone.utc)
    today_day = cur_dt.strftime("%Y-%m-%d")
    now_iso = cur_dt.strftime("%Y-%m-%dT%H:%M:%S")

    result: dict[str, Any] = {
        "daily_sent": False,
        "daily_skipped": None,
        "instant_alert_sent": False,
        "instant_reasons": [],
        "error": None,
    }

    try:
        token, chat_id = app_digest._operator(conn)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    verify_stats = summary.get("tg_handle_verification") if isinstance(summary.get("tg_handle_verification"), Mapping) else {}
    budget_info = summary.get("budget_state") if isinstance(summary.get("budget_state"), Mapping) else {}
    sessions_exhausted = int(verify_stats.get("sessions_exhausted") or 0)
    raw_treg_bal = budget_info.get("treg_balance_usd")
    try:
        treg_bal = float(raw_treg_bal) if raw_treg_bal is not None else None
    except (TypeError, ValueError):
        treg_bal = None

    instant_reasons: list[str] = []
    if sessions_exhausted > 0:
        instant_reasons.append("все RO TG-сессии в FloodWait/недоступны")
    if treg_bal is not None and treg_bal < 2.0:
        instant_reasons.append(f"баланс treg ${treg_bal:.2f} < $2.00")
    result["instant_reasons"] = list(instant_reasons)

    if instant_reasons:
        prev_inst_iso = ""
        prev_row = conn.execute(
            "SELECT value FROM kv_settings WHERE key = 'leadgen_instant_alert_last_at'"
        ).fetchone()
        if prev_row and prev_row["value"]:
            prev_inst_iso = str(prev_row["value"]).strip()
        prev_inst_dt = _parse_iso_utc(prev_inst_iso)
        within_6h = prev_inst_dt is not None and (cur_dt - prev_inst_dt).total_seconds() < 6 * 3600
        if not within_6h:
            alert_text = (
                "🚨 Leadgen Critical Alert\n"
                f"Причина: {'; '.join(instant_reasons)}\n"
                f"Pending B2B: {int(summary.get('pending_b2b') or 0)} | Волн: {int(summary.get('waves_executed') or 0)}"
            )
            try:
                app_digest._post(
                    app_digest.SEND_URL.format(token=token),
                    {"chat_id": str(chat_id).strip(), "text": alert_text},
                    digest_opener,
                )
                result["instant_alert_sent"] = True
                conn.execute(
                    """
                    INSERT INTO kv_settings (key, value, updated_at)
                    VALUES ('leadgen_instant_alert_last_at', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (now_iso, now_iso),
                )
                conn.commit()
            except Exception as exc:
                result["error"] = str(exc)

    last_day_row = conn.execute(
        "SELECT value FROM kv_settings WHERE key = 'leadgen_daily_digest_last_day'"
    ).fetchone()
    last_day = str(last_day_row["value"]).strip() if last_day_row and last_day_row["value"] else ""
    if last_day == today_day:
        result["daily_skipped"] = "already_sent_today"
        return result

    per_src = summary.get("per_source_counts") if isinstance(summary.get("per_source_counts"), Mapping) else {}
    top_pairs = sorted(
        ((str(k), int(v or 0)) for k, v in per_src.items() if int(v or 0) > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )[:3]
    top_str = ", ".join(f"{k}={v}" for k, v in top_pairs) if top_pairs else "нет новых"
    spent = float(budget_info.get("spent_usd") or 0.0)
    cap = float(budget_info.get("daily_cap_usd") or 0.0)
    treg_str = f"${treg_bal:.2f}" if treg_bal is not None else "n/a"
    raw_monid = budget_info.get("monid_balance_usd")
    monid_str = f"${float(raw_monid):.2f}" if raw_monid is not None else "n/a"
    deferred_cnt = int(verify_stats.get("pending_verify_deferred") or 0)
    cd_sources = list(summary.get("cooldown_sources") or [])
    cd_str = ",".join(str(x) for x in cd_sources) if cd_sources else "нет"
    err_cnt = len(summary.get("stage_errors") or [])

    digest_text = "\n".join([
        f"📊 Leadgen ({today_day}): +{int(summary.get('ingested_total') or 0)} в CRM | pending B2B: {int(summary.get('pending_b2b') or 0)} | direct_x: {int(summary.get('direct_x_routed') or 0)} | волн: {int(summary.get('waves_executed') or 1)}",
        f"🔎 Топ источники: {top_str}",
        f"💰 Бюджет: ${spent:.2f}/${cap:.2f} | treg: {treg_str} | monid: {monid_str}",
        f"🛡 Отложено verify: {deferred_cnt} | Cooldown: {cd_str} | Ошибки: {err_cnt}",
    ])
    try:
        app_digest._post(
            app_digest.SEND_URL.format(token=token),
            {"chat_id": str(chat_id).strip(), "text": digest_text},
            digest_opener,
        )
        result["daily_sent"] = True
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('leadgen_daily_digest_last_day', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (today_day, now_iso),
        )
        conn.commit()
    except Exception as exc:
        result["error"] = str(exc)

    return result


def _lookup_injected_checker_client(
    checker_clients: Mapping[str, Any] | None,
    sess_path: Path,
) -> Any:
    if not checker_clients:
        return None
    for key in (sess_path.name, str(sess_path), sess_path.stem):
        if key in checker_clients:
            return checker_clients[key]
    return None


def _run_tg_checker_pool(
    checker_sessions: list[Path],
    unique_phones: list[str],
    *,
    batch_size: int,
    daily_limit: int,
    tg_checker_client: Any = None,
    tg_checker_clients: Mapping[str, Any] | None = None,
) -> dict[str, ResolvedMessenger]:
    """Run ``resolve_phones_telegram`` across checker sessions, failing over unfinished phones cleanly."""
    if not checker_sessions or not unique_phones:
        return {}
    if tg_checker_client is not None and tg_checker_clients is None:
        return _run_coro_sync(
            resolve_phones_telegram(
                tg_checker_client,
                unique_phones,
                batch_size=batch_size,
                daily_limit=daily_limit,
                pause_s=0.0,
                is_checker_session=True,
            )
        ) or {}

    tg_results: dict[str, ResolvedMessenger] = {}
    for sess_path in checker_sessions:
        pending_phones = [
            p
            for p in unique_phones
            if p not in tg_results
            or tg_results[p].tg_check_status not in _TG_TERMINAL_CHECK_STATUSES
        ]
        if not pending_phones:
            break

        batch_res: dict[str, ResolvedMessenger] = {}
        try:
            if tg_checker_clients is not None:
                injected_cli = _lookup_injected_checker_client(tg_checker_clients, sess_path)
                if injected_cli is None:
                    logger.warning("No injected checker client provided for %s; skipping", sess_path.name)
                    continue
                batch_res = _run_coro_sync(
                    resolve_phones_telegram(
                        injected_cli,
                        pending_phones,
                        batch_size=batch_size,
                        daily_limit=daily_limit,
                        pause_s=0.0,
                        is_checker_session=True,
                    )
                ) or {}
            else:
                batch_res = _with_isolated_tg_client(
                    sess_path,
                    lambda cli, phones=pending_phones: resolve_phones_telegram(
                        cli,
                        phones,
                        batch_size=batch_size,
                        daily_limit=daily_limit,
                        is_checker_session=True,
                    ),
                ) or {}
        except Exception as exc:
            logger.warning("Phone checker session %s failed (%s); failing over if possible", sess_path.name, exc)
            continue

        for phone, res_obj in batch_res.items():
            if res_obj.tg_check_status in _TG_TERMINAL_CHECK_STATUSES or phone not in tg_results:
                tg_results[phone] = res_obj

    return tg_results


def resolve_and_route_phone_leads(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    cfg: Mapping[str, Any] | None = None,
    wa_gateway: Any = None,
    wa_session_path: str | None = None,
    checker_session_path: Path | str | None = None,
    tg_checker_client: Any = None,
    tg_checker_clients: Mapping[str, Any] | None = None,
    scraper_sessions_dir: Path | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Layer 2 ($0): Verify discovered E.164 phones on WA + dedicated TG checker, then route TG > WA > social_manual."""
    stats = {
        "phones_checked": 0,
        "wa_verified": 0,
        "tg_verified": 0,
        "routed_telegram": 0,
        "routed_whatsapp": 0,
        "routed_wa_pending": 0,
        "routed_social_manual": 0,
    }
    if not rows:
        return [], stats

    unique_phones: list[str] = []
    seen_phones: set[str] = set()
    for row in rows:
        if _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username")):
            continue
        raw_p = str(row.get("phone") or row.get("mobile_phone") or row.get("whatsapp") or "").strip()
        e164 = normalize_e164(raw_p)
        if e164 and e164 not in seen_phones:
            seen_phones.add(e164)
            unique_phones.append(e164)

    stats["phones_checked"] = len(unique_phones)
    whales_cfg = (cfg or {}).get("whales") if isinstance(cfg, Mapping) and isinstance((cfg or {}).get("whales"), Mapping) else {}
    daily_lim = int(whales_cfg.get("phone_tg_checker_daily_limit") or 120)
    batch_sz = int(whales_cfg.get("phone_tg_checker_batch_size") or 15)

    wa_results: dict[str, ResolvedMessenger] = {}
    tg_results: dict[str, ResolvedMessenger] = {}

    if unique_phones:
        eff_wa_sess = str(wa_session_path or "").strip() or find_connected_wa_session(conn)
        active_gw = None
        if eff_wa_sess:
            active_gw = wa_gateway
            if active_gw is None and Path(eff_wa_sess).exists():
                try:
                    from app.whatsapp_gateway import WhatsAppGateway

                    active_gw = WhatsAppGateway()
                except Exception as exc:
                    logger.debug("WhatsAppGateway unavailable for phone check: %s", exc)
                    active_gw = None
        if eff_wa_sess and active_gw is not None:
            wa_results = _run_coro_sync(
                resolve_phones_whatsapp(active_gw, eff_wa_sess, unique_phones)
            ) or {}
        else:
            wa_results = _run_coro_sync(
                resolve_phones_whatsapp(None, "", unique_phones)
            ) or {}

        checker_sessions: list[Path] = []
        if checker_session_path is not None:
            cand_p = Path(checker_session_path)
            if cand_p.name.startswith("phone_checker_") and cand_p.suffix == ".session" and cand_p.is_file():
                validated = validate_isolated_scraper_session(conn, cand_p)
                if validated is not None and validated.is_file():
                    checker_sessions = [validated]
        else:
            checker_sessions = find_phone_checker_sessions(conn, scraper_sessions_dir)

        if checker_sessions:
            tg_results = _run_tg_checker_pool(
                checker_sessions,
                unique_phones,
                batch_size=batch_sz,
                daily_limit=daily_lim,
                tg_checker_client=tg_checker_client,
                tg_checker_clients=tg_checker_clients,
            )

    stats["wa_verified"] = sum(1 for v in wa_results.values() if v.whatsapp_exists)
    stats["tg_verified"] = sum(1 for v in tg_results.values() if v.telegram_username)
    merged = merge_resolved_messengers(wa_results, tg_results)

    routed_rows: list[dict[str, Any]] = []
    for row in rows:
        lang = str(row.get("language") or "").strip().lower() or detect_lead_language(row)
        routed = route_resolved_lead(row, merged, language=lang)
        if str(routed.get("segment") or "").strip().lower() == "social_manual":
            stats["routed_social_manual"] += 1
        elif str(routed.get("provider") or "").strip().lower() == "telegram" and routed.get("username"):
            stats["routed_telegram"] += 1
        elif str(routed.get("provider") or "").strip().lower() == "whatsapp" and not routed.get("wa_check_pending"):
            stats["routed_whatsapp"] += 1
        elif routed.get("wa_check_pending"):
            stats["routed_wa_pending"] += 1
        routed_rows.append(routed)

    return routed_rows, stats


def _resolve_vac_jobs_session(
    conn: sqlite3.Connection,
    vac_cfg: Mapping[str, Any],
    isolated_session: Path | None,
    *,
    sessions_dir: Path | str | None = None,
) -> Path | None:
    """Resolve isolated Telegram session for VPS `job_monitor` without falling back to outreach sessions."""
    raw_cfg_sess = str(vac_cfg.get("vps_scraper_session") or "").strip()
    if raw_cfg_sess:
        cand = Path(os.path.expanduser(raw_cfg_sess))
        if cand.is_file():
            return validate_isolated_scraper_session(conn, cand)
    if isolated_session is not None and isolated_session.is_file():
        return isolated_session
    default_jobs = Path(sessions_dir or DEFAULT_SCRAPER_SESSIONS_DIR) / "jobs_reader.session"
    if default_jobs.is_file():
        return validate_isolated_scraper_session(conn, default_jobs)
    return None


class _SafeDraftFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _build_assisted_vacancy_draft(
    row: Mapping[str, Any],
    language: str,
    *,
    cfg: Mapping[str, Any] | None = None,
) -> str:
    """Build a short SoftUGC assisted outreach draft for job postings, applying editable_prompt_override."""
    title = str(row.get("title") or row.get("role") or row.get("what_they_do") or "UGC / Short-Form role").strip()
    company = str(row.get("company") or row.get("project") or row.get("name") or "").strip()
    desc = str(row.get("description") or row.get("what_they_do") or "").strip()
    url = str(row.get("manual_action_url") or row.get("job_url") or row.get("url") or "").strip()
    vac_cfg = (cfg or {}).get("vacancies") if isinstance(cfg, Mapping) and isinstance((cfg or {}).get("vacancies"), Mapping) else {}
    showcase_url = str(vac_cfg.get("showcase_url") or "https://tmxfy.github.io/").strip() or "https://tmxfy.github.io/"
    override = str(vac_cfg.get("editable_prompt_override") or "").strip()
    if override:
        if "{" in override and "}" in override:
            rendered = override.format_map(
                _SafeDraftFormatDict(
                    title=title,
                    role=title,
                    company=company,
                    description=desc,
                    showcase_url=showcase_url,
                    url=url,
                )
            )
        elif language == "ru":
            head = f"Добрый день! По вакансии «{title}»" + (f" ({company})" if company else "") + "."
            rendered = f"{head}\n\n{override}\n\nКейсы (SoftUGC): {showcase_url}"
        else:
            head_en = f"Hi! Saw your \"{title}\" opening" + (f" at {company}" if company else "") + "."
            rendered = f"{head_en}\n\n{override}\n\nCases (SoftUGC): {showcase_url}"
        return re.sub(r"\bcreator\s*os\b", "SoftUGC", rendered, flags=re.I).strip()

    if language == "ru":
        head = f"Добрый день! По вакансии «{title}»" + (f" ({company})" if company else "") + "."
        return (
            f"{head}\n\n"
            "Внедряем AI UGC и системный продакшн Reels/Shorts/TikTok под ключ (SoftUGC): "
            "консистентные AI-персонажи и серийный выпуск без затрат на съёмку.\n"
            f"Портфолио и кейсы: {showcase_url} — могу собрать тестовый ролик 10–15 сек по вашему ТЗ."
        )
    head_en = f"Hi! Saw your \"{title}\" opening" + (f" at {company}" if company else "") + "."
    return (
        f"{head_en}\n\n"
        "At SoftUGC we build scalable AI UGC & short-form video pipelines for TikTok, Reels, and Shorts "
        f"with consistent AI creators and fast weekly hook testing (cases: {showcase_url}). "
        "Happy to share a quick 10-15s sample tailored to your product."
    )


def run_wave2_vacancies_pipeline(
    conn: sqlite3.Connection,
    *,
    cfg: Mapping[str, Any],
    state_dir: Path,
    isolated_session: Path | None,
    has_llm_key: bool,
    budget_state: EnrichmentBudgetState,
    runner: Callable[..., Any],
    exec_stage_fn: Callable[[str, list[str]], bool],
    py_bin: str,
    today_str: str,
    sub_env: dict[str, str],
    quarantined: set[str],
    treg_ready: bool,
    stage_errors: list[dict[str, Any]],
    replenish_reasons: list[str],
    hh_collector_fn: Callable[..., dict[str, Any]] | None = None,
    freelance_monitor_fn: Callable[..., dict[str, Any]] | None = None,
    linkedin_collector_fn: Callable[..., list[dict[str, Any]]] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    waterfall_enrich_fn: Callable[..., dict[str, Any]] | None = None,
    wa_gateway: Any = None,
    tg_checker_client: Any = None,
    scraper_sessions_dir: Path | str | None = None,
    is_real_subprocess: bool | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Wave 2 Vacancy pipeline: TG job_monitor, hh.ru, Profi/Kwork/Upwork, LinkedIn Jobs -> waterfall -> language_router."""
    vac_cfg = cfg.get("vacancies") if isinstance(cfg.get("vacancies"), Mapping) else {}
    vac_sources = vac_cfg.get("sources") if isinstance(vac_cfg.get("sources"), Mapping) else {}
    b2b_sources = (cfg.get("b2b") or {}).get("sources") if isinstance(cfg.get("b2b"), Mapping) else {}
    b2b_sources = b2b_sources if isinstance(b2b_sources, Mapping) else {}
    vac_limit = max(1, int(vac_cfg.get("daily_limit") or 20))
    showcase_url = str(vac_cfg.get("showcase_url") or "https://tmxfy.github.io/").strip()

    stats: dict[str, Any] = {
        "tg_jobs_ran": False,
        "hh_status": "skipped",
        "hh_count": 0,
        "freelance_status": "skipped",
        "freelance_count": 0,
        "freelance_sources": {},
        "linkedin_jobs_count": 0,
        "routed_vacancy": 0,
        "routed_vacancy_assisted": 0,
    }

    # 1) TG job_monitor + job_reply (strictly isolated VPS jobs session)
    vac_session = _resolve_vac_jobs_session(
        conn,
        vac_cfg,
        isolated_session,
        sessions_dir=scraper_sessions_dir,
    )
    if vac_sources.get("tg_chats", True) and "tg_chats" not in quarantined and vac_session is not None:
        sub_env["TG_JOBS_SESSION"] = str(vac_session)
        mon_out = state_dir / f"job_monitor_{today_str}.json"
        rep_out = state_dir / f"job_replies_{today_str}.json"
        exec_stage_fn(
            "job_monitor",
            [
                py_bin,
                str(WORKTREE_ROOT / "leadgen" / "job_monitor.py"),
                "--hours",
                "24",
                "--out",
                str(mon_out),
            ],
        )
        stats["tg_jobs_ran"] = True
        is_real_sub = (runner is None or runner is subprocess.run) if is_real_subprocess is None else bool(is_real_subprocess)
        sub_env["BUDGET_PAID_ALLOWED"] = "1" if budget_state.paid_allowed else "0"
        if has_llm_key and (
            (mon_out.is_file() and mon_out.stat().st_size > 0)
            or not is_real_sub
        ):
            exec_stage_fn(
                "job_reply",
                [
                    py_bin,
                    str(WORKTREE_ROOT / "leadgen" / "job_reply.py"),
                    str(mon_out),
                    "--out",
                    str(rep_out),
                    "--limit",
                    str(vac_limit),
                    "--no-crm",
                ],
            )

    is_real_sub = (runner is None or runner is subprocess.run) if is_real_subprocess is None else bool(is_real_subprocess)
    run_external_wave2 = is_real_sub or any(
        fn is not None for fn in (hh_collector_fn, freelance_monitor_fn, linkedin_collector_fn, http_get_fn)
    )
    if not run_external_wave2:
        return [], stats

    raw_candidates: list[dict[str, Any]] = []

    # 2) hh.ru API vacancies (works even when paid enrichment budget == $0)
    custom_draft_tpl = str(vac_cfg.get("editable_prompt_override") or "").strip()
    if vac_sources.get("hh_ru", True) and "hh_ru" not in quarantined:
        try:
            hh_res = (
                _call_with_supported_kwargs(
                    hh_collector_fn,
                    limit=vac_limit,
                    showcase_url=showcase_url,
                    custom_draft_template=custom_draft_tpl,
                    http_get_fn=http_get_fn,
                    cfg=cfg,
                )
                if hh_collector_fn is not None
                else collect_hh_vacancies(
                    limit=vac_limit,
                    showcase_url=showcase_url,
                    custom_draft_template=custom_draft_tpl,
                    http_get_fn=http_get_fn,
                )
            )
        except Exception as exc:
            stage_errors.append({"stage": "hh_vacancies", "error": str(exc), "at": utc_now()})
            hh_res = {"status": "error", "rows": [], "error": str(exc)}
        hh_status = str((hh_res or {}).get("status") or "error")
        stats["hh_status"] = hh_status
        if hh_status == "auth_required" and "hh_ru:auth_required" not in replenish_reasons:
            replenish_reasons.append("hh_ru:auth_required")
        hh_rows = list((hh_res or {}).get("rows") or [])
        stats["hh_count"] = len(hh_rows)
        raw_candidates.extend(hh_rows)

    # 3) Profi.ru / Kwork / Upwork freelance monitors (free assisted feeds)
    enabled_fl = [
        src
        for src in ("profi_ru", "kwork", "upwork")
        if vac_sources.get(src, True) and src not in quarantined
    ]
    if enabled_fl:
        custom_fl_map = {src: custom_draft_tpl for src in enabled_fl} if custom_draft_tpl else None
        try:
            fl_res = (
                _call_with_supported_kwargs(
                    freelance_monitor_fn,
                    sources=tuple(enabled_fl),
                    http_get_fn=http_get_fn,
                    cfg=cfg,
                    showcase_url=showcase_url,
                    custom_template_by_source=custom_fl_map,
                    max_items_per_source=vac_limit,
                )
                if freelance_monitor_fn is not None
                else monitor_all_sources(
                    sources=tuple(enabled_fl),
                    http_get_fn=http_get_fn,
                    cfg=cfg,
                    showcase_url=showcase_url,
                    custom_template_by_source=custom_fl_map,
                    max_items_per_source=vac_limit,
                )
            )
        except Exception as exc:
            stage_errors.append({"stage": "freelance_monitors", "error": str(exc), "at": utc_now()})
            fl_res = {"status": "error", "rows": [], "sources": {}}
        stats["freelance_status"] = str((fl_res or {}).get("status") or "error")
        fl_sources = dict((fl_res or {}).get("sources") or {})
        stats["freelance_sources"] = {
            k: str((v or {}).get("status") or "") for k, v in fl_sources.items()
        }
        for fl_src, fl_meta in fl_sources.items():
            if str((fl_meta or {}).get("status") or "") == "blocked_by_antibot":
                tag = f"{fl_src}:blocked_by_antibot"
                if tag not in replenish_reasons:
                    replenish_reasons.append(tag)
        fl_rows = list((fl_res or {}).get("rows") or [])
        stats["freelance_count"] = len(fl_rows)
        raw_candidates.extend(fl_rows)

    # 4) LinkedIn Jobs (when collector hook injected or paid budget + treg CLI ready)
    if (
        vac_sources.get("linkedin_jobs", True)
        and b2b_sources.get("linkedin_jobs", True)
        and "linkedin_jobs" not in quarantined
    ):
        if linkedin_collector_fn is None and not budget_state.paid_allowed:
            if "budget_guard_skipped:linkedin_jobs" not in replenish_reasons:
                replenish_reasons.append("budget_guard_skipped:linkedin_jobs")
        elif linkedin_collector_fn is not None or treg_ready:
            try:
                li_rows = (
                    linkedin_collector_fn()
                    if linkedin_collector_fn is not None
                    else collect_linkedin_jobs()
                )
            except Exception as exc:
                stage_errors.append({"stage": "linkedin_jobs", "error": str(exc), "at": utc_now()})
                li_rows = []
            li_rows = list(li_rows or [])
            stats["linkedin_jobs_count"] = len(li_rows)
            raw_candidates.extend(li_rows)

    if not raw_candidates:
        return [], stats

    # Enrich domain-bearing vacancy rows (LinkedIn Jobs & domain hh_ru) through budget-guarded contact_waterfall
    enrich_indices: list[int] = []
    enrich_batch: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_candidates):
        has_tg = _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username"))
        has_dom = bool(str(row.get("company_domain") or row.get("project_url") or "").strip())
        if not has_tg and has_dom:
            enrich_indices.append(idx)
            enrich_batch.append(row)

    if enrich_batch:
        wf_rows, _ = run_contact_waterfall_with_budget(
            conn,
            enrich_batch,
            cfg=cfg,
            budget_state=budget_state,
            enrich_fn=waterfall_enrich_fn,
            http_get_fn=http_get_fn,
        )
        for pos, wf_row in zip(enrich_indices, wf_rows):
            raw_candidates[pos] = wf_row

    # Layer 2 phone resolution on Wave 2 leads that yielded E.164 phone numbers
    phone_candidates = [
        r
        for r in raw_candidates
        if normalize_e164(str(r.get("phone") or r.get("whatsapp") or ""))
        and not _is_real_personal_tg_handle(r.get("username") or r.get("telegram_username"))
    ]
    if phone_candidates:
        routed_phones, _ = resolve_and_route_phone_leads(
            conn,
            raw_candidates,
            cfg=cfg,
            wa_gateway=wa_gateway,
            tg_checker_client=tg_checker_client,
            scraper_sessions_dir=scraper_sessions_dir,
        )
        raw_candidates = routed_phones

    routed_vac_rows: list[dict[str, Any]] = []
    for item in raw_candidates:
        row = dict(item)
        lang = str(row.get("language") or "").strip().lower() or detect_lead_language(row)
        lang = "ru" if lang == "ru" else "en"
        row["language"] = lang

        has_tg = _is_real_personal_tg_handle(row.get("username") or row.get("telegram_username"))
        wa = whatsapp_url(row.get("whatsapp") or "") or (
            whatsapp_url(row.get("phone") or "")
            if str(row.get("provider") or "").strip().lower() == "whatsapp"
            and not bool(row.get("wa_check_pending"))
            else ""
        )
        if has_tg:
            row["username"] = str(row.get("username") or row.get("telegram_username") or "").strip().lstrip("@")
            row["provider"] = "telegram"
            row["segment"] = "vacancy"
            if custom_draft_tpl:
                row["step1_text"] = _build_assisted_vacancy_draft(row, lang, cfg=cfg)
                row["first_touch"] = row["step1_text"]
            stats["routed_vacancy"] += 1
        elif wa:
            wa_digits = re.sub(r"\D", "", wa)
            row["whatsapp"] = f"https://wa.me/{wa_digits}"
            row["phone"] = f"+{wa_digits}"
            row["provider"] = "whatsapp"
            row["segment"] = "vacancy"
            if custom_draft_tpl:
                row["step1_text"] = _build_assisted_vacancy_draft(row, lang, cfg=cfg)
                row["first_touch"] = row["step1_text"]
            stats["routed_vacancy"] += 1
        else:
            row["segment"] = "vacancy_assisted"
            row["provider"] = "manual"
            row["manual_action_url"] = str(
                row.get("manual_action_url") or row.get("job_url") or row.get("url") or row.get("alternate_url") or ""
            ).strip()
            if custom_draft_tpl:
                row["manual_draft_text"] = _build_assisted_vacancy_draft(row, lang, cfg=cfg)
            else:
                row["manual_draft_text"] = str(
                    row.get("manual_draft_text") or row.get("step1_text") or _build_assisted_vacancy_draft(row, lang, cfg=cfg)
                ).strip()
            stats["routed_vacancy_assisted"] += 1

        route_info = route_lead(row, cfg=cfg)
        row["target_campaign"] = route_info.target_campaign
        routed_vac_rows.append(row)

    return routed_vac_rows, stats


def run_wave3_ru_experts_pipeline(
    conn: sqlite3.Connection,
    *,
    cfg: Mapping[str, Any],
    budget_state: EnrichmentBudgetState,
    tg_client: Any = None,
    scraper_session: Path | None = None,
    ru_experts_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Wave 3 RU Experts pipeline: discover RU creators/experts cleanly with exclude_from_airtable=1."""
    ru_cfg = cfg.get("ru_experts") if isinstance(cfg.get("ru_experts"), Mapping) else {}
    if not bool(ru_cfg.get("enabled", False)):
        return [], {"enabled": False, "collected": 0}

    def _ru_budget_guard(provider: str = "treg", estimated_cost: float = 0.0) -> bool:
        if not budget_state.paid_allowed:
            return False
        ok, _ = check_and_reserve_budget(conn, provider=provider, estimated_cost=estimated_cost, cfg=cfg)
        return ok

    ig_enabled = bool(ru_cfg.get("ig_to_tg_enabled", True))
    ig_handles_cfg = (
        [str(h).strip() for h in (ru_cfg.get("ig_handles") or ()) if str(h or "").strip()]
        if ig_enabled
        else []
    )

    res: dict[str, Any]
    if ru_experts_fn is not None:
        raw_res = _call_with_supported_kwargs(
            ru_experts_fn,
            cfg=cfg,
            ig_handles=ig_handles_cfg,
            client=tg_client,
            conn=conn,
            budget_guard_fn=_ru_budget_guard,
            treg_fn=treg_fn,
            monid_fn=monid_fn,
            http_get_fn=http_get_fn,
        )
        res = (_run_coro_sync(raw_res) if inspect.isawaitable(raw_res) else raw_res) or {}
    else:
        if tg_client is not None or not (ru_cfg.get("seed_chats") or []):
            res = _run_coro_sync(
                discover_ru_experts(
                    cfg=cfg,
                    ig_handles=ig_handles_cfg if ig_handles_cfg else None,
                    client=tg_client,
                    conn=conn,
                    budget_guard_fn=_ru_budget_guard,
                    treg_fn=treg_fn,
                    monid_fn=monid_fn,
                    http_get_fn=http_get_fn,
                )
            ) or {}
        else:
            res = _with_isolated_tg_client(
                scraper_session,
                lambda cli: discover_ru_experts(
                    cfg=cfg,
                    ig_handles=ig_handles_cfg if ig_handles_cfg else None,
                    client=cli,
                    conn=conn,
                    budget_guard_fn=_ru_budget_guard,
                    treg_fn=treg_fn,
                    monid_fn=monid_fn,
                    http_get_fn=http_get_fn,
                ),
            ) or {"status": "skipped", "rows": [], "stats": {}}

    rows: list[dict[str, Any]] = []
    target_camp = resolve_target_campaign("ru_expert", "ru", "telegram", cfg=cfg)
    for item in res.get("rows") or ():
        row = dict(item)
        row["segment"] = "ru_expert"
        row["language"] = "ru"
        row["exclude_from_airtable"] = 1
        row["target_campaign"] = target_camp
        rows.append(row)
    return rows, {
        "enabled": True,
        "status": str(res.get("status") or "ok"),
        "collected": len(rows),
        "target_campaign": target_camp,
        "stats": dict(res.get("stats") or {}),
    }


def _copy_profile_for_segment_and_lang(segment: str, language: str) -> str:
    return resolve_copy_profile(None, segment=segment, language=language, default_profile="b2b_legacy")


def _ensure_v2_campaign(
    conn: sqlite3.Connection,
    campaign_name: str,
    *,
    segment: str,
    language: str,
    exclude_from_airtable: int = 0,
    daily_limit: int = 20,
) -> dict[str, Any]:
    """Find or create a draft v2 campaign with segment, language, copy_profile, and Airtable isolation flags."""
    existing = _find_campaign_by_name(conn, campaign_name)
    copy_prof = _copy_profile_for_segment_and_lang(segment, language)
    eff_excl = 1 if (exclude_from_airtable or segment == "ru_expert") else 0
    if existing is not None:
        conn.execute(
            """
            UPDATE campaigns
            SET segment = ?, language = ?, copy_profile = ?, exclude_from_airtable = ?, updated_at = ?
            WHERE id = ?
            """,
            (segment, language, copy_prof, eff_excl, utc_now(), int(existing["id"])),
        )
        conn.commit()
        refreshed = _find_campaign_by_name(conn, campaign_name)
        return refreshed or existing

    steps = [
        {"step_order": 1, "delay_hours": 0, "template_text": "{step1_text}"},
        {"step_order": 2, "delay_hours": 24, "template_text": "{step2_text}"},
        {"step_order": 3, "delay_hours": 72, "template_text": "{step3_text}"},
    ]
    return create_campaign(
        conn,
        name=campaign_name,
        daily_limit=max(1, min(500, int(daily_limit or 20))),
        steps=steps,
        status="draft",
        leadgen_enabled=True,
        segment=segment,
        language=language,
        copy_profile=copy_prof,
        exclude_from_airtable=eff_excl,
    )


def _persist_lead_v2_columns(conn: sqlite3.Connection, lead_id: int, row: Mapping[str, Any]) -> None:
    """Persist v2 metadata columns (segment, language, Airtable isolation, manual queue fields) on `leads`."""
    seg = str(row.get("segment") or "").strip().lower() or "b2b"
    lang = "ru" if str(row.get("language") or "").strip().lower() == "ru" else "en"
    excl = 1 if (seg == "ru_expert" or bool(row.get("exclude_from_airtable"))) else 0
    domain = str(row.get("company_domain") or row.get("domain") or "").strip() or None
    raw_pool = row.get("pool_usd") if row.get("pool_usd") is not None else row.get("bounty_usd")
    pool_val: int | None = None
    if raw_pool is not None and str(raw_pool).strip() != "":
        try:
            pool_val = int(round(float(raw_pool)))
        except (TypeError, ValueError):
            pool_val = None
    sec_json = row.get("secondary_contact_json")
    if isinstance(sec_json, Mapping):
        sec_json = json.dumps(dict(sec_json), ensure_ascii=False)
    elif not sec_json:
        sec_json = None
    m_url = str(row.get("manual_action_url") or "").strip() or None
    m_draft = str(row.get("manual_draft_text") or "").strip() or None
    conn.execute(
        """
        UPDATE leads
        SET segment = ?,
            language = ?,
            exclude_from_airtable = ?,
            company_domain = COALESCE(?, company_domain),
            pool_usd = COALESCE(?, pool_usd),
            secondary_contact_json = COALESCE(?, secondary_contact_json),
            manual_action_url = COALESCE(?, manual_action_url),
            manual_draft_text = COALESCE(?, manual_draft_text),
            updated_at = ?
        WHERE id = ?
        """,
        (seg, lang, excl, domain, pool_val, sec_json, m_url, m_draft, utc_now(), int(lead_id)),
    )


def _insert_manual_queue_lead(conn: sqlite3.Connection, row: Mapping[str, Any]) -> int | None:
    """Insert or skip duplicate Manual Social / Assisted Job queue lead into CRM `leads` without auto-sending."""
    seg = str(row.get("segment") or "").strip().lower()
    lang = "ru" if str(row.get("language") or "").strip().lower() == "ru" else "en"
    manual_url = str(row.get("manual_action_url") or "").strip()
    if not manual_url:
        return None
    dup = conn.execute(
        "SELECT id FROM leads WHERE manual_action_url = ? LIMIT 1",
        (manual_url,),
    ).fetchone()
    if dup is not None:
        dup_id = int(dup["id"])
        dup_phone = normalize_e164(str(row.get("phone") or "")) or None
        dup_email = str(row.get("work_email") or row.get("email") or "").strip() or None
        dup_domain = str(row.get("company_domain") or row.get("domain") or "").strip() or None
        dup_draft = str(row.get("manual_draft_text") or "").strip() or None
        conn.execute(
            """
            UPDATE leads
            SET phone = COALESCE(NULLIF(trim(COALESCE(phone, '')), ''), ?),
                email = COALESCE(NULLIF(trim(COALESCE(email, '')), ''), ?),
                company_domain = COALESCE(NULLIF(trim(COALESCE(company_domain, '')), ''), ?),
                manual_draft_text = COALESCE(NULLIF(trim(COALESCE(manual_draft_text, '')), ''), ?),
                updated_at = ?
            WHERE id = ?
            """,
            (dup_phone, dup_email, dup_domain, dup_draft, utc_now(), dup_id),
        )
        conn.commit()
        return None

    company = str(row.get("company") or row.get("project") or "").strip()
    name = str(row.get("name") or company or row.get("title") or row.get("source") or "Manual Lead").strip()
    source = str(row.get("lead_source") or row.get("source") or seg).strip()
    email = str(row.get("work_email") or row.get("email") or "").strip() or None
    site_url = str(row.get("project_url") or row.get("url") or manual_url).strip() or None
    desc = str(row.get("what_they_do") or row.get("description") or row.get("bio") or "").strip() or None
    note = str(row.get("note") or row.get("their_angle") or "").strip() or None
    niche = str(row.get("category") or row.get("niche") or "").strip() or None
    draft = str(row.get("manual_draft_text") or "").strip()
    row_copy = dict(row)
    row_copy["segment"] = seg
    row_copy["language"] = lang
    row_copy["manual_action_url"] = manual_url
    if not draft:
        row_copy["manual_draft_text"] = _build_assisted_vacancy_draft(row, lang) if seg == "vacancy_assisted" else ""

    created = crm_create_lead(
        conn,
        name=name,
        phone=normalize_e164(str(row.get("phone") or "")) or None,
        company=company or None,
        description=desc,
        niche=niche,
        url=site_url,
        lead_source=source,
        email=email,
        note=note,
        provider="telegram",
        next_step="Отправить вручную по ссылке",
    )
    lid = int(created["id"])
    _persist_lead_v2_columns(conn, lid, row_copy)
    conn.commit()
    return lid


def _insert_direct_segment_lead(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    campaign: dict[str, Any],
    segment: str,
    language: str,
    enroll_in_campaign: bool,
) -> tuple[int | None, bool]:
    """Insert lead + campaign recipient cleanly for direct v2 segments (`ru_expert`, `vacancy`, `whale`)."""
    username = str(row.get("username") or row.get("telegram_username") or "").strip().lstrip("@")
    phone = normalize_e164(str(row.get("phone") or "")) or None
    raw_uid = row.get("telegram_user_id")
    tg_uid: int | None = int(raw_uid) if raw_uid not in (None, "", 0, "0") else None
    if not (username or phone or tg_uid):
        return None, False

    existing_lead = None
    if username:
        existing_lead = conn.execute(
            "SELECT * FROM leads WHERE lower(username) = lower(?) LIMIT 1", (username,)
        ).fetchone()
    if existing_lead is None and phone:
        existing_lead = conn.execute(
            "SELECT * FROM leads WHERE phone = ? LIMIT 1", (phone,)
        ).fetchone()
    if existing_lead is not None:
        lid = int(existing_lead["id"])
        lead_tg_id = existing_lead["telegram_user_id"]
    else:
        company = str(row.get("company") or row.get("project") or "").strip()
        name = str(row.get("name") or company or username or phone or "Lead").strip()
        prov = "whatsapp" if str(row.get("provider") or "").lower() == "whatsapp" and not username else "telegram"
        created = crm_create_lead(
            conn,
            telegram_user_id=tg_uid,
            username=username or None,
            name=name,
            phone=phone,
            company=company or None,
            description=str(row.get("what_they_do") or row.get("bio") or "").strip() or None,
            niche=str(row.get("category") or row.get("niche") or "").strip() or None,
            url=str(row.get("project_url") or row.get("url") or "").strip() or None,
            lead_source=str(row.get("lead_source") or row.get("source") or segment).strip(),
            email=str(row.get("work_email") or row.get("email") or "").strip() or None,
            note=str(row.get("note") or row.get("their_angle") or "").strip() or None,
            offer_details=str(row.get("our_angle") or "").strip() or None,
            provider=prov,
            next_step="Отправить цепочку рассылки" if enroll_in_campaign else None,
        )
        lid = int(created["id"])
        lead_tg_id = created.get("telegram_user_id") or tg_uid or (900000 + lid)
        conn.execute(
            "UPDATE leads SET telegram_user_id = ? WHERE id = ? AND telegram_user_id IS NULL",
            (int(lead_tg_id), lid),
        )

    _persist_lead_v2_columns(conn, lid, {**dict(row), "segment": segment, "language": language})

    enrolled = False
    cid = int(campaign["id"])
    if enroll_in_campaign:
        dup_rec = conn.execute(
            """
            SELECT id FROM campaign_recipients
            WHERE campaign_id = ?
              AND (lead_id = ? OR (? != '' AND lower(username) = lower(?)) OR (? != '' AND phone = ?))
            LIMIT 1
            """,
            (cid, lid, username, username, phone or "", phone or ""),
        ).fetchone()
        if dup_rec is None:
            copy_prof = _copy_profile_for_segment_and_lang(segment, language)
            seq = build_outreach_sequence(
                dict(row),
                copy_profile=copy_prof,
                segment=segment,
                language=language,
                provider=str(row.get("provider") or "telegram"),
            )
            step1 = str(row.get("step1_text") or row.get("first_touch") or seq.step1_text).strip()
            step2 = str(row.get("step2_text") or row.get("followup_1") or seq.step2_text).strip()
            step3 = str(row.get("step3_text") or row.get("followup_2") or seq.step3_text).strip()
            now_s = utc_now()
            conn.execute(
                """
                INSERT INTO campaign_recipients (
                    campaign_id, lead_id, telegram_user_id, username, name,
                    phone, company, note, website, category, their_angle,
                    our_angle, hook, step1_text, step2_text, step3_text,
                    current_step, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)
                """,
                (
                    cid,
                    lid,
                    int(lead_tg_id) if lead_tg_id is not None else None,
                    username or None,
                    str(row.get("name") or row.get("company") or username or phone or f"Лид #{lid}"),
                    phone,
                    str(row.get("company") or row.get("project") or "").strip() or None,
                    str(row.get("note") or row.get("their_angle") or "").strip() or None,
                    str(row.get("project_url") or row.get("url") or "").strip() or None,
                    str(row.get("category") or row.get("niche") or "").strip() or None,
                    str(row.get("their_angle") or row.get("what_they_do") or "").strip() or None,
                    str(row.get("our_angle") or "").strip() or None,
                    step1,
                    step1,
                    step2,
                    step3,
                    now_s,
                    now_s,
                ),
            )
            enrolled = True
    conn.commit()
    return lid, enrolled


def _lookup_ingested_lead_id(conn: sqlite3.Connection, item: Mapping[str, Any]) -> int | None:
    """Resolve an ingested lead's SQLite ID cleanly by stable identity keys (never by positional zip)."""
    uname = str(item.get("username") or item.get("telegram_username") or "").strip().lstrip("@")
    if uname:
        row = conn.execute(
            "SELECT id FROM leads WHERE lower(username) = lower(?) LIMIT 1", (uname,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    phone = normalize_e164(str(item.get("phone") or ""))
    if phone:
        row = conn.execute("SELECT id FROM leads WHERE phone = ? LIMIT 1", (phone,)).fetchone()
        if row is not None:
            return int(row["id"])
    manual_url = str(item.get("manual_action_url") or "").strip()
    if manual_url:
        row = conn.execute(
            "SELECT id FROM leads WHERE manual_action_url = ? LIMIT 1", (manual_url,)
        ).fetchone()
        if row is not None:
            return int(row["id"])
    comp = str(item.get("company") or item.get("project") or "").strip()
    name = str(item.get("name") or "").strip()
    if comp and name:
        row = conn.execute(
            "SELECT id FROM leads WHERE lower(COALESCE(company, '')) = lower(?) AND lower(COALESCE(name, '')) = lower(?) ORDER BY id DESC LIMIT 1",
            (comp, name),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    return None


def ingest_v2_routed_leads(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    cfg: Mapping[str, Any] | None = None,
    auto_push: bool = True,
) -> dict[str, Any]:
    """Ingest Wave 1-3 routed leads (`social_manual`, `vacancy_assisted`, `vacancy`, `ru_expert`, `whale`)."""
    stats = {
        "social_manual_ingested": 0,
        "vacancy_assisted_ingested": 0,
        "vacancy_ingested": 0,
        "ru_experts_ingested": 0,
        "whales_ingested": 0,
        "campaigns": {},
    }
    if not rows:
        return stats

    from app.mcp_server import call_mcp_tool_handler

    camp_cache: dict[str, dict[str, Any]] = {}
    mcp_batches: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}

    for raw in rows:
        item = dict(raw)
        route_info = route_lead(item, cfg=cfg)
        seg = route_info.segment
        lang = route_info.language
        excl = 1 if (seg == "ru_expert" or bool(item.get("exclude_from_airtable"))) else 0

        if seg == "social_manual":
            if _insert_manual_queue_lead(conn, {**item, "segment": seg, "language": lang}):
                stats["social_manual_ingested"] += 1
            continue
        if seg == "vacancy_assisted":
            if _insert_manual_queue_lead(conn, {**item, "segment": seg, "language": lang}):
                stats["vacancy_assisted_ingested"] += 1
            continue
        if seg == "ru_expert":
            camp_name = route_info.target_campaign
            if camp_name not in camp_cache:
                camp_cache[camp_name] = _ensure_v2_campaign(
                    conn,
                    camp_name,
                    segment="ru_expert",
                    language="ru",
                    exclude_from_airtable=1,
                    daily_limit=int(((cfg or {}).get("ru_experts") or {}).get("daily_limit") or 20),
                )
            _lid, enrolled = _insert_direct_segment_lead(
                conn,
                item,
                campaign=camp_cache[camp_name],
                segment="ru_expert",
                language="ru",
                enroll_in_campaign=bool(auto_push),
            )
            if enrolled or _lid is not None:
                stats["ru_experts_ingested"] += 1
                stats["campaigns"][camp_name] = camp_cache[camp_name]
            continue

        camp_name = route_info.target_campaign
        mcp_batches.setdefault((camp_name, seg, lang, excl), []).append(item)

    for (camp_name, seg, lang, excl), batch in mcp_batches.items():
        camp_obj = _ensure_v2_campaign(
            conn,
            camp_name,
            segment=seg,
            language=lang,
            exclude_from_airtable=excl,
            daily_limit=int(((cfg or {}).get("vacancies") or {}).get("daily_limit") or 20)
            if seg == "vacancy"
            else 30,
        )
        camp_cache[camp_name] = camp_obj
        stats["campaigns"][camp_name] = camp_obj

        enriched_batch: list[dict[str, Any]] = []
        for item in batch:
            copy_prof = _copy_profile_for_segment_and_lang(seg, lang)
            seq = build_outreach_sequence(
                item,
                copy_profile=copy_prof,
                segment=seg,
                language=lang,
                provider=str(item.get("provider") or "telegram"),
            )
            prepared = dict(item)
            prepared.setdefault("step1_text", seq.step1_text)
            prepared.setdefault("first_touch", seq.step1_text)
            prepared.setdefault("step2_text", seq.step2_text)
            prepared.setdefault("followup_1", seq.step2_text)
            prepared.setdefault("step3_text", seq.step3_text)
            prepared.setdefault("followup_2", seq.step3_text)
            enriched_batch.append(prepared)

        if seg == "vacancy":
            for item in enriched_batch:
                _lid, enrolled = _insert_direct_segment_lead(
                    conn,
                    item,
                    campaign=camp_obj,
                    segment=seg,
                    language=lang,
                    enroll_in_campaign=bool(auto_push),
                )
                if enrolled:
                    stats["vacancy_ingested"] += 1
            continue

        mcp_res = call_mcp_tool_handler(
            conn,
            "ingest_discovered_leads",
            {
                "campaign_id": int(camp_obj["id"]),
                "campaign_name": camp_name,
                "enroll_in_campaign": bool(auto_push),
                "leads": enriched_batch,
            },
        )
        added_n = int((mcp_res or {}).get("enrolled_in_campaign") or 0)
        by_idx = (mcp_res or {}).get("lead_id_by_index")
        if isinstance(by_idx, list) and len(by_idx) == len(enriched_batch):
            for idx, item in enumerate(enriched_batch):
                lid_raw = by_idx[idx]
                if lid_raw is None:
                    continue
                _persist_lead_v2_columns(
                    conn,
                    int(lid_raw),
                    {**item, "segment": seg, "language": lang, "exclude_from_airtable": excl},
                )
        else:
            for item in enriched_batch:
                matched_lid = _lookup_ingested_lead_id(conn, item)
                if matched_lid is not None:
                    _persist_lead_v2_columns(
                        conn,
                        matched_lid,
                        {**item, "segment": seg, "language": lang, "exclude_from_airtable": excl},
                    )
        conn.commit()
        stats["whales_ingested"] += added_n

    return stats


def _stamp_new_lead_v2_fields(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> None:
    """Propagate v2 metadata (`segment`, `language`, `company_domain`, `pool_usd`, `secondary_contact_json`) onto newly ingested `leads`."""
    if not rows:
        return
    for row in rows:
        seg = str(row.get("segment") or "").strip().lower() or detect_lead_segment(row)
        lang = str(row.get("language") or "").strip().lower() or detect_lead_language(row)
        uname = str(row.get("username") or row.get("telegram_username") or "").strip().lstrip("@")
        phone = normalize_e164(str(row.get("phone") or ""))
        found = None
        if uname:
            found = conn.execute(
                "SELECT id FROM leads WHERE lower(username) = lower(?) LIMIT 1", (uname,)
            ).fetchone()
        if found is None and phone:
            found = conn.execute(
                "SELECT id FROM leads WHERE phone = ? LIMIT 1", (phone,)
            ).fetchone()
        if found is not None:
            _persist_lead_v2_columns(
                conn,
                int(found["id"]),
                {**row, "segment": seg, "language": lang},
            )
    conn.commit()


def _resolve_active_b2b_camp_fallback(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Fallback to an active running leadgen B2B TG campaign when target name does not exist yet."""
    soft_cand = find_existing_softugc_tg_campaign(conn)
    if (
        soft_cand is not None
        and str(soft_cand.get("status") or "").strip().lower() == "running"
        and int(soft_cand.get("leadgen_enabled") or 0) == 1
    ):
        return soft_cand

    from app.hygiene import is_ofm_campaign_name

    rows = conn.execute(
        """
        SELECT * FROM campaigns
        WHERE lower(COALESCE(status, '')) = 'running'
          AND leadgen_enabled = 1
        ORDER BY id ASC
        """
    ).fetchall()
    for row in rows:
        cdict = as_dict(row)
        cname = str(cdict.get("name") or "").strip()
        cname_low = cname.lower()
        seg = str(cdict.get("segment") or "").strip().lower()
        if seg in {"ofm", "ru_expert", "vacancy", "vacancy_assisted", "social_manual"}:
            continue
        if any(k in cname_low for k in ("ваканс", "vacanc", "job")):
            continue
        if "(wa)" in cname_low or "whatsapp" in cname_low or " — wa" in cname_low:
            continue
        if is_ofm_campaign_name(cname, cdict.get("leadgen_sources_json")):
            continue
        return cdict
    return None


def _collect_vacancy_campaigns_state(
    conn: sqlite3.Connection,
    vac_name: str,
    *,
    vac_enabled: bool,
    vac_low_water_mark: int,
) -> tuple[bool, bool, int, list[dict[str, Any]]]:
    """Evaluate active status & aggregate pending recipients across legacy + v2 vacancy campaigns."""
    names_ordered: list[str] = []
    seen_names: set[str] = set()
    for cand_name in (
        vac_name,
        CAMPAIGN_VACANCY_RU_TG,
        CAMPAIGN_VACANCY_EN_TG,
        CAMPAIGN_VACANCY_RU_WA,
    ):
        clean_n = str(cand_name or "").strip()
        if clean_n and clean_n.lower() not in seen_names:
            seen_names.add(clean_n.lower())
            names_ordered.append(clean_n)

    found_camps: list[dict[str, Any]] = []
    non_completed_camps: list[dict[str, Any]] = []
    v2_camps: list[dict[str, Any]] = []
    for cname in names_ordered:
        camp = _find_campaign_by_name(conn, cname)
        if camp is None:
            continue
        found_camps.append(camp)
        if cname != vac_name:
            v2_camps.append(camp)
        st = str(camp.get("status") or "").strip().lower()
        if st == "completed":
            continue
        if (
            cname != vac_name
            and st not in ("running", "active")
            and int(camp.get("leadgen_enabled") or 0) != 1
        ):
            continue
        non_completed_camps.append(camp)

    all_completed = len(found_camps) == len(names_ordered) and not non_completed_camps
    # Keep vacancies active if v2 campaigns can still accept leads even when legacy "TG Вакансии" is completed.
    vac_active = bool(vac_enabled) and not all_completed
    if found_camps and not non_completed_camps and not v2_camps:
        vac_active = bool(vac_enabled)

    pending_total = sum(_count_pending(conn, int(c["id"])) for c in non_completed_camps)
    if not vac_active:
        return False, False, pending_total, non_completed_camps

    if not non_completed_camps:
        return True, (0 < int(vac_low_water_mark)), 0, []

    any_below = any(
        _count_pending(conn, int(c["id"])) < int(vac_low_water_mark)
        for c in non_completed_camps
    )
    needs = pending_total < int(vac_low_water_mark) or any_below
    return True, needs, pending_total, non_completed_camps


def _resolve_direct_x_social_manual_url_and_draft(
    row: Mapping[str, Any], language: str
) -> tuple[str, str]:
    """Build (manual_action_url, manual_draft_text) for high-ICP Direct/X fallback rows."""
    lang = "ru" if str(language or "").strip().lower() == "ru" else "en"
    manual_url = ""
    draft_kind = "social"

    for key in ("founder_linkedin", "linkedin_url", "linkedin", "li_url"):
        cand = _normalize_social_url(row.get(key), "linkedin")
        if cand:
            manual_url = cand
            break
    if not manual_url:
        for key in ("founder_x", "x_url", "founder_twitter", "twitter", "twitter_url"):
            cand = _normalize_social_url(row.get(key), "x")
            if cand:
                manual_url = cand
                break
    if not manual_url:
        for key in ("instagram_url", "instagram_handle", "instagram", "ig_url", "ig_handle"):
            cand = _normalize_social_url(row.get(key), "instagram")
            if cand:
                manual_url = cand
                break
    if not manual_url:
        em = email_of_text(row.get("work_email") or row.get("email") or "")
        if em:
            manual_url = f"mailto:{em}"
            draft_kind = "email"
    if not manual_url:
        e164 = normalize_e164(str(row.get("phone") or row.get("mobile_phone") or ""))
        if e164:
            manual_url = f"https://wa.me/{e164.lstrip('+')}"
            draft_kind = "whatsapp_hlr"
    if not manual_url:
        raw_m = str(row.get("manual_action_url") or "").strip()
        if raw_m.lower().startswith(("http://", "https://", "mailto:")):
            manual_url = raw_m
            if raw_m.lower().startswith("mailto:"):
                draft_kind = "email"
            elif raw_m.lower().startswith("https://wa.me/"):
                draft_kind = "whatsapp_hlr"

    if not manual_url:
        return "", ""

    draft = str(row.get("manual_draft_text") or "").strip()
    if not draft:
        if draft_kind in ("email", "whatsapp_hlr"):
            draft = build_fallback_contact_draft(row, lang, draft_kind)
        else:
            draft = _build_bridge_dm(row, lang)
    return manual_url, draft


def _retro_bridge_direct_x_leads(
    conn: sqlite3.Connection,
    *,
    state_dir: Path,
    cfg: Mapping[str, Any],
    budget_state: EnrichmentBudgetState | None,
    is_real_subprocess_runner: bool,
    isolated_session: Path | None,
    tg_client: Any,
    tg_entity_resolver_fn: Callable[..., Any] | None,
    waterfall_treg_fn: Callable[..., Any] | None,
    waterfall_monid_fn: Callable[..., Any] | None,
    waterfall_http_get_fn: Callable[..., Any] | None,
    tg_verify_stats: dict[str, int],
    max_rows: int = 12,
) -> dict[str, int]:
    """Bounded retro-bridge (up to max_rows un-attempted rows) + high-ICP social_manual surfacing."""
    stats = {
        "attempted": 0,
        "tg_bridged": 0,
        "whales_ingested": 0,
        "social_manual_ingested": 0,
    }
    direct_x_path = state_dir / "direct_x_leads.jsonl"
    if not direct_x_path.is_file() or direct_x_path.stat().st_size == 0:
        return stats

    all_rows = _read_jsonl_dicts(direct_x_path)
    if not all_rows:
        return stats

    batch_indices = [
        idx
        for idx, row in enumerate(all_rows)
        if not str(row.get("tg_bridge_attempted_at") or "").strip()
    ][: max(0, int(max_rows))]
    if not batch_indices:
        return stats

    import leadgen.tg_identity_bridge as _tg_bridge_mod

    active_bridge_fn = (
        bridge_lead_to_telegram
        if bridge_lead_to_telegram is not _ORIG_BRIDGE_LEAD_TO_TELEGRAM
        else _tg_bridge_mod.bridge_lead_to_telegram
    )
    bridge_patched = active_bridge_fn is not _ORIG_BRIDGE_LEAD_TO_TELEGRAM
    has_injected_io = any(
        fn is not None
        for fn in (waterfall_treg_fn, waterfall_monid_fn, waterfall_http_get_fn)
    )
    external_bridge_allowed = (
        is_real_subprocess_runner or bridge_patched or has_injected_io
    )
    paid_allowed = bool(budget_state is None or budget_state.paid_allowed)
    _guard_fn, _cost_fn = _make_budget_callbacks(conn, cfg, budget_state)

    eff_treg_fn = waterfall_treg_fn if paid_allowed else (lambda *args, **kwargs: {})
    eff_monid_fn = waterfall_monid_fn if paid_allowed else (lambda *args, **kwargs: {})

    fetch_site_wrapper = None
    if waterfall_http_get_fn is not None:

        def _fetch_site_from_http(domain_arg: str) -> str:
            clean_dom = str(domain_arg or "").strip().lower().removeprefix("www.").strip("/")
            if not clean_dom:
                return ""
            try:
                resp = waterfall_http_get_fn(f"https://{clean_dom}")
                return str(getattr(resp, "text", resp) or "")
            except Exception:
                return ""

        fetch_site_wrapper = _fetch_site_from_http

    live_sess_dx = (
        isolated_session
        if (
            is_real_subprocess_runner
            or tg_client is not None
            or tg_entity_resolver_fn is not None
        )
        else None
    )

    auto_push = bool(cfg.get("auto_push_to_crm", True))
    bridged_for_whales: list[dict[str, Any]] = []
    removed_indices: set[int] = set()
    now_stamp = utc_now()

    for idx in batch_indices:
        stats["attempted"] += 1
        row = dict(all_rows[idx])
        if external_bridge_allowed:
            bridged_raw = _call_with_supported_kwargs(
                active_bridge_fn,
                dict(row),
                allow_paid=paid_allowed,
                budget_guard_fn=_guard_fn,
                cost_record_fn=_cost_fn,
                treg_fn=eff_treg_fn,
                monid_fn=eff_monid_fn,
                fetch_site_fn=fetch_site_wrapper,
            )
        else:
            bridged_raw = _call_with_supported_kwargs(
                active_bridge_fn,
                dict(row),
                allow_paid=False,
                x_profile_fn=lambda *_a, **_kw: None,
                search_fn=lambda *_a, **_kw: None,
                tme_fetch_fn=lambda *_a, **_kw: "",
                fetch_site_fn=lambda *_a, **_kw: "",
                treg_fn=lambda *_a, **_kw: {},
                monid_fn=lambda *_a, **_kw: {},
            )
        candidate = dict(bridged_raw) if isinstance(bridged_raw, Mapping) else dict(row)

        cand_u = (
            str(
                candidate.get("username")
                or candidate.get("telegram_username")
                or candidate.get("tg_username")
                or ""
            )
            .strip()
            .lstrip("@")
        )
        if cand_u:
            verified_rows, v_stats = verify_personal_tg_handles_live(
                [candidate],
                client=tg_client,
                scraper_session=live_sess_dx,
                entity_resolver_fn=tg_entity_resolver_fn,
            )
            for k, val in v_stats.items():
                tg_verify_stats[k] = tg_verify_stats.get(k, 0) + int(val)
            candidate = verified_rows[0] if verified_rows else candidate

        verified_u = (
            str(
                candidate.get("username")
                or candidate.get("telegram_username")
                or candidate.get("tg_username")
                or ""
            )
            .strip()
            .lstrip("@")
        )
        if _is_real_personal_tg_handle(verified_u):
            candidate["username"] = verified_u
            candidate["telegram_username"] = verified_u
            candidate["provider"] = "telegram"
            candidate["segment"] = "whale"
            candidate["language"] = (
                "ru"
                if str(candidate.get("language") or "").strip().lower() == "ru"
                else "en"
            )
            src_val = str(candidate.get("lead_source") or candidate.get("source") or "direct_x_bridge").strip()
            candidate["source"] = src_val
            candidate["lead_source"] = src_val
            bridged_for_whales.append(candidate)
            if auto_push:
                removed_indices.add(idx)
            else:
                candidate["tg_bridge_attempted_at"] = now_stamp
                all_rows[idx] = candidate
            continue

        row["tg_bridge_attempted_at"] = now_stamp
        all_rows[idx] = row

        if _parse_icp_score(row.get("icp_score")) >= 82 and not sniper_reject_reason(row):
            has_direct_contact = any(
                str(row.get(k) or "").strip()
                for k in (
                    "founder_linkedin",
                    "linkedin_url",
                    "linkedin",
                    "li_url",
                    "founder_x",
                    "x_url",
                    "founder_twitter",
                    "twitter",
                    "twitter_url",
                    "instagram_url",
                    "instagram_handle",
                    "instagram",
                    "ig_url",
                    "ig_handle",
                    "phone",
                    "mobile_phone",
                    "email",
                    "work_email",
                )
            )
            if has_direct_contact:
                lang = detect_lead_language(row)
                manual_url, manual_draft = _resolve_direct_x_social_manual_url_and_draft(
                    row, lang
                )
                if manual_url:
                    em_norm = email_of_text(row.get("work_email") or row.get("email") or "")
                    dup_email = False
                    if em_norm:
                        dup_email = (
                            conn.execute(
                                "SELECT 1 FROM leads WHERE lower(COALESCE(email, '')) = lower(?) AND segment = 'social_manual' LIMIT 1",
                                (em_norm,),
                            ).fetchone()
                            is not None
                        )
                    if not dup_email:
                        manual_row = dict(row)
                        manual_row["segment"] = "social_manual"
                        manual_row["language"] = lang
                        manual_row["manual_action_url"] = manual_url
                        manual_row["manual_draft_text"] = manual_draft
                        if not str(manual_row.get("lead_source") or "").strip():
                            manual_row["lead_source"] = str(
                                manual_row.get("source") or "direct_x"
                            ).strip()
                        inserted_id = _insert_manual_queue_lead(conn, manual_row)
                        if inserted_id is not None:
                            stats["social_manual_ingested"] += 1

    if bridged_for_whales:
        stats["tg_bridged"] = len(bridged_for_whales)
        if auto_push:
            ingest_res = ingest_v2_routed_leads(
                conn,
                bridged_for_whales,
                cfg=cfg,
                auto_push=True,
            )
            stats["whales_ingested"] += int(ingest_res.get("whales_ingested") or 0)

    kept_rows = [row for idx, row in enumerate(all_rows) if idx not in removed_indices]
    tmp_dx = direct_x_path.with_suffix(".jsonl.tmp")
    tmp_dx.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept_rows)
        + ("\n" if kept_rows else ""),
        encoding="utf-8",
    )
    tmp_dx.replace(direct_x_path)
    return stats


def run_autonomous_replenish(
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., Any] | None = None,
    low_water_mark: int = 50,
    vac_low_water_mark: int = 10,
    critical_low_mark: int = 15,
    stage_timeout: int = 300,
    scraper_session: str | Path | None = None,
    digest_opener: Any = None,
    lock_path: Path | None = None,
    budget_treg_balance_fn: Callable[[], Any] | None = None,
    budget_monid_balance_fn: Callable[[], Any] | None = None,
    budget_notify_fn: Callable[[str], Any] | None = None,
    waterfall_enrich_fn: Callable[..., dict[str, Any]] | None = None,
    waterfall_http_get_fn: Callable[..., Any] | None = None,
    waterfall_treg_fn: Callable[..., Any] | None = None,
    waterfall_monid_fn: Callable[..., Any] | None = None,
    tg_client: Any = None,
    tg_admin_extractor_fn: Callable[..., Any] | None = None,
    tg_entity_resolver_fn: Callable[..., Any] | None = None,
    wa_gateway: Any = None,
    tg_checker_client: Any = None,
    scraper_sessions_dir: Path | str | None = None,
    hh_collector_fn: Callable[..., dict[str, Any]] | None = None,
    freelance_monitor_fn: Callable[..., dict[str, Any]] | None = None,
    linkedin_collector_fn: Callable[..., list[dict[str, Any]]] | None = None,
    ru_experts_fn: Callable[..., Any] | None = None,
    catalog_discovery_fn: Callable[..., Any] | None = None,
    max_waves: int | None = None,
    max_wall_clock_sec: int = 1500,
    web_discovery_workers: int | None = None,
) -> dict[str, Any]:
    try:
        lock_fh = _acquire_replenish_lock(lock_path)
    except BlockingIOError:
        logger.warning(
            "Skipping run_autonomous_replenish: another instance holds single-instance flock"
        )
        return {
            "ok": True,
            "skipped": "already_running",
            "stage_errors": [],
            "stage_error_count": 0,
        }
    try:
        return _run_autonomous_replenish_locked(
            conn,
            runner=runner,
            low_water_mark=low_water_mark,
            vac_low_water_mark=vac_low_water_mark,
            critical_low_mark=critical_low_mark,
            stage_timeout=stage_timeout,
            scraper_session=scraper_session,
            digest_opener=digest_opener,
            budget_treg_balance_fn=budget_treg_balance_fn,
            budget_monid_balance_fn=budget_monid_balance_fn,
            budget_notify_fn=budget_notify_fn,
            waterfall_enrich_fn=waterfall_enrich_fn,
            waterfall_http_get_fn=waterfall_http_get_fn,
            waterfall_treg_fn=waterfall_treg_fn,
            waterfall_monid_fn=waterfall_monid_fn,
            tg_client=tg_client,
            tg_admin_extractor_fn=tg_admin_extractor_fn,
            tg_entity_resolver_fn=tg_entity_resolver_fn,
            wa_gateway=wa_gateway,
            tg_checker_client=tg_checker_client,
            scraper_sessions_dir=scraper_sessions_dir,
            hh_collector_fn=hh_collector_fn,
            freelance_monitor_fn=freelance_monitor_fn,
            linkedin_collector_fn=linkedin_collector_fn,
            ru_experts_fn=ru_experts_fn,
            catalog_discovery_fn=catalog_discovery_fn,
            max_waves=max_waves,
            max_wall_clock_sec=max_wall_clock_sec,
            web_discovery_workers=web_discovery_workers,
        )
    finally:
        _release_replenish_lock(lock_fh)


def _is_catalog_refresh_due(state_dir: Path, max_age_hours: float = 24.0) -> bool:
    """Return True when tg_catalog_state.json is absent or older than max_age_hours."""
    state_path = state_dir / "tg_catalog_state.json"
    if not state_path.is_file() or state_path.stat().st_size == 0:
        return True
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(payload, Mapping):
        return True
    last_at = _parse_iso_utc(str(payload.get("last_run_at") or ""))
    if last_at is None:
        return True
    age_sec = (datetime.now(timezone.utc) - last_at).total_seconds()
    return age_sec >= max(0.0, float(max_age_hours)) * 3600.0


def _run_autonomous_replenish_locked(
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., Any] | None = None,
    low_water_mark: int = 50,
    vac_low_water_mark: int = 10,
    critical_low_mark: int = 15,
    stage_timeout: int = 300,
    scraper_session: str | Path | None = None,
    digest_opener: Any = None,
    budget_treg_balance_fn: Callable[[], Any] | None = None,
    budget_monid_balance_fn: Callable[[], Any] | None = None,
    budget_notify_fn: Callable[[str], Any] | None = None,
    waterfall_enrich_fn: Callable[..., dict[str, Any]] | None = None,
    waterfall_http_get_fn: Callable[..., Any] | None = None,
    waterfall_treg_fn: Callable[..., Any] | None = None,
    waterfall_monid_fn: Callable[..., Any] | None = None,
    tg_client: Any = None,
    tg_admin_extractor_fn: Callable[..., Any] | None = None,
    tg_entity_resolver_fn: Callable[..., Any] | None = None,
    wa_gateway: Any = None,
    tg_checker_client: Any = None,
    scraper_sessions_dir: Path | str | None = None,
    hh_collector_fn: Callable[..., dict[str, Any]] | None = None,
    freelance_monitor_fn: Callable[..., dict[str, Any]] | None = None,
    linkedin_collector_fn: Callable[..., list[dict[str, Any]]] | None = None,
    ru_experts_fn: Callable[..., Any] | None = None,
    catalog_discovery_fn: Callable[..., Any] | None = None,
    max_waves: int | None = None,
    max_wall_clock_sec: int = 1500,
    web_discovery_workers: int | None = None,
) -> dict[str, Any]:
    is_real_subprocess_runner = runner is None or runner is subprocess.run
    active_runner = subprocess.run if runner is None else runner

    cfg = get_leadgen_config(conn)
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "disabled_in_config"}

    # 1. Hard session guard (fails fast with ValueError if colliding with any live outreach session)
    validated_session = validate_isolated_scraper_session(conn, scraper_session)
    isolated_session = (
        validated_session
        if (validated_session is not None and validated_session.is_file())
        else None
    )
    has_llm_key = bool(
        (os.environ.get("RELAYFAST_API_KEY") or os.environ.get("LLM_API_KEY") or "").strip()
    )

    # 2. Subtract quarantined sources & apply active_market_profile overlay
    quarantined = load_quarantined_sources(conn)
    eff_cfg = resolve_effective_market_config(apply_quarantined_sources(cfg, quarantined))
    market_profile = str(eff_cfg.get("active_market_profile") or "global").strip().lower()
    b2b_cfg = eff_cfg["b2b"]
    vac_cfg = eff_cfg["vacancies"]
    whales_cfg = eff_cfg.get("whales") if isinstance(eff_cfg.get("whales"), Mapping) else {}
    ru_cfg = eff_cfg.get("ru_experts") if isinstance(eff_cfg.get("ru_experts"), Mapping) else {}

    b2b_name = resolve_target_b2b_tg_campaign_name(
        conn, b2b_cfg.get("target_campaign_tg"), fallback_default="Leadgen Auto (TG)"
    )
    vac_name = str(vac_cfg.get("campaign_name") or "TG Вакансии").strip() or "TG Вакансии"
    ru_name = str(ru_cfg.get("target_campaign") or CAMPAIGN_RU_EXPERTS_TG).strip() or CAMPAIGN_RU_EXPERTS_TG

    b2b_camp = _find_campaign_by_name(conn, b2b_name) or _resolve_active_b2b_camp_fallback(conn)
    vac_camp = _find_campaign_by_name(conn, vac_name)
    ru_camp = _find_campaign_by_name(conn, ru_name)

    pending_b2b_before = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
    pending_ru_before = _count_pending(conn, ru_camp["id"] if ru_camp else None)
    pending_b2b = pending_b2b_before

    b2b_active = bool(b2b_cfg.get("enabled", True)) and (
        b2b_camp is None or str(b2b_camp.get("status") or "") != "completed"
    )
    vac_active, needs_vac, pending_vac_before, _ = _collect_vacancy_campaigns_state(
        conn,
        vac_name,
        vac_enabled=bool(vac_cfg.get("enabled", False)),
        vac_low_water_mark=int(vac_low_water_mark),
    )
    pending_vac = pending_vac_before
    ru_active = bool(ru_cfg.get("enabled", False)) and (
        ru_camp is None or str(ru_camp.get("status") or "") != "completed"
    )

    needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
    ru_daily_lim = max(0, int(ru_cfg.get("daily_limit") or 20))
    needs_ru = ru_active and ru_daily_lim > 0 and pending_ru_before < ru_daily_lim

    ingested_total = 0
    direct_x_routed = 0
    social_manual_ingested = 0
    stages_run: list[str] = []
    stage_errors: list[dict[str, Any]] = []
    replenish_reasons: list[str] = []
    rule5_previews: dict[str, Any] = {}
    rule5_quarantined: dict[str, list[int]] = {}
    per_source_counts: dict[str, int] = {}
    source_hashes: dict[str, str] = {}
    tg_bridged_count = 0
    treg_ready = check_treg_ready()
    monid_ready = check_monid_ready()

    offline_mock_mode = (
        not is_real_subprocess_runner
        and budget_treg_balance_fn is None
        and budget_monid_balance_fn is None
    )
    eff_treg_bal_fn = (
        budget_treg_balance_fn
        if budget_treg_balance_fn is not None
        else ((lambda: None) if offline_mock_mode else None)
    )
    eff_monid_bal_fn = (
        budget_monid_balance_fn
        if budget_monid_balance_fn is not None
        else ((lambda: None) if offline_mock_mode else None)
    )
    budget_state: EnrichmentBudgetState | None = None
    tg_admin_stats: dict[str, int] = {"groups_checked": 0, "admins_resolved": 0, "leads_promoted": 0}
    tg_verify_stats: dict[str, int] = {
        "checked": 0,
        "verified_users": 0,
        "heuristic_stripped": 0,
        "channels_stripped": 0,
        "bots_or_invalid_stripped": 0,
        "web_resolved": 0,
        "flood_waits": 0,
        "sessions_exhausted": 0,
        "pending_verify_deferred": 0,
    }
    waves_executed = 0
    pre_waterfall_deduped = 0
    cooldown_sources: list[str] = []
    ro_sessions = find_ro_scraper_sessions(
        conn,
        sessions_dir=scraper_sessions_dir,
        primary_session=isolated_session,
        allow_phone_checker_ro_fallback=bool(whales_cfg.get("allow_phone_checker_ro_fallback", True)),
    )
    primary_ro_session = ro_sessions[0] if ro_sessions else isolated_session
    run_started_mono = time.monotonic()
    phone_res_stats: dict[str, int] = {
        "phones_checked": 0,
        "wa_verified": 0,
        "tg_verified": 0,
        "routed_telegram": 0,
        "routed_whatsapp": 0,
        "routed_wa_pending": 0,
        "routed_social_manual": 0,
    }
    wave2_stats: dict[str, Any] = {}
    wave3_stats: dict[str, Any] = {"enabled": bool(ru_cfg.get("enabled", False)), "collected": 0}
    wave_extra_campaigns: dict[str, dict[str, Any]] = {}
    curated_v2_stamp_rows: list[dict[str, Any]] = []

    if needs_b2b or needs_vac or needs_ru:
        budget_state = _call_with_supported_kwargs(
            check_balances_and_alert,
            conn,
            cfg=eff_cfg,
            treg_balance_fn=eff_treg_bal_fn,
            monid_balance_fn=eff_monid_bal_fn,
            notify_fn=budget_notify_fn,
            allow_unavailable=offline_mock_mode,
        )
        if not budget_state.paid_allowed:
            reason_tag = f"budget_guard_stop:{budget_state.stop_reason or 'cap_reached'}"
            if reason_tag not in replenish_reasons:
                replenish_reasons.append(reason_tag)

        pre_existing_recipient_ids: set[int] = {
            int(r[0]) for r in conn.execute("SELECT id FROM campaign_recipients").fetchall()
        }

        # Step 1: Ingest any already-prepared local .jsonl / job_replies without mutating limits
        push_1 = push_discovered_now(conn)
        ingested_total += int(push_1.get("ingested") or 0)

        b2b_camp = _find_campaign_by_name(conn, b2b_name) or _resolve_active_b2b_camp_fallback(conn)
        vac_camp = _find_campaign_by_name(conn, vac_name)
        pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
        vac_active, still_needs_vac, pending_vac, _ = _collect_vacancy_campaigns_state(
            conn,
            vac_name,
            vac_enabled=bool(vac_cfg.get("enabled", False)),
            vac_low_water_mark=int(vac_low_water_mark),
        )

        still_needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
        still_needs_ru = ru_active and ru_daily_lim > 0 and _count_pending(
            conn, ru_camp["id"] if ru_camp else None
        ) < ru_daily_lim
        wa_camp: dict[str, Any] | None = None
        ofm_camp: dict[str, Any] | None = None

        # Step 2: Run server-available discovery stages via injectable runner + Wave 1-3 orchestrators
        if still_needs_b2b or still_needs_vac or still_needs_ru:
            state_dir = _resolve_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            eff_cfg_path = state_dir / "effective_leadgen_config.json"
            eff_cfg_path.write_text(
                json.dumps(eff_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            sub_env = dict(os.environ)
            sub_env["LEADGEN_STATE_DIR"] = str(state_dir)
            sub_env["LEADGEN_CONFIG_PATH"] = str(eff_cfg_path)
            sub_env["BUDGET_PAID_ALLOWED"] = "1" if budget_state.paid_allowed else "0"
            if primary_ro_session is not None:
                sub_env["TG_SESSION"] = str(primary_ro_session)
                sub_env["TG_JOBS_SESSION"] = str(primary_ro_session)

            stage_lock = threading.Lock()

            def _add_reason(tag: str) -> None:
                with stage_lock:
                    if tag not in replenish_reasons:
                        replenish_reasons.append(tag)

            def _exec_stage(stage_name: str, cmd: list[str], *, env_override: Mapping[str, str] | None = None) -> bool:
                with stage_lock:
                    stages_run.append(stage_name)
                run_env = dict(sub_env)
                if env_override:
                    run_env.update(env_override)
                try:
                    proc = active_runner(
                        cmd,
                        env=run_env,
                        timeout=stage_timeout,
                        cwd=str(ROOT),
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    rc = int(getattr(proc, "returncode", 0) or 0)
                    if rc != 0:
                        stderr_snip = str(getattr(proc, "stderr", "") or "").strip()[-500:]
                        with stage_lock:
                            stage_errors.append(
                                {
                                    "stage": stage_name,
                                    "returncode": rc,
                                    "error": stderr_snip or f"exit_code_{rc}",
                                    "at": utc_now(),
                                }
                            )
                        logger.error(
                            "Leadgen stage %s failed (rc=%d): %s", stage_name, rc, stderr_snip
                        )
                        return False
                    return True
                except subprocess.TimeoutExpired as exc:
                    with stage_lock:
                        stage_errors.append(
                            {
                                "stage": stage_name,
                                "returncode": -1,
                                "timeout": stage_timeout,
                                "error": f"timeout_after_{stage_timeout}s: {exc}",
                                "at": utc_now(),
                            }
                        )
                    logger.error(
                        "Leadgen stage %s timed out after %ds", stage_name, stage_timeout
                    )
                    return False
                except Exception as exc:
                    with stage_lock:
                        stage_errors.append(
                            {
                                "stage": stage_name,
                                "returncode": -1,
                                "error": str(exc),
                                "at": utc_now(),
                            }
                        )
                    logger.error("Leadgen stage %s crashed: %s", stage_name, exc)
                    return False

            py_bin = sys.executable
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if still_needs_b2b:
                sources_cfg = b2b_cfg.get("sources") or {}
                limits_cfg = b2b_cfg.get("limits") or {}

                dx_retro = _retro_bridge_direct_x_leads(
                    conn,
                    state_dir=state_dir,
                    cfg=eff_cfg,
                    budget_state=budget_state,
                    is_real_subprocess_runner=is_real_subprocess_runner,
                    isolated_session=primary_ro_session,
                    tg_client=tg_client,
                    tg_entity_resolver_fn=tg_entity_resolver_fn,
                    waterfall_treg_fn=waterfall_treg_fn,
                    waterfall_monid_fn=waterfall_monid_fn,
                    waterfall_http_get_fn=waterfall_http_get_fn,
                    tg_verify_stats=tg_verify_stats,
                    max_rows=12,
                )
                if dx_retro["attempted"] > 0:
                    with stage_lock:
                        stages_run.append("retro_bridge_direct_x")
                    tg_bridged_count += int(dx_retro["tg_bridged"])
                    ingested_total += int(dx_retro["whales_ingested"])
                    social_manual_ingested += int(dx_retro["social_manual_ingested"])

                verify_retry_res = _retry_pending_tg_verify_queue(
                    conn,
                    state_dir=state_dir,
                    cfg=eff_cfg,
                    ro_sessions=ro_sessions,
                    primary_ro_session=primary_ro_session,
                    use_web_first=is_real_subprocess_runner,
                    is_real_subprocess_runner=is_real_subprocess_runner,
                    tg_client=tg_client,
                    tg_entity_resolver_fn=tg_entity_resolver_fn,
                    tg_admin_extractor_fn=tg_admin_extractor_fn,
                    run_l2_phone_resolver=is_real_subprocess_runner or any(
                        x is not None
                        for x in (wa_gateway, tg_checker_client, scraper_sessions_dir)
                    ),
                    wa_gateway=wa_gateway,
                    tg_checker_client=tg_checker_client,
                    scraper_sessions_dir=scraper_sessions_dir,
                    tg_verify_stats=tg_verify_stats,
                    tg_admin_stats=tg_admin_stats,
                )
                if verify_retry_res["retried"] > 0:
                    with stage_lock:
                        stages_run.append("retry_pending_tg_verify_queue")
                    ingested_total += int(verify_retry_res["ingested"])
                    direct_x_routed += int(verify_retry_res["direct_x_routed"])

                effective_max_waves = (
                    max(1, min(3, int(max_waves)))
                    if max_waves is not None
                    else (3 if is_real_subprocess_runner else 1)
                )
                yield_state = _load_source_yield_state(state_dir)

                # Wave B: 24h auto-replenishment of ad_spy, channel_graph, and comment channel catalogs
                if (
                    still_needs_b2b
                    and sources_cfg.get("tg_catalog_discovery", True)
                    and "tg_catalog_discovery" not in quarantined
                    and _is_catalog_refresh_due(state_dir, max_age_hours=24.0)
                ):
                    cat_paid_allowed = budget_state is None or bool(budget_state.paid_allowed)
                    if catalog_discovery_fn is not None:
                        with stage_lock:
                            stages_run.append("tg_catalog_discovery")
                        try:
                            _call_with_supported_kwargs(
                                catalog_discovery_fn,
                                state_dir,
                                allow_paid=cat_paid_allowed,
                            )
                        except Exception as exc:
                            with stage_lock:
                                stage_errors.append(
                                    {
                                        "stage": "tg_catalog_discovery",
                                        "returncode": -1,
                                        "error": f"{type(exc).__name__}: {exc}",
                                        "at": utc_now(),
                                    }
                                )
                    else:
                        cat_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_catalog_discovery.py"),
                            "--state-dir",
                            str(state_dir),
                            "--limit",
                            "40",
                        ]
                        if not cat_paid_allowed:
                            cat_cmd.append("--no-paid")
                        _exec_stage("tg_catalog_discovery", cat_cmd)

                for wave_idx in range(1, effective_max_waves + 1):
                    if not still_needs_b2b:
                        break
                    if wave_idx > 1 and (time.monotonic() - run_started_mono) >= float(max_wall_clock_sec):
                        _add_reason("batch_loop_wall_clock_cap")
                        break
                    waves_executed += 1
                    wave_ts = ts if wave_idx == 1 else f"{ts}_w{wave_idx}"
                    raw_jsonl = state_dir / f"raw_{wave_ts}.jsonl"
                    master_jsonl = state_dir / "master_ids.jsonl"
                    buyer_public_out = state_dir / f"buyer_public_{wave_ts}.jsonl"
                    catalogs_out = state_dir / f"buyer_catalogs_{wave_ts}.jsonl"
                    high_yield_out = state_dir / f"high_yield_{wave_ts}.jsonl"
                    meta_ads_out = state_dir / f"meta_ads_{wave_ts}.jsonl"
                    social_out = state_dir / f"social_{wave_ts}.jsonl"
                    channel_graph_out = state_dir / f"channel_graph_{wave_ts}.jsonl"
                    ad_spy_out = state_dir / f"tg_ad_spy_{wave_ts}.jsonl"
                    external_out = state_dir / f"external_{wave_ts}.jsonl"
                    yc_ph_out = state_dir / f"yc_ph_{wave_ts}.jsonl"
                    tma_apps_out = state_dir / f"tma_apps_{wave_ts}.jsonl"
                    fb_groups_out = state_dir / f"fb_groups_{wave_ts}.jsonl"
                    b2b_tg_groups_out = state_dir / f"b2b_tg_groups_{wave_ts}.jsonl"
                    follower_graph_out = state_dir / f"follower_graph_{wave_ts}.jsonl"
                    ofm_review_path = state_dir / "ofm_review_leads.jsonl"
                    pre_ofm_count = _count_jsonl_lines(ofm_review_path) if ofm_review_path.is_file() else 0

                    paid_cli_discovery_allowed = (
                        (not is_real_subprocess_runner)
                        or budget_state is None
                        or budget_state.paid_allowed
                    )
                    enabled_veins: list[str] = []
                    if (
                        sources_cfg.get("whop_content_rewards", False)
                        and "whop_content_rewards" not in quarantined
                    ):
                        enabled_veins.append("whop")
                    if sources_cfg.get("tiktok_ads", False) and "tiktok_ads" not in quarantined:
                        enabled_veins.append("tiktok_ads")
                    if sources_cfg.get("tiktok_shop", False) and "tiktok_shop" not in quarantined:
                        enabled_veins.append("tiktok_shop")
                    if sources_cfg.get("app_stores", False) and "app_stores" not in quarantined:
                        enabled_veins.append("app_stores")
                    if sources_cfg.get("ats_hiring", False) and "ats_hiring" not in quarantined:
                        enabled_veins.append("ats_hiring")
                    if sources_cfg.get("ph_detailed", False) and "ph_detailed" not in quarantined:
                        enabled_veins.append("ph_detailed")
                    if sources_cfg.get("d2c_whatsapp", False) and "d2c_whatsapp" not in quarantined:
                        enabled_veins.append("d2c_whatsapp")

                    web_tasks: list[tuple[str, Callable[[], None]]] = []
                    wave_ran_sources: set[str] = set()

                    if sources_cfg.get("buyer_catalogs", True) and "buyer_catalogs" not in quarantined:
                        if "buyer_public" not in quarantined:
                            if _is_source_in_cooldown(yield_state, "buyer_public"):
                                _add_reason("cooldown_48h:buyer_public")
                                if "buyer_public" not in cooldown_sources:
                                    cooldown_sources.append("buyer_public")
                            elif paid_cli_discovery_allowed:
                                wave_ran_sources.add("buyer_public")
                                _bp_cmd = [
                                    py_bin,
                                    str(ROOT / "leadgen" / "buyer_public.py"),
                                    "--output",
                                    str(buyer_public_out),
                                ]
                                web_tasks.append((
                                    "buyer_public",
                                    lambda c=_bp_cmd: _exec_stage("buyer_public", c),
                                ))
                            else:
                                _add_reason("budget_guard_skipped:buyer_public")
                        wave_ran_sources.add("buyer_catalogs")
                        _bc_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "buyer_catalogs.py"),
                            "--output",
                            str(catalogs_out),
                            "--seen",
                            str(state_dir / "buyer_catalogs_seen.json"),
                        ]
                        web_tasks.append((
                            "buyer_catalogs",
                            lambda c=_bc_cmd: _exec_stage("buyer_catalogs", c),
                        ))

                    if enabled_veins and "high_yield_veins" not in quarantined:
                        if not paid_cli_discovery_allowed:
                            _add_reason("budget_guard_skipped:high_yield_veins")
                        elif treg_ready or monid_ready or not is_real_subprocess_runner:
                            whop_min_pool = int(
                                whales_cfg.get("whop_min_pool_usd")
                                if whales_cfg.get("whop_min_pool_usd") is not None
                                else 5000
                            )
                            hy_limit = min(200, max(20, int(limits_cfg.get("high_yield_limit") or 80)))
                            wave_ran_sources.add("high_yield_veins")
                            _hy_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "high_yield_veins.py"),
                                "--output",
                                str(high_yield_out),
                                "--seen",
                                str(state_dir / "high_yield_seen.json"),
                                "--limit",
                                str(hy_limit),
                                "--veins",
                                ",".join(enabled_veins),
                                "--whop-min-pool-usd",
                                str(max(0, whop_min_pool)),
                                "--max-runtime-sec",
                                str(min(220, max(60, int(stage_timeout) - 60))),
                                "--market-profile",
                                market_profile,
                            ]
                            web_tasks.append((
                                "high_yield_veins",
                                lambda c=_hy_cmd: _exec_stage("high_yield_veins", c),
                            ))
                        else:
                            _add_reason("missing_treg_cli")

                    if (
                        sources_cfg.get("facebook_groups", False)
                        and "facebook_groups" not in quarantined
                    ):
                        if _is_source_in_cooldown(yield_state, "facebook_groups"):
                            _add_reason("cooldown_48h:facebook_groups")
                            if "facebook_groups" not in cooldown_sources:
                                cooldown_sources.append("facebook_groups")
                        elif not paid_cli_discovery_allowed:
                            _add_reason("budget_guard_skipped:facebook_groups")
                        elif treg_ready or not is_real_subprocess_runner:
                            fb_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "ofm_and_groups.py"),
                                "--mode",
                                "fb_groups",
                                "--output",
                                str(fb_groups_out),
                                "--ofm-out",
                                str(ofm_review_path),
                            ]
                            fb_urls = [str(u).strip() for u in (b2b_cfg.get("fb_group_urls") or []) if str(u).strip()]
                            if fb_urls:
                                fb_cmd.extend(["--fb-urls", ",".join(fb_urls), "--max-groups", str(max(5, len(fb_urls)))])
                            wave_ran_sources.add("facebook_groups")
                            web_tasks.append((
                                "facebook_groups",
                                lambda c=fb_cmd: _exec_stage("facebook_groups", c),
                            ))
                        else:
                            _add_reason("missing_treg_cli")

                    if sources_cfg.get("meta_ads", True) and "meta_ads" not in quarantined:
                        if not paid_cli_discovery_allowed:
                            _add_reason("budget_guard_skipped:meta_ad_leads")
                        elif treg_ready or not is_real_subprocess_runner:
                            meta_seen_path = state_dir / "meta_ads_seen.json"
                            meta_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "meta_ad_leads.py"),
                                "--output",
                                str(meta_ads_out),
                                "--seen",
                                str(meta_seen_path),
                                "--max-runtime-sec",
                                str(min(210, max(60, int(stage_timeout) - 60))),
                            ]
                            whales_geos = [
                                str(g).strip().upper()
                                for g in (whales_cfg.get("geos") or ())
                                if str(g or "").strip()
                            ]
                            if whales_geos:
                                meta_cursor = 0
                                if meta_seen_path.is_file():
                                    try:
                                        seen_obj = json.loads(
                                            meta_seen_path.read_text(encoding="utf-8")
                                        )
                                        if isinstance(seen_obj, dict):
                                            meta_cursor = max(
                                                0, int(seen_obj.get("cursor") or 0)
                                            )
                                    except (OSError, json.JSONDecodeError, ValueError, TypeError):
                                        meta_cursor = 0
                                sliced_geos = [
                                    whales_geos[(meta_cursor * 3 + i) % len(whales_geos)]
                                    for i in range(min(3, len(whales_geos)))
                                ]
                                meta_cmd.extend(["--country", ",".join(sliced_geos)])
                            meta_cmd.extend(["--market-profile", market_profile])
                            wave_ran_sources.add("meta_ads")
                            web_tasks.append((
                                "meta_ad_leads",
                                lambda c=meta_cmd: _exec_stage("meta_ad_leads", c),
                            ))
                        else:
                            _add_reason("missing_treg_cli")

                    if sources_cfg.get("social_x_reddit", True) and "social_x_reddit" not in quarantined:
                        if not paid_cli_discovery_allowed:
                            _add_reason("budget_guard_skipped:treg_social_leads")
                        elif treg_ready:
                            wave_ran_sources.add("social_x_reddit")
                            _soc_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "treg_social_leads.py"),
                                "--output",
                                str(social_out),
                                "--seen",
                                str(state_dir / "treg_social_seen.json"),
                                "--market-profile",
                                market_profile,
                            ]
                            web_tasks.append((
                                "treg_social_leads",
                                lambda c=_soc_cmd: _exec_stage("treg_social_leads", c),
                            ))
                        elif is_real_subprocess_runner:
                            _add_reason("missing_treg_cli")

                    if sources_cfg.get("follower_graph", False) and "follower_graph" not in quarantined:
                        if not paid_cli_discovery_allowed:
                            _add_reason("budget_guard_skipped:follower_graph")
                        elif treg_ready or not is_real_subprocess_runner:
                            fg_cfg = eff_cfg.get("follower_graph") if isinstance(eff_cfg.get("follower_graph"), Mapping) else {}
                            fg_budget = float(fg_cfg.get("daily_budget_usd") or 4.0)
                            fg_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "follower_graph_leads.py"),
                                "--output",
                                str(follower_graph_out),
                                "--state",
                                str(state_dir / "follower_graph_state.json"),
                                "--summary-out",
                                str(state_dir / "follower_graph_last_summary.json"),
                                "--db",
                                str(db_path),
                                "--market-profile",
                                market_profile,
                                "--platforms",
                                ",".join(fg_cfg.get("platforms") or ["x", "instagram"]),
                                "--budget-usd",
                                str(fg_budget),
                                "--ofm-share-pct",
                                str(int(fg_cfg.get("ofm_share_pct") or 20)),
                                "--max-followers-per-seed",
                                str(int(fg_cfg.get("followers_per_seed_cycle") or 400)),
                                "--max-followers-per-seed-total",
                                str(int(fg_cfg.get("max_followers_per_seed_total") or 2500)),
                                "--max-degree",
                                str(int(fg_cfg.get("max_degree") or 2)),
                                "--fit-threshold",
                                str(float(fg_cfg.get("fit_threshold") or 1.5)),
                                "--cpl-stop-usd",
                                str(float(fg_cfg.get("cpl_stop_usd") or 0.50)),
                            ]
                            wave_ran_sources.add("follower_graph")
                            web_tasks.append((
                                "follower_graph",
                                lambda c=fg_cmd: _exec_stage("follower_graph", c),
                            ))
                        else:
                            _add_reason("missing_treg_cli")

                    if (
                        is_real_subprocess_runner
                        and not treg_ready
                        and (
                            (sources_cfg.get("meta_ads", True) and "meta_ads" not in quarantined)
                            or (sources_cfg.get("social_x_reddit", True) and "social_x_reddit" not in quarantined)
                            or (sources_cfg.get("facebook_groups", False) and "facebook_groups" not in quarantined)
                            or (enabled_veins and "high_yield_veins" not in quarantined)
                        )
                    ):
                        _add_reason("missing_treg_cli")

                    if (
                        sources_cfg.get("yc_ph", True)
                        and "yc_ph" not in quarantined
                        and "yc_ph_leads" not in quarantined
                    ):
                        yc_lim = max(1, min(200, int(limits_cfg.get("yc_limit") or 80)))
                        raw_ph_lim = limits_cfg.get("ph_limit")
                        ph_lim = max(0, min(50, int(0 if raw_ph_lim is None else raw_ph_lim)))
                        yc_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "yc_ph_leads.py"),
                            "--yc-limit",
                            str(yc_lim),
                            "--ph-limit",
                            str(ph_lim),
                            "--betalist-limit",
                            "5",
                            "--microlaunch-limit",
                            "5",
                            "--uneed-limit",
                            "5",
                            "--indiehackers-limit",
                            "5",
                            "--no-llm",
                            "--no-sheet",
                            "--persist-seen",
                        ]
                        if primary_ro_session is None or len(web_tasks) > 0:
                            yc_cmd.append("--no-tg")
                        wave_ran_sources.add("yc_ph")

                        def _run_yc_ph_stage(cmd_arg: list[str] = yc_cmd, out_path: Path = yc_ph_out) -> None:
                            tmp_yc = Path("/tmp/yc_ph_leads.jsonl")
                            prev_yc_mtime = tmp_yc.stat().st_mtime_ns if tmp_yc.is_file() else -1
                            if _exec_stage("yc_ph_leads", cmd_arg) and tmp_yc.is_file():
                                cur_yc_st = tmp_yc.stat()
                                if cur_yc_st.st_size > 0 and cur_yc_st.st_mtime_ns != prev_yc_mtime:
                                    out_path.write_bytes(tmp_yc.read_bytes())

                        web_tasks.append(("yc_ph_leads", _run_yc_ph_stage))

                    raw_ext_lim = limits_cfg.get("external_limit")
                    ext_limit = min(200, max(0, int(40 if raw_ext_lim is None else raw_ext_lim)))
                    if ext_limit > 0 and "creator_external_leads" not in quarantined:
                        wave_ran_sources.add("external")
                        _ext_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "creator_external_leads.py"),
                            "--output",
                            str(external_out),
                            "--limit",
                            str(ext_limit),
                            "--seen",
                            str(state_dir / "creator_external_seen.json"),
                            "--verify-telegram",
                            "--no-qualify",
                        ]
                        web_tasks.append((
                            "creator_external_leads",
                            lambda c=_ext_cmd: _exec_stage("creator_external_leads", c),
                        ))

                    if sources_cfg.get("tma_apps", True) and "tma_apps" not in quarantined:
                        wave_ran_sources.add("tma_apps")
                        tma_lim = min(100, max(10, int(limits_cfg.get("tma_limit") or 40)))
                        _tma_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "tma_apps.py"),
                            "--output",
                            str(tma_apps_out),
                            "--seen",
                            str(state_dir / "tma_apps_seen.json"),
                            "--limit",
                            str(tma_lim),
                        ]
                        tma_paid_allowed = budget_state is None or bool(budget_state.paid_allowed)
                        if not tma_paid_allowed:
                            _tma_cmd.append("--no-paid")
                        web_tasks.append((
                            "tma_apps",
                            lambda c=_tma_cmd: _exec_stage("tma_apps", c),
                        ))

                    if web_tasks:
                        eff_web_workers = (
                            max(1, min(5, int(web_discovery_workers)))
                            if web_discovery_workers is not None
                            else (5 if is_real_subprocess_runner else 1)
                        )
                        if eff_web_workers <= 1 or len(web_tasks) <= 1:
                            for _label, fn in web_tasks:
                                fn()
                        else:
                            pool = concurrent.futures.ThreadPoolExecutor(
                                max_workers=min(eff_web_workers, len(web_tasks))
                            )
                            try:
                                futs = [pool.submit(fn) for _label, fn in web_tasks]
                                for fut in concurrent.futures.as_completed(futs):
                                    fut.result()
                            finally:
                                pool.shutdown(wait=False, cancel_futures=True)

                    # Native TG scrapers (RO pool, non-overlapping SQLite session locking)
                    effective_groups: list[str] = []
                    for g in [*(b2b_cfg.get("tg_groups_new") or []), *(b2b_cfg.get("tg_groups_old") or [])]:
                        if g not in effective_groups:
                            effective_groups.append(g)

                    if (
                        primary_ro_session is not None
                        and sources_cfg.get("tg_groups", True)
                        and effective_groups
                    ):
                        scrape_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_scrape_posters.py"),
                            "--session",
                            str(primary_ro_session),
                            "--groups",
                            ",".join(effective_groups),
                            "--mode",
                            "hybrid",
                            "--msg-limit",
                            str(min(400, max(50, int(limits_cfg.get("msg_limit") or 400)))),
                            "--delay",
                            "0.8",
                            "--max-runtime-sec",
                            "180",
                            "--output",
                            str(raw_jsonl),
                            "--append",
                        ]
                        if master_jsonl.is_file():
                            scrape_cmd.extend(["--exclude", str(master_jsonl)])
                        _exec_stage("tg_scrape_posters", scrape_cmd)

                        comment_excluded = set(quarantined) | {str(g).strip().lstrip("@").lower() for g in effective_groups}
                        comment_batch = _pick_comment_channels_batch(
                            state_dir,
                            excluded=comment_excluded,
                            batch_size=10,
                            include_repo_seed=is_real_subprocess_runner,
                        )
                        if comment_batch:
                            comment_sess = (
                                ro_sessions[1 % len(ro_sessions)]
                                if ro_sessions
                                else primary_ro_session
                            )
                            comment_cmd = [
                                py_bin,
                                str(ROOT / "leadgen" / "tg_scrape_posters.py"),
                                "--session",
                                str(comment_sess),
                                "--groups",
                                ",".join(comment_batch),
                                "--mode",
                                "hybrid",
                                "--msg-limit",
                                str(min(400, max(50, int(limits_cfg.get("msg_limit") or 400)))),
                                "--delay",
                                "0.8",
                                "--max-runtime-sec",
                                "150",
                                "--output",
                                str(raw_jsonl),
                                "--append",
                            ]
                            if master_jsonl.is_file():
                                comment_cmd.extend(["--exclude", str(master_jsonl)])
                            _exec_stage("tg_scrape_comment_channels", comment_cmd)

                        raw_b2b_new = b2b_cfg.get("tg_groups_new")
                        b2b_group_list = _clean_chat_list(
                            ["productcircle_chat", "solofounderschat", "saasfounders"]
                            if raw_b2b_new is None
                            else raw_b2b_new
                        )
                        if b2b_group_list:
                            _exec_stage(
                                "b2b_tg_groups",
                                [
                                    py_bin,
                                    str(ROOT / "leadgen" / "ofm_and_groups.py"),
                                    "--mode",
                                    "b2b_tg",
                                    "--session",
                                    str(primary_ro_session),
                                    "--groups",
                                    ",".join(b2b_group_list),
                                    "--output",
                                    str(b2b_tg_groups_out),
                                    "--seen",
                                    str(state_dir / "b2b_tg_groups_seen.json"),
                                ],
                            )

                    if (
                        primary_ro_session is not None
                        and sources_cfg.get("ofm_groups", False)
                        and "ofm_groups" not in quarantined
                    ):
                        ofm_group_list = _clean_chat_list(b2b_cfg.get("ofm_tg_groups") or [])
                        ofm_cmd = [
                            py_bin,
                            str(ROOT / "leadgen" / "ofm_and_groups.py"),
                            "--mode",
                            "ofm_tg",
                            "--session",
                            str(primary_ro_session),
                            "--ofm-out",
                            str(ofm_review_path),
                            "--seen",
                            str(state_dir / "ofm_seen.json"),
                        ]
                        if ofm_group_list:
                            ofm_cmd.extend(["--groups", ",".join(ofm_group_list)])
                        _exec_stage("ofm_groups", ofm_cmd)

                    if (
                        primary_ro_session is not None
                        and sources_cfg.get("channel_graph", True)
                        and "channel_graph" not in quarantined
                    ):
                        graph_sess = ro_sessions[1 % len(ro_sessions)] if ro_sessions else primary_ro_session
                        _exec_stage(
                            "tg_channel_graph",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "tg_channel_graph.py"),
                                "--session",
                                str(graph_sess),
                                "--state-dir",
                                str(state_dir),
                                "--output",
                                str(channel_graph_out),
                                "--state",
                                str(state_dir / "tg_channel_graph_seen.json"),
                                "--max-runtime-sec",
                                str(min(180, max(60, int(stage_timeout) - 60))),
                            ],
                        )

                    if (
                        primary_ro_session is not None
                        and sources_cfg.get("tg_ad_spy", True)
                        and "tg_ad_spy" not in quarantined
                    ):
                        ad_sess = ro_sessions[2 % len(ro_sessions)] if ro_sessions else primary_ro_session
                        _exec_stage(
                            "tg_ad_spy",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "tg_ad_spy.py"),
                                "--session",
                                str(ad_sess),
                                "--state-dir",
                                str(state_dir),
                                "--output",
                                str(ad_spy_out),
                                "--seen",
                                str(state_dir / "tg_ad_spy_seen.json"),
                                "--max-runtime-sec",
                                str(min(180, max(60, int(stage_timeout) - 60))),
                            ],
                        )

                    wave_tg_groups_cnt = _count_jsonl_lines(raw_jsonl) + _count_jsonl_lines(b2b_tg_groups_out)
                    per_source_counts["tg_groups"] = per_source_counts.get("tg_groups", 0) + wave_tg_groups_cnt
                    wave_ofm_cnt = max(0, _count_jsonl_lines(ofm_review_path) - pre_ofm_count)
                    per_source_counts["ofm_groups"] = per_source_counts.get("ofm_groups", 0) + wave_ofm_cnt
                    wave_source_counts: dict[str, int] = {}
                    for src_label, src_path in (
                        ("buyer_public", buyer_public_out),
                        ("buyer_catalogs", catalogs_out),
                        ("high_yield_veins", high_yield_out),
                        ("facebook_groups", fb_groups_out),
                        ("meta_ads", meta_ads_out),
                        ("social_x_reddit", social_out),
                        ("external", external_out),
                        ("yc_ph", yc_ph_out),
                        ("tma_apps", tma_apps_out),
                        ("channel_graph", channel_graph_out),
                        ("tg_ad_spy", ad_spy_out),
                        ("follower_graph", follower_graph_out),
                    ):
                        cnt_now = _count_jsonl_lines(src_path)
                        wave_source_counts[src_label] = cnt_now
                        per_source_counts[src_label] = per_source_counts.get(src_label, 0) + cnt_now
                        if src_path.is_file() and src_path.stat().st_size > 0:
                            try:
                                source_hashes[src_label] = _file_sha256(src_path)
                            except OSError:
                                pass

                    for cd_src in COOLDOWN_ELIGIBLE_SOURCES & wave_ran_sources:
                        if _record_source_yield(yield_state, cd_src, wave_source_counts.get(cd_src, 0)):
                            _add_reason(f"cooldown_48h_triggered:{cd_src}")
                            if cd_src not in cooldown_sources:
                                cooldown_sources.append(cd_src)
                    _save_source_yield_state(state_dir, yield_state)

                    pending_qualify_queue = state_dir / "pending_qualify_queue.jsonl"
                    llm_failed_file = state_dir / "llm_failed_batches.jsonl"
                    _merge_discovered_jsonl_into_raw(
                        [
                            pending_qualify_queue,
                            buyer_public_out,
                            catalogs_out,
                            high_yield_out,
                            fb_groups_out,
                            b2b_tg_groups_out,
                            meta_ads_out,
                            social_out,
                            external_out,
                            yc_ph_out,
                            tma_apps_out,
                            channel_graph_out,
                            ad_spy_out,
                        ],
                        raw_jsonl,
                    )
                    apply_master_merge(str(raw_jsonl), str(master_jsonl), commit_master=False)
                    new_jsonl = Path(f"{raw_jsonl}.new")
                    if not new_jsonl.exists():
                        new_jsonl.write_text("", encoding="utf-8")

                    has_new_rows = new_jsonl.stat().st_size > 0
                    wave_new_count = _count_jsonl_lines(new_jsonl)
                    enrich_ok = False
                    qual_ok = False
                    curate_ok = True
                    wave_dx_routed = 0
                    wave_ms_ingested = 0
                    wave_stamp_rows: list[dict[str, Any]] = []
                    wave_deferred_rows: list[dict[str, Any]] = []
                    has_fg_rows = follower_graph_out.is_file() and follower_graph_out.stat().st_size > 0
                    if has_new_rows or has_fg_rows or not is_real_subprocess_runner:
                        enriched_jsonl = state_dir / f"raw_{wave_ts}.enriched.jsonl"
                        if has_new_rows or not is_real_subprocess_runner:
                            enrich_ok = _exec_stage(
                                "tg_enrich_leads",
                                [
                                    py_bin,
                                    str(ROOT / "leadgen" / "tg_enrich_leads.py"),
                                    "--input",
                                    str(new_jsonl),
                                    "--output",
                                    str(enriched_jsonl),
                                    "--fetch",
                                    "--skip-copy",
                                ],
                            )
                        else:
                            enrich_ok = True
                        if not enrich_ok:
                            if has_new_rows:
                                _write_pending_qualify_queue(
                                    pending_qualify_queue,
                                    _read_jsonl_dicts(new_jsonl),
                                )
                        else:
                            enrich_src = (
                                enriched_jsonl
                                if enriched_jsonl.is_file() and enriched_jsonl.stat().st_size > 0
                                else new_jsonl
                            )
                            if has_llm_key:
                                llm_failed_file.unlink(missing_ok=True)
                                qualified_jsonl = state_dir / f"raw_{wave_ts}.qualified.jsonl"
                                curated_jsonl = state_dir / f"raw_{wave_ts}.curated_tmp.jsonl"
                                final_jsonl = state_dir / f"raw_{wave_ts}.final.jsonl"
                                if has_new_rows or not is_real_subprocess_runner:
                                    qual_ok = _exec_stage(
                                        "tg_llm_qualify",
                                        [
                                            py_bin,
                                            str(ROOT / "leadgen" / "tg_llm_qualify.py"),
                                            "--input",
                                            str(enrich_src),
                                            "--output",
                                            str(qualified_jsonl),
                                        ],
                                    )
                                else:
                                    qual_ok = True
                                # Follower API rows are already Jev-qualified; append directly after tg_llm_qualify
                                if has_fg_rows:
                                    fg_rows = _read_jsonl_dicts(follower_graph_out)
                                    if fg_rows:
                                        existing_q = qualified_jsonl.read_text(encoding="utf-8") if qualified_jsonl.is_file() else ""
                                        with qualified_jsonl.open("w", encoding="utf-8") as qfh:
                                            if existing_q:
                                                qfh.write(existing_q)
                                                if not existing_q.endswith("\n"):
                                                    qfh.write("\n")
                                            for fg_r in fg_rows:
                                                qfh.write(json.dumps(fg_r, ensure_ascii=False) + "\n")
                                        qual_ok = True
                                # Fail-Closed: if tg_llm_qualify failed or produced 0 qualified leads,
                                # NEVER fall back to enrich_src — 0 leads advance to tg_curate / CRM!
                                if (
                                    qual_ok
                                    and qualified_jsonl.is_file()
                                    and qualified_jsonl.stat().st_size > 0
                                ):
                                    curate_ok = _exec_stage(
                                        "tg_curate",
                                        [
                                            py_bin,
                                            str(ROOT / "leadgen" / "tg_curate.py"),
                                            "--input",
                                            str(qualified_jsonl),
                                            "--output",
                                            str(curated_jsonl),
                                        ],
                                    )
                                    if curate_ok:
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            v_rows_pre = _read_jsonl_dicts(curated_jsonl)
                                            if v_rows_pre:
                                                live_sess_pre = (
                                                    primary_ro_session
                                                    if (
                                                        is_real_subprocess_runner
                                                        or tg_client is not None
                                                        or tg_entity_resolver_fn is not None
                                                    )
                                                    else None
                                                )
                                                v_rows_pre, v_st_pre = verify_personal_tg_handles_live(
                                                    v_rows_pre,
                                                    client=tg_client,
                                                    scraper_session=live_sess_pre,
                                                    scraper_sessions=ro_sessions if (is_real_subprocess_runner and tg_client is None and tg_entity_resolver_fn is None) else None,
                                                    entity_resolver_fn=tg_entity_resolver_fn,
                                                    use_web_first=is_real_subprocess_runner,
                                                    tme_cache_path=state_dir / "tme_probe_cache.json",
                                                    pending_verify_queue_path=state_dir / "pending_tg_verify_queue.jsonl",
                                                )
                                                for k, val in v_st_pre.items():
                                                    tg_verify_stats[k] = tg_verify_stats.get(k, 0) + int(val)
                                                wave_deferred_rows.extend(
                                                    r for r in v_rows_pre if r.get("tg_verify_deferred")
                                                )
                                                curated_jsonl.write_text(
                                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in v_rows_pre)
                                                    + ("\n" if v_rows_pre else ""),
                                                    encoding="utf-8",
                                                )
                                        # Pre-Waterfall CRM DB Dedup (0 paid spend on already ingested companies/handles)
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            dd_rows = _read_jsonl_dicts(curated_jsonl)
                                            if dd_rows:
                                                kept_dd, dropped_dd = _filter_existing_crm_leads(conn, dd_rows)
                                                pre_waterfall_deduped += dropped_dd
                                                if dropped_dd:
                                                    curated_jsonl.write_text(
                                                        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept_dd)
                                                        + ("\n" if kept_dd else ""),
                                                        encoding="utf-8",
                                                    )
                                        pre_wf_tg_cnt = (
                                            _count_personal_tg_rows(curated_jsonl)
                                            if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0
                                            else 0
                                        )
                                        # Paid Tier 1.5 treg_enrich (only when Budget Guard allows paid calls)
                                        if (
                                            treg_ready
                                            and (budget_state is None or budget_state.paid_allowed)
                                            and curated_jsonl.is_file()
                                            and curated_jsonl.stat().st_size > 0
                                        ):
                                            treg_tmp_jsonl = state_dir / f"raw_{wave_ts}.treg_tmp.jsonl"
                                            treg_ok = _exec_stage(
                                                "treg_enrich",
                                                [
                                                    py_bin,
                                                    str(ROOT / "leadgen" / "treg_enrich.py"),
                                                    "--input",
                                                    str(curated_jsonl),
                                                    "--output",
                                                    str(treg_tmp_jsonl),
                                                    "--cache",
                                                    str(state_dir / "treg_market.json"),
                                                ],
                                            )
                                            if (
                                                treg_ok
                                                and treg_tmp_jsonl.is_file()
                                                and treg_tmp_jsonl.stat().st_size > 0
                                            ):
                                                treg_tmp_jsonl.replace(curated_jsonl)
                                            elif treg_tmp_jsonl.is_file():
                                                treg_tmp_jsonl.unlink(missing_ok=True)
                                        # Tier 1 + Budget-guarded Paid Tier 2 contact_waterfall
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            use_inprocess_wf = is_real_subprocess_runner or any(
                                                fn is not None
                                                for fn in (
                                                    waterfall_enrich_fn,
                                                    waterfall_http_get_fn,
                                                    waterfall_treg_fn,
                                                    waterfall_monid_fn,
                                                    budget_treg_balance_fn,
                                                    budget_monid_balance_fn,
                                                )
                                            )
                                            if use_inprocess_wf:
                                                with stage_lock:
                                                    stages_run.append("contact_waterfall")
                                                wf_input_rows = _read_jsonl_dicts(curated_jsonl)
                                                wf_out_rows, budget_state = run_contact_waterfall_with_budget(
                                                    conn,
                                                    wf_input_rows,
                                                    cfg=eff_cfg,
                                                    budget_state=budget_state,
                                                    enrich_fn=waterfall_enrich_fn,
                                                    http_get_fn=waterfall_http_get_fn,
                                                    treg_fn=waterfall_treg_fn,
                                                    monid_fn=waterfall_monid_fn,
                                                )
                                                curated_jsonl.write_text(
                                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in wf_out_rows)
                                                    + ("\n" if wf_out_rows else ""),
                                                    encoding="utf-8",
                                                )
                                            else:
                                                wf_tmp_jsonl = state_dir / f"raw_{wave_ts}.waterfall_tmp.jsonl"
                                                wf_cmd = [
                                                    py_bin,
                                                    str(ROOT / "leadgen" / "contact_waterfall.py"),
                                                    "--input",
                                                    str(curated_jsonl),
                                                    "--output",
                                                    str(wf_tmp_jsonl),
                                                    "--max-budget-usd",
                                                    "0"
                                                    if (budget_state is not None and not budget_state.paid_allowed)
                                                    else "0.08",
                                                ]
                                                if budget_state is not None and not budget_state.paid_allowed:
                                                    wf_cmd.append("--no-paid-tier2")
                                                wf_ok = _exec_stage("contact_waterfall", wf_cmd)
                                                if (
                                                    wf_ok
                                                    and wf_tmp_jsonl.is_file()
                                                    and wf_tmp_jsonl.stat().st_size > 0
                                                ):
                                                    wf_tmp_jsonl.replace(curated_jsonl)
                                                elif wf_tmp_jsonl.is_file():
                                                    wf_tmp_jsonl.unlink(missing_ok=True)
                                        if (
                                            (treg_ready or monid_ready or not is_real_subprocess_runner)
                                            and (budget_state is None or budget_state.paid_allowed or not is_real_subprocess_runner)
                                            and sources_cfg.get("tg_identity_bridge", True)
                                            and "tg_identity_bridge" not in quarantined
                                            and curated_jsonl.is_file()
                                            and curated_jsonl.stat().st_size > 0
                                        ):
                                            bridge_tmp_jsonl = state_dir / f"raw_{wave_ts}.bridge_tmp.jsonl"
                                            bridge_ok = _exec_stage(
                                                "tg_identity_bridge",
                                                [
                                                    py_bin,
                                                    str(ROOT / "leadgen" / "tg_identity_bridge.py"),
                                                    "--input",
                                                    str(curated_jsonl),
                                                    "--output",
                                                    str(bridge_tmp_jsonl),
                                                ],
                                            )
                                            if (
                                                bridge_ok
                                                and bridge_tmp_jsonl.is_file()
                                                and bridge_tmp_jsonl.stat().st_size > 0
                                            ):
                                                bridge_tmp_jsonl.replace(curated_jsonl)
                                            elif bridge_tmp_jsonl.is_file():
                                                bridge_tmp_jsonl.unlink(missing_ok=True)
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            v_rows_post = _read_jsonl_dicts(curated_jsonl)
                                            if v_rows_post:
                                                live_sess_post = (
                                                    primary_ro_session
                                                    if (
                                                        is_real_subprocess_runner
                                                        or tg_client is not None
                                                        or tg_entity_resolver_fn is not None
                                                    )
                                                    else None
                                                )
                                                v_rows_post, v_st_post = verify_personal_tg_handles_live(
                                                    v_rows_post,
                                                    client=tg_client,
                                                    scraper_session=live_sess_post,
                                                    scraper_sessions=ro_sessions if (is_real_subprocess_runner and tg_client is None and tg_entity_resolver_fn is None) else None,
                                                    entity_resolver_fn=tg_entity_resolver_fn,
                                                    use_web_first=is_real_subprocess_runner,
                                                    tme_cache_path=state_dir / "tme_probe_cache.json",
                                                    pending_verify_queue_path=state_dir / "pending_tg_verify_queue.jsonl",
                                                )
                                                for k, val in v_st_post.items():
                                                    tg_verify_stats[k] = tg_verify_stats.get(k, 0) + int(val)
                                                wave_deferred_rows.extend(
                                                    r for r in v_rows_post if r.get("tg_verify_deferred")
                                                )
                                                curated_jsonl.write_text(
                                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in v_rows_post)
                                                    + ("\n" if v_rows_post else ""),
                                                    encoding="utf-8",
                                                )
                                        # Layer 0 ($0): TG Group Admins AFTER waterfall/bridge/verify (handles moved to telegram_group!)
                                        if (
                                            whales_cfg.get("tg_group_admins_enabled", True)
                                            and curated_jsonl.is_file()
                                            and curated_jsonl.stat().st_size > 0
                                            and (
                                                is_real_subprocess_runner
                                                or tg_client is not None
                                                or tg_admin_extractor_fn is not None
                                            )
                                        ):
                                            cur_rows = _read_jsonl_dicts(curated_jsonl)
                                            if cur_rows:
                                                cur_rows, w_admin_stats = enrich_whales_with_tg_group_admins(
                                                    cur_rows,
                                                    client=tg_client,
                                                    scraper_session=primary_ro_session,
                                                    scraper_sessions=ro_sessions if (is_real_subprocess_runner and tg_client is None and tg_admin_extractor_fn is None) else None,
                                                    admin_extractor_fn=tg_admin_extractor_fn,
                                                )
                                                for k, val in w_admin_stats.items():
                                                    tg_admin_stats[k] = tg_admin_stats.get(k, 0) + int(val)
                                                curated_jsonl.write_text(
                                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in cur_rows)
                                                    + ("\n" if cur_rows else ""),
                                                    encoding="utf-8",
                                                )
                                        # Layer 2 ($0): Phone -> WA + dedicated TG checker + Manual Social Queue routing
                                        run_l2_phone_resolver = is_real_subprocess_runner or any(
                                            x is not None
                                            for x in (
                                                wa_gateway,
                                                tg_checker_client,
                                                scraper_sessions_dir,
                                                waterfall_enrich_fn,
                                                budget_treg_balance_fn,
                                                budget_monid_balance_fn,
                                            )
                                        )
                                        manual_social_jsonl = state_dir / f"manual_social_{wave_ts}.jsonl"
                                        if (
                                            run_l2_phone_resolver
                                            and curated_jsonl.is_file()
                                            and curated_jsonl.stat().st_size > 0
                                        ):
                                            l2_rows = _read_jsonl_dicts(curated_jsonl)
                                            if l2_rows:
                                                routed_l2, w_phone_stats = resolve_and_route_phone_leads(
                                                    conn,
                                                    l2_rows,
                                                    cfg=eff_cfg,
                                                    wa_gateway=wa_gateway,
                                                    tg_checker_client=tg_checker_client,
                                                    scraper_sessions_dir=scraper_sessions_dir,
                                                )
                                                for k, val in w_phone_stats.items():
                                                    phone_res_stats[k] = phone_res_stats.get(k, 0) + int(val)
                                                curated_jsonl.write_text(
                                                    "\n".join(json.dumps(r, ensure_ascii=False) for r in routed_l2)
                                                    + ("\n" if routed_l2 else ""),
                                                    encoding="utf-8",
                                                )
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            post_tg_cnt = _count_personal_tg_rows(curated_jsonl)
                                            curated_snapshot = _read_jsonl_dicts(curated_jsonl)
                                            bridged_tag_cnt = sum(
                                                1
                                                for r in curated_snapshot
                                                if str(r.get("tg_bridge_match") or "").strip()
                                            )
                                            tg_bridged_count += max(
                                                0, post_tg_cnt - pre_wf_tg_cnt, bridged_tag_cnt
                                            )
                                            split_res = split_tg_and_direct_x_leads(
                                                curated_jsonl,
                                                state_dir / "direct_x_leads.jsonl",
                                                manual_social_jsonl=(
                                                    manual_social_jsonl if run_l2_phone_resolver else None
                                                ),
                                                reverse_bridge_tg_handle=str(
                                                    b2b_cfg.get("reverse_bridge_tg_handle") or ""
                                                ).strip(),
                                            )
                                            wave_dx_routed = int(split_res.get("direct_x_added") or 0)
                                            direct_x_routed += wave_dx_routed
                                            if manual_social_jsonl.is_file() and manual_social_jsonl.stat().st_size > 0:
                                                ms_rows = _read_jsonl_dicts(manual_social_jsonl)
                                                ms_ingest = ingest_v2_routed_leads(
                                                    conn,
                                                    ms_rows,
                                                    cfg=eff_cfg,
                                                    auto_push=bool(eff_cfg.get("auto_push_to_crm", True)),
                                                )
                                                wave_ms_ingested = int(ms_ingest.get("social_manual_ingested") or 0)
                                                social_manual_ingested += wave_ms_ingested
                                                manual_social_jsonl.unlink(missing_ok=True)
                                            wave_stamp_rows = _read_jsonl_dicts(curated_jsonl)
                                            curated_v2_stamp_rows.extend(wave_stamp_rows)
                                        if curated_jsonl.is_file() and curated_jsonl.stat().st_size > 0:
                                            curated_jsonl.replace(final_jsonl)
                                        else:
                                            curated_jsonl.unlink(missing_ok=True)
                                            if final_jsonl.is_file():
                                                final_jsonl.unlink(missing_ok=True)
                                    else:
                                        curated_jsonl.unlink(missing_ok=True)
                                        if final_jsonl.is_file():
                                            final_jsonl.unlink(missing_ok=True)
                                elif final_jsonl.is_file():
                                    final_jsonl.unlink(missing_ok=True)

                            pipeline_ok = has_llm_key and enrich_ok and qual_ok and curate_ok
                            if pipeline_ok:
                                failed_rows = _read_jsonl_dicts(llm_failed_file) if has_llm_key else []
                                llm_failed_file.unlink(missing_ok=True)
                                exclude_key_set: set[str] = set()
                                for fr in failed_rows:
                                    exclude_key_set.update(_lead_retry_keys(fr))
                                for dr in wave_deferred_rows:
                                    exclude_key_set.update(_lead_retry_keys(dr))
                                if exclude_key_set and raw_jsonl.is_file():
                                    raw_commit_tmp = state_dir / f"raw_{wave_ts}.commit_tmp.jsonl"
                                    kept_raw = [
                                        r
                                        for r in _read_jsonl_dicts(raw_jsonl)
                                        if not (_lead_retry_keys(r) & exclude_key_set)
                                    ]
                                    raw_commit_tmp.write_text(
                                        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept_raw)
                                        + ("\n" if kept_raw else ""),
                                        encoding="utf-8",
                                    )
                                    try:
                                        apply_master_merge(
                                            str(raw_commit_tmp), str(master_jsonl), commit_master=True
                                        )
                                    finally:
                                        raw_commit_tmp.unlink(missing_ok=True)
                                        Path(f"{raw_commit_tmp}.new").unlink(missing_ok=True)
                                    if failed_rows:
                                        _write_pending_qualify_queue(pending_qualify_queue, failed_rows)
                                    else:
                                        pending_qualify_queue.unlink(missing_ok=True)
                                else:
                                    if raw_jsonl.is_file():
                                        apply_master_merge(
                                            str(raw_jsonl), str(master_jsonl), commit_master=True
                                        )
                                    pending_qualify_queue.unlink(missing_ok=True)
                            elif has_new_rows:
                                _write_pending_qualify_queue(
                                    pending_qualify_queue,
                                    _read_jsonl_dicts(new_jsonl),
                                )
                    else:
                        pending_qualify_queue.unlink(missing_ok=True)

                    wave_push = push_discovered_now(conn)
                    wave_ingested_now = int(wave_push.get("ingested") or 0)
                    ingested_total += wave_ingested_now
                    if wave_stamp_rows:
                        _stamp_new_lead_v2_fields(conn, wave_stamp_rows)
                    b2b_camp = _find_campaign_by_name(conn, b2b_name) or _resolve_active_b2b_camp_fallback(conn)
                    pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
                    still_needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
                    if not still_needs_b2b:
                        break
                    if (
                        wave_new_count == 0
                        and wave_ingested_now == 0
                        and wave_dx_routed == 0
                        and wave_ms_ingested == 0
                    ):
                        break
                    if (time.monotonic() - run_started_mono) >= float(max_wall_clock_sec):
                        _add_reason("batch_loop_wall_clock_cap")
                        break

            # Wave 2 Vacancy pipeline (TG job_monitor + hh.ru + Profi/Kwork/Upwork + LinkedIn Jobs)
            if still_needs_vac:
                wave2_rows, wave2_stats = run_wave2_vacancies_pipeline(
                    conn,
                    cfg=eff_cfg,
                    state_dir=state_dir,
                    isolated_session=isolated_session,
                    has_llm_key=has_llm_key,
                    budget_state=budget_state,
                    runner=active_runner,
                    exec_stage_fn=_exec_stage,
                    py_bin=py_bin,
                    today_str=today_str,
                    sub_env=sub_env,
                    quarantined=quarantined,
                    treg_ready=treg_ready,
                    stage_errors=stage_errors,
                    replenish_reasons=replenish_reasons,
                    hh_collector_fn=hh_collector_fn,
                    freelance_monitor_fn=freelance_monitor_fn,
                    linkedin_collector_fn=linkedin_collector_fn,
                    http_get_fn=waterfall_http_get_fn,
                    waterfall_enrich_fn=waterfall_enrich_fn,
                    wa_gateway=wa_gateway,
                    tg_checker_client=tg_checker_client,
                    scraper_sessions_dir=scraper_sessions_dir,
                    is_real_subprocess=is_real_subprocess_runner,
                )
                if wave2_rows:
                    w2_ingest = ingest_v2_routed_leads(
                        conn,
                        wave2_rows,
                        cfg=eff_cfg,
                        auto_push=bool(eff_cfg.get("auto_push_to_crm", True)),
                    )
                    ingested_total += int(w2_ingest.get("vacancy_ingested") or 0)
                    social_manual_ingested += int(w2_ingest.get("social_manual_ingested") or 0)
                    wave_extra_campaigns.update(w2_ingest.get("campaigns") or {})

            # Wave 3 RU Experts pipeline (isolated from Airtable via exclude_from_airtable=1)
            if still_needs_ru:
                wave3_rows, wave3_stats = run_wave3_ru_experts_pipeline(
                    conn,
                    cfg=eff_cfg,
                    budget_state=budget_state,
                    tg_client=tg_client,
                    scraper_session=isolated_session,
                    ru_experts_fn=ru_experts_fn,
                    treg_fn=waterfall_treg_fn,
                    monid_fn=waterfall_monid_fn,
                    http_get_fn=waterfall_http_get_fn,
                )
                if wave3_rows:
                    w3_ingest = ingest_v2_routed_leads(
                        conn,
                        wave3_rows,
                        cfg=eff_cfg,
                        auto_push=bool(eff_cfg.get("auto_push_to_crm", True)),
                    )
                    ingested_total += int(w3_ingest.get("ru_experts_ingested") or 0)
                    wave_extra_campaigns.update(w3_ingest.get("campaigns") or {})

            push_2 = push_discovered_now(conn)
            ingested_total += int(push_2.get("ingested") or 0)
            if curated_v2_stamp_rows:
                _stamp_new_lead_v2_fields(conn, curated_v2_stamp_rows)

            b2b_camp = _find_campaign_by_name(conn, b2b_name) or _resolve_active_b2b_camp_fallback(conn)
            vac_camp = _find_campaign_by_name(conn, vac_name)
            ru_camp = _find_campaign_by_name(conn, ru_name) if ru_active else ru_camp
            wa_name = str(b2b_cfg.get("target_campaign_wa") or "Leadgen Auto (WA)").strip() or "Leadgen Auto (WA)"
            wa_camp = _find_campaign_by_name(conn, wa_name)
            ofm_camp = None
            if b2b_cfg.get("ofm_outreach_enabled", False):
                ofm_name = str(b2b_cfg.get("target_campaign_ofm") or "SoftUGC — OFM (TG)").strip() or "SoftUGC — OFM (TG)"
                ofm_camp = _find_campaign_by_name(conn, ofm_name)

        # Step 3: Rule 5 Dry-Run quarantine rollback BEFORE finalizing replenish state
        gained_camp_ids: set[int] = {
            int(r["campaign_id"])
            for r in conn.execute("SELECT id, campaign_id FROM campaign_recipients").fetchall()
            if int(r["id"]) not in pre_existing_recipient_ids and r["campaign_id"] is not None
        }
        dynamic_rule5_camps: list[dict[str, Any]] = []
        for g_cid in sorted(gained_camp_ids):
            crow = conn.execute("SELECT * FROM campaigns WHERE id = ?", (g_cid,)).fetchone()
            if crow is not None:
                dynamic_rule5_camps.append(as_dict(crow))
        for rrow in conn.execute(
            "SELECT * FROM campaigns WHERE lower(COALESCE(status, '')) = 'running' AND leadgen_enabled = 1 ORDER BY id ASC"
        ).fetchall():
            dynamic_rule5_camps.append(as_dict(rrow))

        seen_camp_ids: set[int] = set()
        rule5_targets: list[dict[str, Any]] = []
        for camp_obj in (
            b2b_camp,
            vac_camp,
            wa_camp,
            ofm_camp,
            ru_camp,
            *wave_extra_campaigns.values(),
            *dynamic_rule5_camps,
        ):
            if camp_obj is None:
                continue
            if str(camp_obj.get("status") or "").strip().lower() == "completed":
                continue
            cid_k = int(camp_obj["id"])
            if cid_k not in seen_camp_ids:
                seen_camp_ids.add(cid_k)
                rule5_targets.append(camp_obj)
        for camp_obj in rule5_targets:
            preview_obj, q_ids = _enforce_rule5_quarantine_on_new_recipients(
                conn,
                campaign=camp_obj,
                pre_existing_ids=pre_existing_recipient_ids,
                stage_errors=stage_errors,
            )
            if q_ids:
                rule5_quarantined[str(camp_obj["name"])] = q_ids
                ingested_total = max(0, ingested_total - len(q_ids))
            if preview_obj is not None:
                rule5_previews[str(camp_obj["name"])] = preview_obj

        pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
        _, _, pending_vac, _ = _collect_vacancy_campaigns_state(
            conn,
            vac_name,
            vac_enabled=bool(vac_cfg.get("enabled", False)),
            vac_low_water_mark=int(vac_low_water_mark),
        )

    whales_ready = ensure_whales_tg_campaign_ready(conn, eff_cfg)
    if whales_ready.get("campaign_id"):
        refreshed_b2b = _find_campaign_by_name(
            conn, str(whales_ready.get("campaign_name") or b2b_name)
        )
        if refreshed_b2b is not None:
            b2b_camp = refreshed_b2b
            pending_b2b = _count_pending(conn, b2b_camp["id"])

    # Persist stage_errors in kv_settings for observability
    now_err_stamp = utc_now()
    conn.execute(
        """
        INSERT INTO kv_settings (key, value, updated_at)
        VALUES ('leadgen_stage_errors', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (json.dumps(stage_errors, ensure_ascii=False), now_err_stamp),
    )
    conn.commit()

    # 4. Early-Warning Alert 24h before queue exhaustion on active running B2B campaign
    low_water_alert: dict[str, Any] | None = None
    if (
        b2b_camp is not None
        and str(b2b_camp.get("status") or "").strip().lower() == "running"
        and pending_b2b < int(critical_low_mark)
    ):
        low_water_alert = _emit_critical_low_water_alert(
            conn,
            campaign=b2b_camp,
            pending_count=pending_b2b,
            critical_low_mark=int(critical_low_mark),
            has_scraper_session=(isolated_session is not None),
            has_llm_key=has_llm_key,
            missing_treg_cli=("missing_treg_cli" in replenish_reasons),
            digest_opener=digest_opener,
        )

    # 5. Update cloud_persistence_dirty_at if leads were added
    if ingested_total > 0:
        now_stamp = utc_now()
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES ('cloud_persistence_dirty_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (now_stamp, now_stamp),
        )
        conn.commit()

    ofm_review_added = int(per_source_counts.get("ofm_groups") or 0)
    st_dir = _resolve_state_dir()
    prune_stats = prune_leadgen_state_jsonl(st_dir)
    run_timestamp = utc_now()
    zero_streak = 0
    static_repeats: list[dict[str, Any]] = []
    budget_summary = (
        {
            "day": budget_state.day,
            "spent_usd": budget_state.spent_usd,
            "daily_cap_usd": budget_state.daily_cap_usd,
            "treg_balance_usd": budget_state.treg_balance_usd,
            "monid_balance_usd": budget_state.monid_balance_usd,
            "paid_allowed": budget_state.paid_allowed,
            "alert_sent": budget_state.alert_sent,
            "stop_reason": budget_state.stop_reason,
        }
        if budget_state is not None
        else None
    )
    if stages_run or ingested_total > 0 or direct_x_routed > 0 or ofm_review_added > 0 or social_manual_ingested > 0:
        zero_streak, static_repeats = _update_run_history_and_detect(
            st_dir,
            {
                "timestamp": run_timestamp,
                "tg_bridged_count": tg_bridged_count,
                "ingested_total": ingested_total,
                "direct_x_routed": direct_x_routed,
                "source_hashes": source_hashes,
                "stage_error_count": len(stage_errors),
            },
        )
    result_payload = {
        "ok": True,
        "timestamp": run_timestamp,
        "ingested_total": ingested_total,
        "direct_x_routed": direct_x_routed,
        "social_manual_ingested": social_manual_ingested,
        "ofm_review_added": ofm_review_added,
        "tg_bridged_count": tg_bridged_count,
        "tg_bridged_zero_streak": zero_streak,
        "per_source_counts": per_source_counts,
        "source_hashes": source_hashes,
        "static_dump_repeats": static_repeats,
        "prune_stats": prune_stats,
        "pending_b2b": pending_b2b,
        "pending_vacancies": pending_vac,
        "quarantined_sources": sorted(quarantined),
        "stages_run": stages_run,
        "stage_errors": stage_errors,
        "reasons": replenish_reasons,
        "rule5_previews": rule5_previews,
        "rule5_quarantined": rule5_quarantined,
        "low_water_alert": low_water_alert,
        "budget_state": budget_summary,
        "tg_group_admins": tg_admin_stats,
        "tg_handle_verification": tg_verify_stats,
        "phone_resolution": phone_res_stats,
        "wave2_vacancies": wave2_stats,
        "wave3_ru_experts": wave3_stats,
        "whales_ready": whales_ready,
        "waves_executed": waves_executed,
        "pre_waterfall_deduped": pre_waterfall_deduped,
        "cooldown_sources": cooldown_sources,
        "ro_sessions_count": len(ro_sessions),
    }
    daily_digest_res: dict[str, Any] | None = None
    should_emit_digest = (
        is_real_subprocess_runner
        or digest_opener is not None
        or int(tg_verify_stats.get("sessions_exhausted") or 0) > 0
        or (
            budget_summary is not None
            and budget_summary.get("treg_balance_usd") is not None
            and float(budget_summary["treg_balance_usd"]) < 2.0
        )
    )
    if should_emit_digest:
        daily_digest_res = emit_leadgen_daily_digest(
            conn,
            summary=result_payload,
            digest_opener=digest_opener,
        )
    result_payload["daily_digest"] = daily_digest_res

    if (
        stages_run
        or ingested_total > 0
        or direct_x_routed > 0
        or ofm_review_added > 0
        or social_manual_ingested > 0
        or wave2_stats.get("hh_count")
        or wave2_stats.get("freelance_count")
        or wave2_stats.get("linkedin_jobs_count")
        or wave3_stats.get("collected")
    ):
        try:
            st_dir.mkdir(parents=True, exist_ok=True)
            summary_tmp = st_dir / "last_run_summary.json.tmp"
            summary_tmp.write_text(
                json.dumps(
                    {
                        "timestamp": result_payload["timestamp"],
                        "ingested_total": ingested_total,
                        "direct_x_routed": direct_x_routed,
                        "social_manual_ingested": social_manual_ingested,
                        "ofm_review_added": ofm_review_added,
                        "tg_bridged_count": tg_bridged_count,
                        "tg_bridged_zero_streak": zero_streak,
                        "per_source_counts": per_source_counts,
                        "source_hashes": source_hashes,
                        "static_dump_repeats": static_repeats,
                        "prune_stats": prune_stats,
                        "pending_b2b": pending_b2b,
                        "pending_vacancies": pending_vac,
                        "stages_run": stages_run,
                        "stage_error_count": len(stage_errors),
                        "stage_errors": stage_errors,
                        "reasons": replenish_reasons,
                        "budget_state": budget_summary,
                        "tg_group_admins": tg_admin_stats,
                        "tg_handle_verification": tg_verify_stats,
                        "phone_resolution": phone_res_stats,
                        "wave2_vacancies": wave2_stats,
                        "wave3_ru_experts": wave3_stats,
                        "waves_executed": waves_executed,
                        "pre_waterfall_deduped": pre_waterfall_deduped,
                        "cooldown_sources": cooldown_sources,
                        "ro_sessions_count": len(ro_sessions),
                        "daily_digest": daily_digest_res,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            summary_tmp.replace(st_dir / "last_run_summary.json")
        except OSError as exc:
            logger.debug("Failed writing last_run_summary.json: %s", exc)

    return result_payload


def _load_dotenv_if_allowed(env_path: Path) -> None:
    try:
        from app.runtime_config import should_skip_dotenv

        if should_skip_dotenv() or not env_path.is_file():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
                val = val[1:-1]
            if key:
                os.environ.setdefault(key, val)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_allowed(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Autonomous Low-Water Mark Leadgen Replenishment for VPS"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CRMTG_DB", DEFAULT_DB_PATH),
        help="Path to SQLite CRM DB",
    )
    parser.add_argument("--low-water-mark", type=int, default=50)
    parser.add_argument("--vacancies-low-water-mark", type=int, default=10)
    parser.add_argument("--critical-low-mark", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    def _handle_sigterm(_signum, _frame):
        raise _SigtermInterrupt("crmtg-leadgen terminated by SIGTERM")

    prev_sigterm = None
    try:
        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        pass

    conn = connect(args.db)
    init_db(conn)
    try:
        try:
            res = run_autonomous_replenish(
                conn,
                low_water_mark=args.low_water_mark,
                vac_low_water_mark=args.vacancies_low_water_mark,
                critical_low_mark=args.critical_low_mark,
                stage_timeout=args.timeout,
            )
        except (Exception, _SigtermInterrupt) as exc:
            import traceback

            traceback.print_exc(file=sys.stderr)
            err_payload = [
                {
                    "stage": "vps_autonomous_replenish_crash",
                    "returncode": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                    "at": utc_now(),
                }
            ]
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                now_stamp = utc_now()
                conn.execute(
                    """
                    INSERT INTO kv_settings (key, value, updated_at)
                    VALUES ('leadgen_stage_errors', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (json.dumps(err_payload, ensure_ascii=False), now_stamp),
                )
                conn.commit()
            except Exception:
                pass
            return 1
    finally:
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError):
                pass
        conn.close()

    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
