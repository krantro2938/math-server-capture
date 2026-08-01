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
 *   POST /photo            read a PHOTO as the assignment (image in the body)
 *   POST /assignment       set the assignment from TYPED markdown (no model call)
 *   GET  /assignment       the assignment as JSON (what you're after)
 *   GET  /assignment.md    the same thing rendered as Markdown + LaTeX
 *   GET  /state            everything, incl. per-capture history
 *   GET  /frame.jpg        the last frame grabbed (to see what the model saw)
 *   GET  /snapshot.jpg     the camera RIGHT NOW (to see where it is pointing)
 *   POST /reset            archive the current attempt and start clean
 *   GET  /archive          every attempt, newest first (the live one included)
 *   GET  /archive/:v       one attempt, same shape as /assignment
 *   GET  /archive/:v.md    one attempt as Markdown, like /assignment.md
 *   GET  /health
 *
 * Bun, zero dependencies:  bun run server.ts
 */
import {
    mkdirSync,
    existsSync,
    readFileSync,
    readdirSync,
    writeFileSync,
    renameSync,
    rmSync,
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

    // Mistral: the backup PROVIDER, tried only after both Gemini models have
    // failed. A second Google model does nothing for an outage at Google, a
    // revoked key or a region-wide 429 — those are exactly the failures that
    // leave a camera pointed at a sheet of paper reading nothing at all. Empty
    // key disables it, and the reader behaves as it did before.
    mistralKey: process.env.MISTRAL_API_KEY ?? "",
    mistralModel: process.env.MISTRAL_MODEL ?? "mistral-medium-latest",
    mistralModelFallback:
        process.env.MISTRAL_MODEL_FALLBACK ?? "mistral-small-2506",
    mistralBase: process.env.MISTRAL_BASE_URL ?? "https://api.mistral.ai",

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
    maxBatchSnapshots: Number(process.env.MAX_BATCH_SNAPSHOTS ?? 40),
    maxBatchBytes: Number(process.env.MAX_BATCH_BYTES ?? 12_000_000),
};

const STATE_FILE = join(cfg.dataDir, "state.json");
const ASSIGNMENT_FILE = join(cfg.dataDir, "assignment.json"); // clean copy, for humans/scripts
const FRAME_FILE = join(cfg.dataDir, "last-frame.jpg");
const ARCHIVE_DIR = join(cfg.dataDir, "archive");
const BATCH_DIR = join(cfg.dataDir, "batches");

// ---------------------------------------------------------------- state -----

type Problem = {
    number: string;
    statement_latex: string;
    complete: boolean;
    clear?: boolean;
    confidence?: number;
};
type Observation = {
    problem_number: string;
    line_key: string;
    span: "start" | "middle" | "end" | "whole";
    location: string;
    text_latex: string;
    confidence: number;
    clear: boolean;
};
type Assignment = {
    title: string;
    subject: string;
    instructions_latex: string;
    problems: Problem[];
};
type CaptureLog = {
    n: number;
    at: string;
    /**
     * Who read this frame. Optional because a state.json written before the
     * provider chain existed has neither, and every reader of this type has to
     * cope with the file already on disk.
     */
    provider?: string;
    model?: string;
    frame_quality: string;
    full_page_visible: boolean;
    camera_advice: string;
    advice_detail: string;
    cut_off_edges: string[];
    next_target: string;
    next_target_short: string;
    region: string;
    observations: Observation[];
    unreadable: string[];
    more_content_beyond: string[];
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
type Batch = {
    active: boolean;
    processing: boolean;
    started_at: string | null;
    finished_at: string | null;
    snapshot_count: number;
    max_snapshots: number;
    note: string;
    error: string | null;
};

type State = {
    version: number; // bumps on every /reset — "one version of the assignment"
    created_at: string;
    updated_at: string;
    capture_count: number;
    done: boolean; // model thinks the assignment is fully captured
    /** Whether any one frame showed the whole sheet; retained for diagnostics. */
    full_page_seen: boolean;
    /** Cumulative paper edges seen across all readable captures. */
    edges_seen: string[];
    /** Persistent exact fragments, keyed by problem + line_key. */
    observations: Observation[];
    assignment: Assignment;
    captures: CaptureLog[];
    job: Job;
    batch: Batch;
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
const idleBatch = (): Batch => ({
    active: false,
    processing: false,
    started_at: null,
    finished_at: null,
    snapshot_count: 0,
    max_snapshots: cfg.maxBatchSnapshots,
    note: "",
    error: null,
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
    full_page_seen: false,
    edges_seen: [],
    observations: [],
    assignment: emptyAssignment(),
    captures: [],
    job: idleJob(),
    batch: idleBatch(),
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
            loaded.batch = { ...idleBatch(), ...(loaded.batch ?? {}) };
            loaded.full_page_seen = Boolean(loaded.full_page_seen); // ditto
            loaded.edges_seen = Array.isArray(loaded.edges_seen)
                ? loaded.edges_seen.map(String)
                : [];
            loaded.observations = Array.isArray(loaded.observations)
                ? loaded.observations
                : [];
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

/**
 * How long /snapshot.jpg may serve the frame it already has.
 *
 * Captures never come through here — they call grabFrame() directly and pass
 * max_age_ms=0 to the gateway, because a capture must see the paper as it is
 * now. This cache is for the aiming preview, which polls: without it, every
 * poll (and every extra viewer) spawns an ffmpeg on the RTSP fallback path and
 * a decode on the gateway. One second is below the round trip to the glasses,
 * so nobody sees a frame they'd call stale.
 */
const SNAPSHOT_TTL_MS = Number(process.env.SNAPSHOT_TTL_MS ?? 1000);

let liveCache: { at: number; jpeg: Buffer } | null = null;
let liveInFlight: Promise<Buffer> | null = null;

/** A recent frame, coalescing concurrent askers onto one grab. */
async function liveFrame(maxAgeMs: number): Promise<Buffer> {
    if (liveCache && Date.now() - liveCache.at <= maxAgeMs) return liveCache.jpeg;
    if (!liveInFlight) {
        liveInFlight = grabFrame()
            .then((jpeg) => {
                liveCache = { at: Date.now(), jpeg };
                return jpeg;
            })
            .finally(() => {
                liveInFlight = null;
            });
    }
    return liveInFlight;
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
                        "move_slightly_up",
                        "move_slightly_down",
                        "move_slightly_left",
                        "move_slightly_right",
                        "refocus",
                        "reduce_glare",
                        "reposition_paper",
                    ],
                },
                advice_detail: { type: "string" },
                region: { type: "string" },
                edges_seen: {
                    type: "array",
                    items: { type: "string", enum: ["top", "bottom", "left", "right"] },
                },
                next_target: { type: "string" },
                // A second, display-safe form for the two-line glasses HUD.
                next_target_short: { type: "string" },
                more_content_beyond: {
                    type: "array",
                    items: { type: "string", enum: ["top", "bottom", "left", "right"] },
                },
            },
            required: [
                "full_page_visible",
                "cut_off_edges",
                "camera_advice",
                "advice_detail",
                "region",
                "edges_seen",
                "next_target",
                "next_target_short",
                "more_content_beyond",
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
                            clear: { type: "boolean" },
                            confidence: { type: "number" },
                        },
                        required: ["number", "statement_latex", "complete", "clear", "confidence"],
                    },
                },
            },
            required: ["title", "subject", "instructions_latex", "problems"],
        },
        changes: { type: "array", items: { type: "string" } },
        confidence: { type: "number" },
        unreadable: { type: "array", items: { type: "string" } },
        observations: {
            type: "array",
            items: {
                type: "object",
                properties: {
                    problem_number: { type: "string" },
                    line_key: { type: "string" },
                    span: { type: "string", enum: ["start", "middle", "end", "whole"] },
                    location: { type: "string" },
                    text_latex: { type: "string" },
                    confidence: { type: "number" },
                    clear: { type: "boolean" },
                },
                required: [
                    "problem_number",
                    "line_key",
                    "span",
                    "location",
                    "text_latex",
                    "confidence",
                    "clear",
                ],
            },
        },
        done: { type: "boolean" },
    },
    required: [
        "frame_quality",
        "framing",
        "assignment",
        "changes",
        "confidence",
        "unreadable",
        "observations",
        "done",
    ],
};

