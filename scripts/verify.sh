#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "${PYTHON_BIN:-}" ]; then
  PYTHON_BIN="python3"
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  else
    COMMON_GIT_DIR="$(git -C "$ROOT_DIR" rev-parse --git-common-dir 2>/dev/null || true)"
    if [ -n "$COMMON_GIT_DIR" ]; then
      MAIN_REPO_ROOT="$(cd "$ROOT_DIR" && cd "$COMMON_GIT_DIR/.." 2>/dev/null && pwd || true)"
      if [ -n "$MAIN_REPO_ROOT" ] && [ -x "$MAIN_REPO_ROOT/.venv/bin/python" ]; then
        PYTHON_BIN="$MAIN_REPO_ROOT/.venv/bin/python"
      fi
    fi
  fi
fi

echo "== [1/4] Python AST & Sacred Rules static harness =="
"$PYTHON_BIN" - <<'PY'
import ast
from pathlib import Path
import re
import sys

root = Path(".").resolve()
errors = []

for folder in ("app", "leadgen", "tests"):
    for py_file in sorted((root / folder).rglob("*.py")):
        src = py_file.read_text(encoding="utf-8")
        try:
            ast.parse(src, filename=str(py_file))
        except SyntaxError as exc:
            errors.append(f"SyntaxError in {py_file}: {exc}")

# Sacred Rule 1: Zero legacy CreatorOS in prompts, templates, or outgoing copy across app/ & leadgen/
forbidden_re = re.compile(r"\bCreator\s*OS\b|creatoros", re.IGNORECASE)
defensive_guard_re = re.compile(
    r"(?:re\.compile|re\.sub|in\s+low|in\s+s1_low|not\s+in\b|\.lower\(\)|\.casefold\(\)|LIKE\s+'%creatoros%'|has_creatoros|legacy_creatoros|legacy_brand_creatoros|устаревший\s+бренд|for\s+marker\s+in|\(\?:creatoros\||\(\?P<|\\bCreatorOS\\b|_CREATOROS_|freeze_legacy_1300_and_scrub_creatoros)",
    re.IGNORECASE,
)
for folder in ("app", "leadgen"):
    for py_file in sorted((root / folder).rglob("*.py")):
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if forbidden_re.search(line) and not defensive_guard_re.search(line):
                errors.append(
                    f"Sacred Rule 1 violation: legacy brand 'CreatorOS' in {py_file}:{lineno}: {stripped[:120]}"
                )

# Anti-fragility check: no broken app.runtime_context import
for py_file in sorted((root / "leadgen").rglob("*.py")):
    src = py_file.read_text(encoding="utf-8")
    if "app.runtime_context" in src:
        errors.append(f"Broken import 'app.runtime_context' found in {py_file}")

# Gate A: Zero PYTEST_CURRENT_TEST / sys.modules["pytest"] / *_IN_TEST env split-brain across all app/ & leadgen/
pytest_split_re = re.compile(r"PYTEST_CURRENT_TEST|[\"']pytest[\"']\s+(?:not\s+)?in\s+sys\.modules")
in_test_env_re = re.compile(r"\b[A-Z][A-Z0-9_]*_IN_TEST\b")
for folder in ("app", "leadgen"):
    for py_file in sorted((root / folder).rglob("*.py")):
        for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if pytest_split_re.search(line):
                errors.append(
                    f"Zero PYTEST_CURRENT_TEST Gate violation in {py_file}:{lineno}: {line.strip()[:120]}"
                )
            if in_test_env_re.search(line):
                errors.append(
                    f"Zero *_IN_TEST Env Gate violation in {py_file}:{lineno}: {line.strip()[:120]}"
                )

# Gate B: Systemd Hollow-Pipe, CRMTG_VPS_RUNTIME=1 & Missing Module Gate across deploy/systemd/*.service
import importlib.util

