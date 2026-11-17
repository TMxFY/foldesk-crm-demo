#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

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
if (root / "app" / "digest.py").is_file():
    digest_src = (root / "app" / "digest.py").read_text(encoding="utf-8")
    if "is_ineligible_reply_notify_peer" not in digest_src or "extract_bot_user_id_from_token" not in digest_src:
        errors.append("Reply-Alert Gate violation: missing is_ineligible_reply_notify_peer / extract_bot_user_id_from_token guard in app/digest.py")
if (root / "app" / "telegram_gateway.py").is_file():
    tg_gw_src = (root / "app" / "telegram_gateway.py").read_text(encoding="utf-8")
    if 'getattr(peer, "bot", False)' not in tg_gw_src or 'getattr(entity, "bot", False)' not in tg_gw_src:
        errors.append("Reply-Alert Gate violation in app/telegram_gateway.py: missing bot peer filter in _private_payload / _load_private")
if (root / "app" / "inbox.py").is_file() and (
    not (root / "scripts" / "proactive_audit.py").is_file()
    or not (root / "docs" / "VERIFICATION_PLAYBOOKS.md").is_file()
):
    errors.append("Harness Gate violation: missing scripts/proactive_audit.py or docs/VERIFICATION_PLAYBOOKS.md")

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

echo "== [4/4] Full pytest harness (zero ignored suites) =="
PYTHONPATH=. "$PYTHON_BIN" -m pytest -q -k "not playwright"
