from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import tg_web_client, tma_apps  # noqa: E402


def _tme_user_html(title: str, handle: str, bio: str = "") -> str:
    desc = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{handle}</div>'
        f"{desc}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Send Message</a></div>'
        "</div></body></html>"
    )


def _tme_bot_html(title: str, handle: str, bio: str = "") -> str:
    desc = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{handle}</div>'
        f"{desc}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Start Bot</a></div>'
        "</div></body></html>"
    )


@pytest.mark.parametrize(
    ("name", "desc", "cat", "expected_reject"),
    [
        ("Hamster Tap-to-Earn", "Tap the coin daily", "stars", True),
        ("FastTap2Earn", "Best tap to earn game", "tools", True),
        ("Mega Clicker Pro", "Auto clicker for tokens", "wallets", True),
        ("TON Airdrop Hub", "Claim instant airdrop rewards", "finance", True),
        ("Lucky Royal Casino", "Play online casino on Telegram", "stars", True),
        ("Crypto Gambling Club", "Real money gambling mini app", "finance", True),
        ("Vegas Slots 777", "Spin slots and win TON", "stars", True),
        ("VIP Roulette", "Live European roulette wheel", "exchanges", True),
        ("Sport Betting Bot", "Football betting odds", "stars", True),
        ("Spin-to-Earn Wheel", "Spin to earn crypto daily", "stars", True),
        ("Pepe Meme Coin", "New meme coin launchpad", "exchanges", True),
        ("Toncoin Miner X", "Free toncoin miner on cloud", "wallets", True),
        ("Daily Giveaway Box", "Enter weekly giveaway", "stars", True),
        ("Viral Referral Farm", "Invite 50 friends referral farm", "tools", True),
        ("Turbo Farm Coin", "Farm coin automatically", "finance", True),
        ("Arcade Hub", "Mini game", "games", True),
        ("VoiceNote AI", "Paid AI voice transcription & summary subscription", "stars", False),
        ("ShieldVPN Pro", "Subscription VPN mini app with Stars billing", "tools", False),
        ("BookSlot Calendar", "Schedule client appointment time slot reminders", "tools", False),
        ("CoinLedger Tracker", "Portfolio & tax analytics for active investors", "wallets", False),
    ],
)
def test_is_rejected_tma_app_filters_junk_and_keeps_real_products(
    name: str, desc: str, cat: str, expected_reject: bool
) -> None:
    rejected, reason = tma_apps.is_rejected_tma_app(name, desc, cat)
    assert rejected is expected_reject
    if expected_reject:
        assert reason
    else:
        assert reason == ""


def test_parse_findmini_category_html_cards_links_and_nav_skip() -> None:
    html = """
    <html><body>
      <nav>
        <a href="/category/wallets/">Wallets</a>
        <a href="/about/">About</a>
        <a href="/submit/">Submit App</a>
      </nav>
      <div class="apps-grid">
        <div class="app-card">
          <a href="/voice_ai_bot/"><h3>VoiceNote AI</h3></a>
          <p>Paid AI meeting notes inside Telegram.</p>
          <a href="https://t.me/voice_ai_bot">Open</a>
          <a href="https://voicenote.ai">Website</a>
        </div>
        <a class="app-card" href="/tonkeeper_pay/">
          <h3>TonPay Pro</h3>
          <p>Merchant checkout &amp; Stars billing.</p>
        </a>
        <div class="app-card">
          <a href="https://t.me/shield_vpn_bot?startapp=home"><h3>ShieldVPN</h3></a>
          <p>Fast WireGuard VPN mini app.</p>
        </div>
      </div>
    </body></html>
    """
    items = tma_apps.parse_findmini_category_html(html, "stars")
    assert len(items) == 3

    by_name = {row["name"]: row for row in items}
    assert by_name["VoiceNote AI"]["tma_bot_handle"] == "voice_ai_bot"
    assert by_name["VoiceNote AI"]["website"] == "https://voicenote.ai"
    assert by_name["VoiceNote AI"]["category"] == "stars"
    assert "Paid AI meeting notes" in by_name["VoiceNote AI"]["description"]

    assert by_name["TonPay Pro"]["slug"] == "tonkeeper_pay"
    assert by_name["TonPay Pro"]["tma_bot_handle"] == ""
    assert "tonkeeper_pay" in by_name["TonPay Pro"]["tg_handles"]

    assert by_name["ShieldVPN"]["tma_bot_handle"] == "shield_vpn_bot"


