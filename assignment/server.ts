/**
 * assignment-reader — turn the camera into an assignment scanner.
 *
 * ONE `POST /start` kicks off a background job: grab a frame from the camera
 * stack → send it to Gemini → wait a second → again, until the model says it has
 * the whole assignment. Each pass sees what has already been transcribed, so the
 * job refines ONE document instead of producing N independent guesses, and it
 * reports whether the sheet is fully in frame so you know to nudge the camera.
 *
 * Watch it happen live on `GET /events` (SSE). Everything is persisted after
 * every capture (atomic write), so a crash or container restart resumes exactly
 * where it left off — including an interrupted job.
 *
 *   POST /start            START HERE — run captures until the assignment is read
 *   POST /stop             stop the running job
 *   GET  /events           SSE — live progress as the job works
 *   POST /capture          one single capture (manual/debug)
 *   GET  /assignment       the assignment as JSON (what you're after)
 *   GET  /assignment.md    the same thing rendered as Markdown + LaTeX
 *   GET  /state            everything, incl. per-capture history
 *   GET  /frame.jpg        the last frame grabbed (to see what the model saw)
 *   POST /reset            archive the current attempt and start clean
 *   GET  /health
 *
 * Bun, zero dependencies:  bun run server.ts
 */
import {
    mkdirSync,
    existsSync,
    readFileSync,
    writeFileSync,
    renameSync,
} from "node:fs";
import { join, dirname } from "node:path";

const cfg = {
    port: Number(process.env.PORT ?? 8091),
    // Shared-secret auth. Leave empty ONLY if this port is unreachable from the
    // internet — a capture endpoint costs money per call.
    token: process.env.API_TOKEN ?? "",

    geminiKey: process.env.GEMINI_API_KEY ?? "",
    // NOTE: this exact model id is what you asked for; it is not one this code has
    // ever seen answer. If Google returns 404 "model not found", the id is the
    // thing to change — nothing else here depends on it.
    // Known-good alternatives: gemini-2.5-flash-lite, gemini-2.5-flash.
    model: process.env.GEMINI_MODEL ?? "gemini-3.5-flash-lite",
    // Tried only when the primary fails in a way the other model might survive
    // (404 / 429 / 5xx, or unusable output). Empty disables the retry.
    modelFallback: process.env.GEMINI_MODEL_FALLBACK ?? "gemini-3.1-flash-lite",
    geminiBase:
        process.env.GEMINI_BASE_URL ??
        "https://generativelanguage.googleapis.com",

    // Where frames come from. Preferred: ask the web gateway, which owns stream
    // access and MediaMTX's credentials — then this service needs nothing but
    // HTTP and can run anywhere, including off the VPS.
    snapshotUrl: process.env.SNAPSHOT_URL ?? "", // e.g. http://web:8080/api/snapshot.jpg
    snapshotToken: process.env.SNAPSHOT_TOKEN ?? "",
    // Fallback when SNAPSHOT_URL is empty: grab from RTSP ourselves (needs ffmpeg).
    rtspUrl:
        process.env.RTSP_URL ??
        "rtsp://viewer:CHANGE_ME_VIEW@mediamtx:8554/cam1",
    grabTimeoutMs: Number(process.env.GRAB_TIMEOUT_MS ?? 20000),

    // Background job defaults (overridable per POST /start).
    intervalMs: Number(process.env.INTERVAL_MS ?? 5000), // pause between captures
    // Hard ceiling on captures per job. Every capture is a paid API call, so the
    // job can never run away — with a camera pointed at a blank wall the model
    // never says "done", and without this it would bill forever.
    maxCaptures: Number(process.env.MAX_CAPTURES ?? 40),
    maxFailures: Number(process.env.MAX_FAILURES ?? 5), // consecutive, then give up
    // Resume an interrupted job when the process restarts. Bounded by the job's
    // own max_captures, so a crash loop can't multiply the bill.
    autoResume: (process.env.AUTO_RESUME ?? "1") !== "0",

    dataDir: process.env.DATA_DIR ?? "./data",
};

