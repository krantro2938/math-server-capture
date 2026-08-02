# utils — Even Realities glasses: camera capture + math solving

One project, three directories, two ways to run it. It reads an assignment off
a physical sheet of paper through a camera, solves it, and shows the working on
a pair of Even Realities G2 glasses — either **online** (a real camera feed +
Gemini reads the page, Claude solves it, a VPS renders and serves everything)
or **offline** (a phone in Termux does all three jobs itself, no internet
required).

```
evens/     the glasses app + the document server (renders markdown -> tiles)
lookcam/   camera capture: online streaming to a VPS, AND the offline
           local-snapshot pipeline this repo added for the Camera page
offline/   the phone-local math solver (Ollama + SymPy-free direct reasoning)
           and the offline camera-preview renderer
scripts/   small shared shell helpers (currently: the single-instance guard
           every phone-side service uses — see below)
```

Each directory has its own README with the real detail — **`evens/README.md`**
and **`lookcam/README.md`** are the places to go deep. This file is the map:
what talks to what, and the order to bring it all up in.

## The two modes

| | Online | Offline |
|---|---|---|
| Camera reading | `lookcam/run/capture.sh` streams one camera to the VPS, with genuine failover to a second (`CAM_UID_FALLBACK`) if the first stops answering — only one ever pushes to the VPS at a time, see `run/config.env`. A Gemini-backed reader transcribes it live with framing advice. | `lookcam/run/dual_capture.sh` keeps a rolling local JPEG from each of up to two cameras and just serves whichever is freshest — no advice, publish a photo instead (see below) |
| Solving | A Claude routine (cloud), triggered by a tap | `offline/solver.py`, driving Qwen2.5-Math through Ollama, on the same phone |
| Serving the glasses | `evens/server` (VPS), renders tiles server-side with headless Chromium | `offline/solver.py --serve` on the phone, renders tiles with Pillow (`offline/render.py`, `offline/camera_render.py`) |
| Switching | `evens/test/src/services/backend.ts` — `auto` picks whichever is reachable, `online`/`offline` pin it. Persisted in the app; changeable from Settings. | |

The glasses app **never knows which mode it's in** beyond that router: every
fetch goes through `serverUrl()`, which is either the VPS domain or
`http://localhost:8384` (the phone). Same endpoints, same JSON shapes, on both
sides — see `evens/README.md`'s "Where every variable lives" table for the
online side's config, and `offline/config.env` for the offline side's.

## Setting up the server side (online mode)

This is the VPS: MediaMTX (camera ingest), the lookcam web gateway + Gemini
reader, and the evens document server. Follow **`lookcam/README.md`**'s Quick
Start section 1, then **`evens/README.md`**'s "Run it" section for the
document server. Bring the camera stack up first — the document server's
`ASSIGNMENT_URL`/`ASSIGNMENT_TOKEN` point at it and it degrades gracefully
(503, not a crash) if the reader isn't there yet, but there's nothing for the
glasses to solve without it.

## Setting up the phone side

The phone runs some subset of four independent services, depending on which
mode(s) you want. All four now guard against a second copy of themselves —
same pattern, described once below.

| Service | What it does | Needed for |
|---|---|---|
| `lookcam/phone/termux-run.sh` | Streams the primary camera to the VPS (online reading) | Online mode |
| `lookcam/phone/gallery/run.sh` | Serves the phone's camera roll on localhost, for "publish the latest photo" (both the companion web app and the glasses Settings page use it) | Either mode, if you publish photos instead of / alongside live scanning |
| `lookcam/run/dual_capture.sh` | Keeps one rolling JPEG per camera (primary + fallback) on disk | Offline mode, if you want the Camera page's live preview |
| `offline/start.sh` | Ollama + `solver.py --serve` — the offline math brain and local document/camera server | Offline mode |

