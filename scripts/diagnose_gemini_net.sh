#!/usr/bin/env bash
# One-shot network diagnosis for "fango_consult is busy" when the consult engine
# itself is healthy (key + SDK ok) but the live Gemini call hangs.
#
# Run from the repo root on the server:   bash scripts/diagnose_gemini_net.sh
#
# It loads .env, then checks: DNS (does the host resolve to IPv6?), IPv4 vs IPv6
# reachability, and whether the google-genai SDK call actually hangs. Prints a
# verdict. No arguments.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then set -a; source .env; set +a; fi
KEY="${GEMINI_API_KEY:-}"
HOST="generativelanguage.googleapis.com"
URL="https://${HOST}/v1beta/models?key=${KEY}"

if [[ -z "$KEY" ]]; then echo "GEMINI_API_KEY empty — fix that first"; exit 1; fi
echo "GEMINI_API_KEY len=${#KEY}"
echo

echo "=== [1/4] DNS resolution (any IPv6 / AAAA?) ==="
getent ahosts "$HOST" || true
echo

echo "=== [2/4] force IPv4 ==="
curl -4 -sS -m 12 -o /dev/null -w "v4: HTTP %{http_code}  %{time_total}s\n" "$URL" \
  || echo "v4: FAILED/timeout"
echo

echo "=== [3/4] force IPv6 ==="
curl -6 -sS -m 12 -o /dev/null -w "v6: HTTP %{http_code}  %{time_total}s\n" "$URL" \
  || echo "v6: FAILED/timeout (this is the usual culprit)"
echo

echo "=== [4/4] does the google-genai SDK call hang? (35s cap) ==="
PY="${FANGO_PYTHON:-$ROOT/.venv/bin/python}"
timeout 35 "$PY" - <<'PYEOF'
import os, time
from google import genai
c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
t = time.time()
r = c.models.generate_content(model=os.environ.get("FANGO_CONSULT_MODEL","gemini-2.5-flash"),
                              contents="say hi")
print("SDK OK in %.1fs: %r" % (time.time()-t, (r.text or "")[:40]))
PYEOF
rc=$?
echo "SDK exit=$rc  (124 = killed by timeout = the call HANGS)"
echo
echo "=== verdict ==="
if [[ $rc -eq 124 ]]; then
  echo "SDK call HANGS while curl works → almost certainly broken IPv6 egress."
  echo "Fix: prefer IPv4 on this host. Edit /etc/gai.conf, uncomment:"
  echo "     precedence ::ffff:0:0/96  100"
  echo "then restart the fango service. (Or disable IPv6 on the VM / set HTTPS_PROXY.)"
elif [[ $rc -eq 0 ]]; then
  echo "SDK call SUCCEEDED here → the earlier hang was transient, OR the running"
  echo "service predates your .env edit. Restart the service and retry consult."
else
  echo "SDK call failed (exit $rc) but not a timeout — see the Python traceback above"
  echo "for the real error (auth / quota / region)."
fi
