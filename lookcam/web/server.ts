// Gateway + frontend for the LookCam bridge.
//
// One public port. Serves the SPA, handles a shared-password login, and proxies
// to MediaMTX (which stays bound to localhost) injecting the viewer credentials:
//   - live:        HLS  ->  http://MTX:8888/<path>/*
//   - history:     playback /list + /get on http://MTX:9996 — /api/timeline is
//                  "when does video exist", /api/clip is "give me that stretch"
//   - snapshot:    GET /api/snapshot.jpg -> one JPEG off the live stream, so
//                  anything that wants a still (the assignment reader, a
//                  dashboard, a cron job) asks HTTP instead of speaking RTSP
//   - photo:       GET /api/photo[/meta] -> the sheet last published from the
//                  companion app, shown and downloaded in the Photo tab
//
// Run with Bun:   bun run server.ts   (or `bun run start`)
// Configure via env (see .env.example). Nothing here is camera-specific.

import { createHmac, timingSafeEqual } from "node:crypto";

const CFG = {
  port: Number(process.env.PORT ?? 8080),
  appPassword: process.env.APP_PASSWORD ?? "changeme",
  sessionSecret: process.env.SESSION_SECRET ?? "please-set-a-long-random-secret",
  sessionHours: Number(process.env.SESSION_HOURS ?? 720), // 30 days
  mtxHost: process.env.MTX_HOST ?? "127.0.0.1",
  hlsPort: Number(process.env.MTX_HLS_PORT ?? 8888),
  playbackPort: Number(process.env.MTX_PLAYBACK_PORT ?? 9996),
  streamPath: process.env.STREAM_PATH ?? "cam1",
  mtxUser: process.env.MTX_VIEW_USER ?? "viewer",
  mtxPass: process.env.MTX_VIEW_PASS ?? "CHANGE_ME_VIEW",
  rtspPort: Number(process.env.MTX_RTSP_PORT ?? 8554),
  // Lets a service (the assignment reader) fetch snapshots without a browser
  // session. Empty = no service access; browser cookie still works.
  snapshotToken: process.env.SNAPSHOT_TOKEN ?? "",
  // Snapshots are served from a short cache so ten callers don't spawn ten
  // ffmpegs. Callers that need a guaranteed-fresh frame pass ?max_age_ms=0.
  snapshotTtlMs: Number(process.env.SNAPSHOT_TTL_MS ?? 1000),

  // The document server (the evens stack), which owns the hand-written Adri
  // documents. It sits on this compose network, so the default is the container
  // — no TLS hop, and the browser never talks to it directly.
  //
  // PROXIED RATHER THAN CALLED FROM THE PAGE on purpose: this app is behind a
  // password and that one is not, so a browser calling it straight would put an
  // editing surface on the public internet that this login does nothing to
  // protect. Going through here means the session cookie gates it.
  evensUrl: process.env.EVENS_URL ?? "http://evens:8787",
  snapshotTimeoutMs: Number(process.env.SNAPSHOT_TIMEOUT_MS ?? 20000),

  // Sending a message to the glasses is the one thing on the document server
  // that must NOT be open: it puts text on someone's face. That server gates
  // POST /messages on this token, and this process is the only thing that holds
  // it — so the send path is "logged into cam.aansl.com" and nothing else.
  // Must match MESSAGE_TOKEN in the evens stack's .env.
  messageToken: process.env.MESSAGE_TOKEN ?? "",
};

const mtxAuth = "Basic " + Buffer.from(`${CFG.mtxUser}:${CFG.mtxPass}`).toString("base64");
const COOKIE = "camsess";

// ---- tiny signed-cookie session -------------------------------------------
function sign(exp: number): string {
  const mac = createHmac("sha256", CFG.sessionSecret).update(String(exp)).digest("base64url");
  return `${exp}.${mac}`;
}
function valid(token: string | undefined): boolean {
  if (!token) return false;
  const dot = token.indexOf(".");
  if (dot < 0) return false;
  const exp = Number(token.slice(0, dot));
  if (!Number.isFinite(exp) || Date.now() > exp) return false;
  const expected = sign(exp);
  const a = Buffer.from(token);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
function cookies(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of (req.headers.get("cookie") ?? "").split(";")) {
    const i = p.indexOf("=");
    if (i > 0) out[p.slice(0, i).trim()] = decodeURIComponent(p.slice(i + 1).trim());
  }
  return out;
}
const authed = (req: Request) => valid(cookies(req)[COOKIE]);

