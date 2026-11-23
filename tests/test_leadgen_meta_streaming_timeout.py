from __future__ import annotations

from datetime import date
import inspect
import json
from pathlib import Path
import signal
import sys

from leadgen import meta_ad_leads


def _make_ad_payload(items: list[tuple[str, str, str, dict | None]], *, id_prefix: str = "88") -> dict:
    search_results = []
    for idx, (page_name, domain, body, extra_snap) in enumerate(items, start=1):
        snap: dict = {
            "display_format": "VIDEO",
            "page_name": page_name,
            "body": {"text": body},
            "link_url": f"https://{domain}" if domain else "",
            "cta_text": "Learn More",
        }
        if extra_snap:
            snap.update(extra_snap)
        search_results.append(
            {
                "is_active": True,
                "page_id": f"{id_prefix}00{idx}",
                "snapshot": snap,
            }
        )
    return {"searchResults": search_results}


def test_collect_signature_supports_max_runtime_sec_on_row_and_country_rotation() -> None:
    sig = inspect.signature(meta_ad_leads.collect)
    assert "max_runtime_sec" in sig.parameters
    assert sig.parameters["max_runtime_sec"].default is None
    assert "on_row" in sig.parameters
    assert sig.parameters["on_row"].default is None


def test_collect_max_runtime_sec_stops_before_search_and_enrichment(monkeypatch) -> None:
    # 1) Deadline expires after step 1 -> step 2 search never runs
    clock = [100.0]
    monkeypatch.setattr(meta_ad_leads.time, "monotonic", lambda: clock[0])

    search_calls: list[tuple[str, str]] = []

    def fake_search(query: str, country: str = "US") -> dict:
        search_calls.append((query, country))
        # Advance past 1.0s deadline during first search
        clock[0] += 1.5
        return _make_ad_payload(
            [
                ("First Brand", "firstbrand.ai", "AI subscription app", None),
                ("Second Brand", "secondbrand.ai", "AI coaching app", None),
            ]
        )

    enrich_calls: list[str] = []

    def fake_enrich(company: str, domain: str) -> dict:
        enrich_calls.append(domain)
        return {"work_email": f"hi@{domain}"}

    rows, updated = meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=10,
        countries=("US", "GB", "AE"),
        query_count=3,
        max_runtime_sec=1.0,
        search_fn=fake_search,
        enrich_fn=fake_enrich,
    )
    assert len(search_calls) == 1
    assert enrich_calls == []
    assert rows == []
    assert updated == {}

    # 2) Deadline expires after candidate 1 enrichment -> candidate 2 is NOT enriched
    clock[0] = 200.0
    search_calls.clear()
    enrich_calls.clear()
    streamed: list[dict] = []

    def fake_search_two(query: str, country: str = "US") -> dict:
        search_calls.append((query, country))
        return _make_ad_payload(
            [
                ("Alpha Co", "alphaco.io", "AI subscription workspace", None),
                ("Beta Co", "betaco.io", "AI fitness tracker", None),
                ("Gamma Co", "gammaco.io", "AI therapy app", None),
            ]
        )

    def fake_enrich_advance(company: str, domain: str) -> dict:
        enrich_calls.append(domain)
        # First candidate finishes just past deadline -> second candidate must not be enriched
        clock[0] += 2.0
        return {"work_email": f"ceo@{domain}"}

    rows2, updated2 = meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=10,
        countries=("US", "GB"),
        query_count=2,
        max_runtime_sec=1.0,
        on_row=streamed.append,
        search_fn=fake_search_two,
        enrich_fn=fake_enrich_advance,
    )
    assert len(search_calls) == 1
    assert enrich_calls == ["alphaco.io"]
    assert [r["name"] for r in rows2] == ["Alpha Co"]
    assert [r["name"] for r in streamed] == ["Alpha Co"]
    assert "domain:alphaco.io" in updated2
    assert "domain:betaco.io" not in updated2


    # 3) Deadline expiring inside store_resolver prevents subsequent enrich_fn call
    clock[0] = 300.0
    enrich_calls.clear()

    def fake_store_slow(url: str) -> dict:
        clock[0] += 5.0
        return {"developer": "SlowApp Studio", "website": "https://slowapp.io", "domain": "slowapp.io"}

    rows3, updated3 = meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=5,
        countries=("US",),
        query_count=1,
        max_runtime_sec=1.0,
        search_fn=lambda q, country="US": _make_ad_payload(
            [("SlowApp", "", "AI subscription", {"link_url": "https://apps.apple.com/us/app/slowapp/id123456"})],
            id_prefix="33",
        ),
        enrich_fn=lambda c, d: (enrich_calls.append(d), {"work_email": f"hi@{d}"})[1],
        store_resolver_fn=fake_store_slow,
    )
    assert enrich_calls == []
    assert rows3 == []
    assert updated3 == {}


