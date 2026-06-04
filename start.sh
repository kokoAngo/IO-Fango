#!/usr/bin/env bash
# Launch the Fango.city web server with the .env file loaded.
#
# Works the same on a dev machine and on the server — it resolves its own
# directory, sources .env, and runs uvicorn. Use it directly (`./start.sh`)
# or as the ExecStart of the systemd unit in deploy/fango.service.
#
# SINGLE PROCESS IS REQUIRED. The app keeps SSE subscribers, consult sessions,
# and the HOMES browser-lookup semaphore in process memory; running multiple
# uvicorn workers would fragment that state and break live updates / rate
# limits. Do NOT add --workers.
set -euo pipefail

# Repo root = the directory this script lives in, so `./start.sh` works from
# anywhere (cron, systemd, a different cwd).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Load .env into the environment so the app (and the bind host/port below) see
# every key — NOTION_TOKEN, GEMINI_API_KEY, FANGO_AGENT_KEY, FANGO_GA_ID, …
# The app does NOT auto-load .env, so this step is what actually wires it up.
if [[ -f "$ROOT/.env" ]]; then
  set -a            # export everything defined while sourcing
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
else
  echo "start.sh: WARNING — no .env found at $ROOT/.env (secrets/GA will be missing)" >&2
fi

PYTHON="${FANGO_PYTHON:-$ROOT/.venv/bin/python}"
HOST="${FANGO_HOST:-127.0.0.1}"   # 127.0.0.1 suits ngrok/reverse-proxy on the same box; set 0.0.0.0 for direct access
PORT="${FANGO_PORT:-8000}"

echo "start.sh: serving fango.http_app:app on ${HOST}:${PORT} (GA=${FANGO_GA_ID:-<off>})" >&2
exec "$PYTHON" -m uvicorn fango.http_app:app --host "$HOST" --port "$PORT"
