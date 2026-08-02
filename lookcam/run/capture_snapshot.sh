#!/usr/bin/env bash
# Camera -> a rolling local JPEG snapshot, for offline/solver.py's
# GET /assignment/camera (no VPS involved — see lookcam-camera-failover
# in project memory, and offline/camera_render.py for the tile rendering).
#
# A sibling of capture.sh, not a replacement for it: same discover -> stream
# -> decode loop, but the last stage writes one JPEG to disk on repeat instead
# of pushing to MediaMTX. Takes camera identity and the output path as
# arguments (rather than from config.env) so TWO of these can run at once —
# one per physical camera — without fighting over the same globals. See
# run/dual_capture.sh, which is what actually starts both.
#
#   bash run/capture_snapshot.sh <slot-name> <cam-uid> <snapshot-path>
#
# Settings shared with capture.sh (CAM_PASS, STREAM, discovery timing) still
# come from run/config.env / config.local.env.
set -uo pipefail
cd "$(dirname "$0")/.."          # repo root (where lookcam_stream.py lives)

SLOT="${1:?usage: capture_snapshot.sh <slot-name> <cam-uid> <snapshot-path>}"
CAM_UID="${2:?usage: capture_snapshot.sh <slot-name> <cam-uid> <snapshot-path>}"
SNAPSHOT_PATH="${3:?usage: capture_snapshot.sh <slot-name> <cam-uid> <snapshot-path>}"

CONF="run/config.env"
[ -f run/config.local.env ] && CONF="run/config.local.env"
if [ -f "$CONF" ]; then . "$CONF"; else echo "[!] missing $CONF"; exit 1; fi
: "${CAM_PASS:=12345678}"   ; : "${STREAM:=1}"
: "${DISCOVER_TIMEOUT:=5}"  ; : "${DISCOVER_RETRY:=10}"
: "${EXTRA_BROADCAST:=}"    ; : "${EXTRA_SUBNET:=}"
: "${SNAPSHOT_FPS:=1}"

PYTHON="./.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v ffmpeg >/dev/null || { echo "[!] ffmpeg not installed"; exit 1; }

mkdir -p "$(dirname "$SNAPSHOT_PATH")"

find_camera() {
  local args=(--uid "$CAM_UID") ip try=0 b
  for b in $EXTRA_BROADCAST; do args+=(--broadcast "$b"); done
  for b in $EXTRA_SUBNET; do args+=(--subnet "$b"); done
  while true; do
    try=$((try + 1))
    if [ "$try" = 1 ]; then
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${args[@]}")"
    else
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${args[@]}" 2>/dev/null)"
    fi
    if [ -n "$ip" ]; then
      echo "$ip"; return 0
    fi
    echo "[$SLOT] camera $CAM_UID not on the network yet (attempt $try) — retrying in ${DISCOVER_RETRY}s..." >&2
    sleep "$DISCOVER_RETRY"
  done
}

echo "[$SLOT] capture: uid=$CAM_UID stream=$STREAM -> $SNAPSHOT_PATH (${SNAPSHOT_FPS}fps)"

FIFO=""
cleanup() {
  [ -n "${PY_PID:-}" ] && kill "$PY_PID" 2>/dev/null
  [ -n "${FF_PID:-}" ] && kill "$FF_PID" 2>/dev/null
  [ -n "$FIFO" ] && rm -f "$FIFO"
}
trap 'cleanup; exit 0' INT TERM

while true; do
  CAM_ADDR="$(find_camera)"
  echo "[$SLOT] camera at ${CAM_ADDR} — starting stream"

  FIFO="$(mktemp -u "${TMPDIR:-/tmp}/lookcam-${SLOT}.XXXXXX")"
  mkfifo "$FIFO" || { echo "[$SLOT] cannot create fifo"; sleep 5; continue; }

  "$PYTHON" lookcam_stream.py -a "$CAM_ADDR" -p "$CAM_PASS" --stream "$STREAM" --pipe \
    > "$FIFO" &
  PY_PID=$!
  # -update 1 on the image2 muxer overwrites the same file every frame rather
  # than numbering one per frame — a rolling snapshot, not a recording. Each
  # write is open/encode/close, so a reader can in principle catch a half
  # written file; solver.py just fails that one poll and tries again on the
  # next (~1s later), which costs nothing worth guarding against here.
  ffmpeg -hide_banner -loglevel warning \
      -fflags +genpts+nobuffer -flags low_delay \
      -probesize 200000 -analyzeduration 200000 \
      -use_wallclock_as_timestamps 1 \
      -f hevc -i "$FIFO" \
      -vf "fps=${SNAPSHOT_FPS}" -update 1 -qscale:v 5 -y "$SNAPSHOT_PATH" &
  FF_PID=$!

  wait -n "$PY_PID" "$FF_PID" 2>/dev/null
  kill "$PY_PID" "$FF_PID" 2>/dev/null
  wait "$PY_PID" 2>/dev/null; wait "$FF_PID" 2>/dev/null
  rm -f "$FIFO"; FIFO=""; PY_PID=""; FF_PID=""

  echo "[$SLOT] pipeline exited (camera/network drop?). re-finding camera in 3s..."
  sleep 3
done