const STATE_FILE = join(cfg.dataDir, "state.json");
const ASSIGNMENT_FILE = join(cfg.dataDir, "assignment.json"); // clean copy, for humans/scripts
const FRAME_FILE = join(cfg.dataDir, "last-frame.jpg");
const ARCHIVE_DIR = join(cfg.dataDir, "archive");

// ---------------------------------------------------------------- state -----

type Problem = { number: string; statement_latex: string; complete: boolean };
type Assignment = {
    title: string;
    subject: string;
    instructions_latex: string;
    problems: Problem[];
};
type CaptureLog = {
    n: number;
    at: string;
    frame_quality: string;
    full_page_visible: boolean;
    camera_advice: string;
    advice_detail: string;
    cut_off_edges: string[];
    changes: string[];
    confidence: number;
};
type Job = {
    running: boolean;
    started_at: string | null;
    finished_at: string | null;
    /** why the job ended: done | stopped | max_captures | failed */
    reason: string | null;
    interval_ms: number;
    max_captures: number;
    note: string;
    consecutive_failures: number;
};

type State = {
    version: number; // bumps on every /reset — "one version of the assignment"
    created_at: string;
    updated_at: string;
    capture_count: number;
    done: boolean; // model thinks the assignment is fully captured
    assignment: Assignment;
    captures: CaptureLog[];
    job: Job;
};

const idleJob = (): Job => ({
    running: false,
    started_at: null,
    finished_at: null,
    reason: null,
    interval_ms: cfg.intervalMs,
    max_captures: cfg.maxCaptures,
    note: "",
    consecutive_failures: 0,
});

const emptyAssignment = (): Assignment => ({
    title: "",
    subject: "",
    instructions_latex: "",
    problems: [],
});

const newState = (version: number): State => ({
    version,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    capture_count: 0,
    done: false,
    assignment: emptyAssignment(),
    captures: [],
    job: idleJob(),
});

/** Write via temp file + rename: a crash mid-write can't leave a half-written
 *  state.json, which is the whole point of persisting in the first place. */
function writeAtomic(path: string, data: string | Buffer) {
    mkdirSync(dirname(path), { recursive: true });
    const tmp = `${path}.tmp`;
    writeFileSync(tmp, data);
    renameSync(tmp, path);
}

function loadState(): State {
    try {
        if (existsSync(STATE_FILE)) {
            const loaded = JSON.parse(
                readFileSync(STATE_FILE, "utf8"),
            ) as State;
            loaded.job = { ...idleJob(), ...(loaded.job ?? {}) }; // state.json from an older build
            return loaded;
        }
    } catch (e) {
        console.error("[!] state.json unreadable, starting fresh:", e);
    }
    return newState(1);
}

let state = loadState();

function saveState() {
    state.updated_at = new Date().toISOString();
    writeAtomic(STATE_FILE, JSON.stringify(state, null, 2));
    // A second, dependency-free file holding just the answer — handy for anything
    // that wants the assignment without knowing this server's shape.
    writeAtomic(
        ASSIGNMENT_FILE,
        JSON.stringify(
            {
                version: state.version,
                updated_at: state.updated_at,
                done: state.done,
                ...state.assignment,
            },
            null,
            2,
        ),
    );
}

// ---------------------------------------------------------------- frame -----

/** One JPEG from the live stream — via the gateway's snapshot API if we have
 *  one, else straight off RTSP. Fails loudly if the stream isn't publishing. */
async function grabFrame(): Promise<Buffer> {
    return cfg.snapshotUrl ? grabViaGateway() : grabViaRtsp();
}