def test_parse_awesome_tma_markdown_strict_section_allowlist() -> None:
    md = """
# Awesome Telegram Mini Apps

## Contents
- [Contents Link](https://t.me/contents_bot) - Should be skipped

## Legend
- [Legend App](https://t.me/legend_bot) - Should be skipped

## 💰 Finance
- [FinTrack](https://t.me/fintrack_bot) - Subscription portfolio analytics ([Site](https://fintrack.app))
### DeFi Tools
- [SwapDesk](https://t.me/swapdesk_bot) - Cross-chain routing desk
### Games
- [HiddenGame](https://t.me/hiddengame_bot) - Subsection game should be skipped

## Tools
- **[ClipStudio](https://t.me/clipstudio_bot)** — AI short-form video editor

## Wallets
| Name | Description |
| --- | --- |
| [VaultPay](https://t.me/vaultpay_bot) | Non-custodial smart wallet |

## Social
- [ClubCast](https://t.me/clubcast_bot) - Paid creator audio rooms

## 🤖 AI Agents
- [AgentFlow](https://t.me/agentflow_bot) - Automated customer support agent ([Website](https://agentflow.io))

## Shopping
- [DropCart](https://t.me/dropcart_bot) - D2C storefront inside Telegram

## Games
- [ClickFarm](https://t.me/clickfarm_bot) - Must skip games

## NFT
- [MintDrop](https://t.me/mintdrop_bot) - Must skip NFT

## Staking
- [StakePool](https://t.me/stakepool_bot) - Must skip Staking

## Contributing
- [Helper](https://t.me/contrib_bot) - Must skip Contributing

## About
- [AboutBot](https://t.me/about_bot) - Must skip About
"""
    items = tma_apps.parse_awesome_tma_markdown(md)
    names = [item["name"] for item in items]
    assert names == ["FinTrack", "SwapDesk", "ClipStudio", "VaultPay", "ClubCast", "AgentFlow", "DropCart"]
    cats = {item["name"]: item["category"] for item in items}
    assert cats["FinTrack"] == "Finance"
    assert cats["AgentFlow"] == "AI Agents"
    assert cats["VaultPay"] == "Wallets"
    assert next(i for i in items if i["name"] == "AgentFlow")["website"] == "https://agentflow.io"


def test_parse_tapps_center_html_next_data_rsc_and_href_safe_recovery() -> None:
    next_data = {
        "props": {
            "pageProps": {
                "apps": [
                    {
                        "name": "LinguaCoach",
                        "description": "AI language tutor mini app",
                        "botUsername": "linguacoach_bot",
                        "websiteUrl": "https://linguacoach.ai",
                        "category": "Education",
                    }
                ]
            }
        }
    }
    rsc_inner = json.dumps(
        {
            "miniApps": [
                {
                    "title": "TradePulse",
                    "shortDescription": "Real-time market alerts via Stars",
                    "tgLink": "https://t.me/tradepulse_bot",
                    "website": "https://tradepulse.io",
                    "categoryName": "Finance",
                }
            ]
        }
    )
    html = (
        "<html><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
        f'<script>self.__next_f.push([1,{json.dumps(rsc_inner)}])</script>'
        '<script id="__NEXT_DATA__" type="application/json">{broken json</script>'
        '<div class="app-tile"><a href="https://t.me/docscan_bot"><h3>DocScan Pro</h3></a><p>PDF OCR assistant</p></div>'
        '<div class="dup"><a href="https://t.me/linguacoach_bot">Open</a></div>'
        "</body></html>"
    )
    items = tma_apps.parse_tapps_center_html(html)
    names = [i["name"] for i in items]
    assert "LinguaCoach" in names
    assert "TradePulse" in names
    assert "DocScan Pro" in names
    assert names.count("LinguaCoach") == 1

    # Malformed HTML never crashes
    assert tma_apps.parse_tapps_center_html('<script id="__NEXT_DATA__">{invalid') == []

    # Large padded RSC chunk parses cleanly without backtracking
    large_rsc = json.dumps({"padding": "x" * 120_000, "apps": [{"name": "BigApp", "botUsername": "bigapp_bot", "description": "Paid utility"}]})
    large_items = tma_apps.parse_tapps_center_html(f"<script>self.__next_f.push([1,{json.dumps(large_rsc)}])</script>")
    assert [i["name"] for i in large_items] == ["BigApp"]