def test_collect_streams_on_row_before_later_steps() -> None:
    streamed_at_step2: list[str] = []
    streamed: list[dict] = []
    step_num = [0]

    def fake_search(query: str, country: str = "US") -> dict:
        step_num[0] += 1
        if step_num[0] == 2:
            streamed_at_step2.extend(r["name"] for r in streamed)
        return _make_ad_payload(
            [(f"Brand {country} {step_num[0]}", f"brand-{step_num[0]}.ai", "AI subscription", None)],
            id_prefix=f"step{step_num[0]}",
        )

    rows, _ = meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=5,
        countries=("US", "GB"),
        query_count=2,
        on_row=streamed.append,
        search_fn=fake_search,
        enrich_fn=lambda company, domain: {"work_email": f"hi@{domain}"},
    )
    assert len(rows) == 2
    assert streamed_at_step2 == ["Brand US 1"]
    assert [r["name"] for r in streamed] == ["Brand US 1", "Brand GB 2"]


def test_country_window_rotation_for_large_lists_and_preserves_small_lists() -> None:
    twelve = ("US", "GB", "AE", "SA", "DE", "FR", "NL", "ES", "IT", "SE", "CA", "AU")
    windows: list[list[str]] = []

    for cur in range(5):
        seen_c: list[str] = []

        def fake_search(query: str, country: str = "US") -> dict:
            seen_c.append(country)
            return {"searchResults": []}

        meta_ad_leads.collect(
            day=date(2026, 11, 23),
            seen={},
            cap=5,
            countries=twelve,
            cursor=cur,
            query_count=3,
            rotate_countries=True,
            search_fn=fake_search,
            enrich_fn=lambda c, d: {},
        )
        windows.append(seen_c)

    assert windows[0] == ["US", "GB", "AE"]
    assert windows[1] == ["SA", "DE", "FR"]
    assert windows[2] == ["NL", "ES", "IT"]
    assert windows[3] == ["SE", "CA", "AU"]
    assert windows[4] == ["US", "GB", "AE"]

    # <= 3 countries stays untouched regardless of cursor
    small_seen: list[str] = []
    meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=5,
        countries=("GB", "CA"),
        cursor=2,
        query_count=2,
        rotate_countries=True,
        search_fn=lambda q, country="US": (small_seen.append(country), {"searchResults": []})[1],
        enrich_fn=lambda c, d: {},
    )
    assert small_seen == ["GB", "CA"]


