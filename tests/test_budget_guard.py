"""Tests for leadgen/budget_guard.py."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from leadgen.budget_guard import (
    EnrichmentBudgetState,
    check_and_reserve_budget,
    check_balances_and_alert,
    ensure_enrichment_ledger_table,
    fetch_live_balances,
    get_daily_enrichment_spend,
    parse_monid_balance_output,
    parse_treg_balance_output,
    record_enrichment_spend,
)


def _mem_db() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_parse_treg_balance_output_formats() -> None:
    real_stdout = (
        "  Balance  $10.94   (10943609 micro-USD)   team multo\n\n"
        "  credit\n"
        "    purchased        $10.00 left  of $10.00 granted 2026-11-22\n"
        "  recent ledger\n"
        "    2026-11-22 16:36:20  settle      -$0.0016  crawl4ai.meta-ads.library.search\n"
    )
    assert parse_treg_balance_output(real_stdout) == 10.94
    assert parse_treg_balance_output("Balance  $0.49   (490000 micro-USD)") == 0.49
    assert parse_treg_balance_output("Balance: $1,234.56 (1234560000 micro-USD)") == 1234.56
    assert parse_treg_balance_output("Balance  -$0.25   (-250000 micro-USD)") == -0.25
    assert parse_treg_balance_output("\x1b[1mBalance\x1b[0m  \x1b[32m$2.05\x1b[0m") == 2.05
    assert parse_treg_balance_output("(450000 micro-USD)") == pytest.approx(0.45)
    assert parse_treg_balance_output(json.dumps({"balance_usd": 10.941959, "balance_micro": 10941959})) == pytest.approx(10.941959)
    assert parse_treg_balance_output(json.dumps({"balance_micro": 450000})) == pytest.approx(0.45)
    assert parse_treg_balance_output("") is None
    assert parse_treg_balance_output("Error: unauthorized") is None


def test_parse_monid_balance_output_formats() -> None:
    real_stdout = (
        "- Fetching balance...\n"
        "✔ Balance retrieved\n\n"
        "  Balance: $1.18 USD\n"
    )
    assert parse_monid_balance_output(real_stdout) == 1.18
    assert parse_monid_balance_output("Balance: $0.35 USD") == 0.35
    assert parse_monid_balance_output("Balance: $1,200.00 USD") == 1200.0
    assert parse_monid_balance_output("Balance: -$0.15 USD") == -0.15
    assert parse_monid_balance_output("Balance: 2.50 USD") == 2.5
    assert parse_monid_balance_output("\x1b[32m✔\x1b[0m Balance retrieved\n  Balance: \x1b[1m$1.18 USD\x1b[0m") == 1.18
    assert parse_monid_balance_output(json.dumps({"balance": {"value": 1.175194, "currency": "USD"}})) == pytest.approx(1.175194)
    assert parse_monid_balance_output("") is None
    assert parse_monid_balance_output("Error: missing credentials") is None


def test_fetch_live_balances_handles_success_and_failures() -> None:
    seen_calls: list[tuple[list[str], int | None]] = []

    def fake_runner(cmd, *, capture_output=True, text=True, timeout=None):
        seen_calls.append((list(cmd), timeout))
        if "treg" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0, stdout="  Balance  $10.94   (10943609 micro-USD)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="  Balance: $1.18 USD\n", stderr="")

    treg_bal, monid_bal = fetch_live_balances(
        timeout=9,
        treg_binary_fn=lambda: "/usr/local/bin/treg",
        monid_binary_fn=lambda: "/usr/local/bin/monid",
        runner=fake_runner,
    )
    assert treg_bal == 10.94
    assert monid_bal == 1.18
    assert seen_calls == [
        (["/usr/local/bin/treg", "balance"], 9),
        (["/usr/local/bin/monid", "balance"], 9),
    ]

    def timeout_on_treg(cmd, **kwargs):
        if "treg" in cmd[0]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        return subprocess.CompletedProcess(cmd, 0, stdout="Balance: $0.45 USD", stderr="")

    treg_bal2, monid_bal2 = fetch_live_balances(
        treg_binary_fn=lambda: "treg",
        monid_binary_fn=lambda: "monid",
        runner=timeout_on_treg,
    )
    assert treg_bal2 is None
    assert monid_bal2 == 0.45

    def missing_and_error(cmd, **kwargs):
        if "treg" in cmd[0]:
            raise FileNotFoundError("treg not found")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="failed")

    assert fetch_live_balances(
        treg_binary_fn=lambda: None,
        monid_binary_fn=lambda: None,
        runner=missing_and_error,
    ) == (None, None)


def test_ledger_schema_spend_recording_and_daily_cap() -> None:
    conn = _mem_db()
    ensure_enrichment_ledger_table(conn)
    ensure_enrichment_ledger_table(conn)  # idempotent

    indexes = [
        r[1]
        for r in conn.execute("PRAGMA index_list('enrichment_ledger')").fetchall()
    ]
    assert "idx_enrichment_ledger_day_provider" in indexes

    ok, reason = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.04, day="2026-11-22")
    assert ok is True
    assert reason == "ok"

    # Spend $4.96 across two providers (with IEEE-754 float edge case)
    record_enrichment_spend(
        conn,
        provider="treg",
        endpoint="treg.people.phone.find",
        lead_key="acme.com",
        cost_usd=4.80,
        hit=True,
        day="2026-11-22",
    )
    record_enrichment_spend(
        conn,
        provider="monid",
        endpoint="anyapi/facebook.page_contact",
        lead_key="acme-fb",
        cost_usd=0.16,
        hit=True,
        day="2026-11-22",
    )
    assert get_daily_enrichment_spend(conn, day="2026-11-22") == pytest.approx(4.96)
    assert get_daily_enrichment_spend(conn, day="2026-11-22", provider="treg") == pytest.approx(4.80)

    # Exactly $0.04 remaining fits inside $5.00 cap
    ok_exact, _ = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.04, day="2026-11-22")
    assert ok_exact is True

    # $0.15 super-whale would exceed $5.00 cap
    ok_over, reason_over = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.15, day="2026-11-22")
    assert ok_over is False
    assert "daily_budget_cap" in reason_over

    # Record final $0.04 -> cap hit at $5.00
    record_enrichment_spend(
        conn,
        provider="treg",
        endpoint="treg.people.phone.find",
        lead_key="beta.com",
        cost_usd=0.04,
        hit=True,
        day="2026-11-22",
    )
    ok_full, reason_full = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.01, day="2026-11-22")
    assert ok_full is False
    assert "daily_budget_cap_reached" in reason_full

    # Free layers ($0) ALWAYS continue even when cap is reached
    ok_free, reason_free = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.0, day="2026-11-22")
    assert ok_free is True
    assert reason_free == "free_tier"

    # Next UTC day resets budget; custom cfg overrides default $5.00 cap
    ok_next_day, _ = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.15, day="2026-11-23")
    assert ok_next_day is True
    ok_raised, _ = check_and_reserve_budget(
        conn,
        provider="treg",
        estimated_cost=0.15,
        cfg={"enrichment": {"daily_budget_cap_usd": 6.0}},
        day="2026-11-22",
    )
    assert ok_raised is True

    with pytest.raises(ValueError):
        check_and_reserve_budget(conn, provider="treg", estimated_cost=-0.01)
    with pytest.raises(ValueError):
        record_enrichment_spend(conn, provider="treg", endpoint="x", lead_key="k", cost_usd=-0.5, hit=False)


def test_check_balances_and_alert_warn_stop_and_dedup(tmp_path) -> None:
    db_path = tmp_path / "crm.db"
    conn = sqlite3.connect(db_path)
    alerts: list[str] = []

    # 1. Healthy balances -> no alert, paid allowed
    s1 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.94,
        monid_balance_fn=lambda: 2.00,  # exact boundary 2.00 -> no warning
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert isinstance(s1, EnrichmentBudgetState)
    assert s1.paid_allowed is True
    assert s1.alert_sent is False
    assert s1.stop_reason == ""
    assert alerts == []

    # 2. Balance < $2.00 (and >= $0.50) -> 1 warn alert, paid still allowed
    s2 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.94,
        monid_balance_fn=lambda: 1.18,
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert s2.paid_allowed is True
    assert s2.alert_sent is True
    assert len(alerts) == 1
    assert "Warning" in alerts[0]
    assert "monid=$1.18" in alerts[0]

    # 3. Repeated warn check same UTC day -> deduplicated via kv_settings
    s3 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.94,
        monid_balance_fn=lambda: 0.50,  # exact 0.50 boundary -> warn only, not stop
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert s3.paid_allowed is True
    assert s3.alert_sent is False
    assert len(alerts) == 1

    # 4. Escalation same day: balance drops < $0.50 -> hard stop + 1 stop alert
    s4 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.94,
        monid_balance_fn=lambda: 0.49,
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert s4.paid_allowed is False
    assert s4.alert_sent is True
    assert "monid=$0.49" in s4.stop_reason
    assert len(alerts) == 2
    assert "STOP" in alerts[1]

    # Low-balance stop blocks paid check_and_reserve_budget, but free ($0) continues
    ok_paid, stop_msg = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.04, day="2026-11-22")
    assert ok_paid is False
    assert "low_balance_stop" in stop_msg
    ok_free, _ = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.0, day="2026-11-22")
    assert ok_free is True

    # 5. Repeated stop check same day -> deduplicated
    s5 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 0.20,
        monid_balance_fn=lambda: 0.40,
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert s5.paid_allowed is False
    assert s5.alert_sent is False
    assert len(alerts) == 2

    # 6. Top-up clears low-balance stop so paid calls resume
    s6 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 25.00,
        monid_balance_fn=lambda: 15.00,
        notify_fn=alerts.append,
        day="2026-11-22",
    )
    assert s6.paid_allowed is True
    assert s6.stop_reason == ""
    ok_resumed, _ = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.04, day="2026-11-22")
    assert ok_resumed is True

    # 7. Next UTC day sends fresh alert
    s7 = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 0.30,
        monid_balance_fn=lambda: 15.00,
        notify_fn=alerts.append,
        day="2026-11-23",
    )
    assert s7.paid_allowed is False
    assert s7.alert_sent is True
    assert len(alerts) == 3

    # Zero JSON sidecars created on disk (only crm.db in tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["crm.db"]


def test_daily_cap_stop_alert_and_notify_failure_retry() -> None:
    conn = _mem_db()
    record_enrichment_spend(
        conn,
        provider="treg",
        endpoint="treg.people.phone.find",
        lead_key="whale.io",
        cost_usd=5.0,
        hit=True,
        day="2026-11-22",
    )

    def failing_notify(_text: str) -> bool:
        return False

    # Failed notification must NOT crash and must NOT lock out retry
    s_fail = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 20.0,
        monid_balance_fn=lambda: 20.0,
        notify_fn=failing_notify,
        day="2026-11-22",
    )
    assert s_fail.paid_allowed is False
    assert s_fail.alert_sent is False
    assert "daily_cap=" in s_fail.stop_reason

    def raising_notify(_text: str) -> None:
        raise RuntimeError("tg timeout")

    s_err = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 20.0,
        monid_balance_fn=lambda: 20.0,
        notify_fn=raising_notify,
        day="2026-11-22",
    )
    assert s_err.paid_allowed is False
    assert s_err.alert_sent is False

    sent: list[str] = []
    s_retry = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 20.0,
        monid_balance_fn=lambda: 20.0,
        notify_fn=sent.append,
        day="2026-11-22",
    )
    assert s_retry.paid_allowed is False
    assert s_retry.alert_sent is True
    assert len(sent) == 1

    # Third call is deduplicated
    s_dedup = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 20.0,
        monid_balance_fn=lambda: 20.0,
        notify_fn=sent.append,
        day="2026-11-22",
    )
    assert s_dedup.alert_sent is False
    assert len(sent) == 1


def test_default_telegram_alert_uses_cfg_alert_chat_id_and_bot_token(monkeypatch) -> None:
    conn = _mem_db()
    ensure_enrichment_ledger_table(conn)
    conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES ('bot_token', '123:TESTTOKEN', '2026-11-22T00:00:00')"
    )
    conn.commit()
    monkeypatch.delenv("DISABLE_BOT_ALERTS", raising=False)
    monkeypatch.delenv("CRM_E2E_SANDBOX", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    captured: list[tuple[str, dict]] = []

    class _FakeResp:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 42}}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):
        captured.append((req.full_url, json.loads(req.data.decode("utf-8"))))
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    state = check_balances_and_alert(
        conn,
        cfg={"enrichment": {"alert_chat_id": "-100999888"}},
        treg_balance_fn=lambda: "Balance  $0.25   (250000 micro-USD)",
        monid_balance_fn=lambda: "Balance: $5.00 USD",
        day="2026-11-22",
    )
    assert state.paid_allowed is False
    assert state.alert_sent is True
    assert state.treg_balance_usd == 0.25
    assert state.monid_balance_usd == 5.0
    assert len(captured) == 1
    assert captured[0][0] == "https://api.telegram.org/bot123:TESTTOKEN/sendMessage"
    assert captured[0][1]["chat_id"] == "-100999888"
    assert "STOP" in captured[0][1]["text"]


def test_app_db_thread_safe_connection_compatibility(tmp_path) -> None:
    from app.db import connect, init_db

    conn = connect(tmp_path / "crm.db")
    try:
        init_db(conn)
        ensure_enrichment_ledger_table(conn)
        ok, reason = check_and_reserve_budget(conn, provider="treg", estimated_cost=0.04, day="2026-11-22")
        assert ok is True
        assert reason == "ok"
        record_enrichment_spend(
            conn,
            provider="treg",
            endpoint="treg.people.phone.find",
            lead_key="lead-1",
            cost_usd=0.04,
            hit=True,
            day="2026-11-22",
        )
        assert get_daily_enrichment_spend(conn, day="2026-11-22") == pytest.approx(0.04)
        state = check_balances_and_alert(
            conn,
            treg_balance_fn=lambda: 10.0,
            monid_balance_fn=lambda: 5.0,
            notify_fn=lambda _t: True,
            day="2026-11-22",
        )
        assert state.paid_allowed is True
        assert state.spent_usd == pytest.approx(0.04)
    finally:
        conn.close()


def test_check_balances_fail_closed_when_unavailable_unless_opted_in(monkeypatch) -> None:
    conn = _mem_db()
    alerts: list[str] = []

    # 1. Default allow_unavailable=False fails closed when treg balance returns None
    s_treg_none = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: None,
        monid_balance_fn=lambda: 10.0,
        notify_fn=lambda msg: alerts.append(msg) or True,
        day="2026-11-22",
    )
    assert s_treg_none.paid_allowed is False
    assert s_treg_none.alert_sent is True
    assert "treg_balance_unavailable" in s_treg_none.stop_reason
    assert len(alerts) == 1

    ok_paid, stop_reason = check_and_reserve_budget(
        conn, provider="treg", estimated_cost=0.04, day="2026-11-22"
    )
    assert ok_paid is False
    assert "treg_balance_unavailable" in stop_reason
    ok_free, free_reason = check_and_reserve_budget(
        conn, provider="treg", estimated_cost=0.0, day="2026-11-22"
    )
    assert ok_free is True
    assert free_reason == "free_tier"

    # 2. Live CLI failure / timeout via fetch_live_balances also fails closed
    monkeypatch.setattr("leadgen.budget_guard.fetch_live_balances", lambda: (None, None))
    s_cli_fail = check_balances_and_alert(
        conn,
        notify_fn=lambda msg: alerts.append(msg) or True,
        day="2026-11-23",
    )
    assert s_cli_fail.paid_allowed is False
    assert "treg_balance_unavailable" in s_cli_fail.stop_reason
    assert "monid_balance_unavailable" in s_cli_fail.stop_reason
    ok_paid_23, _ = check_and_reserve_budget(
        conn, provider="monid", estimated_cost=0.01, day="2026-11-23"
    )
    assert ok_paid_23 is False

    # 3. Unparseable CLI stdout also fails closed
    s_bad_stdout = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: 10.0,
        monid_balance_fn=lambda: "Error: unauthorized",
        notify_fn=lambda msg: alerts.append(msg) or True,
        day="2026-11-24",
    )
    assert s_bad_stdout.paid_allowed is False
    assert "monid_balance_unavailable" in s_bad_stdout.stop_reason

    # 4. allow_unavailable=True with None balances clears unavailable stop & only checks daily cap
    s_opted_in = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: None,
        monid_balance_fn=lambda: None,
        notify_fn=lambda msg: alerts.append(msg) or True,
        day="2026-11-22",
        allow_unavailable=True,
    )
    assert s_opted_in.paid_allowed is True
    assert s_opted_in.stop_reason == ""
    ok_paid_after, _ = check_and_reserve_budget(
        conn, provider="treg", estimated_cost=0.04, day="2026-11-22"
    )
    assert ok_paid_after is True

    # Even with allow_unavailable=True, hitting daily_cap still stops paid calls
    record_enrichment_spend(
        conn,
        provider="treg",
        endpoint="treg.people.phone.find",
        lead_key="cap.ai",
        cost_usd=5.0,
        hit=True,
        day="2026-11-25",
    )
    s_cap = check_balances_and_alert(
        conn,
        treg_balance_fn=lambda: None,
        monid_balance_fn=lambda: None,
        notify_fn=lambda msg: alerts.append(msg) or True,
        day="2026-11-25",
        allow_unavailable=True,
    )
    assert s_cap.paid_allowed is False
    assert "daily_cap=" in s_cap.stop_reason