// ---- MediaMTX proxy helpers -----------------------------------------------
async function proxyHls(rest: string): Promise<Response> {
  // rest is the part after /live/, INCLUDING any query string — MediaMTX's
  // low-latency HLS needs the ?session=… param, so it must be forwarded intact
  // (e.g. "index.m3u8" or "video1_stream.m3u8?session=…" or "segment3.mp4").
  const url = `http://${CFG.mtxHost}:${CFG.hlsPort}/${CFG.streamPath}/${rest}`;
  const upstream = await fetch(url, { headers: { Authorization: mtxAuth } });
  const headers = new Headers(upstream.headers);
  headers.delete("www-authenticate");
  return new Response(upstream.body, { status: upstream.status, headers });
}

// ---- the history timeline --------------------------------------------------
//
// What the page needs is not a file listing but "when does video exist" — the
// answer it draws a DVR scrubber from. MediaMTX's /list is already that shape:
// it concatenates contiguous segment FILES into one entry each, so a run of
// eight 15-minute files that never dropped comes back as a single two-hour
// timespan, and a new entry only starts where the publisher actually broke.
//
// So this is a normaliser, not a builder. Two things are worth doing here
// rather than in the browser:
//
//   - Overlaps are real. When two capture pipelines fight over the same path
//     (see "One at a time" in the README) MediaMTX records both, and /list
//     hands back timespans that genuinely overlap — negative gaps. Drawn
//     unmerged those stack into a barcode; merged, they are one range.
//   - Everything else is left EXACTLY as MediaMTX reported it. Merging two
//     spans across a real hole would be a lie the player then has to live
//     with: /get stops dead at a gap, so a clip requested across one ends
//     early and the page would re-request the same spot forever. The rule is
//     "never ask for a stretch MediaMTX won't serve in one piece".
async function playbackTimeline(from: Date, to: Date): Promise<Response> {
  const qs = new URLSearchParams({
    path: CFG.streamPath,
    start: from.toISOString(),
    end: to.toISOString(),
  });
  const url = `http://${CFG.mtxHost}:${CFG.playbackPort}/list?${qs}`;

  let raw: Array<{ start: string; duration: number }> = [];
  try {
    const upstream = await fetch(url, { headers: { Authorization: mtxAuth } });
    // A path that has never recorded 404s here. That is "nothing yet", not an
    // error worth showing — the page says "no recordings" either way.
    if (upstream.ok) raw = (await upstream.json()) as typeof raw;
  } catch { /* MediaMTX down: same empty answer, and the live tab will say so */ }

  // MediaMTX stamps these with MICROSECOND precision and Date.parse keeps only
  // milliseconds — truncating, so a parsed start is up to 1ms EARLIER than the
  // real one. That is not a rounding curiosity: playback /get 404s on an
  // instant that predates every segment, so a start reported as
  // 09:59:52.497 (really .49736) is a range whose first frame cannot be
  // fetched. Measured: .497 -> 404, .498 -> 200. Boundaries are rounded INWARD —
  // starts up, ends down — and a range then only ever names instants MediaMTX
  // will actually serve.
  const subMs = (iso: string) => {
    const m = /\.(\d+)/.exec(iso);
    return !!m && m[1].length > 3 && /[1-9]/.test(m[1].slice(3));
  };
  const spans = raw
    .map((s) => {
      const t = Date.parse(s.start);
      return { s: t + (subMs(s.start) ? 1 : 0), e: t + Math.floor(s.duration * 1000) };
    })
    .filter((r) => Number.isFinite(r.s) && r.e > r.s)
    .sort((a, b) => a.s - b.s);

  const ranges: Array<{ start: string; end: string; duration: number }> = [];
  let cur: { s: number; e: number } | null = null;
  const flush = () => {
    if (cur) ranges.push({
      start: new Date(cur.s).toISOString(),
      end: new Date(cur.e).toISOString(),
      duration: (cur.e - cur.s) / 1000,
    });
  };
  for (const r of spans) {
    if (cur && r.s <= cur.e) cur.e = Math.max(cur.e, r.e);   // overlap only
    else { flush(); cur = { ...r }; }
  }
  flush();

  // `now` is the server's clock, and the page anchors the live edge of the
  // scrubber to it. Sending it means a browser whose clock is minutes out
  // draws the timeline in the right place anyway.
  return Response.json({
    now: new Date().toISOString(),
    from: from.toISOString(),
    to: to.toISOString(),
    ranges,
  });
}

