from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from leadgen import treg_enrich


def test_find_company_decision_makers_prioritizes_slot1_and_slot2_and_skips_non_dm():
    calls: list[str] = []

    def fake_treg(ep: str, query: dict | None = None, **kw):
        assert ep == "crawl4ai.web.search"
        calls.append((query or {}).get("q", ""))
        return {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/eng-dave",
                    "title": "Dave Kim - Software Engineer at NovaLabs | LinkedIn",
                    "snippet": "Building backend at novalabs.ai",
                },
                {
                    "url": "https://www.linkedin.com/in/cofounder-bob",
                    "title": "Bob Lee - Co-Founder at NovaLabs | LinkedIn",
                    "snippet": "Co-Founder at novalabs.ai",
                },
                {
                    "url": "https://www.linkedin.com/in/founder-alice",
                    "title": "Alice Chen - Founder & CEO at NovaLabs | LinkedIn",
                    "snippet": "Founder & CEO at novalabs.ai alice@novalabs.ai",
                },
                {
                    "url": "https://www.linkedin.com/in/cmo-carol",
                    "title": "Carol Vance - Head of Growth at NovaLabs | LinkedIn",
                    "snippet": "Scaling paid and organic growth at novalabs.ai carol@novalabs.ai",
                },
            ]
        }

    res = treg_enrich.find_company_decision_makers(
        "NovaLabs", "novalabs.ai", limit=2, treg_fn=fake_treg
    )

    assert len(calls) == 1
    assert len(res) == 2

    slot1, slot2 = res
    assert slot1["founder_name"] == "Alice Chen"
    assert slot1["founder_linkedin"] == "https://www.linkedin.com/in/founder-alice"
    assert slot1["work_email"] == "alice@novalabs.ai"
    assert slot1["project_url"] == "https://novalabs.ai"
    assert slot1["role_bucket"] == "founder"
    assert "Founder" in slot1["role_title"]

    assert slot2["founder_name"] == "Carol Vance"
    assert slot2["founder_linkedin"] == "https://www.linkedin.com/in/cmo-carol"
    assert slot2["work_email"] == "carol@novalabs.ai"
    assert slot2["project_url"] == "https://novalabs.ai"
    assert slot2["role_bucket"] == "marketing"
    assert slot2["role_title"] == "Head of Growth"


def test_find_company_decision_makers_optimal_dual_role_assignment():
    # First hit has dual role (Co-Founder & CMO), second hit is pure Founder & CEO.
    # Optimal assignment gives pure Founder Slot 1 and dual-role person Slot 2.
    def fake_treg(ep: str, query: dict | None = None, **kw):
        return {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/jane-doe",
                    "title": "Jane Doe - Co-Founder & CMO at Acme | LinkedIn",
                    "snippet": "Leading marketing at acme.io",
                },
                {
                    "url": "https://www.linkedin.com/in/bob-smith",
                    "title": "Bob Smith - Founder & CEO at Acme | LinkedIn",
                    "snippet": "Building acme.io",
                },
            ]
        }

    res = treg_enrich.find_company_decision_makers("Acme", "acme.io", limit=2, treg_fn=fake_treg)
    assert len(res) == 2
    assert res[0]["founder_name"] == "Bob Smith"
    assert res[0]["role_bucket"] == "founder"
    assert res[1]["founder_name"] == "Jane Doe"
    assert res[1]["role_bucket"] == "marketing"


def test_find_company_decision_makers_deduplicates_linkedin_and_name():
    def fake_treg(ep: str, query: dict | None = None, **kw):
        return {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/Jane-Doe/?trk=public_profile",
                    "title": "Jane Doe - Founder at PulseFit | LinkedIn",
                    "snippet": "pulsefit.ai",
                },
                {
                    "url": "https://uk.linkedin.com/in/jane-doe/",
                    "title": "J. Doe - Co-Founder & CMO at PulseFit | LinkedIn",
                    "snippet": "pulsefit.ai jane@pulsefit.ai",
                },
                {
                    "url": "https://www.linkedin.com/in/jane-doe-alt",
                    "title": "jane   doe - VP Marketing at PulseFit | LinkedIn",
                    "snippet": "pulsefit.ai",
                },
                {
                    "url": "https://www.linkedin.com/in/mark-taylor",
                    "title": "Mark Taylor - Head of UA at PulseFit | LinkedIn",
                    "snippet": "pulsefit.ai",
                },
            ]
        }

    res = treg_enrich.find_company_decision_makers(
        "PulseFit", "pulsefit.ai", limit=2, treg_fn=fake_treg
    )
    assert len(res) == 2
    assert res[0]["founder_name"] == "Jane Doe"
    assert res[0]["founder_linkedin"] == "https://www.linkedin.com/in/Jane-Doe"
    assert res[0]["work_email"] == "jane@pulsefit.ai"
    assert res[1]["founder_name"] == "Mark Taylor"
    assert res[1]["role_bucket"] == "marketing"


