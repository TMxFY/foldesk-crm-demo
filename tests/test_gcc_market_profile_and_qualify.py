from __future__ import annotations

import asyncio
import copy
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
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
    assert effective["enrichment"]["enrichment_sub_budget_usd"] == cfg["follower_graph"]["enrichment_sub_budget_usd"]
    assert effective["enrichment"]["discovery_sub_budget_usd"] == cfg["follower_graph"]["discovery_sub_budget_usd"]
    assert effective["enrichment"]["cpl_stop_usd"] == cfg["follower_graph"]["cpl_stop_usd"]
    assert effective["enrichment"]["contactout_for_all_fit_leads"] is cfg["follower_graph"]["contactout_for_all_fit_leads"]

    # Rollback to global restores base user settings without loss
    cfg["active_market_profile"] = "global"
    restored = leadgen_settings.resolve_effective_market_config(cfg)
    assert restored["whales"]["geos"] == ["US", "CA"]
    assert restored["enrichment"]["low_balance_warn_usd"] == 1.5


def test_clinic_whitelist_admits_large_gcc_clinics_keeps_solo_salon_banned() -> None:
    captured_states: list[tuple[str, dict, dict]] = []

    async def fake_run(states, cache_path):
        captured_states.extend(states)
        return [
            {
                "typesafe_fit": 0.9,
                "typesafe_category": "E-com / D2C",
                "typesafe_role": "Founder",
            }
            for _ in states
        ]

    with patch.object(typesafe_leads, "_run", side_effect=fake_run):
        res = asyncio.run(
            typesafe_leads.qualify_creator_rows(
                [{"handle": "adamhealth", "name": "Adam Health", "bio": "Telehealth & lab tests in Riyadh"}]
            )
        )

    assert len(res) == 1
    assert len(captured_states) == 1
    instructions = captured_states[0][2]["questions"]["fit"].instructions
    assert "aesthetic, dental, dermatology" in instructions
    assert "telehealth" in instructions
    assert "lab-testing" in instructions
    assert "multi-location" in instructions
    assert "small solo local salon/barbershop" in instructions

    assert "Large aesthetic, dental, dermatology, telehealth" in tg_llm_qualify.SYSTEM_PROMPT
    assert "solopreneurs" in tg_llm_qualify.SYSTEM_PROMPT.lower()
    assert "b2b sales agencies" in tg_llm_qualify.SYSTEM_PROMPT.lower()


