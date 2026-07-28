// Gateway + frontend for the LookCam bridge.
//
// One public port. Serves the SPA, handles a shared-password login, and proxies
// to MediaMTX (which stays bound to localhost) injecting the viewer credentials:
//   - live:        HLS  ->  http://MTX:8888/<path>/*
//   - recordings:  playback /list + /get on http://MTX:9996
//   - snapshot:    GET /api/snapshot.jpg -> one JPEG off the live stream, so
//                  anything that wants a still (the assignment reader, a
//                  dashboard, a cron job) asks HTTP instead of speaking RTSP
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

async function playbackList(date: string): Promise<Response> {
  // date = YYYY-MM-DD -> list that whole local day.
  const start = new Date(`${date}T00:00:00`);
  const end = new Date(start.getTime() + 24 * 3600 * 1000);
  const qs = new URLSearchParams({
    path: CFG.streamPath,
    start: start.toISOString(),
    end: end.toISOString(),
  });
  const url = `http://${CFG.mtxHost}:${CFG.playbackPort}/list?${qs}`;
  const upstream = await fetch(url, { headers: { Authorization: mtxAuth } });
  if (!upstream.ok) return new Response("[]", { status: 200, headers: json });
  const segs = (await upstream.json()) as Array<{ start: string; duration: number }>;
  // Rewrite to point at our own /api/clip so creds stay server-side.
  const rewritten = segs.map((s) => ({
    start: s.start,
    duration: s.duration,
    url: `/api/clip?start=${encodeURIComponent(s.start)}&duration=${s.duration}`,
  }));
  return new Response(JSON.stringify(rewritten), { headers: json });
}

async function playbackClip(start: string, duration: string): Promise<Response> {
  const qs = new URLSearchParams({
    path: CFG.streamPath,
    start,
    duration,
    format: "fmp4",
  });
  const url = `http://${CFG.mtxHost}:${CFG.playbackPort}/get?${qs}`;
  const upstream = await fetch(url, { headers: { Authorization: mtxAuth } });
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

// ---- routing ---------------------------------------------------------------
const INDEX = await Bun.file(new URL("./index.html", import.meta.url)).text();
const LOGIN = await Bun.file(new URL("./login.html", import.meta.url)).text();

Bun.serve({
  port: CFG.port,
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

    if (path.startsWith("/live/")) return proxyHls(path.slice("/live/".length) + url.search);

    if (path === "/api/recordings") {
      const date = url.searchParams.get("date") ?? new Date().toISOString().slice(0, 10);
      return playbackList(date);
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

    if (path === "/api/clip") {
      const start = url.searchParams.get("start");
      const duration = url.searchParams.get("duration");
      if (!start || !duration) return new Response("bad request", { status: 400 });
      return playbackClip(start, duration);
    }

    return new Response("not found", { status: 404 });
  },
});

console.log(`camera frontend on http://0.0.0.0:${CFG.port}  (stream: ${CFG.streamPath})`);
console.log(`proxying MediaMTX at ${CFG.mtxHost} (HLS :${CFG.hlsPort}, playback :${CFG.playbackPort})`);
