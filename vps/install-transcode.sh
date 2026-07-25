#!/usr/bin/env bash
# Install vps/transcode.sh as a systemd service. Only needed when the capture box
# runs MODE=copy (phone / weak box pushes H.265 and the VPS makes the H.264 that
# browsers can play). Skip this entirely if the capture box runs MODE=h264.
#
#   sudo bash vps/install-transcode.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

command -v ffmpeg >/dev/null || { echo "[*] installing ffmpeg..."; apt-get update -y && apt-get install -y ffmpeg; }
[ -f "$DIR/config.local.env" ] || [ -f "$DIR/config.env" ] || { echo "[!] no vps/config.env"; exit 1; }

cat > /etc/systemd/system/lookcam-transcode.service <<EOF
[Unit]
Description=LookCam HEVC->H.264 transcode (MODE=copy)
After=network-online.target mediamtx.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/env bash $DIR/transcode.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now lookcam-transcode
echo "[✓] transcode service running (logs: journalctl -u lookcam-transcode -f)"