async function grabViaGateway(): Promise<Buffer> {
    // max_age_ms=0: never accept the gateway's cached frame — each capture must
    // see the paper as it is now, not as it was a second ago.
    const url = new URL(cfg.snapshotUrl);
    url.searchParams.set("max_age_ms", "0");
    let res: Response;
    try {
        res = await fetch(url, {
            headers: cfg.snapshotToken
                ? { "x-snapshot-token": cfg.snapshotToken }
                : {},
            signal: AbortSignal.timeout(cfg.grabTimeoutMs),
        });
    } catch (e: any) {
        // fetch's own message doesn't name the target, which is useless when the
        // whole question is "can this service reach the gateway?".
        throw new Error(
            `snapshot API unreachable at ${url.origin}${url.pathname}: ${e?.message ?? e}`,
        );
    }
    if (!res.ok) {
        throw new Error(
            `snapshot API ${res.status}: ${(await res.text()).slice(0, 300)}`,
        );
    }
    const jpeg = Buffer.from(await res.arrayBuffer());
    if (jpeg.length === 0)
        throw new Error("snapshot API returned an empty image");
    return jpeg;
}

async function grabViaRtsp(): Promise<Buffer> {
    const args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        cfg.rtspUrl,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ];
    const proc = Bun.spawn(args, { stdout: "pipe", stderr: "pipe" });
    const timer = setTimeout(() => proc.kill(), cfg.grabTimeoutMs);
    try {
        const [buf, err] = await Promise.all([
            new Response(proc.stdout).arrayBuffer(),
            new Response(proc.stderr).text(),
        ]);
        await proc.exited;
        const jpeg = Buffer.from(buf);
        if (jpeg.length === 0) {
            throw new Error(
                `ffmpeg produced no frame — is the stream publishing? ${err.trim()}`,
            );
        }
        return jpeg;
    } finally {
        clearTimeout(timer);
    }
}

// --------------------------------------------------------------- gemini -----

// Structured output: Gemini is constrained to this shape, so the server never
// has to parse prose. Keep `required` tight — optional fields come back missing.
const RESPONSE_SCHEMA = {
    type: "object",
    properties: {
        frame_quality: {
            type: "string",
            enum: ["good", "blurry", "dark", "glare", "no_paper"],
        },
        framing: {
            type: "object",
            properties: {
                full_page_visible: { type: "boolean" },
                cut_off_edges: {
                    type: "array",
                    items: {
                        type: "string",
                        enum: ["top", "bottom", "left", "right"],
                    },
                },
                camera_advice: {
                    type: "string",
                    enum: [
                        "ok",
                        "move_up",
                        "move_down",
                        "move_left",
                        "move_right",
                        "move_closer",
                        "move_farther",
                        "refocus",
                        "reduce_glare",
                        "reposition_paper",
                    ],
                },
                advice_detail: { type: "string" },
            },
            required: [
                "full_page_visible",
                "cut_off_edges",
                "camera_advice",
                "advice_detail",
            ],
        },
        assignment: {
            type: "object",
            properties: {
                title: { type: "string" },
                subject: { type: "string" },
                instructions_latex: { type: "string" },
                problems: {
                    type: "array",
                    items: {
                        type: "object",
                        properties: {
                            number: { type: "string" },
                            statement_latex: { type: "string" },
                            complete: { type: "boolean" },
                        },
                        required: ["number", "statement_latex", "complete"],
                    },
                },
            },
            required: ["title", "subject", "instructions_latex", "problems"],
        },
        changes: { type: "array", items: { type: "string" } },
        confidence: { type: "number" },
        done: { type: "boolean" },
    },
    required: [
        "frame_quality",
        "framing",
        "assignment",
        "changes",
        "confidence",
        "done",
    ],
};

