#!/usr/bin/env bash
# Capture box -> VPS pipeline for the LookCam.
#
#   camera --(LAN)--> lookcam_stream.py (H.265) --> ffmpeg --> MediaMTX on the VPS
#
# Runs forever: finds the camera (retrying until it appears), streams it, and on
# any drop goes back to looking for it. Safe to start before the camera is even
# powered on.
#
# Works identically on a Linux box (Pi, mini-PC, laptop) and on a phone in
# Termux. Needs: python3 + aiopppp + ffmpeg.
#   pip install "git+https://github.com/devbis/aiopppp"     (or use the repo .venv)
#
#   bash run/capture.sh              # settings come from run/config.env
#
# OPTIONAL SECOND CAMERA (CAM_UID_FALLBACK in config.env): only one camera
# ever streams to the VPS at a time — SRT/RTMP has one publisher per path, and
# two pushing at once just fight over it (see termux-run.sh's "One at a time")
# — so this is real failover, not the two-independent-feeds trick
# run/dual_capture.sh uses for the offline local-snapshot pipeline. Every time
# the pipeline (re)starts, find_camera() tries the primary UID first and only
# falls to the fallback if it doesn't answer; and while the fallback is the one
# actually streaming, a background probe (watch_primary, every
# FAILOVER_RECHECK seconds) keeps checking for the primary and ends the round
# the moment it's back, so the outer loop picks it straight back up. Only
# active when CAMERA_IP=auto (the default) — a hardcoded CAMERA_IP has nowhere
# to fail over TO — and CAM_UID must be a real UID, not "any camera", or
# "primary is back" has no camera to mean.
set -uo pipefail
cd "$(dirname "$0")/.."          # repo root (where lookcam_stream.py lives)

# ---- config: run/config.local.env wins over the committed run/config.env ----
CONF="run/config.env"
[ -f run/config.local.env ] && CONF="run/config.local.env"
# shellcheck source=config.env
if [ -f "$CONF" ]; then . "$CONF"; else echo "[!] missing $CONF"; exit 1; fi
: "${CAMERA_IP:=auto}"      ; : "${CAM_UID:=}"    ; : "${CAM_PASS:=12345678}"
: "${STREAM:=1}"            ; : "${VPS_HOST:?set it in $CONF}"
: "${STREAM_KEY:=cam1}"     ; : "${PUBLISH_USER:=publisher}"
: "${PUBLISH_PASS:?set it in $CONF}"
: "${MODE:=copy}"           ; : "${FPS:=15}" ; : "${CRF:=20}" ; : "${MAXRATE:=5000k}"
: "${DISCOVER_TIMEOUT:=5}"  ; : "${DISCOVER_RETRY:=10}" ; : "${EXTRA_BROADCAST:=}"
: "${EXTRA_SUBNET:=}"  ; : "${SRT_LATENCY:=120000}"
# Second camera, and how often to check for the primary while streaming it —
# see the failover note above. Empty CAM_UID_FALLBACK (the default) disables
# the feature entirely: find_camera() behaves exactly as it always has.
: "${CAM_UID_FALLBACK:=}"   ; : "${FAILOVER_RECHECK:=60}"

PYTHON="./.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v ffmpeg >/dev/null || { echo "[!] ffmpeg not installed"; exit 1; }

# The :? guards above only catch an *empty* value — and run/config.env ships a
# CHANGE_ME placeholder, so they never fired. The result was the worst possible
# failure shape: discovery worked, the camera logged in and streamed, and only
# the last hop died, with an opaque SRT "I/O error" repeating forever. Reject
# placeholders up front instead, before we touch the camera at all.
# 78 = EX_CONFIG: the supervisor treats it as "stop hammering, a human must fix
# this" rather than a transient fault to retry in 5s.
for _v in PUBLISH_PASS VPS_HOST STREAM_KEY; do
  case "$(eval "printf '%s' \"\$$_v\"")" in
    *CHANGE_ME*|*change-me*|*example.com*)
      echo "[!] $_v is still a placeholder — edit $CONF"
      echo "    PUBLISH_PASS must equal PUBLISH_PASS in the VPS's vps/docker/.env"
      echo "    (bare-metal: the 'publisher' pass in /opt/mediamtx/mediamtx.yml)."
      exit 78 ;;
  esac
done