systemd_dir = root / "deploy" / "systemd"
discovery_markers = (
    "vps_autonomous_replenish",
    "tg_pipeline",
    "yc_ph_leads",
    "job_monitor",
    "tg_channel_graph",
    "discover_",
)
dash_m_re = re.compile(r"(?:^|\s)-m\s+([A-Za-z_][A-Za-z0-9_.]*)")
for svc_file in sorted(systemd_dir.glob("*.service")):
    raw_lines = svc_file.read_text(encoding="utf-8").splitlines()
    logical_lines = []
    buf = ""
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1].rstrip() + " "
        else:
            logical_lines.append((buf + stripped).strip())
            buf = ""
    if buf:
        logical_lines.append(buf.strip())

    if 'Environment="CRMTG_VPS_RUNTIME=1"' not in logical_lines:
        errors.append(
            f'Systemd Runtime Gate violation in {svc_file}: missing Environment="CRMTG_VPS_RUNTIME=1"'
        )

    exec_starts = [
        ln.split("=", 1)[1].strip()
        for ln in logical_lines
        if ln.startswith("ExecStart=")
    ]
    if not exec_starts:
        errors.append(f"Systemd Hollow-Pipe Gate violation in {svc_file}: missing ExecStart=")
        continue
    for exec_cmd in exec_starts:
        if "push_discovered_now" in exec_cmd and not any(
            m in exec_cmd for m in discovery_markers
        ):
            errors.append(
                f"Systemd Hollow-Pipe Gate violation in {svc_file}: bare push_discovered_now without upstream discovery"
            )
        for mod_name in dash_m_re.findall(exec_cmd):
            top_pkg = mod_name.split(".", 1)[0]
            mod_rel = Path(*mod_name.split("."))
            if (root / top_pkg).is_dir() or top_pkg in ("app", "leadgen", "scripts", "tests"):
                py_target = (root / mod_rel).with_suffix(".py")
                pkg_target = root / mod_rel / "__init__.py"
                if not (py_target.is_file() or pkg_target.is_file()):
                    errors.append(
                        f"Systemd Hollow-Pipe Gate violation in {svc_file}: missing repo module '-m {mod_name}'"
                    )
            else:
                if importlib.util.find_spec(mod_name) is None:
                    errors.append(
                        f"Systemd Hollow-Pipe Gate violation in {svc_file}: unresolvable module '-m {mod_name}'"
                    )

# Gate C: Reply-Alert Eligibility & Anti-Echo Invariant Gate
if (root / "app" / "inbox.py").is_file():
    inbox_src = (root / "app" / "inbox.py").read_text(encoding="utf-8")
    if re.search(r"base_notify_trigger\s*=\s*\([\s\S]{0,400}dlg_row\s+is\s+not\s+None", inbox_src):
        errors.append("Reply-Alert Gate violation in app/inbox.py: bare 'dlg_row is not None' in base_notify_trigger spams non-leads")
    if "is_ineligible_reply_notify_peer" not in inbox_src:
        errors.append("Reply-Alert Gate violation: missing canonical is_ineligible_reply_notify_peer guard in app/inbox.py")
    if re.search(r"require_backing\s*=\s*(?:False\b|bool\()", inbox_src):
        errors.append("Reply-Alert Gate violation in app/inbox.py: forbidden require_backing=False/bool() bypass")
    if 'direction or "").strip().lower() != "in"' not in inbox_src:
        errors.append("Reply-Alert Gate violation in app/inbox.py: stop_outreach_on_reply must reject direction != 'in'")
if (root / "app" / "digest.py").is_file():
    digest_src = (root / "app" / "digest.py").read_text(encoding="utf-8")
    if "is_ineligible_reply_notify_peer" not in digest_src or "extract_bot_user_id_from_token" not in digest_src:
        errors.append("Reply-Alert Gate violation: missing is_ineligible_reply_notify_peer / extract_bot_user_id_from_token guard in app/digest.py")
    if re.search(r"require_backing\s*=\s*(?:False\b|bool\()", digest_src):
        errors.append("Reply-Alert Gate violation in app/digest.py: forbidden require_backing=False/bool() bypass")
    if 'direction or "").strip().lower() != "in"' not in digest_src:
        errors.append("Reply-Alert Gate violation in app/digest.py: send_lead_reply_notification must reject direction != 'in'")
if (root / "app" / "main.py").is_file() and (root / "app" / "inbox.py").is_file():
    main_guard_src = (root / "app" / "main.py").read_text(encoding="utf-8")
    if "record_account_health_transition" not in main_guard_src:
        errors.append("Reply-Alert/Health Gate violation in app/main.py: missing immediate record_account_health_transition on auth failure")
if (root / "app" / "outreach.py").is_file():
    outreach_guard_src = (root / "app" / "outreach.py").read_text(encoding="utf-8")
    if "skip_reply_stop=True" not in outreach_guard_src:
        errors.append("Outreach Gate violation in app/outreach.py: pre-followup cache_messages must pass skip_reply_stop=True")
if (root / "app" / "telegram_gateway.py").is_file():
    tg_gw_src = (root / "app" / "telegram_gateway.py").read_text(encoding="utf-8")
    if 'getattr(peer, "bot", False)' not in tg_gw_src or 'getattr(entity, "bot", False)' not in tg_gw_src:
        errors.append("Reply-Alert Gate violation in app/telegram_gateway.py: missing bot peer filter in _private_payload / _load_private")
if (root / "app" / "inbox.py").is_file() and (
    not (root / "scripts" / "proactive_audit.py").is_file()
    or not (root / "docs" / "VERIFICATION_PLAYBOOKS.md").is_file()
):
    errors.append("Harness Gate violation: missing scripts/proactive_audit.py or docs/VERIFICATION_PLAYBOOKS.md")

