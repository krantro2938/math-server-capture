#!/data/data/com.termux/files/usr/bin/bash
# One-time Termux setup for the phone-as-capture-box.
#
# The phone hosts a WiFi hotspot the camera joins, pulls the camera's H.265 with
# lookcam_stream.py, and pushes it to the VPS. Nothing is transcoded on the phone
# (MODE=copy), so this is cheap enough to run 24/7 on an old handset.
#
#   bash ~/lookcam/phone/setup-termux.sh
#
# Prereqs you do ONCE in the Android UI (Termux can't do these for you):
#   - Install Termux + "Termux:Boot" from F-Droid (NOT the Play Store versions)
#   - Open Termux:Boot once, so Android grants it the boot permission
#   - Settings > Apps > Termux > Battery > Unrestricted (disable optimisation)
#   - Turn ON the phone's WiFi hotspot; join the camera to it via the LookCam
#     app once (so the camera remembers the hotspot SSID/password)
#   - Hotspot settings: DISABLE "turn off hotspot when no devices are connected"
#   - Keep the phone on a charger.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "[*] updating packages..."
pkg update -y && pkg upgrade -y
# clang is needed because aiohttp (an aiopppp dependency) has no prebuilt wheel
# for Android and gets compiled here.
pkg install -y python python-pip clang binutils ffmpeg iproute2 termux-tools nano git

echo "[*] installing aiopppp (this compiles aiohttp — a few minutes on a phone)..."
# NB: do NOT `pip install --upgrade pip` on Termux — it refuses (pip is managed
# by the python-pip package). Just install the library directly.
pip install "git+https://github.com/devbis/aiopppp"

echo "[*] verifying..."
python -c "import aiopppp, aiohttp; print('    aiopppp OK')"
ffmpeg -hide_banner -version | head -n1 | sed 's/^/    /'

# Config: keep real passwords in config.local.env, which capture.sh prefers.
if [ ! -f "$REPO/run/config.local.env" ]; then
  cp "$REPO/run/config.env" "$REPO/run/config.local.env"
  echo "[*] created run/config.local.env — EDIT IT (VPS_HOST, PUBLISH_PASS, CAM_UID)"
fi

# Auto-start on boot: Termux:Boot runs everything in ~/.termux/boot/ at boot.
mkdir -p "$HOME/.termux/boot"
BOOT="$HOME/.termux/boot/lookcam.sh"
cat > "$BOOT" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec bash "$REPO/phone/termux-run.sh"
EOF
chmod +x "$BOOT"

cat <<EOF

[✓] setup done.
    1. Edit the config:   nano $REPO/run/config.local.env
         VPS_HOST, PUBLISH_PASS (match vps/mediamtx.yml), CAM_UID, CAM_PASS
         Leave CAMERA_IP="auto" and MODE="copy".
         If discovery struggles on the hotspot, set EXTRA_BROADCAST="192.168.43.255".
    2. Sanity-check discovery (camera powered on and joined to the hotspot):
         python $REPO/discover.py --uid G683009DYDYB
    3. Start it:           bash $REPO/phone/termux-run.sh
       Watch the log:      tail -f ~/lookcam.log
    4. It also starts automatically on every reboot now (Termux:Boot -> $BOOT),
       headless — so after a reboot it is ALREADY streaming even though no
       Termux session shows it. Starting a second one would make both flap, so
       termux-run.sh refuses; add --force to take over instead.
EOF
