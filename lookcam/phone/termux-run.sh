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
# Layers 1-3 all key off something EXITING. A hang exits nothing, so each layer
# also has to convert silence into an exit: lookcam_stream.py drops a session
# that stops producing video, capture.sh rebuilds a pipeline that stops muxing
# it, and this script kills a capture.sh that stops reporting progress at all
# (see supervise_capture).
#
#   bash ~/lookcam/phone/termux-run.sh          # run it by hand (Ctrl-C to stop)
#   bash ~/lookcam/phone/termux-run.sh --force  # ...taking over from whatever is
#                                               #    already streaming
# It is also what ~/.termux/boot/ runs on every reboot (see setup-termux.sh).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python"   # Termux ships `python`
LOG="${LOG:-$HOME/lookcam.log}"
MAX_LOG_BYTES=${MAX_LOG_BYTES:-5000000}     # rotate at ~5 MB, keep one old copy
RESTART_DELAY=${RESTART_DELAY:-5}
PIDFILE="${PIDFILE:-$HOME/.lookcam.pid}"
# run/capture.sh touches this whenever it is demonstrably making progress; see
# supervise_capture() for why waiting on the process alone is not enough.
# Exported because capture.sh has to agree with us on the path.
HEARTBEAT_FILE="${HEARTBEAT_FILE:-$HOME/.lookcam.heartbeat}"; export HEARTBEAT_FILE
HEARTBEAT_MAX=${HEARTBEAT_MAX:-180}
# Kept short because it is also the latency added to a NORMAL restart: the loop
# below only notices capture.sh exiting on its next tick, and an exit is the
# common case (camera drop, ffmpeg death) that used to be handled instantly.
SUPERVISOR_POLL=${SUPERVISOR_POLL:-5}

FORCE=0
case "${1:-}" in
  --force|--takeover) FORCE=1 ;;
  -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
  "") ;;
  *) echo "usage: $(basename "$0") [--force]" >&2; exit 2 ;;
esac

log() { echo "[$(date '+%F %T')] $*"; }

# An interruptible sleep. A plain `sleep 60` is a FOREGROUND child, and bash runs
# no trap handler until one of those returns — so a supervisor asked to stop mid-
# retry would sit there for up to a minute, still holding the pidfile and the
# wake lock, and a --force takeover would have to SIGKILL it. Backgrounding the
# sleep and `wait`ing lets the signal land immediately.
nap() { sleep "$1" & wait $!; }

# When run from Termux:Boot there is no terminal, so everything goes to the log.
# Do this BEFORE the guard below: a boot-time "refusing to start" has to be
# readable afterwards, and under Termux:Boot stdout is otherwise thrown away.
#
# Redirect with exec rather than piping `main | tee`: a pipeline would run main
# in a SUBSHELL, which resets our traps and would hide CAPTURE_PID from the
# cleanup() that needs to kill it.
if [ -t 1 ]; then exec > >(tee -a "$LOG") 2>&1; else exec >>"$LOG" 2>&1; fi

###############################################################################
# Single-instance guard
#
# Two capture pipelines running at once is not "twice as reliable", it is a
# stream that flaps: both log into the camera (it reports connectNum: 2), and
# both SRT-publish to the same MediaMTX path, where overridePublisher defaults
# to yes — so each new publisher KICKS the other one off and ffmpeg dies with
# "Error submitting a packet to the muxer: I/O error" / broken pipe, forever.
#
# A pidfile alone is not enough to detect this. Android kills the supervisor
# (it is the idlest process in the tree) while leaving its capture.sh child
# alive, re-parented to init — and capture.sh loops on its own, so the stream
# keeps running with nobody supervising it and no pidfile owner. So we look for
# the *pipeline*, not just for another copy of this script.
###############################################################################

# Anything already talking to the camera or to the VPS. We have not started
# anything yet at guard time, so every hit here is somebody else's — except us
# and the shell that launched us, which match if either was invoked with one of
# these words on its command line.
stray_pids() {
  { pgrep -f "$REPO/run/capture.sh"
    pgrep -f 'lookcam_stream\.py'
    pgrep -f 'streamid=publish:'      # the ffmpeg SRT push
  } 2>/dev/null | sort -un | grep -vx -e "$$" -e "$PPID"
}

# One-line, truncated command line for pid $1 — /proc separates args with NULs,
# and an arg can itself contain newlines, so flatten both before cutting.
describe_pid() {
  tr '\0\n' '  ' < "/proc/$1/cmdline" 2>/dev/null | cut -c1-90
}