# Gate D: Vacancy Outreach Default-Off & Hard B2B Account Pool Isolation Gate
lg_settings_file = root / "app" / "leadgen_settings.py"
if lg_settings_file.is_file():
    lg_src = lg_settings_file.read_text(encoding="utf-8")
    if re.search(r'"vacancies"\s*:\s*\{\s*"enabled"\s*:\s*True', lg_src):
        errors.append(
            "Vacancy Gate violation: DEFAULT_LEADGEN_CONFIG['vacancies']['enabled'] must be False by default"
        )
db_file = root / "app" / "db.py"
if db_file.is_file():
    db_src = db_file.read_text(encoding="utf-8")
    if "def apply_disable_vacancy_outreach_v6(" not in db_src:
        errors.append(
            "Vacancy Gate violation: missing apply_disable_vacancy_outreach_v6 migration in app/db.py"
        )
outreach_file = root / "app" / "outreach.py"
if outreach_file.is_file():
    outreach_src = outreach_file.read_text(encoding="utf-8")
    if "def find_vacancy_account_pool_violations(" not in outreach_src:
        errors.append(
            "Vacancy Gate violation: missing find_vacancy_account_pool_violations in app/outreach.py"
        )
    if outreach_src.count("find_vacancy_account_pool_violations(") < 4:
        errors.append(
            "Vacancy Gate violation: find_vacancy_account_pool_violations must guard create_campaign, update_campaign, run_campaign_batch, and preview_outreach_batch"
        )
    if "_VACANCY_JUNK_ROLE_RE" not in outreach_src or "l.created_at AS lead_created_at" not in outreach_src:
        errors.append(
            "Vacancy Gate violation: app/outreach.py must enforce _VACANCY_JUNK_ROLE_RE and select l.created_at AS lead_created_at"
        )
job_mon_file = root / "leadgen" / "job_monitor.py"
if job_mon_file.is_file():
    if "VACANCY_JUNK_RX" not in job_mon_file.read_text(encoding="utf-8"):
        errors.append(
            "Vacancy Gate violation: leadgen/job_monitor.py must enforce VACANCY_JUNK_RX"
        )
app_js_file = root / "app" / "static" / "app.js"
if app_js_file.is_file():
    app_js_src = app_js_file.read_text(encoding="utf-8")
    if re.search(r"vacancies\s*:\s*\{\s*enabled\s*:\s*true", app_js_src):
        errors.append(
            "Vacancy Gate violation: app/static/app.js must default vacancies.enabled to false"
        )
index_html_file = root / "app" / "templates" / "index.html"
if index_html_file.is_file():
    index_html_src = index_html_file.read_text(encoding="utf-8")
    if 'id="leadgen-vacancies-enabled" checked' in index_html_src:
        errors.append(
            "Vacancy Gate violation: app/templates/index.html must not pre-check #leadgen-vacancies-enabled"
        )

