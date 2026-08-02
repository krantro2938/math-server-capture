#!/data/data/com.termux/files/usr/bin/bash
# The always-on supervisor for the gallery bridge. This is the thing that makes
# "publish the latest photo" work at the moment you tap the temple, rather than
# the moment you last remembered to start a script.
#
# The bridge itself is a plain HTTP server and does not try to be robust — it is
# robust because of this file. Layers, from inside out:
#
#   1. gallery.py     survives a failing request; exits cleanly on SIGTERM, and
#                     with 78 when the port is already taken
#   2. this script    restarts it whenever it exits, and kills it when it stops
#                     answering (a wedged server is worse than a dead one:
#                     nothing restarts it and the app just hangs)
#   3. Termux:Boot    starts this again after a reboot (see setup.sh)
#
# What none of this can fix is Android deciding to kill Termux — the wake lock
# below and an Unrestricted battery setting are what actually prevent that, and
# the setup script says so.
#
#   bash ~/lookcam/phone/gallery/run.sh            # by hand (Ctrl-C to stop)
#   bash ~/lookcam/phone/gallery/run.sh --force    # ...taking over from a
#                                                  #    copy already running
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="python3"; command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python"

PORT="${GALLERY_PORT:-8790}"
HOST="${GALLERY_HOST:-127.0.0.1}"
LOG="${LOG:-$HOME/gallery-bridge.log}"
MAX_LOG_BYTES=${MAX_LOG_BYTES:-2000000}   # rotate at ~2 MB, keep one old copy
RESTART_DELAY=${RESTART_DELAY:-5}
CONFIG_RETRY=${CONFIG_RETRY:-60}
# How often to ask the bridge whether it is still answering, and how many
# consecutive misses before it is treated as wedged rather than busy.
PROBE_EVERY=${PROBE_EVERY:-30}
PROBE_MISSES=${PROBE_MISSES:-2}
PIDFILE="${PIDFILE:-$HOME/.lookcam-gallery.pid}"

FORCE=0
case "${1:-}" in
  --force|--takeover) FORCE=1; shift ;;
  "") ;;
esac

log() { echo "[$(date '+%F %T')] $*"; }

# ---- single-instance guard ---------------------------------------------------
# Two copies of the bridge both try to bind $PORT — the second one's gallery.py
# exits 78 (port taken) and this script just backs off and retries forever,
# which used to be the only feedback you got. Refuse outright instead, and name
# what's already running, the same way termux-run.sh does for the camera
# stream.
source "$HERE/../../../scripts/singleton-guard.sh"
singleton_guard "gallery bridge" "$PIDFILE" "gallery/run.sh" \
  "gallery/run\.sh" "gallery\.py"
echo $$ > "$PIDFILE"

# Keep the CPU alive. Without this Android suspends Termux within minutes of the
# screen going off — and this is exactly the moment you want the bridge up: the
# phone is in your pocket and you are looking at the glasses.
command -v termux-wake-lock >/dev/null && termux-wake-lock
cleanup() {
  [ -n "${BRIDGE_PID:-}" ] && kill "$BRIDGE_PID" 2>/dev/null
  command -v termux-wake-unlock >/dev/null && termux-wake-unlock
  [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

rotate_log() {
  local size
  size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  [ "$size" -gt "$MAX_LOG_BYTES" ] && mv -f "$LOG" "$LOG.1"
}

# Python rather than curl: the bridge already needs python, and curl is a
# separate `pkg install` that a phone set up for this may well not have.
probe() {
  "$PYTHON" - "$HOST" "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
host, port = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

# Run the bridge in the background and watch it two ways at once: for the
# process exiting, and for it going quiet while still alive.
run_once() {
  "$PYTHON" "$HERE/gallery.py" --host "$HOST" --port "$PORT" "$@" &
  BRIDGE_PID=$!

  local misses=0
  while kill -0 "$BRIDGE_PID" 2>/dev/null; do
    sleep "$PROBE_EVERY"
    kill -0 "$BRIDGE_PID" 2>/dev/null || break
    if probe; then
      misses=0
    else
      misses=$((misses + 1))
      log "health probe failed ($misses/$PROBE_MISSES)"
      if [ "$misses" -ge "$PROBE_MISSES" ]; then
        log "bridge is not answering — restarting it"
        kill "$BRIDGE_PID" 2>/dev/null
        sleep 2
        kill -9 "$BRIDGE_PID" 2>/dev/null
        wait "$BRIDGE_PID" 2>/dev/null
        BRIDGE_PID=""
        return 1
      fi
    fi
  done

  wait "$BRIDGE_PID"
  local rc=$?
  BRIDGE_PID=""
  return $rc
}

main() {
  log "gallery supervisor starting ($HERE, port $PORT)"
  while true; do
    rotate_log
    run_once "$@"
    rc=$?
    # 78 = EX_CONFIG: the port is taken, which almost always means a second copy
    # of the bridge is already serving. Restarting into the same collision every
    # five seconds fills the log and fixes nothing.
    if [ "$rc" = 78 ]; then
      log "bridge exited (rc=78: port $PORT already in use) — re-checking in ${CONFIG_RETRY}s"
      sleep "$CONFIG_RETRY"
      continue
    fi
    log "bridge exited (rc=$rc) — restarting in ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
  done
}

# From Termux:Boot there is no terminal, so everything goes to the log.
if [ -t 1 ]; then main "$@" 2>&1 | tee -a "$LOG"; else main "$@" >>"$LOG" 2>&1; fi