// THE SIZE OF THIS REQUEST IS THE ONLY THING BOUNDING THIS PROCESS'S MEMORY.
// Read that before raising the cap.
//
// A proxied body is not streamed through Bun in the sense that matters here:
// Bun drains the source as fast as MediaMTX will send it and buffers whatever
// the client has not taken yet. A video player reads a recording at ~400 kB/s;
// MediaMTX, on the same docker network reading off local disk, pushes it a
// hundred times faster, and the difference lands in this heap.
//
// Measured on the VPS, one 300s clip against a client reading at 50 kB/s:
//
//   new Response(upstream.body)                        24MB -> 205MB
//   ReadableStream, highWaterMark 1 (pull-gated)       38MB -> 358MB
//   node:http source with socket pause()/resume()      47MB -> 397MB
//
// So it is not fetch() and it is not the source: the response writer is eager,
// and no amount of backpressure upstream of it changes the outcome. The heap
// cost is ~1.5x the bytes requested, whatever the client does.
//
// That is what killed the box on 2026-07-30. The History tab shipped asking for
// hour-long clips — ~1.4GB of video — and the kernel OOM-killed this container
// twice (anon-rss 1089612kB) on a 2GB host with no swap, taking every other
// service down with it.
//
// The page therefore asks for ~60s at a time and stitches the chunks together
// (double-buffered, so the joins are invisible). This cap exists for the URLs
// the page does not control: a stale bookmark, a retry, someone curling by
// hand. 90s is ~36MB of video, ~55MB of heap.
const MAX_CLIP_S = 90;

async function playbackClip(start: string, duration: string, signal: AbortSignal): Promise<Response> {
  const secs = Math.min(Math.max(Number(duration) || 0, 1), MAX_CLIP_S);
  const qs = new URLSearchParams({
    path: CFG.streamPath,
    start,
    duration: String(secs),
    format: "fmp4",
  });
  const url = `http://${CFG.mtxHost}:${CFG.playbackPort}/get?${qs}`;
  // The signal is what stops MediaMTX reading on for a viewer who has already
  // seeked somewhere else — every seek abandons the request before it.
  const upstream = await fetch(url, { headers: { Authorization: mtxAuth }, signal });
  const headers = new Headers(upstream.headers);
  headers.delete("www-authenticate");
  if (!headers.has("content-type")) headers.set("content-type", "video/mp4");
  return new Response(upstream.body, { status: upstream.status, headers });
}

// ---- snapshot (one JPEG off the live stream) -------------------------------
// MediaMTX has no still-image endpoint, so we decode a single frame with
// ffmpeg. Kept here rather than in each consumer: this process already holds
// the MediaMTX credentials, so nothing else needs them or needs to speak RTSP.
let snapCache: { at: number; jpeg: Buffer } | null = null;
let snapInFlight: Promise<Buffer> | null = null;

async function grabJpeg(): Promise<Buffer> {
  const rtsp = `rtsp://${encodeURIComponent(CFG.mtxUser)}:${encodeURIComponent(CFG.mtxPass)}` +
               `@${CFG.mtxHost}:${CFG.rtspPort}/${CFG.streamPath}`;
  const proc = Bun.spawn([
    "ffmpeg", "-hide_banner", "-loglevel", "error",
    "-rtsp_transport", "tcp", "-i", rtsp,
    "-frames:v", "1", "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "-",
  ], { stdout: "pipe", stderr: "pipe" });
  const timer = setTimeout(() => proc.kill(), CFG.snapshotTimeoutMs);
  try {
    const [buf, err] = await Promise.all([
      new Response(proc.stdout).arrayBuffer(),
      new Response(proc.stderr).text(),
    ]);
    await proc.exited;
    const jpeg = Buffer.from(buf);
    if (jpeg.length === 0) throw new Error(`no frame — is the stream publishing? ${err.trim()}`);
    return jpeg;
  } finally {
    clearTimeout(timer);
  }
}

