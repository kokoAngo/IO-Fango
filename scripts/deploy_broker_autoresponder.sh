#!/usr/bin/env bash
# Deploy the broker autoresponder as a systemd service on the Fango server.
#
# Auto-detects the venv python / service user / working dir / port from the
# existing `fangoio` unit, prompts for the broker key (hidden input; stored in a
# 0600 broker.env, never in git or argv), runs a non-destructive dry-run to
# validate, then installs + enables + starts fango-broker.service.
#
# Usage (on the server, after `git pull`):
#     bash scripts/deploy_broker_autoresponder.sh
#
# Options via env:
#     FANGO_BROKER_KEY=...   skip the prompt (e.g. for automation)
#     FANGO_MCP_URL=...      override the endpoint (default: local port, else fango.city)
#     INTERVAL=120           seconds between polls
#
# Re-runnable: overwrites the env file + unit and restarts the service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INTERVAL="${INTERVAL:-120}"
UNIT_PATH="/etc/systemd/system/fango-broker.service"
ENV_FILE="$REPO_DIR/broker.env"

echo "==> Fango broker autoresponder deploy"
echo "    repo: $REPO_DIR"

# --- derive settings from the existing fangoio service --------------------
FANGO_UNIT="$(systemctl cat fangoio 2>/dev/null || true)"
[ -n "$FANGO_UNIT" ] || echo "    (fangoio unit not found — falling back to defaults)"

# fangoio's ExecStart may be uvicorn/gunicorn, not python — take the python that
# lives in the SAME bin dir (the venv), not the ExecStart binary itself.
EXEC_BIN="$(printf '%s\n' "$FANGO_UNIT" | sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' | head -1)"
PYTHON=""
if [ -n "$EXEC_BIN" ]; then
  BIN_DIR="$(dirname "$EXEC_BIN")"
  for cand in "$BIN_DIR/python" "$BIN_DIR/python3"; do
    [ -x "$cand" ] && { PYTHON="$cand"; break; }
  done
fi
if [ -z "$PYTHON" ]; then
  for cand in "$REPO_DIR/.venv/bin/python" "$REPO_DIR/.venv/bin/python3"; do
    [ -x "$cand" ] && { PYTHON="$cand"; break; }
  done
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || {
  echo "error: python not found next to '$EXEC_BIN' or in $REPO_DIR/.venv/bin" >&2; exit 1; }

SVC_USER="$(printf '%s\n' "$FANGO_UNIT" | sed -n 's/^User=\(.*\)/\1/p' | head -1)"
[ -n "${SVC_USER:-}" ] || SVC_USER="$(id -un)"

# endpoint: prefer local port (faster, internal), else the public URL
if [ -n "${FANGO_MCP_URL:-}" ]; then
  MCP_URL="$FANGO_MCP_URL"
else
  PORT="$(printf '%s\n' "$FANGO_UNIT" | grep -oE -- '--port[ =][0-9]+|-p [0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
  if [ -z "${PORT:-}" ] && [ -f "$REPO_DIR/.env" ]; then
    PORT="$(grep -oiE '^ *PORT *= *[0-9]+' "$REPO_DIR/.env" | grep -oE '[0-9]+' | head -1 || true)"
  fi
  if [ -n "${PORT:-}" ]; then MCP_URL="http://127.0.0.1:$PORT/mcp2/mcp"; else MCP_URL="https://fango.city/mcp2/mcp"; fi
fi

echo "    python: $PYTHON"
echo "    user:   $SVC_USER"
echo "    url:    $MCP_URL"
echo "    poll:   every ${INTERVAL}s"

# --- broker key ------------------------------------------------------------
if [ -n "${FANGO_BROKER_KEY:-}" ]; then
  KEY="$FANGO_BROKER_KEY"
else
  read -rsp "Paste the broker agent key: " KEY; echo
fi
[ -n "${KEY:-}" ] || { echo "error: empty broker key" >&2; exit 1; }

# --- preflight: validate the key with a non-destructive dry-run ------------
echo "==> Preflight dry-run (no posts)..."
if ! FANGO_BROKER_KEY="$KEY" FANGO_MCP_URL="$MCP_URL" "$PYTHON" -m scripts.broker_autoresponder --once --dry-run; then
  echo "error: dry-run failed — check the key / URL. Service NOT installed." >&2
  exit 1
fi

# --- write the 0600 env file ----------------------------------------------
( umask 077; cat > "$ENV_FILE" <<EOF
FANGO_BROKER_KEY=$KEY
FANGO_MCP_URL=$MCP_URL
EOF
)
chmod 600 "$ENV_FILE"
echo "==> Wrote $ENV_FILE (chmod 600)"

# --- install the systemd unit (needs sudo) --------------------------------
echo "==> Installing $UNIT_PATH (sudo may prompt for your password)"
sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Fango broker autoresponder
After=network-online.target fangoio.service
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON -m scripts.broker_autoresponder --interval $INTERVAL
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable fango-broker >/dev/null 2>&1 || true
sudo systemctl restart fango-broker
sleep 1
echo "==> Done. Status:"
sudo systemctl status fango-broker --no-pager -n 15 || true
echo
echo "Follow logs:  sudo journalctl -u fango-broker -f"
echo "Stop:         sudo systemctl disable --now fango-broker"
