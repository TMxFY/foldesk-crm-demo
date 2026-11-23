from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leadgen import (
    tg_ad_spy,
    tg_channel_graph,
    tg_enrich_leads,
    tg_llm_qualify,
    tg_scrape_posters,
)


def test_spam_shill_ring_rejected_in_scrape_posters_and_enrich(tmp_path, monkeypatch):
    # 1. is_match rejects spam-shill ring phrases even if disguised with Founder/SaaS words
    spam_samples = [
        ("Founder building SaaS", "Founder", "Check Launch Q Credits https://qnation.tech"),
        ("Founder @qnationtech", "Founder", "Building AI growth"),
        ("Product maker", "Founder", "Join @qnationtech for Launch Q Credits"),
    ]
    for bio, role, sample in spam_samples:
        assert tg_scrape_posters.is_match(bio, role, sample) is False

    # 2. scrape_group skips emitting spam-shill posters while keeping real posters
    msgs = [
        SimpleNamespace(
            id=201,
            sender_id=3001,
            message="Hey guys check Launch Q Credits at https://qnation.tech @qnationtech",
            post=False,
            from_id=None,
            sender=None,
        ),
        SimpleNamespace(
            id=202,
            sender_id=3002,
            message="We are scaling B2B CRM reels at metriccrm.io",
            post=False,
            from_id=None,
            sender=None,
        ),
    ]

    class FakeClient:
        async def get_entity(self, group):
            return SimpleNamespace(id=55, title="saasfounders")

        async def get_messages(self, entity, limit=400):
            return msgs

        async def __call__(self, req):
            raise AssertionError("profile_cache is fresh; GetFullUserRequest should not be called")

    import time

    now_ts = time.time()
    profile_cache = {
        "3001": {
            "fetched_at": now_ts,
            "profile": {
                "id": 3001,
                "username": "q_shill_acc",
                "first_name": "Q",
                "last_name": "Shill",
                "bio": "Founder at QNation",
                "bot": False,
                "deleted": False,
                "verified": False,
                "premium": False,
            },
        },
        "3002": {
            "fetched_at": now_ts,
            "profile": {
                "id": 3002,
                "username": "real_crm_founder",
                "first_name": "Alex",
                "last_name": "CRM",
                "bio": "B2B sales platform & Google Ads analytics",
                "bot": False,
                "deleted": False,
                "verified": False,
                "premium": False,
            },
        },
    }
    cursors = {}
    scrape_out = tmp_path / "scraped.jsonl"
    with scrape_out.open("w", encoding="utf-8") as fh:
        emitted = asyncio.run(
            tg_scrape_posters.scrape_group(
                FakeClient(),
                "saasfounders",
                50,
                0.0,
                fh,
                [],
                profile_cache=profile_cache,
                cursors=cursors,
                mode="posters",
            )
        )
    assert [r["username"] for r in emitted] == ["real_crm_founder"]
    assert cursors["saasfounders"] == 202

    # 3. tg_enrich_leads.main rejects spam-shill even if icp_match=True
    inp = tmp_path / "in.jsonl"
    out = tmp_path / "out.jsonl"
    rows = [
        {
            "source": "tg_groups",
            "username": "qnation_shill",
            "name": "Fake Founder",
            "bio": "Founder @qnationtech",
            "sample": "Launch Q Credits at https://qnation.tech",
            "icp_match": True,
        },
        {
            "source": "tg_groups",
            "username": "real_crm_founder",
            "name": "Alex CRM",
            "bio": "B2B sales platform & Google Ads analytics",
            "sample": "We are scaling B2B CRM reels",
            "icp_match": False,
        },
    ]
    inp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["tg_enrich_leads.py", "--input", str(inp), "--output", str(out), "--skip-copy"])
    tg_enrich_leads.main()

    enriched = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["username"] for r in enriched] == ["real_crm_founder"]