function buildPrompt(note: string): string {
    const soFar = JSON.stringify(state.assignment, null, 2);
    return `You are reading a school assignment from a single frame of a ceiling-mounted camera pointed at a sheet of paper on a desk.

YOUR TWO JOBS
1. TRANSCRIBE the assignment exactly as written — every problem, in order.
2. JUDGE THE FRAMING and tell the operator how to move the camera so the ENTIRE
   sheet becomes visible. Be decisive: if the top of the page is cut off, the
   camera must move up (camera_advice="move_up"). If text is too small or
   unreadable, "move_closer". If nothing readable is in frame, "no_paper".

MATH MUST BE LaTeX
Every mathematical symbol, fraction, exponent, root, integral, matrix or
equation goes in LaTeX: inline as $...$ and display as $$...$$. Never
approximate math with plain text (write $\\frac{3}{4}$, not 3/4; $x^2$, not x2).
Plain prose stays plain.

THIS IS ONE CONTINUING TRANSCRIPTION
You have seen earlier frames of the SAME assignment. Here is the transcription
so far (JSON):

${soFar}

Return the COMPLETE, UPDATED transcription — not just the new bits:
- Keep everything already correct; do not drop problems you can no longer see.
- Fix mistakes and fill in parts that were previously cut off or unreadable.
- Do not duplicate a problem that is already listed; match by its number.
- Mark a problem complete=false if you can only see part of its statement.
- List what this frame changed in "changes" (e.g. "added problem 4", "fixed
  exponent in problem 2"). If nothing changed, return an empty array.
- Set done=true only when every problem is complete and the whole sheet has
  been seen.
${note ? `\nOPERATOR NOTE FOR THIS FRAME: ${note}\n` : ""}`;
}

type GeminiResult = {
    frame_quality: string;
    framing: {
        full_page_visible: boolean;
        cut_off_edges: string[];
        camera_advice: string;
        advice_detail: string;
    };
    assignment: Assignment;
    changes: string[];
    confidence: number;
    done: boolean;
};

/** Whether trying the fallback model can plausibly help. A rejected id, a
 *  rate-limited model or a transient Google failure are all worth a second
 *  attempt on the other model; a malformed request (400) or a bad key/permission
 *  (401/403) would fail identically there, so those propagate as-is. */
function worthFallback(status: number): boolean {
    return status === 404 || status === 429 || status >= 500;
}

/** Primary model, then GEMINI_MODEL_FALLBACK if the primary fails in a way the
 *  other model might survive. A capture costs one API call, so the fallback is
 *  only ever a second call when the first genuinely failed — never a routine
 *  double-spend. */
async function readFrame(jpeg: Buffer, note: string): Promise<GeminiResult> {
    if (!cfg.geminiKey) throw new Error("GEMINI_API_KEY is not set");
    const chain = cfg.modelFallback
        ? [cfg.model, cfg.modelFallback]
        : [cfg.model];

    let lastErr: any;
    for (let i = 0; i < chain.length; i++) {
        try {
            return await callGemini(chain[i], jpeg, note);
        } catch (e: any) {
            lastErr = e;
            const next = chain[i + 1];
            if (!next || !e?.fallbackWorthwhile) throw e;
            console.warn(
                `[gemini] ${chain[i]} failed (${e.message?.slice(0, 160)}) — retrying on ${next}`,
            );
            emit("model_fallback", {
                from: chain[i],
                to: next,
                reason: String(e?.message ?? e).slice(0, 300),
            });
        }
    }
    throw lastErr;
}

async function callGemini(
    model: string,
    jpeg: Buffer,
    note: string,
): Promise<GeminiResult> {
    const url = `${cfg.geminiBase}/v1beta/models/${model}:generateContent`;
    const res = await fetch(url, {
        method: "POST",
        headers: {
            "content-type": "application/json",
            "x-goog-api-key": cfg.geminiKey,
        },
        body: JSON.stringify({
            contents: [
                {
                    role: "user",
                    parts: [
                        {
                            inline_data: {
                                mime_type: "image/jpeg",
                                data: jpeg.toString("base64"),
                            },
                        },
                        { text: buildPrompt(note) },
                    ],
                },
            ],
            generationConfig: {
                temperature: 0, // transcription, not creativity
                responseMimeType: "application/json",
                responseSchema: RESPONSE_SCHEMA,
            },
        }),
    });

    const raw = await res.text();
    if (!res.ok) {
        // Surfaced verbatim: a wrong model id shows up here as a 404 with the exact
        // name Google rejected, rather than as a vague failure.
        throw Object.assign(
            new Error(
                `Gemini ${res.status} (model="${model}"): ${raw.slice(0, 800)}`,
            ),
            { fallbackWorthwhile: worthFallback(res.status) },
        );
    }
    let body: any;
    try {
        body = JSON.parse(raw);
    } catch {
        throw new Error(`Gemini returned non-JSON: ${raw.slice(0, 400)}`);
    }
    const text = body?.candidates?.[0]?.content?.parts?.[0]?.text;
    // An empty candidate (safety block, truncation) and unparseable output are
    // both model-specific, so the fallback gets a shot at them too.
    if (!text)
        throw Object.assign(
            new Error(
                `Gemini returned no content (model="${model}"): ${raw.slice(0, 400)}`,
            ),
            { fallbackWorthwhile: true },
        );
    try {
        return JSON.parse(text) as GeminiResult;
    } catch {
        throw Object.assign(
            new Error(
                `model output was not valid JSON (model="${model}"): ${String(text).slice(0, 400)}`,
            ),
            { fallbackWorthwhile: true },
        );
    }
}

