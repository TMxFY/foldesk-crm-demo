#!/usr/bin/env bash
# Foldesk CRM / CreatorOS TG lead pipeline: scrape -> enrich -> curate -> append to sheet -> push to CRM via MCP.
# Safe-paced; dedupes against all prior runs. Sends personalized DM if SEND=1.
# The launchd entrypoint holds the shared Telethon-session lock before invoking
# this script; keep direct runs available for explicit operator use.
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/usr/local/bin:$PATH"
# tim2 already sits in the older chats. tim3 is only used for the new public set.
# YC/PH lookup stays on tim2. The launchd lock covers tim2 so that lookup
# does not collide with ForexSbor.
SESSION_OLD="/Users/timofei/.claude/skill-telegram/tim2.session"
SESSION_NEW="/Users/timofei/.claude/skill-telegram/tim3.session"
# 14д raw→QUAL: выкинуты мёртвые и job/agency/local чаты.
# tim3 вступил 2026-11-04 в shopifyeasy и productcircle_chat, сообщений не слал.
# buildinpublic ответил заявкой, в список не ставим, пока админ не пустит.
# id1613398776 = «Чат - SaaS по Понятиям».
TG_GROUPS_OLD="solofounders,producthunt_heroes,id1613398776"
TG_GROUPS_NEW="solofounderschat,shopifyeasy,productcircle_chat"
SID="1Z4KwmUMNuA9Q562y3Vvxa40AFcqGaMyAXAqr7QlbmOQ"
SHEET="Все2"

DEFAULT_CREATOROS_STATE="/Users/timofei/dev/Python/new era/creatoros/.tg_pipeline"
if [ -n "${LEADGEN_STATE_DIR:-}" ]; then
  STATE_DIR="$LEADGEN_STATE_DIR"
elif [ -d "$DEFAULT_CREATOROS_STATE" ]; then
  STATE_DIR="$DEFAULT_CREATOROS_STATE"
else
  STATE_DIR="$ROOT/data/leadgen_state"
fi
export LEADGEN_STATE_DIR="$STATE_DIR"
mkdir -p "$STATE_DIR"
PENDING_DIR="$STATE_DIR/pending_sheet_appends"
mkdir -p "$PENDING_DIR"

# env
[ -f ~/.telegram/.env ] && set -a && source ~/.telegram/.env && set +a
[ -f "/Users/timofei/dev/Python/new era/creatoros/.env" ] && set -a && source "/Users/timofei/dev/Python/new era/creatoros/.env" && set +a
# repo .env (LLM keys, MCP_API_KEY, CRMTG_MCP_URL etc.); missing file is fine
[ -f "$ROOT/.env" ] && set -a && source "$ROOT/.env" && set +a

TS="$(date +%Y%m%d_%H%M%S)"
RAW="$STATE_DIR/raw_$TS.jsonl"
MASTER="$STATE_DIR/master_ids.jsonl"   # every id ever scraped
EXTERNAL="$STATE_DIR/external_$TS.jsonl"

echo "=== run $TS ==="

# Name the treg columns. Rewriting the four titles is safe: they belong to this step.
token="$(/Users/timofei/.local/bin/gsheets-token)"
sheet_enc="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$SHEET")"
curl -sS -o /dev/null -X PUT \
  "https://sheets.googleapis.com/v4/spreadsheets/$SID/values/${sheet_enc}%21P1:S1?valueInputOption=USER_ENTERED" \
  --header "Authorization: Bearer $token" --header "Content-Type: application/json" \
  -d '{"values":[["Почта","Проверка","Спрос","Реклама"]]}' || true
unset token sheet_enc