case "$MODE" in
  h264)
    # Quality + low latency: CRF keeps detail close to the camera's native HEVC,
    # while -tune zerolatency drops x264's ~40-frame lookahead + B-frames (which
    # otherwise add ~2-3s of delay). 1s GOP (-g = FPS) lets MediaMTX cut short
    # HLS segments for lower latency. 1080p15 is light — even a Pi 4 keeps up.
    PUSH=(-c:v libx264 -preset medium -tune zerolatency -profile:v high -crf "$CRF"
          -maxrate "$MAXRATE" -bufsize "$MAXRATE" -g "$FPS" -keyint_min "$FPS"
          -sc_threshold 0 -pix_fmt yuv420p
          -an -f flv "rtmp://${VPS_HOST}:1935/${STREAM_KEY}?user=${PUBLISH_USER}&pass=${PUBLISH_PASS}")
    ;;
  copy)
    # Push raw HEVC to a *_hevc source path; vps/transcode.sh turns it into the
    # browser-ready H.264 path the frontend reads.
    #
    # -muxdelay/-muxpreload 0 matter more than they look: ffmpeg's mpegts muxer
    # defaults to a 0.7s preload, which is 0.7s of pure latency added here for
    # no benefit on a live push. -flush_packets emits each packet immediately
    # instead of letting the muxer accumulate.
    PUSH=(-c:v copy -an -f mpegts -muxdelay 0 -muxpreload 0 -flush_packets 1
          "srt://${VPS_HOST}:8890?streamid=publish:${STREAM_KEY}_hevc:${PUBLISH_USER}:${PUBLISH_PASS}&pkt_size=1316&latency=${SRT_LATENCY}")
    ;;
  *) echo "bad MODE=$MODE (want copy or h264)"; exit 1 ;;
esac

# Discovery args for one UID — shared by find_camera() and watch_primary()
# below so the two can't quietly drift apart on --broadcast/--subnet.
_discover_args() {
  local uid="$1" b
  [ -n "$uid" ] && printf '%s\n' "--uid" "$uid"
  for b in $EXTRA_BROADCAST; do printf '%s\n' "--broadcast" "$b"; done
  for b in $EXTRA_SUBNET; do printf '%s\n' "--subnet" "$b"; done
}

# Resolve which camera to stream from, blocking until one answers a LAN
# broadcast. This is what makes the setup survivable unattended: the phone can
# boot with no camera in range, a camera can be unplugged and moved, DHCP can
# hand it a new lease — we just keep probing until something shows up again.
#
# Echoes "IP ROLE" (role: primary | fallback | fixed). Tries the primary UID
# first on every cycle — even a cycle that starts right after we've been
# streaming the fallback for a while — so whichever this returns already IS
# the preferred camera available *right now*; the caller doesn't separately
# track "should I switch back yet".
find_camera() {
  if [ "$CAMERA_IP" != "auto" ]; then echo "$CAMERA_IP fixed"; return 0; fi
  local ip try=0
  local -a primary_args=() fallback_args=()
  mapfile -t primary_args < <(_discover_args "$CAM_UID")
  [ -n "$CAM_UID_FALLBACK" ] && mapfile -t fallback_args < <(_discover_args "$CAM_UID_FALLBACK")

  while true; do
    try=$((try + 1))
    # Let discovery's own diagnostics through on the FIRST attempt only: they
    # name the interfaces probed and any device that answered with a different
    # UID — which is exactly what you need to see when nothing is found — but
    # repeating that every 10s forever would bury the log.
    if [ "$try" = 1 ]; then
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${primary_args[@]}")"
    else
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${primary_args[@]}" 2>/dev/null)"
    fi
    if [ -n "$ip" ]; then
      echo "$ip primary"; return 0
    fi

    if [ -n "$CAM_UID_FALLBACK" ]; then
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${fallback_args[@]}" 2>/dev/null)"
      if [ -n "$ip" ]; then
        echo "$ip fallback"; return 0
      fi
    fi

    echo "[.] no camera on the network yet (attempt $try) — retrying in ${DISCOVER_RETRY}s..." >&2
    sleep "$DISCOVER_RETRY"
  done
}

