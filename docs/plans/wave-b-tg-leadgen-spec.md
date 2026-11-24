# SPEC: Wave B — Telegram Mini Apps, Auto-Catalogs, Segment Copy & Leadgen UI (`crmtg`)

Locked from Deep HTML Grill (`40/40` answers) on `2026-11-23` and live VPS Wave A measurement on `2026-11-24`.

---

## 1. Goal & Target Metrics
- **Target TG volume:** Scale from Wave A (`~65–100` TG leads/day) to **`120–160` fresh qualified TG leads/day** to keep all `8` connected TG outreach accounts (`[39, 40, 43, 27, 30, 33, 34, 35]`, `160/day` pool) busy.
- **Wave B Pillars (`q18`–`q26`):**
  1. **Paid/Subscription Telegram Mini Apps & Stars (`tma_apps`):** New TG-native vein discovering paid/subscription AI, VPN, Trading/Fintech, Utility, E-com, Education, and Dating Mini Apps (`findmini.app`, GitHub `awesome-telegram-mini-apps` curated sections, `tapps.center`, optional `crawl4ai.web.search`), strictly rejecting tap-to-earn/clickers/gambling (`q18a`), and resolving bot -> human founder/team handle, official channel, or product website without ever putting `*bot` into `username`.
  2. **Hybrid Auto-Replenished Channel & Seed Catalogs (`tg_catalog_discovery`):** Periodic (every 24h) auto-discovery expanding `tg_ad_spy`, `tg_channel_graph`, and `discovered_comment_channels.txt` via curated pool (`60–80` channels) + TMA/catalog extraction + Graph frontier + `crawl4ai.web.search` (`site:t.me/`, `site:tgstat.com/channel/`), with a hard **`MAX_RO_JOINS_PER_DAY = 2`** ledger cap and captcha/join-request deferral into `manual_join_queue.jsonl`.
  3. **Segment Copy Profiles & Reverse-Bridge Queue (`outreach_copy` + `leadgen_settings`):** Tailored Paragraph-1 SoftUGC hooks inside `whale_mass` for `tma_apps`, `tg_ad_spy`, `app_stores` (`mobile_ua`), and `d2c` (`tiktok_shop`/`d2c_whatsapp`), keeping zero `CreatorOS`; plus `build_reverse_bridge_draft` generating ready WhatsApp & Email drafts containing the owner's `https://t.me/<handle>` showcase link for whales without TG. Any new segment campaign synced by `sync_leadgen_campaigns` strictly starts in `"draft"` (`Rule 5`).
  4. **Leadgen UI Funnel, Scraper Health & Batch TG Scraper (`leadgen-ui-batch`):** Source-level conversion funnel table (`Сырьё -> Лиды -> В TG -> Ответы -> Конверсия`), RO scraper health panel (`signals_ro`, `phone_checker_1/2`, queues, cooldowns, errors), and a quick `Batch-сбор` tool (`POST/GET /api/leadgen/batch-scrape`) inside the Leadgen modal (`#leadgen-settings-modal`), preserving `DialogListPane.tsx` untouched (`q23`).

---

## 2. Public Interfaces & Contracts (Wave B)

### `leadgen/tma_apps.py` (new deep module)
- `is_rejected_tma_app(name: str, description: str = "", category: str = "") -> tuple[bool, str]`
- `parse_findmini_category_html(html: str, category: str) -> list[dict[str, Any]]`
- `parse_awesome_tma_markdown(md_text: str) -> list[dict[str, Any]]`
- `parse_tapps_center_html(html: str) -> list[dict[str, Any]]`
- `resolve_tma_to_human_lead(candidate: Mapping[str, Any], *, probe_fn=None, fetch_posts_fn=None, cache_path=None) -> dict[str, Any]`
- `collect_tma_leads(..., output_path, seen_path, limit=40, allow_paid=True, http_get_fn=None, treg_fn=None, probe_fn=None, fetch_posts_fn=None) -> dict[str, Any]`
- **Sacred Invariant:** Bot handles stay exclusively in `tma_bot_handle`; never populate `username` / `telegram_username` with `*bot`.