function buildPrompt(note: string): string {
    const soFar = JSON.stringify(state.assignment, null, 2);
    const evidenceSoFar = JSON.stringify(state.observations, null, 2);
    return `You are reading a school assignment from a single frame of a ceiling-mounted camera pointed at a sheet of paper on a desk.

EVIDENCE-ONLY RULE: NEVER GUESS, COMPLETE, OR RECONSTRUCT text that is not
actually legible in this frame. A blurry character, cropped line, glare-covered
word, or tiny unreadable problem is NOT readable. If anything is uncertain,
omit that problem from assignment.problems and explain exactly what is missing
in unreadable and next_target. It is better to say "problem 4 final line is
not visible" than to invent it. However, DO report every legible fragment in
observations, even when it is only a few words or half a line. This is a
persistent evidence store: use a stable line_key such as "line-1" or
"line-2", set span to start/middle/end/whole, record where it was in the frame,
and include only the exact visible text. Later frames may edit the same
line_key with a clearer or longer fragment.

YOUR TWO JOBS
1. TRANSCRIBE the assignment exactly as written — every problem, in order.
2. JUDGE THE FRAMING and tell the operator how to move the camera so the ENTIRE
   sheet becomes visible. Be decisive: if the top of the page is cut off, the
   camera must move up (camera_advice="move_up"). If it is only slightly off,
   use move_slightly_up/down/left/right. If text is too small or unreadable,
   "move_closer". If nothing readable is in frame, "no_paper".

MATH MUST BE LaTeX, PROSE MUST NOT BE
Every mathematical symbol, fraction, exponent, root, integral, matrix or
equation goes in LaTeX: inline as $...$ and display as $$...$$. Never
approximate math with plain text (write $\\frac{3}{4}$, not 3/4; $x^2$, not x2).

A statement is ORDINARY SENTENCES with $...$ islands in them. It is NOT one
long LaTeX expression. Never wrap words in \\text{} — if you find yourself
writing \\text{}, the words belong outside the math, not inside it. Every
statement_latex must contain at least one $ if it contains any mathematics.

  RIGHT: Отрезки $AB$ и $CD$ являются хордами. Найдите $AB$, если $CD = 18$.
  WRONG: \\text{Отрезки } AB \\text{ и } CD \\text{ являются хордами.}

The wrong form cannot be rendered or read by anything downstream.

THIS IS ONE CONTINUING TRANSCRIPTION
You have seen earlier frames of the SAME assignment. Here is the transcription
so far (JSON):

${soFar}

Persistent exact line evidence from earlier close-ups (you may improve a matching
line_key, but do not erase evidence just because it is outside this frame):

${evidenceSoFar}

Return the COMPLETE, UPDATED transcription — not just the new bits:
- Keep everything already correct; do not drop problems you can no longer see.
- Fix mistakes and fill in parts that were previously cut off or unreadable.
- Do not duplicate a problem that is already listed; match by its number.
- Only include a problem when every character of its statement is clearly
  readable in THIS frame. Set clear=true and a confidence from 0 to 1 only
  when you can defend the transcription. If it is partial or uncertain, omit
  it, add a precise description to unreadable, and point at it with
  next_target. Never return a guessed statement with clear=false as filler.
- complete means the entire statement is visible and readable; clear means the
  characters themselves are trustworthy.
- observations is evidence only and is never an assignment problem. Use it for
  a quarter-page close-up: a few visible words are useful even when the rest of
  that problem is below the frame. Never fill missing characters with guesses
  or ellipses.
- Keep next_target_short to 20 characters or fewer. It must be a compact HUD
  label such as "Below problem 4" or "Top-right, line 2"; put the full
  explanation in next_target and advice_detail.
- List what this frame changed in "changes" (e.g. "added problem 4", "fixed
  exponent in problem 2"). If nothing changed, return an empty array.
- Set done=true only when every problem you have transcribed is complete and
  all four paper edges have been seen across the sequence of frames. The whole
  page does NOT need to fit in one frame: the operator may scan top, middle,
  and bottom sections. If any edge or problem may still be missing, keep
  done=false and identify the next section in next_target.
${note ? `\nOPERATOR NOTE FOR THIS FRAME: ${note}\n` : ""}`;
}

type GeminiResult = {
    frame_quality: string;
    framing: {
        full_page_visible: boolean;
        cut_off_edges: string[];
        camera_advice: string;
        advice_detail: string;
        region: string;
        edges_seen: string[];
        next_target: string;
        next_target_short: string;
        more_content_beyond: string[];
    };
    assignment: Assignment;
    changes: string[];
    confidence: number;
    unreadable: string[];
    observations: Observation[];
    done: boolean;
};

/** Whether trying the next model can plausibly help. A rejected id, a
 *  rate-limited model or a transient upstream failure are all worth another
 *  attempt; a malformed request (400) would fail identically on a sibling model,
 *  so it only earns a retry once the chain crosses to a different PROVIDER —
 *  see stepIsWorthIt. A bad key (401/403) is worth crossing providers too, since
 *  the other one's key is a different secret entirely. */
function worthFallback(status: number): boolean {
    return status === 404 || status === 429 || status >= 500;
}

/** One rung of the chain: which provider, which model there. */
type Attempt = { provider: "gemini" | "mistral"; model: string };

/**
 * Gemini first, both of its models, then Mistral's two.
 *
 * Order is the point. Gemini is the primary because the prompt and the schema
 * were written against it and it is what every capture so far has been read by;
 * Mistral exists for the failures a second Gemini model cannot survive. A rung
 * with no key or no model id is dropped rather than attempted, so a deployment
 * that never sets MISTRAL_API_KEY has exactly the behaviour it had before.
 */
