from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import (
    ChatWriteForbiddenError,
    InviteRequestSentError,
    UserBannedInChannelError,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import (  # noqa: E402
    tg_ad_spy,
    tg_catalog_discovery,
    tg_channel_graph,
    tg_web_client,
)


def _channel_page_html(title: str, bio: str = "", subs: str = "2 400 subscribers") -> str:
    desc = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">{subs}</div>'
        f"{desc}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">View in Telegram</a></div>'
        "</div></body></html>"
    )


def _bot_page_html(title: str, handle: str) -> str:
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{handle}</div>'
        '<div class="tgme_page_action"><a class="tgme_action_button_new">Start Bot</a></div>'
        "</div></body></html>"
    )


def test_curated_pool_size_and_hygiene_and_cli_help() -> None:
    pool = tg_catalog_discovery.CURATED_CHANNEL_POOL
    assert 60 <= len(pool) <= 80
    lows = [h.lower() for h in pool]
    assert len(set(lows)) == len(pool)
    for handle in pool:
        assert not handle.lower().endswith("bot")
        assert not tg_catalog_discovery._is_junk_channel(handle)

    for sig in ("captcha", "shieldy", "combot", "missrose"):
        assert sig in tg_catalog_discovery.CAPTCHA_BOT_SIGNATURES

    proc = subprocess.run(
        [sys.executable, "-m", "leadgen.tg_catalog_discovery", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    for flag in ("--state-dir", "--limit", "--no-paid", "--safe-join", "--max-joins-per-day"):
        assert flag in proc.stdout


def test_discover_and_replenish_catalogs_sources_validation_dedup_and_no_tgstat_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)

    # Pre-seed existing files to verify preservation + case-insensitive dedup
    (state_dir / "discovered_comment_channels.txt").write_text(
        "id777001\n@Existing_Chat\n",
        encoding="utf-8",
    )
    (state_dir / "discovered_ad_spy_channels.txt").write_text(
        "tma_alpha\n",
        encoding="utf-8",
    )
    (state_dir / "tg_channel_graph_state.json").write_text(
        json.dumps(
            {
                "seen": ["id99999", "graph_saas_hub"],
                "frontier": [["graph_frontier_ai", 1]],
            }
        ),
        encoding="utf-8",
    )

    fetched_urls: list[str] = []

    def fake_http_get(url: str, **_: Any) -> Any:
        fetched_urls.append(url)
        assert "tgstat." not in url.lower(), f"Must NEVER fetch tgstat over HTTP: {url}"
        if "findmini.app" in url:
            return SimpleNamespace(
                status_code=200,
                text=(
                    '<a href="https://t.me/tma_alpha">TMA Alpha</a>'
                    '<a href="https://t.me/mini_wallet_bot/app">Bot</a>'
                    '<a href="https://t.me/casino_slots_vip">Casino</a>'
                ),
            )
        if "SzczepanBEN" in url:
            return SimpleNamespace(
                status_code=200,
                text=(
                    "Check https://t.me/awesome_tma_founders and "
                    "https://t.me/fragment_monitor and "
                    "https://t.me/+PrivateInviteHash123"
                ),
            )
        if "tapps.center" in url:
            return SimpleNamespace(
                status_code=200,
                text='<a href="https://t.me/tapps_builders_chat">Builders Chat</a> <a href="https://t.me/dead_missing_chan">Dead</a>',
            )
        if url.startswith("https://t.me/"):
            handle = url.rsplit("/", 1)[-1]
            if handle == "mini_wallet_bot":
                return SimpleNamespace(status_code=200, text=_bot_page_html("Wallet Bot", handle), headers={}, is_redirect=False)
            if handle == "dead_missing_chan":
                return SimpleNamespace(status_code=404, text="", headers={}, is_redirect=False)
            if handle == "porn_adult_chan":
                return SimpleNamespace(
                    status_code=200,
                    text=_channel_page_html("Adult 18+ Sliv", "OnlyFans leak xxx"),
                    headers={},
                    is_redirect=False,
                )
            if handle == "tapps_builders_chat":
                return SimpleNamespace(
                    status_code=200,
                    text=_channel_page_html("TMA Builders Chat", "Founders community chat", "1 200 members"),
                    headers={},
                    is_redirect=False,
                )
            if handle in {
                "tma_alpha",
                "awesome_tma_founders",
                "graph_saas_hub",
                "graph_frontier_ai",
                "tgstat_saas_news",
                "search_prop_desk",
            } or handle in tg_catalog_discovery.CURATED_CHANNEL_POOL:
                return SimpleNamespace(
                    status_code=200,
                    text=_channel_page_html(f"Channel {handle}", f"Global SaaS & AI updates @{handle}"),
                    headers={},
                    is_redirect=False,
                )
            return SimpleNamespace(status_code=404, text="", headers={}, is_redirect=False)
        return SimpleNamespace(status_code=404, text="", headers={}, is_redirect=False)

    treg_calls: list[tuple[str, str]] = []

    def fake_treg(endpoint: str, *, query: dict[str, str] | None = None, **_: Any) -> dict[str, Any]:
        q = (query or {}).get("q", "")
        treg_calls.append((endpoint, q))
        return {
            "results": [
                {
                    "url": "https://tgstat.com/channel/@tgstat_saas_news",
                    "snippet": "Best SaaS channel https://t.me/search_prop_desk and https://t.me/porn_adult_chan",
                }
            ]
        }

    # 1) When BUDGET_PAID_ALLOWED=0 -> crawl4ai.web.search MUST NOT run
    monkeypatch.setenv("BUDGET_PAID_ALLOWED", "0")
    res_no_paid = tg_catalog_discovery.discover_and_replenish_catalogs(
        state_dir,
        limit=12,
        allow_paid=True,
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
    )
    assert treg_calls == []
    assert res_no_paid["paid_search_used"] is False

    # 2) When allow_paid=False -> crawl4ai.web.search MUST NOT run
    monkeypatch.setenv("BUDGET_PAID_ALLOWED", "1")
    res_flag_off = tg_catalog_discovery.discover_and_replenish_catalogs(
        state_dir,
        limit=12,
        allow_paid=False,
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
    )
    assert treg_calls == []
    assert res_flag_off["paid_search_used"] is False

    # 3) When allow_paid=True and BUDGET_PAID_ALLOWED != "0" -> paid search runs and parses tgstat URLs without HTTP fetch
    res_paid = tg_catalog_discovery.discover_and_replenish_catalogs(
        state_dir,
        limit=12,
        allow_paid=True,
        http_get_fn=fake_http_get,
        treg_fn=fake_treg,
    )
    assert res_paid["paid_search_used"] is True
    assert len(treg_calls) == 2
    assert any(ep == "crawl4ai.web.search" for ep, _ in treg_calls)
    assert not any("tgstat." in u.lower() for u in fetched_urls)

    ad_spy_lines = (state_dir / "discovered_ad_spy_channels.txt").read_text(encoding="utf-8").splitlines()
    graph_lines = (state_dir / "discovered_graph_seeds.txt").read_text(encoding="utf-8").splitlines()
    comment_lines = (state_dir / "discovered_comment_channels.txt").read_text(encoding="utf-8").splitlines()

    for expected in ("tma_alpha", "awesome_tma_founders", "tapps_builders_chat", "tgstat_saas_news", "search_prop_desk"):
        assert expected in ad_spy_lines
        assert expected in graph_lines

    # Existing comment entries preserved; chat channel appended; junk/bots/missing excluded
    assert "id777001" in comment_lines
    assert "Existing_Chat" in comment_lines
    assert "tapps_builders_chat" in comment_lines
    for bad in ("mini_wallet_bot", "casino_slots_vip", "fragment_monitor", "dead_missing_chan", "porn_adult_chan"):
        assert bad not in ad_spy_lines
        assert bad not in graph_lines
        assert bad not in comment_lines

    # Deduplication: tma_alpha appears exactly once despite pre-existing + catalog hit
    assert sum(1 for x in ad_spy_lines if x.lower() == "tma_alpha") == 1

    # 7d tme_probe_cache.json populated & state timestamp recorded
    probe_cache = json.loads((state_dir / "tme_probe_cache.json").read_text(encoding="utf-8"))
    assert "awesome_tma_founders" in probe_cache
    cat_state = json.loads((state_dir / "tg_catalog_state.json").read_text(encoding="utf-8"))
    assert cat_state["last_run_at"]
    assert cat_state["last_run_ts"] > 0

    # Verify 7d cache prevents re-probing awesome_tma_founders over HTTP on repeat run
    tme_hits_before = sum(1 for u in fetched_urls if u == "https://t.me/awesome_tma_founders")
    tg_catalog_discovery.discover_and_replenish_catalogs(
        state_dir,
        limit=5,
        allow_paid=False,
        http_get_fn=fake_http_get,
    )
    tme_hits_after = sum(1 for u in fetched_urls if u == "https://t.me/awesome_tma_founders")
    assert tme_hits_after == tme_hits_before


def test_safe_join_public_groups_ledger_cap_isolation_and_captcha_deferral(tmp_path: Path) -> None:
    state_dir = tmp_path / "join_state"
    state_dir.mkdir(parents=True)

    # 1) Never joins without explicit client or join_fn
    no_op = tg_catalog_discovery.safe_join_public_groups(
        ["public_saas_chat"],
        state_dir=state_dir,
        client=None,
        join_fn=None,
    )
    assert no_op["joined"] == []
    assert no_op["skipped_reason"] == "no_client_or_join_fn"
    assert not (state_dir / "ro_join_ledger.json").exists()

    # 2) Refuses live outreach session paths
    fake_outreach_client = SimpleNamespace(session=SimpleNamespace(filename="/opt/crmtg/data/sessions/acc1.session"))
    with pytest.raises(ValueError, match="outreach"):
        tg_catalog_discovery.safe_join_public_groups(
            ["public_saas_chat"],
            state_dir=state_dir,
            client=fake_outreach_client,
        )

    # 3) Telethon client joins + error/captcha deferral + hard cap of MAX_RO_JOINS_PER_DAY = 2
    join_calls: list[str] = []

    class FakeJoinClient:
        def __init__(self) -> None:
            self.session = SimpleNamespace(filename=str(tmp_path / "signals_ro_1.session"))

        async def get_messages(self, target: str, limit: int = 5) -> list[Any]:
            if target == "shieldy_chat":
                return [SimpleNamespace(message="Hello! Click Shieldy button to pass captcha", sender=SimpleNamespace(username="Shieldy_bot"))]
            return []

        async def __call__(self, req: Any) -> Any:
            assert req.__class__.__name__ == "JoinChannelRequest"
            chan = str(req.channel)
            join_calls.append(chan)
            if chan == "approval_needed_chat":
                raise InviteRequestSentError(request=req)
            if chan == "readonly_chat":
                raise ChatWriteForbiddenError(request=req)
            if chan == "banned_chat":
                raise UserBannedInChannelError(request=req)
            if chan == "missrose_error_chat":
                raise RuntimeError("Blocked by MissRose captcha verification")
            return SimpleNamespace(chats=[SimpleNamespace(username=chan)])

    res1 = tg_catalog_discovery.safe_join_public_groups(
        [
            "shieldy_chat",
            "approval_needed_chat",
            "readonly_chat",
            "banned_chat",
            "missrose_error_chat",
            "valid_chat_1",
            "valid_chat_2",
            "valid_chat_3",
        ],
        state_dir=state_dir,
        client=FakeJoinClient(),
        max_joins_per_day=10,  # must still hard-cap at MAX_RO_JOINS_PER_DAY (2)
    )
    assert res1["effective_cap"] == 2
    assert res1["joined"] == ["valid_chat_1", "valid_chat_2"]
    assert res1["remaining_quota"] == 0
    assert set(res1["deferred"]) == {
        "shieldy_chat",
        "approval_needed_chat",
        "readonly_chat",
        "banned_chat",
        "missrose_error_chat",
    }
    assert "valid_chat_3" in res1["skipped"]

    # Ledger persisted with count=2
    ledger = json.loads((state_dir / "ro_join_ledger.json").read_text(encoding="utf-8"))
    assert ledger["count"] == 2
    assert set(ledger["handles"]) == {"valid_chat_1", "valid_chat_2"}

    # Manual queue persisted atomically & deduped
    queue_rows = [
        json.loads(line)
        for line in (state_dir / "manual_join_queue.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(queue_rows) == 5
    assert {r["handle"] for r in queue_rows} == {
        "shieldy_chat",
        "approval_needed_chat",
        "readonly_chat",
        "banned_chat",
        "missrose_error_chat",
    }

    # 4) Subsequent run on same UTC day:
    #    - Never retries deferred groups automatically
    #    - Enforces 0 remaining daily quota even with join_fn
    join_calls_before = list(join_calls)
    fn_calls: list[str] = []

    res2 = tg_catalog_discovery.safe_join_public_groups(
        ["shieldy_chat", "approval_needed_chat", "valid_chat_3"],
        state_dir=state_dir,
        join_fn=lambda h: fn_calls.append(h) or True,
        max_joins_per_day=2,
    )
    assert join_calls == join_calls_before
    assert fn_calls == []
    assert res2["joined"] == []
    assert set(res2["skipped"]) == {"shieldy_chat", "approval_needed_chat", "valid_chat_3"}

    # 5) Check combot signature via join_fn does not duplicate queue or crash
    res3 = tg_catalog_discovery.safe_join_public_groups(
        ["combot_chat", "combot_chat"],
        state_dir=state_dir,
        join_fn=lambda h: {"deferred": True, "reason": "Combot anti-spam active"},
    )
    # Note: daily cap already reached for valid_chat_3, while combot_chat was skipped by cap
    # Test combot_chat on a fresh state_dir so join_fn actually runs:
    fresh_state = tmp_path / "fresh_join_state"
    res4 = tg_catalog_discovery.safe_join_public_groups(
        ["combot_chat", "ok_via_fn"],
        state_dir=fresh_state,
        join_fn=lambda h: {"deferred": True, "reason": "Combot verify"} if h == "combot_chat" else {"joined": True},
        max_joins_per_day=1,
    )
    assert res4["deferred"] == ["combot_chat"]
    assert res4["joined"] == ["ok_via_fn"]
    assert res4["effective_cap"] == 1


def test_tg_ad_spy_and_channel_graph_wire_discovered_catalog_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "catalog_state"
    state_dir.mkdir(parents=True)
    (state_dir / "discovered_ad_spy_channels.txt").write_text(
        "@extra_ad_chan\nhttps://t.me/extra_ad_chan_2\nmy_skip_bot\ngonzo_ML\n",
        encoding="utf-8",
    )
    (state_dir / "discovered_graph_seeds.txt").write_text(
        "@extra_graph_seed\nhttps://t.me/s/extra_graph_seed_2\nbad_helper_bot\nstartupoftheday\n",
        encoding="utf-8",
    )

    # Ensure tg_ad_spy CLI --help shows --state-dir
    spy_help = subprocess.run(
        [sys.executable, str(ROOT / "leadgen" / "tg_ad_spy.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert spy_help.returncode == 0
    assert "--state-dir" in spy_help.stdout

    # Ensure tg_channel_graph CLI --help shows --state-dir
    graph_help = subprocess.run(
        [sys.executable, str(ROOT / "leadgen" / "tg_channel_graph.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert graph_help.returncode == 0
    assert "--state-dir" in graph_help.stdout

    monkeypatch.setenv("TG_AD_SPY_CHANNELS", "")
    monkeypatch.setenv("TG_GRAPH_SEED_CHANNELS", "")
    monkeypatch.setenv("TG_API_ID", "2040")
    monkeypatch.setenv("TG_API_HASH", "fakehash123")

    class DummyTelethonClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def start(self) -> DummyTelethonClient:
            return self

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(tg_ad_spy, "TelegramClient", DummyTelethonClient)
    monkeypatch.setattr(tg_channel_graph, "TelegramClient", DummyTelethonClient)

    spy_captured: dict[str, Any] = {}

    async def fake_scan_ad_channels(**kwargs: Any) -> list[dict[str, Any]]:
        spy_captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("", encoding="utf-8")
        return []

    monkeypatch.setattr(tg_ad_spy, "scan_ad_channels", fake_scan_ad_channels)

    # Test tg_ad_spy with --state-dir
    asyncio.run(
        tg_ad_spy._async_main(
            argparse.Namespace(
                session="dummy.session",
                channels="",
                no_default_channels=False,
                state_dir=str(state_dir),
                msg_limit=10,
                seen=str(tmp_path / "other_seen.json"),
                output=str(tmp_path / "spy_out.jsonl"),
                delay=0.0,
            )
        )
    )
    spy_channels = spy_captured["channels"]
    assert spy_channels[: len(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS)] == list(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS)
    assert "extra_ad_chan" in spy_channels
    assert "extra_ad_chan_2" in spy_channels
    assert "my_skip_bot" not in spy_channels
    assert sum(1 for c in spy_channels if c.lower() == "gonzo_ml") == 1

    # Test tg_ad_spy via LEADGEN_STATE_DIR env when state_dir is empty
    spy_captured.clear()
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(state_dir))
    asyncio.run(
        tg_ad_spy._async_main(
            argparse.Namespace(
                session="dummy.session",
                channels="",
                no_default_channels=False,
                msg_limit=10,
                seen=str(tmp_path / "other_seen.json"),
                output=str(tmp_path / "spy_out_env.jsonl"),
                delay=0.0,
            )
        )
    )
    assert "extra_ad_chan" in spy_captured["channels"]

    # Test tg_channel_graph with --state inside state_dir (resolved via state_path dir)
    graph_captured: dict[str, Any] = {}

    async def fake_crawl_channel_graph(**kwargs: Any) -> list[dict[str, Any]]:
        graph_captured.update(kwargs)
        Path(kwargs["output_path"]).write_text("", encoding="utf-8")
        return []

    monkeypatch.setattr(tg_channel_graph, "crawl_channel_graph", fake_crawl_channel_graph)

    asyncio.run(
        tg_channel_graph._async_main(
            argparse.Namespace(
                session="dummy.session",
                seeds="",
                no_default_seeds=False,
                depth=2,
                limit=10,
                state=str(state_dir / "tg_channel_graph_seen.json"),
                output=str(tmp_path / "graph_out.jsonl"),
                delay=0.0,
            )
        )
    )
    graph_seeds = graph_captured["seeds"]
    assert graph_seeds[: len(tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS)] == list(
        tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS
    )
    assert "extra_graph_seed" in graph_seeds
    assert "extra_graph_seed_2" in graph_seeds
    assert "bad_helper_bot" not in graph_seeds
    assert sum(1 for s in graph_seeds if s.lower() == "startupoftheday") == 1

    # Test tg_channel_graph with explicit --state-dir overriding state_path dir
    graph_captured.clear()
    asyncio.run(
        tg_channel_graph._async_main(
            argparse.Namespace(
                session="dummy.session",
                seeds="",
                no_default_seeds=False,
                state_dir=str(state_dir),
                depth=2,
                limit=10,
                state=str(tmp_path / "isolated_seen.json"),
                output=str(tmp_path / "graph_out2.jsonl"),
                delay=0.0,
            )
        )
    )
    assert "extra_graph_seed" in graph_captured["seeds"]


def test_edge_cases_cli_tgstat_block_day_rollover_adspy_rotation_and_graph_replenish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1) Direct HTTP fetch to tgstat.com MUST raise ValueError
    with pytest.raises(ValueError, match="tgstat"):
        tg_catalog_discovery._http_get_text("https://tgstat.com/channel/@saasgrowth")

    # 2) UTC day rollover in ro_join_ledger.json does not block today's 2 joins, and max_joins_per_day=0 blocks all
    rollover_dir = tmp_path / "rollover"
    rollover_dir.mkdir(parents=True)
    (rollover_dir / "ro_join_ledger.json").write_text(
        json.dumps(
            {
                "date": "2020-01-01",
                "count": 2,
                "handles": ["old_chat_1", "old_chat_2"],
                "days": {"2020-01-01": {"count": 2, "handles": ["old_chat_1", "old_chat_2"]}},
            }
        ),
        encoding="utf-8",
    )
    zero_cap = tg_catalog_discovery.safe_join_public_groups(
        ["new_chat_1"],
        state_dir=rollover_dir,
        join_fn=lambda h: True,
        max_joins_per_day=0,
    )
    assert zero_cap["effective_cap"] == 0
    assert zero_cap["joined"] == []
    assert zero_cap["skipped"] == ["new_chat_1"]

    new_day = tg_catalog_discovery.safe_join_public_groups(
        ["new_chat_1", "new_chat_2", "new_chat_3"],
        state_dir=rollover_dir,
        join_fn=lambda h: True,
        max_joins_per_day=2,
    )
    assert new_day["joined"] == ["new_chat_1", "new_chat_2"]
    assert new_day["skipped"] == ["new_chat_3"]

    # 3) CLI main() with --state-dir, --limit, --no-paid, --safe-join, --max-joins-per-day
    cli_dir = tmp_path / "cli_state"
    monkeypatch.setattr(
        tg_catalog_discovery.requests,
        "get",
        lambda url, **kw: SimpleNamespace(
            status_code=200,
            text='<a href="https://t.me/cli_tma_chan">CLI TMA</a>',
        ),
    )
    monkeypatch.setattr(
        tg_web_client,
        "probe_tme_handle_web",
        lambda handle, **kw: {
            "handle": handle,
            "kind": "channel",
            "title": f"Title {handle}",
            "bio": "SaaS & TMA founders chat",
            "subscribers": 1500,
            "cached": False,
        },
    )
    rc = tg_catalog_discovery.main(
        [
            "--state-dir",
            str(cli_dir),
            "--limit",
            "3",
            "--no-paid",
            "--safe-join",
            "--max-joins-per-day",
            "5",
        ]
    )
    assert rc == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert "cli_tma_chan" in cli_payload["discovered"]
    assert cli_payload["paid_search_used"] is False
    assert cli_payload["safe_join"]["effective_cap"] == 2
    assert cli_payload["safe_join"]["joined"] == []

    # 4) tg_ad_spy caps & rotates > MAX_DISCOVERED_AD_SPY_CHANNELS cleanly via seen cursor
    rot_dir = tmp_path / "rot_state"
    rot_dir.mkdir(parents=True)
    many_channels = [f"disc_chan_{i:03d}" for i in range(tg_ad_spy.MAX_DISCOVERED_AD_SPY_CHANNELS + 30)]
    (rot_dir / "discovered_ad_spy_channels.txt").write_text("\n".join(many_channels) + "\n", encoding="utf-8")
    seen_file = rot_dir / "tg_ad_spy_seen.json"
    seen_file.write_text(json.dumps({"seen": [], "cursor": 5}), encoding="utf-8")
    resolved_chs = tg_ad_spy.resolve_default_ad_spy_channels(state_dir=str(rot_dir), seen_path=str(seen_file))
    extra_chs = resolved_chs[len(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS) :]
    assert len(extra_chs) == tg_ad_spy.MAX_DISCOVERED_AD_SPY_CHANNELS
    assert extra_chs[0] == "disc_chan_005"

    # 5) tg_channel_graph replenishment picks up discovered_graph_seeds.txt when frontier & seeds are exhausted
    rep_dir = tmp_path / "rep_state"
    rep_dir.mkdir(parents=True)
    rep_state_file = rep_dir / "tg_channel_graph_seen.json"
    all_exhausted = ["seed_done"] + [s.lower() for s in tg_channel_graph.FALLBACK_EN_GRAPH_SEEDS]
    rep_state_file.write_text(
        json.dumps({"seen": all_exhausted, "expanded": all_exhausted, "frontier": []}),
        encoding="utf-8",
    )
    (rep_dir / "discovered_graph_seeds.txt").write_text("replenished_tma_seed\n", encoding="utf-8")

    visited_targets: list[str] = []

    class FakeReplenishClient:
        async def get_entity(self, target: Any) -> Any:
            visited_targets.append(str(target))
            raise ValueError("stop")

    asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=FakeReplenishClient(),
            seeds=["seed_done"],
            state_path=str(rep_state_file),
            output_path=str(tmp_path / "rep_out.jsonl"),
            delay=0.0,
            max_runtime_sec=5.0,
            web_posts_fn=lambda ch, lim: ({}, []),
            probe_fn=lambda h: {"handle": h, "kind": "unknown"},
        )
    )
    assert "replenished_tma_seed" in visited_targets


def test_skeptic_group_a_ro_isolation_concurrent_cap_and_nonempty_frontier(tmp_path: Path) -> None:
    # 1) Relative data/sessions paths, sessions/vlad.session, data/sessions/signals_ro.session, and phone_checker_1 must be rejected
    bad_sessions = [
        "data/sessions/acc39.session",
        "data/sessions/signals_ro.session",
        "sessions/vlad.session",
        "/opt/crmtg/data/scraper_sessions/phone_checker_1.session",
        "outreach_helper.session",
    ]
    for bad in bad_sessions:
        assert tg_catalog_discovery._is_isolated_ro_session_path(bad) is False
        bad_client = SimpleNamespace(session=SimpleNamespace(filename=bad))
        with pytest.raises(ValueError):
            tg_catalog_discovery.safe_join_public_groups(
                ["public_saas_chat"],
                state_dir=tmp_path / "bad_sess_state",
                client=bad_client,
            )

    # phone_checker_2 inside scraper_sessions/ and signals_ro* inside scraper_sessions/ pass isolation
    assert tg_catalog_discovery._is_isolated_ro_session_path("/opt/crmtg/data/scraper_sessions/signals_ro.session") is True
    assert tg_catalog_discovery._is_isolated_ro_session_path("/opt/crmtg/data/scraper_sessions/phone_checker_2.session") is True

    # 2) Concurrent safe_join_public_groups calls cannot race past MAX_RO_JOINS_PER_DAY = 2
    race_state = tmp_path / "race_join_state"
    race_state.mkdir(parents=True)
    calls_lock = threading.Lock()
    actual_joins: list[str] = []

    def slow_join_fn(handle: str) -> dict[str, Any]:
        time.sleep(0.03)
        with calls_lock:
            actual_joins.append(handle)
        return {"joined": True}

    def _worker(idx: int) -> dict[str, Any]:
        return tg_catalog_discovery.safe_join_public_groups(
            [f"race_chat_{idx}_a", f"race_chat_{idx}_b", f"race_chat_{idx}_c"],
            state_dir=race_state,
            join_fn=slow_join_fn,
            max_joins_per_day=2,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_worker, range(4)))

    assert len(actual_joins) == 2
    assert sum(len(r["joined"]) for r in results) == 2
    ledger = json.loads((race_state / "ro_join_ledger.json").read_text(encoding="utf-8"))
    assert ledger["count"] == 2
    assert len(ledger["handles"]) == 2

    # 3) Captcha dict with ok=True without explicit joined=True defers WITHOUT burning quota
    cap_state = tmp_path / "captcha_ok_state"
    res_cap = tg_catalog_discovery.safe_join_public_groups(
        ["captcha_ok_chat"],
        state_dir=cap_state,
        join_fn=lambda h: {"ok": True, "captcha": True, "reason": "Shieldy verify"},
        max_joins_per_day=2,
    )
    assert res_cap["joined"] == []
    assert res_cap["deferred"] == ["captcha_ok_chat"]
    assert res_cap["used_after"] == 0

    # 4) tg_channel_graph injects unseen discovered_graph_seeds.txt even when frontier is non-empty
    nonempty_dir = tmp_path / "nonempty_frontier"
    nonempty_dir.mkdir(parents=True)
    nonempty_state = nonempty_dir / "tg_channel_graph_seen.json"
    nonempty_state.write_text(
        json.dumps({"seen": ["seed_old"], "expanded": [], "frontier": ["frontier_pending"]}),
        encoding="utf-8",
    )
    (nonempty_dir / "discovered_graph_seeds.txt").write_text("fresh_auto_seed\n", encoding="utf-8")
    visited_ne: list[str] = []

    class FakeNonEmptyClient:
        async def get_entity(self, target: Any) -> Any:
            visited_ne.append(str(target))
            raise ValueError("stop")

    asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=FakeNonEmptyClient(),
            seeds=["seed_old"],
            state_path=str(nonempty_state),
            output_path=str(tmp_path / "ne_out.jsonl"),
            delay=0.0,
            max_runtime_sec=5.0,
            web_posts_fn=lambda ch, lim: ({}, []),
            probe_fn=lambda h: {"handle": h, "kind": "unknown"},
        )
    )
    assert "fresh_auto_seed" in visited_ne

