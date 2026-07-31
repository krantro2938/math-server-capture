#!/usr/bin/env bash
# Install the web frontend gateway as a systemd service on the VPS, behind
# Caddy for automatic HTTPS. Run AFTER setup-vps.sh (MediaMTX).
#
#   sudo bash vps/setup-web.sh yourdomain.com
set -euo pipefail
DOMAIN="${1:-}"
WEBDIR="$(cd "$(dirname "$0")/../web" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

# 1) bun (if missing) — installed for the invoking user
if ! command -v bun >/dev/null 2>&1 && [ ! -x "/home/$USER_NAME/.bun/bin/bun" ]; then
  echo "[*] installing bun for $USER_NAME..."
  sudo -u "$USER_NAME" bash -c 'curl -fsSL https://bun.sh/install | bash'
fi
BUN="/home/$USER_NAME/.bun/bin/bun"; command -v bun >/dev/null 2>&1 && BUN="$(command -v bun)"

# 2) require web/.env (edit it first: APP_PASSWORD, SESSION_SECRET, MTX_VIEW_PASS, PORT)
if [ ! -f "$WEBDIR/.env" ]; then
  echo "[!] create $WEBDIR/.env from .env.example first (set APP_PASSWORD, SESSION_SECRET, MTX_VIEW_PASS)."
  exit 1
fi
PORT="$(grep -E '^PORT=' "$WEBDIR/.env" | cut -d= -f2 | tr -d ' ')"; PORT="${PORT:-8090}"

# 3) systemd service
cat > /etc/systemd/system/lookcam-web.service <<EOF
[Unit]
Description=LookCam web frontend gateway
After=network-online.target mediamtx.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$WEBDIR
ExecStart=$BUN run server.ts
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now lookcam-web
echo "[✓] gateway running on 127.0.0.1:$PORT (logs: journalctl -u lookcam-web -f)"

# 4) Caddy for HTTPS (optional but recommended for a public VPS)
if [ -n "$DOMAIN" ]; then
  if ! command -v caddy >/dev/null 2>&1; then
    echo "[*] installing Caddy..."
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null 2>&1 || true
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -y >/dev/null && apt-get install -y caddy
  fi
  echo "$DOMAIN {
    reverse_proxy 127.0.0.1:$PORT
}" > /etc/caddy/Caddyfile
  systemctl restart caddy
  echo "[✓] HTTPS live at https://$DOMAIN  (Caddy auto-obtains a cert)"
else
  echo "[i] no domain given — skipping HTTPS. Pass a domain to enable Caddy:"
  echo "    sudo bash vps/setup-web.sh camera.example.com"
fi
