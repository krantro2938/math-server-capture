# assignment-reader

Point the camera at a sheet of paper and fire **one** `POST /start`. The service
then runs a background job — grab a frame → send it to Gemini → wait a second →
again — until the model says it has the whole assignment. You get it back as
structured JSON with all math in LaTeX, plus a straight answer at every step
about whether the page is fully in frame or the camera needs to move.

Every pass sees what has already been transcribed, so the job refines **one**
document instead of producing N independent guesses. State is written to disk
after every capture, so a crash or restart resumes exactly where it left off —
including an interrupted job.

```
POST /start ─┐
             ├─► grab frame ─► Gemini ─► merge ─► sleep 1s ─┐
             │        ▲                                      │
             │        └──────────────────────────────────────┘
             │                  until done
             └─► returns immediately (202); watch GET /events
```

## Endpoints

| Method | Path | Does |
|---|---|---|
| `POST` | `/start` | **Start here.** Runs captures in the background until the assignment is read. Returns `202` immediately. Optional body `{"note":"...", "interval_ms":1000, "max_captures":40}` |
| `POST` | `/stop` | Stop the running job (idempotent, takes effect immediately) |
| `GET` | `/events` | **SSE feed** — live progress while the job works (see below) |
| `POST` | `/capture` | One single capture, no job. For a manual check or to debug framing. Optional body `{"note":"..."}` |
| `POST` | `/photo` | **Read a PHOTO instead of the camera.** The request body IS the image (`image/jpeg\|png\|webp\|heic\|heif`). Resets first — a photo is a different sheet, and merging it into the transcription the camera has been building would interleave two assignments. `?reset=0` opts out, `?note=` passes a note |
| `GET` | `/assignment` | The assignment as JSON — the thing you're after |
| `GET` | `/assignment.md` | Same, rendered as Markdown + LaTeX |
| `GET` | `/state` | Everything, including per-capture history |
| `GET` | `/frame.jpg` | The last frame grabbed (see what the model saw) |
| `GET` | `/snapshot.jpg` | The camera **now** — for aiming. Costs a frame grab, never an API call. Served from a ~1s cache; `?max_age_ms=0` forces a fresh grab |
| `POST` | `/reset` | Archive the current attempt, start a clean version |
| `GET` | `/health` | Unauthenticated liveness check |

All endpoints except `/health` require the token, sent as `Authorization:
Bearer <token>`, `X-API-Token: <token>`, or `?token=`.

## The job

```bash
curl -s -X POST -H "X-API-Token: $TOKEN" http://127.0.0.1:8091/start
# {"ok":true,"started":true,"job":{...},"watch":"/events"}      ← returns in ~20ms
```

It ends on its own, and `job.reason` says why:

| reason | meaning |
|---|---|
| `done` | the model has the complete assignment — this is the good one |
| `max_captures` | hit the spend ceiling (default 40) without ever reaching `done` |
| `failed` | 5 consecutive failed captures (stream down, bad API key, …) |
| `stopped` | you called `/stop`, or `/reset` while it was running |

Guard rails worth knowing:

- **`max_captures` is a spend ceiling, not a target.** Every capture is a paid
  API call, and a camera pointed at a blank desk never reaches `done` — without
  the cap the job would bill forever. It counts captures for the current
  version, so resuming after a crash spends the *original* budget, not a fresh one.
- **`/start` on a finished assignment returns `409`**, telling you to `/reset`
  first, rather than quietly spending a call to re-confirm what's already known.
- **`/start` while a job runs returns `409`** — one job at a time.
- **An interrupted job resumes on restart** (`AUTO_RESUME=1`). If the process
  dies mid-job, it picks up where it stopped.
- **A capture in flight when you `/reset` is discarded**, not merged into the
  fresh assignment.

A `/capture` response — and the `assignment_updated` event — leads with what you
act on:

