#!/usr/bin/env python3
"""Backfill runner for `direct_x_leads.jsonl` using Wave A Multi-DM + Bridge v2.

Pipeline per eligible row:
1. Multi-DM `contact_waterfall.enrich_lead_contacts` (budget-guarded)
2. `tg_identity_bridge.bridge_lead_to_telegram` (if personal TG still missing)
3. `tg_group_admins` (`enrich_whales_with_tg_group_admins`, $0)
4. `resolve_and_route_phone_leads` (WA + dedicated TG phone_checker, $0)
5. Per-row atomic checkpoint `tg_bridge_v2_attempted_at` to `direct_x_leads.jsonl`
6. When not `--dry-run`, ingest resolved TG whales via `ingest_v2_routed_leads` + Rule 5 preview.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)


def _extract_tg_handle(row: Mapping[str, Any]) -> str:
    return (
        str(
            row.get("username")
            or row.get("telegram_username")
            or row.get("tg_username")
            or ""
        )
        .strip()
        .lstrip("@")
    )


def _has_personal_tg_handle(row: Mapping[str, Any], repl_mod: Any) -> bool:
    handle = _extract_tg_handle(row)
    return bool(handle and repl_mod._is_real_personal_tg_handle(handle))


def _is_resolved_tg_whale(row: Mapping[str, Any], repl_mod: Any) -> bool:
    if _has_personal_tg_handle(row, repl_mod):
        return True
    if (
        str(row.get("provider") or "").strip().lower() == "telegram"
        and row.get("telegram_user_id") is not None
        and str(row.get("telegram_user_id") or "").strip() != ""
    ):
        return True
    return False


def _merge_candidate_fields(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if v is None or v == "":
            continue
        if k not in out or out.get(k) in (None, "", []):
            out[k] = v
    for tg_key in (
        "username",
        "telegram_username",
        "tg_username",
        "telegram_url",
        "tg_bridge_match",
        "tg_handle_is_guessed",
        "provider",
    ):
        if tg_key in extra and extra.get(tg_key) not in (None, ""):
            out[tg_key] = extra[tg_key]
    return out


def _write_direct_x_atomic(
    direct_x_path: Path,
    rows: list[dict[str, Any]],
    removed_indices: set[int],
) -> None:
    kept = [r for idx, r in enumerate(rows) if idx not in removed_indices]
    direct_x_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = direct_x_path.with_name(f".{direct_x_path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
        encoding="utf-8",
    )
    os.replace(tmp, direct_x_path)


def _stdlib_load_dotenv(env_path: Path) -> None:
    if (os.getenv("CRMTG_E2E_SANDBOX") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if (os.getenv("CRMTG_SKIP_DOTENV") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
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


def backfill_direct_x_leads(
    conn: sqlite3.Connection,
    *,
    state_dir: Path | str,
    canary: int = 0,
    retry_v2: bool = False,
    dry_run: bool = False,
    cfg: Mapping[str, Any] | None = None,
    budget_state: Any | None = None,
    treg_balance_fn: Callable[[], Any] | None = None,
    monid_balance_fn: Callable[[], Any] | None = None,
    notify_fn: Callable[[str], Any] | None = None,
    http_get_fn: Callable[..., Any] | None = None,
    treg_fn: Callable[..., Any] | None = None,
    monid_fn: Callable[..., Any] | None = None,
    dm_finder_fn: Callable[..., Any] | None = None,
    dm_multi_finder_fn: Callable[..., Any] | None = None,
    tg_bridge_fn: Callable[..., Any] | None = None,
    tg_client: Any = None,
    tg_admin_extractor_fn: Callable[..., Any] | None = None,
    tg_entity_resolver_fn: Callable[..., Any] | None = None,
    wa_gateway: Any = None,
    tg_checker_client: Any = None,
    tg_checker_clients: Mapping[str, Any] | None = None,
    scraper_session: Path | str | None = None,
    scraper_sessions_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Run Wave A backfill over `direct_x_leads.jsonl` with per-row v2 checkpointing."""
    from app.db import utc_now
    from app.leadgen_settings import get_leadgen_config
    from app.outreach import preview_outreach_batch
    from leadgen.budget_guard import check_balances_and_alert
    from leadgen.contact_waterfall import enrich_lead_contacts
    from leadgen.language_router import detect_lead_language
    from leadgen.tg_identity_bridge import bridge_lead_to_telegram
    from leadgen import vps_autonomous_replenish as _repl

    state_path = Path(state_dir)
    direct_x_path = state_path / "direct_x_leads.jsonl"
    stats: dict[str, Any] = {
        "total_rows": 0,
        "eligible_rows": 0,
        "attempted": 0,
        "tg_resolved": 0,
        "tg_group_admins_promoted": 0,
        "phones_checked": 0,
        "whales_ingested": 0,
        "dry_run": bool(dry_run),
        "retry_v2": bool(retry_v2),
        "canary": int(canary or 0),
        "paid_allowed": False,
        "rule5_previews": {},
        "rule5_quarantined": {},
        "sample_resolved": [],
    }
    if not direct_x_path.is_file() or direct_x_path.stat().st_size == 0:
        return stats

    all_rows = _repl._read_jsonl_dicts(direct_x_path)
    stats["total_rows"] = len(all_rows)
    if not all_rows:
        return stats

    eligible_indices: list[int] = []
    for idx, row in enumerate(all_rows):
        if str(row.get("tg_bridge_v2_attempted_at") or "").strip():
            continue
        if not retry_v2 and str(row.get("tg_bridge_attempted_at") or "").strip():
            continue
        eligible_indices.append(idx)

    canary_limit = max(0, int(canary or 0))
    if canary_limit > 0:
        eligible_indices = eligible_indices[:canary_limit]

    stats["eligible_rows"] = len(eligible_indices)
    if not eligible_indices:
        return stats

    eff_cfg = dict(cfg) if cfg is not None else get_leadgen_config(conn)
    eff_budget = (
        budget_state
        if budget_state is not None
        else check_balances_and_alert(
            conn,
            cfg=eff_cfg,
            treg_balance_fn=treg_balance_fn,
            monid_balance_fn=monid_balance_fn,
            notify_fn=notify_fn,
            allow_unavailable=False,
        )
    )
    paid_allowed = bool(eff_budget.paid_allowed)
    stats["paid_allowed"] = paid_allowed
    _guard_fn, _cost_fn = _repl._make_budget_callbacks(conn, eff_cfg, eff_budget)

    eff_treg_fn = treg_fn if paid_allowed else (lambda *a, **kw: {})
    eff_monid_fn = monid_fn if paid_allowed else (lambda *a, **kw: {})

    isolated_session: Path | None = None
    try:
        isolated_session = _repl.validate_isolated_scraper_session(
            conn,
            scraper_session,
            scraper_sessions_dir=scraper_sessions_dir,
        )
    except ValueError as exc:
        logger.warning("Skipping non-isolated scraper session in backfill_direct_x: %s", exc)
        isolated_session = None

    pre_existing_ids: set[int] = {
        int(r[0]) for r in conn.execute("SELECT id FROM campaign_recipients").fetchall()
    }
    touched_campaigns: dict[str, dict[str, Any]] = {}
    removed_indices: set[int] = set()
    stage_errors: list[dict[str, Any]] = []

    for idx in eligible_indices:
        stats["attempted"] += 1
        row = dict(all_rows[idx])

        # 1. Multi-DM contact waterfall (budget-guarded)
        cw_out = _repl._call_with_supported_kwargs(
            enrich_lead_contacts,
            dict(row),
            allow_paid_tier2=paid_allowed,
            budget_usd=None,
            http_get_fn=http_get_fn,
            treg_fn=eff_treg_fn,
            monid_fn=eff_monid_fn,
            dm_finder_fn=dm_finder_fn,
            dm_multi_finder_fn=dm_multi_finder_fn,
            tg_bridge_fn=tg_bridge_fn,
            budget_guard_fn=_guard_fn,
            cost_record_fn=_cost_fn,
            cfg=eff_cfg,
        )
        candidate = dict(cw_out) if isinstance(cw_out, Mapping) else dict(row)

        # 2. Direct TG identity bridge fallback if still missing personal TG
        if not _has_personal_tg_handle(candidate, _repl):
            if tg_bridge_fn is not None:
                bridged_b = _repl._call_with_supported_kwargs(
                    tg_bridge_fn,
                    dict(candidate),
                    allow_paid=paid_allowed,
                    budget_guard_fn=_guard_fn,
                    cost_record_fn=_cost_fn,
                    treg_fn=eff_treg_fn,
                    monid_fn=eff_monid_fn,
                )
            else:
                bridged_b = bridge_lead_to_telegram(
                    dict(candidate),
                    allow_paid=paid_allowed,
                    budget_guard_fn=_guard_fn,
                    cost_record_fn=_cost_fn,
                    treg_fn=eff_treg_fn,
                    monid_fn=eff_monid_fn,
                )
            if isinstance(bridged_b, Mapping):
                candidate = _merge_candidate_fields(candidate, bridged_b)

        # 3. $0 TG group/channel admin extraction if still missing TG
        if not _has_personal_tg_handle(candidate, _repl):
            adm_rows, adm_stats = _repl.enrich_whales_with_tg_group_admins(
                [candidate],
                client=tg_client,
                scraper_session=isolated_session,
                admin_extractor_fn=tg_admin_extractor_fn,
            )
            stats["tg_group_admins_promoted"] += int(adm_stats.get("leads_promoted") or 0)
            if adm_rows:
                candidate = dict(adm_rows[0])

        # Live PeerType verification when resolver/client/isolated session is available
        cand_u = _extract_tg_handle(candidate)
        if cand_u and (
            tg_client is not None
            or tg_entity_resolver_fn is not None
            or (isolated_session is not None and isolated_session.is_file())
        ):
            verified_rows, _v_stats = _repl.verify_personal_tg_handles_live(
                [candidate],
                client=tg_client,
                scraper_session=isolated_session,
                entity_resolver_fn=tg_entity_resolver_fn,
            )
            if verified_rows:
                candidate = dict(verified_rows[0])

        # 4. $0 Phone messenger resolution (WA + dedicated TG checker) when still missing TG
        if not _has_personal_tg_handle(candidate, _repl) and str(
            candidate.get("phone") or candidate.get("mobile_phone") or candidate.get("whatsapp") or ""
        ).strip():
            routed_rows, phone_stats = _repl.resolve_and_route_phone_leads(
                conn,
                [candidate],
                cfg=eff_cfg,
                wa_gateway=wa_gateway,
                tg_checker_client=tg_checker_client,
                tg_checker_clients=tg_checker_clients,
                scraper_sessions_dir=scraper_sessions_dir,
            )
            stats["phones_checked"] += int(phone_stats.get("phones_checked") or 0)
            if routed_rows:
                candidate = dict(routed_rows[0])

        # Stamp v2 checkpoint
        candidate["tg_bridge_v2_attempted_at"] = utc_now()
        all_rows[idx] = candidate

        if _is_resolved_tg_whale(candidate, _repl):
            stats["tg_resolved"] += 1
            resolved_u = _extract_tg_handle(candidate)
            if resolved_u:
                candidate["username"] = resolved_u
                candidate["telegram_username"] = resolved_u
            candidate["provider"] = "telegram"
            candidate["segment"] = "whale"
            candidate["language"] = (
                "ru"
                if (
                    str(candidate.get("language") or "").strip().lower() == "ru"
                    or detect_lead_language(candidate) == "ru"
                )
                else "en"
            )
            src_val = str(
                candidate.get("lead_source") or candidate.get("source") or "direct_x_backfill"
            ).strip()
            candidate["source"] = src_val
            candidate["lead_source"] = src_val

            if len(stats["sample_resolved"]) < 5:
                stats["sample_resolved"].append(
                    {
                        "id": candidate.get("id"),
                        "company": candidate.get("company") or candidate.get("project") or "",
                        "username": resolved_u,
                        "telegram_user_id": candidate.get("telegram_user_id"),
                        "phone": candidate.get("phone") or "",
                        "tg_bridge_match": candidate.get("tg_bridge_match") or "",
                    }
                )

            if not dry_run:
                ingest_res = _repl.ingest_v2_routed_leads(
                    conn,
                    [candidate],
                    cfg=eff_cfg,
                    auto_push=True,
                )
                ingested_now = int(ingest_res.get("whales_ingested") or 0)
                stats["whales_ingested"] += ingested_now
                for cname, cobj in (ingest_res.get("campaigns") or {}).items():
                    if isinstance(cobj, dict):
                        touched_campaigns[str(cname)] = cobj
                if ingested_now > 0:
                    removed_indices.add(idx)

        if not dry_run:
            _write_direct_x_atomic(direct_x_path, all_rows, removed_indices)

    if dry_run and stats["sample_resolved"]:
        import re as _re
        from leadgen.outreach_copy import build_outreach_sequence

        legacy_brand_re = _re.compile(r"\bCreator\s*OS\b|creatoros", _re.IGNORECASE)
        dry_samples: list[dict[str, Any]] = []
        forbidden_cnt = 0
        for item_idx in eligible_indices:
            cand = all_rows[item_idx]
            if not _is_resolved_tg_whale(cand, _repl):
                continue
            lang = str(cand.get("language") or "en")
            seq = build_outreach_sequence(
                cand,
                copy_profile="whale_mass",
                segment="whale",
                language=lang,
                provider="telegram",
            )
            combined_copy = f"{seq.step1_text}\n{seq.step2_text}\n{seq.step3_text}"
            if legacy_brand_re.search(combined_copy):
                forbidden_cnt += 1
            if len(dry_samples) < 5:
                dry_samples.append(
                    {
                        "username": _extract_tg_handle(cand),
                        "company": cand.get("company") or cand.get("project") or "",
                        "step1_text": seq.step1_text,
                    }
                )
        stats["rule5_previews"]["dry_run_preview"] = {
            "ok": forbidden_cnt == 0,
            "total_ready": len(dry_samples),
            "forbidden_pattern_count": forbidden_cnt,
            "sample": dry_samples,
        }

    if not dry_run and touched_campaigns:
        for cname, camp_obj in touched_campaigns.items():
            preview, quarantined_ids = _repl._enforce_rule5_quarantine_on_new_recipients(
                conn,
                campaign=camp_obj,
                pre_existing_ids=pre_existing_ids,
                stage_errors=stage_errors,
            )
            if preview is None and camp_obj.get("id") is not None:
                preview = preview_outreach_batch(
                    conn, int(camp_obj["id"]), sample_size=5, strict=False
                )
            if preview is not None:
                stats["rule5_previews"][cname] = {
                    "ok": bool(preview.get("ok", True)),
                    "total_ready": int(preview.get("total_ready") or 0),
                    "forbidden_pattern_count": int(preview.get("forbidden_pattern_count") or 0),
                    "sample": list(preview.get("sample") or [])[:5],
                }
            if quarantined_ids:
                stats["rule5_quarantined"][cname] = quarantined_ids

    return stats


