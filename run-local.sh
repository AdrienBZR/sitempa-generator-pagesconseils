#!/usr/bin/env bash
# Local dev runner. Loads secrets from .env.local (gitignored) then runs main.py.
#
#   ./run-local.sh --dump          # write sitemap.xml and exit (inspect result)
#   ./run-local.sh --dump out.xml  # write to a custom path
#   ./run-local.sh                 # start the FastAPI server on :8000
#
# First time:
#   cp .env.local.example .env.local   # then paste your Coolify base64 value
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
else
  echo "WARN: no .env.local found - copy .env.local.example and fill it in." >&2
fi

if [ ! -x .venv/bin/python ]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

exec ./.venv/bin/python main.py "$@"
