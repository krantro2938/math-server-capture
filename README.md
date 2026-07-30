# lookcam — direct capture of a LookCam (CS2/PPPP) camera

**Status: SOLVED.** A pure-Python client (`lookcam_stream.py`) connects to a
LookCam-family camera on the LAN, logs in, starts the stream, and pulls live
**H.265 (HEVC) 1080p** video — no LookCam app, no cloud. From there it feeds a
normal ffmpeg → MediaMTX → web pipeline for remote viewing, recording, and a DVR.

Device: `G683009DYDYB` = `GHBB-683009-DYDYB` (CS2 "mykj", JSON commands), from
micro-cam.ru. Password `12345678`.

## How it was cracked
Reverse-engineered from the LookCam APK (jadx) + a real packet capture:
- **Transport:** PPPP over UDP (via `aiopppp`), with a one-line fix — this
  firmware's "ready" packet is `0x43`, not the standard `0x42`.
- **Commands (channel 1):** `f1 d0 <len>` · `d1 01 <idx>` · `a0 af af af` +
  18-byte header (not validated) + `<jsonlen LE>` + JSON + `f4 f3 f2 f1`.
  - login  → `{"cmd":"LoginDev","pwd":"…"}`
  - stream → `{"cmd":"OpenVideo","state":1,"pwd":"…","stream":1,"userid":N}`
- **Video (channel 0):** 1024-byte chunks, frames marked `01 af af af`, payload
  is Annex-B **HEVC**. (Many more commands exist: audio, PTZ, snapshot, SD
  playback, wifi, download — all in the decompiled `com.tkzn.look_cam.ppcs`.)

## Architecture

```
Camera ──LAN──► lookcam_stream.py ──HEVC──► ffmpeg ──► MediaMTX (VPS) ──► web/ ──► browser
   (H.265)        (capture box)                        record + HLS/WebRTC     live + DVR
```

The **capture box** is any Linux machine on the camera's network (Pi 4/5,
mini-PC, laptop, or a phone in Termux). Because browsers mostly can't play HEVC,
the stream is converted to **H.264** for the web — either on the capture box
(`MODE=h264`, needs some CPU) or on the VPS (`MODE=copy` + `vps/transcode.sh`,
for weak boxes; smaller uplink).

## Quick start

### 0. Prove it locally (optional)
```bash
python3 discover.py --uid G683009DYDYB          # prints its LAN IP
python3 -m venv .venv && .venv/bin/pip install "git+https://github.com/devbis/aiopppp"
.venv/bin/python lookcam_stream.py -a <CAM_IP> -p 12345678 --seconds 15 --out live.h265
ffmpeg -f hevc -i live.h265 -c copy out.mp4      # play out.mp4
```

### 1. VPS — MediaMTX + transcode + web frontend

**Docker (recommended).** One `.env`, one command, no host dependencies —
`vps/mediamtx.yml` keeps its `CHANGE_ME` placeholders and the real passwords are
injected as `MTX_*` env overrides at runtime.

```bash
git clone <this repo> ~/lookcam && cd ~/lookcam/vps/docker
cp .env.example .env && nano .env       # passwords, DOMAIN, ports
docker compose up -d                    # mediamtx + transcode + web
docker compose --profile caddy up -d    # ...and HTTPS on $DOMAIN
sudo systemctl enable docker            # so it all comes back after a reboot
```
`docker compose up -d --scale transcode=0` if the capture box runs `MODE=h264`
(no HEVC to convert). Logs: `docker compose logs -f`. Recordings land in
`vps/docker/recordings/cam1/`.

**Or bare-metal systemd**, if you'd rather not run Docker:
```bash
sudo bash vps/setup-vps.sh                # MediaMTX under systemd
sudo nano /opt/mediamtx/mediamtx.yml      # set the two CHANGE_ME passwords
sudo systemctl restart mediamtx

# MODE=copy only (phone/weak capture box): make browser-playable H.264 here
cp -n vps/config.env vps/config.local.env && nano vps/config.local.env   # same passwords
sudo bash vps/install-transcode.sh

cp -n web/.env.example web/.env && nano web/.env   # APP_PASSWORD, SESSION_SECRET,
                                                   # MTX_VIEW_PASS, PORT
sudo bash vps/setup-web.sh cam.example.com         # bun + systemd + Caddy HTTPS
```

Either way, open **8890/udp** (SRT in) in the firewall — plus **1935/tcp** only
if the capture box uses `MODE=h264`, and 80+443 for the web UI. Everything else
stays internal.

```bash
sudo ufw allow 8890/udp && sudo ufw allow 80,443/tcp
```

### 2. Capture box — start streaming
```bash
cp -n run/config.env run/config.local.env && nano run/config.local.env
#   CAMERA_IP="auto"  CAM_UID  CAM_PASS  VPS_HOST  PUBLISH_PASS  MODE
bash run/capture.sh                  # find camera → stream → push; retries forever
sudo bash run/install-capture.sh     # ...and as a boot service (systemd boxes)
```