def main(argv: list[str] | None = None) -> int:
    _stdlib_load_dotenv(ROOT / ".env")
    from app.db import DEFAULT_DB, connect, init_db
    from app.leadgen_settings import _resolve_state_dir

    parser = argparse.ArgumentParser(
        description="Backfill direct_x_leads.jsonl using Wave A Multi-DM + Bridge v2"
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("CRMTG_DB") or str(DEFAULT_DB),
        help="Path to crm.db",
    )
    parser.add_argument(
        "--state-dir",
        default="",
        help="Path to leadgen state directory containing direct_x_leads.jsonl",
    )
    parser.add_argument(
        "--canary",
        type=int,
        default=0,
        help="Max eligible rows to process (0 = all eligible rows)",
    )
    parser.add_argument(
        "--retry-v2",
        action="store_true",
        help="Re-attempt rows with legacy tg_bridge_attempted_at (still skips tg_bridge_v2_attempted_at)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without modifying direct_x_leads.jsonl or ingesting into CRM",
    )
    args = parser.parse_args(argv)

    eff_state_dir = Path(args.state_dir) if str(args.state_dir or "").strip() else _resolve_state_dir()
    conn = connect(Path(args.db))
    try:
        init_db(conn)
        result = backfill_direct_x_leads(
            conn,
            state_dir=eff_state_dir,
            canary=max(0, int(args.canary or 0)),
            retry_v2=bool(args.retry_v2),
            dry_run=bool(args.dry_run),
        )
    finally:
        conn.close()

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