append_pending_json() {
  # Telegram rows go to Все2. X-only rows go to X. LinkedIn, email, and
  # WhatsApp without Telegram go to Direct. An empty side is not posted.
  # Sheets X and Direct must already exist; this function does not create them.
  local payload="$1"
  local label="$2"
  local token response status sheet_enc routed key tab file failed posted
  local -a failed_files
  routed="$(mktemp -d /tmp/creatoros-route.XXXXXX)"
  if ! python3 - "$ROOT" "$payload" "$routed" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from leadgen.tg_to_gsheet import write_routed_payloads
write_routed_payloads(sys.argv[2], sys.argv[3])
PY
  then
    echo "sheet route failed ($label)" >&2
    rm -rf "$routed"
    return 1
  fi
  token="$(/Users/timofei/.local/bin/gsheets-token)"
  failed=0
  posted=0
  failed_files=()
  for pair in "tg:Все2" "x:X" "direct:Direct"; do
    key="${pair%%:*}"
    tab="${pair#*:}"
    file="$routed/${key}.json"
    [[ -f "$file" ]] || continue
    sheet_enc="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$tab")"
    response="$(mktemp /tmp/creatoros-sheet-response.XXXXXX)"
    status="$(curl -sS -o "$response" -w '%{http_code}' -X POST \
      "https://sheets.googleapis.com/v4/spreadsheets/$SID/values/${sheet_enc}%21A:S:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS" \
      --header "Authorization: Bearer $token" --header "Content-Type: application/json" \
      -d @"$file")"
    if [[ "$status" =~ ^2 ]]; then
      jq '.updates | {updatedRows, updatedCells}' "$response"
      rm -f "$response"
      posted=1
    else
      echo "sheet append failed ($label $tab): HTTP $status $(tr '\n' ' ' < "$response" | cut -c1-1000)" >&2
      rm -f "$response"
      failed=1
      failed_files+=("$file")
    fi
  done
  if [[ "$failed" -ne 0 && ${#failed_files[@]} -gt 0 ]]; then
    python3 - "$payload" "${failed_files[@]}" <<'PY'
import json, sys
rows = []
for path in sys.argv[2:]:
    rows.extend(json.load(open(path, encoding="utf-8")).get("values") or [])
with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump({"values": rows}, out, ensure_ascii=False)
PY
  fi
  rm -rf "$routed"
  unset token
  if [[ "$posted" -eq 0 && "$failed" -eq 0 ]]; then
    echo "sheet append skipped ($label): empty telegram, x, and direct batches" >&2
    return 0
  fi
  [[ "$failed" -eq 0 ]]
}

# Retry rows that were qualified in previous runs but could not be written.
# This makes a temporary Sheets outage recoverable instead of losing leads.
for pending in "$PENDING_DIR"/*.json; do
  [[ -e "$pending" ]] || continue
  if append_pending_json "$pending" "pending $(basename "$pending")"; then
    rm -f "$pending"
  else
    break
  fi
done

# build command args
scrape() {
  local session="$1" groups="$2" append_flag="$3"
  local cmd=(python3 "$ROOT/leadgen/tg_scrape_posters.py"
    --session "$session" --groups "$groups"
    --mode "${TG_SCRAPE_MODE:-posters}"
    --member-limit "${TG_MEMBER_LIMIT:-100}"
    --msg-limit "${MSG_LIMIT:-1800}" --delay "${TG_SCRAPE_DELAY:-0.25}"
    --output "$RAW")
  [ -n "$append_flag" ] && cmd+=(--append)
  [ -f "$MASTER" ] && cmd+=(--exclude "$MASTER")
  # Лимит 5 личных, группы не писать.
  if [ "${SEND:-0}" = "1" ]; then
    cmd+=(--send --send-delay "${SEND_DELAY:-45}" --max-send "${MAX_SEND:-5}")
  fi
  "${cmd[@]}"
}

# 1. New public chats on tim3, then the older chats on tim2.
scrape "$SESSION_NEW" "$TG_GROUPS_NEW" ""
scrape "$SESSION_OLD" "$TG_GROUPS_OLD" append

# 1b2. Comments under channel posts: resolve linked discussion chats for the
# channels in TG_COMMENT_CHANNELS and any linked chats discovered by tg_channel_graph.py.
LINKED_GROUPS=""
if [ -n "${TG_COMMENT_CHANNELS:-}" ]; then
  LINKED_GROUPS="$(python3 "$ROOT/leadgen/tg_resolve_linked.py" \
    --session "$SESSION_NEW" --channels "$TG_COMMENT_CHANNELS" 2>&1 | grep '^id' | paste -sd, -)"
fi
if [ -s "$STATE_DIR/discovered_comment_channels.txt" ]; then
  DISCOVERED_LINKED="$(grep '^id' "$STATE_DIR/discovered_comment_channels.txt" | sort -u | paste -sd, -)"
  if [ -n "$DISCOVERED_LINKED" ]; then
    LINKED_GROUPS="${LINKED_GROUPS:+$LINKED_GROUPS,}$DISCOVERED_LINKED"
  fi
fi
if [ -n "$LINKED_GROUPS" ]; then
  scrape "$SESSION_NEW" "$LINKED_GROUPS" append
fi

# 1b3. Lurkers: member lists of the same groups, appended to the same RAW.
# Profile cache (.tg_pipeline/profile_cache.json, TTL 7d) absorbs the repeat lookups.
TG_SCRAPE_MODE=members TG_MEMBER_LIMIT="${TG_LURKER_LIMIT:-1000}" scrape "$SESSION_NEW" "$TG_GROUPS_NEW" append
TG_SCRAPE_MODE=members TG_MEMBER_LIMIT="${TG_LURKER_LIMIT:-1000}" scrape "$SESSION_OLD" "$TG_GROUPS_OLD" append

# 1b. Bounded public discovery. A failing source is non-fatal.
# Admission is only admit_discovered_row: Telegram, else X, else LinkedIn/email/WhatsApp, else drop.
python3 "$ROOT/leadgen/creator_external_leads.py" \
  --output "$EXTERNAL" \
  --limit "${CREATOR_EXTERNAL_LIMIT:-20}" \
  --verify-telegram --verify-telegram-telethon --no-qualify \
  >/dev/null 2>"$EXTERNAL.log" \
  || echo "# external CreatorOS discovery failed (non-fatal)"

# All buyer X queries plus competitor comments, and one subreddit. Same Jev gate.
SOCIAL="$STATE_DIR/social_$TS.jsonl"
{ python3 "$ROOT/leadgen/treg_social_leads.py" \
    --output "$SOCIAL" \
    --seen "$STATE_DIR/treg_social_seen.json" \
    || echo "# treg X/Reddit discovery failed (non-fatal)"; } &
SOCIAL_PID=$!

# Whop owner only with x.com or t.me.
BUYER="$STATE_DIR/buyer_$TS.jsonl"
{ python3 "$ROOT/leadgen/buyer_public.py" --output "$BUYER" \
    || echo "# whop/bloss/propinfa failed (non-fatal)"; } &
BUYER_PID=$!

# One LinkedIn jobs search. A dead call must not stop the scrape.
JOBS="$STATE_DIR/linkedin_jobs_$TS.jsonl"
{ python3 "$ROOT/leadgen/linkedin_jobs.py" --output "$JOBS" \
    || echo "# linkedin jobs failed (non-fatal)"; } &
JOBS_PID=$!

# Public catalogs, one request each. A dead page must not stop the scrape.
CATALOGS="$STATE_DIR/buyer_catalogs_$TS.jsonl"
{ python3 "$ROOT/leadgen/buyer_catalogs.py" --output "$CATALOGS" \
    || echo "# buyer catalogs failed (non-fatal)"; } &
CATALOGS_PID=$!

# Active Meta Ad Library video advertisers enriched with decision-maker contacts.
export META_ADS="$STATE_DIR/meta_ads_$TS.jsonl"
{ python3 "$ROOT/leadgen/meta_ad_leads.py" \
    --output "$META_ADS" \
    --seen "$STATE_DIR/meta_ads_seen.json" \
    || echo "# meta ad leads failed (non-fatal)"; } &
META_ADS_PID=$!

# Optional Workstream C sources: Telegram channel graph lookalike & Telegram Ad Spy.
# Run sequentially inside one background job so both do not lock tim3.session concurrently.
export CHANNEL_GRAPH="$STATE_DIR/channel_graph_$TS.jsonl"
export AD_SPY="$STATE_DIR/tg_ad_spy_$TS.jsonl"
{
  if [ -f "$ROOT/leadgen/tg_channel_graph.py" ]; then
    python3 "$ROOT/leadgen/tg_channel_graph.py" \
      --output "$CHANNEL_GRAPH" \
      --state "$STATE_DIR/tg_channel_graph_seen.json" \
      || echo "# tg_channel_graph failed (non-fatal)"
  fi
  if [ -f "$ROOT/leadgen/tg_ad_spy.py" ]; then
    python3 "$ROOT/leadgen/tg_ad_spy.py" \
      --output "$AD_SPY" \
      --seen "$STATE_DIR/tg_ad_spy_seen.json" \
      || echo "# tg_ad_spy failed (non-fatal)"
  fi
} &
TG_EXTRA_PID=$!

wait "$SOCIAL_PID" "$BUYER_PID" "$JOBS_PID" "$CATALOGS_PID" "$META_ADS_PID" "$TG_EXTRA_PID" || true

python3 - "$ROOT" "$RAW" "$EXTERNAL" "$SOCIAL" "$BUYER" "$JOBS" "$CATALOGS" <<'PY'
import json, os, sys
root, raw, external, social, buyer, jobs, catalogs = sys.argv[1:]
sys.path.insert(0, root)
from leadgen.tg_to_gsheet import admit_discovered_row
added = 0
counts = {"telegram": 0, "x": 0, "linkedin": 0, "site": 0}
rows = []
for path in (external, social, buyer, jobs, catalogs):
    try:
        rows.extend(json.loads(line) for line in open(path) if line.strip())
    except FileNotFoundError:
        pass
for env_key in ("META_ADS", "CHANNEL_GRAPH", "AD_SPY"):
    extra_path = os.environ.get(env_key) or ""
    if extra_path and os.path.exists(extra_path):
        rows.extend(json.loads(line) for line in open(extra_path) if line.strip())
with open(raw, "a") as out:
    for row in rows:
        admitted = admit_discovered_row(row)
        if not admitted:
            continue
        if str(admitted.get("username") or "").strip():
            counts["telegram"] += 1
        if admitted.get("founder_x"):
            counts["x"] += 1
        if admitted.get("founder_linkedin"):
            counts["linkedin"] += 1
        if "site" in (admitted.get("contact_channels") or []):
            counts["site"] += 1
        out.write(json.dumps(admitted, ensure_ascii=False) + "\n")
        added += 1
print(f"external usable social contacts: {added} (telegram={counts['telegram']}, x={counts['x']}, linkedin={counts['linkedin']}, site={counts['site']})")
PY

# merge raw into master (dedup by id). X-only stays out of master_ids
# so a later t.me is not blocked, and stays in raw.new for the X sheet.
python3 - "$ROOT" "$RAW" "$MASTER" <<'PY'
import sys
root, raw, master = sys.argv[1:]
sys.path.insert(0, root)
from leadgen.tg_to_gsheet import apply_master_merge
print(f"new unique this run: {apply_master_merge(raw, master)}")
PY

NEW="$RAW.new"

# 6. YC/Product Hunt fresh startups — independent of the TG chain, so it runs
# in parallel with enrich/qualify/curate/treg. We wait for it BEFORE the sheet
# append stage: both sources append to «Все2», and the dedup reads must not
# race each other. crm_mcp_push then covers both sources.
YC_PH_PID=""
if [ "${SKIP_YC_PH:-0}" = "1" ]; then
  echo "# yc_ph_leads skipped (SKIP_YC_PH=1)"
else
  { python3 "$ROOT/leadgen/yc_ph_leads.py" --yc-limit "${YC_LIMIT:-50}" --ph-limit "${PH_LIMIT:-0}" \
      || echo "# yc_ph_leads failed (non-fatal)"; } &
  YC_PH_PID=$!
fi

if [ -s "$NEW" ]; then
  # 2. enrich (fetch sites)  3. LLM-qualify (batched)  4. curate (overrides+categories)
  python3 "$ROOT/leadgen/tg_enrich_leads.py" --input "$NEW" --output "$RAW.enriched.jsonl" --fetch 2>/dev/null
  python3 "$ROOT/leadgen/tg_llm_qualify.py" --input "$RAW.enriched.jsonl" --output "$RAW.qualified.jsonl"
  python3 "$ROOT/leadgen/tg_curate.py" --input "$RAW.qualified.jsonl" --output "$RAW.final.jsonl" 2>/dev/null
  # Work email, mailbox check, LinkedIn, search demand, competitor ads.
  # Missing treg or an empty balance must not drop the lead batch.
  python3 "$ROOT/leadgen/treg_enrich.py" --input "$RAW.final.jsonl" --output "$RAW.treg.jsonl" \
    --cache "$STATE_DIR/treg_market.json" \
    || cp "$RAW.final.jsonl" "$RAW.treg.jsonl"

  # Sheet writes start here: the parallel YC/PH stage must be finished first.
  if [ -n "$YC_PH_PID" ]; then
    wait "$YC_PH_PID" || true
    YC_PH_PID=""
  fi

  # 5. map to sheet rows (no header) and append
  python3 "$ROOT/leadgen/tg_to_gsheet.py" --input "$RAW.treg.jsonl" --output "$RAW.rows.json" --no-header --dedup-sheet

  if ! append_pending_json "$RAW.rows.json" "current TG batch"; then
    cp "$RAW.rows.json" "$PENDING_DIR/${TS}_tg.json"
    echo "# current TG rows saved for retry: $PENDING_DIR/${TS}_tg.json"
  fi
else
  echo "no new TG leads; skipping enrich/curate/sheet-append"
fi

# no new TG leads path (or a failed append): still wait for YC/PH before the
# final CRM MCP push so the report covers both sources.
if [ -n "$YC_PH_PID" ]; then
  wait "$YC_PH_PID" || true
fi

# Push new leads directly into Foldesk CRM via MCP (replaces csv_notify.py; never fails the pipeline)
python3 "$ROOT/leadgen/crm_mcp_push.py" || echo "# crm_mcp_push failed (non-fatal)"

python3 - "$STATE_DIR" "$TS" "$RAW" "$EXTERNAL" "$SOCIAL" "$BUYER" "$JOBS" "$CATALOGS" "$META_ADS" "$CHANNEL_GRAPH" "$AD_SPY" "$NEW" "$RAW.treg.jsonl" <<'PY' || true
import json, os, sys
state_dir, ts, raw, external, social, buyer, jobs, catalogs, meta_ads, channel_graph, ad_spy, new_f, treg_f = sys.argv[1:]
def _count(p):
    try:
        return sum(1 for l in open(p) if l.strip()) if p and os.path.exists(p) else 0
    except Exception:
        return 0
summary = {
    "ts": ts,
    "raw": _count(raw),
    "external": _count(external),
    "social": _count(social),
    "buyer": _count(buyer),
    "linkedin_jobs": _count(jobs),
    "buyer_catalogs": _count(catalogs),
    "meta_ads": _count(meta_ads),
    "channel_graph": _count(channel_graph),
    "ad_spy": _count(ad_spy),
    "new_unique": _count(new_f),
    "treg_enriched": _count(treg_f),
}
with open(os.path.join(state_dir, "last_run_summary.json"), "w") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f"pipeline summary: {json.dumps(summary, ensure_ascii=False)}")
PY

echo "=== done $TS ==="