# Gate E: Sniper ICP Hygiene & Zero-Hollow-Pipe Server Readiness Gate
main_py_file = root / "app" / "main.py"
if main_py_file.is_file():
    main_py_src = main_py_file.read_text(encoding="utf-8")
    try:
        main_tree = ast.parse(main_py_src, filename=str(main_py_file))
    except SyntaxError:
        main_tree = None
    health_strs = set()
    if main_tree is not None:
        for node in ast.walk(main_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "api_health":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        health_strs.add(sub.value)
    if "icp_hygiene_ok" not in health_strs or "leadgen_ready" not in health_strs:
        errors.append(
            "Gate E violation in app/main.py: /api/health (api_health) must surface 'icp_hygiene_ok' and 'leadgen_ready'"
        )

proactive_audit_file = root / "scripts" / "proactive_audit.py"
if proactive_audit_file.is_file():
    pa_src = proactive_audit_file.read_text(encoding="utf-8")
    if "icp_hygiene_ok" not in pa_src or "leadgen_ready" not in pa_src:
        errors.append(
            "Gate E violation in scripts/proactive_audit.py: must enforce 'icp_hygiene_ok' and 'leadgen_ready'"
        )

deploy_wf_file = root / ".github" / "workflows" / "deploy-vps.yml"
if deploy_wf_file.is_file():
    wf_src = deploy_wf_file.read_text(encoding="utf-8")
    if "icp_hygiene_ok" not in wf_src or "leadgen_ready" not in wf_src:
        errors.append(
            "Gate E violation in .github/workflows/deploy-vps.yml: deep healthcheck must verify 'icp_hygiene_ok' and 'leadgen_ready'"
        )

for required_svc_name in ("crmtg.service", "crmtg-leadgen.service"):
    req_svc = systemd_dir / required_svc_name
    if req_svc.is_file():
        svc_active_lines = [
            ln.strip()
            for ln in req_svc.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith(";")
        ]
        has_usr_local_path = any(
            ln.startswith("Environment=") and "PATH=" in ln and "/usr/local/bin" in ln
            for ln in svc_active_lines
        )
        if not has_usr_local_path:
            errors.append(
                f"Gate E violation in {req_svc}: missing Environment PATH= containing /usr/local/bin"
            )

hygiene_py_file = root / "app" / "hygiene.py"
if hygiene_py_file.is_file():
    hygiene_src = hygiene_py_file.read_text(encoding="utf-8")
    for required_def in (
        "def audit_crm_hygiene(",
        "def heal_crm_hygiene(",
        "def audit_server_leadgen_readiness(",
    ):
        if required_def not in hygiene_src:
            errors.append(
                f"Gate E violation in app/hygiene.py: missing required contract function '{required_def}'"
            )

# Gate F: Zero Bare Mac Session Path Gate across leadgen/*.py
bare_mac_session_re = re.compile(
    r"^\s*(?:DEFAULT_)?SESSION\s*=\s*[\"']/Users/timofei/.*\.session[\"']"
)
for py_file in sorted((root / "leadgen").rglob("*.py")):
    for lineno, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
        if bare_mac_session_re.search(line):
            errors.append(
                f"Gate F violation in {py_file}:{lineno}: hardcoded Mac .session path without TG_SESSION/SCRAPER_TG_SESSION env override"
            )

# Gate G: High-Yield Veins, Non-Empty Ad-Spy/Graph Seeds & Store Bridge Gate
def _has_nonempty_literal_assign(py_path: Path, target_name: str) -> bool:
    if not py_path.is_file():
        return True
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        val = None
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == target_name for t in node.targets):
                val = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == target_name:
                val = node.value
        if val is not None:
            if isinstance(val, (ast.Tuple, ast.List, ast.Set)) and len(val.elts) > 0:
                return True
            return False
    return False

ad_spy_file = root / "leadgen" / "tg_ad_spy.py"
if not _has_nonempty_literal_assign(ad_spy_file, "DEFAULT_AD_SPY_CHANNELS"):
    errors.append("Gate G violation in leadgen/tg_ad_spy.py: missing or empty DEFAULT_AD_SPY_CHANNELS literal")

ch_graph_file = root / "leadgen" / "tg_channel_graph.py"
if not _has_nonempty_literal_assign(ch_graph_file, "DEFAULT_GRAPH_SEED_CHANNELS"):
    errors.append("Gate G violation in leadgen/tg_channel_graph.py: missing or empty DEFAULT_GRAPH_SEED_CHANNELS literal")

catalogs_file = root / "leadgen" / "buyer_catalogs.py"
if catalogs_file.is_file():
    cat_src = catalogs_file.read_text(encoding="utf-8")
    m_sources = re.search(r"SOURCES\s*=\s*\(([\s\S]*?)\n\)", cat_src)
    if m_sources and ("fivem-server.org" in m_sources.group(1) or "6charts.com" in m_sources.group(1)):
        errors.append("Gate G violation in leadgen/buyer_catalogs.py: dead 0%-contact fivem/gta hosts left in default SOURCES")

