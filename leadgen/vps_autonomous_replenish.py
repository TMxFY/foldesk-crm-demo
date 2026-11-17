from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any, Callable

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

import app.digest as app_digest
from app.db import ROOT, as_dict, connect, init_db, resolve_session, utc_now
from app.leadgen_settings import (
    _clean_chat_list,
    _resolve_state_dir,
    get_leadgen_config,
    push_discovered_now,
    resolve_target_b2b_tg_campaign_name,
)
from app.outreach import preview_outreach_batch
from leadgen.tg_to_gsheet import admit_discovered_row, apply_master_merge

DEFAULT_DB_PATH = "/opt/crmtg/data/crm.db"
DEFAULT_SCRAPER_SESSIONS_DIR = "/opt/crmtg/data/scraper_sessions"


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
            if found:
                candidate_raw = str(found[0])

    if not candidate_raw:
        return None

    cand_paths, cand_names = _session_path_variants(candidate_raw)
    acc_rows = conn.execute(
        "SELECT id, phone, session_path, telegram_username FROM accounts"
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
            return with_suffix
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
                admitted = admit_discovered_row(row)
                if not admitted:
                    continue
                admitted.setdefault("icp_match", True)
                out_fh.write(json.dumps(admitted, ensure_ascii=False) + "\n")
                added += 1
    return added


def run_autonomous_replenish(
    conn: sqlite3.Connection,
    *,
    runner: Callable[..., Any] = subprocess.run,
    low_water_mark: int = 50,
    vac_low_water_mark: int = 10,
    critical_low_mark: int = 15,
    stage_timeout: int = 180,
    scraper_session: str | Path | None = None,
    digest_opener: Any = None,
) -> dict[str, Any]:
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

    # 2. Subtract quarantined sources
    quarantined = load_quarantined_sources(conn)
    eff_cfg = apply_quarantined_sources(cfg, quarantined)
    b2b_cfg = eff_cfg["b2b"]
    vac_cfg = eff_cfg["vacancies"]

    b2b_name = resolve_target_b2b_tg_campaign_name(
        conn, b2b_cfg.get("target_campaign_tg"), fallback_default="Leadgen Auto (TG)"
    )
    vac_name = str(vac_cfg.get("campaign_name") or "TG Вакансии").strip() or "TG Вакансии"

    b2b_camp = _find_campaign_by_name(conn, b2b_name)
    vac_camp = _find_campaign_by_name(conn, vac_name)

    pending_b2b_before = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
    pending_vac_before = _count_pending(conn, vac_camp["id"] if vac_camp else None)
    pending_b2b = pending_b2b_before
    pending_vac = pending_vac_before

    b2b_active = bool(b2b_cfg.get("enabled", True)) and (
        b2b_camp is None or str(b2b_camp.get("status") or "") != "completed"
    )
    vac_active = bool(vac_cfg.get("enabled", True)) and (
        vac_camp is None or str(vac_camp.get("status") or "") != "completed"
    )

    needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
    needs_vac = vac_active and pending_vac < int(vac_low_water_mark)

    ingested_total = 0
    stages_run: list[str] = []
    rule5_previews: dict[str, Any] = {}

    if needs_b2b or needs_vac:
        # Step 1: Ingest any already-prepared local .jsonl / job_replies without mutating limits
        push_1 = push_discovered_now(conn)
        ingested_total += int(push_1.get("ingested") or 0)

        b2b_camp = _find_campaign_by_name(conn, b2b_name)
        vac_camp = _find_campaign_by_name(conn, vac_name)
        pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
        pending_vac = _count_pending(conn, vac_camp["id"] if vac_camp else None)

        still_needs_b2b = b2b_active and pending_b2b < int(low_water_mark)
        still_needs_vac = vac_active and pending_vac < int(vac_low_water_mark)

        # Step 2: Run server-available discovery stages via injectable runner
        if still_needs_b2b or still_needs_vac:
            state_dir = _resolve_state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            eff_cfg_path = state_dir / "effective_leadgen_config.json"
            eff_cfg_path.write_text(
                json.dumps(eff_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            sub_env = dict(os.environ)
            sub_env["LEADGEN_STATE_DIR"] = str(state_dir)
            sub_env["LEADGEN_CONFIG_PATH"] = str(eff_cfg_path)
            if isolated_session is not None:
                sub_env["TG_SESSION"] = str(isolated_session)
                sub_env["TG_JOBS_SESSION"] = str(isolated_session)

            def _exec_stage(stage_name: str, cmd: list[str]) -> bool:
                stages_run.append(stage_name)
                try:
                    proc = runner(
                        cmd,
                        env=sub_env,
                        timeout=stage_timeout,
                        cwd=str(ROOT),
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    return int(getattr(proc, "returncode", 0) or 0) == 0
                except Exception:
                    return False

            py_bin = sys.executable
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if still_needs_b2b:
                raw_jsonl = state_dir / f"raw_{ts}.jsonl"
                master_jsonl = state_dir / "master_ids.jsonl"
                catalogs_out = state_dir / f"buyer_catalogs_{ts}.jsonl"
                external_out = state_dir / f"external_{ts}.jsonl"
                sources_cfg = b2b_cfg.get("sources") or {}
                limits_cfg = b2b_cfg.get("limits") or {}

                # Web / API discovery stages (no TG session needed)
                if sources_cfg.get("buyer_catalogs", True) and "buyer_catalogs" not in quarantined:
                    _exec_stage(
                        "buyer_catalogs",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "buyer_catalogs.py"),
                            "--output",
                            str(catalogs_out),
                        ],
                    )
                ext_limit = int(limits_cfg.get("external_limit") or 20)
                if ext_limit > 0 and "creator_external_leads" not in quarantined:
                    _exec_stage(
                        "creator_external_leads",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "creator_external_leads.py"),
                            "--output",
                            str(external_out),
                            "--limit",
                            str(ext_limit),
                            "--verify-telegram",
                            "--no-qualify",
                        ],
                    )

                # TG group scraper ONLY if valid isolated SCRAPER_TG_SESSION is available
                effective_groups: list[str] = []
                for g in [*(b2b_cfg.get("tg_groups_new") or []), *(b2b_cfg.get("tg_groups_old") or [])]:
                    if g not in effective_groups:
                        effective_groups.append(g)

                if (
                    isolated_session is not None
                    and sources_cfg.get("tg_groups", True)
                    and effective_groups
                ):
                    scrape_cmd = [
                        py_bin,
                        str(ROOT / "leadgen" / "tg_scrape_posters.py"),
                        "--session",
                        str(isolated_session),
                        "--groups",
                        ",".join(effective_groups),
                        "--mode",
                        "posters",
                        "--msg-limit",
                        str(int(limits_cfg.get("msg_limit") or 1800)),
                        "--output",
                        str(raw_jsonl),
                        "--append",
                    ]
                    if master_jsonl.is_file():
                        scrape_cmd.extend(["--exclude", str(master_jsonl)])
                    _exec_stage("tg_scrape_posters", scrape_cmd)

                _merge_discovered_jsonl_into_raw([catalogs_out, external_out], raw_jsonl)
                apply_master_merge(str(raw_jsonl), str(master_jsonl))
                new_jsonl = Path(f"{raw_jsonl}.new")
                if not new_jsonl.exists():
                    new_jsonl.write_text("", encoding="utf-8")

                has_new_rows = new_jsonl.stat().st_size > 0
                if has_new_rows or runner is not subprocess.run:
                    enriched_jsonl = state_dir / f"raw_{ts}.enriched.jsonl"
                    _exec_stage(
                        "tg_enrich_leads",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "tg_enrich_leads.py"),
                            "--input",
                            str(new_jsonl),
                            "--output",
                            str(enriched_jsonl),
                            "--fetch",
                        ],
                    )
                    enrich_src = (
                        enriched_jsonl
                        if enriched_jsonl.is_file() and enriched_jsonl.stat().st_size > 0
                        else new_jsonl
                    )
                    if has_llm_key:
                        qualified_jsonl = state_dir / f"raw_{ts}.qualified.jsonl"
                        final_jsonl = state_dir / f"raw_{ts}.final.jsonl"
                        _exec_stage(
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
                        qual_src = (
                            qualified_jsonl
                            if qualified_jsonl.is_file() and qualified_jsonl.stat().st_size > 0
                            else enrich_src
                        )
                        _exec_stage(
                            "tg_curate",
                            [
                                py_bin,
                                str(ROOT / "leadgen" / "tg_curate.py"),
                                "--input",
                                str(qual_src),
                                "--output",
                                str(final_jsonl),
                            ],
                        )
                        if final_jsonl.is_file() and final_jsonl.stat().st_size == 0:
                            final_jsonl.unlink()

            # Vacancy scraper ONLY if valid isolated SCRAPER_TG_SESSION is available
            if still_needs_vac and isolated_session is not None:
                mon_out = state_dir / f"job_monitor_{today_str}.json"
                rep_out = state_dir / f"job_replies_{today_str}.json"
                _exec_stage(
                    "job_monitor",
                    [
                        py_bin,
                        str(ROOT / "leadgen" / "job_monitor.py"),
                        "--hours",
                        "24",
                        "--out",
                        str(mon_out),
                    ],
                )
                if has_llm_key and (
                    (mon_out.is_file() and mon_out.stat().st_size > 0)
                    or runner is not subprocess.run
                ):
                    _exec_stage(
                        "job_reply",
                        [
                            py_bin,
                            str(ROOT / "leadgen" / "job_reply.py"),
                            str(mon_out),
                            "--out",
                            str(rep_out),
                            "--limit",
                            str(int(vac_cfg.get("daily_limit") or 20)),
                            "--no-crm",
                        ],
                    )

            push_2 = push_discovered_now(conn)
            ingested_total += int(push_2.get("ingested") or 0)

            b2b_camp = _find_campaign_by_name(conn, b2b_name)
            vac_camp = _find_campaign_by_name(conn, vac_name)
            pending_b2b = _count_pending(conn, b2b_camp["id"] if b2b_camp else None)
            pending_vac = _count_pending(conn, vac_camp["id"] if vac_camp else None)

        # Step 3: Mandatory Rule 5 preview_outreach_batch(conn, campaign_id, strict=True) on replenished campaigns
        if b2b_camp is not None and pending_b2b > pending_b2b_before and pending_b2b > 0:
            rule5_previews[str(b2b_camp["name"])] = preview_outreach_batch(
                conn, int(b2b_camp["id"]), strict=True
            )
        if vac_camp is not None and pending_vac > pending_vac_before and pending_vac > 0:
            rule5_previews[str(vac_camp["name"])] = preview_outreach_batch(
                conn, int(vac_camp["id"]), strict=True
            )

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

    return {
        "ok": True,
        "ingested_total": ingested_total,
        "pending_b2b": pending_b2b,
        "pending_vacancies": pending_vac,
        "quarantined_sources": sorted(quarantined),
        "stages_run": stages_run,
        "rule5_previews": rule5_previews,
        "low_water_alert": low_water_alert,
    }


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    try:
        res = run_autonomous_replenish(
            conn,
            low_water_mark=args.low_water_mark,
            vac_low_water_mark=args.vacancies_low_water_mark,
            critical_low_mark=args.critical_low_mark,
            stage_timeout=args.timeout,
        )
    finally:
        conn.close()

    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
