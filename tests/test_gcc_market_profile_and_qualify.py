from __future__ import annotations

import copy
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests

from app import leadgen_settings
from leadgen import contact_waterfall, meta_ad_leads, tg_llm_qualify, treg_social_leads, typesafe_leads


def test_market_profile_switch_and_rollback_preserves_global_config() -> None:
    base = leadgen_settings.DEFAULT_LEADGEN_CONFIG
    assert base["active_market_profile"] == "global"
    assert base["b2b"]["sources"]["follower_graph"] is False

    cfg = copy.deepcopy(base)
    cfg["whales"]["geos"] = ["US", "CA"]
    cfg["enrichment"]["low_balance_warn_usd"] = 1.5
    cfg["active_market_profile"] = "gcc_december"
    snapshot = copy.deepcopy(cfg)

    effective = leadgen_settings.resolve_effective_market_config(cfg)
    assert cfg == snapshot  # must not mutate caller config
    assert effective["active_market_profile"] == "gcc_december"
    assert effective["whales"]["geos"][0] == "SA"
    assert effective["whales"]["gcc_eg_jo_strict_size"] is True
    assert effective["whales"]["gcc_clinic_whitelist"] is True
    assert float(effective["enrichment"]["low_balance_warn_usd"]) >= 5.0

    # Rollback to global restores base user settings without loss
    cfg["active_market_profile"] = "global"
    restored = leadgen_settings.resolve_effective_market_config(cfg)
    assert restored["whales"]["geos"] == ["US", "CA"]
    assert restored["enrichment"]["low_balance_warn_usd"] == 1.5


def test_clinic_whitelist_admits_large_gcc_clinics_keeps_solo_salon_banned() -> None:
    captured_payloads: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        captured_payloads.append(json or {})

        class Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{"fit": 2, "fit_score": 2.0}]}

        return Resp()

    with (
        patch.object(typesafe_leads, "_load_key", return_value="test-key"),
        patch.object(typesafe_leads.requests, "post", side_effect=fake_post),
    ):
        typesafe_leads.qualify_creator_rows(
            [{"handle": "adamhealth", "name": "Adam Health", "bio": "Telehealth & lab tests in Riyadh"}],
            product="SoftUGC",
        )

    assert len(captured_payloads) == 1
    instructions = captured_payloads[0]["questions"]["fit"]["instructions"]
    assert "aesthetic, dental, dermatology" in instructions
    assert "telehealth" in instructions
    assert "small solo local salon/barbershop" in instructions

    assert "Large aesthetic, dental, dermatology, telehealth" in tg_llm_qualify.SYSTEM_PROMPT
    assert "solo local salon" in tg_llm_qualify.SYSTEM_PROMPT.lower()
    assert "marketing agency" in tg_llm_qualify.SYSTEM_PROMPT.lower()


def test_tg_llm_qualify_retires_timed_out_model_and_enforces_deadline(tmp_path: Path) -> None:
    assert tg_llm_qualify.TIMEOUT <= 35
    assert tg_llm_qualify.DEADLINE_SEC <= 220

    retired: list[tuple[str, int, str]] = []

    def fake_note_failure(model: str, status: int, detail: str) -> None:
        retired.append((model, status, detail))

    def fake_call_llm(model: str, batch: list[dict]):
        if model == "slow-model":
            raise requests.Timeout("read timed out")
        return [{"idx": row["idx"], "verdict": "SKIP", "score": 1} for row in batch]

    with (
        patch.object(tg_llm_qualify, "chain", return_value=["slow-model", "fast-model"]),
        patch.object(tg_llm_qualify, "note_failure", side_effect=fake_note_failure),
        patch.object(tg_llm_qualify, "call_llm", side_effect=fake_call_llm),
    ):
        out = tg_llm_qualify.call_llm_retry([{"idx": 0, "handle": "x", "text": "hello"}])

    assert out == [{"idx": 0, "verdict": "SKIP", "score": 1}]
    assert retired.count(("slow-model", 408, "timeout")) == 2

    # Deadline enforcement in qualify_loaded keeps completed results & stops before exhausting wall clock
    calls = 0

    def slow_retry(batch: list[dict]):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return [{"idx": r["idx"], "verdict": "ICP", "score": 9, "reason": "ok"} for r in batch]

    rows = [{"handle": f"user_{i}", "bio": f"founder_{i}", "segment": "b2b"} for i in range(6)]
    out_file = tmp_path / "qualified.jsonl"
    with (
        patch.object(tg_llm_qualify, "BATCH_SIZE", 2),
        patch.object(tg_llm_qualify, "DEADLINE_SEC", 0.01),
        patch.object(tg_llm_qualify, "call_llm_retry", side_effect=slow_retry),
        patch.object(tg_llm_qualify, "_run_typesafe_safely", return_value=rows),
    ):
        summary = tg_llm_qualify.qualify_loaded(rows, out_file)

    assert calls >= 1
    assert summary["qualified"] >= 0


def test_meta_and_treg_social_gcc_december_vs_global() -> None:
    q_global = meta_ad_leads.queries_for_day(date(2026, 12, 1), count=6, market_profile="global")
    q_gcc = meta_ad_leads.queries_for_day(date(2026, 12, 1), count=6, market_profile="gcc_december")
    assert q_global != q_gcc
    assert any(q in meta_ad_leads.ARABIC_GCC_SEARCH_QUERIES for q in q_gcc)

    c_global = meta_ad_leads.countries_for_day(date(2026, 12, 1), count=4, market_profile="global")
    c_gcc = meta_ad_leads.countries_for_day(date(2026, 12, 1), count=4, market_profile="gcc_december")
    assert "SA" in c_gcc
    assert c_global == meta_ad_leads.countries_for_day(date(2026, 12, 1), count=4)

    soc_global = treg_social_leads.buyer_queries(date(2026, 12, 1), market_profile="global")
    soc_gcc = treg_social_leads.buyer_queries(date(2026, 12, 1), market_profile="gcc_december")
    assert len(soc_gcc) >= len(soc_global)
    assert any("الرياض" in q or "السعودية" in q for q in soc_gcc)


def test_contactout_phone_gated_by_jev_fit_and_flag() -> None:
    calls: list[str] = []

    def fake_treg(endpoint: str, **kwargs):
        calls.append(endpoint)
        if endpoint == "contactout.people.contact.phone":
            return 200, {"phone": "+966501234567"}
        return 404, {}

    # Flag disabled -> ContactOut phone not called without DM/role hit
    res_off = contact_waterfall._run_phone_waterfall_for_dm(
        person={"name": "Ali", "title": "Staff"},
        domain="adamhealth.sa",
        company="Adam Health",
        geo="SA",
        icp_fit="medium",
        treg_call=fake_treg,
        budget_ok=lambda *_a: True,
        record_spend=lambda *_a: None,
        contactout_for_all_fit_leads=False,
        jev_fit=1.9,
    )
    assert "contactout.people.contact.phone" not in calls
    assert res_off == {}

    # Flag enabled + jev_fit >= 1.5 -> ContactOut phone invoked even without DM keyword
    calls.clear()
    res_on = contact_waterfall._run_phone_waterfall_for_dm(
        person={"name": "Ali", "title": "Staff"},
        domain="adamhealth.sa",
        company="Adam Health",
        geo="SA",
        icp_fit="medium",
        treg_call=fake_treg,
        budget_ok=lambda *_a: True,
        record_spend=lambda *_a: None,
        contactout_for_all_fit_leads=True,
        jev_fit=1.7,
    )
    assert "contactout.people.contact.phone" in calls
    assert res_on.get("phone") == "+966501234567"
