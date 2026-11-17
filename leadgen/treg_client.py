#!/usr/bin/env python3
"""Small treg CLI client for the lead pipelines.

Calls go through the logged-in `treg` binary, so the team token stays in
~/.treg and never enters this repo. A missing CLI or a failed call returns
None; the pipeline still writes the lead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def treg_bin() -> str | None:
    found = shutil.which("treg")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/treg"),
        "/usr/local/bin/treg",
        "/opt/crmtg/.venv/bin/treg",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def check_treg_ready() -> bool:
    """Return True iff treg CLI is installed, enabled, and backed by credentials."""
    if os.getenv("TREG_ENRICH", "1").lower() in {"0", "false", "no"}:
        return False
    if not treg_bin():
        return False
    if (os.environ.get("TREG_API_KEY") or "").strip():
        return True
    cfg_raw = (os.environ.get("TREG_CONFIG_PATH") or "").strip()
    candidates = (
        [os.path.expanduser(cfg_raw)]
        if cfg_raw
        else [os.path.expanduser("~/.treg/config.json"), "/root/.treg/config.json"]
    )
    for cfg_path in candidates:
        if not os.path.isfile(cfg_path):
            continue
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError:
            continue
        if not content:
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if any(bool(str(v or "").strip()) for v in parsed.values()):
                    return True
            elif bool(parsed):
                return True
        except json.JSONDecodeError:
            return True
    return False


def call(
    endpoint: str,
    *,
    method: str | None = None,
    data: dict | list | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> dict[str, Any] | None:
    binary = treg_bin()
    if not binary or os.getenv("TREG_ENRICH", "1").lower() in {"0", "false", "no"}:
        return None
    cmd = [binary, "call", endpoint]
    if method:
        cmd += ["--method", method]
    for key, value in (query or {}).items():
        cmd += ["--query", f"{key}={value}"]
    for key, value in (headers or {}).items():
        cmd += ["--header", f"{key}: {value}"]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _parse_stdout(proc.stdout)


def call_status(
    endpoint: str,
    *,
    method: str | None = None,
    data: dict | list | None = None,
    query: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[int | None, dict[str, Any] | None]:
    """`treg --json call`. Returns (http_status, body). Missing CLI is (None, None).

    A 4xx/5xx keeps the status and drops the body. Stderr is not returned.
    """
    binary = treg_bin()
    if not binary or os.getenv("TREG_ENRICH", "1").lower() in {"0", "false", "no"}:
        return None, None
    cmd = [binary, "--json", "call", endpoint]
    if method:
        cmd += ["--method", method]
    for key, value in (query or {}).items():
        cmd += ["--query", f"{key}={value}"]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    parsed = _parse_stdout(proc.stdout)
    if not isinstance(parsed, dict):
        return None, None
    meta = parsed.get("_treg")
    if isinstance(meta, dict) and meta.get("http_status") is not None:
        try:
            status = int(meta["http_status"])
        except (TypeError, ValueError):
            return None, None
        result = parsed.get("result")
        body = result if isinstance(result, dict) else None
        if status >= 400:
            return status, None
        return status, body
    if proc.returncode == 0:
        return 200, parsed
    return None, None


def _parse_stdout(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def walk_strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif isinstance(value, str):
        yield value