function buildChain(): Attempt[] {
    const chain: Attempt[] = [];
    if (cfg.geminiKey) {
        if (cfg.model) chain.push({ provider: "gemini", model: cfg.model });
        if (cfg.modelFallback)
            chain.push({ provider: "gemini", model: cfg.modelFallback });
    }
    if (cfg.mistralKey) {
        if (cfg.mistralModel)
            chain.push({ provider: "mistral", model: cfg.mistralModel });
        if (cfg.mistralModelFallback)
            chain.push({ provider: "mistral", model: cfg.mistralModelFallback });
    }
    return chain;
}

/**
 * Which rung to try after `chain[i]` failed with `err`, or -1 to give up.
 *
 * Within a provider only a retryable failure earns another call: a 400 means
 * this code built a request that provider rejects, and its sibling model would
 * reject it identically — spending a second call to learn that is just money.
 *
 * But it is NOT a reason to stop, and that distinction is the whole point of
 * having a second provider. An invalid key is a 400 from Google ("API key not
 * valid"), not a 401; so is a request shape one API dislikes and the other is
 * fine with. Those must skip past the rest of this provider's models and try the
 * other one, whose endpoint, key and schema dialect are all different. Anything
 * genuinely unrecoverable — a bad image both providers refuse — simply fails
 * again over there, once, and the capture fails with the last error.
 */
function nextRung(err: any, chain: Attempt[], i: number): number {
    if (err?.fallbackWorthwhile && i + 1 < chain.length) return i + 1;
    const provider = chain[i].provider;
    for (let k = i + 1; k < chain.length; k++) {
        if (chain[k].provider !== provider) return k;
    }
    return -1;
}

/** Primary model, then GEMINI_MODEL_FALLBACK if the primary fails in a way the
 *  other model might survive, then Mistral. A capture costs one API call per
 *  rung, so a later rung is only ever reached when the one before it genuinely
 *  failed — never a routine double-spend.
 *
 *  Returns WHICH rung answered along with the reading: with four possible
 *  readers, "what did the model say" is not a complete answer to what happened
 *  during a capture, and the fallbacks are silent by nature — the picture still
 *  gets transcribed, so without this the only trace of Google being down is a
 *  line in the log nobody is watching. */
async function readFrame(
    jpeg: Buffer | Buffer[],
    note: string,
    mime = "image/jpeg",
): Promise<{ result: GeminiResult; used: Attempt }> {
    const chain = buildChain();
    if (!chain.length) {
        throw new Error("no reader configured: set GEMINI_API_KEY or MISTRAL_API_KEY");
    }

    let lastErr: any;
    let attempts = 0;
    for (let i = 0; i >= 0 && i < chain.length; ) {
        const rung = chain[i];
        attempts++;
        try {
            const result =
                rung.provider === "gemini"
                    ? await callGemini(rung.model, jpeg, note, mime)
                    : await callMistral(rung.model, jpeg, note, mime);
            if (attempts > 1) {
                console.warn(
                    `[read] ${rung.provider}/${rung.model} answered after ${attempts - 1} failed attempt(s)`,
                );
            }
            return { result, used: rung };
        } catch (e: any) {
            lastErr = e;
            const next = nextRung(e, chain, i);
            if (next < 0) throw e;
            console.warn(
                `[read] ${rung.provider}/${rung.model} failed (${e.message?.slice(0, 160)}) — retrying on ${chain[next].provider}/${chain[next].model}`,
            );
            emit("model_fallback", {
                from: rung.model,
                from_provider: rung.provider,
                to: chain[next].model,
                to_provider: chain[next].provider,
                reason: String(e?.message ?? e).slice(0, 300),
            });
            i = next;
        }
    }
    throw lastErr;
}

async function callGemini(
    model: string,
    jpeg: Buffer | Buffer[],
    note: string,
    // Camera frames are always JPEG; an uploaded photo is whatever the phone's
    // gallery holds, and Gemini needs to be told which. Sending image/jpeg for
    // a PNG works often enough to be a trap: it fails on the one HEIC shot you
    // actually needed, long after this was written.
    mime = "image/jpeg",
): Promise<GeminiResult> {
    const images = Array.isArray(jpeg) ? jpeg : [jpeg];
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
                        ...images.map((image) => ({
                            inline_data: { mime_type: mime, data: image.toString("base64") },
                        })),
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

/**
 * The same schema, in the dialect Mistral's strict mode wants.
 *
 * Gemini's `responseSchema` and OpenAI-style `json_schema` are both JSON Schema
 * as far as this schema uses it, with one difference that matters: strict mode
 * refuses an object that doesn't forbid extra keys. Adding that here — rather
 * than to RESPONSE_SCHEMA itself — keeps ONE schema as the source of truth, so
 * a field added for Gemini cannot silently fail to reach Mistral.
 */
function strictJsonSchema(node: any): any {
    if (Array.isArray(node)) return node.map(strictJsonSchema);
    if (!node || typeof node !== "object") return node;
    const out: any = {};
    for (const [k, v] of Object.entries(node)) out[k] = strictJsonSchema(v);
    if (out.type === "object") out.additionalProperties = false;
    return out;
}

/**
 * Read the frame with Mistral. Same contract as callGemini — same prompt, same
 * schema, same GeminiResult back — so everything downstream is unaware of which
 * one answered.
 *
 * Note this cannot read HEIC: Mistral takes jpeg/png/webp/gif, and Gemini is
 * what makes `POST /photo` from an iPhone gallery work. A camera frame is always
 * JPEG, so the chain is intact for captures; a HEIC upload that gets this far has
 * already had both Gemini models fail, and fails here too rather than silently
 * transcribing nothing.
 */
async function callMistral(
    model: string,
    jpeg: Buffer | Buffer[],
    note: string,
    mime = "image/jpeg",
): Promise<GeminiResult> {
    const images = Array.isArray(jpeg) ? jpeg : [jpeg];
    const res = await fetch(`${cfg.mistralBase}/v1/chat/completions`, {
        method: "POST",
        headers: {
            "content-type": "application/json",
            authorization: `Bearer ${cfg.mistralKey}`,
        },
        body: JSON.stringify({
            model,
            temperature: 0, // transcription, not creativity
            messages: [
                {
                    role: "user",
                    content: [
                        ...images.map((image) => ({
                            type: "image_url",
                            image_url: { url: `data:${mime};base64,${image.toString("base64")}` },
                        })),
                        { type: "text", text: buildPrompt(note) },
                    ],
                },
            ],
            response_format: {
                type: "json_schema",
                json_schema: {
                    name: "assignment_reading",
                    strict: true,
                    schema: strictJsonSchema(RESPONSE_SCHEMA),
                },
            },
        }),
    });

    const raw = await res.text();
    if (!res.ok) {
        // A model id Mistral doesn't know is a 400 here, where the same mistake
        // is a 404 at Google — so status alone would have this skip its own
        // sibling on exactly the failure the sibling exists for. Both API ids in
        // this file are configurable and one of them being renamed upstream is
        // the single most likely way this chain ever breaks, so the id is read
        // out of the body rather than inferred from the code.
        const rejectedId = /invalid[_ ]model|model.{0,20}not found/i.test(raw);
        throw Object.assign(
            new Error(
                `Mistral ${res.status} (model="${model}"): ${raw.slice(0, 800)}`,
            ),
            { fallbackWorthwhile: worthFallback(res.status) || rejectedId },
        );
    }
    let body: any;
    try {
        body = JSON.parse(raw);
    } catch {
        throw new Error(`Mistral returned non-JSON: ${raw.slice(0, 400)}`);
    }
    const text = body?.choices?.[0]?.message?.content;
    if (!text)
        throw Object.assign(
            new Error(
                `Mistral returned no content (model="${model}"): ${raw.slice(0, 400)}`,
            ),
            { fallbackWorthwhile: true },
        );
    let parsed: any;
    try {
        parsed = typeof text === "string" ? JSON.parse(text) : text;
    } catch {
        throw Object.assign(
            new Error(
                `model output was not valid JSON (model="${model}"): ${String(text).slice(0, 400)}`,
            ),
            { fallbackWorthwhile: true },
        );
    }

    // Gemini's responseSchema is enforced by Google; strict json_schema is
    // enforced by Mistral — but "enforced" is a claim by the thing being
    // checked, and a reply missing `framing` would crash the merge rather than
    // fail the capture. One shape check, then the fallback can have a go.
    if (
        !parsed ||
        typeof parsed !== "object" ||
        !parsed.framing ||
        typeof parsed.framing !== "object" ||
        !parsed.assignment ||
        !Array.isArray(parsed.assignment.problems)
    ) {
        throw Object.assign(
            new Error(
                `Mistral output did not match the schema (model="${model}"): ${JSON.stringify(parsed).slice(0, 400)}`,
            ),
            { fallbackWorthwhile: true },
        );
    }
    return parsed as GeminiResult;
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
    batch: state.batch,
    problems: state.assignment.problems?.length ?? 0,
    // How much of what we have is finished, and whether the sheet has ever been
    // fully in frame. Together these are "how far along is this really", which
    // a bare problem count cannot say.
    problems_complete: completeCount(),
    full_page_seen: state.full_page_seen,
    edges_seen: state.edges_seen,
    edges_unseen: ["top", "bottom", "left", "right"].filter(
        (edge) => !state.edges_seen.includes(edge),
    ),
    next_target: state.captures.at(-1)?.next_target ?? "",
    last_capture: state.captures.at(-1) ?? null,
});

