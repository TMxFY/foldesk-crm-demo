"""Enrichment budget guard for paid treg/monid layers.

Tracks per-day enrichment spend in SQLite (`enrichment_ledger`), enforces
per-day dollar caps (`$5/day` default), parses live `treg balance` and
`monid balance` CLI outputs, and sends deduplicated Telegram alerts (warn < $2,
hard paid-layer stop < $0.50 or daily cap reached) using `kv_settings`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
from typing import Any
import urllib.error
import urllib.request

from app.db import locked_conn, utc_now, utc_today
from leadgen.monid_client import monid_bin
from leadgen.treg_client import treg_bin

logger = logging.getLogger(__name__)

DEFAULT_DAILY_CAP_USD = 5.0
DEFAULT_LOW_BALANCE_WARN_USD = 2.0
DEFAULT_LOW_BALANCE_STOP_USD = 0.50
_EPS = 1e-9

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BALANCE_DOLLAR_RE = re.compile(
    r"\bBalance\b\s*[:=]?\s*(?:(-\s*)\$\s*|\$\s*(-?))([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BALANCE_COLON_RE = re.compile(
    r"\bBalance\b\s*[:=]\s*(-?)([\d,]+(?:\.\d+)?)(?:\s*USD)?\b",
    re.IGNORECASE,
)
_TREG_MICRO_USD_RE = re.compile(r"(-?[\d,]+)\s*micro-?usd\b", re.IGNORECASE)
_STANDALONE_DOLLAR_RE = re.compile(
    r"^\s*(?:(-\s*)\$\s*|\$\s*(-?))([\d,]+(?:\.\d+)?)\s*(?:USD)?\s*$",
    re.IGNORECASE,
)


@dataclass
class EnrichmentBudgetState:
    day: str
    spent_usd: float
    daily_cap_usd: float
    treg_balance_usd: float | None
    monid_balance_usd: float | None
    paid_allowed: bool
    alert_sent: bool
    stop_reason: str = ""


@dataclass(frozen=True)
class _BudgetThresholds:
    daily_cap_usd: float
    low_balance_warn_usd: float
    low_balance_stop_usd: float
    alert_chat_id: str


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _to_finite_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        stripped = stripped[start : end + 1]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_thresholds(cfg: Mapping[str, Any] | None) -> _BudgetThresholds:
    section: Mapping[str, Any] = {}
    if isinstance(cfg, Mapping):
        nested = cfg.get("enrichment")
        section = nested if isinstance(nested, Mapping) else cfg

    def _num(key: str, default: float) -> float:
        raw = section.get(key) if key in section else (cfg.get(key) if isinstance(cfg, Mapping) else None)
        parsed = _to_finite_float(raw)
        if parsed is None or parsed < 0:
            return default
        return parsed

    raw_chat = section.get("alert_chat_id") if "alert_chat_id" in section else (
        cfg.get("alert_chat_id") if isinstance(cfg, Mapping) else ""
    )
    return _BudgetThresholds(
        daily_cap_usd=_num("daily_budget_cap_usd", DEFAULT_DAILY_CAP_USD),
        low_balance_warn_usd=_num("low_balance_warn_usd", DEFAULT_LOW_BALANCE_WARN_USD),
        low_balance_stop_usd=_num("low_balance_stop_usd", DEFAULT_LOW_BALANCE_STOP_USD),
        alert_chat_id=str(raw_chat or "").strip(),
    )


def _resolve_day(day: str | None) -> str:
    cleaned = str(day or "").strip()
    return cleaned or utc_today()


def _signed_match(sign_before: str | None, sign_after: str | None, amount_str: str) -> float | None:
    val = _to_finite_float(amount_str)
    if val is None:
        return None
    if (sign_before and "-" in sign_before) or (sign_after and "-" in sign_after):
        return -abs(val)
    return val


def parse_treg_balance_output(stdout: str) -> float | None:
    """Parse USD balance from `treg balance` text or JSON output."""
    cleaned = _strip_ansi(stdout)
    if not cleaned.strip():
        return None

    match = _BALANCE_DOLLAR_RE.search(cleaned)
    if match:
        return _signed_match(match.group(1), match.group(2), match.group(3))

    colon_match = _BALANCE_COLON_RE.search(cleaned)
    if colon_match:
        return _signed_match(colon_match.group(1), None, colon_match.group(2))

    micro_match = _TREG_MICRO_USD_RE.search(cleaned)
    if micro_match:
        micro_val = _to_finite_float(micro_match.group(1))
        if micro_val is not None:
            return micro_val / 1_000_000.0

    payload = _extract_json_object(cleaned)
    if payload is not None:
        for key in ("balance_usd", "balance"):
            if key in payload:
                val = _to_finite_float(payload[key])
                if val is not None:
                    return val
                if isinstance(payload[key], Mapping):
                    nested_val = _to_finite_float(payload[key].get("value"))
                    if nested_val is not None:
                        return nested_val
        for key in ("balance_micro", "balance_micro_usd", "micro_usd"):
            if key in payload:
                micro_val = _to_finite_float(payload[key])
                if micro_val is not None:
                    return micro_val / 1_000_000.0

    single = _STANDALONE_DOLLAR_RE.match(cleaned)
    if single:
        return _signed_match(single.group(1), single.group(2), single.group(3))

    return None


def parse_monid_balance_output(stdout: str) -> float | None:
    """Parse USD balance from `monid balance` text or JSON output."""
    cleaned = _strip_ansi(stdout)
    if not cleaned.strip():
        return None

    match = _BALANCE_DOLLAR_RE.search(cleaned)
    if match:
        return _signed_match(match.group(1), match.group(2), match.group(3))

    colon_match = _BALANCE_COLON_RE.search(cleaned)
    if colon_match:
        return _signed_match(colon_match.group(1), None, colon_match.group(2))

    payload = _extract_json_object(cleaned)
    if payload is not None:
        if isinstance(payload.get("balance"), Mapping):
            nested_val = _to_finite_float(payload["balance"].get("value"))
            if nested_val is not None:
                return nested_val
        for key in ("balance_usd", "balance"):
            if key in payload:
                val = _to_finite_float(payload[key])
                if val is not None:
                    return val

    single = _STANDALONE_DOLLAR_RE.match(cleaned)
    if single:
        return _signed_match(single.group(1), single.group(2), single.group(3))

    return None


def _run_balance_cli(
    binary: str,
    parser: Callable[[str], float | None],
    *,
    timeout: int,
    runner: Callable[..., Any],
) -> float | None:
    try:
        proc = runner(
            [binary, "balance"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return parser(getattr(proc, "stdout", "") or "")


def fetch_live_balances(
    *,
    timeout: int = 15,
    treg_binary_fn: Callable[[], str | None] | None = None,
    monid_binary_fn: Callable[[], str | None] | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[float | None, float | None]:
    """Run `treg balance` and `monid balance` CLIs with timeouts and return USD balances."""
    exec_fn = runner or subprocess.run
    treg_cmd = (treg_binary_fn or treg_bin)() or "treg"
    monid_cmd = (monid_binary_fn or monid_bin)() or "monid"
    treg_bal = _run_balance_cli(treg_cmd, parse_treg_balance_output, timeout=timeout, runner=exec_fn)
    monid_bal = _run_balance_cli(monid_cmd, parse_monid_balance_output, timeout=timeout, runner=exec_fn)
    return treg_bal, monid_bal


def _ensure_kv_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )


def ensure_enrichment_ledger_table(conn: sqlite3.Connection | None) -> None:
    """Idempotently create `enrichment_ledger` and its `(day, provider)` index."""
    if conn is None:
        return
    with locked_conn(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS enrichment_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                lead_key TEXT NOT NULL,
                cost_usd REAL NOT NULL,
                hit INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_enrichment_ledger_day_provider
            ON enrichment_ledger(day, provider)
            """
        )
        _ensure_kv_settings_table(conn)
        conn.commit()