def test_main_streams_jsonl_rotates_12_geos_and_handles_deadline_and_sigterm(
    tmp_path: Path, monkeypatch
) -> None:
    out_file = tmp_path / "meta_ads.jsonl"
    seen_file = tmp_path / "meta_ads_seen.json"
    twelve_csv = "US,GB,AE,SA,DE,FR,NL,ES,IT,SE,CA,AU"

    countries_run1: list[str] = []
    flushed_before_step2: list[str] = []

    def fake_default_search(query: str, country: str = "US") -> dict:
        countries_run1.append(country)
        if len(countries_run1) == 2:
            # Row from step 1 MUST already be flushed to disk before step 2 runs
            if out_file.is_file():
                flushed_before_step2.extend(
                    line for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            signal.raise_signal(signal.SIGTERM)
        return _make_ad_payload(
            [(f"StreamBrand {len(countries_run1)}", f"stream{len(countries_run1)}.ai", "AI app", None)],
            id_prefix=f"r1_{len(countries_run1)}",
        )

    monkeypatch.setattr(meta_ad_leads, "_default_search", fake_default_search)
    monkeypatch.setattr(
        meta_ad_leads,
        "find_company_decision_maker",
        lambda company, domain: {"work_email": f"ceo@{domain}", "founder_name": "Elena Vance"},
    )
    monkeypatch.setattr(meta_ad_leads, "_scrape_site_contacts", lambda url: {})

    prev_handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meta_ad_leads.py",
            "--output",
            str(out_file),
            "--seen",
            str(seen_file),
            "--country",
            twelve_csv,
            "--day",
            "2026-11-23",
            "--max-runtime-sec",
            "210.0",
        ],
    )
    meta_ad_leads.main()

    # Signal handler must be restored
    assert signal.getsignal(signal.SIGTERM) == prev_handler
    # Row 1 was flushed immediately before step 2 ran
    assert len(flushed_before_step2) == 1
    written = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(written) == 1
    assert written[0]["name"] == "StreamBrand 1"
    assert written[0]["founder_name"] == "Elena Vance"

    # Seen + cursor + 1 persisted despite SIGTERM
    seen_payload = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen_payload["cursor"] == 1
    assert "domain:stream1.ai" in seen_payload["advertisers"]

    # Run 2 picks next 3-country window (SA, DE, FR) and stops cleanly on deadline
    out_file2 = tmp_path / "meta_ads_2.jsonl"
    countries_run2: list[str] = []
    clock = [500.0]
    monkeypatch.setattr(meta_ad_leads.time, "monotonic", lambda: clock[0])

    def fake_default_search_2(query: str, country: str = "US") -> dict:
        countries_run2.append(country)
        if len(countries_run2) >= 2:
            clock[0] += 300.0
        return _make_ad_payload(
            [(f"Run2Brand {len(countries_run2)}", f"run2-{len(countries_run2)}.ai", "AI app", None)],
            id_prefix=f"r2_{len(countries_run2)}",
        )

    monkeypatch.setattr(meta_ad_leads, "_default_search", fake_default_search_2)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meta_ad_leads.py",
            "--output",
            str(out_file2),
            "--seen",
            str(seen_file),
            "--country",
            twelve_csv,
            "--day",
            "2026-11-23",
        ],
    )
    meta_ad_leads.main()

    assert countries_run2 == ["SA", "DE"]
    seen_payload2 = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen_payload2["cursor"] == 2
    assert "domain:run2-1.ai" in seen_payload2["advertisers"]
    assert "domain:run2-2.ai" not in seen_payload2["advertisers"]
    written2 = [json.loads(line) for line in out_file2.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["name"] for r in written2] == ["Run2Brand 1"]

    # Run 3: unexpected exception mid-collect still persists streamed rows + seen + cursor + 1 via finally
    out_file3 = tmp_path / "meta_ads_3.jsonl"
    orig_parse = meta_ad_leads.parse_ad_candidates
    parse_calls = [0]

    def boom_on_second_parse(payload, *, country_code: str = ""):
        parse_calls[0] += 1
        if parse_calls[0] == 2:
            raise RuntimeError("unexpected parse crash")
        return orig_parse(payload, country_code=country_code)

    clock[0] = 900.0
    monkeypatch.setattr(meta_ad_leads, "parse_ad_candidates", boom_on_second_parse)
    monkeypatch.setattr(
        meta_ad_leads,
        "_default_search",
        lambda q, country="US": _make_ad_payload(
            [(f"CrashSurvive {country}", f"crash-{country.lower()}.ai", "AI app", None)],
            id_prefix=f"r3_{country}",
        ),
    )
    meta_ad_leads.main(
        [
            "--output",
            str(out_file3),
            "--seen",
            str(seen_file),
            "--country",
            "GB,CA",
            "--day",
            "2026-11-23",
        ]
    )
    written3 = [json.loads(line) for line in out_file3.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["name"] for r in written3] == ["CrashSurvive GB"]
    seen_payload3 = json.loads(seen_file.read_text(encoding="utf-8"))
    assert seen_payload3["cursor"] == 3
    assert "domain:crash-gb.ai" in seen_payload3["advertisers"]