// ------------------------------------------------------------------ sse -----

// Live progress feed. A capture takes seconds (frame grab + model call), so
// rather than making the caller stare at a pending POST, every step is pushed
// to whoever is listening on /events. Multiple listeners are fine, and they
// stay connected across captures and resets.
const enc = new TextEncoder();
const clients = new Set<ReadableStreamDefaultController<Uint8Array>>();

function frame(event: string, data: unknown): Uint8Array {
    return enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

function emit(event: string, data: Record<string, unknown> = {}) {
    const payload = frame(event, { at: new Date().toISOString(), ...data });
    for (const c of clients) {
        try {
            c.enqueue(payload);
        } catch {
            clients.delete(c);
        }
    }
}

/** What a client needs to know the moment it connects, without asking. */
const snapshot = () => ({
    version: state.version,
    capture_count: state.capture_count,
    done: state.done,
    capturing,
    job: state.job,
    problems: state.assignment.problems?.length ?? 0,
    last_capture: state.captures.at(-1) ?? null,
});

// Comment lines keep the connection (and any proxy in front of it) alive.
setInterval(() => {
    for (const c of clients) {
        try {
            c.enqueue(enc.encode(": ping\n\n"));
        } catch {
            clients.delete(c);
        }
    }
}, 20000);

function eventStream(): Response {
    let self: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
        start(c) {
            self = c;
            clients.add(c);
            c.enqueue(enc.encode("retry: 3000\n\n")); // auto-reconnect after 3s
            c.enqueue(
                frame("snapshot", {
                    at: new Date().toISOString(),
                    ...snapshot(),
                }),
            );
        },
        cancel() {
            clients.delete(self);
        },
    });
    return new Response(stream, {
        headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache, no-transform",
            connection: "keep-alive",
            "x-accel-buffering": "no", // stop nginx-style proxies buffering the feed
        },
    });
}

// -------------------------------------------------------------- captures ----

// One capture at a time: two overlapping calls would each merge against the same
// "before" state and the second would clobber the first.
let capturing = false;

