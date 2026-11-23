from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
from telethon.tl.functions.channels import (
    GetChannelRecommendationsRequest,
    GetFullChannelRequest,
    GetParticipantsRequest,
)
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    ChatAdminRights,
    PeerChannel,
    User,
)
from telethon.tl.types.channels import ChannelParticipants

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import (  # noqa: E402
    tg_ad_spy,
    tg_channel_graph,
    tg_group_admins,
    tg_scrape_posters,
    tg_web_client,
)


def _user_html(title: str, handle: str, bio: str = "") -> str:
    desc = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">@{handle}</div>'
        f"{desc}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">Send Message</a></div>'
        "</div></body></html>"
    )


def _channel_html(title: str, extra: str = "18 900 subscribers", bio: str = "") -> str:
    desc = f'<div class="tgme_page_description">{bio}</div>' if bio else ""
    return (
        "<html><body><div class='tgme_page'>"
        f'<div class="tgme_page_title"><span>{title}</span></div>'
        f'<div class="tgme_page_extra">{extra}</div>'
        f"{desc}"
        '<div class="tgme_page_action"><a class="tgme_action_button_new" href="tg://resolve">View in Telegram</a></div>'
        "</div></body></html>"
    )


def _tme_s_channel_html(title: str, about: str, subs: str, posts: list[str]) -> str:
    msg_blocks = "".join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message_text">{p}</div></div>'
        for p in posts
    )
    return (
        "<html><body>"
        f'<div class="tgme_channel_info_header_title">{title}</div>'
        f'<div class="tgme_channel_info_description">{about}</div>'
        f'<div class="tgme_channel_info_counter"><span class="counter_value">{subs}</span>'
        '<span class="counter_type">subscribers</span></div>'
        f"{msg_blocks}"
        "</body></html>"
    )


