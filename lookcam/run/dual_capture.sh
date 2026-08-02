#!/usr/bin/env bash
# Runs TWO capture_snapshot.sh loops at once — the primary camera and a
# fallback — each writing its own rolling JPEG. offline/solver.py picks
# whichever file is freshest for GET /assignment/camera (see
# _pick_camera_snapshot() there); this script does no picking itself, it just
# keeps both snapshots as up to date as each camera allows.
#
# "Sometimes still try to query the first camera" falls out for free: each
# capture_snapshot.sh loop finds-and-streams its own camera forever, on its
# own schedule, regardless of what the other one is doing. There is no
# explicit failover state machine here — the primary camera going quiet just
# means its snapshot file stops updating and solver.py starts preferring the
# fallback's, and the moment the primary's loop reconnects (which it never
# stops trying to do) its file goes fresh again and solver.py switches back.
#
#   cp -n run/config.env run/config.local.env && nano run/config.local.env
#   #   CAM_UID, CAM_UID_FALLBACK, CAM_PASS, SNAPSHOT_DIR
#   bash run/dual_capture.sh
#   bash run/dual_capture.sh --force   # take over from a copy already running
#
# On a phone this is what phone/termux-run.sh's camera-feed counterpart would
# supervise (wake-lock, restart-on-crash) the same way it supervises
# capture.sh — not wired up here; run this by hand or under your own
# supervisor for now. It DOES guard against a second copy of itself, the same
# way termux-run.sh does for the streaming pipeline (see below).
set -uo pipefail
cd "$(dirname "$0")/.."

CONF="run/config.env"
[ -f run/config.local.env ] && CONF="run/config.local.env"
if [ -f "$CONF" ]; then . "$CONF"; else echo "[!] missing $CONF"; exit 1; fi
: "${CAM_UID:?set it in $CONF}"
: "${CAM_UID_FALLBACK:?set it in $CONF}"
: "${SNAPSHOT_DIR:=$HOME/lookcam-snapshots}"

FORCE=0
case "${1:-}" in --force|--takeover) FORCE=1 ;; esac
PIDFILE="${PIDFILE:-$HOME/.lookcam-dual-capture.pid}"

# ---- single-instance guard ---------------------------------------------------
# Two copies would each log into BOTH cameras as separate viewer sessions and
# both write the same two snapshot files — not fatal the way two SRT
# publishers fighting over one MediaMTX path is, but pointless and double the
# camera load for nothing. Same guard termux-run.sh uses for the online
# stream.
source "$(pwd)/../scripts/singleton-guard.sh"
singleton_guard "dual camera capture" "$PIDFILE" "dual_capture.sh" \
  "run/dual_capture\.sh" "run/capture_snapshot\.sh"
echo $$ > "$PIDFILE"

mkdir -p "$SNAPSHOT_DIR"
PRIMARY_JPG="$SNAPSHOT_DIR/primary.jpg"
FALLBACK_JPG="$SNAPSHOT_DIR/fallback.jpg"

echo "[dual] primary=$CAM_UID -> $PRIMARY_JPG"
echo "[dual] fallback=$CAM_UID_FALLBACK -> $FALLBACK_JPG"

PIDS=()
cleanup() {
  [ "${#PIDS[@]}" -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null
  [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
}
trap 'cleanup; exit 0' INT TERM

bash run/capture_snapshot.sh primary "$CAM_UID" "$PRIMARY_JPG" &
PIDS+=($!)
bash run/capture_snapshot.sh fallback "$CAM_UID_FALLBACK" "$FALLBACK_JPG" &
PIDS+=($!)

# Each loop runs forever on its own (camera drops and reconnects happen
# *inside* capture_snapshot.sh, not by exiting), so in the ordinary case this
# just blocks until Ctrl-C/SIGTERM fires the trap above.
wait "${PIDS[@]}"
