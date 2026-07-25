#!/data/data/com.termux/files/usr/bin/bash
# The always-on Termux supervisor. This is the thing that "just runs forever" on
# the phone: it holds a wake-lock, waits for the network, then runs the capture
# pipeline (run/capture.sh) in a loop that nothing short of a reboot escapes —
# and Termux:Boot restarts it after that too.
#
# Layers of retry, from inside out:
#   1. lookcam_stream.py  — reconnects to a known camera IP; exits after
#                           --give-up seconds if that IP goes dead
#   2. run/capture.sh     — re-broadcasts to find the camera's (possibly new) IP,
#                           then restarts the stream+ffmpeg pipeline
#   3. this script        — restarts capture.sh if it dies for any reason at all
#                           (OOM kill, Termux restart, config typo, ffmpeg crash)
#
#   bash ~/lookcam/phone/termux-run.sh          # run it by hand (Ctrl-C to stop)
# It is also what ~/.termux/boot/ runs on every reboot (see setup-termux.sh).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${LOG:-$HOME/lookcam.log}"
MAX_LOG_BYTES=${MAX_LOG_BYTES:-5000000}     # rotate at ~5 MB, keep one old copy
RESTART_DELAY=${RESTART_DELAY:-5}

log() { echo "[$(date '+%F %T')] $*"; }

# Keep the CPU alive. Without this Android suspends Termux within minutes of the
# screen going off and the stream dies silently.
command -v termux-wake-lock >/dev/null && termux-wake-lock
trap 'command -v termux-wake-unlock >/dev/null && termux-wake-unlock' EXIT

rotate_log() {
  local size
  size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  [ "$size" -gt "$MAX_LOG_BYTES" ] && mv -f "$LOG" "$LOG.1"
}

# Don't start broadcasting into the void before Android has brought WiFi/hotspot
# up — on boot this script often wins the race against the network.
wait_for_network() {
  command -v ip >/dev/null || return 0        # can't tell — just proceed
  local waited=0
  while [ -z "$(ip -o -4 addr show scope global 2>/dev/null)" ]; do
    [ $((waited % 60)) -eq 0 ] && log "waiting for a network interface..."
    sleep 5; waited=$((waited + 5))
  done
}

main() {
  log "supervisor starting (repo=$REPO)"
  wait_for_network
  while true; do
    rotate_log
    log "starting capture pipeline"
    bash "$REPO/run/capture.sh"
    log "capture exited (rc=$?) — restarting in ${RESTART_DELAY}s"
    sleep "$RESTART_DELAY"
  done
}

# When run from Termux:Boot there is no terminal, so everything goes to the log.
if [ -t 1 ]; then main 2>&1 | tee -a "$LOG"; else main >>"$LOG" 2>&1; fi