def test_preserves_founder_name_founder_x_and_personal_tg_username_from_enricher(monkeypatch) -> None:
    payload = _make_ad_payload(
        [
            ("Alice AI", "alice.ai", "AI subscription coach", None),
            (
                "Bob Labs",
                "boblabs.io",
                "Daily supplement gummies",
                {
                    "link_url": "https://boblabs.io",
                    "cta_type": "WHATSAPP_MESSAGE",
                    "caption": "+44 7911 123456",
                },
            ),
            (
                "Charlie Corp",
                "charliecorp.com",
                "Wellness app",
                {
                    "link_url": "https://charliecorp.com",
                    "cta_type": "WHATSAPP_MESSAGE",
                    "caption": "+1 415 555 0199",
                },
            ),
        ]
    )

    def fake_site_scrape(url: str) -> dict[str, str]:
        if "boblabs.io" in url:
            # Site has channel handle and site founder_x (should beat DM founder_x, while DM personal TG wins)
            return {"username": "BobLabs_Official", "founder_x": "https://x.com/boblabs_site"}
        return {}

    monkeypatch.setattr(meta_ad_leads, "_scrape_site_contacts", fake_site_scrape)

    def fake_enrich(company: str, domain: str) -> dict:
        if domain == "alice.ai":
            return {
                "founder_name": "Alice Vance",
                "founder_linkedin": "https://www.linkedin.com/in/alice-vance",
                "founder_x": "https://x.com/alice_vance",
                "telegram_username": "@alice_vance_tg",
                "work_email": "alice@alice.ai",
            }
        if domain == "boblabs.io":
            return {
                "founder_name": "Bob Miller",
                "founder_x": "https://x.com/bob_dm_fallback",
                "username": "bob_miller_real",
            }
        if domain == "charliecorp.com":
            return {
                "founder_name": "Charlie Day",
                "username": "charlie_support_bot",
                "telegram_username": "charlie_official",
            }
        return {}

    rows, _ = meta_ad_leads.collect(
        day=date(2026, 11, 23),
        seen={},
        cap=10,
        countries=("US",),
        query_count=1,
        search_fn=lambda q, country="US": payload,
        enrich_fn=fake_enrich,
        store_resolver_fn=lambda _u: {},
    )
    by_name = {r["name"]: r for r in rows}

    r_alice = by_name["Alice AI"]
    assert r_alice["founder_name"] == "Alice Vance"
    assert r_alice["founder_x"] == "https://x.com/alice_vance"
    assert r_alice["username"] == "alice_vance_tg"
    assert r_alice["telegram_username"] == "alice_vance_tg"
    assert r_alice["provider"] == "telegram"
    assert "https://x.com/alice_vance" in r_alice["links"]
    assert "https://t.me/alice_vance_tg" in r_alice["links"]

    r_bob = by_name["Bob Labs"]
    assert r_bob["founder_name"] == "Bob Miller"
    assert r_bob["founder_x"] == "https://x.com/boblabs_site"
    assert r_bob["username"] == "bob_miller_real"
    assert r_bob["telegram_username"] == "bob_miller_real"
    assert r_bob["telegram_group"] == "https://t.me/BobLabs_Official"
    assert r_bob["provider"] == "telegram"

    r_charlie = by_name["Charlie Corp"]
    assert r_charlie["founder_name"] == "Charlie Day"
    assert r_charlie["username"] == ""
    assert r_charlie["telegram_username"] == ""
    assert r_charlie["telegram_group"] == "https://t.me/charlie_official"
    assert r_charlie["provider"] == "whatsapp"
