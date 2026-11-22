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

# Gate N: Inbox, Filters & Stable Workspace Counters
if idx_path.is_file() and js_path.is_file():
    if 'data-chip="groups"' in idx_html:
        errors.append("Gate N violation in app/templates/index.html: dead filter data-chip=\"groups\" must stay removed")
    if 'id="chip-note"' not in idx_html or 'id="stat-dialogs"' not in idx_html:
        errors.append("Gate N violation in app/templates/index.html: missing #chip-note or #stat-dialogs")
    js_src_n = js_path.read_text(encoding="utf-8")
    m_chip = re.search(r"function syncChipNote\([\s\S]*?(?=\nfunction |\ndocument\.querySelector)", js_src_n)
    if not m_chip or "#chip-note" not in m_chip.group(0):
        errors.append("Gate N violation in app/static/app.js: syncChipNote() must gate #chip-note")
    m_uws = re.search(r"function updateWorkspaceStats\([\s\S]*?(?=\nfunction )", js_src_n)
    if (
        not m_uws
        or "#stat-dialogs" not in m_uws.group(0)
        or "state.workspaceTotalDialogs" not in m_uws.group(0)
        or re.search(r"dialogsNode\.textContent\s*=\s*String\(\s*\(?\s*state\.dialogs", m_uws.group(0))
    ):
        errors.append("Gate N violation in app/static/app.js: #stat-dialogs must bind to state.workspaceTotalDialogs")
    m_qform = re.search(
        r'document\.querySelector\("#search-form"\)\.addEventListener\("submit"[\s\S]*?(?=\ndocument\.querySelector\("\.chips"\))',
        js_src_n,
    )
    if not m_qform or "loadLeads(" in m_qform.group(0) or "loadInbox(" not in m_qform.group(0):
        errors.append("Gate N violation in app/static/app.js: #search-form submit must query loadInbox(), never loadLeads()")

# Gate O: Leads & Airtable Zero-Clobber / Zero-Diff-Loop Invariant
at_file = root / "app" / "airtable_sync.py"
if at_file.is_file():
    at_src = at_file.read_text(encoding="utf-8")
    at_tree = ast.parse(at_src, filename=str(at_file))
    pull_fn = next((n for n in at_tree.body if isinstance(n, ast.FunctionDef) and n.name == "pull_from_airtable"), None)
    if pull_fn is None:
        errors.append("Gate O violation in app/airtable_sync.py: missing pull_from_airtable")
    else:
        diff_lines = []
        update_lines = []
        event_lines = []
        preserve_lines = []
        for node in ast.walk(pull_fn):
            if isinstance(node, ast.Call):
                fn_id = node.func.id if isinstance(node.func, ast.Name) else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if fn_id == "_compute_effective_lead_patch":
                    diff_lines.append(node.lineno)
                elif fn_id == "update_lead":
                    update_lines.append(node.lineno)
                elif fn_id == "_should_preserve_local_lead_source":
                    preserve_lines.append(node.lineno)
                elif fn_id == "record_lead_event" and any(isinstance(a, ast.Constant) and a.value == "airtable_synced" for a in node.args):
                    event_lines.append(node.lineno)
        if not diff_lines or not preserve_lines or (update_lines and min(diff_lines) >= min(update_lines)) or (event_lines and min(diff_lines) >= min(event_lines)):
            errors.append(
                "Gate O violation in app/airtable_sync.py: pull_from_airtable must compute _compute_effective_lead_patch & _should_preserve_local_lead_source before update_lead / airtable_synced"
            )

# Gate P: Leadgen Per-Campaign Starvation & UI Health Strip
hyg_file = root / "app" / "hygiene.py"
if idx_path.is_file() and js_path.is_file() and hyg_file.is_file() and main_py_file.is_file():
    hyg_src = hyg_file.read_text(encoding="utf-8")
    if "def _audit_per_campaign_leadgen_status(" not in hyg_src or "def inspect_leadgen_run_anomalies(" not in hyg_src:
        errors.append("Gate P violation in app/hygiene.py: missing per-campaign starvation or run anomaly helpers")
    m_rd = re.search(r"def audit_server_leadgen_readiness\([\s\S]*?(?=\ndef\s)", hyg_src)
    if not m_rd or not all(k in m_rd.group(0) for k in ("campaigns", "campaigns_starved", "starvation_alert", "tg_bridged_zero_streak")):
        errors.append("Gate P violation in app/hygiene.py: audit_server_leadgen_readiness must expose per-campaign starvation telemetry")
    main_src_p = main_py_file.read_text(encoding="utf-8")
    for req_p in (
        "leadgen_campaigns",
        "campaigns_starved",
        "leadgen_starvation_alert",
        "tg_bridged_zero_streak",
        '"/api/leadgen/health-summary"',
        '"/api/leadgen/direct-x-leads"',
    ):
        if req_p not in main_src_p:
            errors.append(f"Gate P violation in app/main.py: missing {req_p}")
    if 'id="leadgen-health-strip"' not in idx_html:
        errors.append('Gate P violation in app/templates/index.html: missing id="leadgen-health-strip"')
    if "#leadgen-health-strip" not in js_path.read_text(encoding="utf-8"):
        errors.append("Gate P violation in app/static/app.js: missing #leadgen-health-strip wiring")
    if "fcntl.flock" not in repl_src:
        errors.append("Gate P violation in leadgen/vps_autonomous_replenish.py: missing fcntl.flock single-instance lock")