def test_resolve_tma_to_human_lead_resolves_founder_channel_website_and_7d_cache(
    tmp_path: Path,
) -> None:
    http_calls: list[str] = []

    def fake_http(url: str, *, timeout: float = 0.0, allow_redirects: bool = True) -> Any:
        http_calls.append(url)
        if url.endswith("/voice_ai_bot"):
            return SimpleNamespace(
                status_code=200,
                text=_tme_bot_html(
                    "VoiceAI",
                    "voice_ai_bot",
                    "Official channel @voiceai_news | Website: https://voiceai.app",
                ),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/s/voiceai_news"):
            return SimpleNamespace(
                status_code=200,
                text=(
                    "<html><body>"
                    '<div class="tgme_channel_info_header_title">VoiceAI News</div>'
                    '<div class="tgme_channel_info_description">Updates https://voiceai.app</div>'
                    '<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">'
                    "Founder &amp; support: @alice_founder — try https://voiceai.app"
                    "</div></div>"
                    "</body></html>"
                ),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/voiceai_news"):
            return SimpleNamespace(
                status_code=200,
                text=(
                    '<div class="tgme_page_title">VoiceAI News</div>'
                    '<div class="tgme_page_extra">4 200 subscribers</div>'
                    '<div class="tgme_page_description">Updates for VoiceAI</div>'
                    '<div class="tgme_page_action"><a class="tgme_action_button_new">View in Telegram</a></div>'
                ),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/alice_founder"):
            return SimpleNamespace(
                status_code=200,
                text=_tme_user_html("Alice Founder", "alice_founder", "Building VoiceAI.app"),
                headers={},
                is_redirect=False,
            )
        return SimpleNamespace(status_code=404, text="", headers={}, is_redirect=False)

    cache_file = tmp_path / "tme_probe_cache.json"
    probe_with_fake_http = lambda h, **kw: tg_web_client.probe_tme_handle_web(
        h, http_get_fn=fake_http, cache_path=kw.get("cache_path") or cache_file
    )
    posts_with_fake_http = lambda ch, **kw: tg_web_client.fetch_channel_posts_web(
        ch, http_get_fn=fake_http, allow_paid=False
    )

    candidate = {
        "name": "VoiceAI",
        "description": "AI voice subscription app",
        "category": "stars",
        "tma_bot_handle": "voice_ai_bot",
    }
    row = tma_apps.resolve_tma_to_human_lead(
        candidate,
        probe_fn=probe_with_fake_http,
        fetch_posts_fn=posts_with_fake_http,
        cache_path=cache_file,
    )
    assert row["username"] == "alice_founder"
    assert row["telegram_username"] == "alice_founder"
    assert row["tg_username"] == "alice_founder"
    assert row["tma_bot_handle"] == "voice_ai_bot"
    assert row["website"] == "https://voiceai.app"
    assert row["project_url"] == "https://voiceai.app"
    assert "https://t.me/voiceai_news" in row["links"]
    assert "https://voiceai.app" in row["links"]
    assert row["lead_source"] == "tma_apps"
    assert row["source"] == "tma_apps"
    assert row["segment"] == "whale"
    assert row["category"] == "tma"

    # Second resolution reuses 7d tme_probe_cache without extra t.me/<handle> probes
    first_probe_count = sum(1 for u in http_calls if "/s/" not in u)
    row2 = tma_apps.resolve_tma_to_human_lead(
        candidate,
        probe_fn=probe_with_fake_http,
        fetch_posts_fn=posts_with_fake_http,
        cache_path=cache_file,
    )
    second_probe_count = sum(1 for u in http_calls if "/s/" not in u)
    assert row2["username"] == "alice_founder"
    assert second_probe_count == first_probe_count
    assert cache_file.is_file()


def test_sacred_invariant_never_puts_bot_into_username_fields() -> None:
    def fake_probe(handle: str, **_kw: Any) -> dict[str, Any]:
        if handle.lower().endswith("bot"):
            # Even if upstream mistakenly reports kind='user' for a *bot handle, Sacred Invariant blocks it!
            return {
                "handle": handle,
                "kind": "user",
                "title": "Only Bot",
                "bio": "Help desk @only_support | Helper @second_helper_bot",
            }
        return {"handle": handle, "kind": "user", "title": "Support", "bio": ""}

    candidate = {
        "name": "OnlyBotApp",
        "description": "Utility bot",
        "tma_bot_handle": "only_app_bot",
        "username": "only_app_bot",
        "telegram_username": "only_app_bot",
        "tg_username": "only_app_bot",
    }
    row = tma_apps.resolve_tma_to_human_lead(
        candidate,
        probe_fn=fake_probe,
        fetch_posts_fn=lambda *_a, **_kw: ({}, []),
    )
    assert row["tma_bot_handle"] == "only_app_bot"
    assert row["username"] == ""
    assert row["telegram_username"] == ""
    assert row["tg_username"] == ""
    assert all("bot" not in link.lower() for link in row["links"])


def test_collect_tma_leads_budget_guard_resilience_dedup_and_atomic_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findmini_stars = """
    <div class="grid">
      <div class="app-card">
        <a href="/nova_ai_bot/"><h3>NovaAI</h3></a>
        <p>Subscription AI copilot</p>
      </div>
      <div class="app-card">
        <a href="/tap_coin_bot/"><h3>Super Tap-to-Earn</h3></a>
        <p>Clicker airdrop game</p>
      </div>
      <div class="app-card">
        <a href="/bot_only_app_bot/"><h3>BotOnlyTool</h3></a>
        <p>Paid PDF converter bot</p>
      </div>
    </div>
    """
    awesome_md = """
## AI Agents
- [NovaAI](https://t.me/nova_ai_bot) - Duplicate bot across catalogs should be deduped
- [AgentDesk](https://t.me/agentdesk_bot) - AI support desk ([Site](https://agentdesk.io))
## Games
- [SlotsKing](https://t.me/slotsking_bot) - Casino slots game
"""
    tapps_html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(
            {
                "apps": [
                    {
                        "name": "VaultPay",
                        "description": "Crypto card wallet",
                        "botUsername": "vaultpay_bot",
                        "websiteUrl": "https://vaultpay.xyz",
                    }
                ]
            }
        )
        + "</script>"
    )

    def fake_http_get(url: str, **_kw: Any) -> Any:
        if "findmini.app/category/stars" in url:
            return SimpleNamespace(status_code=200, text=findmini_stars)
        if "findmini.app/category/wallets" in url:
            raise TimeoutError("simulated timeout")
        if "SzczepanBEN/awesome-telegram-mini-apps" in url:
            return SimpleNamespace(status_code=200, text=awesome_md)
        if "telegram-mini-apps-dev/awesome-telegram-mini-apps" in url:
            return SimpleNamespace(status_code=404, text="Not Found")
        if "tapps.center" in url:
            return SimpleNamespace(status_code=200, text=tapps_html)
        return SimpleNamespace(status_code=404, text="")

    def fake_probe(handle: str, **_kw: Any) -> dict[str, Any]:
        h = handle.lower()
        if h == "nova_ai_bot":
            return {"handle": "nova_ai_bot", "kind": "bot", "title": "NovaAI", "bio": "Founder: @elena_founder | https://nova.ai"}
        if h == "elena_founder":
            return {"handle": "elena_founder", "kind": "user", "title": "Elena K", "bio": "Founder at Nova.ai"}
        if h == "agentdesk_bot":
            return {"handle": "agentdesk_bot", "kind": "bot", "title": "AgentDesk", "bio": "News: @agentdesk_news"}
        if h == "agentdesk_news":
            return {"handle": "agentdesk_news", "kind": "channel", "title": "AgentDesk News", "bio": "DM @mark_ceo"}
        if h == "mark_ceo":
            return {"handle": "mark_ceo", "kind": "user", "title": "Mark CEO", "bio": "Building AgentDesk"}
        if h == "vaultpay_bot":
            return {"handle": "vaultpay_bot", "kind": "bot", "title": "VaultPay", "bio": "Card wallet"}
        if h == "bot_only_app_bot":
            return {"handle": "bot_only_app_bot", "kind": "bot", "title": "BotOnlyTool", "bio": "No owner"}
        if h == "paid_found_bot":
            return {"handle": "paid_found_bot", "kind": "bot", "title": "PaidApp", "bio": "Contact @paid_founder"}
        if h == "paid_founder":
            return {"handle": "paid_founder", "kind": "user", "title": "Paid Founder", "bio": "Founder"}
        return {"handle": handle, "kind": "missing", "title": "", "bio": ""}

    def fake_fetch_posts(channel: str, **_kw: Any) -> tuple[dict[str, Any], list[str]]:
        if channel.lower() == "agentdesk_news":
            return {"title": "AgentDesk News", "about": "DM @mark_ceo"}, ["Launch update https://agentdesk.io"]
        return {}, []

    treg_calls: list[tuple[str, Any]] = []

    def fake_treg(endpoint: str, **kw: Any) -> dict[str, Any]:
        treg_calls.append((endpoint, kw.get("query")))
        return {
            "results": [
                {
                    "url": "https://t.me/paid_found_bot",
                    "title": "PaidVPN Mini App",
                    "snippet": "VPN subscription inside Telegram https://paidvpn.io",
                }
            ]
        }

    out1 = tmp_path / "out1.jsonl"
    seen_path = tmp_path / "tma_apps_seen.json"

    # 1) BUDGET_PAID_ALLOWED=0 must skip treg_fn even when allow_paid=True
    monkeypatch.setenv("BUDGET_PAID_ALLOWED", "0")
    res1 = tma_apps.collect_tma_leads(
        output_path=out1,
        seen_path=seen_path,
        limit=40,
        allow_paid=True,
        categories=["stars", "wallets"],
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
        probe_fn=fake_probe,
        fetch_posts_fn=fake_fetch_posts,
        cache_path=tmp_path / "probe_cache.json",
    )
    assert treg_calls == []
    assert res1["paid_search_used"] is False
    assert res1["rejected"] >= 1
    assert any("findmini:wallets" in e for e in res1["errors"])
    assert res1["written"] == 4

    rows1 = [json.loads(ln) for ln in out1.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows1) == 4
    for r in rows1:
        assert r["lead_source"] == "tma_apps"
        assert r["source"] == "tma_apps"
        assert r["segment"] == "whale"
        assert r["category"] == "tma"
        assert not r["username"].lower().endswith("bot")
        assert not r["telegram_username"].lower().endswith("bot")

    bot_only_row = next(r for r in rows1 if r["tma_bot_handle"] == "bot_only_app_bot")
    assert bot_only_row["username"] == ""
    assert bot_only_row["telegram_username"] == ""

    # Seen file persisted atomically with zero leftover temp files
    assert seen_path.is_file()
    seen_payload = json.loads(seen_path.read_text(encoding="utf-8"))
    assert "bot:nova_ai_bot" in seen_payload["seen"]
    assert not list(tmp_path.glob(".*.tmp"))

    # 2) allow_paid=False skips treg_fn and skips already-seen items
    out2 = tmp_path / "out2.jsonl"
    monkeypatch.setenv("BUDGET_PAID_ALLOWED", "1")
    res2 = tma_apps.collect_tma_leads(
        output_path=out2,
        seen_path=seen_path,
        limit=40,
        allow_paid=False,
        categories=["stars"],
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
        probe_fn=fake_probe,
        fetch_posts_fn=fake_fetch_posts,
        cache_path=tmp_path / "probe_cache.json",
    )
    assert treg_calls == []
    assert res2["paid_search_used"] is False
    assert res2["written"] == 0
    assert res2["skipped_seen"] >= 4

    # 3) allow_paid=True & BUDGET_PAID_ALLOWED=1 runs crawl4ai.web.search for unseen paid hit
    out3 = tmp_path / "out3.jsonl"
    res3 = tma_apps.collect_tma_leads(
        output_path=out3,
        seen_path=seen_path,
        limit=40,
        allow_paid=True,
        categories=["stars"],
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
        probe_fn=fake_probe,
        fetch_posts_fn=fake_fetch_posts,
        cache_path=tmp_path / "probe_cache.json",
    )
    assert len(treg_calls) == 1
    assert treg_calls[0][0] == "crawl4ai.web.search"
    assert res3["paid_search_used"] is True
    assert res3["written"] == 1
    rows3 = [json.loads(ln) for ln in out3.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows3[0]["tma_bot_handle"] == "paid_found_bot"
    assert rows3[0]["username"] == "paid_founder"


def test_cli_help_and_jsonl_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "leadgen.tma_apps", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    for flag in ("--output", "--seen", "--limit", "--no-paid", "--categories"):
        assert flag in proc.stdout

    # Offline CLI main() smoke producing valid JSONL
    def fake_req_get(url: str, **_kw: Any) -> Any:
        if "findmini.app/category/stars" in url:
            return SimpleNamespace(
                status_code=200,
                text='<div class="card"><a href="/smoke_app_bot/"><h3>SmokeApp</h3></a><p>Paid AI tool</p></div>',
                headers={},
                is_redirect=False,
            )
        if url == "https://t.me/smoke_app_bot":
            return SimpleNamespace(
                status_code=200,
                text=_tme_bot_html("SmokeApp", "smoke_app_bot", "Founder @smoke_founder | https://smokeapp.io"),
                headers={},
                is_redirect=False,
            )
        if url == "https://t.me/smoke_founder":
            return SimpleNamespace(
                status_code=200,
                text=_tme_user_html("Smoke Founder", "smoke_founder", "Creator of SmokeApp"),
                headers={},
                is_redirect=False,
            )
        return SimpleNamespace(status_code=404, text="", headers={}, is_redirect=False)

    monkeypatch.setattr(tma_apps.requests, "get", fake_req_get)
    monkeypatch.setattr(tg_web_client.requests, "get", fake_req_get)
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))

    out_jsonl = tmp_path / "cli_out.jsonl"
    seen_json = tmp_path / "cli_seen.json"
    rc = tma_apps.main(
        [
            "--output",
            str(out_jsonl),
            "--seen",
            str(seen_json),
            "--limit",
            "5",
            "--no-paid",
            "--categories",
            "stars",
        ]
    )
    assert rc == 0
    lines = [ln for ln in out_jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["username"] == "smoke_founder"
    assert row["tma_bot_handle"] == "smoke_app_bot"
    assert row["website"] == "https://smokeapp.io"
    assert row["lead_source"] == "tma_apps"
    assert seen_json.is_file()

    # Telegram system/directory handles caught in live canary must never pass personal-handle filter
    for sys_handle in ("business", "sensor", "wallet", "stars", "premium", "fragment", "findmini"):
        assert tma_apps._is_real_personal_tg_handle(sys_handle) is False
