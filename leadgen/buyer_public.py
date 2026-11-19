#!/usr/bin/env python3
"""Public buyer pages that do not need a new account or a join.

A Whop / Content Rewards owner is a lead only with an x.com profile or a
t.me link. Clipper submission posts and the platform's own profile are not
owners. Discord Bloss and @propinfa are skipped when reading them would mean
joining. A failure here must not fail the Telegram batch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

_X_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?=(?:[?#\"'\s<>]|$))",
    re.I,
)
_TG_PROFILE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})(?=(?:[?#\"'\s<>]|$))",
    re.I,
)
_DENY = {
    "whop", "contentrewards", "softugc", "i",
    "pamba_ai", "karolzdeb", "usefastlane", "gauravsbuilding", "arcads_ai",
    "rom1trs", "creatify_ai", "makeugc", "fyncas",
}
_TG_DENY = {"joinchat", "share", "addstickers", "proxy", "iv", "s"}
DISCOVER_URL = "https://contentrewards.com/c/discover"
_TOKEN_KEYS = ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "BLOSS_DISCORD_TOKEN")
_WHOP_SEARCH_QUERIES = (
    'site:whop.com ("x.com/" OR "t.me/") -blog -terms',
    'site:whop.com ("content rewards" OR "clipping" OR "ugc") ("x.com" OR "t.me")',
    'site:whop.com ("saas" OR "ai" OR "fitness" OR "app") ("x.com/" OR "t.me/")',
    'site:whop.com ("bounty" OR "creator program" OR "affiliates") ("x.com/" OR "t.me/")',
    'site:whop.com ("software" OR "ecom" OR "trading" OR "automation") "t.me/"',
    'site:whop.com ("mobile app" OR "ai tool" OR "brand") "x.com/"',
)


def _resolve_buyer_public_seen_path() -> Path:
    state_dir_raw = str(os.environ.get("LEADGEN_STATE_DIR") or "").strip()
    if state_dir_raw:
        return Path(state_dir_raw) / "buyer_public_seen.json"
    return Path(__file__).resolve().parents[1] / "data" / "leadgen_state" / "buyer_public_seen.json"


def _load_buyer_public_state(seen_path: Path) -> tuple[int, list[str]]:
    if not seen_path.is_file():
        return 0, []
    try:
        obj = json.loads(seen_path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            idx = int(obj.get("query_idx") or 0)
            seen = [str(x) for x in (obj.get("seen_ids") or []) if str(x).strip()]
            return idx, seen
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0, []
    return 0, []


def _save_buyer_public_state(seen_path: Path, query_idx: int, seen_ids: list[str]) -> None:
    try:
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(
            json.dumps(
                {"query_idx": int(query_idx) % len(_WHOP_SEARCH_QUERIES), "seen_ids": seen_ids[-2000:]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _owner_row(*, handle: str, telegram: str, source_url: str) -> dict:
    x_url = f"https://x.com/{handle}" if handle else ""
    tg_url = f"https://t.me/{telegram}" if telegram else ""
    social = []
    if x_url:
        social.append({"platform": "x", "url": x_url, "handle": handle})
    key = (handle or telegram).casefold()
    row = {
        "id": hashlib.sha256(f"whop:{key}".encode()).hexdigest()[:24],
        "name": (handle or telegram)[:160],
        "username": "",
        "bio": "",
        "sample": "",
        "links": [item for item in (x_url, tg_url) if item],
        "source": "whop",
        "source_url": source_url,
        "group": "whop",
        "contact_url": x_url or tg_url,
        "project": "",
        "project_url": "",
        "what_they_do": "",
        "icp_match": True,
        "founder_x": x_url,
        "founder_twitter": x_url,
        "social_verified": social,
        "social_contacts": social,
        "telegram_candidates": [],
    }
    if telegram:
        row["telegram_verified"] = [{"username": telegram, "url": tg_url}]
    return row


def owner_contacts(html: str, *, source_url: str) -> list[dict]:
    """Owners with a profile link. Status URLs are clipper posts, not owners."""
    found: dict[str, dict[str, str]] = {}
    for match in _X_PROFILE.finditer(html or ""):
        handle = match.group(1)
        if handle.casefold() in _DENY:
            continue
        slot = found.setdefault(handle.casefold(), {"handle": handle, "tg": ""})
        slot["handle"] = handle
    for match in _TG_PROFILE.finditer(html or ""):
        nick = match.group(1)
        if nick.casefold() in _TG_DENY or nick.casefold() in _DENY:
            continue
        slot = found.setdefault(nick.casefold(), {"handle": "", "tg": nick})
        slot["tg"] = nick
    return [
        _owner_row(handle=slot["handle"], telegram=slot["tg"], source_url=source_url)
        for slot in found.values()
        if slot["handle"] or slot["tg"]
    ]


def _curl(url: str) -> str:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "25", "-A", "Mozilla/5.0", url],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise OSError("fetch failed")
    return proc.stdout


def _whop_treg_search_rows(
    *,
    seen_path: Path | None = None,
    seen_ids: set[str] | None = None,
) -> list[dict]:
    try:
        from leadgen.treg_client import call as treg_call, check_treg_ready
    except ImportError:
        try:
            from treg_client import call as treg_call, check_treg_ready
        except ImportError:
            return []
    if not check_treg_ready():
        return []

    st_path = seen_path if seen_path is not None else _resolve_buyer_public_seen_path()
    saved_idx, saved_list = _load_buyer_public_state(st_path)
    already_seen = set(saved_list)
    if seen_ids is not None:
        already_seen.update(seen_ids)

    n_q = len(_WHOP_SEARCH_QUERIES)
    start_idx = saved_idx % n_q
    ordered_queries = [_WHOP_SEARCH_QUERIES[(start_idx + i) % n_q] for i in range(n_q)]
    snippets: list[str] = []
    queries_tried = 0
    for query in ordered_queries[:3]:
        queries_tried += 1
        payload = treg_call(
            "crawl4ai.web.search",
            query={"q": query},
            timeout=25,
        )
        if not isinstance(payload, dict):
            continue
        results = payload.get("results") or payload.get("result") or []
        if isinstance(results, dict):
            results = results.get("results") or []
        batch_snips: list[str] = []
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    batch_snips.append(
                        " ".join(str(item.get(k) or "") for k in ("url", "title", "snippet", "description"))
                    )
        if batch_snips:
            cand_rows = [
                r
                for r in owner_contacts("\n".join(batch_snips), source_url=DISCOVER_URL)
                if r.get("id") not in already_seen
            ]
            if cand_rows:
                snippets.extend(batch_snips)
                break
    _save_buyer_public_state(st_path, (start_idx + max(1, queries_tried)) % n_q, saved_list)
    if not snippets:
        return []
    rows = [
        r
        for r in owner_contacts("\n".join(snippets), source_url=DISCOVER_URL)
        if r.get("id") not in already_seen
    ]
    for row in rows:
        row["has_monetization_signal"] = True
        row["monetization"] = "paid_product"
    return rows


def whop_rows(fetch_fn=None) -> list[dict]:
    fetch = fetch_fn or _curl
    html = ""
    try:
        html = fetch(DISCOVER_URL)
    except Exception:
        html = ""
    rows = owner_contacts(html or "", source_url=DISCOVER_URL)
    if fetch_fn is not None:
        return rows

    seen_path = _resolve_buyer_public_seen_path()
    saved_idx, saved_list = _load_buyer_public_state(seen_path)
    already_seen = set(saved_list)

    fresh_rows: list[dict] = []
    batch_ids: set[str] = set()
    for r in rows:
        rid = str(r.get("id") or "")
        if rid and rid not in already_seen and rid not in batch_ids:
            batch_ids.add(rid)
            fresh_rows.append(r)

    treg_rows = _whop_treg_search_rows(
        seen_path=seen_path,
        seen_ids=(already_seen | batch_ids),
    )
    # Reload query_idx updated by _whop_treg_search_rows and append emitted IDs
    next_idx, latest_saved = _load_buyer_public_state(seen_path)
    if not treg_rows:
        next_idx = (saved_idx + 1) % len(_WHOP_SEARCH_QUERIES)
    for r in treg_rows:
        rid = str(r.get("id") or "")
        if rid and rid not in already_seen and rid not in batch_ids:
            batch_ids.add(rid)
            fresh_rows.append(r)

    updated_seen = list(latest_saved)
    for r in fresh_rows:
        rid = str(r.get("id") or "")
        if rid and rid not in updated_seen:
            updated_seen.append(rid)
    _save_buyer_public_state(seen_path, next_idx, updated_seen)

    if not fresh_rows:
        print("whop: skipped (no fresh owner with x.com or t.me)", flush=True)
    return fresh_rows


def bloss_rows() -> list[dict]:
    if not any(os.getenv(key) for key in _TOKEN_KEYS):
        print("discord bloss: skipped (no discord token)", flush=True)
        return []
    print("discord bloss: skipped (no read-without-join path)", flush=True)
    return []


def propinfa_rows() -> list[dict]:
    print("propinfa: skipped (public channel read needs join; not attempted)", flush=True)
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows: list[dict] = []
    for loader in (whop_rows, bloss_rows, propinfa_rows):
        try:
            rows.extend(loader())
        except Exception as exc:
            print(f"{loader.__name__}: skipped ({exc.__class__.__name__})", flush=True)
    with open(args.output, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