```jsonc
{
  "ok": true,
  "capture": {
    "n": 1,
    "camera_advice": "move_down",          // ok | move_up | move_down | move_left |
    "advice_detail": "The lower third of the sheet is out of frame; tilt down.",
    "cut_off_edges": ["bottom"],           //   move_right | move_closer | move_farther |
    "frame_quality": "good",               //   refocus | reduce_glare | reposition_paper
    "changes": ["added problem 1"],
    "confidence": 0.6
  },
  "needs_adjustment": true,   // false once the full page is visible and framing is ok
  "done": false,              // true when the model has the whole assignment
  "assignment": { "title": "...", "problems": [ ... ] }
}
```

The job keeps capturing on its own; `camera_advice` tells *you* how to nudge the
camera between passes, and the next capture picks up the improvement.

## Live feedback: `GET /events`

A capture takes a few seconds (frame grab, then the model call), so the progress
is pushed as Server-Sent Events instead of leaving you waiting on the POST. The
feed is independent of who triggered the capture: connect once, leave it open,
and watch every capture and reset as it happens. Several listeners can attach at
the same time.

```js
// EventSource can't set headers, so the token goes in the query string.
const es = new EventSource("/events?token=" + TOKEN);
es.addEventListener("snapshot",           e => render(JSON.parse(e.data))); // sent on connect
es.addEventListener("capture_started",    e => setStatus("grabbing frame…"));
es.addEventListener("frame_grabbed",      e => setStatus("reading it…"));
es.addEventListener("model_response",     e => showAdvice(JSON.parse(e.data)));
es.addEventListener("assignment_updated", e => render(JSON.parse(e.data)));
es.addEventListener("capture_failed",     e => showError(JSON.parse(e.data).error));
```

```bash
curl -N "http://127.0.0.1:8091/events?token=$TOKEN"     # watch from a terminal
```

| Event | When | Payload |
|---|---|---|
| `snapshot` | on connect | version, capture count, `done`, live `job` status |
| `job_started` | `/start` | `interval_ms`, `max_captures`, `note` |
| `job_resumed` | restart after a crash | `from_capture`, `max_captures` |
| `capture_started` | each pass begins | `n`, operator `note` |
| `frame_grabbed` | frame is in hand | `bytes`, `ms` |
| `model_request` | request sent | `model` |
| `model_response` | model replied | `camera_advice`, `advice_detail`, `cut_off_edges`, `changes`, `confidence`, `done`, `ms` |
| `assignment_updated` | state merged + saved | the full capture log + updated `assignment` |
| `done` | model says it has it all | `version`, problem count |
| `job_finished` | job ended | `reason` (`done`/`max_captures`/`failed`/`stopped`), final `assignment` |
| `capture_failed` | a pass failed (job keeps going) | `error` |
| `capture_discarded` | `/reset` landed mid-capture | `reason` |
| `reset` | `/reset` called | new `version` |

The stream sends `retry: 3000` (browsers reconnect on their own), a `: ping`
comment every 20s so idle connections aren't dropped, and
`X-Accel-Buffering: no` so a reverse proxy doesn't buffer it into uselessness.

## Run it

With the camera stack (recommended — it reaches MediaMTX by service name):

```bash
cd vps/docker
nano .env                 # GEMINI_API_KEY, ASSIGNMENT_TOKEN
docker compose --profile assignment up -d --build

curl -N "http://127.0.0.1:8091/events?token=$TOKEN" &            # watch
curl -s -X POST -H "X-API-Token: $TOKEN" http://127.0.0.1:8091/start
# ...job runs...
curl -s -H "X-API-Token: $TOKEN" http://127.0.0.1:8091/assignment | jq
```

Port 8091 is published on all interfaces so it can be reached from off-box —
`sudo ufw allow 8091/tcp`. `API_TOKEN` is the only gate, and this is plain
HTTP, so the token crosses the network in the clear: anyone who sniffs it can
read the assignment and spend Gemini calls via `/start`. Set
`ASSIGNMENT_BIND=127.0.0.1` in `.env` to keep it local and reach it over an SSH
tunnel instead, or put it behind the stack's Caddy for TLS.