async function snapshot(maxAgeMs: number): Promise<Buffer> {
  if (snapCache && Date.now() - snapCache.at <= maxAgeMs) return snapCache.jpeg;
  // Coalesce concurrent callers onto one ffmpeg run.
  if (!snapInFlight) {
    snapInFlight = grabJpeg()
      .then((jpeg) => { snapCache = { at: Date.now(), jpeg }; return jpeg; })
      .finally(() => { snapInFlight = null; });
  }
  return snapInFlight;
}

const json = { "content-type": "application/json" };
const html = { "content-type": "text/html; charset=utf-8" };

// ---- filename for a downloaded photo ---------------------------------------
// The page passes the name the phone knew (?name=IMG_4821.HEIC) so the file that
// lands in Downloads is the one you took. Sanitised rather than trusted: it ends
// up in a Content-Disposition header, where a quote or a newline is a header
// injection and a slash is someone else's directory.
const EXT: Record<string, string> = {
  "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
  "image/heic": "heic", "image/heif": "heif",
};

function downloadName(url: URL, mime: string): string {
  const raw = (url.searchParams.get("name") ?? "").split(/[\\/]/).pop() ?? "";
  const safe = raw.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "").slice(0, 80);
  if (safe) return safe;
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  return `assignment-${stamp}.${EXT[mime.split(";")[0].trim()] ?? "jpg"}`;
}

// ---- routing ---------------------------------------------------------------
const INDEX = await Bun.file(new URL("./index.html", import.meta.url)).text();
const LOGIN = await Bun.file(new URL("./login.html", import.meta.url)).text();

