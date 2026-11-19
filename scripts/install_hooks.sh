#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chmod +x "$ROOT/githooks/pre-commit" "$ROOT/githooks/pre-push"
git -C "$ROOT" config core.hooksPath githooks
echo "Git hooks enabled (core.hooksPath=githooks)"