### On the glasses

The [evens document server](../../evens/server) reads this one and renders the
assignment onto the Even Realities glasses, live, as the transcription fills
in — with the model's camera advice in the corner and tap-to-start/stop. Point
it here:

```bash
ASSIGNMENT_URL=http://<vps-ip>:8091 ASSIGNMENT_TOKEN=$TOKEN bun run start
```

It holds one SSE connection no matter how many glasses are watching, and keeps
the token on its side — `EventSource` can't send headers, so a client talking
to this service directly would have to carry the token in a query string.

Standalone:

```bash
cp .env.example .env && nano .env
bun --env-file=.env run server.ts
```

Needs `ffmpeg` on PATH (the Docker image installs it).

## Configuration

| Var | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | required |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | **Unverified id.** If Google answers `404 model not found`, change this — nothing else depends on it. Known-good: `gemini-2.5-flash-lite`, `gemini-2.5-flash`. |
| `SNAPSHOT_URL` | — | **preferred frame source**: the gateway's `/api/snapshot.jpg` |
| `SNAPSHOT_TOKEN` | — | must match `SNAPSHOT_TOKEN` on the gateway |
| `RTSP_URL` | `rtsp://viewer:…@mediamtx:8554/cam1` | fallback source, used only when `SNAPSHOT_URL` is empty (needs ffmpeg here) |
| `API_TOKEN` | — | shared secret; empty disables auth |
| `DATA_DIR` | `./data` | `state.json`, `assignment.json`, `last-frame.jpg`, `archive/` |
| `PORT` | `8091` | |
| `GRAB_TIMEOUT_MS` | `20000` | ffmpeg frame-grab timeout |
| `INTERVAL_MS` | `1000` | pause between captures in a job |
| `MAX_CAPTURES` | `40` | spend ceiling per job |
| `MAX_FAILURES` | `5` | consecutive failures before the job gives up |
| `SNAPSHOT_TTL_MS` | `1000` | how long `/snapshot.jpg` may reuse a frame. Captures ignore it and always grab fresh |
| `AUTO_RESUME` | `1` | resume an interrupted job on restart |

## How it works

0. `POST /start` records the job in `state.json` and returns; the loop runs in
   the background, one capture at a time, sleeping `interval_ms` between passes.
   `/stop` cuts the sleep short instead of waiting it out.
1. The frame comes from the **web gateway's** `GET /api/snapshot.jpg?max_age_ms=0`
   — that service already owns stream access and MediaMTX's credentials, so this
   one needs nothing but HTTP and could run on a different machine entirely.
   (`max_age_ms=0` refuses the gateway's cached frame: a capture must see the
   paper as it is now.) With `SNAPSHOT_URL` unset it falls back to running
   `ffmpeg -frames:v 1` against RTSP itself.
2. The frame plus the current transcription goes to Gemini with a
   `responseSchema`, so the reply is schema-constrained JSON — no prose parsing.
3. The model returns the **complete updated** assignment; that replaces the
   stored one, and the per-capture `changes` list is appended to history.
4. `state.json` is written via temp-file + rename, so a crash mid-write can't
   corrupt it. `assignment.json` is written alongside as a clean copy.
5. `/reset` archives to `data/archive/v<n>-<timestamp>.json` before clearing —
   flushing progress can't destroy a transcription you meant to keep.

A photo published through `POST /photo` is the same thing as a capture from
there on: same model call, same merge, same events, so nothing downstream has to
know where the frame came from. It is written to `/frame.jpg` too — that route
means "the frame the last capture read", and after a photo, that is the photo.

One capture runs at a time; a concurrent `POST /capture` gets `409` rather than
racing to merge against stale state.
