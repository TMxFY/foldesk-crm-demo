# SPEC: Wave A + Wave B — Telegram Leadgen Scale-Up (`crmtg`)

Locked from Deep HTML Grill (`40/40` answers) on `2026-11-23`.

---

## 1. Goal & Target Metrics
- **Target TG volume:** `120–160` fresh qualified TG leads/day to utilize all `8` connected TG outreach accounts (`20` msgs/account/day; `Whales EN — TG` stays at `20/account/day` and scales via connecting more accounts).
- **ICP Gate (`q2`, `q5`):** Lower `MIN_ICP_SCORE` from `80` to `70` while preserving core priority verticals (Consumer AI / SaaS, E-com / D2C, large high-CPM brands `$500k–$700k+`, Prop Trading / Fintech) and fixing false `typesafe_drop` rejections on real external product URLs (`0.15 < ts_fit <= 0.45` routes to Relay Fast LLM).
- **Cadence (`q3`, `q4`):** Batch Loop (up to `3` waves per timer invocation until `pending_b2b >= low_water_mark`) + `3h` 24/7 systemd schedule (`00,03,06,09,12,15,18,21:15:00 UTC`). Shipped in two releases: **Wave A** (core pipeline & bridge scale-up) → live VPS measurement → **Wave B** (TMA/Stars live vein, auto-replenished channel catalogs, segment angles, Leadgen modal conversion table & Batch-scraper tool).
- **Budget & Graceful Degradation (`q13`, `q28`, `q28a`):** `daily_budget_cap_usd = $10.00/day`, aggressive ContactOut phone reveal (`CONTACTOUT_VERIFIED_MIN_ICP = 80` when `available=True`), `phone_tg_checker_daily_limit = 200`. When balance `< $0.50` or cap reached, gracefully degrade paid APIs while continuing `$0` TG groups/comments/web/phone_checker stages.
- **Session Isolation (`q6`, `q6a`, `q29`):** Never use the `8` live outreach accounts for scraping or phone import. Allow `phone_checker_2.session` (and higher `phone_checker_*`) as read-only fallback alongside `signals_ro*.session` when `allow_phone_checker_ro_fallback=True` (still enforced by `validate_isolated_scraper_session`).

---

## 2. Public Interfaces & Contracts (Wave A)

### `leadgen/tg_web_client.py` (new deep module)
- `TME_CACHE_TTL_SEC = 7 * 86400`
- `probe_tme_handle_web(handle: str, *, http_get_fn=None, timeout: float = 5.0, cache_path: Path | None = None) -> dict[str, Any]`
  - Returns `{"handle": str, "kind": "user"|"channel"|"bot"|"missing"|"unknown", "title": str, "bio": str, "subscribers": int, "cached": bool}`.
- `fetch_channels_info_batch(handles: list[str], *, treg_fn=None, allow_paid: bool = True, budget_guard_fn=None, cost_record_fn=None) -> dict[str, dict[str, Any]]`
  - Uses `tikhub.x.telegram-web-batch-channel-info` in chunks of 20 (`$0.008/call`) with `$0` HTTP `t.me/s/` fallback.
- `fetch_channel_posts_web(channel: str, limit: int = 20, *, treg_fn=None, allow_paid: bool = True, budget_guard_fn=None, cost_record_fn=None, http_get_fn=None) -> tuple[dict[str, Any], list[str]]`
  - Order: free `https://t.me/s/<ch>` → `tikhub.x.telegram-web-fetch-channel-posts` (`$0.001`) → `crawl4ai.telegram.channel.posts` (`$0.0009`).

### `app/telegram_gateway.py` & `app/outreach.py` (`q13a`)
- `TelegramGateway.send_message_by_phone(self, session_path: str, phone: str, text: str, *, first_name: str = "", image_path: str | None = None) -> dict`
  - Imports contact via `ImportContactsRequest`, resolves non-bot/non-deleted `User`, sends message/file, and **always** runs `DeleteContactsRequest` in `finally:`.