def test_find_company_decision_makers_empty_names_do_not_collide():
    def fake_treg(ep: str, query: dict | None = None, **kw):
        return {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/tarotflow-ceo",
                    "title": "TarotFlow CEO | LinkedIn",
                    "snippet": "tarotflow.app",
                },
                {
                    "url": "https://www.linkedin.com/in/tarotflow-cmo",
                    "title": "TarotFlow CMO | LinkedIn",
                    "snippet": "tarotflow.app",
                },
            ]
        }

    res = treg_enrich.find_company_decision_makers(
        "TarotFlow", "tarotflow.app", limit=2, treg_fn=fake_treg
    )
    assert len(res) == 2
    assert res[0]["founder_name"] == ""
    assert res[0]["founder_linkedin"] == "https://www.linkedin.com/in/tarotflow-ceo"
    assert res[0]["role_bucket"] == "founder"
    assert res[1]["founder_name"] == ""
    assert res[1]["founder_linkedin"] == "https://www.linkedin.com/in/tarotflow-cmo"
    assert res[1]["role_bucket"] == "marketing"


def test_find_company_decision_makers_followup_search_fills_missing_slot():
    queries: list[str] = []

    def fake_treg(ep: str, query: dict | None = None, **kw):
        q = (query or {}).get("q", "")
        queries.append(q)
        if "Founder" in q and "CMO" in q:
            return {
                "results": [
                    {
                        "url": "https://www.linkedin.com/in/owner-sam",
                        "title": "Sam Patel - Business Owner at FinBoost | LinkedIn",
                        "snippet": "Building https://finboost.co",
                    }
                ]
            }
        if "Head of Growth" in q:
            return {
                "results": [
                    {
                        "url": "https://www.linkedin.com/in/maya-dir",
                        "title": "Maya Lin - Marketing Director at FinBoost | LinkedIn",
                        "snippet": "Growth at finboost.co",
                    }
                ]
            }
        return {"results": []}

    res = treg_enrich.find_company_decision_makers("FinBoost", "", limit=2, treg_fn=fake_treg)
    assert len(queries) == 2
    assert len(res) == 2
    assert res[0]["founder_name"] == "Sam Patel"
    assert res[0]["role_bucket"] == "founder"
    assert res[0]["project_url"] == "https://finboost.co"
    assert res[1]["founder_name"] == "Maya Lin"
    assert res[1]["role_bucket"] == "marketing"
    assert res[1]["project_url"] == "https://finboost.co"


def test_find_company_decision_makers_followup_fills_missing_slot1_and_handles_exhausted_mock():
    mock_fn = Mock(
        side_effect=[
            {
                "output": {
                    "results": [
                        {
                            "url": "https://www.linkedin.com/in/vp-elena",
                            "title": "Elena Rostova - VP of Marketing at ClipPulse | LinkedIn",
                            "snippet": "clippulse.ai",
                        }
                    ]
                }
            },
            [
                {
                    "url": "https://www.linkedin.com/in/ceo-dan",
                    "title": "Dan Brooks - CEO at ClipPulse | LinkedIn",
                    "snippet": "clippulse.ai",
                }
            ],
        ]
    )

    res = treg_enrich.find_company_decision_makers(
        "ClipPulse", "clippulse.ai", limit=2, treg_fn=mock_fn
    )
    assert len(res) == 2
    assert res[0]["founder_name"] == "Dan Brooks"
    assert res[0]["role_bucket"] == "founder"
    assert res[1]["founder_name"] == "Elena Rostova"
    assert res[1]["role_bucket"] == "marketing"

    # Single-item side_effect exhausting on follow-up should not raise StopIteration
    one_shot = Mock(
        side_effect=[
            {
                "results": [
                    {
                        "url": "https://www.linkedin.com/in/ceo-only",
                        "title": "Leo King - CEO at ClipPulse | LinkedIn",
                        "snippet": "clippulse.ai",
                    }
                ]
            }
        ]
    )
    res2 = treg_enrich.find_company_decision_makers(
        "ClipPulse", "clippulse.ai", limit=2, treg_fn=one_shot
    )
    assert len(res2) == 1
    assert res2[0]["founder_name"] == "Leo King"