def test_probe_tme_handle_web_classifications_and_7d_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tg_web_client.TME_CACHE_TTL_SEC == 7 * 86400
    monkeypatch.setenv("LEADGEN_STATE_DIR", str(tmp_path))

    seen_calls: list[tuple[str, float, bool]] = []

    def fake_http(url: str, *, timeout: float = 0.0, allow_redirects: bool = True) -> Any:
        seen_calls.append((url, timeout, allow_redirects))
        if url.endswith("/alice_ceo"):
            return SimpleNamespace(
                status_code=200,
                text=_user_html("Alice Founder", "alice_ceo", "Building VoiceApp.io"),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/ai_news_hub"):
            return SimpleNamespace(
                status_code=200,
                text=_channel_html("AI News Hub", "12.4K subscribers", "Official channel @alice_ceo"),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/founders_group"):
            return SimpleNamespace(
                status_code=200,
                text=_channel_html("Founders Group", "1 250 members, 84 online", "Group chat"),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/my_helper_bot"):
            return SimpleNamespace(
                status_code=200,
                text=(
                    '<div class="tgme_page_title">Helper</div>'
                    '<div class="tgme_page_extra">@my_helper_bot</div>'
                    '<div class="tgme_page_action"><a class="tgme_action_button_new">Start Bot</a></div>'
                ),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/monthly_bot"):
            return SimpleNamespace(
                status_code=200,
                text=(
                    '<div class="tgme_page_title">Utility Tool</div>'
                    '<div class="tgme_page_extra">45 000 monthly users</div>'
                    '<div class="tgme_page_action"><a class="tgme_action_button_new">Open</a></div>'
                ),
                headers={},
                is_redirect=False,
            )
        if url.endswith("/redir_missing"):
            return SimpleNamespace(
                status_code=302,
                text="",
                headers={"Location": "https://telegram.org/"},
                is_redirect=True,
            )
        if url.endswith("/stub_missing"):
            return SimpleNamespace(
                status_code=200,
                text='<div class="tgme_page"><div class="tgme_page_description">If you have <strong>Telegram</strong>, you can contact @stub_missing right away.</div></div>',
                headers={},
                is_redirect=False,
            )
        return SimpleNamespace(status_code=503, text="", headers={}, is_redirect=False)

    u = tg_web_client.probe_tme_handle_web("@alice_ceo", http_get_fn=fake_http)
    assert u == {
        "handle": "alice_ceo",
        "kind": "user",
        "title": "Alice Founder",
        "bio": "Building VoiceApp.io",
        "subscribers": 0,
        "cached": False,
    }
    assert seen_calls[-1] == ("https://t.me/alice_ceo", 5.0, False)

    # Cached hit: does NOT call fake_http again
    u_cached = tg_web_client.probe_tme_handle_web("alice_ceo", http_get_fn=fake_http)
    assert u_cached["cached"] is True
    assert u_cached["kind"] == "user"
    assert len(seen_calls) == 1

    ch = tg_web_client.probe_tme_handle_web("ai_news_hub", http_get_fn=fake_http)
    assert ch["kind"] == "channel"
    assert ch["subscribers"] == 12400

    grp = tg_web_client.probe_tme_handle_web("founders_group", http_get_fn=fake_http)
    assert grp["kind"] == "channel"
    assert grp["subscribers"] == 1250

    b1 = tg_web_client.probe_tme_handle_web("my_helper_bot", http_get_fn=fake_http)
    b2 = tg_web_client.probe_tme_handle_web("monthly_bot", http_get_fn=fake_http)
    assert b1["kind"] == "bot"
    assert b2["kind"] == "bot"

    m1 = tg_web_client.probe_tme_handle_web("redir_missing", http_get_fn=fake_http)
    m2 = tg_web_client.probe_tme_handle_web("stub_missing", http_get_fn=fake_http)
    assert m1["kind"] == "missing"
    assert m2["kind"] == "missing"

    # Transient 503 -> "unknown" and MUST NOT be cached
    unk = tg_web_client.probe_tme_handle_web("flaky_503", http_get_fn=fake_http)
    assert unk["kind"] == "unknown"
    assert unk["cached"] is False
    unk_retry = tg_web_client.probe_tme_handle_web("flaky_503", http_get_fn=fake_http)
    assert unk_retry["cached"] is False
    assert sum(1 for u_url, _, _ in seen_calls if u_url.endswith("/flaky_503")) == 2

    cache_file = tmp_path / "tme_probe_cache.json"
    assert cache_file.is_file()
    cache_payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "alice_ceo" in cache_payload
    assert "flaky_503" not in cache_payload

    # Expired entry (> 7d) triggers fresh probe
    cache_payload["alice_ceo"]["ts"] = time.time() - (7 * 86400 + 60)
    cache_file.write_text(json.dumps(cache_payload), encoding="utf-8")
    u_expired = tg_web_client.probe_tme_handle_web("alice_ceo", http_get_fn=fake_http)
    assert u_expired["cached"] is False

    # Thread-safe concurrent cache writes produce valid JSON with all handles
    def _probe_worker(idx: int) -> dict[str, Any]:
        return tg_web_client.probe_tme_handle_web(
            f"user_{idx}",
            http_get_fn=lambda url: _user_html(f"User {idx}", f"user_{idx}", "Bio"),
            cache_path=tmp_path / "concurrent_cache.json",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_probe_worker, range(16)))
    assert len(results) == 16
    concurrent_data = json.loads((tmp_path / "concurrent_cache.json").read_text(encoding="utf-8"))
    assert len(concurrent_data) == 16


def test_fetch_channels_info_batch_and_posts_waterfall() -> None:
    # 1) Batch info: 25 handles -> chunks of 20 + 5, partial response falls back to t.me/s/
    handles = [f"chan_{i}" for i in range(25)]
    batch_queries: list[str] = []
    recorded_costs: list[tuple[str, float]] = []

    def fake_treg_batch(endpoint: str, *, query: dict[str, str] | None = None, **_: Any) -> Any:
        assert endpoint == "tikhub.x.telegram-web-batch-channel-info"
        chs = (query or {}).get("channels", "")
        batch_queries.append(chs)
        batch_list = chs.split(",")
        # Omit the very last handle to verify graceful t.me/s/ fallback for missing items
        return {
            "data": [
                {
                    "username": ch,
                    "title": f"Title {ch}",
                    "description": f"About {ch}",
                    "subscribers": 2500,
                }
                for ch in batch_list
                if ch != "chan_24"
            ]
        }

    batch_res = tg_web_client.fetch_channels_info_batch(
        handles,
        treg_fn=fake_treg_batch,
        allow_paid=True,
        budget_guard_fn=lambda *a, **k: True,
        cost_record_fn=lambda **kw: recorded_costs.append((kw["endpoint"], kw["cost_usd"])),
        http_get_fn=lambda url: _tme_s_channel_html("Title chan_24", "Web about 24", "3.1K", [])
        if url.endswith("/chan_24")
        else "",
    )
    assert len(batch_queries) == 2
    assert len(batch_queries[0].split(",")) == 20
    assert len(batch_queries[1].split(",")) == 5
    assert len(recorded_costs) == 2
    assert batch_res["chan_0"]["title"] == "Title chan_0"
    assert batch_res["chan_24"]["title"] == "Title chan_24"
    assert batch_res["chan_24"]["subscribers"] == 3100

    # Budget stopped -> 0 treg calls, falls back to t.me/s/
    fallback_res = tg_web_client.fetch_channels_info_batch(
        ["chan_a"],
        treg_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("Paid batch must be skipped when budget denied")),
        budget_guard_fn=lambda *a, **k: False,
        http_get_fn=lambda url: _tme_s_channel_html("Free Chan A", "Founder @alice", "900", []),
    )
    assert fallback_res["chan_a"]["title"] == "Free Chan A"
    assert fallback_res["chan_a"]["about"] == "Founder @alice"

    # 2) fetch_channel_posts_web order: free t.me/s/ -> tikhub ($0.001) -> crawl4ai ($0.0009)
    info1, posts1 = tg_web_client.fetch_channel_posts_web(
        "free_chan",
        limit=10,
        http_get_fn=lambda url: _tme_s_channel_html("Free Chan", "Bio", "4.2K", ["Post 1", "Post 2"]),
        treg_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("Should not hit treg when free t.me/s/ has posts")),
    )
    assert info1["title"] == "Free Chan"
    assert posts1 == ["Post 1", "Post 2"]

    endpoints_called: list[str] = []
    costs_posts: list[tuple[str, float]] = []

    def treg_tikhub_ok(endpoint: str, **_: Any) -> Any:
        endpoints_called.append(endpoint)
        if endpoint == "tikhub.x.telegram-web-fetch-channel-posts":
            return {"data": {"title": "TK Chan", "posts": [{"text": "TK post"}]}}
        raise AssertionError("crawl4ai should not run when tikhub succeeded")

    info2, posts2 = tg_web_client.fetch_channel_posts_web(
        "tk_chan",
        http_get_fn=lambda url: "",
        treg_fn=treg_tikhub_ok,
        cost_record_fn=lambda **kw: costs_posts.append((kw["endpoint"], kw["cost_usd"])),
    )
    assert endpoints_called == ["tikhub.x.telegram-web-fetch-channel-posts"]
    assert costs_posts == [("tikhub.x.telegram-web-fetch-channel-posts", 0.001)]
    assert posts2 == ["TK post"]

    endpoints_called.clear()
    costs_posts.clear()

    def treg_crawl4ai_ok(endpoint: str, **_: Any) -> Any:
        endpoints_called.append(endpoint)
        if endpoint == "tikhub.x.telegram-web-fetch-channel-posts":
            return {"data": {"posts": []}}
        if endpoint == "crawl4ai.telegram.channel.posts":
            return {"rows": [{"text": "Crawl4AI post"}]}
        return None

    _, posts3 = tg_web_client.fetch_channel_posts_web(
        "c4_chan",
        http_get_fn=lambda url: "",
        treg_fn=treg_crawl4ai_ok,
        cost_record_fn=lambda **kw: costs_posts.append((kw["endpoint"], kw["cost_usd"])),
    )
    assert endpoints_called == [
        "tikhub.x.telegram-web-fetch-channel-posts",
        "crawl4ai.telegram.channel.posts",
    ]
    assert costs_posts[-1] == ("crawl4ai.telegram.channel.posts", 0.0009)
    assert posts3 == ["Crawl4AI post"]


def test_tg_ad_spy_junk_filter_web_first_and_100_plus_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS) >= 100
    assert len({c.lower() for c in tg_ad_spy.DEFAULT_AD_SPY_CHANNELS}) >= 100

    # Junk targets filtered
    assert tg_ad_spy.is_ad_spy_junk_target(url="https://fragment.com/username/saas") is True
    assert tg_ad_spy.is_ad_spy_junk_target(handle="fragment_monitor") is True
    assert tg_ad_spy.is_ad_spy_junk_target(url="https://tonviewer.com/EQ123") is True
    assert tg_ad_spy.is_ad_spy_junk_target(url="https://getgems.io/collection/abc") is True
    assert tg_ad_spy.is_ad_spy_junk_target(text="Аукцион юзернеймов и номеров +888") is True
    assert tg_ad_spy.is_ad_spy_junk_target(text="Забирай фриспины в онлайн казино 1win!") is True
    assert tg_ad_spy.is_ad_spy_junk_target(text="Огромный розыгрыш призов / giveaway airdrop") is True
    assert tg_ad_spy.is_ad_spy_junk_target(handle="helper_support_bot") is True

    # Real B2B SaaS / AI / Ecom targets NOT filtered
    assert tg_ad_spy.is_ad_spy_junk_target(
        url="https://voicecraft.ai",
        handle="voicecraft_ceo",
        text="Sponsored: Try VoiceCraft AI agents for customer support teams!",
    ) is False

    # scan_ad_channels skips junk posts BEFORE _resolve_tg_target is called
    resolve_calls: list[str] = []

    async def spy_resolve(client: Any, handle: str, **kw: Any) -> dict:
        resolve_calls.append(handle)
        return {"id": 101, "username": handle, "name": "Real Founder", "company": "", "bio": "Founder", "site": ""}

    monkeypatch.setattr(tg_ad_spy, "_resolve_tg_target", spy_resolve)
    posts_map = {
        "ai_newz": [
            "Sponsored: Покупайте редкие ники на https://fragment.com/username/ai — @fragment_monitor #реклама",
            "Sponsored: Check TON address on https://tonviewer.com/EQABC @ton_whale_alert #ad",
            "Brought to you by @good_founder — scaling AI video SaaS https://goodsaas.ai/?utm_source=tg",
        ]
    }
    rows = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=None,
            channels=["ai_newz"],
            seen_path=str(tmp_path / "spy_seen.json"),
            output_path=str(tmp_path / "spy_out.jsonl"),
            delay=0.0,
            fetch_html=lambda u: "",
            public_posts_fn=lambda ch, lim: posts_map.get(ch, []),
        )
    )
    assert resolve_calls == ["good_founder"]
    assert [r["username"] for r in rows] == ["good_founder"]