repl_file = root / "leadgen" / "vps_autonomous_replenish.py"
repl_src = repl_file.read_text(encoding="utf-8") if repl_file.is_file() else ""
if (root / "leadgen" / "meta_ad_leads.py").is_file() or "def run_autonomous_replenish(" in repl_src:
    for req_leadgen_mod in (
        "monid_client.py",
        "high_yield_veins.py",
        "tg_identity_bridge.py",
        "contact_waterfall.py",
        "ofm_and_groups.py",
    ):
        if not (root / "leadgen" / req_leadgen_mod).is_file():
            errors.append(f"Gate G violation: missing required module leadgen/{req_leadgen_mod}")
    if "def run_autonomous_replenish(" in repl_src:
        for req_stage in (
            "high_yield_veins.py",
            "tg_identity_bridge.py",
            "contact_waterfall.py",
            "ofm_and_groups.py",
            "last_run_summary.json",
        ):
            if req_stage not in repl_src:
                errors.append(f"Gate G violation in leadgen/vps_autonomous_replenish.py: missing '{req_stage}' wiring")
    lg_settings_src = lg_settings_file.read_text(encoding="utf-8") if lg_settings_file.is_file() else ""
    if "ofm_review_leads.jsonl" not in lg_settings_src:
        errors.append("Gate G violation in app/leadgen_settings.py: push_discovered_now must exclude ofm_review_leads.jsonl")
    hy_veins_file = root / "leadgen" / "high_yield_veins.py"
    hy_veins_src = hy_veins_file.read_text(encoding="utf-8") if hy_veins_file.is_file() else ""
    for d2c_label, d2c_src in (
        ("leadgen/high_yield_veins.py", hy_veins_src),
        ("leadgen/vps_autonomous_replenish.py", repl_src),
        ("app/leadgen_settings.py", lg_settings_src),
    ):
        if "d2c_whatsapp" not in d2c_src:
            errors.append(f"Gate G violation in {d2c_label}: missing 'd2c_whatsapp' wiring")
    for ecom_path in (
        root / "leadgen" / "tg_enrich_leads.py",
        root / "leadgen" / "tg_llm_qualify.py",
        root / "leadgen" / "tg_curate.py",
        root / "leadgen" / "tg_to_gsheet.py",
    ):
        if ecom_path.is_file():
            ecom_src = ecom_path.read_text(encoding="utf-8")
            if "E-com / D2C" not in ecom_src:
                errors.append(f"Gate G violation in {ecom_path.name}: missing 'E-com / D2C' category")
    tg_enrich_file = root / "leadgen" / "tg_enrich_leads.py"
    if tg_enrich_file.is_file():
        tg_enrich_src = tg_enrich_file.read_text(encoding="utf-8")
        if "d2c_whatsapp" not in tg_enrich_src or "fb_groups" not in tg_enrich_src:
            errors.append("Gate G violation in leadgen/tg_enrich_leads.py: missing 'd2c_whatsapp' or 'fb_groups' protection")
    mcp_push_file = root / "leadgen" / "crm_mcp_push.py"
    if mcp_push_file.is_file() and "_push_tg_wa_split" not in mcp_push_file.read_text(encoding="utf-8"):
        errors.append("Gate G violation in leadgen/crm_mcp_push.py: missing WA/TG split on --jsonl / Все2")
    mcp_srv_file = root / "app" / "mcp_server.py"
    if mcp_srv_file.is_file() and "is_item_wa_only" not in mcp_srv_file.read_text(encoding="utf-8"):
        errors.append("Gate G violation in app/mcp_server.py: missing WA-only isolation in ingest_discovered_leads")

# Gate H: Zero Fake Data, Zero Session Stubs & WhatsApp Anti-Ban Gate
forbidden_prod_tokens = (
    "generate_pairing_code",
    "generate_qr_svg",
    "generateWaPairingCode",
    "DEFAULT_WA_QR_SVG",
    "WA84-",
    "QR_SCANNED",
    "SQLite format 3\\x00whatsapp",
    "stub_bytes",
    "PENDING:{json",
    "_use_live_neonize",
    "+79990001122",
    "+79990000000",
    "SAMPLE_SESSION_JSON",
    "fill-sample-session-btn",
    'f"+7999000',
    "Math.floor(1000000",
)
for folder in ("app", "leadgen"):
    folder_path = root / folder
    if not folder_path.is_dir():
        continue
    for fpath in sorted(folder_path.rglob("*")):
        if not fpath.is_file() or fpath.suffix not in (".py", ".js", ".html"):
            continue
        fsrc = fpath.read_text(encoding="utf-8")
        for tok in forbidden_prod_tokens:
            if tok in fsrc:
                errors.append(f"Gate H violation in {fpath}: forbidden fake/stub token '{tok}'")

wa_gw_file = root / "app" / "whatsapp_gateway.py"
if (root / "app" / "accounts.py").is_file() and not wa_gw_file.is_file():
    errors.append("Gate H violation: missing app/whatsapp_gateway.py")
if wa_gw_file.is_file():
    wa_gw_src = wa_gw_file.read_text(encoding="utf-8")
    for req_wa_tok in (
        "ProxySettings",
        "is_on_whatsapp",
        "send_chat_presence",
        "CHAT_PRESENCE_COMPOSING",
        "def apply_proxy(",
        "data:image/svg+xml;base64,",
    ):
        if req_wa_tok not in wa_gw_src:
            errors.append(f"Gate H violation in app/whatsapp_gateway.py: missing anti-ban contract '{req_wa_tok}'")

    if main_py_file.is_file() and "apply_proxy" not in main_py_file.read_text(encoding="utf-8"):
        errors.append("Gate H violation in app/main.py: missing WhatsApp apply_proxy wiring")

    if outreach_file.is_file() and "сессия whatsapp не авторизована" not in outreach_file.read_text(encoding="utf-8"):
        errors.append("Gate H violation in app/outreach.py: missing WhatsApp error classification")

    if hygiene_py_file.is_file() and "healed_fake_accounts" not in hygiene_py_file.read_text(encoding="utf-8"):
        errors.append("Gate H violation in app/hygiene.py: missing healed_fake_accounts self-healing")