def get_daily_enrichment_spend(
    conn: sqlite3.Connection | None,
    *,
    day: str | None = None,
    provider: str | None = None,
) -> float:
    """Return total recorded USD spend in `enrichment_ledger` for the UTC day."""
    if conn is None:
        return 0.0
    eff_day = _resolve_day(day)
    ensure_enrichment_ledger_table(conn)
    with locked_conn(conn):
        if provider:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM enrichment_ledger WHERE day = ? AND provider = ?",
                (eff_day, str(provider).strip().lower()),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM enrichment_ledger WHERE day = ?",
                (eff_day,),
            ).fetchone()
    return round(float(row[0] or 0.0), 6) if row else 0.0


def _kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    with locked_conn(conn):
        _ensure_kv_settings_table(conn)
        row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    with locked_conn(conn):
        _ensure_kv_settings_table(conn)
        conn.execute(
            """
            INSERT INTO kv_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, utc_now()),
        )
        conn.commit()


def _kv_delete(conn: sqlite3.Connection, key: str) -> None:
    with locked_conn(conn):
        _ensure_kv_settings_table(conn)
        conn.execute("DELETE FROM kv_settings WHERE key = ?", (key,))
        conn.commit()


def _balance_stop_kv_key(day: str) -> str:
    return f"budget_guard_low_balance_stop:{day}"


def _alert_kv_key(tier: str, day: str) -> str:
    return f"budget_guard_alert_{tier}:{day}"


def check_and_reserve_budget(
    conn: sqlite3.Connection | None,
    *,
    provider: str,
    estimated_cost: float,
    cfg: Mapping[str, Any] | None = None,
    day: str | None = None,
) -> tuple[bool, str]:
    """Check whether a paid call of `estimated_cost` is within today's budget and active balance guard."""
    est = _to_finite_float(estimated_cost)
    if est is None or est < 0:
        raise ValueError(f"estimated_cost must be a finite non-negative number, got {estimated_cost!r}")
    if est <= _EPS:
        return True, "free_tier"
    if conn is None:
        return False, "missing_db_connection"

    eff_day = _resolve_day(day)
    thresholds = _resolve_thresholds(cfg)
    ensure_enrichment_ledger_table(conn)

    stop_reason = _kv_get(conn, _balance_stop_kv_key(eff_day))
    if stop_reason:
        return False, stop_reason

    spent_today = get_daily_enrichment_spend(conn, day=eff_day)
    cap = thresholds.daily_cap_usd
    if cap <= 0.0 or spent_today >= cap - _EPS:
        return False, f"daily_budget_cap_reached: spent=${spent_today:.4f} >= cap=${cap:.2f}"
    if spent_today + est > cap + _EPS:
        return (
            False,
            f"daily_budget_cap_would_exceed: provider={provider} spent=${spent_today:.4f} + est=${est:.4f} > cap=${cap:.2f}",
        )
    return True, "ok"


def record_enrichment_spend(
    conn: sqlite3.Connection | None,
    *,
    provider: str,
    endpoint: str,
    lead_key: str,
    cost_usd: float,
    hit: bool,
    day: str | None = None,
) -> None:
    """Record one enrichment API attempt/spend row in `enrichment_ledger`."""
    parsed_cost = _to_finite_float(cost_usd)
    if parsed_cost is None or parsed_cost < 0:
        raise ValueError(f"cost_usd must be a finite non-negative number, got {cost_usd!r}")
    if conn is None:
        return

    eff_day = _resolve_day(day)
    clean_provider = str(provider or "").strip().lower() or "unknown"
    clean_endpoint = str(endpoint or "").strip()
    clean_lead_key = str(lead_key or "").strip()
    hit_flag = 1 if hit else 0

    ensure_enrichment_ledger_table(conn)
    with locked_conn(conn):
        conn.execute(
            """
            INSERT INTO enrichment_ledger (day, provider, endpoint, lead_key, cost_usd, hit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (eff_day, clean_provider, clean_endpoint, clean_lead_key, parsed_cost, hit_flag, utc_now()),
        )
        conn.commit()


def _coerce_balance_fn_result(
    fn: Callable[[], Any],
    parser: Callable[[str], float | None],
) -> float | None:
    try:
        raw = fn()
    except Exception as exc:
        logger.warning("Balance probe failed: %s", exc)
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        return parser(raw)
    return _to_finite_float(raw)


def _send_telegram_alert(
    conn: sqlite3.Connection | None,
    text: str,
    *,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    if os.environ.get("DISABLE_BOT_ALERTS") == "1":
        return False
    from app.digest import SEND_URL, resolve_colleague_bot_credentials

    token, chat_id = resolve_colleague_bot_credentials(conn)
    thresholds = _resolve_thresholds(cfg)
    if thresholds.alert_chat_id:
        chat_id = thresholds.alert_chat_id
    if not token or not chat_id:
        return False

    payload = json.dumps({"chat_id": str(chat_id), "text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SEND_URL.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = getattr(resp, "status", getattr(resp, "code", 200))
            body = resp.read()
        if status != 200:
            return False
        parsed = json.loads((body or b"{}").decode("utf-8"))
        return isinstance(parsed, dict) and parsed.get("ok") is True
    except (urllib.error.URLError, ValueError, OSError) as exc:
        logger.warning("Failed to send budget guard Telegram alert: %s", exc)
        return False


def _format_alert_message(
    *,
    day: str,
    spent_usd: float,
    thresholds: _BudgetThresholds,
    treg_balance_usd: float | None,
    monid_balance_usd: float | None,
    stop_reasons: list[str],
    warn_reasons: list[str],
    is_stop: bool,
) -> str:
    header = "🛑 Budget Guard STOP (платный Tier 2 остановлен)" if is_stop else "⚠️ Budget Guard Warning (низкий баланс API)"
    treg_str = f"${treg_balance_usd:.2f}" if treg_balance_usd is not None else "n/a"
    monid_str = f"${monid_balance_usd:.2f}" if monid_balance_usd is not None else "n/a"
    details = "; ".join(stop_reasons if is_stop else warn_reasons)
    return (
        f"{header}\n"
        f"Дата (UTC): {day}\n"
        f"Причина: {details}\n"
        f"Балансы: treg={treg_str}, monid={monid_str} "
        f"(warn < ${thresholds.low_balance_warn_usd:.2f}, stop < ${thresholds.low_balance_stop_usd:.2f})\n"
        f"Расход за день: ${spent_usd:.2f} / ${thresholds.daily_cap_usd:.2f}\n"
        f"Бесплатные слои (Слой 0, TG-админы, hh.ru, TG-вакансии) продолжают работу."
    )


def _dispatch_alert(
    conn: sqlite3.Connection,
    *,
    tier: str,
    day: str,
    text: str,
    notify_fn: Callable[[str], Any] | None,
    cfg: Mapping[str, Any] | None,
    mark_warn_also: bool = False,
) -> bool:
    if _kv_get(conn, _alert_kv_key(tier, day)) is not None:
        return False

    sent_ok = False
    if notify_fn is not None:
        try:
            res = notify_fn(text)
            sent_ok = res is not False
        except Exception as exc:
            logger.warning("Budget guard notify_fn failed: %s", exc)
            sent_ok = False
    else:
        sent_ok = _send_telegram_alert(conn, text, cfg=cfg)

    if not sent_ok:
        return False

    _kv_set(conn, _alert_kv_key(tier, day), utc_now())
    if mark_warn_also:
        _kv_set(conn, _alert_kv_key("warn", day), utc_now())
    return True


def check_balances_and_alert(
    conn: sqlite3.Connection | None,
    *,
    cfg: Mapping[str, Any] | None = None,
    treg_balance_fn: Callable[[], Any] | None = None,
    monid_balance_fn: Callable[[], Any] | None = None,
    notify_fn: Callable[[str], Any] | None = None,
    day: str | None = None,
    allow_unavailable: bool = False,
) -> EnrichmentBudgetState:
    """Check daily spend & provider balances, persist stop state, and send deduplicated TG alerts."""
    eff_day = _resolve_day(day)
    thresholds = _resolve_thresholds(cfg)
    spent_usd = get_daily_enrichment_spend(conn, day=eff_day)

    if treg_balance_fn is None and monid_balance_fn is None:
        treg_bal, monid_bal = fetch_live_balances()
    else:
        if treg_balance_fn is not None:
            treg_bal = _coerce_balance_fn_result(treg_balance_fn, parse_treg_balance_output)
        else:
            treg_bal = _run_balance_cli(
                treg_bin() or "treg",
                parse_treg_balance_output,
                timeout=15,
                runner=subprocess.run,
            )
        if monid_balance_fn is not None:
            monid_bal = _coerce_balance_fn_result(monid_balance_fn, parse_monid_balance_output)
        else:
            monid_bal = _run_balance_cli(
                monid_bin() or "monid",
                parse_monid_balance_output,
                timeout=15,
                runner=subprocess.run,
            )

    balance_stop_reasons: list[str] = []
    warn_reasons: list[str] = []
    has_measured_stop_balance = False
    for name, bal in (("treg", treg_bal), ("monid", monid_bal)):
        if bal is None:
            if not allow_unavailable:
                balance_stop_reasons.append(f"{name}_balance_unavailable")
            continue
        if bal < thresholds.low_balance_stop_usd:
            has_measured_stop_balance = True
            balance_stop_reasons.append(f"{name}=${bal:.2f} < ${thresholds.low_balance_stop_usd:.2f}")
        elif bal < thresholds.low_balance_warn_usd:
            warn_reasons.append(f"{name}=${bal:.2f} < ${thresholds.low_balance_warn_usd:.2f}")

    stop_reasons = list(balance_stop_reasons)
    if thresholds.daily_cap_usd <= 0.0 or spent_usd >= thresholds.daily_cap_usd - _EPS:
        stop_reasons.append(f"daily_cap=${spent_usd:.2f}/${thresholds.daily_cap_usd:.2f}")

    paid_allowed = not stop_reasons
    stop_reason = "; ".join(stop_reasons)

    alert_sent = False
    if conn is not None:
        ensure_enrichment_ledger_table(conn)
        if balance_stop_reasons:
            _kv_set(conn, _balance_stop_kv_key(eff_day), f"low_balance_stop: {'; '.join(balance_stop_reasons)}")
        else:
            _kv_delete(conn, _balance_stop_kv_key(eff_day))

        if not paid_allowed:
            msg = _format_alert_message(
                day=eff_day,
                spent_usd=spent_usd,
                thresholds=thresholds,
                treg_balance_usd=treg_bal,
                monid_balance_usd=monid_bal,
                stop_reasons=stop_reasons,
                warn_reasons=warn_reasons,
                is_stop=True,
            )
            alert_sent = _dispatch_alert(
                conn,
                tier="stop",
                day=eff_day,
                text=msg,
                notify_fn=notify_fn,
                cfg=cfg,
                mark_warn_also=bool(warn_reasons or has_measured_stop_balance),
            )
        elif warn_reasons:
            msg = _format_alert_message(
                day=eff_day,
                spent_usd=spent_usd,
                thresholds=thresholds,
                treg_balance_usd=treg_bal,
                monid_balance_usd=monid_bal,
                stop_reasons=stop_reasons,
                warn_reasons=warn_reasons,
                is_stop=False,
            )
            alert_sent = _dispatch_alert(
                conn,
                tier="warn",
                day=eff_day,
                text=msg,
                notify_fn=notify_fn,
                cfg=cfg,
            )

        _kv_set(
            conn,
            "budget_guard_last_state",
            json.dumps(
                {
                    "day": eff_day,
                    "spent_usd": spent_usd,
                    "daily_cap_usd": thresholds.daily_cap_usd,
                    "treg_balance_usd": treg_bal,
                    "monid_balance_usd": monid_bal,
                    "paid_allowed": paid_allowed,
                    "stop_reason": stop_reason,
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
            ),
        )

    return EnrichmentBudgetState(
        day=eff_day,
        spent_usd=spent_usd,
        daily_cap_usd=thresholds.daily_cap_usd,
        treg_balance_usd=treg_bal,
        monid_balance_usd=monid_bal,
        paid_allowed=paid_allowed,
        alert_sent=alert_sent,
        stop_reason=stop_reason,
    )