# The pid of a live supervisor, or "" if the pidfile is missing/stale.
#
# `kill -0` alone is not proof: $PIDFILE lives in $HOME and so SURVIVES REBOOTS,
# while pids do not. Android hands out low pids fast after boot, so the stale
# file would eventually name some unrelated live process — and this script would
# then refuse to start, forever, with the stream down. Confirm the pid really is
# a termux-run.sh before believing it.
running_supervisor() {
  local p
  p="$(cat "$PIDFILE" 2>/dev/null)" || return 0
  case "$p" in ''|*[!0-9]*) return 0 ;; esac
  [ "$p" = "$$" ] && return 0
  kill -0 "$p" 2>/dev/null || return 0
  case "$(describe_pid "$p")" in
    *termux-run.sh*) echo "$p" ;;
    *) log "ignoring stale $PIDFILE (pid $p is not a supervisor)" >&2 ;;
  esac
}

acquire_lock() {
  local sup strays pid
  sup="$(running_supervisor)"
  strays="$(stray_pids)"

  if [ -z "$sup" ] && [ -z "$strays" ]; then
    echo $$ > "$PIDFILE"
    return 0
  fi

  if [ "$FORCE" != 1 ]; then
    log "REFUSING TO START — the camera is already being streamed:"
    [ -n "$sup" ] && log "  supervisor pid $sup (from $PIDFILE)"
    for pid in $strays; do
      log "  $pid $(describe_pid "$pid")"
    done
    [ -z "$sup" ] && [ -n "$strays" ] &&
      log "  (no supervisor — an orphaned pipeline, probably from a killed boot run)"
    log "Starting a second one would make BOTH flap. To take over, run:"
    log "  bash $0 --force"
    exit 3
  fi

  # Takeover. Kill the supervisor FIRST: kill the pipeline while a supervisor is
  # still watching and it just starts a fresh one 5s later, right on top of us.
  if [ -n "$sup" ]; then log "--force: stopping supervisor $sup"; kill "$sup" 2>/dev/null; fi
  strays="$(stray_pids)"
  if [ -n "$strays" ]; then
    log "--force: stopping pipeline $(echo "$strays" | tr '\n' ' ')"
    # SIGTERM lets capture.sh run its own cleanup trap and reap its children.
    kill $strays 2>/dev/null
    local waited=0
    while [ -n "$(stray_pids)" ] && [ "$waited" -lt 10 ]; do sleep 1; waited=$((waited + 1)); done
    strays="$(stray_pids)"
    [ -n "$strays" ] && { log "--force: SIGKILL for stubborn $(echo "$strays" | tr '\n' ' ')"; kill -9 $strays 2>/dev/null; sleep 1; }
  fi
  echo $$ > "$PIDFILE"
}

# Everything below here runs only once we own the lock — in particular the wake
# lock, which is NOT per-process: termux-wake-lock/unlock toggle it for the whole
# Termux app. A second instance that grabbed it on the way in and released it on
# the way out would silently un-protect the instance that was already running,
# and Android would suspend the survivor the next time the screen went off.
WAKE_LOCK_HELD=0
CAPTURE_PID=""

