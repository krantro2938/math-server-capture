#!/data/data/com.termux/files/usr/bin/bash
# One-time Termux setup for the gallery bridge.
#
#   bash ~/lookcam/phone/gallery/setup.sh
#
# Prereqs you do ONCE in the Android UI (Termux can't do these for you):
#   - Install Termux + "Termux:Boot" from F-Droid (NOT the Play Store versions)
#   - Open Termux:Boot once, so Android grants it the boot permission
#   - Settings > Apps > Termux > Battery > Unrestricted (disable optimisation)
#
# That last one is not optional if you want this to be there when you reach for
# it. The supervisor restarts the bridge whenever it exits, but nothing inside
# Termux can restart Termux — an Android that decides to reclaim the app takes
# the supervisor with it, and only the battery exemption prevents that.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[*] installing python..."
pkg install -y python

# The gallery lives on shared storage, which Termux cannot see until this has
# been run AND the Android permission dialog accepted.
if [ ! -d /sdcard/DCIM ]; then
  echo "[*] requesting storage access — ACCEPT the dialog Android shows..."
  termux-setup-storage || true
  sleep 3
fi

if [ -d /sdcard/DCIM ]; then
  echo "[✓] /sdcard/DCIM is readable"
else
  echo "[!] /sdcard/DCIM is still not readable — rerun 'termux-setup-storage' and accept"
fi

chmod +x "$HERE/run.sh"

# Auto-start on boot: Termux:Boot runs everything in ~/.termux/boot/ at boot.
mkdir -p "$HOME/.termux/boot"
BOOT="$HOME/.termux/boot/gallery-bridge.sh"
cat > "$BOOT" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec bash "$HERE/run.sh"
EOF
chmod +x "$BOOT"

# Print the URL to paste, generating the token now rather than making you go
# and find it in the log after the supervisor has swallowed the first start.
TOKEN_FILE="$HOME/.evens-gallery-token"
if [ ! -s "$TOKEN_FILE" ]; then
  python3 -c "import secrets,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(secrets.token_urlsafe(24)+'\n')" "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE" 2>/dev/null || true
fi
TOKEN="$(cat "$TOKEN_FILE")"

cat <<EOF

[✓] setup done.
    1. Start it:        bash $HERE/run.sh
       Watch the log:   tail -f ~/gallery-bridge.log
    2. In the companion app's Photo tab, under "Phone gallery bridge", paste:

         http://127.0.0.1:${GALLERY_PORT:-8790}?t=$TOKEN

    3. It restarts on its own if it dies or stops answering, and starts again
       on every reboot (Termux:Boot -> $BOOT).

    Do the battery step if you haven't:
      Settings > Apps > Termux > Battery > Unrestricted
    Without it Android will eventually kill Termux, and nothing in here can
    restart something that isn't running.
EOF
