# gallery-bridge — the phone's camera roll, on localhost

Lives here rather than in the `evens` repo because it is phone-side tooling, and
this is where the phone-side tooling is: `../termux-run.sh` supervises the
camera capture the same way `run.sh` supervises this.

A web page cannot read a gallery. The most it can do is open a file picker,
which means picking the phone up and tapping twice — fine in the companion app,
where you are already holding it, and useless on the glasses, where the entire
point is not to.

So the phone runs a ~250-line Python script that serves its newest photos over
HTTP on loopback. The app runs **on that phone**, so it can just fetch it, and
the workflow becomes:

> shoot the sheet with the phone camera → look up → tap the temple

Nothing else in this repo depends on it. Without the bridge the companion app's
file picker still works and the glasses' Settings page says it is not
configured.

## Install

```bash
bash ~/lookcam/phone/gallery/setup.sh
```

That installs Python, asks Android for storage access, registers the boot hook
and prints the URL to paste. Then start it:

```bash
bash ~/lookcam/phone/gallery/run.sh      # the supervisor; Ctrl-C to stop
tail -f ~/gallery-bridge.log
```

Running `gallery.py` directly still works and is the right thing when you are
debugging it — you just get no supervision.

It prints the line you paste into the companion app:

```
gallery-bridge on http://127.0.0.1:8790
  ✓ /sdcard/DCIM/Camera
  ✗ /sdcard/Pictures

  paste this into the companion app's Photo tab:

    http://127.0.0.1:8790?t=Xf3k…
```

Open the companion app → **Photo** tab → *Phone gallery bridge* → paste → Save.
The glasses' Settings page reads the same setting; it is one web app on one
phone, so configuring it once configures both.

## Staying up

`gallery.py` is a plain HTTP server and does not try to be robust. It is robust
because of `run.sh`, which is what you actually run:

| Failure | What happens |
|---|---|
| a request handler raises | isolated per request — the server keeps serving |
| the process exits or is killed (OOM, crash) | supervisor restarts it after 5s |
| **alive but wedged** — not answering | a health probe every 30s; two consecutive misses and it is SIGKILLed and restarted |
| the port is already taken | exits 78, and the supervisor backs off to once a minute instead of hot-looping into the same collision |
| phone reboots | Termux:Boot runs the supervisor again |
| screen off / phone in pocket | `termux-wake-lock`, or Android suspends Termux within minutes |

Verified by killing it (`SIGTERM` → clean exit 0, `SIGKILL` → rc 137) and by
`SIGSTOP`ping it to simulate a server that is alive and deaf — the probe caught
that one and recovered on its own.

**What none of it can fix is Android killing Termux itself.** Nothing inside
Termux can restart Termux. That is what the battery step is for:

> Settings > Apps > Termux > Battery > **Unrestricted**

Skip it and this will be reliable for a while and then quietly not be there
when you reach for it — which is the worst of the available outcomes.

Tuning, all environment variables: `PROBE_EVERY` (30s), `PROBE_MISSES` (2),
`RESTART_DELAY` (5s), `CONFIG_RETRY` (60s), `LOG`, `MAX_LOG_BYTES` (2MB, one
old copy kept).

## Endpoints

| route | what |
|---|---|
| `GET /health` | is it up, and which directories exist. **No token** — the app has to be able to ask before it has been given one |
| `GET /recent.json?n=12` | the newest photos as metadata, no pixels |
| `GET /latest.json` | just the newest one's metadata |
| `GET /latest` | the newest photo itself |
| `GET /photo?id=…` | one specific photo from a listing |

## Why there is a token

Loopback binding stops anything **off** the phone reaching this. It does not
stop anything **on** it: without a token, any web page you happened to visit
while this was running could fetch `localhost:8790/latest` and read your camera
roll. So a token is generated on first run into `~/.evens-gallery-token`,
printed as part of the URL, and required on every route but `/health`.

`--allow-any` turns the check off. It is convenient while setting up and a bad
idea to leave on, which is why it says so on startup every time.

It never writes, deletes or moves anything, and serves only files under the
configured roots with an image extension. `id` is resolved against a fresh
listing rather than used as a path, so it cannot be walked out of the gallery.

## Options

| flag / env | default | |
|---|---|---|
| `--port` / `GALLERY_PORT` | `8790` | |
| `--host` / `GALLERY_HOST` | `127.0.0.1` | anything else exposes your camera roll to the network |
| `--roots` / `GALLERY_DIRS` | `DCIM/Camera`, `DCIM`, `Pictures`, `Download` | colon-separated in the env var |
| `--allow-any` | off | no token (see above) |
| `GALLERY_TOKEN` | generated | fix the token instead of reading the file |