def test_tg_llm_qualify_retires_timed_out_model_and_enforces_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tg_llm_qualify.TIMEOUT <= 35
    assert tg_llm_qualify.DEADLINE_SEC <= 220

    retired: list[tuple[str, int, str]] = []

    def fake_note_failure(model: str, status: int, detail: str) -> None:
        retired.append((model, status, detail))

    def fake_call_llm(batch: list[dict], model: str):
        if model == "slow-model":
            raise requests.Timeout("read timed out")
        return [
            {
                "is_lead": True,
                "icp_score": 88,
                "category": "E-com / D2C",
                "role": "Founder",
                "project": "AdamHealthApp",
                "project_url": "https://adamhealth.sa",
                "what_they_do": "Telehealth & skincare brand",
                "monetization": "ecom",
                "short_form_fit": True,
            }
            for _ in batch
        ]

    with (
        patch.object(tg_llm_qualify, "chain", return_value=["slow-model", "fast-model"]),
        patch.object(tg_llm_qualify, "note_failure", side_effect=fake_note_failure),
        patch.object(tg_llm_qualify, "call_llm", side_effect=fake_call_llm),
    ):
        out = tg_llm_qualify.call_llm_retry([{"idx": 0, "handle": "x", "text": "hello"}])

    assert len(out) == 1
    assert out[0]["is_lead"] is True
    assert out[0]["icp_score"] == 88
    assert retired.count(("slow-model", 408, "timeout")) == 2

    # Deadline enforcement in qualify_loaded keeps completed results & stops before exhausting wall clock
    monkeypatch.setenv("RELAYFAST_API_KEY", "test-key")
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))
    calls = 0

    def slow_retry(batch: list[dict]):
        nonlocal calls
        calls += 1
        if batch[0].get("username") != "user_0":
            time.sleep(0.25)
        return [
            {
                "is_lead": True,
                "icp_score": 88,
                "category": "E-com / D2C",
                "role": "Founder",
                "project": r["project"],
                "project_url": r["project_url"],
                "what_they_do": "Skincare D2C brand",
                "monetization": "ecom",
                "short_form_fit": True,
            }
            for r in batch
        ]

    rows = [
        {
            "name": f"Founder {i}",
            "username": f"user_{i}",
            "bio": f"Founder of AdamHealthApp {i} selling skincare online",
            "project": f"AdamHealthApp {i}",
            "project_url": "https://adamhealth.sa",
            "segment": "b2b",
        }
        for i in range(6)
    ]
    with (
        patch.object(tg_llm_qualify, "DEADLINE_SEC", 0.05),
        patch.object(tg_llm_qualify, "call_llm_retry", side_effect=slow_retry),
        patch.object(tg_llm_qualify, "_run_typesafe_safely", return_value=[None] * len(rows)),
    ):
        qualified_rows, dropped = tg_llm_qualify.qualify_loaded(rows, batch_size=2)

    assert calls >= 1
    assert len(qualified_rows) >= 2
    assert dropped >= 2


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
        if endpoint == "contactout.people.phone.available":
            return {"available": True}
        if endpoint in ("crawl4ai.people.phone.find", "treg.people.phone.find"):
            return {}
        if endpoint == "contactout.people.contact.phone":
            return {"phones": ["+966501234567"]}
        if endpoint == "hlrlookup.people.phone.validate":
            return {"telephone_number_type": "MOBILE"}
        return {}

    dm = {"founder_name": "Ali", "founder_linkedin": "https://www.linkedin.com/in/ali-test"}

    # Flag disabled -> ContactOut phone not called when icp_score < CONTACTOUT_VERIFIED_MIN_ICP
    res_off = contact_waterfall._run_phone_waterfall_for_dm(
        dm,
        dom="adamhealth.sa",
        lead_key="adamhealth.sa",
        active_treg=fake_treg,
        remaining_fn=lambda: 1.0,
        is_super_whale=False,
        hlr_cache={},
        icp_score=78,
        cfg={"follower_graph": {"contactout_for_all_fit_leads": False}},
        jev_fit=1.9,
    )
    assert "contactout.people.contact.phone" not in calls
    assert res_off["phone"] == ""

    # Flag enabled + jev_fit < 1.5 -> ContactOut phone still not called
    calls.clear()
    res_low_fit = contact_waterfall._run_phone_waterfall_for_dm(
        dm,
        dom="adamhealth.sa",
        lead_key="adamhealth.sa",
        active_treg=fake_treg,
        remaining_fn=lambda: 1.0,
        is_super_whale=False,
        hlr_cache={},
        icp_score=78,
        cfg={"follower_graph": {"contactout_for_all_fit_leads": True}},
        jev_fit=1.4,
    )
    assert "contactout.people.contact.phone" not in calls
    assert res_low_fit["phone"] == ""

    # Flag enabled + jev_fit >= 1.5 -> ContactOut phone invoked even with icp_score=78 (< 80)
    calls.clear()
    res_on = contact_waterfall._run_phone_waterfall_for_dm(
        dm,
        dom="adamhealth.sa",
        lead_key="adamhealth.sa",
        active_treg=fake_treg,
        remaining_fn=lambda: 1.0,
        is_super_whale=False,
        hlr_cache={},
        icp_score=78,
        cfg={"follower_graph": {"contactout_for_all_fit_leads": True}},
        jev_fit=1.7,
    )
    assert "contactout.people.contact.phone" in calls
    assert res_on["phone"] == "+966501234567"
    assert res_on["line_type"] == "MOBILE"