# Gate Q: Session Safety, Lease Guard Order & Systemd Graceful Shutdown
tg_gw_file_q = root / "app" / "telegram_gateway.py"
if main_py_file.is_file() and tg_gw_file_q.is_file():
    main_src_q = main_py_file.read_text(encoding="utf-8")
    main_tree_q = ast.parse(main_src_q, filename=str(main_py_file))
    top_fns_q = {
        n.name: n
        for n in main_tree_q.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for gw_fn_name in ("_gateway", "_resolve_app_gateway"):
        fn_node = top_fns_q.get(gw_fn_name)
        if fn_node is None:
            errors.append(f"Gate Q violation in app/main.py: missing {gw_fn_name}")
            continue
        lease_lines = [n.lineno for n in ast.walk(fn_node) if isinstance(n, ast.Constant) and n.value == "tg_lease_ready"]
        gw_attr_lines = [
            n.lineno
            for n in ast.walk(fn_node)
            if isinstance(n, ast.Attribute)
            and n.attr == "gateway"
            and isinstance(n.ctx, ast.Load)
        ]
        if not lease_lines or not gw_attr_lines or min(lease_lines) >= min(gw_attr_lines):
            errors.append(f"Gate Q violation in app/main.py:{gw_fn_name}: must check tg_lease_ready before returning app.state.gateway")

    hb_fn = top_fns_q.get("_tg_lease_heartbeat_loop")
    if hb_fn is None:
        errors.append("Gate Q violation in app/main.py: missing _tg_lease_heartbeat_loop")
    else:
        stop_watch_lines = [
            n.lineno
            for n in ast.walk(hb_fn)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == "stop_inbox_watches") or (isinstance(n.func, ast.Attribute) and n.func.attr == "stop_inbox_watches"))
        ]
        clear_gw_lines = [
            n.lineno
            for n in ast.walk(hb_fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "gateway" for t in n.targets)
            and isinstance(n.value, ast.Constant)
            and n.value.value is None
        ]
        reacq_lines = [
            n.lineno
            for n in ast.walk(hb_fn)
            if isinstance(n, ast.Name) and n.id == "wait_and_acquire_tg_runtime_lease"
        ]
        if not stop_watch_lines or not clear_gw_lines or not reacq_lines or not (min(stop_watch_lines) < min(reacq_lines) and min(clear_gw_lines) < min(reacq_lines)):
            errors.append("Gate Q violation in app/main.py:_tg_lease_heartbeat_loop: must stop_inbox_watches & clear gateway=None before wait_and_acquire_tg_runtime_lease")

    rt_fn = top_fns_q.get("should_enable_live_tg_runtime")
    if rt_fn is None:
        errors.append("Gate Q violation in app/main.py: missing should_enable_live_tg_runtime")
    else:
        for stmt in ast.walk(rt_fn):
            if isinstance(stmt, ast.If) and any(isinstance(c, ast.Constant) and c.value == "RENDER" for c in ast.walk(stmt.test)):
                if any(isinstance(r, ast.Return) and isinstance(r.value, ast.Constant) and r.value.value is True for r in stmt.body):
                    errors.append("Gate Q violation in app/main.py: RENDER env must never enable live TG runtime")

    tg_src_q = tg_gw_file_q.read_text(encoding="utf-8")
    if "_schedule_bg_disconnect" in tg_src_q:
        errors.append("Gate Q violation in app/telegram_gateway.py: _schedule_bg_disconnect must be removed")
    m_rp = re.search(r"def _resolve_proxy\([\s\S]*?(?=\n    def |\ndef )", tg_src_q)
    if m_rp and re.search(r"\bacc_id\s+or\s+session_path\b", m_rp.group(0)):
        errors.append("Gate Q violation in app/telegram_gateway.py:_resolve_proxy: forbidden 'acc_id or session_path' global fallback")

svc_crmtg_q = systemd_dir / "crmtg.service"
if svc_crmtg_q.is_file():
    svc_text_q = svc_crmtg_q.read_text(encoding="utf-8")
    if "uvicorn app.main:app" in svc_text_q:
        m_gs = re.search(r"--timeout-graceful-shutdown\s+(\d+)", svc_text_q)
        m_ts = re.search(r"^TimeoutStopSec\s*=\s*(\d+)", svc_text_q, flags=re.MULTILINE)
        if not m_gs or not m_ts or int(m_ts.group(1)) < int(m_gs.group(1)) + 10:
            errors.append("Gate Q violation in deploy/systemd/crmtg.service: TimeoutStopSec must be >= timeout-graceful-shutdown + 10")

# Gate R: Analytics & Inbox SSOT Parity and Payload Budget
if (root / "app" / "inbox.py").is_file() and (root / "app" / "analytics.py").is_file():
    for mod_name in ("inbox.py", "analytics.py"):
        msrc = (root / "app" / mod_name).read_text(encoding="utf-8")
        if "is_waiting_our_answer" not in msrc and "sql_is_waiting_our_answer_expr" not in msrc:
            errors.append(f"Gate R violation in app/{mod_name}: must import and use canonical is_waiting_our_answer / sql_is_waiting_our_answer_expr")
    air_test = root / "tests" / "test_airtable_inbox_analytics_adequacy.py"
    if not air_test.is_file() or ("60 * 1024" not in air_test.read_text(encoding="utf-8") and "61440" not in air_test.read_text(encoding="utf-8")):
        errors.append("Gate R violation: tests/test_airtable_inbox_analytics_adequacy.py must test analytics <= 60 KB payload budget")