async function doCapture(note: string) {
    if (capturing)
        throw Object.assign(new Error("a capture is already in progress"), {
            status: 409,
        });
    capturing = true;
    const startedAt = Date.now();
    const startedVersion = state.version;
    try {
        emit("capture_started", {
            n: state.capture_count + 1,
            note: note || null,
        });

        const jpeg = await grabFrame();
        writeAtomic(FRAME_FILE, jpeg);
        emit("frame_grabbed", {
            bytes: jpeg.length,
            ms: Date.now() - startedAt,
            source: cfg.snapshotUrl ? "gateway" : "rtsp",
        });

        // The model actually used may end up being the fallback — a
        // model_fallback event follows if it does.
        emit("model_request", { model: cfg.model });
        const modelStart = Date.now();
        const result = await readFrame(jpeg, note);
        emit("model_response", {
            ms: Date.now() - modelStart,
            frame_quality: result.frame_quality,
            camera_advice: result.framing.camera_advice,
            advice_detail: result.framing.advice_detail,
            full_page_visible: result.framing.full_page_visible,
            cut_off_edges: result.framing.cut_off_edges ?? [],
            changes: result.changes ?? [],
            confidence: result.confidence,
            done: result.done,
        });

        // /reset landed while this frame was in flight — the result describes an
        // assignment that no longer exists, so drop it rather than merge it into
        // the fresh one.
        if (state.version !== startedVersion) {
            emit("capture_discarded", {
                reason: "assignment was reset during this capture",
            });
            throw Object.assign(
                new Error("assignment was reset during this capture"),
                { status: 409 },
            );
        }

        state.capture_count += 1;
        state.assignment = result.assignment;
        state.done = result.done;
        const log: CaptureLog = {
            n: state.capture_count,
            at: new Date().toISOString(),
            frame_quality: result.frame_quality,
            full_page_visible: result.framing.full_page_visible,
            camera_advice: result.framing.camera_advice,
            advice_detail: result.framing.advice_detail,
            cut_off_edges: result.framing.cut_off_edges ?? [],
            changes: result.changes ?? [],
            confidence: result.confidence,
        };
        state.captures.push(log);
        saveState();

        const needs_adjustment =
            !result.framing.full_page_visible ||
            result.framing.camera_advice !== "ok";
        emit("assignment_updated", {
            capture: log,
            needs_adjustment,
            done: state.done,
            problems: state.assignment.problems?.length ?? 0,
            assignment: state.assignment,
        });
        if (state.done)
            emit("done", {
                version: state.version,
                problems: state.assignment.problems?.length ?? 0,
            });

        return {
            ok: true,
            capture: log,
            // What the operator actually needs to act on, hoisted to the top level.
            needs_adjustment,
            done: state.done,
            assignment: state.assignment,
        };
    } catch (e: any) {
        emit("capture_failed", { error: String(e?.message ?? e) });
        throw e;
    } finally {
        capturing = false;
    }
}

// ------------------------------------------------------------------ job -----

// The whole point: one POST starts this, and it keeps capturing until the model
// says the assignment is fully read (or a guard rail stops it).
let jobRunning = false;
let wakeFromSleep: (() => void) | null = null;

/** Sleep that /stop can cut short, so stopping is instant rather than up to a
 *  full interval late. */
function interruptibleSleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
        const t = setTimeout(() => {
            wakeFromSleep = null;
            resolve();
        }, ms);
        wakeFromSleep = () => {
            clearTimeout(t);
            wakeFromSleep = null;
            resolve();
        };
    });
}

function finishJob(reason: string) {
    state.job.running = false;
    state.job.finished_at = new Date().toISOString();
    state.job.reason = reason;
    saveState();
    emit("job_finished", {
        reason,
        captures: state.capture_count,
        done: state.done,
        problems: state.assignment.problems?.length ?? 0,
        assignment: state.assignment,
    });
    console.log(
        `[job] finished: ${reason} after ${state.capture_count} capture(s)`,
    );
}

async function jobLoop() {
    if (jobRunning) return;
    jobRunning = true;
    try {
        while (state.job.running) {
            // Stop conditions checked before spending an API call.
            if (state.done) return finishJob("done");
            if (state.capture_count >= state.job.max_captures)
                return finishJob("max_captures");

            try {
                await doCapture(state.job.note);
                state.job.consecutive_failures = 0;
            } catch (e: any) {
                // A failed capture is usually the stream being down for a moment — keep
                // going, but don't hammer a broken setup forever. (doCapture already
                // emitted capture_failed with the reason.)
                state.job.consecutive_failures += 1;
                console.error(
                    `[job] capture failed (${state.job.consecutive_failures}/${cfg.maxFailures}):`,
                    e?.message ?? e,
                );
                if (state.job.consecutive_failures >= cfg.maxFailures)
                    return finishJob("failed");
            }
            saveState();

            if (!state.job.running) break; // /stop arrived mid-capture
            if (state.done) return finishJob("done");
            await interruptibleSleep(state.job.interval_ms);
        }
        // Loop exited because running went false — /stop already reported it.
    } finally {
        jobRunning = false;
    }
}