def test_tg_ad_spy_resolve_tg_target_web_first_and_unknown_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telethon_calls: list[str] = []

    class FakeClient:
        async def get_entity(self, target: str) -> Any:
            telethon_calls.append(f"get_entity:{target}")
            return SimpleNamespace(id=777, username=target, first_name="Fallback", last_name="User")

        async def __call__(self, req: Any) -> Any:
            telethon_calls.append(req.__class__.__name__)
            return SimpleNamespace(
                users=[SimpleNamespace(id=777, username="unk_user", first_name="Unk", last_name="User")],
                full_user=SimpleNamespace(about="Unknown fallback bio"),
            )

    def fake_probe(handle: str, **_: Any) -> dict[str, Any]:
        h = handle.lstrip("@").lower()
        if h == "web_founder":
            return {"handle": h, "kind": "user", "title": "Web Founder", "bio": "SaaS CEO", "subscribers": 0, "cached": False}
        if h == "web_chan":
            return {"handle": h, "kind": "channel", "title": "Web Chan", "bio": "Founder: @chan_ceo https://webchan.io", "subscribers": 1500, "cached": False}
        if h == "chan_ceo":
            return {"handle": h, "kind": "user", "title": "Chan CEO", "bio": "CEO", "subscribers": 0, "cached": False}
        if h == "dead_handle":
            return {"handle": h, "kind": "missing", "title": "", "bio": "", "subscribers": 0, "cached": False}
        return {"handle": h, "kind": "unknown", "title": "", "bio": "", "subscribers": 0, "cached": False}

    monkeypatch.setattr(tg_web_client, "probe_tme_handle_web", fake_probe)
    monkeypatch.setattr(
        tg_web_client,
        "fetch_channel_posts_web",
        lambda ch, **kw: ({"title": "Web Chan", "about": "Founder: @chan_ceo https://webchan.io", "subscribers": 1500}, ["Shipped v2!"]),
    )

    client = FakeClient()

    # 1) User resolved purely via Web -> 0 Telethon calls
    r_user = asyncio.run(tg_ad_spy._resolve_tg_target(client, "web_founder"))
    assert r_user["username"] == "web_founder"
    assert r_user["name"] == "Web Founder"
    assert telethon_calls == []

    # 2) Channel + founder resolved purely via Web -> 0 Telethon calls
    r_chan = asyncio.run(tg_ad_spy._resolve_tg_target(client, "web_chan"))
    assert r_chan["username"] == "chan_ceo"
    assert r_chan["site"] == "https://webchan.io"
    assert telethon_calls == []

    # 3) Missing resolved via Web -> returns empty with 0 Telethon calls
    r_miss = asyncio.run(tg_ad_spy._resolve_tg_target(client, "dead_handle"))
    assert r_miss["username"] == ""
    assert telethon_calls == []

    # 4) Web returns "unknown" -> falls back to Telethon client
    r_unk = asyncio.run(tg_ad_spy._resolve_tg_target(client, "unk_user"))
    assert r_unk["username"] == "unk_user"
    assert r_unk["id"] == 777
    assert "get_entity:unk_user" in telethon_calls


