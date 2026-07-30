# assignment-reader

Point the camera at a sheet of paper and fire **one** `POST /start`. The service
then runs a background job — grab a frame → send it to Gemini → wait a second →
again — until the model says it has the whole assignment. You get it back as
structured JSON with all math in LaTeX, plus a straight answer at every step
about which part of the sheet still needs to be shown to the camera.

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

## The sheet is read a piece at a time

**No frame has to hold the whole page.** A camera close enough to read
handwriting cannot fit an A4 sheet in one shot, and demanding one was the
reader's central mistake: the transcription would be finished and correct while
`done` stayed false, because no single frame had ever shown the page end to end,
and every job ran to its capture ceiling.

So the sheet is covered rather than photographed. Each frame reports which
**edges of the paper** it can see, and the union accumulates across the whole
attempt:

```
frame 1: top ▔▔▔▔  sees top, left, right     edges seen: top left right
frame 2: bottom ▁▁ sees bottom, left, right  edges seen: ALL FOUR → covered
```

Two close-up frames, neither of them containing the sheet, and between them the
paper has been seen from edge to edge. That — plus every problem complete, plus
the model reporting no writing running off a frame it hasn't followed — is what
`done` now means.

Meanwhile the model says **where to point next**: `coverage.next_target` is one
sentence naming the part of the sheet it still needs ("show the bottom of the
page, below problem 7"), and `camera_advice` is the move that gets you there.
The prompt is explicit that a close, readable part of the page beats a distant
view of all of it — "move_farther" is for text so large you can't tell where you
are, never for making the sheet fit.

If the gate is wrong — a torn edge the model won't call an edge, a sheet that
runs under the edge of the desk — `POST /complete` is the operator saying
"that's all of it". No API call, no frame: it stops the job and marks what has
been transcribed as final.

## Endpoints

| Method | Path | Does |
|---|---|---|
| `POST` | `/start` | **Start here.** Runs captures in the background until the assignment is read. Returns `202` immediately. Optional body `{"note":"...", "interval_ms":1000, "max_captures":40}` |
| `POST` | `/stop` | Stop the running job (idempotent, takes effect immediately) |
| `GET` | `/events` | **SSE feed** — live progress while the job works (see below) |
| `POST` | `/capture` | One single capture, no job. For a manual check or to debug framing. Optional body `{"note":"..."}` |
| `POST` | `/photo` | **Read a PHOTO instead of the camera.** The request body IS the image (`image/jpeg\|png\|webp\|heic\|heif`). **Merges** into the current assignment, exactly as a camera frame does — several photos of one sheet is the ordinary way to read it. `?reset=1` starts a new version instead (a different sheet), `?note=` passes a note |
| `POST` | `/complete` | **"That's all of it."** Marks the current transcription final without a frame or an API call. The override for when the coverage gate can't be satisfied |
| `GET` | `/assignment` | The assignment as JSON — the thing you're after, plus a `coverage` block saying what is still unseen |
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
- **A job that ends on `max_captures` with the transcription looking finished**
  usually means an edge of the sheet was never shown to the camera. `GET
  /assignment` names which one in `coverage.edges_unseen` — point the camera
  there and `/start` again, or `POST /complete` if the paper really does end
  there.
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
    "next_target": "Show the bottom of the page, below problem 7.",
    "camera_advice": "move_down",          // ok | move_up | move_down | move_left |
    "advice_detail": "Problem 8 starts below the frame.",
    "region": "top two thirds",            //   move_right | move_closer | move_farther |
    "sheet_edges_visible": ["top","left","right"],  // refocus | reduce_glare |
    "more_content_beyond": ["bottom"],     //   reposition_paper
    "cut_off_edges": ["bottom"],
    "frame_quality": "good",
    "changes": ["added problem 1"],
    "confidence": 0.6
  },
  "needs_adjustment": true,   // false once the whole sheet has been covered
  "done": false,              // true when the model has the whole assignment
  "assignment": { "title": "...", "problems": [ ... ] }
}
```

`next_target` is the sentence to act on and `camera_advice` is the direction to
move; `sheet_edges_visible` is what this frame contributed to coverage, and
`more_content_beyond` is the model saying it can see the writing continue past
the frame. The job keeps capturing on its own, so the next pass picks up
wherever you moved to.

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
| `snapshot` | on connect | version, capture count, `done`, live `job` status, `edges_seen`/`edges_unseen`, `next_target` |
| `job_started` | `/start` | `interval_ms`, `max_captures`, `note` |
| `job_resumed` | restart after a crash | `from_capture`, `max_captures` |
| `capture_started` | each pass begins | `n`, operator `note` |
| `frame_grabbed` | frame is in hand | `bytes`, `ms` |
| `model_request` | request sent | `model` |
| `model_response` | model replied | `next_target`, `camera_advice`, `advice_detail`, `region`, `sheet_edges_visible`, `more_content_beyond`, `cut_off_edges`, `changes`, `confidence`, `done`, `ms` |
| `assignment_updated` | state merged + saved | the full capture log + updated `assignment`, `edges_seen`, `open_edges`, `next_target` |
| `done` | the sheet is covered and every problem read | `version`, problem count; `by: "operator"` when it came from `POST /complete` |
| `job_finished` | job ended | `reason` (`done`/`max_captures`/`failed`/`stopped`), final `assignment` |
| `capture_failed` | a pass failed (job keeps going) | `error` |
| `capture_discarded` | `/reset` landed mid-capture | `reason` |
| `done_rejected` | the model claimed `done` and the gate refused | `reason`, `edges_unseen`, `open_edges` |
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

## Four readers, tried in order

```
gemini-3.5-flash-lite  →  gemini-3.1-flash-lite  →  mistral-medium-latest  →  mistral-small-2506
└──────────── GEMINI_API_KEY ────────────┘        └────────── MISTRAL_API_KEY ──────────┘
```

A capture costs one API call **per rung reached**, and a rung is only ever reached
because the one before it genuinely failed — never a routine double-spend. Which
one answered is reported: `model_response` carries `provider` and `model`, each
`captures[]` entry in `/state` records them, and a `model_fallback` event fires
for every rung that failed.

Why a second *provider* and not just a second Google model: a second Gemini model
does nothing about an outage at Google, a revoked key or a region-wide 429 — and
those are exactly the failures that leave a camera pointed at a sheet of paper
transcribing nothing at all.

**How far a failure skips.** Inside one provider only a recoverable failure
(404 / 429 / 5xx, or unusable output) earns another call: a 400 means this code
built a request that provider rejects, and its sibling model would reject it
identically. But that is not a reason to *stop* — an invalid Google key is a
`400 API key not valid`, not a 401 — so anything unrecoverable here skips the rest
of this provider's models and tries the other one, whose endpoint, key and schema
dialect are all different. One exception, because the two APIs disagree: a model
id nobody knows is a **404 at Google and a 400 at Mistral**, so Mistral's error
body is checked for `invalid_model` and treated as recoverable. A renamed id is
the most likely way this chain ever breaks, and it must not take the sibling down
with it.

Mistral gets the same prompt and the same schema (as strict `json_schema`, which
additionally forbids unknown keys), and its reply is shape-checked before it is
merged — "enforced" is a claim by the thing being checked. It cannot read HEIC,
so `POST /photo` from an iPhone gallery is Gemini's job; a camera frame is always
JPEG.

## Configuration

| Var | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | required |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | **Unverified id.** If Google answers `404 model not found`, change this — nothing else depends on it. Known-good: `gemini-2.5-flash-lite`, `gemini-2.5-flash`. |
| `GEMINI_MODEL_FALLBACK` | `gemini-3.1-flash-lite` | second Gemini attempt, only when the first fails recoverably |
| `MISTRAL_API_KEY` | — | enables the **backup provider**. Empty and the reader is Gemini-only, exactly as before |
| `MISTRAL_MODEL` | `mistral-medium-latest` | third attempt overall |
| `MISTRAL_MODEL_FALLBACK` | `mistral-small-2506` | fourth and last |
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
2. The frame, the current transcription AND the coverage so far — which edges
   of the sheet have been seen, which parts have been read, which problems are
   still half-read — go to Gemini with a `responseSchema`, so the reply is
   schema-constrained JSON and no prose is parsed. If that call fails, **the
   next reader in the chain is tried** (below).
3. The model returns the **complete updated** assignment; that is merged over
   the stored one (a problem never goes backwards — see `mergeAssignment`), the
   edges this frame saw are added to the running coverage set, and the
   per-capture `changes` list is appended to history.
4. `state.json` is written via temp-file + rename, so a crash mid-write can't
   corrupt it. `assignment.json` is written alongside as a clean copy.
5. `/reset` archives to `data/archive/v<n>-<timestamp>.json` before clearing —
   flushing progress can't destroy a transcription you meant to keep.

A photo published through `POST /photo` is the same thing as a capture from
there on: same model call, same merge, same coverage accounting, same events, so
nothing downstream has to know where the frame came from — and several photos of
one sheet accumulate into one assignment exactly as several camera frames do. It is written to `/frame.jpg` too — that route
means "the frame the last capture read", and after a photo, that is the photo.

One capture runs at a time; a concurrent `POST /capture` gets `409` rather than
racing to merge against stale state.