# Gate S: Security, Deep Health Auth, Zero Hardcoded E2E Secret & Bare Except Ceiling
forbidden_secret_marker = "e2e-" + "not-a-secret"
for scan_dir in ("app", "leadgen", "tests", "e2e"):
    dpath = root / scan_dir
    if not dpath.is_dir():
        continue
    for fpath in sorted(dpath.rglob("*")):
        if not fpath.is_file() or fpath.suffix not in (".py", ".ts", ".js") or "node_modules" in fpath.parts:
            continue
        if forbidden_secret_marker in fpath.read_text(encoding="utf-8", errors="ignore"):
            errors.append(f"Gate S violation: forbidden hardcoded secret literal in {fpath.relative_to(root)}")

sec_file_s = root / "app" / "security.py"
if main_py_file.is_file():
    ms_s = main_py_file.read_text(encoding="utf-8")
    if re.search(r'\.mount\(\s*["\']/a["\']|["\']/a/["\']', ms_s):
        errors.append("Gate S violation in app/main.py: /a/ Astryx proxy mount must remain unmounted")
    if sec_file_s.is_file() and "is_strict_loopback_direct" not in ms_s:
        errors.append("Gate S violation in app/main.py: api_health deep gate must enforce is_strict_loopback_direct")

if sec_file_s.is_file():
    sec_src_s = sec_file_s.read_text(encoding="utf-8")
    if "_is_deep_health_request" not in sec_src_s or "is_strict_loopback_direct" not in sec_src_s:
        errors.append("Gate S violation in app/security.py: /api/health?deep=1 must not bypass external auth")

bare_except_ceiling = 196
bare_except_actual = 0
for scan_sub in ("app", "leadgen"):
    for py_f in sorted((root / scan_sub).rglob("*.py")):
        t_ast = ast.parse(py_f.read_text(encoding="utf-8"), filename=str(py_f))
        for nd in ast.walk(t_ast):
            if isinstance(nd, ast.ExceptHandler) and len(nd.body) == 1 and isinstance(nd.body[0], ast.Pass):
                if nd.type is None or (isinstance(nd.type, ast.Name) and nd.type.id in ("Exception", "BaseException")):
                    bare_except_actual += 1
if bare_except_actual > bare_except_ceiling:
    errors.append(
        f"Gate S violation: bare except/pass count grew from {bare_except_ceiling} to {bare_except_actual}"
    )

# Gate T: Unified Inbox v2 React Bundle, Template Host, Modern Web CSS & SW Cache Contract
if (root / "app" / "inbox.py").is_file() or idx_path.is_file() or (root / "frontend").is_dir():
    req_t_files = (
        root / "frontend" / "package.json",
        root / "frontend" / "tsconfig.json",
        root / "app" / "static" / "inbox-v2" / "inbox-v2.js",
        root / "app" / "static" / "inbox-v2" / "inbox-v2.css",
        root / "app" / "static" / "sw-inbox.js",
    )
    for req_tf in req_t_files:
        if not req_tf.is_file():
            errors.append(f"Gate T violation: missing required file {req_tf.relative_to(root)}")

    if main_py_file.is_file():
        ms_t = main_py_file.read_text(encoding="utf-8")
        for route_tok in ("/sw-inbox.js", "/api/dialogs/switch-account", "/api/inbox/unified", "group_by_lead"):
            if route_tok not in ms_t:
                errors.append(f"Gate T violation in app/main.py: missing unified inbox route/param '{route_tok}'")

    if idx_path.is_file():
        for html_tok in ('id="inbox-v2-host"', "inbox-v2.css", "inbox-v2.js", "/sw-inbox.js"):
            if html_tok not in idx_html:
                errors.append(f"Gate T violation in app/templates/index.html: missing '{html_tok}'")

    v2_css_file = root / "app" / "static" / "inbox-v2" / "inbox-v2.css"
    v2_js_file = root / "app" / "static" / "inbox-v2" / "inbox-v2.js"
    if v2_css_file.is_file():
        v2_css_src = v2_css_file.read_text(encoding="utf-8")
        if re.search(r"figtree", v2_css_src, flags=re.IGNORECASE):
            errors.append("Gate T violation in app/static/inbox-v2/inbox-v2.css: forbidden font 'Figtree'")
        if not re.search(r"@supports\s*\(\s*position-area\s*:", v2_css_src):
            errors.append("Gate T violation in app/static/inbox-v2/inbox-v2.css: missing '@supports (position-area:' anchor positioning")
        if not re.search(r"\.dialog-row-deferred[^}]*content-visibility\s*:\s*auto", v2_css_src):
            errors.append("Gate T violation in app/static/inbox-v2/inbox-v2.css: missing '.dialog-row-deferred { ... content-visibility: auto }'")
        if not re.search(r"scrollbar-color\s*:", v2_css_src):
            errors.append("Gate T violation in app/static/inbox-v2/inbox-v2.css: missing 'scrollbar-color'")

    if v2_js_file.is_file():
        v2_js_src = v2_js_file.read_text(encoding="utf-8")
        if re.search(r"figtree", v2_js_src, flags=re.IGNORECASE):
            errors.append("Gate T violation in app/static/inbox-v2/inbox-v2.js: forbidden font 'Figtree'")

    fe_src_dir = root / "frontend" / "src"
    if fe_src_dir.is_dir():
        for fe_file in sorted(fe_src_dir.rglob("*")):
            if not fe_file.is_file() or fe_file.suffix not in (".ts", ".tsx", ".css"):
                continue
            if re.search(r"figtree", fe_file.read_text(encoding="utf-8"), flags=re.IGNORECASE):
                errors.append(f"Gate T violation in {fe_file.relative_to(root)}: forbidden font 'Figtree'")