/** Problems whose statement the model says it has in full. */
function completeCount(): number {
    return (state.assignment.problems ?? []).filter((p) => p.complete).length;
}

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

/**
 * Fold a capture's transcription into the one we already have.
 *
 * The prompt asks for the COMPLETE updated transcription every pass, and mostly
 * that is what comes back. "Mostly" is not a guarantee, and the assignment used
 * to be REPLACED wholesale with whatever the last frame said — so one bad frame
 * could delete the work of a dozen good ones. It doesn't take a misbehaving
 * model: a frame with the bottom half of the sheet out of view genuinely cannot
 * see problems it transcribed two captures ago, and saying so is the honest
 * answer to the question we asked.
 *
 * So the union wins and a problem never goes backwards. A complete statement is
 * never replaced by an incomplete one, a problem that has been seen once is
 * never dropped because a later frame missed it, and a statement that suddenly
 * loses most of its text — what truncation and half-reads look like — is kept
 * as it was.
 */
function mergeAssignment(
    prev: Assignment,
    next: Assignment,
): { merged: Assignment; kept: string[] } {
    const minProblemConfidence = Number(
        process.env.MIN_PROBLEM_CONFIDENCE ?? 0.85,
    );
    // The model is explicitly told to omit uncertain text, but enforce that
    // contract here too. This is the last boundary before anything becomes
    // persistent assignment data.
    const readable = (p: Problem) =>
        p.clear === true &&
        Number(p.confidence ?? 0) >= minProblemConfidence &&
        p.statement_latex.trim().length >= 3;
    const candidates = (next.problems ?? []).filter(readable);
    const byNumber = new Map<string, Problem>();
    for (const p of prev.problems ?? []) byNumber.set(p.number, p);

    /** Problems where we rejected the new reading in favour of what we had. */
    const kept: string[] = [];

    for (const p of candidates) {
        const old = byNumber.get(p.number);
        if (!old) {
            byNumber.set(p.number, p);
            continue;
        }
        // Finished → partial is always a loss.
        if (old.complete && !p.complete) {
            kept.push(p.number);
            continue;
        }
        // Both finished: take the newer reading, which is usually a refinement —
        // unless most of the text vanished, which is not a refinement.
        if (
            old.complete &&
            p.complete &&
            p.statement_latex.length < old.statement_latex.length * 0.6
        ) {
            kept.push(p.number);
            continue;
        }
        byNumber.set(p.number, p);
    }

    // Order: whatever the model last said, then anything it forgot, in the order
    // we already knew them — so a problem the camera has drifted off doesn't
    // jump to the end of the sheet.
    const order: string[] = [];
    for (const p of candidates) order.push(p.number);
    for (const p of prev.problems ?? []) {
        if (!order.includes(p.number)) order.push(p.number);
    }

    return {
        merged: {
            // An empty field on this frame doesn't unname the assignment.
            title: next.title || prev.title,
            subject: next.subject || prev.subject,
            instructions_latex:
                next.instructions_latex || prev.instructions_latex,
            problems: order
                .map((n) => byNumber.get(n))
                .filter((p): p is Problem => Boolean(p)),
        },
        kept,
    };
}

/**
 * Persist exact line fragments independently of the final assignment.
 * A later close-up can replace the same line_key with a longer/clearer span;
 * moving the camera to another part of the page cannot erase earlier evidence.
 */
function mergeObservations(
    prev: Observation[],
    next: Observation[],
): Observation[] {
    const minConfidence = Number(process.env.MIN_OBSERVATION_CONFIDENCE ?? 0.65);
    const byKey = new Map<string, Observation>();
    for (const observation of prev ?? []) {
        byKey.set(`${observation.problem_number}:${observation.line_key}`, observation);
    }
    for (const observation of next ?? []) {
        if (
            observation.clear !== true ||
            Number(observation.confidence ?? 0) < minConfidence ||
            observation.text_latex.trim().length < 1
        ) continue;
        const key = `${observation.problem_number}:${observation.line_key}`;
        const old = byKey.get(key);
        // Keep the clearer/longer evidence when two close-ups overlap.
        if (
            !old ||
            observation.confidence > old.confidence ||
            (observation.confidence === old.confidence &&
                observation.text_latex.length > old.text_latex.length)
        ) byKey.set(key, observation);
    }
    return [...byKey.values()];
}

/** What Gemini accepts inline, and therefore what POST /photo accepts. */
const GEMINI_IMAGE_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
];

/**
 * Ceiling on an uploaded photo. Gemini's own inline limit is around 20MB for
 * the whole request and base64 costs a third on top, so this leaves room for
 * the prompt without ever being the thing that fails.
 */
const MAX_PHOTO_BYTES = Number(process.env.MAX_PHOTO_BYTES ?? 12_000_000);

/** Ceiling on a typed assignment. Costs no API call, but it does get rendered. */
const MAX_TEXT_CHARS = Number(process.env.MAX_TEXT_CHARS ?? 200_000);