function startJob(opts: {
    note?: string;
    interval_ms?: number;
    max_captures?: number;
}) {
    state.job = {
        running: true,
        started_at: new Date().toISOString(),
        finished_at: null,
        reason: null,
        interval_ms: Math.max(0, opts.interval_ms ?? cfg.intervalMs),
        // The cap counts total captures for this version, so a restart mid-job
        // resumes toward the same ceiling instead of getting a fresh budget.
        max_captures: Math.max(1, opts.max_captures ?? cfg.maxCaptures),
        note: opts.note ?? "",
        consecutive_failures: 0,
    };
    saveState();
    emit("job_started", {
        interval_ms: state.job.interval_ms,
        max_captures: state.job.max_captures,
        note: state.job.note || null,
        from_capture: state.capture_count,
    });
    void jobLoop(); // deliberately not awaited: POST /start returns now
    return state.job;
}

// ----------------------------------------------------------------- http -----

const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data, null, 2), {
        status,
        headers: { "content-type": "application/json; charset=utf-8" },
    });

function authed(req: Request): boolean {
    if (!cfg.token) return true;
    const url = new URL(req.url);
    const bearer = (req.headers.get("authorization") ?? "").replace(
        /^Bearer\s+/i,
        "",
    );
    return (
        bearer === cfg.token ||
        req.headers.get("x-api-token") === cfg.token ||
        url.searchParams.get("token") === cfg.token
    );
}

function toMarkdown(a: Assignment): string {
    const lines: string[] = [];
    if (a.title) lines.push(`# ${a.title}`, "");
    if (a.subject) lines.push(`*${a.subject}*`, "");
    if (a.instructions_latex) lines.push(a.instructions_latex, "");
    for (const p of a.problems ?? []) {
        lines.push(
            `## ${p.number}${p.complete ? "" : "  _(incomplete)_"}`,
            "",
            p.statement_latex,
            "",
        );
    }
    return lines.join("\n");
}