def test_tg_channel_graph_seeds_web_first_and_unexpanded_frontier_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS) >= 58
    assert len(tg_channel_graph.FALLBACK_EN_GRAPH_SEEDS) >= 50

    rpc_log: list[str] = []

    class WebFirstGraphClient:
        async def get_entity(self, target: Any) -> Any:
            key = str(target).lstrip("@").lower()
            rpc_log.append(f"get_entity:{key}")
            return SimpleNamespace(id=100 if key == "seed_web" else 200, username=key, title=key.title())

        async def get_messages(self, entity: Any, limit: int = 5) -> list[Any]:
            raise AssertionError("client.get_messages must NOT be called when fetch_channel_posts_web provides data")

        async def __call__(self, req: Any) -> Any:
            name = req.__class__.__name__
            key = getattr(getattr(req, "channel", None), "username", "")
            rpc_log.append(f"{name}:{key}")
            if name == "GetFullChannelRequest":
                if key == "seed_web":
                    raise AssertionError("GetFullChannelRequest must be skipped when linked_chat_id is already present")
                return SimpleNamespace(
                    full_chat=SimpleNamespace(about="ignored", participants_count=2000, linked_chat_id=888111),
                    chats=[SimpleNamespace(id=200, username=key, title="Need Linked")],
                )
            if name == "GetChannelRecommendationsRequest":
                if key == "seed_web":
                    return SimpleNamespace(
                        chats=[SimpleNamespace(id=200, username="need_linked_chan", title="Need Linked")]
                    )
                return SimpleNamespace(chats=[])
            if name == "GetFullUserRequest":
                raise AssertionError("GetFullUserRequest must NOT be called when probe_tme_handle_web resolves user")
            raise NotImplementedError(name)

    def fake_posts_web(ch: str, limit: int = 20, **_: Any) -> tuple[dict[str, Any], list[str]]:
        if ch == "seed_web":
            return (
                {
                    "username": "seed_web",
                    "title": "Seed Web",
                    "about": "Founder: @seed_web_ceo https://seedweb.ai",
                    "subscribers": 3100,
                    "linked_chat_id": 777111,
                },
                ["Launching SeedWeb!"],
            )
        if ch == "need_linked_chan":
            return (
                {
                    "username": "need_linked_chan",
                    "title": "Need Linked",
                    "about": "CEO @need_linked_ceo https://needlinked.io",
                    "subscribers": 1900,
                },
                ["Post from web"],
            )
        return {}, []

    monkeypatch.setattr(tg_web_client, "fetch_channel_posts_web", fake_posts_web)
    monkeypatch.setattr(
        tg_web_client,
        "probe_tme_handle_web",
        lambda handle, **kw: {
            "handle": handle,
            "kind": "user",
            "title": handle.replace("_", " ").title(),
            "bio": "Founder",
            "subscribers": 0,
            "cached": False,
        },
    )

    state_file = tmp_path / "cg_seen.json"
    # Pre-seed state with already-seen+expanded 'old_seen' AND unexpanded frontier item 'seed_web'
    state_file.write_text(
        json.dumps(
            {
                "seen": ["old_seen"],
                "expanded": ["old_seen"],
                "frontier": [["old_seen", 0], ["seed_web", 0]],
            }
        ),
        encoding="utf-8",
    )
    linked_file = tmp_path / "discovered_comment_channels.txt"
    out_file = tmp_path / "cg_out.jsonl"

    rows = asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=WebFirstGraphClient(),
            seeds=["old_seen"],
            depth=2,
            limit=5,
            state_path=str(state_file),
            output_path=str(out_file),
            linked_chats_path=str(linked_file),
            delay=0.0,
        )
    )
    assert [r["username"] for r in rows] == ["seed_web_ceo", "need_linked_ceo"]
    assert "GetFullChannelRequest:seed_web" not in rpc_log
    assert "GetChannelRecommendationsRequest:seed_web" in rpc_log
    assert "GetFullChannelRequest:need_linked_chan" in rpc_log

    # old_seen is preserved in seen, linked_chat_id 777111 & 888111 saved
    saved_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "old_seen" in saved_state["seen"]
    assert "seed_web" in saved_state["seen"]
    linked_lines = linked_file.read_text(encoding="utf-8").splitlines()
    assert "id777111" in linked_lines
    assert "id888111" in linked_lines


