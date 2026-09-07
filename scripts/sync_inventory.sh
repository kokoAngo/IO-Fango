#!/usr/bin/env bash
# One LAN-side inventory sync cycle. Driven by launchd (see
# scripts/city.fango.inventory-sync.plist); safe to run by hand.
#
# Order matters:
#   1. ingest    — pull new/changed upstream rows (incremental via a watermark)
#   2. reconcile — re-read the gate columns for everything we already hold.
#                  The incremental pass only ever revisits rows it re-sees, so
#                  without this a listing withdrawn after ingest stays
#                  advertised (おとり広告). It runs every cycle, not weekly.
#   3. photos    — fetch bytes for listings that have none yet
#   4. export    — package an artifact for the app server
#
# Delivery is deliberately NOT here: see DELIVER_CMD below.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
STATE_DIR="${FANGO_SYNC_STATE_DIR:-data/sync}"
ARTIFACT_DIR="${FANGO_SYNC_ARTIFACT_DIR:-data/sync/artifact}"
WATERMARK_FILE="$STATE_DIR/watermark"
mkdir -p "$STATE_DIR"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { log "FAILED at: $*"; exit 1; }

# The upstream rental table has no updated_at, only created_time, so the
# incremental window is deliberately generous — cheap, and step 2 is what
# actually catches mutations.
#
# With no watermark this does NOT sweep the whole upstream table. Nothing older
# than the visibility window can ever be shown, so pulling 355k rentals to hide
# 350k of them costs an hour and buys nothing. The floor is the widest window
# in use (sale's, the longer of the two) so a first run still fills both kinds.
SINCE_ARG=()
if [ -f "$WATERMARK_FILE" ]; then
  SINCE_ARG=(--since "$(cat "$WATERMARK_FILE")")
  log "incremental since $(cat "$WATERMARK_FILE")"
else
  FLOOR=$($PY -c "from fango.listings import service as ls; print(ls.window_start('sale'))")
  SINCE_ARG=(--since "$FLOOR")
  log "no watermark — seeding from the visibility window floor $FLOOR"
fi

log "1/4 ingest"
# ${a[@]+"${a[@]}"} — macOS ships bash 3.2, where "${a[@]}" on an EMPTY array
# trips `set -u`. This form expands to nothing when the array is empty.
$PY -m scripts.ingest_pg --write --advertisable-only \
  ${SINCE_ARG[@]+"${SINCE_ARG[@]}"} || fail ingest

log "2/4 reconcile"
$PY -m scripts.ingest_pg --reconcile || fail reconcile

log "3/4 photos"
$PY -m scripts.fetch_listing_images || fail photos

log "4/4 export"
rm -rf "$ARTIFACT_DIR"
$PY -m scripts.export_inventory --out "$ARTIFACT_DIR" || fail export

# Advance the watermark only after a clean export, so a failure re-does the
# window rather than skipping it.
NEW_WM=$($PY -c "import json,sys;print(json.load(open('$ARTIFACT_DIR/manifest.json'))['watermark'] or '')")
if [ -n "$NEW_WM" ]; then
  echo "$NEW_WM" > "$WATERMARK_FILE"
  log "watermark -> $NEW_WM"
fi

# Delivery to the app server. The app server is off-LAN and this machine has
# no route to it, so the transport is left as a configuration hook rather than
# guessed at. Set FANGO_SYNC_DELIVER to e.g.
#   'rsync -az --delete <artifact>/ user@host:/srv/fango-inventory/'
# with "$ARTIFACT_DIR" substituted in.
if [ -n "${FANGO_SYNC_DELIVER:-}" ]; then
  log "deliver: $FANGO_SYNC_DELIVER"
  ARTIFACT_DIR="$ARTIFACT_DIR" bash -c "$FANGO_SYNC_DELIVER" || fail deliver
else
  log "no FANGO_SYNC_DELIVER set — artifact left at $ARTIFACT_DIR"
fi

log "done"
