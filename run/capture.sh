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

PYTHON="./.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v ffmpeg >/dev/null || { echo "[!] ffmpeg not installed"; exit 1; }

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
    PUSH=(-c:v copy -an -f mpegts
          "srt://${VPS_HOST}:8890?streamid=publish:${STREAM_KEY}_hevc:${PUBLISH_USER}:${PUBLISH_PASS}&pkt_size=1316")
    ;;
  *) echo "bad MODE=$MODE (want copy or h264)"; exit 1 ;;
esac

# Resolve the camera's IP, blocking until it answers a LAN broadcast. This is
# what makes the setup survivable unattended: the phone can boot with no camera
# in range, the camera can be unplugged and moved, DHCP can hand it a new lease
# — we just keep probing until it shows up again.
find_camera() {
  if [ "$CAMERA_IP" != "auto" ]; then echo "$CAMERA_IP"; return 0; fi
  local args=() ip try=0 b
  [ -n "$CAM_UID" ] && args+=(--uid "$CAM_UID")
  for b in $EXTRA_BROADCAST; do args+=(--broadcast "$b"); done
  while true; do
    try=$((try + 1))
    # Let discovery's own diagnostics through on the FIRST attempt only: they
    # name the interfaces probed and any device that answered with a different
    # UID — which is exactly what you need to see when nothing is found — but
    # repeating that every 10s forever would bury the log.
    if [ "$try" = 1 ]; then
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${args[@]}")"
    else
      ip="$("$PYTHON" discover.py --print-ip --timeout "$DISCOVER_TIMEOUT" "${args[@]}" 2>/dev/null)"
    fi
    if [ -n "$ip" ]; then
      echo "$ip"; return 0
    fi
    echo "[.] camera not on the network yet (attempt $try) — retrying in ${DISCOVER_RETRY}s..." >&2
    sleep "$DISCOVER_RETRY"
  done
}

echo "[*] capture: uid=${CAM_UID:-any} ip=${CAMERA_IP} stream=${STREAM} -> ${VPS_HOST} (${MODE})"
while true; do
  CAM_ADDR="$(find_camera)"
  echo "[*] camera at ${CAM_ADDR} — starting stream"
  "$PYTHON" lookcam_stream.py -a "$CAM_ADDR" -p "$CAM_PASS" --stream "$STREAM" --pipe \
    | ffmpeg -hide_banner -loglevel warning \
        -use_wallclock_as_timestamps 1 -fflags +genpts \
        -f hevc -i - "${PUSH[@]}"
  echo "[!] pipeline exited (camera/network drop?). re-finding camera in 3s..."
  sleep 3
done