# Gate U: Global Accounts Connect / Reconnect Modal Contract
if idx_path.is_file():
    for tok_u in (
        'id="open-accounts-modal-btn"',
        '<dialog id="accounts-modal"',
        'closedby="any"',
        'id="account-reauth-banner"',
        'id="accounts-modal-status"',
    ):
        if tok_u not in idx_html:
            errors.append(f"Gate U violation in app/templates/index.html: missing '{tok_u}'")
    m_dlg_u = re.search(r"<dialog\b[^>]*id=\"accounts-modal\"[^>]*>", idx_html)
    if m_dlg_u and re.search(r"\brole=\"dialog\"|\baria-modal\b", m_dlg_u.group(0)):
        errors.append("Gate U violation in app/templates/index.html: <dialog id='accounts-modal'> must use native modal semantics without role='dialog' or aria-modal")

if idx_path.is_file() and js_path.is_file():
    js_src_u = js_path.read_text(encoding="utf-8")
    for js_tok_u in (
        "function openAccountsModal(",
        "function openReauthForAccount(",
        "openAccountsModal(opts)",
        "if (select && !isInboxV2Active())",
        "function resetAccountAuthFormState(",
    ):
        if js_tok_u not in js_src_u:
            errors.append(f"Gate U violation in app/static/app.js: missing '{js_tok_u}'")

css_path_u = root / "app" / "static" / "app.css"
if css_path_u.is_file():
    css_src_u = css_path_u.read_text(encoding="utf-8")
    if not re.search(r"dialog\.accounts-modal:modal\s*\{[^}]*overscroll-behavior:\s*contain", css_src_u):
        errors.append("Gate U violation in app/static/app.css: missing overscroll-behavior: contain on dialog.accounts-modal:modal")
    if not re.search(r"dialog\.accounts-modal::backdrop\s*\{[^}]*overscroll-behavior:\s*contain", css_src_u):
        errors.append("Gate U violation in app/static/app.css: missing overscroll-behavior: contain on dialog.accounts-modal::backdrop")

fe_dlg_pane = root / "frontend" / "src" / "components" / "DialogListPane.tsx"
if fe_dlg_pane.is_file():
    if 'id="inbox-connect-account-btn"' not in fe_dlg_pane.read_text(encoding="utf-8"):
        errors.append("Gate U violation in frontend/src/components/DialogListPane.tsx: missing id='inbox-connect-account-btn'")

fe_thread_pane = root / "frontend" / "src" / "components" / "ThreadPane.tsx"
if fe_thread_pane.is_file():
    if 'id="inbox-picker-manage-accounts-btn"' not in fe_thread_pane.read_text(encoding="utf-8"):
        errors.append("Gate U violation in frontend/src/components/ThreadPane.tsx: missing id='inbox-picker-manage-accounts-btn'")

fe_bundle_js = root / "app" / "static" / "inbox-v2" / "inbox-v2.js"
if fe_bundle_js.is_file() and fe_dlg_pane.is_file():
    bundle_txt = fe_bundle_js.read_text(encoding="utf-8")
    for b_tok in ("inbox-connect-account-btn", "inbox-picker-manage-accounts-btn"):
        if b_tok not in bundle_txt:
            errors.append(f"Gate U violation in app/static/inbox-v2/inbox-v2.js: stale bundle missing '{b_tok}' (run npm run build --prefix frontend)")

acc_py_u = root / "app" / "accounts.py"
if acc_py_u.is_file() and fe_thread_pane.is_file():
    acc_src_u = acc_py_u.read_text(encoding="utf-8")
    m_apply = re.search(r"def _apply_result\b[\s\S]{0,900}", acc_src_u)
    if m_apply and '"offline"' not in m_apply.group(0):
        errors.append("Gate U violation in app/accounts.py: _apply_result() must include 'offline' in replaceable health statuses")