def test_tg_scrape_posters_hybrid_mode_cooldown_and_guards(tmp_path: Path) -> None:
    # 1) CLI accepts --mode hybrid
    help_proc = subprocess.run(
        [sys.executable, str(ROOT / "leadgen" / "tg_scrape_posters.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    assert "hybrid" in help_proc.stdout

    # 2) Rule 5 direct send guard still blocks
    out_path = tmp_path / "out.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        with pytest.raises(RuntimeError, match="Rule 5"):
            asyncio.run(
                tg_scrape_posters.scrape_group(
                    SimpleNamespace(),
                    "test_group",
                    10,
                    0.0,
                    fh,
                    [],
                    mode="hybrid",
                    send=True,
                )
            )

    # 3) Hybrid mode: runs bounded iter_participants (when due) + incremental posters,
    # deduplicates overlap while preserving poster sample, and respects 7d cooldown on 2nd run.
    iter_limits: list[int] = []
    lurker = SimpleNamespace(id=101, username="lurker_ceo", first_name="Lurk", last_name="Founder", bot=False, deleted=False)
    both_user = SimpleNamespace(id=102, username="active_founder", first_name="Act", last_name="Founder", bot=False, deleted=False)
    poster_only = SimpleNamespace(id=103, username="new_poster", first_name="New", last_name="Poster", bot=False, deleted=False)

    msgs_run1 = [
        SimpleNamespace(id=501, sender_id=102, message="Scaling SaaS at https://actapp.io", post=False, from_id=None, sender=both_user),
    ]
    msgs_run2 = [
        SimpleNamespace(id=502, sender_id=103, message="Launched AI app https://newapp.ai", post=False, from_id=None, sender=poster_only),
    ]

    class HybridFakeClient:
        def __init__(self, msgs: list[Any], *, fail_members: bool = False) -> None:
            self.msgs = msgs
            self.fail_members = fail_members

        async def get_entity(self, target: Any) -> Any:
            return Channel(
                id=555,
                title="Founders",
                photo=None,
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                megagroup=True,
                username="founders_grp",
                access_hash=123,
            )

        async def iter_participants(self, entity: Any, limit: int = 100):
            iter_limits.append(limit)
            if self.fail_members:
                raise RuntimeError("Participants hidden")
            for u in (lurker, both_user):
                yield u

        async def get_messages(self, entity: Any, limit: int = 400):
            return self.msgs

        async def iter_messages(self, entity: Any, min_id: int = 0, limit: int = 400, reverse: bool = True):
            for m in self.msgs:
                if m.id > min_id:
                    yield m

    now_ts = time.time()
    profile_cache = {
        str(uid): {
            "fetched_at": now_ts,
            "profile": {
                "id": uid,
                "username": uname,
                "first_name": "First",
                "last_name": "Last",
                "bio": "Founder building SaaS https://example.ai",
                "bot": False,
                "deleted": False,
                "verified": False,
                "premium": False,
            },
        }
        for uid, uname in ((101, "lurker_ceo"), (102, "active_founder"), (103, "new_poster"))
    }
    cursors: dict[str, Any] = {}

    with out_path.open("w", encoding="utf-8") as fh:
        rows1 = asyncio.run(
            tg_scrape_posters.scrape_group(
                HybridFakeClient(msgs_run1),
                "founders_grp",
                50,
                0.0,
                fh,
                [],
                mode="hybrid",
                member_limit=0,  # exercises `member_limit or 300` bound
                profile_cache=profile_cache,
                cursors=cursors,
            )
        )
    assert iter_limits == [300]
    assert [r["username"] for r in rows1] == ["lurker_ceo", "active_founder"]
    by_u1 = {r["username"]: r for r in rows1}
    assert by_u1["active_founder"]["sample"] == "Scaling SaaS at https://actapp.io"
    assert cursors["founders_grp"] == 501
    assert "founders_grp__members_ts" in cursors

    # 2nd run inside 7-day cooldown -> skips iter_participants, scrapes incremental msg 502
    with out_path.open("w", encoding="utf-8") as fh:
        rows2 = asyncio.run(
            tg_scrape_posters.scrape_group(
                HybridFakeClient(msgs_run2),
                "founders_grp",
                50,
                0.0,
                fh,
                [],
                mode="hybrid",
                member_limit=150,
                profile_cache=profile_cache,
                cursors=cursors,
            )
        )
    assert iter_limits == [300]  # NOT called again inside 7d cooldown
    assert [r["username"] for r in rows2] == ["new_poster"]
    assert cursors["founders_grp"] == 502

    # 3rd run after >7d cooldown with hidden participants -> attempts iter_participants, still finishes posters
    cursors["founders_grp__members_ts"] = time.time() - (7 * 86400 + 120)
    cursors["founders_grp"] = 500
    with out_path.open("w", encoding="utf-8") as fh:
        rows3 = asyncio.run(
            tg_scrape_posters.scrape_group(
                HybridFakeClient(msgs_run2, fail_members=True),
                "founders_grp",
                50,
                0.0,
                fh,
                [],
                mode="hybrid",
                member_limit=250,
                profile_cache=profile_cache,
                cursors=cursors,
            )
        )
    assert iter_limits == [300, 250]
    assert [r["username"] for r in rows3] == ["new_poster"]


def test_tg_group_admins_extracts_linked_chat_id_discussion_admins() -> None:
    rights = ChatAdminRights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        add_admins=True,
        manage_call=True,
    )
    broadcast_ch = Channel(
        id=10001,
        title="Brand Broadcast",
        photo=None,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        broadcast=True,
        megagroup=False,
        username="brand_news",
        access_hash=111,
    )
    discussion_ch = Channel(
        id=99001,
        title="Brand Discussion",
        photo=None,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        broadcast=False,
        megagroup=True,
        username="brand_chat",
        access_hash=222,
    )
    u_creator = User(id=701, first_name="Max", last_name="Owner", username="max_brand_ceo")
    u_cmo = User(id=702, first_name="Nina", last_name="CMO", username="nina_growth")
    u_about = User(id=703, first_name="Sam", last_name="About", username="sam_contact")

    class BroadcastWithLinkedClient:
        def __init__(self, *, linked_fail: bool = False) -> None:
            self.linked_fail = linked_fail
            self.seen_peers: list[Any] = []

        async def get_entity(self, target: Any) -> Any:
            self.seen_peers.append(target)
            if isinstance(target, PeerChannel) and target.channel_id == 99001:
                if self.linked_fail:
                    raise ChannelPrivateError(request=None)
                return discussion_ch
            key = str(target).strip().lstrip("@").lower()
            if key == "brand_news":
                return broadcast_ch
            if key == "sam_contact":
                return u_about
            raise ValueError(f"Unknown target: {target}")

        async def __call__(self, req: Any) -> Any:
            if isinstance(req, GetParticipantsRequest):
                if getattr(req.channel, "id", None) == 10001:
                    raise ChatAdminRequiredError(request=req)
                if getattr(req.channel, "id", None) == 99001:
                    return ChannelParticipants(
                        count=2,
                        participants=[
                            ChannelParticipantCreator(user_id=701, admin_rights=rights, rank="Founder"),
                            ChannelParticipantAdmin(
                                user_id=702,
                                promoted_by=701,
                                date=datetime(2026, 2, 1, tzinfo=timezone.utc),
                                admin_rights=rights,
                                rank="Head of Marketing",
                            ),
                        ],
                        chats=[discussion_ch],
                        users=[u_cmo, u_creator],
                    )
            if isinstance(req, GetFullChannelRequest):
                return SimpleNamespace(
                    full_chat=SimpleNamespace(
                        about="Official news. Contact @sam_contact",
                        linked_chat_id=99001,
                    ),
                    chats=[broadcast_ch],
                )
            raise NotImplementedError(req.__class__.__name__)

    client_ok = BroadcastWithLinkedClient()
    contacts = asyncio.run(tg_group_admins.extract_brand_tg_admins(client_ok, "https://t.me/brand_news", max_results=3))
    assert any(isinstance(p, PeerChannel) and p.channel_id == 99001 for p in client_ok.seen_peers)
    assert [c.username for c in contacts] == ["max_brand_ceo", "nina_growth", "sam_contact"]
    assert contacts[0].role_guess == "Founder / CEO / Owner"
    assert contacts[1].role_guess == "CMO / Marketing / Growth / UA"

    # When linked discussion chat is private, still returns full_chat.about contacts gracefully
    client_priv = BroadcastWithLinkedClient(linked_fail=True)
    fallback_contacts = asyncio.run(tg_group_admins.extract_brand_tg_admins(client_priv, "brand_news", max_results=2))
    assert [c.username for c in fallback_contacts] == ["sam_contact"]