const server = Bun.serve({
    port: cfg.port,
    hostname: "0.0.0.0",
    async fetch(req) {
        const url = new URL(req.url);
        const path = url.pathname;

        if (path === "/health") {
            return json({
                ok: true,
                version: state.version,
                captures: state.capture_count,
                done: state.done,
                job_running: state.job.running,
            });
        }
        if (!authed(req)) return json({ error: "unauthorized" }, 401);

        // Live feed. EventSource can't send headers, so authenticate with
        // ?token=… here:  new EventSource("/events?token=…")
        if (path === "/events" && req.method === "GET") return eventStream();

        // START HERE. Runs captures in the background until the assignment is read.
        // Optional body: {"note": "...", "interval_ms": 1000, "max_captures": 40}
        if (path === "/start" && req.method === "POST") {
            if (state.job.running) {
                return json(
                    {
                        ok: false,
                        error: "a job is already running",
                        job: state.job,
                    },
                    409,
                );
            }
            if (state.done) {
                // Refusing here beats silently burning a capture to re-confirm: the
                // caller almost certainly wants /reset first.
                return json(
                    {
                        ok: false,
                        error: "this assignment is already complete — POST /reset to start a new one",
                        job: state.job,
                    },
                    409,
                );
            }
            let opts: any = {};
            try {
                opts = (await req.json()) ?? {};
            } catch {
                /* empty body is normal */
            }
            const job = startJob({
                note: typeof opts.note === "string" ? opts.note : undefined,
                interval_ms: Number.isFinite(opts.interval_ms)
                    ? Number(opts.interval_ms)
                    : undefined,
                max_captures: Number.isFinite(opts.max_captures)
                    ? Number(opts.max_captures)
                    : undefined,
            });
            // 202: accepted and running in the background — watch /events for progress.
            return json(
                { ok: true, started: true, job, watch: "/events" },
                202,
            );
        }

        // Stop the background job. Idempotent.
        if (path === "/stop" && req.method === "POST") {
            const was = state.job.running;
            if (was) {
                state.job.running = false;
                wakeFromSleep?.(); // don't wait out the remaining interval
                finishJob("stopped");
            }
            return json({ ok: true, was_running: was, job: state.job });
        }

        // One capture, no job. Handy for a manual check or to debug framing.
        if (path === "/capture" && req.method === "POST") {
            let note = "";
            try {
                const body = await req.json();
                if (body && typeof body.note === "string") note = body.note;
            } catch {
                /* empty body is the normal case */
            }
            try {
                return json(await doCapture(note));
            } catch (e: any) {
                console.error("[!] capture failed:", e?.message ?? e);
                return json(
                    { ok: false, error: String(e?.message ?? e) },
                    e?.status ?? 502,
                );
            }
        }

        if (path === "/assignment" && req.method === "GET") {
            return json({
                version: state.version,
                updated_at: state.updated_at,
                capture_count: state.capture_count,
                done: state.done,
                job: { running: state.job.running, reason: state.job.reason },
                assignment: state.assignment,
            });
        }

        if (path === "/assignment.md" && req.method === "GET") {
            return new Response(toMarkdown(state.assignment), {
                headers: { "content-type": "text/markdown; charset=utf-8" },
            });
        }

        if (path === "/state" && req.method === "GET") return json(state);

        if (path === "/frame.jpg" && req.method === "GET") {
            if (!existsSync(FRAME_FILE))
                return json({ error: "no frame captured yet" }, 404);
            return new Response(readFileSync(FRAME_FILE), {
                headers: { "content-type": "image/jpeg" },
            });
        }

        // Start over. The old attempt is archived rather than deleted — flushing
        // progress shouldn't be able to destroy a transcription you wanted.
        if (path === "/reset" && req.method === "POST") {
            const previous = state;
            if (previous.job.running) {
                // a reset implies "stop what you're doing"
                previous.job.running = false;
                wakeFromSleep?.();
            }
            if (previous.capture_count > 0) {
                writeAtomic(
                    join(
                        ARCHIVE_DIR,
                        `v${previous.version}-${previous.created_at.replace(/[:.]/g, "-")}.json`,
                    ),
                    JSON.stringify(previous, null, 2),
                );
            }
            state = newState(previous.version + 1);
            saveState();
            emit("reset", {
                version: state.version,
                archived: previous.capture_count > 0,
            });
            return json({
                ok: true,
                version: state.version,
                archived: previous.capture_count > 0,
            });
        }

        return json(
            {
                error: "not found",
                endpoints: [
                    "POST /start",
                    "POST /stop",
                    "GET /events",
                    "POST /capture",
                    "GET /assignment",
                    "GET /assignment.md",
                    "GET /state",
                    "GET /frame.jpg",
                    "POST /reset",
                    "GET /health",
                ],
            },
            404,
        );
    },
});

mkdirSync(cfg.dataDir, { recursive: true });
saveState(); // make sure data/ is writable now, not on the first capture
console.log(`assignment-reader on http://0.0.0.0:${server.port}`);
console.log(
    `  model:  ${cfg.model}${cfg.modelFallback ? ` (fallback: ${cfg.modelFallback})` : ""}`,
);
console.log(
    `  frames: ${
        cfg.snapshotUrl
            ? `${cfg.snapshotUrl} (gateway snapshot API)`
            : `${cfg.rtspUrl.replace(/\/\/[^@]*@/, "//***@")} (direct RTSP)`
    }`,
);
console.log(
    `  data:   ${cfg.dataDir}  (version ${state.version}, ${state.capture_count} captures)`,
);
if (!cfg.geminiKey)
    console.warn("  [!] GEMINI_API_KEY is not set — captures will fail");
if (!cfg.token)
    console.warn("  [!] API_TOKEN is empty — this server is unauthenticated");

// A job that was running when the process died: pick it back up. Its
// max_captures counts total captures for this version, so resuming can't spend
// more than the original budget however many times we restart.
if (state.job.running) {
    if (
        cfg.autoResume &&
        !state.done &&
        state.capture_count < state.job.max_captures
    ) {
        console.log(
            `[job] resuming interrupted job (${state.capture_count}/${state.job.max_captures} captures used)`,
        );
        emit("job_resumed", {
            from_capture: state.capture_count,
            max_captures: state.job.max_captures,
        });
        void jobLoop();
    } else {
        finishJob(cfg.autoResume ? "max_captures" : "stopped");
    }
}
