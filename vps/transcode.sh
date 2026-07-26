#!/usr/bin/env bash
# Only needed when the capture box runs run/capture.sh with MODE=copy (it pushes
# raw H.265 to the "<key>_hevc" path). This pulls that HEVC and republishes a
# browser-friendly H.264 path "<key>" that the web frontend + recordings use.
#
# Run on the VPS, alongside MediaMTX. Settings live in vps/config.env (or
# config.local.env). Install it as a service with vps/install-transcode.sh.
#
#   bash vps/transcode.sh
set -uo pipefail
cd "$(dirname "$0")"

CONF="config.env"; [ -f config.local.env ] && CONF="config.local.env"
# shellcheck source=config.env
if [ -f "$CONF" ]; then . "$CONF"; else echo "[!] missing vps/$CONF"; exit 1; fi
: "${STREAM_KEY:=cam1}"    ; : "${MTX_HOST:=127.0.0.1}"
: "${PUBLISH_USER:=publisher}" ; : "${PUBLISH_PASS:?set it in vps/$CONF}"
: "${VIEW_USER:=viewer}"       ; : "${VIEW_PASS:?set it in vps/$CONF}"
: "${BITRATE:=2000k}"          ; : "${GOP_SECONDS:=1}"
: "${FPS:=15}"                 ; : "${PRESET:=veryfast}"
GOP_FRAMES=$(( FPS * GOP_SECONDS )); [ "$GOP_FRAMES" -ge 1 ] || GOP_FRAMES=15

SRC="rtsp://${VIEW_USER}:${VIEW_PASS}@${MTX_HOST}:8554/${STREAM_KEY}_hevc"
DST="rtsp://${PUBLISH_USER}:${PUBLISH_PASS}@${MTX_HOST}:8554/${STREAM_KEY}"

echo "[*] transcoding ${STREAM_KEY}_hevc (HEVC) -> ${STREAM_KEY} (H.264 @ ${BITRATE}," \
     "keyframe every ${GOP_SECONDS}s / ${GOP_FRAMES}f, preset ${PRESET})"
while true; do
  # Fails immediately while the capture box is offline (nothing published on the
  # _hevc path yet) — that's expected; we just keep retrying until it appears.
  #
  # Keyframe cadence is the whole ballgame for latency. HLS cannot cut a segment
  # anywhere but a keyframe, so this interval — not hlsSegmentDuration — decides
  # how long segments really are, and the player then sits a multiple of that
  # behind live. The old -g 40 meant 2.7s segments at 15fps and made the 1s
  # setting in mediamtx.yml a dead letter.
  #
  # Both a frame count AND a time expression, deliberately: the camera's
  # delivered rate wanders either side of 15fps, so a pure -g would drift the
  # segment length with it. -force_key_frames pins keyframes to real seconds,
  # and scenecut is disabled so nothing inserts extras in between (irregular
  # segments upset LL-HLS part timing).
  #
  # -r/-fps_mode cfr pin the OUTPUT rate. Without them ffmpeg inferred a rate
  # from the phone's wallclock timestamps and landed on ~49fps, tripling every
  # frame from a 15fps camera: the bitrate got spread over 3x the frames it
  # should cover (visibly worse picture for the same bandwidth), and the stream
  # advertised 1920x1080@49fps, which is enough for a stricter browser's
  # MediaSource check to reject the init segment and render nothing at all.
  ffmpeg -hide_banner -loglevel warning \
    -fflags nobuffer -flags low_delay -avioflags direct \
    -probesize 500000 -analyzeduration 500000 \
    -rtsp_transport tcp -i "$SRC" \
    -c:v libx264 -preset "$PRESET" -tune zerolatency -b:v "$BITRATE" \
    -r "$FPS" -fps_mode cfr \
    -g "$GOP_FRAMES" -keyint_min "$GOP_FRAMES" -sc_threshold 0 \
    -force_key_frames "expr:gte(t,n_forced*${GOP_SECONDS})" \
    -pix_fmt yuv420p -flush_packets 1 \
    -an -f rtsp -rtsp_transport tcp "$DST"
  echo "[!] transcode exited (source down?). retrying in 3s..."
  sleep 3
done