**On a phone (Termux)** — no systemd, so use the supervisor instead:
```bash
pkg install git && git clone <this repo> ~/lookcam
bash ~/lookcam/phone/setup-termux.sh   # deps + config + Termux:Boot autostart
nano ~/lookcam/run/config.local.env    # VPS_HOST, PUBLISH_PASS, CAM_UID; MODE="copy"
bash ~/lookcam/phone/termux-run.sh     # runs forever; log: ~/lookcam.log
```
See [Running unattended](#running-unattended) for what retries what.

Then open the web page → **Live** tab (HLS) and **Recordings** tab (day timeline,
click to play, auto-advance). Recordings are 15-min fMP4 segments in
`/opt/mediamtx/recordings/cam1/` — parse them however you like.

## Running unattended

The capture side is built to be started before the camera even exists and left
alone. Four nested layers, each restarting the one inside it:

| Layer | Handles | How |
|-------|---------|-----|
| `lookcam_stream.py` | session drops, stalls, retransmits | reconnects to the known IP; `--give-up 60` exits(3) if that IP goes dead |
| `run/capture.sh` | camera absent, moved, or on a new DHCP lease | re-broadcasts (`discover.py --print-ip`) every 10s until it answers, then restarts the pipeline |
| `phone/termux-run.sh` | Termux/OOM/ffmpeg death, boot races | wake-lock, waits for a network, restarts `capture.sh` forever, rotates `~/lookcam.log` |
| Termux:Boot / systemd | reboots | `~/.termux/boot/lookcam.sh`, or `lookcam-capture.service` |

So: power-cycling the camera, roaming it to another hotspot, losing WiFi, or
rebooting the phone all recover on their own with no interaction.

On the **VPS**, `restart: unless-stopped` covers crashes, OOM kills and reboots
(with `systemctl enable docker`); `transcode.sh` also retries internally, so it
survives the capture box being offline for days. One deliberate gap: a container
you stop by hand (`docker stop`/`docker kill`) stays stopped until you start it
again — that's what "unless-stopped" means, and it's why `docker kill` is not a
valid test of the restart policy.

Phone specifics worth doing once: Termux + **Termux:Boot** from F-Droid (not
Play Store), Termux set to **Unrestricted** battery, hotspot's "turn off when no
devices connected" **disabled**, and the phone on a charger.

If discovery can't see the camera, run `python3 discover.py -v --timeout 10`
(without `--print-ip`) and read the first line: it lists every interface probed
and every device that answered, with the UID it reported. Two things it handles
by itself that used to need manual help — broadcast being routed onto Android's
mobile-data default route (it now sends from a socket bound to each interface),
and APs that drop client-to-client broadcast (it falls back to unicast-probing
every host on the attached /24s). `EXTRA_BROADCAST` in `run/config.local.env`
remains as a last resort for when the AP interface is missing from `ip addr`.

## Files

| Path | Role |
|------|------|
| **`lookcam_stream.py`** | **The working client** — connect → login → OpenVideo → clean HEVC (keyframe-gated, dedup, auto-reconnect, stall watchdog) |
| `discover.py` | LAN broadcast discovery — find the camera's IP (`--print-ip` for scripts) |
| `run/config.env` | **All capture-box settings** (copy to `config.local.env`) |
| `run/capture.sh` | Capture pipeline: find camera → client → ffmpeg → MediaMTX |
| `run/install-capture.sh` | systemd unit for the capture box |
| `phone/setup-termux.sh` · `phone/termux-run.sh` | Termux one-time setup · always-on supervisor |
| `vps/docker/docker-compose.yml` | **The whole VPS stack** — MediaMTX + transcode + web (+ optional Caddy) |
| `vps/docker/.env.example` | The only file on the VPS holding secrets |
| `vps/setup-vps.sh` | Install MediaMTX under systemd on the VPS (non-Docker path) |
| `vps/mediamtx.yml` | MediaMTX: ingest + record + playback, with auth |
| `vps/transcode.sh` · `vps/install-transcode.sh` | VPS HEVC→H.264 transcode (only for `MODE=copy`) + its systemd unit |
| `vps/setup-web.sh` | Install the web gateway + Caddy HTTPS |
| `web/server.ts` · `web/index.html` | Bun gateway + SPA: password login, live + DVR, `GET /api/snapshot.jpg` (one still frame) |
| `assignment/` | Reads an assignment off the paper via Gemini — one `POST /start`, SSE progress, LaTeX output. The sheet is covered by several close-up frames rather than fitted into one, and the model says where to point next |
| `client.py` · `run_aiopppp.py` · `probe_cmds.py` · `pcap_*.py` | RE tools used to crack the protocol (kept for reference) |

`stream` index: `0`=4K, `1`=2K/1080p, `2`=1080p (from the camera's `GetDevStream`).

## Notes
- **Codec is HEVC**, so storage is `-c copy` (full quality, no re-encode). Only
  the browser path needs H.264.
- The camera barely validates: the password and the 18-byte command header
  aren't checked — reaching it on the network is the only real gate.
- Long-term de-cloud alternative: reflash to open firmware (`Nemobi/Anyka`) for
  native RTSP — physical access + brick risk.

## References
- Palant, LookCam teardown — https://palant.info/2025/09/08/a-look-at-a-p2p-camera-lookcam-app/
- aiopppp — https://github.com/devbis/aiopppp
- MediaMTX — https://github.com/bluenviron/mediamtx