- `MAX_TG_PHONE_DIRECT_PER_ACCOUNT_DAY = 5` in `app/outreach.py`: sends directly via `send_message_by_phone` when `provider == "telegram"`, `username` is empty (or entity resolution fails), and E.164 `phone` is present (capped at 5/day/account via `kv_settings`).

### `leadgen/vps_autonomous_replenish.py`
- `find_ro_scraper_sessions(conn, sessions_dir=None, primary_session=None, *, allow_phone_checker_ro_fallback=True) -> list[Path]`
- `emit_leadgen_daily_digest(conn, *, summary: Mapping[str, Any], digest_opener=None) -> dict[str, Any]`
- **Stage Ordering:** `contact_waterfall` → `tg_identity_bridge` → `verify_personal_tg_handles_live` (Web-first + RO pool + fail-closed `pending_tg_verify_queue.jsonl` for guessed handles on FloodWait) → `enrich_whales_with_tg_group_admins` (now sees discovered `t.me/` links + `linked_chat_id`!) → `resolve_and_route_phone_leads`.
- **Pre-Waterfall CRM DB Dedup (`q31`):** Filter out domains/usernames already present in `crm.db` `leads` before paid `treg_enrich` / `contact_waterfall`.

### `leadgen/backfill_direct_x.py` (`q14`)
- CLI: `python -m leadgen.backfill_direct_x --db <path> --state-dir <path> --canary <N> --retry-v2 [--dry-run]`
- Runs Multi-DM `contact_waterfall` + `tg_identity_bridge` + `tg_group_admins` + `phone_checker` over `direct_x_leads.jsonl` with per-row `tg_bridge_v2_attempted_at` checkpointing and `ingest_v2_routed_leads` + `Rule 5` preview.

---

## 3. Wave A Scope
1. **ICP & Source Throughput (`icp-sources`):** `MIN_ICP_SCORE=70`, `typesafe_drop` rescue for real product URLs, `meta_ads` `CAP=80` / 12 queries / 6 GEOs, parallel `high_yield_veins` (`limit=80`, `[20]` per-vein call budget), cleaner `treg_x` queries & YC pre-LLM consumer filter.
2. **Native TG Hybrid (`native-tg-hybrid`):** `tg_web_client.py` (7d `tme_probe_cache.json`, Web/TikHub/Crawl4AI offload), `tg_ad_spy.py` (pre-Telethon Fragment/auction/bot filter + Web-first + 100+ channels), `tg_channel_graph.py` (+30 fresh seeds, Web-first posts/bio, unexpanded priority), `tg_scrape_posters.py` (`--mode hybrid` with 7d `members` cooldown + `posters`), `tg_group_admins.py` (`linked_chat_id` discussion admins).
3. **Bridge, Multi-DM & Backfill (`bridge-waterfall`):** `@the_insider` pseudo-name guard, `ThreadPoolExecutor(8)` + 5s timeout + 7d cache in `tg_identity_bridge.py`, fix `elif not li_url` bug + 3 DMs (`limadata` `$0.002` + `leadmagic`) + `CONTACTOUT_VERIFIED_MIN_ICP=80` in `contact_waterfall.py`, `backfill_direct_x.py`.
4. **Phone-Direct TG & Settings (`phone-outreach-settings`):** `tg_phone_direct` routing without `@username`, `send_message_by_phone` with `DeleteContacts` cleanup, `MAX_TG_PHONE_DIRECT_PER_ACCOUNT_DAY=5`, hygiene/MCP support, `$10/day` + `max_dms=3` + `checker=200` defaults.
5. **Orchestrator, Schedule & Gate Y:** Parallel Web discovery (`ThreadPoolExecutor(5)`), `discovered_comment_channels.txt` `--mode hybrid` scraping, 48h auto-cooldown on 0-yield sources, removal of `min(12, yc_limit)` / `min(10, external_limit)` caps, Pre-Waterfall CRM DB dedup, corrected `tg_group_admins` ordering, fail-closed `pending_tg_verify_queue`, Batch Loop (`max_waves=3`), 4-line daily TG digest + instant critical alert, 3h `crmtg-leadgen.timer`, `scripts/verify.sh` **Gate Y**.

