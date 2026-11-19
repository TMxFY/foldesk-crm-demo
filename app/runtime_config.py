"""Explicit operational runtime flags for CRM Telegram (zero test-runner split-brain)."""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    return default


def is_e2e_sandbox() -> bool:
    """Return True when running against an explicit E2E sandbox environment."""
    return _env_flag("CRMTG_E2E_SANDBOX", False)


def is_vps_runtime() -> bool:
    """Return True when explicitly running as the canonical Contabo VPS service."""
    return _env_flag("CRMTG_VPS_RUNTIME", False)


def should_skip_dotenv() -> bool:
    """Return True when local .env auto-loading and fallback TG API keys are disabled."""
    if is_e2e_sandbox():
        return True
    return _env_flag("CRMTG_SKIP_DOTENV", False)


def is_ephemeral_runtime() -> bool:
    """Return True for isolated ephemeral/staging/CI runs (never True on CRMTG_VPS_RUNTIME=1)."""
    if is_vps_runtime():
        return False
    if is_e2e_sandbox():
        return True
    return _env_flag("CRMTG_EPHEMERAL_RUNTIME", False)


def should_start_airtable_bg_sync() -> bool:
    """Return True when background Airtable sync threads/loops should run."""
    raw = os.getenv("AIRTABLE_BG_SYNC")
    if raw is not None and raw.strip() != "":
        return _env_flag("AIRTABLE_BG_SYNC", False)
    return not is_ephemeral_runtime()


def should_allow_auto_sonnet() -> bool:
    """Return True when automatic Sonnet copy generation on MCP B2B ingest is enabled."""
    raw = os.getenv("CRMTG_AUTO_SONNET")
    if raw is not None and raw.strip() != "":
        return _env_flag("CRMTG_AUTO_SONNET", False)
    if _env_flag("CRMTG_FORCE_AUTO_SONNET", False):
        return True
    return not is_ephemeral_runtime()
