#!/usr/bin/env bash
# Shared single-instance guard for the phone-side services (gallery bridge,
# offline solver stack, camera capture). One implementation of the pattern
# lookcam/phone/termux-run.sh pioneered for the camera stream: refuse to
# start a second copy of a service — two of anything talking to the same
# camera, the same port, or the same VPS path is not "twice as reliable", it
# is a flap, with both copies fighting and neither working — and take over
# with --force instead of refusing, killing whatever was already running.
#
# termux-run.sh itself is NOT rewritten to use this: it works, it is what
# every phone deployment already runs, and there was no reason to put it at
# risk rewiring it onto a shared helper written after the fact. This is that
# same logic, generalised, for everything added since.
#
# Not meant to be run directly — source it, then call:
#
#   source ".../scripts/singleton-guard.sh"
#   singleton_guard "<service name>" "$PIDFILE" "<self-match>" "<pattern>" ["<pattern>" ...]
#
#   service name   for the messages ("gallery bridge")
#   PIDFILE        where this service records its own pid once it wins the
#                   guard — the caller writes it (with $$) immediately after
#                   singleton_guard returns, and removes it on exit
#   self-match     a substring of this service's OWN command line, used to
#                   confirm a pid found in PIDFILE is really a copy of this
#                   service and not some unrelated process Android has since
#                   reissued that pid to (pidfiles survive reboots; pids
#                   don't)
#   pattern...     one or more `pgrep -f` patterns matching every process
#                   this service's pipeline can leave running — not just the
#                   supervisor itself but whatever it launches, or a kill
#                   that only stops the parent is exactly how an orphan
#                   child gets left behind holding a socket
#
# Honors FORCE=1 (set by the caller's own arg parsing, same convention as
# termux-run.sh's --force): kills what it found and returns instead of
# refusing. Without it, prints who is running and `exit 3`.

_sg_describe_pid() {
  # 2>/dev/null on the pipeline itself, not on `tr`: a `<` redirect that fails
  # to open is reported by the shell before tr's own stderr redirection would
  # take effect, so putting it inline (`tr ... 2>/dev/null`) still leaks "No
  # such file or directory" for a pid that exited between pgrep listing it and
  # us reading /proc — this races constantly, and losing that race is normal,
  # not an error worth printing.
  { tr '\0\n' '  ' < "/proc/$1/cmdline" | cut -c1-90; } 2>/dev/null || true
}

# The pid recorded in a pidfile, or "" if missing/stale/foreign.
_sg_running_owner() {
  local pidfile="$1" self_match="$2" p
  p="$(cat "$pidfile" 2>/dev/null)" || return 0
  case "$p" in ''|*[!0-9]*) return 0 ;;
  esac
  [ "$p" = "$$" ] && return 0
  kill -0 "$p" 2>/dev/null || return 0
  case "$(_sg_describe_pid "$p")" in
    *"$self_match"*) echo "$p" ;;
    *) return 0 ;;
  esac
}

# Every pid matching any of the given patterns, except ourself and our parent
# shell (which match if either happens to have one of these words on its own
# command line — e.g. the shell that invoked us).
_sg_stray_pids() {
  local pat
  # `|| true`: the ordinary case is that nothing matches, and grep (with
  # pipefail on) then returns non-zero — which is exactly the result every
  # caller here assigns straight into a variable (`strays="$(_sg_stray_pids
  # ...)"`). Under `set -e` that specific form — an assignment whose RHS is a
  # failing command substitution — exits the whole calling script on the spot,
  # silently, before the caller ever gets to look at $strays. Callers that
  # only ever use this inside a `while`/`if` condition wouldn't need this (those
  # are exempt from errexit already), but the ones here don't, so make the
  # function itself never fail instead of relying on every call site to
  # remember to guard it.
  { for pat in "$@"; do pgrep -f "$pat"; done; } 2>/dev/null \
    | sort -un | grep -vx -e "$$" -e "$PPID" || true
}

singleton_guard() {
  local name="$1" pidfile="$2" self_match="$3"; shift 3
  local owner strays pid waited=0

  owner="$(_sg_running_owner "$pidfile" "$self_match")"
  strays="$(_sg_stray_pids "$@")"

  [ -z "$owner" ] && [ -z "$strays" ] && return 0

  if [ "${FORCE:-0}" != 1 ]; then
    echo "[!] REFUSING TO START — $name is already running:" >&2
    [ -n "$owner" ] && echo "[!]   supervisor pid $owner (from $pidfile)" >&2
    for pid in $strays; do
      echo "[!]   $pid $(_sg_describe_pid "$pid")" >&2
    done
    [ -z "$owner" ] && [ -n "$strays" ] &&
      echo "[!]   (no supervisor — an orphaned process, probably from a killed boot run)" >&2
    echo "[!] Starting a second one would make both flap. To take over, run with --force." >&2
    exit 3
  fi

  if [ -n "$owner" ]; then
    echo "[*] --force: stopping $name supervisor $owner" >&2
    kill "$owner" 2>/dev/null || true
  fi
  strays="$(_sg_stray_pids "$@")"
  if [ -n "$strays" ]; then
    echo "[*] --force: stopping $name: $(echo "$strays" | tr '\n' ' ')" >&2
    # SIGTERM first, so the target runs its own cleanup trap and reaps its
    # own children (an SRT push, an ffmpeg, a python subprocess) rather than
    # leaving them orphaned.
    kill $strays 2>/dev/null || true
    while [ -n "$(_sg_stray_pids "$@")" ] && [ "$waited" -lt 10 ]; do
      sleep 1; waited=$((waited + 1))
    done
    strays="$(_sg_stray_pids "$@")"
    if [ -n "$strays" ]; then
      echo "[*] --force: SIGKILL for stubborn $(echo "$strays" | tr '\n' ' ')" >&2
      kill -9 $strays 2>/dev/null || true
      sleep 1
    fi
  fi
}
