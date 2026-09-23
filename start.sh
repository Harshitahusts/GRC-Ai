#!/usr/bin/env bash
# Start the GRC agent web app on this machine (macOS / Linux).
# First run: creates .venv, installs the app, and asks you to create an account.
# Extra arguments go to `grc-web serve`, e.g. ./start.sh --port 9000
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is required (found $("$PYTHON" --version))." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment in .venv ..."
  "$PYTHON" -m venv .venv
fi

# Reinstall only when the dependencies change.
if ! cmp -s pyproject.toml .venv/.installed-pyproject.toml; then
  echo "Installing the app (first run takes a minute) ..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e .
  cp pyproject.toml .venv/.installed-pyproject.toml
fi

[ -f .env ] || cp .env.example .env

.venv/bin/grc-web init
exec .venv/bin/grc-web serve --open "$@"