# Runs ONLY while the fallback camera is the one actually streaming (see the
# main loop below). A plain discovery probe every FAILOVER_RECHECK seconds —
# cheap, and never touches the active pipeline — that ends this round the
# moment the primary answers, so the outer loop's find_camera() re-runs and
# (per the comment above) picks the primary straight back up.
watch_primary() {
  local -a args=()
  mapfile -t args < <(_discover_args "$CAM_UID")
  while true; do
    sleep "$FAILOVER_RECHECK"
    local ip
    ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${args[@]}" 2>/dev/null)"
    if [ -n "$ip" ]; then
      echo "[*] primary camera is back at $ip — switching off the fallback" >&2
      return 0
    fi
  done
}

echo "[*] capture: primary=${CAM_UID:-any} fallback=${CAM_UID_FALLBACK:-none} ip=${CAMERA_IP} stream=${STREAM} -> ${VPS_HOST} (${MODE})"

# A plain `python … | ffmpeg …` only completes when BOTH ends exit, so half a
# dead pipeline hangs the whole supervisor. That is not hypothetical: bringing a
# VPN up on the phone killed ffmpeg's SRT socket, ffmpeg quit, the Python side
# kept happily sending P2PAlive into a pipe nobody was reading, and the stream
# stayed down until someone noticed by hand. Run the two as explicit jobs
# instead, wait for whichever dies first, and take the survivor with it.
FIFO=""
cleanup() {
  [ -n "${PY_PID:-}" ] && kill "$PY_PID" 2>/dev/null
  [ -n "${FF_PID:-}" ] && kill "$FF_PID" 2>/dev/null
  [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2>/dev/null
  [ -n "$FIFO" ] && rm -f "$FIFO"
}
trap 'cleanup; exit 0' INT TERM

while true; do
  read -r CAM_ADDR CAM_ROLE <<< "$(find_camera)"
  echo "[*] camera at ${CAM_ADDR} (${CAM_ROLE}) — starting stream"

  FIFO="$(mktemp -u "${TMPDIR:-/tmp}/lookcam.XXXXXX")"
  mkfifo "$FIFO" || { echo "[!] cannot create fifo"; sleep 5; continue; }

  "$PYTHON" lookcam_stream.py -a "$CAM_ADDR" -p "$CAM_PASS" --stream "$STREAM" --pipe \
    > "$FIFO" &
  PY_PID=$!
  # -probesize/-analyzeduration are capped deliberately. ffmpeg's defaults are
  # 5MB / 5s, and -fflags nobuffer does not override them: on a raw HEVC stream
  # it sits there swallowing seconds of video before it emits anything, and
  # (with -use_wallclock_as_timestamps) then flushes a burst already several
  # seconds old. That is a startup cost, but this pipeline restarts constantly —
  # every camera drop, every re-find, every supervisor retry — so it is really a
  # recurring one. Raw HEVC needs only the first VPS/SPS/PPS + a frame to be
  # probed, which fits in far less than 200KB.
  ffmpeg -hide_banner -loglevel warning \
      -fflags +genpts+nobuffer -flags low_delay \
      -probesize 200000 -analyzeduration 200000 \
      -use_wallclock_as_timestamps 1 \
      -f hevc -i "$FIFO" "${PUSH[@]}" &
  FF_PID=$!

  WAIT_PIDS=("$PY_PID" "$FF_PID")
  WATCH_PID=""
  # Only worth watching for something better while we're already on the worse
  # option — streaming the primary already IS the preferred outcome, so a
  # watcher here would have nothing to switch to and would never fire.
  if [ "$CAM_ROLE" = "fallback" ] && [ -n "$CAM_UID_FALLBACK" ]; then
    watch_primary &
    WATCH_PID=$!
    WAIT_PIDS+=("$WATCH_PID")
  fi

  # Whichever exits first — camera drop, SRT death, OOM, a hang we SIGTERM, or
  # (on the fallback) the primary coming back — ends the round. Then kill the
  # rest so we never leak a half-pipeline or a watcher with nothing left to watch.
  wait -n "${WAIT_PIDS[@]}" 2>/dev/null
  kill "${WAIT_PIDS[@]}" 2>/dev/null
  wait "${WAIT_PIDS[@]}" 2>/dev/null
  rm -f "$FIFO"; FIFO=""; PY_PID=""; FF_PID=""; WATCH_PID=""

  echo "[!] pipeline exited (camera/network drop, or a better camera came back). re-finding in 3s..."
  sleep 3
done