// The two standalone Claude prompts, served to the Prompts tab. They used to be
// a pair of hand-escaped string literals in index.html, copied from the .md
// files by hand — which is exactly how evens/routine/solve.md came to be a
// generation behind the prompt actually in use. One copy now: prompts/*.md is
// the source, this reads it, the tab fetches it.
//
// `## Prompt` is the extraction contract, the same one routine/render-prompt.sh
// uses: everything after that heading is the prompt, everything above it is
// commentary for whoever opens the file. Read once at startup like INDEX, so a
// change needs a restart (not a rebuild — web/ is bind-mounted into /app).
//
// A bad prompt file must NOT be fatal. This process also serves the live feed
// and the DVR, and a missing heading in a markdown file is no reason for the
// camera page to crash-loop — so each prompt fails on its own, loudly in the
// log, and its card shows the reason instead of stale or wrong text.
async function promptSection(file: string): Promise<string> {
  const md = await Bun.file(new URL(`./prompts/${file}`, import.meta.url)).text();
  const m = md.match(/^## Prompt$/m);
  if (!m || m.index === undefined) {
    throw new Error(`no '## Prompt' heading — the tab would otherwise show commentary`);
  }
  const body = md.slice(m.index + m[0].length).replace(/^\s*\n/, "").trimEnd();
  if (!body) throw new Error(`the '## Prompt' section is empty`);
  return body + "\n";
}

const PROMPT_FILES = {
  solve: "solve-assignment-prompt.md",
  extract: "extract-assignment-prompt.md",
} as const;

const PROMPTS: Record<string, string> = Object.fromEntries(
  await Promise.all(
    Object.entries(PROMPT_FILES).map(async ([key, file]) => {
      try {
        return [key, await promptSection(file)];
      } catch (e: any) {
        const why = `prompts/${file}: ${e?.message ?? e}`;
        console.error(`[prompts] ${why}`);
        return [key, `This prompt could not be loaded.\n\n${why}\n`];
      }
    }),
  ),
);

Bun.serve({
  port: CFG.port,

  // Bun.serve closes a connection after `idleTimeout` seconds without traffic,
  // AND THE DEFAULT IS 10. That default took this whole site down the day the
  // message widget shipped: /api/messages/events is an SSE stream that is
  // silent between messages, so Bun killed it from underneath itself every ten
  // seconds — `[Bun.serve]: request timed out after 10 seconds` in this
  // container's log, `EOF ... status 502` in Caddy's.
  //
  // It did not stop at the stream. Caddy keeps upstream connections alive and
  // reuses them, so every connection Bun timed out was one Caddy still believed
  // in: the next request on it — /live/index.m3u8, /api/messages, /api/doc/* —
  // died at the same EOF without ever reaching this process. A live stream and
  // two document tabs went 502 because of an endpoint neither of them touches.
  //
  // The evens document server hit exactly this and fixed it exactly here (see
  // IDLE_TIMEOUT_S in evens/server/index.ts). Bun caps the value at 255s; 0
  // would disable the timeout entirely, which is not what this wants — a
  // genuinely dead connection should still be reaped.
  idleTimeout: 120,

  async fetch(req) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/login" && req.method === "POST") {
      const form = await req.formData();
      const pw = String(form.get("password") ?? "");
      const a = Buffer.from(pw);
      const b = Buffer.from(CFG.appPassword);
      const ok = a.length === b.length && timingSafeEqual(a, b);
      if (!ok) return new Response(LOGIN.replace("<!--ERR-->", "Wrong password"), { status: 401, headers: html });
      const exp = Date.now() + CFG.sessionHours * 3600 * 1000;
      return new Response(null, {
        status: 302,
        headers: {
          Location: "/",
          "Set-Cookie": `${COOKIE}=${sign(exp)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${CFG.sessionHours * 3600}`,
        },
      });
    }

    if (path === "/logout") {
      return new Response(null, {
        status: 302,
        headers: { Location: "/", "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0` },
      });
    }

    // Service access to snapshots: a shared token instead of a browser session,
    // so the assignment reader (or anything else) can pull a still. Checked
    // before the cookie gate; every other route stays session-only.
    const snapToken = req.headers.get("x-snapshot-token") ?? url.searchParams.get("token") ?? "";
    const tokenOk = CFG.snapshotToken !== "" && snapToken === CFG.snapshotToken;

    if (!authed(req) && !(tokenOk && path === "/api/snapshot.jpg")) {
      if (path === "/") return new Response(LOGIN.replace("<!--ERR-->", ""), { headers: html });
      return new Response("unauthorized", { status: 401 });
    }

    // One still frame from the live stream.
    //   /api/snapshot.jpg              — may be up to SNAPSHOT_TTL_MS old
    //   /api/snapshot.jpg?max_age_ms=0 — force a fresh grab
    if (path === "/api/snapshot.jpg") {
      const raw = url.searchParams.get("max_age_ms");
      const maxAge = raw !== null && Number.isFinite(Number(raw)) ? Number(raw) : CFG.snapshotTtlMs;
      try {
        const jpeg = await snapshot(maxAge);
        return new Response(jpeg, {
          headers: {
            "content-type": "image/jpeg",
            "cache-control": "no-store",
            "x-frame-age-ms": String(snapCache ? Date.now() - snapCache.at : 0),
          },
        });
      } catch (e: any) {
        return new Response(JSON.stringify({ error: String(e?.message ?? e) }), { status: 502, headers: json });
      }
    }

    // --- authenticated routes ---
    if (path === "/") return new Response(INDEX, { headers: html });

    // The Prompts tab's text. Session-gated like everything else here, and
    // no-store so a restart after editing prompts/*.md is actually visible.
    if (path === "/api/prompts") {
      return new Response(JSON.stringify(PROMPTS), {
        headers: { ...json, "cache-control": "no-store" },
      });
    }

    if (path.startsWith("/live/")) return proxyHls(path.slice("/live/".length) + url.search);

    // When video exists, as ranges. ?from/&to are ISO instants; the default
    // window is the whole retention period plus a little, so the scrubber can
    // show everything there is in one go.
    if (path === "/api/timeline") {
      const now = Date.now();
      const parse = (v: string | null, fallback: number) => {
        const t = v ? Date.parse(v) : NaN;
        return Number.isFinite(t) ? t : fallback;
      };
      const to = parse(url.searchParams.get("to"), now + 60_000);
      const from = Math.max(
        parse(url.searchParams.get("from"), now - 30 * 3600 * 1000),
        to - 31 * 24 * 3600 * 1000,   // a month is already far past any retention
      );
      if (from >= to) return new Response("bad range", { status: 400 });
      return playbackTimeline(new Date(from), new Date(to));
    }

    // --- the Adri documents, proxied to the document server ---------------
    //
    // One row each, edited here and read back on the glasses. This is a dumb
    // forwarder: the slug whitelist, the size limit and the versioning all live
    // over there, and duplicating any of them here would be two places to
    // change and one to forget.
    const doc = /^\/api\/doc\/([a-z0-9-]+)$/.exec(path);
    if (doc && (req.method === "GET" || req.method === "PUT")) {
      try {
        const upstream = await fetch(`${CFG.evensUrl}/doc/${doc[1]}`, {
          method: req.method,
          headers: req.method === "PUT" ? { "content-type": "application/json" } : {},
          body: req.method === "PUT" ? await req.text() : undefined,
          signal: AbortSignal.timeout(15000),
        });
        return new Response(await upstream.text(), {
          status: upstream.status,
          headers: { "content-type": "application/json" },
        });
      } catch (e: any) {
        // Named so the page can say which service is down. "Failed to fetch"
        // in a browser console is the same message for every possible cause.
        return Response.json(
          { ok: false, reason: `document server unreachable: ${e?.message ?? e}` },
          { status: 502 },
        );
      }
    }

    // --- messages to and from the glasses ---------------------------------
    //
    // Same forwarding shape as /api/doc, and for the same reason: the document
    // server has no login of its own. The difference is the token — the send
    // route over there refuses anything that doesn't carry it, so this proxy is
    // structurally the only way to put a message on the glasses.
    if (path === "/api/messages" || path.startsWith("/api/messages/")) {
      const upstreamPath = path.slice("/api".length);

      // The event stream is proxied rather than polled so a quick reply tapped
      // on the glasses lands in the open widget immediately. Body is piped, not
      // buffered: buffering an SSE stream would hold it until it ended, which
      // for a stream that never ends means forever.
      if (upstreamPath === "/messages/events") {
        const upstream = await fetch(`${CFG.evensUrl}/messages/events`, {
          headers: { accept: "text/event-stream" },
        });
        return new Response(upstream.body, {
          status: upstream.status,
          headers: {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            connection: "keep-alive",
          },
        });
      }

      try {
        const upstream = await fetch(`${CFG.evensUrl}${upstreamPath}${url.search}`, {
          method: req.method,
          headers: {
            ...(req.method === "POST" ? { "content-type": "application/json" } : {}),
            ...(CFG.messageToken ? { "x-message-token": CFG.messageToken } : {}),
          },
          body: req.method === "POST" ? await req.text() : undefined,
          signal: AbortSignal.timeout(15000),
        });
        return new Response(await upstream.text(), {
          status: upstream.status,
          headers: json,
        });
      } catch (e: any) {
        return Response.json(
          { ok: false, reason: `document server unreachable: ${e?.message ?? e}` },
          { status: 502 },
        );
      }
    }

    // --- the photo published as the assignment ----------------------------
    //
    // Whatever was last uploaded in the companion app (or picked off the phone's
    // gallery), so it can be looked at — and kept — from a real screen instead
    // of a phone. Proxied for the same reason /api/doc is: the document server
    // has no login, and this one does.
    //
    // It holds that photo IN MEMORY only, so a restart of the evens stack turns
    // this into a 404 until the next publish. That is the document server's
    // choice, not something to paper over here — the page says so.
    if (path === "/api/photo" || path === "/api/photo/meta") {
      const meta = path.endsWith("/meta");
      try {
        const upstream = await fetch(`${CFG.evensUrl}/assignment/photo${meta ? "/meta" : ""}`, {
          signal: AbortSignal.timeout(30000),
        });
        if (meta) {
          return new Response(await upstream.text(), { status: upstream.status, headers: json });
        }
        if (!upstream.ok) {
          return new Response(await upstream.text(), { status: upstream.status, headers: json });
        }
        const type = upstream.headers.get("content-type") ?? "image/jpeg";
        const headers = new Headers({ "content-type": type, "cache-control": "no-store" });
        // ?download=1 turns the same bytes into a save — one fetch for the
        // <img> and one for the button, rather than the page holding a blob.
        if (url.searchParams.get("download") === "1") {
          headers.set("content-disposition", `attachment; filename="${downloadName(url, type)}"`);
        }
        return new Response(upstream.body, { headers });
      } catch (e: any) {
        return Response.json(
          { ok: false, reason: `document server unreachable: ${e?.message ?? e}` },
          { status: 502 },
        );
      }
    }

    if (path === "/api/clip") {
      const start = url.searchParams.get("start");
      const duration = url.searchParams.get("duration");
      if (!start || !duration) return new Response("bad request", { status: 400 });
      return playbackClip(start, duration, req.signal);
    }

    return new Response("not found", { status: 404 });
  },
});

console.log(`camera frontend on http://0.0.0.0:${CFG.port}  (stream: ${CFG.streamPath})`);
console.log(`proxying MediaMTX at ${CFG.mtxHost} (HLS :${CFG.hlsPort}, playback :${CFG.playbackPort})`);
