"""Global pytest configuration setting explicit operational runtime flags."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import pytest

_WORKTREE_ROOT = str(Path(__file__).resolve().parents[1])
_TESTS_ROOT = str(Path(__file__).resolve().parent)
for _p in (_TESTS_ROOT, _WORKTREE_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("CRMTG_SKIP_DOTENV", "1")
    os.environ.setdefault("CRMTG_EPHEMERAL_RUNTIME", "1")
    os.environ.setdefault("AIRTABLE_BG_SYNC", "0")


@pytest.fixture(autouse=True)
def _crmtg_ephemeral_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRMTG_SKIP_DOTENV", "1")
    monkeypatch.setenv("CRMTG_EPHEMERAL_RUNTIME", "1")
    monkeypatch.setenv("AIRTABLE_BG_SYNC", "0")

    orig_delenv = monkeypatch.delenv

    def _compat_delenv(name: str, raising: bool = True) -> None:
        orig_delenv(name, raising=raising)
        if name == "PYTEST_CURRENT_TEST":
            orig_delenv("CRMTG_EPHEMERAL_RUNTIME", raising=False)

    monkeypatch.delenv = _compat_delenv  # type: ignore[method-assign]
