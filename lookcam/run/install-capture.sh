#!/usr/bin/env bash
# Install run/capture.sh as a systemd service on the capture box (the Linux
# machine on the camera's LAN), so it starts on boot and restarts on failure.
#
#   sudo bash run/install-capture.sh
# (Edit run/config.env — or config.local.env — first: CAM_UID, CAM_PASS,
#  VPS_HOST, PUBLISH_PASS, MODE. On a phone use phone/setup-termux.sh instead;
#  Termux has no systemd.)
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

cat > /etc/systemd/system/lookcam-capture.service <<EOF
[Unit]
Description=LookCam capture -> VPS
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/env bash $DIR/run/capture.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now lookcam-capture
echo "[✓] capture service running (logs: journalctl -u lookcam-capture -f)"