def test_tg_enrich_leads_admits_icp_false_tg_discovery_and_drops_junk(tmp_path, monkeypatch):
    inp = tmp_path / "raw.jsonl"
    out = tmp_path / "enriched.jsonl"

    candidates = [
        # Admitted: tg_groups icp_match=False, personal username, >=15 chars bio, non-supply
        {
            "source": "tg_groups",
            "username": "alex_saas",
            "name": "Alex",
            "role": "Other",
            "bio": "B2B sales platform & CRM automation",
            "sample": "hi",
            "links": [],
            "icp_match": False,
        },
        # Admitted: tg_groups icp_match=False, short bio but product link present
        {
            "source": "b2b_tg",
            "username": "maya_builder",
            "name": "Maya",
            "role": "Other",
            "bio": "Maker",
            "sample": "",
            "links": ["https://pulseflow.io"],
            "icp_match": False,
        },
        # Admitted: tg_channel_graph without icp_match key
        {
            "source": "tg_channel_graph",
            "username": "graph_founder",
            "name": "Dan",
            "bio": "Shopify/store for clean skincare",
            "links": ["https://glowskin.shop"],
        },
        # Admitted: tg_ad_spy without icp_match key
        {
            "source": "tg_ad_spy",
            "username": "adspy_founder",
            "name": "Sam",
            "bio": "Analytics dashboard for ecommerce",
            "links": ["https://ecommetrics.ai"],
        },
        # Dropped: bot username
        {
            "source": "tg_groups",
            "username": "helper_support_bot",
            "name": "Helper",
            "bio": "B2B sales platform & CRM automation",
            "icp_match": False,
        },
        # Dropped: empty username
        {
            "source": "tg_groups",
            "username": "",
            "name": "Anon",
            "bio": "B2B sales platform & CRM automation",
            "icp_match": False,
        },
        # Dropped: short noise (<15 chars and no product link)
        {
            "source": "tg_groups",
            "username": "noise_guy",
            "name": "Bob",
            "bio": "hi there",
            "sample": "gm",
            "links": ["https://t.me/somechannel"],
            "icp_match": False,
        },
        # Dropped: obvious supply-side
        {
            "source": "tg_groups",
            "username": "agency_shill",
            "name": "Tom",
            "bio": "UGC Creator | Freelancer | Video editor portfolio",
            "sample": "DM for rates",
            "icp_match": False,
        },
        # Dropped: non-TG source with icp_match=False
        {
            "source": "meta_ads",
            "username": "skipped_meta",
            "name": "Skip",
            "bio": "B2B sales platform & CRM automation",
            "icp_match": False,
        },
        # Admitted: non-TG source with icp_match=True (no username needed)
        {
            "source": "meta_ads",
            "username": "",
            "name": "Meta Brand",
            "bio": "Online skincare store",
            "project_url": "https://brandglow.com",
            "icp_match": True,
        },
    ]
    inp.write_text("\n".join(json.dumps(r) for r in candidates) + "\n", encoding="utf-8")
    monkeypatch.delenv("RELAYFAST_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["tg_enrich_leads.py", "--input", str(inp), "--output", str(out), "--skip-copy"])
    tg_enrich_leads.main()

    out_rows = [json.loads(ln) for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out_usernames = [r.get("username") for r in out_rows]
    assert out_usernames == ["alex_saas", "maya_builder", "graph_founder", "adspy_founder", ""]

    # Ensure placeholder role="Other" was cleared so tg_llm_qualify.gate_before_model does NOT reject before LLM
    alex = next(r for r in out_rows if r["username"] == "alex_saas")
    assert alex["role"] == ""
    assert alex["category"] == "SaaS / AI app"
    kept, notes = tg_llm_qualify.gate_before_model(out_rows)
    assert [r.get("username") for r in kept] == ["alex_saas", "maya_builder", "graph_founder", "adspy_founder", ""]
    assert notes == []


def test_classify_cat_handles_saas_ads_crm_builders_and_ecom_stores():
    saas_bios = [
        "Google Ads & paid user acquisition platform",
        "B2B sales platform for outbound teams",
        "CRM and workflow automation for agencies",
        "Product analytics dashboard for mobile apps",
        "Building metricflow.io — subscription software",
        "Maker at voicepilot.ai",
        "Creator of habittracker.app",
        "AI assistant for customer support",
        "Сервис автоматизации маркетинга и аналитики",
    ]
    for bio in saas_bios:
        assert tg_enrich_leads.classify_cat(bio) == "SaaS / AI app", f"Misclassified SaaS bio: {bio}"

    ecom_bios = [
        "Shopify/store for clean supplements",
        "Online store selling eco skincare",
        "D2C beauty brand & wellness products",
        "Интернет-магазин косметики и парфюмерии",
        "Store owner selling athletic apparel",
    ]
    for bio in ecom_bios:
        assert tg_enrich_leads.classify_cat(bio) == "E-com / D2C", f"Misclassified E-com bio: {bio}"

    # Existing priority categories stay intact
    assert tg_enrich_leads.classify_cat("Prop trading challenge for funded traders") == "Проп трейдинг"
    assert tg_enrich_leads.classify_cat("DeFi staking and DEX liquidity") == "DeFi / DEX"
    assert tg_enrich_leads.classify_cat("Mobile game studio building RPG games") == "Gaming / AI"
    assert tg_enrich_leads.classify_cat("Web3 community chat") == "Web3 (общее)"


def test_channel_graph_russian_owner_hints_and_frontier_replenishment(tmp_path):
    # 1. Russian owner tokens in FOUNDER_HINT_RE
    for line, expected_user in [
        ("Канал о кибербезопасности. Владелец: @cyberhackGL https://cyberhack.io", "cyberhackGL"),
        ("ИИ стартапы. Создатель: @ai_creator_ru https://aistart.ai", "ai_creator_ru"),
        ("SaaS блог. Руководитель: @saas_head_ru https://saashead.app", "saas_head_ru"),
    ]:
        parsed = tg_channel_graph.parse_channel_founder_info(
            about=line,
            posts=[],
            channel_username="some_channel",
            channel_title="Some Channel",
            subscribers=2500,
        )
        assert parsed is not None, f"Failed to parse: {line}"
        assert parsed["username"] == expected_user

    # 2. Expanded seeds include EN/global SaaS, AI, indiehacker, D2C/Shopify, app channels
    assert len(tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS) >= 20
    assert any(
        s in tg_channel_graph.DEFAULT_GRAPH_SEED_CHANNELS
        for s in ("shopifyeasy", "dtc_brands", "shopify_merchants", "ecommerce_news")
    )

    # 3. Replenishment when frontier == [] and all seeds + fallbacks are already in seen/expanded:
    #    - pulls unseen channels from discovered_comment_channels.txt
    #    - re-scans recent posts of seed channels for newly linked/forwarded channels
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "tg_channel_graph_seen.json"
    discovered_file = state_dir / "discovered_comment_channels.txt"
    discovered_file.write_text("id88001\n@comment_saas_chan\n", encoding="utf-8")

    all_exhausted = ["seed_ru"] + [s.lower() for s in tg_channel_graph.FALLBACK_EN_GRAPH_SEEDS]
    state_file.write_text(
        json.dumps({"seen": all_exhausted, "expanded": all_exhausted, "frontier": []}),
        encoding="utf-8",
    )

    class FakeMsg:
        def __init__(self, id: int, message: str, fwd_channel_id: int | None = None):
            self.id = id
            self.message = message
            self.fwd_from = (
                SimpleNamespace(from_id=SimpleNamespace(channel_id=fwd_channel_id))
                if fwd_channel_id is not None
                else None
            )

    class FakeChannel:
        def __init__(self, id: int, username: str, title: str, participants_count: int = 1200):
            self.id = id
            self.username = username
            self.title = title
            self.participants_count = participants_count

    class FakeUser:
        def __init__(self, id: int, username: str, first_name: str, last_name: str):
            self.id = id
            self.username = username
            self.first_name = first_name
            self.last_name = last_name
            self.bot = False

    graph_data = {
        "seed_ru": {
            "entity": FakeChannel(10, "seed_ru", "Seed RU"),
            "about": "Владелец: @old_seed_owner",
            "messages": [
                FakeMsg(1, "Крутой новый проект @linked_post_chan https://linkedpost.io"),
                FakeMsg(2, "Репост из другого канала", fwd_channel_id=99001),
            ],
        },
        "comment_saas_chan": {
            "entity": FakeChannel(20, "comment_saas_chan", "Comment SaaS"),
            "about": "Владелец: @cyberhackGL | https://cybercrm.io",
            "messages": [],
        },
        "88001": {
            "entity": FakeChannel(88001, "linked_disc_88001", "Disc 88001"),
            "about": "Создатель: @disc_owner | https://discapp.ai",
            "messages": [],
        },
        "linked_post_chan": {
            "entity": FakeChannel(30, "linked_post_chan", "Linked Post Chan"),
            "about": "Руководитель: @linked_founder | https://linkedpost.io",
            "messages": [],
        },
        "99001": {
            "entity": FakeChannel(99001, "fwd_chan_99001", "Forwarded Chan"),
            "about": "Founder: @fwd_founder | https://fwdtool.app",
            "messages": [],
        },
        "cyberhackgl": {"user": FakeUser(501, "cyberhackGL", "Cyber", "Owner")},
        "disc_owner": {"user": FakeUser(502, "disc_owner", "Disc", "Maker")},
        "linked_founder": {"user": FakeUser(503, "linked_founder", "Linked", "CEO")},
        "fwd_founder": {"user": FakeUser(504, "fwd_founder", "Fwd", "Founder")},
    }

    class FakeRescanClient:
        async def get_entity(self, target):
            key = str(target).lstrip("@").lower()
            if key in graph_data and "entity" in graph_data[key]:
                return graph_data[key]["entity"]
            raise ValueError(f"Unknown channel {target}")

        async def get_messages(self, entity, limit: int = 10):
            key = getattr(entity, "username", str(getattr(entity, "id", entity))).lower()
            info = graph_data.get(key) or graph_data.get(str(getattr(entity, "id", ""))) or {}
            return list(info.get("messages", []))[:limit]

        async def __call__(self, req):
            name = req.__class__.__name__
            if name == "GetFullChannelRequest":
                ch = req.channel
                key = getattr(ch, "username", str(getattr(ch, "id", ""))).lower()
                info = graph_data.get(key) or graph_data.get(str(getattr(ch, "id", "")))
                ent = info["entity"]
                return SimpleNamespace(
                    full_chat=SimpleNamespace(about=info.get("about", ""), participants_count=ent.participants_count, linked_chat_id=None),
                    chats=[ent],
                )
            if name == "GetChannelRecommendationsRequest":
                return SimpleNamespace(chats=[])
            if name == "GetFullUserRequest":
                uname = str(req.id).lstrip("@").lower()
                u = graph_data.get(uname, {}).get("user") or FakeUser(999, uname, "User", "Founder")
                return SimpleNamespace(users=[u], full_user=SimpleNamespace(about="Founder"))
            raise NotImplementedError(name)

    out_file = tmp_path / "graph_out.jsonl"
    rows = asyncio.run(
        tg_channel_graph.crawl_channel_graph(
            client=FakeRescanClient(),
            seeds=["seed_ru"],
            depth=2,
            limit=10,
            state_path=str(state_file),
            output_path=str(out_file),
            delay=0.0,
        )
    )
    found_users = {r["username"] for r in rows}
    # Must discover leads from discovered_comment_channels.txt AND rescanned linked/forwarded posts,
    # without re-emitting old_seed_owner from already-seen seed_ru
    assert "cyberhackGL" in found_users
    assert "disc_owner" in found_users
    assert "linked_founder" in found_users
    assert "fwd_founder" in found_users
    assert "old_seed_owner" not in found_users


def test_ad_spy_en_global_posts_and_seen_only_tracks_advertisers(tmp_path):
    assert len(tg_ad_spy.DEFAULT_AD_SPY_CHANNELS) >= 22

    # 1. EN/global ad posts without RU markers are detected
    en_promo_post = (
        "Scale your Shopify revenue with GlowMetrics — grab 25% off with promo code SCALE25 "
        "at https://glowmetrics.io"
    )
    sig_promo = tg_ad_spy.detect_ad_post(en_promo_post, host_channel="saasgrowth")
    assert sig_promo["is_ad"] is True
    assert sig_promo["product_site"] == "https://glowmetrics.io"

    en_partner_post = (
        "Sponsored by VoicePilot — AI voice agents for sales. Start your free trial at "
        "https://voicepilot.ai or reach out to @voicepilot_ceo"
    )
    sig_partner = tg_ad_spy.detect_ad_post(en_partner_post, host_channel="ai_founders")
    assert sig_partner["is_ad"] is True
    assert "voicepilot_ceo" in sig_partner["tg_targets"]

    en_cta_post = (
        "Our friends at @snapreel_founder just shipped an AI video studio. "
        "Book a demo or try for free today!"
    )
    sig_cta = tg_ad_spy.detect_ad_post(en_cta_post, host_channel="indiehackers")
    assert sig_cta["is_ad"] is True
    assert "snapreel_founder" in sig_cta["tg_targets"]

    # 2. Inline button URL in t.me/s HTML is extracted cleanly
    tme_inline_html = """
    <html><body>
      <div class="tgme_widget_message_wrap">
        <div class="tgme_widget_message_text">Paid partnership: try FlowCRM for B2B sales teams!</div>
        <div class="tgme_widget_message_inline_keyboard">
          <a class="tgme_widget_message_inline_button" href="https://flowcrm.io/?utm_source=tg">Start Free Trial</a>
        </div>
      </div>
    </body></html>
    """
    posts = tg_ad_spy._parse_tme_s_html(tme_inline_html, limit=10)
    assert len(posts) == 1
    assert "https://flowcrm.io/?utm_source=tg" in posts[0]
    sig_inline = tg_ad_spy.detect_ad_post(posts[0], host_channel="b2bsaas")
    assert sig_inline["is_ad"] is True
    assert sig_inline["product_site"] == "https://flowcrm.io"

    # 3. seen file tracks ONLY advertiser keys, never permanently skipping host channels
    seen_file = tmp_path / "ad_spy_seen.json"
    # Even if a polluted seen file contains host channel names, host channels are STILL scanned
    seen_file.write_text(json.dumps({"seen": ["saasgrowth", "oldadv.io"], "cursor": 5}), encoding="utf-8")

    posts_by_chan = {
        "saasgrowth": [
            "Old ad https://oldadv.io/?utm_source=tg #ad",
            "New EN ad: Try MetricPulse with promo code LAUNCH20 at https://metricpulse.io — DM @pulse_ceo",
        ],
        "indiehackers": [
            "Brought to you by CartBoost — boost Shopify AOV! https://cartboost.app #sponsored",
        ],
    }

    class FakeSpyClient:
        async def get_entity(self, target):
            key = str(target).lstrip("@").lower()
            return SimpleNamespace(id=700, username=key, first_name="Alex", last_name="Founder")

        async def get_messages(self, entity, limit=40):
            return []

        async def __call__(self, req):
            uid = str(getattr(req, "id", "")).lstrip("@")
            return SimpleNamespace(
                users=[SimpleNamespace(id=701, username=uid, first_name="Alex", last_name="Founder")],
                full_user=SimpleNamespace(about="Founder"),
            )

    out1 = tmp_path / "spy_out1.jsonl"
    rows1 = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=FakeSpyClient(),
            channels=["saasgrowth", "indiehackers"],
            msg_limit=20,
            seen_path=str(seen_file),
            output_path=str(out1),
            delay=0.0,
            fetch_html=lambda u: "",
            public_posts_fn=lambda ch, lim: posts_by_chan.get(ch, []),
        )
    )
    assert {r.get("site") for r in rows1} == {"https://metricpulse.io", "https://cartboost.app"}

    # Check saved seen state does NOT add host channel "indiehackers"
    saved_state = json.loads(seen_file.read_text(encoding="utf-8"))
    assert "indiehackers" not in saved_state["seen"]
    assert "https://cartboost.app" in saved_state["seen"]
    assert "pulse_ceo" in saved_state["seen"]

    # Run again on SAME host channels with a new post -> still scans saasgrowth & indiehackers and emits new ad!
    posts_by_chan["indiehackers"].append(
        "Use discount code INDIE30 for 30% off at https://freshsaas.io #promo"
    )
    out2 = tmp_path / "spy_out2.jsonl"
    rows2 = asyncio.run(
        tg_ad_spy.scan_ad_channels(
            client=FakeSpyClient(),
            channels=["saasgrowth", "indiehackers"],
            msg_limit=20,
            seen_path=str(seen_file),
            output_path=str(out2),
            delay=0.0,
            fetch_html=lambda u: "",
            public_posts_fn=lambda ch, lim: posts_by_chan.get(ch, []),
        )
    )
    assert [r.get("site") for r in rows2] == ["https://freshsaas.io"]