cleanup() {
  # Take the pipeline's grandchildren (lookcam_stream.py, ffmpeg) with us, not
  # just capture.sh: killing only the parent is how an orphan that keeps
  # publishing to the VPS gets left behind in the first place. Scoped to our own
  # child's children, so a supervisor that has already taken over is untouched.
  if [ -n "$CAPTURE_PID" ]; then
    kill $(pgrep -P "$CAPTURE_PID" 2>/dev/null) "$CAPTURE_PID" 2>/dev/null
  fi
  [ "$WAKE_LOCK_HELD" = 1 ] && command -v termux-wake-unlock >/dev/null && termux-wake-unlock
  [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
  return 0
}
trap cleanup EXIT
trap 'cleanup; exit 0' INT TERM

acquire_lock

# Keep the CPU alive. Without this Android suspends Termux within minutes of the
# screen going off and the stream dies silently.
if command -v termux-wake-lock >/dev/null; then termux-wake-lock; WAKE_LOCK_HELD=1; fi

rotate_log() {
  local size
  size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  [ "$size" -gt "$MAX_LOG_BYTES" ] && mv -f "$LOG" "$LOG.1"
}

# Don't start broadcasting into the void before Android has brought WiFi/hotspot
# up — on boot this script often wins the race against the network.
#
# `ip` is useless as the test here: Android blocks netlink for unprivileged apps,
# so in Termux `ip -o -4 addr` succeeds while printing NOTHING, and this loop
# waited forever on a phone that was online the whole time. discover.py's own
# enumeration (ioctl-based, works on Android) is the honest check, and we cap the
# wait either way — never block the pipeline on a test that can't answer.
wait_for_network() {
  local waited=0 max=${NETWORK_WAIT_MAX:-120}
  while [ "$waited" -lt "$max" ]; do
    if "${PYTHON:-python}" -c 'import sys; sys.path.insert(0,sys.argv[1]); import discover; sys.exit(0 if discover.interfaces() else 1)' "$REPO" 2>/dev/null; then
      return 0
    fi
    [ $((waited % 60)) -eq 0 ] && log "waiting for a network interface..."
    nap 5; waited=$((waited + 5))
  done
  log "no interface detected after ${max}s — starting anyway"
}

# Is $1 a live process, as opposed to an exited one we have not reaped yet?
# `kill -0` cannot answer this: it succeeds on zombies, so a loop built on it
# would keep "supervising" a capture.sh that finished seconds ago. Field 3 of
# /proc/<pid>/stat is the state letter; comm (field 2) can contain spaces, so
# cut past its closing paren rather than counting fields.
child_alive() {
  local line st
  line="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  st="${line##*) }"; st="${st%% *}"
  [ -n "$st" ] && [ "$st" != "Z" ]
}

# Wait for the capture pipeline, but treat silence as a fault.
#
# `wait $CAPTURE_PID` on its own is a bet that every fault below eventually
# becomes an exit — and it isn't one. A camera that answers discovery but never
# sends video (OpenVideo refused, another client attached, a resync it never
# finished) leaves lookcam_stream.py alive and mute; ffmpeg then blocks reading
# the FIFO with no -timeout to save it; capture.sh blocks in `wait -n`; and we
# block here, forever, still holding the wake lock and the pidfile — which also
# means acquire_lock refuses a plain restart, because it correctly sees a live
# supervisor and a live pipeline. Only a reboot or --force got out of that.
#
# lookcam_stream.py's own watchdog now catches that specific case. This is the
# general backstop: if capture.sh stops reporting progress at all, restart it,
# whatever the reason. Enforcement ARMS only after the child's first beat, so an
# old capture.sh without heartbeats (a half-finished deploy) is left alone
# rather than being killed every HEARTBEAT_MAX seconds forever.
supervise_capture() {
  local started="$1" armed=0 stamp age waited
  while child_alive "$CAPTURE_PID"; do
    nap "$SUPERVISOR_POLL"
    stamp="$(stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null)"
    [ -z "$stamp" ] && continue          # no heartbeat yet, or no usable stat
    if [ "$armed" = 0 ]; then
      [ "$stamp" -gt "$started" ] && armed=1
      continue
    fi
    age=$(( $(date +%s) - stamp ))
    [ "$age" -lt "$HEARTBEAT_MAX" ] && continue

    log "capture pipeline silent for ${age}s (limit ${HEARTBEAT_MAX}s) — killing it"
    # Grandchildren too: killing only capture.sh is how an orphan that keeps
    # publishing to the VPS gets left behind, and here it would be a HUNG one,
    # which acquire_lock would then see as a live pipeline.
    kill $(pgrep -P "$CAPTURE_PID" 2>/dev/null) "$CAPTURE_PID" 2>/dev/null
    waited=0
    while child_alive "$CAPTURE_PID" && [ "$waited" -lt 10 ]; do nap 1; waited=$((waited + 1)); done
    child_alive "$CAPTURE_PID" &&
      kill -9 $(pgrep -P "$CAPTURE_PID" 2>/dev/null) "$CAPTURE_PID" 2>/dev/null
    return 0
  done
}

main() {
  log "supervisor starting (repo=$REPO)"
  wait_for_network
  [ -r "/proc/$$/stat" ] ||
    log "warning: /proc is unreadable — cannot detect a hung capture pipeline"
  while true; do
    rotate_log
    log "starting capture pipeline"
    # Backgrounded + waited on, rather than run in the foreground, so that our
    # signal traps fire immediately: bash defers trap handlers until a foreground
    # child returns, which is exactly when we most want to shoot it. This is also
    # what lets cleanup() guarantee we never leave an orphaned capture.sh behind
    # streaming to the VPS with no supervisor — the failure mode that made a
    # dead-looking phone keep publishing.
    STARTED_AT="$(date +%s)"
    bash "$REPO/run/capture.sh" &
    CAPTURE_PID=$!
    supervise_capture "$STARTED_AT"
    wait "$CAPTURE_PID"
    rc=$?
    CAPTURE_PID=""
    # 78 = EX_CONFIG: a placeholder password or host that no amount of retrying
    # will fix. Back right off so the log stays readable until someone edits the
    # config, instead of reprinting the same error every 5s.
    if [ "$rc" = 78 ]; then
      log "capture exited (rc=78: config needs editing) — re-checking in ${CONFIG_RETRY:-60}s"
      nap "${CONFIG_RETRY:-60}"
      continue
    fi
    log "capture exited (rc=$rc) — restarting in ${RESTART_DELAY}s"
    nap "$RESTART_DELAY"
  done
}

main
