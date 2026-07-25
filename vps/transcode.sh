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
: "${BITRATE:=2000k}"

SRC="rtsp://${VIEW_USER}:${VIEW_PASS}@${MTX_HOST}:8554/${STREAM_KEY}_hevc"
DST="rtsp://${PUBLISH_USER}:${PUBLISH_PASS}@${MTX_HOST}:8554/${STREAM_KEY}"

echo "[*] transcoding ${STREAM_KEY}_hevc (HEVC) -> ${STREAM_KEY} (H.264 @ ${BITRATE})"
while true; do
  # Fails immediately while the capture box is offline (nothing published on the
  # _hevc path yet) — that's expected; we just keep retrying until it appears.
  ffmpeg -hide_banner -loglevel warning -rtsp_transport tcp -i "$SRC" \
    -c:v libx264 -preset veryfast -tune zerolatency -b:v "$BITRATE" -g 40 -pix_fmt yuv420p \
    -an -f rtsp -rtsp_transport tcp "$DST"
  echo "[!] transcode exited (source down?). retrying in 3s..."
  sleep 3
done