forbidden_test_tokens = (
    "zip_session_bytes",
    "1BVtsOHwButest_session_string",
    "generate_pairing_code",
    "SQLite format 3\\x00whatsapp",
)
tests_dir = root / "tests"
if tests_dir.is_dir():
    for tpath in sorted(tests_dir.rglob("*.py")):
        tsrc = tpath.read_text(encoding="utf-8")
        for tok in forbidden_test_tokens:
            if tok in tsrc:
                errors.append(f"Gate H violation in {tpath}: forbidden fake test stub '{tok}'")

# Gate I: Static AST check for calling/reading a nested `def <name>` before its definition in the same function scope
def _check_local_def_use_before_def(py_path: Path) -> list[str]:
    found: list[str] = []
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except SyntaxError:
        return found

    def _walk_scope_exprs(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from _walk_scope_exprs(child)

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        globals_or_nonlocals: set[str] = set()
        arg_names = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        if fn.args.vararg:
            arg_names.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            arg_names.add(fn.args.kwarg.arg)
        local_defs: dict[str, int] = {}
        local_imports: dict[str, int] = {}
        earliest_assigns: dict[str, int] = {}
        loads: list[tuple[str, int]] = []
        stack = list(fn.body)
        while stack:
            node = stack.pop(0)
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                globals_or_nonlocals.update(node.names)
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_defs[node.name] = min(local_defs.get(node.name, 10**9), node.lineno)
                for dec in node.decorator_list:
                    for sub in _walk_scope_exprs(dec):
                        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                            loads.append((sub.id, sub.lineno))
                continue
            if isinstance(node, ast.ClassDef):
                local_defs[node.name] = min(local_defs.get(node.name, 10**9), node.lineno)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound = (alias.asname or alias.name.split(".", 1)[0])
                    if bound != "*":
                        local_imports[bound] = min(local_imports.get(bound, 10**9), node.lineno)
                        earliest_assigns[bound] = min(earliest_assigns.get(bound, 10**9), node.lineno)
                continue
            if isinstance(node, ast.ExceptHandler) and node.name:
                earliest_assigns[node.name] = min(earliest_assigns.get(node.name, 10**9), node.lineno)
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    earliest_assigns[node.id] = min(earliest_assigns.get(node.id, 10**9), node.lineno)
                elif isinstance(node.ctx, ast.Load):
                    loads.append((node.id, node.lineno))
            for child in ast.iter_child_nodes(node):
                stack.append(child)
        for name, uline in loads:
            if name in globals_or_nonlocals or name in arg_names:
                continue
            if name in local_defs or name in local_imports:
                ebind = min(
                    local_defs.get(name, 10**9),
                    local_imports.get(name, 10**9),
                    earliest_assigns.get(name, 10**9),
                )
                if uline < ebind:
                    bind_line = local_defs.get(name, local_imports.get(name, ebind))
                    found.append(
                        f"Gate I violation in {py_path}:{uline} ({fn.name}): local '{name}' used before def/import at line {bind_line}"
                    )
    return found

for folder in ("app", "leadgen"):
    folder_path = root / folder
    if not folder_path.is_dir():
        continue
    for py_file in sorted(folder_path.rglob("*.py")):
        errors.extend(_check_local_def_use_before_def(py_file))

# Gate J: Zero Hardcoded Stats & Undefined UI Symbols Gate
forbidden_stats_literals = (
    "2 174",
    "2174",
    "129 ждущих",
    "3.6% отказов",
    "30.5% всей выручки",
    "AI (30.6%",
)
for ui_sub in (root / "app").rglob("*"):
    if not ui_sub.is_file() or ui_sub.suffix not in (".py", ".js", ".html"):
        continue
    usrc = ui_sub.read_text(encoding="utf-8")
    for lit in forbidden_stats_literals:
        if lit in usrc:
            errors.append(f"Gate J violation in {ui_sub}: hardcoded stats literal '{lit}'")
    if ui_sub.suffix in (".js", ".html") and "setActiveSheet(" in usrc:
        errors.append(f"Gate J violation in {ui_sub}: undefined call 'setActiveSheet(' (use activateSheet)")

# Gate K: Simplified 4-Tab CRM UI Contract
idx_path = root / "app" / "templates" / "index.html"
js_path = root / "app" / "static" / "app.js"
if idx_path.is_file():
    idx_html = idx_path.read_text(encoding="utf-8")
    nav_match = re.search(r'<nav class="nav"[^>]*>([\s\S]*?)</nav>', idx_html)
    if not nav_match:
        errors.append("Gate K violation: missing <nav class=\"nav\"> in app/templates/index.html")
    else:
        nav_links = re.findall(
            r'<a\b[^>]*href="([^"]+)"[^>]*data-sheet-link="([^"]+)"[^>]*>([\s\S]*?)</a>',
            nav_match.group(1),
        )
        cleaned_links = [
            (href, sheet, re.sub(r"<[^>]+>", "", lbl).strip())
            for href, sheet, lbl in nav_links
        ]
        expected_links = [
            ("#s04", "inbox", "Входящие"),
            ("#leads", "leads", "Лиды"),
            ("#outreach", "outreach", "Рассылки"),
            ("#analytics", "analytics", "Статистика"),
        ]
        if cleaned_links != expected_links:
            errors.append(
                f"Gate K violation in app/templates/index.html: expected 4 nav tabs {expected_links}, got {cleaned_links}"
            )
    for req_id in (
        'id="leads-view-toggle"',
        'id="outreach-subtabs"',
        'id="stats-campaigns-grid"',
        'id="stats-accounts-health"',
        'id="miniapp-bottom-nav"',
    ):
        if req_id not in idx_html:
            errors.append(f"Gate K violation in app/templates/index.html: missing {req_id}")
    m_user_btn = re.search(r'<button[^>]*id="crm-user-btn"[^>]*>', idx_html)
    if not m_user_btn or "topbar-duplicate-btn" in m_user_btn.group(0):
        errors.append("Gate K violation in app/templates/index.html: #crm-user-btn must remain visible (no topbar-duplicate-btn)")

if idx_path.is_file() and js_path.is_file():
    js_src = js_path.read_text(encoding="utf-8")
    for req_fn in (
        "function moveDialogToStage(",
        "function renderStatsExecutiveBlocks(",
        "function buildInboxStageBar(",
        "function openClientProfile(",
    ):
        if req_fn not in js_src:
            errors.append(f"Gate K violation in app/static/app.js: missing {req_fn}")

# Gate L: Strict CRM-Only Inbox Eligibility & Full Client Profile Drawer
if idx_path.is_file() and 'id="client-profile-modal"' not in idx_html:
    errors.append("Gate L violation in app/templates/index.html: missing id=\"client-profile-modal\"")
if (root / "app" / "inbox.py").is_file():
    inbox_src_l = (root / "app" / "inbox.py").read_text(encoding="utf-8")
    m_fcm = re.search(r"def _filter_crm_only_rows\([\s\S]*?(?=\ndef\s)", inbox_src_l)
    if not m_fcm or "is_ineligible_reply_notify_peer" not in m_fcm.group(0):
        errors.append("Gate L violation in app/inbox.py: _filter_crm_only_rows must enforce is_ineligible_reply_notify_peer")
if main_py_file.is_file() and (root / "app" / "inbox.py").is_file():
    main_src_l = main_py_file.read_text(encoding="utf-8")
    for prof_route in ("/api/dialogs/{dialog_id}/profile", "/api/leads/{lead_id}/profile"):
        if prof_route not in main_src_l:
            errors.append(f"Gate L violation in app/main.py: missing route '{prof_route}'")

# Gate M: Old-Lead & Completed-Recipient Message Visibility, Chunked SQL & Git Hooks Gate
if (root / "app" / "inbox.py").is_file():
    db_src_m = (root / "app" / "db.py").read_text(encoding="utf-8") if (root / "app" / "db.py").is_file() else ""
    for db_sym in (
        "def clean_tg_username(",
        "def sql_norm_tg_username_expr(",
        "def is_recipient_contacted_outreach(",
        "def is_newer_dialog_snapshot_unbacked(",
    ):
        if db_sym not in db_src_m:
            errors.append(f"Gate M violation in app/db.py: missing canonical helper {db_sym}")

    inbox_src_m = (root / "app" / "inbox.py").read_text(encoding="utf-8")
    for fn_name, req_call in (
        ("def stop_outreach_on_reply(", "is_recipient_contacted_outreach("),
        ("def _person_is_crm_relevant(", "is_recipient_contacted_outreach("),
        ("def _filter_crm_only_rows(", "is_recipient_contacted_outreach("),
        ("def list_messages(", "is_newer_dialog_snapshot_unbacked("),
    ):
        m_fn = re.search(rf"{re.escape(fn_name)}[\s\S]*?(?=\ndef\s)", inbox_src_m)
        if not m_fn or req_call not in m_fn.group(0):
            errors.append(f"Gate M violation in app/inbox.py: {fn_name} must call {req_call}")

    outreach_src_m = (root / "app" / "outreach.py").read_text(encoding="utf-8") if (root / "app" / "outreach.py").is_file() else ""
    m_frd = re.search(r"def _find_recipient_dialogs\([\s\S]*?(?=\ndef\s)", outreach_src_m)
    if not m_frd or "sent_account_id" not in m_frd.group(0) or "sql_norm_tg_username_expr(" not in m_frd.group(0):
        errors.append("Gate M violation in app/outreach.py: _find_recipient_dialogs must use sent_account_id and sql_norm_tg_username_expr")

    analytics_src_m = (root / "app" / "analytics.py").read_text(encoding="utf-8") if (root / "app" / "analytics.py").is_file() else ""
    m_cca = re.search(r"def _compute_charts_analytics\([\s\S]*?(?=\ndef\s)", analytics_src_m)
    if not m_cca or "range(0, len(eligible_ids), 400)" not in m_cca.group(0):
        errors.append("Gate M violation in app/analytics.py: _compute_charts_analytics must chunk eligible_ids by 400")

    if main_py_file.is_file():
        main_src_m = main_py_file.read_text(encoding="utf-8")
        m_fpec = re.search(r"def _filter_people_to_existing_or_crm\([\s\S]*?(?=\ndef\s|\nasync def\s)", main_src_m)
        if not m_fpec or "is_recipient_contacted_outreach(" not in m_fpec.group(0):
            errors.append("Gate M violation in app/main.py: _filter_people_to_existing_or_crm must call is_recipient_contacted_outreach")
        m_auto = re.search(r"async def autopilot_loop\([\s\S]*?(?=\ndef\s|\nasync def\s)", main_src_m)
        if not m_auto or "sync_crm_dialog_messages_backfill_once(" not in m_auto.group(0):
            errors.append("Gate M violation in app/main.py: autopilot_loop must call sync_crm_dialog_messages_backfill_once")
        m_sync_now = re.search(r"    async def inbox_sync_now\([\s\S]*?(?=\n    @app\.|\n    def |\n    async def |\ndef )", main_src_m)
        if not m_sync_now or "sync_crm_dialog_messages_backfill_once(" not in m_sync_now.group(0):
            errors.append("Gate M violation in app/main.py: inbox_sync_now must call sync_crm_dialog_messages_backfill_once")
        m_prof = re.search(r"    async def _resolve_and_enrich_client_profile\([\s\S]*?(?=\n    @app\.|\n    def |\n    async def |\ndef )", main_src_m)
        if not m_prof or "get_thread(" not in m_prof.group(0):
            errors.append("Gate M violation in app/main.py: _resolve_and_enrich_client_profile must call get_thread for stale/missing messages")

    if js_path.is_file():
        js_src_m = js_path.read_text(encoding="utf-8")
        if "function sortDialogsByRecency(" not in js_src_m or js_src_m.count("sortDialogsByRecency(") < 5:
            errors.append("Gate M violation in app/static/app.js: sortDialogsByRecency must be defined and used across upsertDialog/renderDialogs/openInboxLive/loadInbox")

    if (root / "scripts" / "proactive_audit.py").is_file():
        audit_src_m = (root / "scripts" / "proactive_audit.py").read_text(encoding="utf-8")
        if "_audit_old_lead_message_visibility" not in audit_src_m or "--self-test" not in audit_src_m or "limit=0" not in audit_src_m:
            errors.append("Gate M violation in scripts/proactive_audit.py: missing _audit_old_lead_message_visibility, --self-test, or limit=0")

    import os as _os

    for hk in ("pre-commit", "pre-push"):
        hk_path = root / "githooks" / hk
        if not hk_path.is_file() or not _os.access(hk_path, _os.X_OK):
            errors.append(f"Gate M violation: githooks/{hk} must exist and be executable")

if errors:
    for err in errors:
        print("FAIL:", err, file=sys.stderr)
    sys.exit(1)
print("Static AST & Sacred Rules invariants OK.")
PY

echo "== [2/4] Frontend JS syntax check =="
if command -v node >/dev/null 2>&1; then
  node --check app/static/app.js
  echo "app/static/app.js syntax OK."
fi

echo "== [3/4] Design, launch & CLI smoke gates =="
"$PYTHON_BIN" .claude/skills/replica-design/contrast.py replica/design/tokens.json
"$PYTHON_BIN" .claude/skills/replica-launch/listing.py replica/launch/listing.json
PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" -m leadgen.vps_autonomous_replenish --help >/dev/null
if [ -f "$ROOT_DIR/scripts/proactive_audit.py" ]; then
  PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" scripts/proactive_audit.py --help >/dev/null
fi
echo "leadgen.vps_autonomous_replenish & proactive_audit --help smoke checks OK."

if [ "${1:-}" = "--fast-checks-only" ]; then
  echo "== Fast static checks passed =="
  exit 0
fi

echo "== [4/4] Proactive audit self-test & full pytest harness (zero ignored suites) =="
PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" scripts/proactive_audit.py --self-test
PYTHONPATH=. "$PYTHON_BIN" -m pytest -q -k "not playwright"