---

## 4. Wave B Scope (Next Release After Wave A Canary)
- Paid/subscription Telegram Mini Apps (`tapps.center`, GitHub `awesome-telegram-mini-apps`, AI/VPN/Trading/Fintech/Dating/Tools; no tap-to-earn/gambling).
- Hybrid channel auto-replenishment (`tgstat` public catalogs + `tapps.center` + `site:t.me` + Telethon graph; safe public/no-captcha `<=2/day` join policy).
- Segment-tailored 1st-paragraph SoftUGC copy profiles (`tma`, `tg_ad_spy`, `mobile_ua`, `d2c`) + Rule 5 `draft` for new campaigns.
- Batch TG chat/channel scraper tool inside Leadgen UI + source-level conversion funnel & scraper health dashboard (`Syr'yo -> ICP -> TG -> Campaign -> Replies`).

---

## 5. Deletions & Anti-Goals (`q32`)
- **Delete** hardcoded `min(12, yc_limit)` and `min(10, external_limit)` caps in `leadgen/vps_autonomous_replenish.py`.
- **Add** automatic 48h cooldown for repeated 0-yield discovery stages (`buyer_public`, `facebook_groups`).
- **No** ClickHouse, Redis, or Celery infrastructure; keep SQLite + Python + systemd.
- **Do not** alter warmup / antiban send cadence in `app/outreach.py` (only add capped phone-direct fallback).

---

## 6. Worker Ownership (Stage 1 — Zero File Overlaps)
1. `icp-sources`: `leadgen/tg_llm_qualify.py`, `leadgen/typesafe_leads.py`, `leadgen/tg_curate.py`, `leadgen/meta_ad_leads.py`, `leadgen/high_yield_veins.py`, `leadgen/treg_social_leads.py`, `leadgen/yc_ph_leads.py`, `tests/test_sniper_leadgen_gate.py`, `tests/test_crm_icp_hygiene_and_self_healing.py`, `tests/test_whale_icp_gate.py`, `tests/test_wave_a_icp_and_sources.py`.
2. `native-tg-hybrid`: `leadgen/tg_web_client.py`, `leadgen/tg_ad_spy.py`, `leadgen/tg_channel_graph.py`, `leadgen/tg_scrape_posters.py`, `leadgen/tg_group_admins.py`, `tests/test_wave_a_native_tg.py`.
3. `bridge-waterfall`: `leadgen/tg_identity_bridge.py`, `leadgen/contact_waterfall.py`, `leadgen/backfill_direct_x.py`, `tests/test_wave_a_bridge_waterfall.py`.
4. `phone-outreach-settings`: `leadgen/phone_messenger_resolver.py`, `app/telegram_gateway.py`, `app/outreach.py`, `app/hygiene.py`, `app/mcp_server.py`, `app/leadgen_settings.py`, `tests/test_leadgen_settings_v2.py`, `tests/test_wave_a_phone_direct_outreach.py`.
5. Stage 2 (Coordinator after merging Stage 1): `leadgen/vps_autonomous_replenish.py`, `deploy/systemd/crmtg-leadgen.timer`, `scripts/verify.sh` (Gate Y), `tests/test_wave_a_orchestrator.py`.

---

## 7. Verification & Canary Gate (`q33`)
- Local verification: `bash scripts/verify.sh` (`exit 0`, including Gate Y & full `pytest`) + `scripts/proactive_audit.py --self-test` + cold `adversarial-skeptic` review.
- Park on `REALITY_OK` for Timofei's deploy confirmation → deploy to VPS → 1 controlled Canary wave + `Rule 5` preview (5 messages) + `$` spend report + `Canary=5` backfill of `direct_x_leads.jsonl` → enable 24/7 3h timer.