### `leadgen/tg_catalog_discovery.py` (new deep module)
- `MAX_RO_JOINS_PER_DAY = 2`
- `CURATED_CHANNEL_POOL: tuple[str, ...]` (60–80 curated channels/chats)
- `discover_and_replenish_catalogs(state_dir, *, limit=40, allow_paid=True, http_get_fn=None, treg_fn=None, probe_fn=None) -> dict[str, Any]`
- `safe_join_public_groups(handles, *, state_dir, client=None, join_fn=None, max_joins_per_day=MAX_RO_JOINS_PER_DAY) -> dict[str, Any]`
- Wired into `leadgen/tg_ad_spy.py` (`discovered_ad_spy_channels.txt`) and `leadgen/tg_channel_graph.py` (`discovered_graph_seeds.txt`).

### `leadgen/outreach_copy.py` & `app/leadgen_settings.py`
- `_infer_subniche_pain(...)`: dedicated hooks for `tma_apps`, `tg_ad_spy`, `app_stores` (`mobile_ua`), and `d2c` (`tiktok_shop` / `d2c_whatsapp`).
- `build_reverse_bridge_draft(lead: Mapping[str, Any], *, tg_handle: str = "") -> dict[str, str]` -> `{"whatsapp_draft", "email_subject", "email_body", "tg_link"}`.
- `DEFAULT_LEADGEN_CONFIG["b2b"]`: `sources.tma_apps = True`, `sources.tg_catalog_discovery = True`, `reverse_bridge_tg_handle = ""`.

### `app/hygiene.py`, `app/main.py`, `app/templates/index.html`, `app/static/app.js`, `app/static/app.css`
- `read_leadgen_health_summary` returns `source_funnel`, `scraper_health`, `reverse_bridge_ready_count`.
- `POST /api/leadgen/batch-scrape`, `GET /api/leadgen/batch-scrape`, `GET /api/leadgen/batch-scrape/{job_id}` (web `$0` + RO-session `hybrid` fail-closed; Rule 5 preview; zero `*bot` ingestion).
- New tab `#lg-tab-funnel` (`#lg-pane-funnel`) inside `#leadgen-settings-modal` + `#open-leadgen-funnel-btn` in `#leadgen-health-strip`.

---

## 3. Deletions & Anti-Goals
- Do NOT modify `frontend/src/components/DialogListPane.tsx`.
- Do NOT scrape `tgstat.com` directly (`403`); use `crawl4ai.web.search` only when `allow_paid=True`.
- Do NOT introduce Redis, Celery, or ClickHouse.
- Do NOT touch live outreach Telegram `.session` files in `batch-scrape` or `safe_join_public_groups`.
- Do NOT put any `*bot` handle into `leads.username` / `campaign_recipients.username`.

---

## 4. Worker Ownership (Stage 1 — Zero File Overlap)
1. `tma-apps-vein`: `leadgen/tma_apps.py`, `tests/test_wave_b_tma_apps.py`.
2. `tg-catalog-replenish`: `leadgen/tg_catalog_discovery.py`, `leadgen/tg_ad_spy.py`, `leadgen/tg_channel_graph.py`, `tests/test_wave_b_catalog_discovery.py`.
3. `segment-copy-bridge`: `leadgen/outreach_copy.py`, `app/leadgen_settings.py`, `tests/test_wave_b_copy_and_settings.py`.
4. `leadgen-ui-batch`: `app/hygiene.py`, `app/main.py`, `app/templates/index.html`, `app/static/app.js`, `app/static/app.css`, `tests/test_wave_b_ui_and_batch_scraper.py`.
5. Stage 2 (Coordinator in `wave-b-tg-leadgen`): `leadgen/vps_autonomous_replenish.py`, `scripts/verify.sh` (**Gate Z**), `tests/test_wave_b_orchestrator.py`.

---

## 5. Verification
- Targeted per-worker pytest suites -> `exit 0`.
- `impeccable detect --json app/templates/index.html app/static/app.js app/static/app.css` clean.
- Live Playwright screenshot of `#lg-pane-funnel` saved & visually verified.
- Full `bash scripts/verify.sh` (Gates A–Z + `proactive_audit --self-test` + full `pytest`) -> `exit 0`.
- Independent cold `adversarial-skeptic` audit & fixes -> final `bash scripts/verify.sh` `exit 0` -> Park on `REALITY_OK`.