def test_find_company_decision_makers_rejects_false_positive_roles():
    def fake_treg(ep: str, query: dict | None = None, **kw):
        return {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/po-tim",
                    "title": "Tim Ross - Product Owner at Acme | LinkedIn",
                    "snippet": "Product Owner for agile sprint at acme.io",
                },
                {
                    "url": "https://www.linkedin.com/in/ea-sue",
                    "title": "Sue Park - Executive Assistant to CEO at Acme | LinkedIn",
                    "snippet": "Supporting CEO at acme.io",
                },
                {
                    "url": "https://www.linkedin.com/in/intern-max",
                    "title": "Max Miller - Marketing Intern at Acme | LinkedIn",
                    "snippet": "Summer intern at acme.io",
                },
                {
                    "url": "https://www.linkedin.com/in/ex-ceo-ron",
                    "title": "Ron Hall - Former CEO at OtherCo | LinkedIn",
                    "snippet": "Software Engineer at acme.io",
                },
                {
                    "url": "https://www.linkedin.com/in/real-vp",
                    "title": "Nina Gomez - VP Marketing at Acme | LinkedIn",
                    "snippet": "Driving revenue at acme.io",
                },
            ]
        }

    res = treg_enrich.find_company_decision_makers("Acme", "acme.io", limit=2, treg_fn=fake_treg)
    assert len(res) == 1
    assert res[0]["founder_name"] == "Nina Gomez"
    assert res[0]["role_bucket"] == "marketing"
    assert res[0]["role_title"] == "VP Marketing"


def test_find_company_decision_makers_required_keys_and_edge_inputs():
    assert treg_enrich.find_company_decision_makers("", "") == []
    assert treg_enrich.find_company_decision_makers("The App", "") == []
    assert treg_enrich.find_company_decision_makers("Acme", "acme.io", limit=0) == []
    assert treg_enrich.find_company_decision_makers("Acme", "acme.io", treg_fn=lambda ep, **kw: None) == []

    # SKIP_HOSTS domain like whop.com must not leak as project_url
    res = treg_enrich.find_company_decision_makers(
        "AlphaDrop",
        "whop.com",
        limit=2,
        treg_fn=lambda ep, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/alpha-ceo",
                    "title": "Omar Ali - Founder at AlphaDrop | LinkedIn",
                    "snippet": "Building AlphaDrop on whop.com and https://forbes.com/story",
                }
            ]
        },
    )
    assert len(res) == 1
    assert set(res[0].keys()) == {
        "founder_name",
        "founder_linkedin",
        "work_email",
        "project_url",
        "role_title",
        "role_bucket",
    }
    assert res[0]["project_url"] == ""
    assert res[0]["work_email"] == ""


def test_find_company_decision_maker_backward_compatibility_and_enrich_rows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/alex-lee",
                    "title": "Alex Lee - TarotFlow | LinkedIn",
                    "snippet": "Building tarotflow.app",
                }
            ]
        },
    )
    legacy = treg_enrich.find_company_decision_maker("TarotFlow", "tarotflow.app")
    assert legacy["founder_linkedin"] == "https://www.linkedin.com/in/alex-lee"
    assert legacy["founder_name"] == "Alex Lee"
    assert legacy["project_url"] == "https://tarotflow.app"
    assert "work_email" not in legacy

    monkeypatch.setattr(
        treg_enrich,
        "call",
        lambda ep, query=None, **kw: {
            "results": [
                {
                    "url": "https://www.linkedin.com/in/kbeauty-cmo",
                    "title": "Hana Kim - Head of Growth at K-Beauty | LinkedIn",
                    "snippet": "kbeauty-labs.com",
                },
                {
                    "url": "https://www.linkedin.com/in/kbeauty-ceo",
                    "title": "Min Seo - Founder & CEO at K-Beauty | LinkedIn",
                    "snippet": "https://forbes.com/feature and https://kbeauty-labs.com",
                },
            ]
        },
    )
    best_one = treg_enrich.find_company_decision_maker("K-Beauty", "")
    assert best_one["founder_linkedin"] == "https://www.linkedin.com/in/kbeauty-ceo"
    assert best_one["founder_name"] == "Min Seo"
    assert best_one["project_url"] == "https://kbeauty-labs.com"
    assert best_one["role_bucket"] == "founder"

    # Integration with enrich_creator_rows for linkedin_jobs rows
    cache = tmp_path / "treg_cache.json"
    cache.write_text('{"creator": {"at": 9999999999, "text": "cached"}}', encoding="utf-8")
    out_rows = treg_enrich.enrich_creator_rows(
        [{"source": "linkedin_jobs", "project": "K-Beauty", "name": "Growth Role"}],
        cache,
    )
    assert len(out_rows) == 1
    assert out_rows[0]["founder_linkedin"] == "https://www.linkedin.com/in/kbeauty-ceo"
