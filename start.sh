#!/usr/bin/env bash
# One command to start. First run also sets up the venv + installs deps.
set -e
cd "$(dirname "$0")"

# Pick a Python >= 3.10.
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
done
[ -n "$PY" ] || { echo "Need Python 3.10+ installed."; exit 1; }

if [ ! -d .venv ]; then
  echo "· first-time setup: creating venv + installing deps (~30s)…"
  "$PY" -m venv .venv
  .venv/bin/pip install -q -U pip
  .venv/bin/pip install -q -r requirements.txt
fi
[ -f .env ] || cp .env.example .env   # you still need to put ANTHROPIC_API_KEY in .env

exec .venv/bin/python chat.py
