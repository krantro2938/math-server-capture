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
Note that after `setup-termux.sh` the supervisor **already starts itself on every
boot**, headless — Termux:Boot runs it with no session attached, so it is invisible
in the Termux UI even while it is streaming. Starting a second one by hand does
not double anything up; it makes both flap (see
[One at a time](#one-at-a-time)). `termux-run.sh` refuses to start in that case,
and `--force` takes over instead.

See [Running unattended](#running-unattended) for what retries what.

Then open the web page → **Live** tab (HLS) and **History** tab (see below).
Recordings are 15-min fMP4 segments in `/opt/mediamtx/recordings/cam1/` — parse
them however you like.

## The web UI

### History — one continuous recording

The History tab does not show files. MediaMTX's playback `/list` already
concatenates contiguous segments into unbroken **timespans**, so the page treats
the whole retention window as a single recording with holes in it: a scrubber
drawn in wall-clock time, a clock burned over the picture, and playback that
crosses file boundaries and gaps on its own.

- **Click** the timeline to jump to that instant, **drag** to pan, **scroll**
  (or pinch) to zoom from ~20 seconds to the full day. `All` fits everything
  kept; `⏭ Newest` jumps to the live edge; the date/time box goes to an exact
  moment.
- Blue is recorded, black is not, the amber dashed line is now. A run of thin
  bars means the stream was flapping then — usually two capture pipelines
  fighting (see [One at a time](#one-at-a-time)).
- Gaps are crossed automatically and named ("Skipped 3s with no recording"),
  so leaving it playing walks forward through the day.
- `−1m / −10s / +10s / +1m` seek inside the buffer when they can (instant) and
  re-request when they can't; speed goes to 8× for scanning.

Two properties of MediaMTX playback shape all of that, and are worth knowing
before changing it: `/get` is a progressive stream with `accept-ranges: none`
(so every seek off the buffer is a new request — the native seek bar is hidden
because it could not work), and it **stops dead at a gap** (so the page never
asks for a stretch that spans one). Its timestamps are also microsecond-precise
while `Date.parse` truncates to milliseconds, which is why `/api/timeline`
rounds range boundaries *inward* — a start rounded down by 0.4ms is a 404.

#### Why playback is chopped into minutes

**`/api/clip` size is the gateway's memory budget.** Bun's response writer is
eager: it drains the proxied body as fast as MediaMTX will send it and buffers
whatever the client has not taken, so a request costs ~1.5x its bytes in RSS no
matter how slowly the player reads. Measured against a 50 kB/s client, a 300s
clip took the container from 24MB to 205MB — and neither a pull-gated
`ReadableStream` (358MB) nor a `node:http` source with `pause()`/`resume()`
(397MB) changed it, because the buffering is downstream of both.

The first version of this tab asked for an hour at a time. On 2026-07-30 that
grew the container to ~1GB twice and the kernel OOM-killer took the whole 2GB
host down with it. So the page now asks for **60s at a time** and pre-loads the
next chunk into a second `<video>` while the current one plays, swapping at the
boundary — measured over 150s of playback: 2 hand-overs, no stalls, no dropped
frames, wall clock and media clock in lockstep. `MAX_CLIP_S` in `server.ts`
caps anything that asks for more, and `mem_limit: 512m` on the `web` service
means a future mistake here kills one container instead of the machine.

### Capture frame

Both players have a **📷 Capture frame** button. It grabs the frame you are
actually looking at (a canvas draw off the `<video>` — not `/api/snapshot.jpg`,
which opens its own RTSP session and would return a different moment, or
nothing at all while you are scrubbed back) and keeps it in a strip under the
player. The timestamp is the frame's, not the click's: live uses the HLS
`EXT-X-PROGRAM-DATE-TIME`, history uses the scrubber position.

Capture **saves; it does not download** — grabbing ten frames while you look for
the right one should not put ten files in Downloads. Each thumbnail carries its
own **⤓** button (on hover, and always on touch) that saves that one as
`cam-YYYY-MM-DD_HH-MM-SS.jpg`.

Saved photos live in **IndexedDB in that browser** — they are per-device and
per-browser, survive reloads, and never reach the VPS. Click one to open it
full size (arrow keys to move, `Esc` to close), where it can also be downloaded
or deleted; `Clear all` empties the store.

### Assignment — the transcription, as the glasses see it

The **Assignment** tab shows what the reader actually made of the sheet, drawn
by the document server's own renderer rather than described in text:

- **Pages** — one image per page of the glasses display, at its native 576×252.
  Same layout, same greys, same overlapping rows between pages. It is a
  pixel-exact picture of what is on the panel, which is why the images are never
  scaled up and only ever scaled down where a phone leaves no room for 1:1.
- **Whole sheet** — the same render uncut and at 2×, for reading here. The
  **Download** button saves it as `assignment-v<scan>-<date>.png`; each page has
  its own **Save** too.
- The picker lists the reader's scan archive, so an earlier attempt can be
  rendered without pointing the camera at the paper again. The live scan is the
  head of the list and follows the camera; the rest are fixed.

This is the tab for *reading the transcription before spending a solve on it* —
the glasses can show it, but not at a size or on a screen where checking it is
pleasant. Everything is proxied through this gateway (`/api/assignment/sheet*`,
session-gated) because the document server has no login of its own; the proxy is
a **whitelist**, not a wildcard forward, since `/assignment/*` over there also
holds the camera controls and the photo-publish endpoint.

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

### One at a time

Every layer above restarts things, so the one failure they cannot fix between
them is *too many* of them. Two capture pipelines do not share the load, they
fight: both log into the camera (which then reports `connectNum: 2`), and both
SRT-publish to the same MediaMTX path — where `overridePublisher` defaults to
`yes`, so each new publisher kicks the other off. Every few seconds one of them
dies with `Error submitting a packet to the muxer: I/O error` and a broken pipe,
restarts, and kicks the other. The site keeps showing video the whole time,
hitching, which is why this can run for hours before anyone calls it a fault.

The usual way in is invisible: Android kills the *supervisor* (the idlest process
in the tree) but leaves `capture.sh` running, re-parented to init. It has its own
retry loop, so the stream carries on with nothing supervising it, no session in
the Termux UI, and no obvious sign the phone is streaming at all — until you
start a second supervisor by hand and the two start fighting.

So `termux-run.sh` looks for a live pipeline (`capture.sh`, `lookcam_stream.py`,
or an ffmpeg holding `streamid=publish:`) as well as for another supervisor, and
refuses to start alongside one:

```bash
bash phone/termux-run.sh            # refuses (exit 3) and names what is running
bash phone/termux-run.sh --force    # stops that, orphans included, then takes over
pgrep -af 'termux-run|capture.sh|lookcam_stream|ffmpeg'   # see it yourself
```

A related trap worth knowing: `termux-wake-lock`/`unlock` are **app-wide**, not
per-process. A second instance that took the wake lock on the way in and released
it on exit would leave the *surviving* supervisor unprotected, and Android would
suspend it the next time the screen went off — a stream that dies minutes after
you put the phone down, with nothing in the log. The guard runs before the wake
lock is ever touched, which is what keeps that from happening.

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
| `web/server.ts` · `web/index.html` | Bun gateway + SPA: password login, live + [continuous-history DVR](#history--one-continuous-recording), [frame capture](#capture-frame), `GET /api/snapshot.jpg` (one still frame) |
| `assignment/` | Reads an assignment off the paper via Gemini — one `POST /start`, SSE progress, LaTeX output |
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
