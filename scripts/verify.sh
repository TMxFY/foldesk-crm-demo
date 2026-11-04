#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m pytest -q
.venv/bin/python .claude/skills/replica-design/contrast.py replica/design/tokens.json
.venv/bin/python .claude/skills/replica-launch/listing.py replica/launch/listing.json