/**
 * Archive the current attempt and start a clean one.
 *
 * Shared by POST /reset and POST /photo — a photo of a new sheet needs exactly
 * this, and two copies of "which fields make a fresh version" is how one of
 * them ends up forgetting `full_page_seen`.
 */
function resetAssignment(): { version: number; archived: boolean } {
    const previous = state;
    // Worth keeping if it holds anything, which is NOT the same as "it had
    // captures". A typed assignment (POST /assignment) has real content and
    // zero captures, and gating on the capture count alone silently dropped it
    // the moment you edited the text — the version picker on the glasses would
    // show only the newest, and the older ones were gone for good.
    const worthKeeping =
        previous.capture_count > 0 || (previous.assignment.problems?.length ?? 0) > 0;
    if (previous.job.running) {
        // a reset implies "stop what you're doing"
        previous.job.running = false;
        wakeFromSleep?.();
    }
    if (worthKeeping) {
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
    emit("reset", { version: state.version, archived: worthKeeping });
    return { version: state.version, archived: worthKeeping };
}

// One capture at a time: two overlapping calls would each merge against the same
// "before" state and the second would clobber the first.
let capturing = false;

/**
 * @param supplied A photo to read INSTEAD of grabbing one off the camera —
 *        an upload from the phone's gallery (see POST /photo). Everything after
 *        the grab is deliberately identical: the same model call, the same
 *        merge, the same events, so a photographed sheet and a captured one are
 *        the same kind of thing to every consumer downstream.
 */
async function doCapture(
    note: string,
    supplied?: Buffer | Buffer[],
    mime = "image/jpeg",
) {
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
            source: supplied ? (Array.isArray(supplied) ? "batch" : "upload") : "camera",
        });

        const frames = supplied
            ? (Array.isArray(supplied) ? supplied : [supplied])
            : [await grabFrame()];
        const jpeg = frames.at(-1)!;
        // Written even for an upload: /frame.jpg is "the frame the last capture
        // read", and after this one that is the photo.
        writeAtomic(FRAME_FILE, jpeg);
        emit("frame_grabbed", {
            bytes: frames.reduce((sum, frame) => sum + frame.length, 0),
            frames: frames.length,
            ms: Date.now() - startedAt,
            source: supplied
                ? (Array.isArray(supplied) ? "batch" : "upload")
                : cfg.snapshotUrl
                  ? "gateway"
                  : "rtsp",
        });

        // The reader actually used may end up being a fallback — a
        // model_fallback event follows for each rung that failed, and
        // model_response names the one that answered.
        const chain = buildChain();
        emit("model_request", {
            model: chain[0]?.model ?? "",
            provider: chain[0]?.provider ?? "",
            chain: chain.map((a) => `${a.provider}/${a.model}`),
        });
        const modelStart = Date.now();
        const { result, used } = await readFrame(frames, note, mime);
        emit("model_response", {
            ms: Date.now() - modelStart,
            provider: used.provider,
            model: used.model,
            frame_quality: result.frame_quality,
            camera_advice: result.framing.camera_advice,
            advice_detail: result.framing.advice_detail,
            full_page_visible: result.framing.full_page_visible,
            cut_off_edges: result.framing.cut_off_edges ?? [],
            region: result.framing.region ?? "",
            edges_seen: result.framing.edges_seen ?? [],
            next_target: result.framing.next_target ?? "",
            next_target_short: result.framing.next_target_short ?? "",
            more_content_beyond: result.framing.more_content_beyond ?? [],
            unreadable: result.unreadable ?? [],
            observations: result.observations ?? [],
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

        const minFrameConfidence = Number(
            process.env.MIN_FRAME_CONFIDENCE ?? 0.8,
        );
        // A frame with no paper, blur, glare, darkness, or low overall
        // confidence is feedback only. It still counts as a capture because it
        // cost an API call, but it cannot mutate the assignment.
        const usable =
            result.frame_quality === "good" &&
            Number(result.confidence ?? 0) >= minFrameConfidence;
        let kept: string[] = [];
        if (usable) {
            const merge = mergeAssignment(state.assignment, result.assignment);
            state.assignment = merge.merged;
            kept = merge.kept;
            state.observations = mergeObservations(state.observations, result.observations);
        }

        if (result.framing.full_page_visible) state.full_page_seen = true;
        for (const edge of result.framing.edges_seen ?? []) {
            if (["top", "bottom", "left", "right"].includes(edge) && !state.edges_seen.includes(edge)) {
                state.edges_seen.push(edge);
            }
        }

        // `done` is a claim, and it is now checked rather than believed.
        //
        // The model can only speak for what it can see. Asked "is the whole
        // assignment captured?" while looking at the top two thirds of a sheet,
        // it can answer yes in perfectly good faith — every problem IN FRAME is
        // complete — and the job stops with problem 25 never read. That is the
        // failure this exists to prevent: it is silent, it looks like success,
        // and the only sign is a solution that answers fewer problems than the
        // paper has.
        //
        // Finishing is cumulative: the model must say so, every problem we hold
        // must be complete, and all four paper edges must have been observed
        // across the attempt. The camera can therefore read a close-up section
        // at a time; no single frame has to contain the entire sheet.
        const problems = state.assignment.problems ?? [];
        const allComplete = problems.length > 0 && problems.every((p) => p.complete);
        const coverageComplete = ["top", "bottom", "left", "right"].every((edge) =>
            state.edges_seen.includes(edge),
        );
        state.done =
            Boolean(result.done) && usable && allComplete && coverageComplete;

        if (result.done && !state.done) {
            emit("done_rejected", {
                reason: !usable
                    ? "frame is not clear enough to accept transcription"
                    : !allComplete
                      ? "not every problem is complete"
                      : "not all four paper edges have been seen across the scan",
                problems: problems.length,
                problems_complete: completeCount(),
                full_page_seen: state.full_page_seen,
            });
        }

        const log: CaptureLog = {
            n: state.capture_count,
            at: new Date().toISOString(),
            provider: used.provider,
            model: used.model,
            frame_quality: result.frame_quality,
            full_page_visible: result.framing.full_page_visible,
            camera_advice: result.framing.camera_advice,
            advice_detail: result.framing.advice_detail,
            cut_off_edges: result.framing.cut_off_edges ?? [],
            next_target: result.framing.next_target ?? "",
            next_target_short: result.framing.next_target_short ?? "",
            region: result.framing.region ?? "",
            unreadable: result.unreadable ?? [],
            observations: result.observations ?? [],
            more_content_beyond: result.framing.more_content_beyond ?? [],
            // Never advertise a textual change that was rejected by the
            // server-side evidence gate.
            changes: usable ? result.changes ?? [] : [],
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
            problems_complete: completeCount(),
            full_page_seen: state.full_page_seen,
            edges_seen: state.edges_seen,
            edges_unseen: ["top", "bottom", "left", "right"].filter(
                (edge) => !state.edges_seen.includes(edge),
            ),
            next_target: log.next_target,
            next_target_short: log.next_target_short,
            observations: state.observations,
            // Problems whose older reading we preferred to this frame's.
            kept: kept.length ? kept : undefined,
            assignment: state.assignment,
        });
        if (state.done)
            emit("done", {
                version: state.version,
                problems: state.assignment.problems?.length ?? 0,
                problems_complete: completeCount(),
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

// ---------------------------------------------------------- manual batches ----

function batchFolder(): string {
    return join(BATCH_DIR, `v${state.version}`);
}

function startBatch(note: string, maxSnapshots: number): Batch {
    if (state.job.running) throw Object.assign(new Error("a live scan is running"), { status: 409 });
    if (capturing || state.batch.active || state.batch.processing) {
        throw Object.assign(new Error("a snapshot batch is already active"), { status: 409 });
    }
    resetAssignment();
    state.batch = {
        ...idleBatch(),
        active: true,
        started_at: new Date().toISOString(),
        max_snapshots: Math.max(1, Math.min(maxSnapshots, cfg.maxBatchSnapshots)),
        note,
    };
    mkdirSync(batchFolder(), { recursive: true });
    saveState();
    emit("batch_started", { batch: state.batch });
    return state.batch;
}

async function takeBatchSnapshot(): Promise<Batch> {
    if (!state.batch.active || state.batch.processing) {
        throw Object.assign(new Error("no snapshot batch is active"), { status: 409 });
    }
    if (state.batch.snapshot_count >= state.batch.max_snapshots) {
        throw Object.assign(new Error("snapshot batch limit reached"), { status: 409 });
    }
    const frame = await grabFrame();
    const n = state.batch.snapshot_count + 1;
    const path = join(batchFolder(), `frame-${String(n).padStart(4, "0")}.jpg`);
    writeAtomic(path, frame);
    state.batch.snapshot_count = n;
    saveState();
    emit("batch_snapshot", {
        n,
        bytes: frame.length,
        max_snapshots: state.batch.max_snapshots,
    });
    return state.batch;
}

/**
 * Walk away from a batch without spending a model call.
 *
 * The frames go with it: they are only ever read as one batch, so keeping them
 * would leave the next reading of this version silently carrying pictures the
 * operator already threw away. The version is NOT bumped — `start` did that,
 * and a cancel should leave the sheet exactly as blank as it found it.
 *
 * Idempotent, because the thing you press it from is a menu on a pair of
 * glasses several hops from this state: "there was nothing to cancel" and "the
 * cancel worked" want the same answer.
 */
function cancelBatch(): Batch {
    if (state.batch.processing) {
        throw Object.assign(new Error("the batch is already being read"), { status: 409 });
    }
    if (state.batch.active) {
        rmSync(batchFolder(), { recursive: true, force: true });
    }
    state.batch = idleBatch();
    saveState();
    emit("batch_cancelled", { batch: state.batch });
    return state.batch;
}

async function processBatch(): Promise<void> {
    const version = state.version;
    const folder = batchFolder();
    const count = state.batch.snapshot_count;
    const note = state.batch.note;
    const files = Array.from({ length: count }, (_, i) =>
        join(folder, `frame-${String(i + 1).padStart(4, "0")}.jpg`),
    ).filter(existsSync);
    state.batch.active = false;
    state.batch.processing = true;
    state.batch.finished_at = null;
    saveState();
    emit("batch_processing", { snapshots: files.length });
    try {
        if (state.version !== version || !files.length) throw new Error("snapshot batch is empty");
        const frames = files.map((file) => readFileSync(file));
        const totalBytes = frames.reduce((sum, frame) => sum + frame.length, 0);
        if (totalBytes > cfg.maxBatchBytes) {
            throw new Error(`snapshot batch is ${(totalBytes / 1e6).toFixed(1)}MB; limit is ${(cfg.maxBatchBytes / 1e6).toFixed(1)}MB`);
        }
        await doCapture(note, frames);
        state.batch.processing = false;
        state.batch.finished_at = new Date().toISOString();
        saveState();
        emit("batch_finished", { batch: state.batch, done: state.done });
    } catch (error) {
        state.batch.processing = false;
        state.batch.error = String((error as Error)?.message ?? error);
        state.batch.finished_at = new Date().toISOString();
        saveState();
        emit("batch_failed", { error: state.batch.error });
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

// ---------------------------------------------------------------- archive ---
//
// /reset files the outgoing attempt away rather than deleting it, and until now
// nothing could read those files back — the archive was write-only, which makes
// it a backup nobody can restore from. These three routes are the read side.
//
// The listing includes the LIVE attempt alongside the filed ones, because a
// caller offering "which scan do you want to look at?" wants one list, and the
// difference between "on disk" and "in memory" is this module's problem rather
// than theirs. `archived` says which is which.

type ArchiveEntry = {
    version: number;
    created_at: string;
    updated_at: string;
    capture_count: number;
    done: boolean;
    problems: number;
    title: string;
    /** False for the attempt still in progress — it isn't a file yet. */
    archived: boolean;
};

/** Archive filenames are `v{version}-{created_at}.json`; the version leads. */
function archiveFiles(): { version: number; file: string }[] {
    try {
        return readdirSync(ARCHIVE_DIR)
            .filter((f) => f.endsWith(".json"))
            .map((f) => ({
                version: Number(/^v(\d+)-/.exec(f)?.[1] ?? NaN),
                file: join(ARCHIVE_DIR, f),
            }))
            .filter((e) => Number.isFinite(e.version))
            .sort((a, b) => b.version - a.version);
    } catch {
        return []; // nothing has ever been reset, so there is no archive dir
    }
}

function readArchived(version: number): State | null {
    const hit = archiveFiles().find((e) => e.version === version);
    if (!hit) return null;
    try {
        return JSON.parse(readFileSync(hit.file, "utf8")) as State;
    } catch (e) {
        // One corrupt file must not take the listing down with it.
        console.error(`[!] archive v${version} unreadable:`, e);
        return null;
    }
}

const describeAttempt = (s: State, archived: boolean): ArchiveEntry => ({
    version: s.version,
    created_at: s.created_at,
    updated_at: s.updated_at,
    capture_count: s.capture_count,
    done: s.done,
    problems: s.assignment.problems.length,
    title: s.assignment.title,
    archived,
});

/** Newest first, live attempt at the head. */
function archiveList(): ArchiveEntry[] {
    const filed = archiveFiles()
        .map((e) => readArchived(e.version))
        .filter((s): s is State => s !== null)
        // A version equal to the live one would be a duplicate of the head.
        .filter((s) => s.version !== state.version)
        .map((s) => describeAttempt(s, true));
    return [describeAttempt(state, false), ...filed];
}

/**
 * Rescue a statement the model wrote as ONE LaTeX expression.
 *
 * The prompt asks for prose with `$…$` islands, and most of the time that is
 * what comes back. Sometimes it doesn't: the whole sentence arrives as bare
 * LaTeX with the words in `\text{…}` and no `$` anywhere —
 *
 *   \text{Решите уравнение } 2x^2 - 3x + \sqrt{4 - x} = 27.
 *
 * Nothing downstream can render that. A markdown renderer has no delimiter to
 * trigger on, so it prints the backslashes verbatim, and the solver on the
 * glasses is handed the same soup. Wrapping the lot in `$…$` instead would
 * render but never wrap — one unbreakable line off the side of a 576px display.
 *
 * So: turn it back into what was asked for. `\text{…}` runs become prose, and
 * the mathematics between them becomes `$…$`. Strings that already contain `$`
 * are left exactly as they are.
 */
function normalizeLatex(raw: string): string {
    const s = (raw ?? "").trim();
    // Already delimited, empty, or plain prose with nothing to fix.
    if (!s || s.includes("$") || !s.includes("\\")) return s;

    const out: string[] = [];
    /** Append, keeping a space at every prose/math boundary. */
    const push = (text: string) => {
        if (!text) return;
        const last = out[out.length - 1];
        if (last && !/\s$/.test(last) && !/^\s/.test(text)) out.push(" ");
        out.push(text);
    };

    const TEXT = "\\text{";
    let i = 0;
    let mathFrom = 0;

    const flushMath = (end: number) => {
        let chunk = s.slice(mathFrom, end).trim();
        if (!chunk) return;
        // Sentence punctuation belongs to the sentence. Left inside the math it
        // picks up math spacing and reads as part of the formula.
        const tail = /[.,;:]+$/.exec(chunk);
        if (tail) chunk = chunk.slice(0, -tail[0].length).trim();
        if (chunk) push(`$${chunk}$`);
        if (tail) out.push(tail[0]);
    };

    while (i < s.length) {
        if (!s.startsWith(TEXT, i)) {
            i++;
            continue;
        }
        flushMath(i);
        // Brace matching rather than a regex: \text{} legitimately contains
        // nested groups, and a lazy match would end at the first inner brace.
        let depth = 1;
        let j = i + TEXT.length;
        for (; j < s.length && depth > 0; j++) {
            if (s[j] === "{") depth++;
            else if (s[j] === "}") depth--;
        }
        push(s.slice(i + TEXT.length, j - 1));
        i = j;
        mathFrom = i;
    }
    flushMath(s.length);

    const joined = out.join("").replace(/[ \t]+/g, " ").trim();
    // Nothing recognisable was found — better the original than a mangling.
    return joined || s;
}

/**
 * Markdown back into an Assignment — the inverse of toMarkdown below.
 *
 * For an assignment you TYPED rather than photographed (POST /assignment). It
 * has to produce the same structure a capture does, because everything
 * downstream counts problems out of it: the glasses' footer, the solve button's
 * "3 problems read", and the `done` gate.
 *
 * Deliberately forgiving. It round-trips this server's own output exactly, and
 * for anything else it degrades rather than failing: text with no `##` headings
 * becomes a single instructions block, which renders correctly and simply
 * reports one problem instead of pretending to know better.
 */
function fromMarkdown(markdown: string): Assignment {
    const lines = markdown.replace(/\r\n/g, "\n").split("\n");
    const out = emptyAssignment();

    let cursor = 0;
    if (lines[cursor]?.startsWith("# ")) {
        out.title = lines[cursor]!.slice(2).trim();
        cursor++;
    }
    while (lines[cursor]?.trim() === "") cursor++;
    const subject = /^\*([^*]+)\*$/.exec(lines[cursor]?.trim() ?? "");
    if (subject) {
        out.subject = subject[1]!.trim();
        cursor++;
    }

    const preamble: string[] = [];
    for (; cursor < lines.length; cursor++) {
        if (lines[cursor]!.startsWith("## ")) break;
        preamble.push(lines[cursor]!);
    }
    out.instructions_latex = preamble.join("\n").trim();

    let current: Problem | null = null;
    const body: string[] = [];
    const flush = () => {
        if (!current) return;
        current.statement_latex = body.join("\n").trim();
        out.problems.push(current);
        body.length = 0;
    };
    for (; cursor < lines.length; cursor++) {
        const heading = /^##\s+(.*)$/.exec(lines[cursor]!);
        if (heading) {
            flush();
            // `_(incomplete)_` is what toMarkdown appends; anything you type by
            // hand is complete by construction — you wrote the whole thing.
            const label = heading[1]!.replace(/\s*_\(incomplete\)_\s*$/, "").trim();
            current = { number: label, statement_latex: "", complete: true };
        } else if (current) {
            body.push(lines[cursor]!);
        }
    }
    flush();

    // No headings at all: one problem, so the counts downstream mean something.
    if (!out.problems.length && out.instructions_latex) {
        out.problems.push({
            number: "1",
            statement_latex: out.instructions_latex,
            complete: true,
        });
        out.instructions_latex = "";
    }
    return out;
}

function toMarkdown(a: Assignment): string {
    const lines: string[] = [];
    if (a.title) lines.push(`# ${a.title}`, "");
    if (a.subject) lines.push(`*${a.subject}*`, "");
    if (a.instructions_latex) lines.push(normalizeLatex(a.instructions_latex), "");
    for (const p of a.problems ?? []) {
        lines.push(
            `## ${p.number}${p.complete ? "" : "  _(incomplete)_"}`,
            "",
            normalizeLatex(p.statement_latex),
            "",
        );
    }
    return lines.join("\n");
}

const server = Bun.serve({
    port: cfg.port,
    hostname: "0.0.0.0",
    // /events is a long-lived SSE stream and Bun's default is 10s, which is
    // shorter than the 20s keep-alive ping below — so every listener was being
    // dropped before its first heartbeat and reconnecting in a loop. Must stay
    // comfortably above that ping interval. (Bun caps this at 255.)
    idleTimeout: 120,
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

        // Manual multi-snapshot mode. Starting a batch creates a fresh attempt;
        // snapshots only grab JPEGs and cost no model calls. Finish sends every
        // stored image to the model together as one reading.
        if (path === "/batch/start" && req.method === "POST") {
            let body: any = {};
            try { body = (await req.json()) ?? {}; } catch { /* empty body */ }
            try {
                const batch = startBatch(
                    typeof body.note === "string" ? body.note : "",
                    Number.isFinite(body.max_snapshots)
                        ? Number(body.max_snapshots)
                        : cfg.maxBatchSnapshots,
                );
                return json({ ok: true, batch }, 202);
            } catch (e: any) {
                return json({ ok: false, error: String(e?.message ?? e) }, e?.status ?? 409);
            }
        }

        if (path === "/batch/snapshot" && req.method === "POST") {
            try {
                return json({ ok: true, batch: await takeBatchSnapshot() });
            } catch (e: any) {
                return json({ ok: false, error: String(e?.message ?? e) }, e?.status ?? 502);
            }
        }

        if (path === "/batch/cancel" && req.method === "POST") {
            try {
                return json({ ok: true, batch: cancelBatch() });
            } catch (e: any) {
                return json({ ok: false, error: String(e?.message ?? e) }, e?.status ?? 409);
            }
        }

        if (path === "/batch/finish" && req.method === "POST") {
            if (!state.batch.active) {
                return json({ ok: false, error: "no snapshot batch is active" }, 409);
            }
            if (!state.batch.snapshot_count) {
                return json({ ok: false, error: "take at least one snapshot first" }, 400);
            }
            void processBatch();
            return json({ ok: true, processing: true, batch: state.batch }, 202);
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

        if (path === "/archive" && req.method === "GET") {
            return json({ current: state.version, versions: archiveList() });
        }

        // /archive/2 and /archive/2.md — one attempt, as JSON or as the same
        // markdown /assignment.md renders for the live one. The live version is
        // served from memory rather than 404'd: callers page through one list
        // and shouldn't have to special-case its first entry.
        const archived = /^\/archive\/(\d+)(\.md)?$/.exec(path);
        if (archived && req.method === "GET") {
            const version = Number(archived[1]);
            const found = version === state.version ? state : readArchived(version);
            if (!found) return json({ error: `no version ${version}` }, 404);
            if (archived[2]) {
                return new Response(toMarkdown(found.assignment), {
                    headers: { "content-type": "text/markdown; charset=utf-8" },
                });
            }
            return json({
                version: found.version,
                created_at: found.created_at,
                updated_at: found.updated_at,
                capture_count: found.capture_count,
                done: found.done,
                archived: version !== state.version,
                assignment: found.assignment,
            });
        }

        if (path === "/frame.jpg" && req.method === "GET") {
            if (!existsSync(FRAME_FILE))
                return json({ error: "no frame captured yet" }, 404);
            return new Response(readFileSync(FRAME_FILE), {
                headers: { "content-type": "image/jpeg" },
            });
        }

        // The camera as it is NOW, which is a different question from /frame.jpg
        // — that one is the last frame a capture happened to take, so it is
        // stale between captures and absent entirely before the first one. This
        // is what you watch while aiming: it costs no API call, only a frame
        // grab, and it answers the one thing the model's advice enum cannot say
        // (which way up the camera is).
        if (path === "/snapshot.jpg" && req.method === "GET") {
            const maxAge = Number(url.searchParams.get("max_age_ms") ?? SNAPSHOT_TTL_MS);
            try {
                const jpeg = await liveFrame(Number.isFinite(maxAge) ? maxAge : SNAPSHOT_TTL_MS);
                return new Response(jpeg, {
                    headers: {
                        "content-type": "image/jpeg",
                        // A preview that polls must never be handed a proxy's
                        // copy of where the camera used to point.
                        "cache-control": "no-store",
                    },
                });
            } catch (e: any) {
                return json({ error: String(e?.message ?? e) }, 502);
            }
        }

        // Start over. The old attempt is archived rather than deleted — flushing
        // progress shouldn't be able to destroy a transcription you wanted.
        if (path === "/reset" && req.method === "POST") {
            return json({ ok: true, ...resetAssignment() });
        }

        // An assignment you TYPED. No camera, no model call, no cost.
        //
        // It lands as a new version exactly as a photo or a scan does, so
        // everything downstream — the glasses' Assignment page, the archive,
        // the solve button — treats it as the assignment, because it is one.
        // The previous attempt is archived, not lost.
        //
        // `done` is true and `full_page_seen` with it: those gates exist to stop
        // the model claiming it has read a whole sheet it only saw two thirds
        // of, and neither doubt applies to text you wrote out yourself.
        if (path === "/assignment" && req.method === "POST") {
            let body: any = {};
            try {
                body = (await req.json()) ?? {};
            } catch {
                return json({ ok: false, error: "expected a JSON body" }, 400);
            }
            const markdown = typeof body.markdown === "string" ? body.markdown.trim() : "";
            if (!markdown) return json({ ok: false, error: "markdown is required" }, 400);
            if (markdown.length > MAX_TEXT_CHARS) {
                return json(
                    { ok: false, error: `markdown is ${markdown.length} chars, limit is ${MAX_TEXT_CHARS}` },
                    413,
                );
            }

            const previous = resetAssignment();
            state.assignment = fromMarkdown(markdown);
            state.done = true;
            state.full_page_seen = true;
            state.updated_at = new Date().toISOString();
            saveState();

            emit("assignment_updated", {
                capture: null,
                needs_adjustment: false,
                done: true,
                problems: state.assignment.problems.length,
                problems_complete: completeCount(),
                full_page_seen: true,
                source: "typed",
                assignment: state.assignment,
            });
            emit("done", {
                version: state.version,
                problems: state.assignment.problems.length,
                problems_complete: completeCount(),
            });

            return json({
                ok: true,
                version: state.version,
                archived: previous.archived,
                problems: state.assignment.problems.length,
                assignment: state.assignment,
            });
        }

        // A photo, read as the whole assignment. The body IS the image — an
        // upload from the phone's gallery, see the companion app.
        //
        // It resets first (archiving whatever was there, exactly as /reset does)
        // because a photo is a different sheet, not another look at the one the
        // camera has been building up: merging it would interleave two
        // assignments' problems into one document. `?reset=0` opts out for the
        // case where it IS the same sheet, shot properly.
        if (path === "/photo" && req.method === "POST") {
            const mime = (req.headers.get("content-type") ?? "").split(";")[0]!.trim();
            if (!GEMINI_IMAGE_TYPES.includes(mime)) {
                return json(
                    {
                        ok: false,
                        error: `send the image as the request body with one of: ${GEMINI_IMAGE_TYPES.join(", ")}`,
                        got: mime || "(no content-type)",
                    },
                    415,
                );
            }

            const photo = Buffer.from(await req.arrayBuffer());
            if (photo.length === 0) return json({ ok: false, error: "empty body" }, 400);
            if (photo.length > MAX_PHOTO_BYTES) {
                // Refused rather than sent: the model's request has its own
                // ceiling, and finding it costs a failed call and a long wait.
                return json(
                    {
                        ok: false,
                        error: `photo is ${(photo.length / 1e6).toFixed(1)}MB, limit is ${MAX_PHOTO_BYTES / 1e6}MB`,
                    },
                    413,
                );
            }

            const note = url.searchParams.get("note") ?? "";
            const reset = url.searchParams.get("reset") !== "0";
            const previous = reset ? resetAssignment() : null;
            try {
                const result = await doCapture(note, photo, mime);
                return json({ ok: true, reset: previous, ...result });
            } catch (e: any) {
                console.error("[!] photo capture failed:", e?.message ?? e);
                return json(
                    { ok: false, error: String(e?.message ?? e), reset: previous },
                    e?.status ?? 502,
                );
            }
        }

        return json(
            {
                error: "not found",
                endpoints: [
                    "POST /start",
                    "POST /stop",
                    "GET /events",
                    "POST /capture",
                    "POST /photo",
                    "POST /assignment",
                    "GET /assignment",
                    "GET /assignment.md",
                    "GET /state",
                    "GET /frame.jpg",
                    "GET /snapshot.jpg",
                    "POST /reset",
                    "GET /archive",
                    "GET /archive/:version",
                    "GET /archive/:version.md",
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
// The whole chain, in order, because "which model is reading my paper" now has
// up to four answers and the log is where you look first when one of them is
// missing a key.
const bootChain = buildChain();
console.log(
    bootChain.length
        ? `  readers: ${bootChain.map((a) => `${a.provider}/${a.model}`).join("  →  ")}`
        : `  readers: NONE — set GEMINI_API_KEY or MISTRAL_API_KEY, or every capture fails`,
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
