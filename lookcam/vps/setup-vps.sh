#!/usr/bin/env bash
# Install & run MediaMTX on the VPS (Linux). Downloads the latest release,
# drops in our config, and starts it under systemd so it survives reboots.
#
#   sudo bash setup-vps.sh
set -euo pipefail

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64)  MM_ARCH="amd64" ;;
  aarch64|arm64) MM_ARCH="arm64v8" ;;
  armv7l)        MM_ARCH="armv7" ;;
  *) echo "unsupported arch: $ARCH"; exit 1 ;;
esac

DEST="/opt/mediamtx"
mkdir -p "$DEST/recordings"

echo "[*] resolving latest MediaMTX release for linux/${MM_ARCH}..."
URL="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
  | grep -oE "https://[^\"]*mediamtx_[^\"]*_linux_${MM_ARCH}\.tar\.gz" | head -n1)"
[ -n "$URL" ] || { echo "could not find release asset"; exit 1; }

echo "[*] downloading $URL"
curl -fsSL "$URL" | tar xz -C "$DEST" mediamtx

# Place the config next to the binary. Copy vps/mediamtx.yml from this repo:
if [ -f "$(dirname "$0")/mediamtx.yml" ]; then
  cp "$(dirname "$0")/mediamtx.yml" "$DEST/mediamtx.yml"
  echo "[*] installed mediamtx.yml — REMEMBER to change the two CHANGE_ME passwords"
else
  echo "[!] mediamtx.yml not found next to this script; copy it into $DEST manually"
fi

cat > /etc/systemd/system/mediamtx.service <<EOF
[Unit]
Description=MediaMTX (LookCam bridge)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=${DEST}
ExecStart=${DEST}/mediamtx ${DEST}/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mediamtx

echo
echo "[✓] MediaMTX running. Open these ports on your firewall/security group:"
echo "    1935/tcp (RTMP in)  8890/udp (SRT in)  8554/tcp (RTSP)  8888/tcp (HLS)  8889/tcp (WebRTC)"
echo "    Recordings -> ${DEST}/recordings/cam1/"
echo "    Watch      -> http://<VPS>:8888/cam1  (HLS)  or  rtsp://viewer:<pass>@<VPS>:8554/cam1"
echo "    Logs       -> journalctl -u mediamtx -f"