None of these talk to each other directly except through files on disk
(`dual_capture.sh`'s snapshots) or the glasses app's own mode switch — you can
run any subset. A typical **fully offline** phone runs `dual_capture.sh` (if
you have cameras to preview) and `start.sh`; a typical **online capture box**
runs `termux-run.sh` and, if you want to publish photos too, `gallery/run.sh`.

### Single-instance guard, once, for all four

Two copies of any of these fight instead of helping — over a camera login, a
port, or a VPS publish path — and the usual way it happens is invisible:
Android kills the *supervisor* (the idlest thing in the tree) but leaves its
child running, re-parented to init, with nothing watching it and no sign in
the Termux UI that anything is still streaming. `lookcam/phone/termux-run.sh`
solved this first (see its own README section, "One at a time"); `scripts/singleton-guard.sh`
is that same logic shared by the other three:

```bash
bash lookcam/phone/gallery/run.sh        # refuses (exit 3) and names what's running
bash lookcam/phone/gallery/run.sh --force    # stops it, orphans included, takes over
bash lookcam/run/dual_capture.sh [--force]
bash offline/start.sh [--force]
```

`termux-run.sh` itself keeps its own original, independent implementation of
this (untouched, still the reference) rather than being rewired onto the
shared script after the fact.

### Setting up offline mode specifically

1. **Models, while you have internet** — `bash offline/setup-termux.sh` (Pillow,
   matplotlib, llama.cpp build deps, a Pillow/matplotlib smoke test), then
   `bash offline/setup-models.sh` to pull the Qwen2.5-Math and vision (OCR)
   Ollama models.
2. **Cameras** — copy `lookcam/run/config.env` to `config.local.env` and set
   `CAM_UID` (primary) and `CAM_UID_FALLBACK` (the second physical camera, if
   you have one — `discover.py -v` finds a UID you don't already know). Then
   `bash lookcam/run/dual_capture.sh`. Skip this step entirely if you'd rather
   just photograph the sheet (next section) — the Camera page's live preview
   is a nice-to-have, not a requirement.
3. **The solver** — `bash offline/start.sh`. Starts Ollama, waits for it, then
   `solver.py --serve` on `:8384`, wired to whatever `dual_capture.sh` is
   writing to `~/lookcam-snapshots/`.
4. **On the glasses app**, switch mode to `offline` (or `auto`, which follows
   whichever backend answers `/health`) from Settings.
5. **Get an assignment onto the phone**: either aim a configured camera at the
   sheet and use the Camera page's live preview, or — the always-available
   path, since it doesn't need a camera at all — photograph the sheet with the
   phone and publish it (the companion web app's Photo tab, or the glasses
   Settings page's gallery import; both need `gallery/run.sh` running). Either
   way ends with `POST /assignment/photo`, OCR'd locally by the vision model,
   and the AI page's "Solve" button lighting up.

Offline mode has **no framing advice and no incremental page-coverage
tracking** — those are Gemini-backed features of the online reader with no
local equivalent. The Camera page's live preview is genuinely just a picture;
getting the assignment transcribed is the one-shot photo-publish flow either
way.

## Where things live, if you're debugging

- Camera won't discover: `python3 lookcam/discover.py -v --timeout 10` (lists
  every interface probed and every device that answered).
- Offline solve won't start / says "no assignment": nothing has published a
  photo yet — check `GET http://localhost:8384/solution/status` reports
  `no_assignment` rather than `idle` with zero problems (the two look similar
  but only one means "there's really nothing to solve yet").
- Offline camera preview 503s: `GET http://localhost:8384/assignment/status`
  says which camera (if either) is fresh; `~/lookcam-snapshots/*.jpg` mtimes
  are the ground truth `solver.py` reads.
- Online reader/camera issues: `lookcam/README.md`'s own sections cover the
  VPS side (MediaMTX, the web gateway, `vps/transcode.sh`) in depth.
- Which repo branch is real: `github.com/krantro2938/math-server-capture` has
  two branches with **unrelated histories**. `master` is the combined monorepo
  tree (`evens/` + `lookcam/` + `offline/` + `scripts/`, as siblings — this
  branch) and is what's actually deployed. `main` is GitHub's default branch
  but an abandoned, pre-restructuring lookcam-only tree (no `evens/`, no
  `offline/`) — a plain `git clone` checks it out by mistake. If a nested
  `lookcam/.git` shows files as "modified" even when the working tree matches
  this repo exactly, it's comparing against that abandoned `main` lineage, not
  real drift — don't trust its status/diff, and don't commit there.
