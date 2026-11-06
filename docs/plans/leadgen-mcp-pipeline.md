# Plan: Leadgen Pipeline Migration to `crmtg` + MCP Ingestion + Conversion Boost

## Goal
1. Move the entire lead generation pipeline from `creatoros/scripts/` into `crmtg/leadgen/` so lead discovery, qualification, CRM pipeline, and outreach live in one repository.
2. Replace manual CSV chat notifications (`csv_notify.py`) with automated MCP ingestion (`ingest_discovered_leads` tool in `app/mcp_server.py` + `leadgen/crm_mcp_push.py`).
3. Add two uncommon automated discovery sources (`leadgen/tg_channel_graph.py` via `GetChannelRecommendationsRequest` and `leadgen/tg_ad_spy.py` for active Telegram advertisers).
4. Add conversion boost mechanics (2-step Micro-Ping first touch + full `183.5M/90d` case study on step 2/reply, plus Telegram Stories pre-warm in `app/outreach.py`).

## Workstreams (Parallel Subagents)
1. **Workstream A (`app/mcp_server.py`, `app/outreach.py`, `tests/test_mcp_leadgen_ingest.py`)**:
   - Add `ingest_discovered_leads` MCP tool to create/update CRM leads and enroll them into an outreach campaign (`Leadgen Autopilot` default or specified `campaign_id`).
   - Add `prewarm_recipient_stories` in `app/outreach.py` using Telethon `GetPeerStoriesRequest` / `ReadStoriesRequest` / `SendReactionRequest` before sending step 1.
2. **Workstream B (`leadgen/`, `leadgen/crm_mcp_push.py`, `leadgen/tg_pipeline.sh`, `tests/test_leadgen_mcp_push.py`)**:
   - Migrate leadgen scripts from `creatoros/scripts/` into `crmtg/leadgen/`.
   - Build `leadgen/crm_mcp_push.py` supporting both stdio/in-process MCP and remote HTTP `POST /mcp` ingestion, plus 2-step micro-ping outreach copy generation.
   - Wire `leadgen/tg_pipeline.sh` to run the full pipeline + new discovery scripts + MCP push.
3. **Workstream C (`leadgen/tg_channel_graph.py`, `leadgen/tg_ad_spy.py`, `tests/test_leadgen_graph_and_adspy.py`)**:
   - Build recursive Telegram founder channel graph crawler (`GetChannelRecommendationsRequest` + `GetFullChannelRequest` bio `@username` & website extraction + linked discussion chat discovery).
   - Build Telegram Ad-Spy scraper detecting active advertisers in tech/SaaS/AI channels and extracting decision-maker handles/domains.

## Verification
- Run `.venv/bin/pytest` on all new and existing test suites in `/Users/timofei/Developer/worktrees/crmtg/leadgen-mcp-pipeline`.
- Verify end-to-end dry-run of `crm_mcp_push.py` -> `app.mcp_server` (`ingest_discovered_leads`) -> SQLite DB verification.