# Gate V: Outreach v2 Spec-9 Static Regression Gates
# V1. Ban roblox / fortnite / fivem inside leadgen/meta_ad_leads.py SEARCH_QUERIES
#     and leadgen/tg_llm_qualify.py Golden ICP clusters (while allowing them in REJECT sections)
meta_ads_v_file = root / "leadgen" / "meta_ad_leads.py"
if meta_ads_v_file.is_file():
    ma_tree = ast.parse(meta_ads_v_file.read_text(encoding="utf-8"), filename=str(meta_ads_v_file))
    sq_val = None
    for nd in ma_tree.body:
        if isinstance(nd, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SEARCH_QUERIES" for t in nd.targets):
            sq_val = nd.value
            break
        if isinstance(nd, ast.AnnAssign) and isinstance(nd.target, ast.Name) and nd.target.id == "SEARCH_QUERIES":
            sq_val = nd.value
            break
    if sq_val is None:
        errors.append("Gate V violation in leadgen/meta_ad_leads.py: missing SEARCH_QUERIES literal")
    else:
        sq_strings = [
            sub.value
            for sub in ast.walk(sq_val)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        ]
        if not sq_strings:
            errors.append("Gate V violation in leadgen/meta_ad_leads.py: empty SEARCH_QUERIES")
        for q_str in sq_strings:
            q_low = q_str.lower()
            for banned_tok in ("roblox", "fortnite", "fivem"):
                if banned_tok in q_low:
                    errors.append(
                        f"Gate V violation in leadgen/meta_ad_leads.py: banned gaming token '{banned_tok}' in SEARCH_QUERIES ({q_str!r})"
                    )

tg_qual_v_file = root / "leadgen" / "tg_llm_qualify.py"
if tg_qual_v_file.is_file():
    tq_tree = ast.parse(tg_qual_v_file.read_text(encoding="utf-8"), filename=str(tg_qual_v_file))
    sp_val = None
    for nd in tq_tree.body:
        if isinstance(nd, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SYSTEM_PROMPT" for t in nd.targets):
            sp_val = nd.value
            break
        if isinstance(nd, ast.AnnAssign) and isinstance(nd.target, ast.Name) and nd.target.id == "SYSTEM_PROMPT":
            sp_val = nd.value
            break
    if isinstance(sp_val, ast.Constant) and isinstance(sp_val.value, str):
        sp_text = sp_val.value
        if "Golden ICP clusters" not in sp_text or "REJECT" not in sp_text:
            errors.append(
                "Gate V violation in leadgen/tg_llm_qualify.py: SYSTEM_PROMPT must define 'Golden ICP clusters' and 'REJECT' sections"
            )
        else:
            golden_section = sp_text.split("REJECT", 1)[0]
            golden_low = golden_section.lower()
            for banned_tok in ("roblox", "fortnite", "fivem"):
                if banned_tok in golden_low:
                    errors.append(
                        f"Gate V violation in leadgen/tg_llm_qualify.py: banned gaming token '{banned_tok}' in Golden ICP clusters section"
                    )
    else:
        errors.append("Gate V violation in leadgen/tg_llm_qualify.py: missing string SYSTEM_PROMPT")

# V2. Ban 'or has_x or has_personal_email' inside leadgen/contact_waterfall.py Tier 2 trigger
cw_v_file = root / "leadgen" / "contact_waterfall.py"
if cw_v_file.is_file():
    cw_src_v = cw_v_file.read_text(encoding="utf-8")
    if re.search(r"\bor\s+has_x\b|\bhas_x\s+or\b|not\s*\(\s*has_tg\s+or\s+has_wa\s+or\s+[^\n)]*(?:has_x|has_personal_email)", cw_src_v):
        errors.append(
            "Gate V violation in leadgen/contact_waterfall.py: Tier 2 trigger must not include has_x or has_personal_email"
        )
    cw_tree = ast.parse(cw_src_v, filename=str(cw_v_file))
    enrich_fn = next(
        (n for n in cw_tree.body if isinstance(n, ast.FunctionDef) and n.name == "enrich_lead_contacts"),
        None,
    )
    if enrich_fn is None:
        errors.append("Gate V violation in leadgen/contact_waterfall.py: missing enrich_lead_contacts")
    else:
        tier2_if_nodes = [
            nd
            for nd in ast.walk(enrich_fn)
            if isinstance(nd, ast.If)
            and any(isinstance(sub, ast.Name) and sub.id == "allow_paid_tier2" for sub in ast.walk(nd.test))
        ]
        if not tier2_if_nodes:
            errors.append("Gate V violation in leadgen/contact_waterfall.py: missing allow_paid_tier2 Tier 2 trigger guard")
        else:
            for t_if in tier2_if_nodes:
                test_names = {sub.id for sub in ast.walk(t_if.test) if isinstance(sub, ast.Name)}
                if "has_x" in test_names or "has_personal_email" in test_names:
                    errors.append(
                        f"Gate V violation in leadgen/contact_waterfall.py:{t_if.lineno}: Tier 2 trigger condition must not block on has_x / has_personal_email"
                    )
                if "has_tg" not in test_names or "has_wa" not in test_names:
                    errors.append(
                        f"Gate V violation in leadgen/contact_waterfall.py:{t_if.lineno}: Tier 2 trigger condition must gate on has_tg and has_wa"
                    )

# V3. Verify exclude_from_airtable and ru_expert isolation guards exist in app/airtable_sync.py
if at_file.is_file():
    at_src_v = at_file.read_text(encoding="utf-8")
    for req_at_tok in ("def is_lead_airtable_excluded(", "exclude_from_airtable", "ru_expert"):
        if req_at_tok not in at_src_v:
            errors.append(f"Gate V violation in app/airtable_sync.py: missing Airtable isolation guard '{req_at_tok}'")
    at_tree_v = ast.parse(at_src_v, filename=str(at_file))
    at_fns_v = {n.name: n for n in at_tree_v.body if isinstance(n, ast.FunctionDef)}
    for req_fn_v in ("_select_airtable_push_candidates", "push_lead_to_airtable", "sync_airtable"):
        if req_fn_v not in at_fns_v:
            errors.append(f"Gate V violation in app/airtable_sync.py: missing {req_fn_v}")
    cand_fn = at_fns_v.get("_select_airtable_push_candidates")
    if cand_fn is not None:
        cand_calls = {
            (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
            for n in ast.walk(cand_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
        }
        if "_sql_lead_not_excluded_clause" not in cand_calls or "_is_db_lead_airtable_excluded" not in cand_calls:
            errors.append(
                "Gate V violation in app/airtable_sync.py: _select_airtable_push_candidates must call _sql_lead_not_excluded_clause and _is_db_lead_airtable_excluded"
            )

# V4. Verify REVSHARE_OFFER_TEXT is NOT used in whale_mass Step 1 (leadgen/outreach_copy.py)
oc_v_file = root / "leadgen" / "outreach_copy.py"
if oc_v_file.is_file():
    oc_src_v = oc_v_file.read_text(encoding="utf-8")
    oc_tree = ast.parse(oc_src_v, filename=str(oc_v_file))
    oc_fns = {n.name: n for n in oc_tree.body if isinstance(n, ast.FunctionDef)}
    whale_steps_fn = oc_fns.get("_build_whale_mass_steps")
    if whale_steps_fn is None:
        errors.append("Gate V violation in leadgen/outreach_copy.py: missing _build_whale_mass_steps")
    else:
        used_names = {n.id for n in ast.walk(whale_steps_fn) if isinstance(n, ast.Name)}
        if "REVSHARE_OFFER_TEXT" in used_names:
            errors.append("Gate V violation in leadgen/outreach_copy.py: _build_whale_mass_steps must NOT reference REVSHARE_OFFER_TEXT")
        str_lits = " ".join(n.value.lower() for n in ast.walk(whale_steps_fn) if isinstance(n, ast.Constant) and isinstance(n.value, str))
        if "revshare" in str_lits or "cpa" in str_lits:
            errors.append("Gate V violation in leadgen/outreach_copy.py: _build_whale_mass_steps must NOT contain revshare/CPA copy")
    for mod_assign in oc_tree.body:
        if isinstance(mod_assign, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("WHALE_OFFER_TEXT", "WHALE_PITCH_TEXT") for t in mod_assign.targets):
            lit_txt = " ".join(n.value.lower() for n in ast.walk(mod_assign.value) if isinstance(n, ast.Constant) and isinstance(n.value, str))
            if "revshare" in lit_txt or "cpa" in lit_txt:
                errors.append("Gate V violation in leadgen/outreach_copy.py: WHALE_OFFER_TEXT / WHALE_PITCH_TEXT must NOT mention revshare/CPA")
    for fn_nm in ("_default_steps_for_profile", "upgrade_to_vlad_step1", "_validate_and_assemble_llm_step1"):
        fn_nd = oc_fns.get(fn_nm)
        if fn_nd is not None:
            for if_nd in ast.walk(fn_nd):
                if (
                    isinstance(if_nd, ast.If)
                    and isinstance(if_nd.test, ast.Compare)
                    and len(if_nd.test.ops) == 1
                    and isinstance(if_nd.test.ops[0], ast.Eq)
                    and any(
                        isinstance(c, ast.Constant) and c.value == "whale_mass"
                        for c in (if_nd.test.left, *if_nd.test.comparators)
                    )
                ):
                    for body_stmt in if_nd.body:
                        if any(isinstance(nm, ast.Name) and nm.id == "REVSHARE_OFFER_TEXT" for nm in ast.walk(body_stmt)):
                            errors.append(
                                f"Gate V violation in leadgen/outreach_copy.py:{fn_nm}: whale_mass branch must NOT use REVSHARE_OFFER_TEXT"
                            )

# V5. Verify frozen-account no-spillover guard & language match guard exist in app/outreach.py and leadgen/language_router.py
lr_v_file = root / "leadgen" / "language_router.py"
if lr_v_file.is_file():
    lr_src_v = lr_v_file.read_text(encoding="utf-8")
    for req_lr in ("def assert_copy_matches_language(", "def detect_lead_language(", "def resolve_target_campaign("):
        if req_lr not in lr_src_v:
            errors.append(f"Gate V violation in leadgen/language_router.py: missing {req_lr}")

if outreach_file.is_file():
    outreach_src_v = outreach_file.read_text(encoding="utf-8")
    for req_outreach_guard in (
        "def is_account_eligible_for_outreach(",
        "def assert_campaign_copy_language(",
        "def get_account_global_sent_today(",
        "BLOCKED_OUTREACH_HEALTHS",
        "assert_copy_matches_language",
    ):
        if req_outreach_guard not in outreach_src_v:
            errors.append(
                f"Gate V violation in app/outreach.py: missing v2 guard '{req_outreach_guard}'"
            )
    if '"frozen"' not in outreach_src_v:
        errors.append("Gate V violation in app/outreach.py: missing 'frozen' account isolation guard")
    if outreach_src_v.count("assert_campaign_copy_language(") < 2:
        errors.append("Gate V violation in app/outreach.py: assert_campaign_copy_language must be wired into campaign ingestion/sending")

    outreach_tree_v = ast.parse(outreach_src_v, filename=str(outreach_file))
    outreach_fns_v = {
        nd.name: nd
        for nd in ast.walk(outreach_tree_v)
        if isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def _fn_calls_v(fn_node: ast.AST) -> list[ast.Call]:
        return [c for c in ast.walk(fn_node) if isinstance(c, ast.Call)]

    def _call_name_v(call_node: ast.Call) -> str:
        if isinstance(call_node.func, ast.Name):
            return call_node.func.id
        if isinstance(call_node.func, ast.Attribute):
            return call_node.func.attr
        return ""

    def _has_call_v(fn_node: ast.AST, target: str, req_kw: str | None = None) -> bool:
        for c in _fn_calls_v(fn_node):
            if _call_name_v(c) == target:
                if req_kw is None or any(kw.arg == req_kw for kw in c.keywords):
                    return True
        return False

    v5_outreach_reqs = (
        ("_validate_template_text_language", "assert_campaign_copy_language", None),
        ("_normalize_steps", "_validate_template_text_language", None),
        ("create_campaign", "_normalize_steps", "expected_language"),
        ("create_campaign", "_normalize_custom_step_templates_json", "expected_language"),
        ("update_campaign", "_normalize_steps", "expected_language"),
        ("update_campaign", "_validate_template_text_language", None),
        ("_validate_campaign_steps_for_preview", "_validate_template_text_language", None),
        ("run_campaign_batch", "is_account_eligible_for_outreach", None),
        ("run_campaign_batch", "get_account_global_sent_today", None),
    )
    for fn_name, req_callee, req_kw in v5_outreach_reqs:
        fn_nd = outreach_fns_v.get(fn_name)
        if fn_nd is not None and not _has_call_v(fn_nd, req_callee, req_kw=req_kw):
            kw_desc = f"(..., {req_kw}=...)" if req_kw else "()"
            errors.append(
                f"Gate V violation in app/outreach.py:{fn_name}: must call {req_callee}{kw_desc}"
            )

main_v_file = root / "app" / "main.py"
if main_v_file.is_file():
    main_tree_v = ast.parse(main_v_file.read_text(encoding="utf-8"), filename=str(main_v_file))
    main_fns_v = {
        nd.name: nd
        for nd in ast.walk(main_tree_v)
        if isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    sct_fn = main_fns_v.get("_save_campaign_templates")
    if sct_fn is not None:
        sct_calls = {
            (c.func.id if isinstance(c.func, ast.Name) else c.func.attr if isinstance(c.func, ast.Attribute) else "")
            for c in ast.walk(sct_fn)
            if isinstance(c, ast.Call)
        }
        if "_validate_template_text_language" not in sct_calls:
            errors.append(
                "Gate V violation in app/main.py:_save_campaign_templates: must call _validate_template_text_language"
            )

pmr_v_file = root / "leadgen" / "phone_messenger_resolver.py"
if pmr_v_file.is_file():
    pmr_tree_v = ast.parse(pmr_v_file.read_text(encoding="utf-8"), filename=str(pmr_v_file))
    pmr_fn = next(
        (
            nd for nd in ast.walk(pmr_tree_v)
            if isinstance(nd, (ast.FunctionDef, ast.AsyncFunctionDef)) and nd.name == "resolve_phones_telegram"
        ),
        None,
    )
    if pmr_fn is None:
        errors.append("Gate V violation in leadgen/phone_messenger_resolver.py: missing resolve_phones_telegram")
    else:
        kw_defaults = dict(zip([a.arg for a in pmr_fn.args.kwonlyargs], pmr_fn.args.kw_defaults))
        pos_defaults = dict(
            zip(
                [a.arg for a in pmr_fn.args.args[-len(pmr_fn.args.defaults):]],
                pmr_fn.args.defaults,
            )
        ) if pmr_fn.args.defaults else {}
        chk_def = kw_defaults.get("is_checker_session") or pos_defaults.get("is_checker_session")
        if not (isinstance(chk_def, ast.Constant) and chk_def.value is False):
            errors.append(
                "Gate V violation in leadgen/phone_messenger_resolver.py:resolve_phones_telegram must default is_checker_session=False"
            )

bg_v_file = root / "leadgen" / "budget_guard.py"
if bg_v_file.is_file():
    bg_tree_v = ast.parse(bg_v_file.read_text(encoding="utf-8"), filename=str(bg_v_file))
    bg_fn = next(
        (
            nd for nd in ast.walk(bg_tree_v)
            if isinstance(nd, ast.FunctionDef) and nd.name == "check_balances_and_alert"
        ),
        None,
    )
    if bg_fn is None:
        errors.append("Gate V violation in leadgen/budget_guard.py: missing check_balances_and_alert")
    else:
        kw_defaults = dict(zip([a.arg for a in bg_fn.args.kwonlyargs], bg_fn.args.kw_defaults))
        pos_defaults = dict(
            zip(
                [a.arg for a in bg_fn.args.args[-len(bg_fn.args.defaults):]],
                bg_fn.args.defaults,
            )
        ) if bg_fn.args.defaults else {}
        allow_def = kw_defaults.get("allow_unavailable") or pos_defaults.get("allow_unavailable")
        if not (isinstance(allow_def, ast.Constant) and allow_def.value is False):
            errors.append(
                "Gate V violation in leadgen/budget_guard.py:check_balances_and_alert must default allow_unavailable=False"
            )

bridge_v_file = root / "leadgen" / "tg_identity_bridge.py"
if bridge_v_file.is_file():
    bridge_src_v = bridge_v_file.read_text(encoding="utf-8")
    bridge_tree_v = ast.parse(bridge_src_v, filename=str(bridge_v_file))
    bridge_fns = {nd.name: nd for nd in ast.walk(bridge_tree_v) if isinstance(nd, ast.FunctionDef)}
    if bridge_fns or (root / "app" / "outreach.py").is_file():
        for b_fn_name in ("bridge_lead_to_telegram", "enrich_rows_with_telegram_bridge", "_default_x_profile", "_default_search"):
            b_fn = bridge_fns.get(b_fn_name)
            if b_fn is None:
                errors.append(f"Gate V violation in leadgen/tg_identity_bridge.py: missing {b_fn_name}")
            else:
                b_arg_names = {a.arg for a in (*b_fn.args.args, *b_fn.args.kwonlyargs)}
                if "allow_paid" not in b_arg_names or "budget_guard_fn" not in b_arg_names:
                    errors.append(
                        f"Gate V violation in leadgen/tg_identity_bridge.py:{b_fn_name}: must accept allow_paid and budget_guard_fn"
                    )
                if b_fn_name in ("_default_x_profile", "_default_search"):
                    b_calls = {
                        (c.func.id if isinstance(c.func, ast.Name) else c.func.attr if isinstance(c.func, ast.Attribute) else "")
                        for c in ast.walk(b_fn)
                        if isinstance(c, ast.Call)
                    }
                    if "_check_bridge_budget_guard" not in b_calls:
                        errors.append(
                            f"Gate V violation in leadgen/tg_identity_bridge.py:{b_fn_name}: must enforce _check_bridge_budget_guard"
                        )

if cw_v_file.is_file():
    cw_tree_v = ast.parse(cw_v_file.read_text(encoding="utf-8"), filename=str(cw_v_file))
    cw_elc = next(
        (nd for nd in ast.walk(cw_tree_v) if isinstance(nd, ast.FunctionDef) and nd.name == "enrich_lead_contacts"),
        None,
    )
    if cw_elc is not None:
        cw_args = {a.arg for a in (*cw_elc.args.args, *cw_elc.args.kwonlyargs)}
        if "allow_paid_tier2" not in cw_args or "budget_guard_fn" not in cw_args:
            errors.append(
                "Gate V violation in leadgen/contact_waterfall.py:enrich_lead_contacts must accept allow_paid_tier2 and budget_guard_fn"
            )
        bridge_calls_in_cw = [
            c for c in ast.walk(cw_elc)
            if isinstance(c, ast.Call)
            and ((isinstance(c.func, ast.Name) and c.func.id == "bridge_lead_to_telegram")
                 or (isinstance(c.func, ast.Attribute) and c.func.attr == "bridge_lead_to_telegram"))
        ]
        if bridge_calls_in_cw and not all(
            any(kw.arg == "allow_paid" for kw in c.keywords) and any(kw.arg == "budget_guard_fn" for kw in c.keywords)
            for c in bridge_calls_in_cw
        ):
            errors.append(
                "Gate V violation in leadgen/contact_waterfall.py:enrich_lead_contacts must pass allow_paid and budget_guard_fn to bridge_lead_to_telegram"
            )

hygiene_v_file = root / "app" / "hygiene.py"
if hygiene_v_file.is_file():
    hyg_src_v = hygiene_v_file.read_text(encoding="utf-8")
    hyg_tree_v = ast.parse(hyg_src_v, filename=str(hygiene_v_file))
    hyg_fn = next(
        (nd for nd in ast.walk(hyg_tree_v) if isinstance(nd, ast.FunctionDef) and nd.name == "_collect_hygiene_findings"),
        None,
    )
    if hyg_fn is not None or "_CONTACTLESS_EXEMPT_SEGMENTS" in hyg_src_v or (root / "app" / "outreach.py").is_file():
        if "_CONTACTLESS_EXEMPT_SEGMENTS" not in hyg_src_v or '"social_manual"' not in hyg_src_v or '"vacancy_assisted"' not in hyg_src_v:
            errors.append(
                "Gate V violation in app/hygiene.py: must define _CONTACTLESS_EXEMPT_SEGMENTS with 'social_manual' and 'vacancy_assisted'"
            )
        if hyg_fn is not None:
            hyg_names = {nm.id for nm in ast.walk(hyg_fn) if isinstance(nm, ast.Name)}
            if "_CONTACTLESS_EXEMPT_SEGMENTS" not in hyg_names:
                errors.append(
                    "Gate V violation in app/hygiene.py:_collect_hygiene_findings must use _CONTACTLESS_EXEMPT_SEGMENTS"
                )

if errors:
    for err in errors:
        print("FAIL:", err, file=sys.stderr)
    sys.exit(1)
print("Static AST & Sacred Rules invariants OK.")
PY

echo "== [2/4] Frontend JS syntax & TypeScript check =="
if command -v node >/dev/null 2>&1; then
  node --check app/static/app.js
  if [ -f "app/static/sw-inbox.js" ]; then
    node --check app/static/sw-inbox.js
  fi
  if [ -f "app/static/inbox-v2/inbox-v2.js" ]; then
    node --check app/static/inbox-v2/inbox-v2.js
  fi
  echo "Frontend JS syntax OK."
  if [ -d "$ROOT_DIR/frontend/node_modules" ] && command -v npx >/dev/null 2>&1; then
    npx --prefix "$ROOT_DIR/frontend" tsc -p "$ROOT_DIR/frontend/tsconfig.json" --noEmit
    echo "frontend TypeScript check OK."
  fi
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
PYTHONPATH=. "$PYTHON_BIN" -m pytest -q
