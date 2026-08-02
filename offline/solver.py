"""
Offline solver: claims a run from evens/server, solves each problem with
Phi-4-mini-reasoning + SymPy, and submits the formatted markdown.

The local model does NOT need to be good at math. It only needs to:
  1. Parse the assignment into individual problems
  2. Translate each problem into SymPy Python code
  3. Format the SymPy output as markdown for the glasses

SymPy does the actual computation — symbolic, exact, no approximations.

Usage:
    python3 solver.py --server http://localhost:8787 \
                      --llama http://localhost:8082 \
                      --token <SOLVER_TOKEN>
    # Or: called by start.sh, which manages the llama-server lifecycle.

Requires: requests, sympy (both pip-installable in Termux).
"""

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from io import StringIO

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import render
except ImportError:
    render = None

try:
    import camera_render
except ImportError:
    camera_render = None

# ── configuration ───────────────────────────────────────────────────────────

MODEL_NAME = "qwen2.5-math (offline)"
SYMPY_TIMEOUT = 30
PROBLEM_TIMEOUT = 90
TOTAL_TIMEOUT = 600

VISION_MODEL = ""

# ── camera feed (offline Camera page) ───────────────────────────────────────
#
# Two LookCam-family cameras, each fed by its own run/capture_snapshot.sh loop
# on the same phone (see lookcam/run/dual_capture.sh), writing a rolling JPEG
# to disk independently and forever. This process never talks to the cameras
# itself — it only reads whichever snapshot file is freshest. See
# _pick_camera_snapshot().
CAMERA_SNAPSHOT_PRIMARY = ""
CAMERA_SNAPSHOT_FALLBACK = ""
# How old a snapshot can be and still count as "live" for picking between the
# two cameras. Matches capture_snapshot.sh's default ~1fps.
CAMERA_FRESH_MS = 4000
# Past this, neither camera is producing anything usable — both capture loops
# retry forever on their own, so this is only ever "tell the truth" territory,
# never a reason to give up.
CAMERA_STALL_MS = 30_000

# ── llama-server client ─────────────────────────────────────────────────────


OLLAMA_MODEL = ""


def _strip_think(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    if "<think>" in text:
        import re as _re
        # Closed tags
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
        # Unclosed tag (model ran out of tokens mid-thought) — drop everything
        text = _re.sub(r"<think>.*", "", text, flags=_re.DOTALL)
    return text.strip()


def llama_generate(url: str, prompt: str, max_tokens: int = 2048,
                   temperature: float = 0.1, stop: list[str] | None = None,
                   timeout: int = 120) -> str:
    """Call llama-server /completion or Ollama /api/generate."""
    if OLLAMA_MODEL:
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "10s",
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
                "stop": stop or [],
            },
        }
        resp = requests.post(f"{url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        return _strip_think(resp.json().get("response", ""))

    payload = {
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": temperature,
        "stop": stop or [],
        "stream": False,
    }
    resp = requests.post(f"{url}/completion", json=payload, timeout=timeout)
    resp.raise_for_status()
    return _strip_think(resp.json().get("content", ""))


# ── SymPy sandbox ───────────────────────────────────────────────────────────


def run_sympy(code: str, timeout: int = SYMPY_TIMEOUT) -> tuple[str | None, str | None]:
    """
    Execute SymPy code in a subprocess with a hard timeout.

    Returns (stdout, None) on success, (None, error_message) on failure.
    """
    preamble = (
        "import sys\n"
        "import warnings\n"
        "warnings.filterwarnings('ignore')\n"
        "from sympy import *\n"
        "x, y, z, t, a, b, c, n, k, m = symbols('x y z t a b c n k m')\n"
    )
    wrapper = preamble + "\n" + code

    try:
        result = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            err = result.stderr.strip()
            # Keep just the last few lines of the traceback.
            err_lines = err.splitlines()
            short_err = "\n".join(err_lines[-4:]) if len(err_lines) > 4 else err
            print(f"  [sympy] error: {short_err[:200]}", file=sys.stderr)
            return None, short_err
        return (output if output else None), None
    except subprocess.TimeoutExpired:
        print(f"  [sympy] timed out after {timeout}s", file=sys.stderr)
        return None, f"Timed out after {timeout}s"
    except Exception as e:
        print(f"  [sympy] failed: {e}", file=sys.stderr)
        return None, str(e)


# ── problem parsing ─────────────────────────────────────────────────────────


def parse_problems(markdown: str) -> list[dict]:
    """
    Split assignment markdown into individual problems.

    Looks for ## headings, numbered lines, or "Problem N" / "Задание N"
    patterns. Returns a list of {number, text} dicts.
    """
    lines = markdown.split("\n")
    problems: list[dict] = []
    current: dict | None = None

    # Patterns that start a new problem.
    heading_re = re.compile(
        r"^#{1,3}\s*(?:(?:Problem|Задание|Задача|Упражнение|№)\s*)?(\d+)",
        re.IGNORECASE,
    )
    numbered_re = re.compile(r"^(\d+)\s*[.)]\s+(.+)")

    for line in lines:
        h = heading_re.match(line)
        n = numbered_re.match(line) if not h else None

        if h:
            if current:
                problems.append(current)
            current = {"number": h.group(1), "text": line}
        elif n:
            if current:
                problems.append(current)
            current = {"number": n.group(1), "text": line}
        elif current is not None:
            current["text"] += "\n" + line
        # Lines before the first problem are title/preamble — skip.

    if current:
        problems.append(current)

    # Clean up whitespace.
    for p in problems:
        p["text"] = p["text"].strip()

    # Fallback: if parsing found nothing, treat the whole thing as one problem.
    if not problems and markdown.strip():
        problems = [{"number": "1", "text": markdown.strip()}]

    return problems


# ── the solver ──────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 3

# ── prompts ────────────────────────────────────────────────────────────────

SOLVE_PROMPT = """\
Solve this math problem step by step. Give the exact answer.
End with your final answer inside \\boxed{{}}.

Problem:
{problem}"""


FORMAT_PROMPT = """\
Format this solution for a 576x288 monochrome display.

Rules:
- Start with ## {number}
- Show key steps and the answer — not a lecture, not a bare number.
- Math: $...$ inline, $$...$$ on its own line.
- End with **Ответ: ...** or **Answer: ...**
- Same language as the problem. Short lines. No tables.
- Output ONLY the formatted section.

Problem:
{problem}

Full solution:
{solution}

Formatted solution:"""


def _remaining(start: float, timeout: int, minimum: int = 10) -> int:
    return max(minimum, timeout - int(time.time() - start))


def _extract_boxed(text: str) -> str | None:
    """Extract content from \\boxed{...}, handling nested braces."""
    idx = text.find("\\boxed{")
    if idx == -1:
        return None
    start = idx + len("\\boxed{")
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _solve_once(llama_url: str, text: str, start: float, timeout: int,
                temp: float = 0.1) -> tuple[str | None, str]:
    """One solve attempt. Returns (boxed_answer, full_response)."""
    try:
        resp = llama_generate(
            llama_url,
            SOLVE_PROMPT.format(problem=text),
            max_tokens=2048,
            temperature=temp,
            timeout=min(180, _remaining(start, timeout)),
        )
    except Exception as e:
        print(f"    generation failed: {e}", file=sys.stderr)
        return None, ""

    answer = _extract_boxed(resp)
    return answer, resp


def solve_problem(llama_url: str, problem: dict, timeout: int = PROBLEM_TIMEOUT) -> str:
    """
    Multi-attempt solver: asks the math model to solve directly,
    extracts \\boxed{answer}, picks the majority across attempts.
    """
    num = problem["number"]
    text = problem["text"]
    start = time.time()

    answers: list[tuple[str, str]] = []  # (boxed_answer, full_response)
    temps = [0.1, 0.3, 0.5]

    for attempt, temp in enumerate(temps[:MAX_ATTEMPTS]):
        if _remaining(start, timeout) < 30:
            break

        print(f"  Problem {num}: attempt {attempt + 1}/{MAX_ATTEMPTS} "
              f"(temp={temp})...", file=sys.stderr)
        answer, response = _solve_once(llama_url, text, start, timeout, temp)

        if answer is not None:
            print(f"  Problem {num}: attempt {attempt + 1} -> "
                  f"\\boxed{{{answer[:60]}}}", file=sys.stderr)
            answers.append((answer, response))
            if len(answers) >= 2:
                if answers[-1][0] == answers[-2][0]:
                    print(f"  Problem {num}: two attempts agree, done", file=sys.stderr)
                    break
        else:
            print(f"  Problem {num}: attempt {attempt + 1} no \\boxed{{}} found",
                  file=sys.stderr)

    if not answers:
        return f"## {num}\n\n*Could not solve this problem offline.*"

    from collections import Counter
    counts = Counter(a for a, _ in answers)
    best_answer = counts.most_common(1)[0][0]
    best_response = next(r for a, r in answers if a == best_answer)

    print(f"  Problem {num}: best: \\boxed{{{best_answer[:60]}}} "
          f"({counts[best_answer]}/{len(answers)} agree)", file=sys.stderr)

    print(f"  Problem {num}: formatting...", file=sys.stderr)
    try:
        formatted = llama_generate(
            llama_url,
            FORMAT_PROMPT.format(number=num, problem=text, solution=best_response),
            max_tokens=1024,
            temperature=0.2,
            timeout=min(120, _remaining(start, timeout)),
        )
        if formatted.strip():
            return formatted.strip()
    except Exception as e:
        print(f"  Problem {num}: formatting failed: {e}", file=sys.stderr)

    return f"## {num}\n\n$$\n{best_answer}\n$$\n\n**Ответ: ${best_answer}$**"


# ── run lifecycle ───────────────────────────────────────────────────────────


def claim_run(server: str, token: str) -> dict | None:
    """Claim the next pending run from evens/server."""
    headers = {}
    if token:
        headers["X-Solver-Token"] = token
    try:
        resp = requests.get(f"{server}/solution/claim", headers=headers, timeout=10)
        data = resp.json()
        if data.get("ok"):
            return data
        return None
    except Exception as e:
        print(f"[solver] claim failed: {e}", file=sys.stderr)
        return None


def submit_solution(server: str, run_token: str, markdown: str,
                    model: str = MODEL_NAME, notes: str = "offline solver") -> bool:
    """Submit the finished solution."""
    try:
        resp = requests.post(
            f"{server}/solution/submit",
            json={
                "run_token": run_token,
                "markdown": markdown,
                "model": model,
                "notes": notes,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            print(f"[solver] submitted: solution {data.get('solution_id')}", file=sys.stderr)
            return True
        print(f"[solver] submit rejected: {data.get('reason')}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[solver] submit failed: {e}", file=sys.stderr)
        return False


def fail_run(server: str, run_token: str, error: str) -> None:
    """Report a failed solve so the glasses show a reason."""
    try:
        requests.post(
            f"{server}/solution/fail",
            json={"run_token": run_token, "error": error},
            timeout=10,
        )
    except Exception:
        pass


# ── main ────────────────────────────────────────────────────────────────────


def solve_assignment(server: str, llama_url: str, token: str) -> bool:
    """
    One solve cycle: claim → parse → solve each problem → submit.
    Returns True if a run was found and solved.
    """
    claim = claim_run(server, token)
    if not claim:
        return False

    run_id = claim.get("run_id")
    run_token = claim.get("run_token", "")
    assignment = claim.get("assignment", {})
    markdown = assignment.get("markdown", "")
    problem_count = assignment.get("problems", 0)
    complete = assignment.get("complete", False)

    print(f"[solver] claimed run {run_id}: {problem_count} problems, "
          f"{'complete' if complete else 'partial'}", file=sys.stderr)

    if not markdown.strip():
        fail_run(server, run_token, "empty assignment")
        return True

    problems = parse_problems(markdown)
    print(f"[solver] parsed {len(problems)} problems", file=sys.stderr)

    start = time.time()
    sections: list[str] = []

    # Extract a title from the assignment if present.
    title_match = re.match(r"^#\s+(.+)", markdown)
    if title_match:
        sections.append(f"# {title_match.group(1).strip()}")

    for problem in problems:
        elapsed = time.time() - start
        if elapsed > TOTAL_TIMEOUT:
            print(f"[solver] total timeout ({TOTAL_TIMEOUT}s) — submitting partial", file=sys.stderr)
            sections.append(f"\n\n*Remaining problems skipped (time limit).*")
            break

        remaining = TOTAL_TIMEOUT - elapsed
        section = solve_problem(
            llama_url, problem,
            timeout=min(PROBLEM_TIMEOUT, int(remaining)),
        )
        sections.append(section)

    solution = "\n\n".join(sections).strip()
    total_time = time.time() - start

    print(f"[solver] solved in {total_time:.0f}s ({len(solution)} chars)", file=sys.stderr)
    submit_solution(server, run_token, solution)
    return True


def get_server_mode(server: str) -> str:
    """Read the mode setting from the server. Returns 'auto' on failure."""
    try:
        resp = requests.get(f"{server}/settings/mode", timeout=5)
        return resp.json().get("value", "auto") or "auto"
    except Exception:
        return "auto"


def effective_grace(server: str, default_grace: int) -> int:
    """How long to wait before claiming, based on the server's mode setting."""
    mode = get_server_mode(server)
    if mode == "offline":
        return 0
    if mode == "online":
        return default_grace * 3
    return default_grace


def poll_loop(server: str, llama_url: str, token: str,
              interval: int = 15, grace: int = 10) -> None:
    """Poll for runs and solve them. Runs until interrupted."""
    print(f"[solver] polling {server} every {interval}s (grace {grace}s)", file=sys.stderr)

    while True:
        try:
            found = solve_assignment(server, llama_url, token)
            if found:
                continue
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[solver] error: {e}", file=sys.stderr)

        time.sleep(interval)


# ── vision OCR ─────────────────────────────────────────────────────────────


VISION_PROMPT = """\
Look at this image of a math problem sheet. Extract ALL the math problems \
you see, numbered exactly as written. Use LaTeX for math notation. \
Output ONLY the problems as a markdown list, nothing else."""


def ocr_image(ollama_url: str, image_b64: str, timeout: int = 120) -> str | None:
    """Send a base64 image to the VL model, return extracted text."""
    if not VISION_MODEL:
        return None
    payload = {
        "model": VISION_MODEL,
        "prompt": VISION_PROMPT,
        "images": [image_b64],
        "stream": False,
        "keep_alive": "10s",
        "options": {"num_predict": 2048, "temperature": 0.1},
    }
    try:
        resp = requests.post(
            f"{ollama_url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"[ocr] failed: {e}", file=sys.stderr)
        return None


# ── local HTTP server ──────────────────────────────────────────────────────
#
# --serve mode: a lightweight HTTP server that mirrors the VPS API shape.
# The glasses app talks to this instead of the remote server when in
# offline mode.  Same endpoints, same JSON, no difference from the
# client's point of view.

import http.server
import threading
from urllib.parse import urlparse, parse_qs

_serve_state: dict = {
    "state": "idle",
    "markdown": "",
    "solution": None,
    "run": None,
    "error": None,
}
_serve_llama_url = ""
_serve_lock = threading.Lock()


def _pick_camera_snapshot() -> tuple[bytes | None, str, float]:
    """The freshest camera's JPEG bytes, which one it was, and its age in ms.

    Primary wins whenever it's fresh; the fallback only takes over once the
    primary's snapshot has gone stale. There is no explicit "switch back":
    each camera's capture_snapshot.sh loop discovers and reconnects forever
    on its own, independently of the other, so the primary reappearing here
    is simply its file becoming fresh again on the next poll.
    """
    now = time.time()
    candidates = []
    for name, path in (("primary", CAMERA_SNAPSHOT_PRIMARY), ("fallback", CAMERA_SNAPSHOT_FALLBACK)):
        if not path:
            continue
        try:
            age_ms = (now - os.path.getmtime(path)) * 1000
        except OSError:
            continue
        candidates.append((name, path, age_ms))

    if not candidates:
        return None, "", 0.0

    fresh = [c for c in candidates if c[2] <= CAMERA_FRESH_MS]
    # Fresh candidates keep the configured preference order (primary first,
    # since that's the order the tuple above was built in); among stale ones,
    # the least stale is the more honest thing to show.
    name, path, age_ms = fresh[0] if fresh else min(candidates, key=lambda c: c[2])

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, "", 0.0
    return data, name, age_ms


def _build_camera_status() -> dict:
    """A minimal AssignmentStatus (see evens/test/src/state.ts) reflecting
    camera-feed health only.

    Offline mode has no scan-tracking pipeline — no per-frame OCR, no framing
    advice, no edge-coverage gate. Getting problems into the assignment is
    POST /assignment/photo (one-shot). This exists so the Camera page's menu
    and corner box read something honest ("nothing read yet") instead of
    "Connecting..." forever; the "start/stop reading" menu entries it offers
    have nothing behind them offline and will answer "couldn't start: refused".
    """
    _, which, age_ms = _pick_camera_snapshot()
    if not which:
        upstream = "disabled"
    elif age_ms <= CAMERA_STALL_MS:
        upstream = "open"
    else:
        upstream = "error"

    markdown = _serve_state["markdown"]
    return {
        "upstream": upstream,
        "running": False,
        "done": bool(markdown),
        "captures": 0,
        "max_captures": 0,
        "reason": None,
        "problems": len(parse_problems(markdown)) if markdown else 0,
        "problems_complete": 0,
        "full_page_seen": False,
        "edges_unseen": [],
        "next_target": "",
        "next_target_short": "",
        "feedback": None,
        "error": None if which else "no camera configured",
        "version": 1,
        "active_version": None,
        "versions": [],
        "last_capture_at": None,
        "batch": {"active": False, "processing": False, "snapshot_count": 0, "max_snapshots": 40},
    }


def _ollama_status() -> dict:
    """Query Ollama /api/ps for loaded models."""
    try:
        resp = requests.get(f"{_serve_llama_url}/api/ps", timeout=3)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        loaded = []
        for m in models:
            size_mb = round(m.get("size", 0) / 1024 / 1024)
            loaded.append({
                "name": m.get("name", ""),
                "size_mb": size_mb,
                "expires_at": m.get("expires_at", ""),
            })
        return {"ok": True, "models": loaded}
    except Exception:
        return {"ok": False, "models": []}


class _LocalHandler(http.server.BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/health":
            return self._json(200, {"ok": True, "backend": "offline"})

        if path == "/settings/mode":
            return self._json(200, {"value": "offline"})

        if path == "/ollama/status":
            return self._json(200, _ollama_status())

        if path == "/solution/status":
            with _serve_lock:
                return self._json(200, _build_status())

        if path == "/tiles":
            with _serve_lock:
                return self._json(200, _build_tiles())

        if path == "/markdown":
            with _serve_lock:
                md = _serve_state["solution"]["markdown"] if _serve_state["solution"] else ""
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self._cors()
            self.end_headers()
            self.wfile.write(md.encode())
            return

        if path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            with _serve_lock:
                data = json.dumps(_build_status())
            self.wfile.write(f"event: status\ndata: {data}\n\n".encode())
            self.wfile.flush()
            try:
                while True:
                    time.sleep(5)
                    with _serve_lock:
                        data = json.dumps(_build_status())
                    self.wfile.write(f"event: status\ndata: {data}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        if path == "/assignment/status":
            with _serve_lock:
                return self._json(200, _build_camera_status())

        if path == "/assignment/camera":
            return self._handle_camera_preview()

        if path == "/assignment/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            try:
                while True:
                    with _serve_lock:
                        data = json.dumps(_build_camera_status())
                    self.wfile.write(f"event: status\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                return

        self._json(404, {"ok": False, "detail": "not found"})

    def _handle_camera_preview(self):
        """GET /assignment/camera — one frame from whichever camera is
        currently active, as tiles (see camera_render.py). Mirrors the shape
        of evens/server/render/camera.ts's PreviewResponse."""
        if camera_render is None or not camera_render.CAMERA_RENDER_AVAILABLE:
            return self._json(503, {"detail": "Pillow not installed"})

        jpeg, which, age_ms = _pick_camera_snapshot()
        if jpeg is None:
            return self._json(503, {"detail": "no camera configured"})
        if age_ms > CAMERA_STALL_MS:
            return self._json(
                503,
                {"detail": f"{which} camera stalled ({round(age_ms / 1000)}s since last frame)"},
            )

        query = parse_qs(urlparse(self.path).query)
        size = 1 if query.get("size", ["4"])[0] == "1" else 4
        try:
            rotate = int(query.get("rotate", ["0"])[0])
        except ValueError:
            rotate = 0
        mode = query.get("mode", ["ink"])[0]

        try:
            preview = camera_render.render_camera_tiles(jpeg, size=size, rotate=rotate, mode=mode)
        except Exception as e:
            print(f"[serve] camera render failed: {e}", file=sys.stderr)
            return self._json(500, {"detail": "render failed"})

        return self._json(200, preview)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        # The photo is the raw image body (see gallery.ts publishPhoto: POST
        # with Content-Type: image/jpeg and the bytes as the body), never
        # JSON — do not run it through json.loads with the other endpoints.
        if path == "/assignment/photo":
            return self._handle_photo(raw)

        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "detail": "invalid JSON body"})

        if path == "/solution/solve":
            return self._handle_solve(body)

        if path == "/solution/cancel":
            return self._handle_cancel()

        if path == "/assignment/markdown":
            with _serve_lock:
                _serve_state["markdown"] = body.get("markdown", "")
            return self._json(200, {"ok": True})

        self._json(404, {"ok": False, "detail": "not found"})

    def _handle_photo(self, raw: bytes):
        """Publish a photo of the assignment sheet.

        The offline substitute for the camera-based reader: no continuous
        scan or framing advice, just one-shot OCR of whatever was photographed
        (see gallery.ts publishPhoto, used by both the companion web app's
        "Publish" button and the glasses Settings page's gallery import).
        """
        if not raw:
            return self._json(400, {"ok": False, "detail": "no image"})
        if not VISION_MODEL:
            return self._json(500, {"ok": False, "detail": "no vision model configured"})

        query = parse_qs(urlparse(self.path).query)
        reset = query.get("reset", ["0"])[0] == "1"

        image_b64 = base64.b64encode(raw).decode()
        text = ocr_image(_serve_llama_url, image_b64)
        if not text:
            return self._json(500, {"ok": False, "detail": "OCR failed"})

        with _serve_lock:
            # Merge onto the existing transcription the way a camera frame
            # would, unless the caller said this is a different sheet.
            if reset or not _serve_state["markdown"]:
                _serve_state["markdown"] = text
            else:
                _serve_state["markdown"] = _serve_state["markdown"].rstrip() + "\n\n" + text
            # A changed assignment answers a different question than whatever
            # was last solved — bring the button back rather than leaving it
            # on a stale "solved".
            if _serve_state["state"] != "solving":
                _serve_state["state"] = "idle"
            problems = len(parse_problems(_serve_state["markdown"]))

        return self._json(200, {"ok": True, "problems": problems, "done": True})

    def do_PUT(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/settings/mode":
            return self._json(200, {"ok": True, "value": body.get("value", "offline")})

        self._json(404, {"ok": False, "detail": "not found"})

    def _handle_solve(self, body: dict):
        with _serve_lock:
            if _serve_state["state"] == "solving":
                return self._json(409, {"ok": False, "detail": "already solving"})
            if body.get("markdown"):
                _serve_state["markdown"] = body["markdown"]
            if not _serve_state["markdown"]:
                return self._json(400, {"ok": False, "detail": "no assignment"})
            _serve_state["state"] = "solving"
            _serve_state["error"] = None

        self._json(200, {"ok": True, "action": "queued"})
        threading.Thread(target=_solve_thread, daemon=True).start()

    def _handle_cancel(self):
        with _serve_lock:
            _serve_state["state"] = "idle"
            _serve_state["run"] = None
        self._json(200, {"ok": True, "action": "cancelled"})

    def log_message(self, fmt, *args):
        print(f"[serve] {args[0]}", file=sys.stderr)


def _build_status() -> dict:
    s = _serve_state
    solution = None
    if s["solution"]:
        solution = {
            "created_at": s["solution"]["created_at"],
            "age_ms": int((time.time() - s["solution"]["created_at"] / 1000) * 1000),
            "model": MODEL_NAME,
            "assignment_version": 1,
            "stale": False,
            "chars": len(s["solution"].get("markdown", "")),
        }
    # The client's button only invites a tap for `idle`/`failed`; without this
    # a fresh server with nothing transcribed yet still reports "idle" and the
    # AI page shows "SOLVE WITH CLAUDE / TAP TO RUN" over zero problems, which
    # then 400s "no assignment" the moment you tap it. See server/solver.ts's
    # getSolverStatus for the online equivalent of this gate.
    state = s["state"]
    if state == "idle" and not s["markdown"]:
        state = "no_assignment"

    return {
        "state": state,
        "assignment": {
            "available": bool(s["markdown"]),
            "version": 1 if s["markdown"] else None,
            "problems": len(parse_problems(s["markdown"])) if s["markdown"] else 0,
            "done": bool(s["markdown"]),
        },
        "solution": solution,
        "run": s["run"],
        "trigger": {"configured": True, "detail": "local solver"},
        "mode": "offline",
        "solutions": 1 if s["solution"] else 0,
        "solution_history": [],
    }


def _build_tiles() -> dict:
    """Render the current solution to tile PNGs (see render.py).

    Cached on the solution dict and keyed by its created_at timestamp, so
    repeated polling (the client hits /tiles on every status change) doesn't
    re-render. If render.py's deps aren't installed, falls back to empty
    pages — the client then renders client-side in the WebView instead (see
    evens/test/src/render/tiles.ts), which is what this replaces.
    """
    sol = _serve_state["solution"]
    if not sol or not sol.get("markdown") or render is None or not render.RENDER_AVAILABLE:
        return {"version": 0, "pages": []}

    version = sol["created_at"]
    if sol.get("tiles_version") != version:
        try:
            sol["tiles"] = render.render_markdown_to_tiles(sol["markdown"])
        except Exception as e:
            print(f"[serve] tile render failed: {e}", file=sys.stderr)
            sol["tiles"] = []
        sol["tiles_version"] = version

    return {"version": version, "pages": sol.get("tiles", [])}


def _solve_thread():
    """Run the solver in a background thread, updating shared state."""
    global _serve_state
    with _serve_lock:
        markdown = _serve_state["markdown"]
        _serve_state["run"] = {
            "id": 1, "state": "solving",
            "created_at": int(time.time() * 1000),
            "age_ms": 0, "claimed": True,
            "trigger": "triggered", "trigger_detail": "local",
            "error": None,
        }

    problems = parse_problems(markdown) if markdown else []
    sections: list[str] = []
    start = time.time()

    for p in problems:
        elapsed = time.time() - start
        if elapsed > TOTAL_TIMEOUT:
            break
        section = solve_problem(
            _serve_llama_url, p,
            timeout=min(PROBLEM_TIMEOUT, int(TOTAL_TIMEOUT - elapsed)),
        )
        sections.append(section)

    solution_md = "\n\n".join(sections).strip()

    with _serve_lock:
        _serve_state["state"] = "solved"
        _serve_state["solution"] = {
            "markdown": solution_md,
            "created_at": int(time.time() * 1000),
        }
        _serve_state["run"] = None

    print(f"[serve] solved: {len(solution_md)} chars", file=sys.stderr)


def serve(host: str, port: int, llama_url: str):
    """Run the local HTTP server."""
    global _serve_llama_url
    _serve_llama_url = llama_url

    server = http.server.HTTPServer((host, port), _LocalHandler)
    print(f"[serve] listening on http://{host}:{port}", file=sys.stderr)
    print(f"[serve] llama: {llama_url}  model: {OLLAMA_MODEL or '(llama.cpp)'}", file=sys.stderr)
    if CAMERA_SNAPSHOT_PRIMARY or CAMERA_SNAPSHOT_FALLBACK:
        print(f"[serve] camera: primary={CAMERA_SNAPSHOT_PRIMARY or '(none)'} "
              f"fallback={CAMERA_SNAPSHOT_FALLBACK or '(none)'}", file=sys.stderr)
    elif camera_render is None or not camera_render.CAMERA_RENDER_AVAILABLE:
        print("[serve] camera: Pillow not installed — /assignment/camera will 503", file=sys.stderr)
    else:
        print("[serve] camera: no --camera-primary/--camera-fallback configured — "
              "/assignment/camera will 503", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[serve] shutting down", file=sys.stderr)
        server.shutdown()


# ── main ────────────────────────────────────────────────────────────────────


def main():
    global SYMPY_TIMEOUT, PROBLEM_TIMEOUT, TOTAL_TIMEOUT, OLLAMA_MODEL, VISION_MODEL
    global CAMERA_SNAPSHOT_PRIMARY, CAMERA_SNAPSHOT_FALLBACK, CAMERA_FRESH_MS

    parser = argparse.ArgumentParser(description="Offline SymPy solver")
    parser.add_argument("--server", default="http://localhost:8787",
                        help="evens/server URL")
    parser.add_argument("--llama", default="http://localhost:8082",
                        help="llama-server or Ollama URL")
    parser.add_argument("--ollama-model", default="",
                        help="Ollama model name (uses /api/generate instead of /completion)")
    parser.add_argument("--vision-model", default="",
                        help="Ollama vision model for OCR (e.g. qwen2.5vl:3b)")
    parser.add_argument("--token", default="",
                        help="SOLVER_TOKEN for claim auth")
    parser.add_argument("--poll", type=int, default=15,
                        help="poll interval (seconds)")
    parser.add_argument("--grace", type=int, default=10,
                        help="grace period before claiming (seconds)")
    parser.add_argument("--once", action="store_true",
                        help="solve one run and exit (don't poll)")
    parser.add_argument("--serve", action="store_true",
                        help="run as local HTTP server (offline backend for the glasses app)")
    parser.add_argument("--serve-host", default="0.0.0.0")
    parser.add_argument("--serve-port", type=int, default=8384)
    parser.add_argument("--sympy-timeout", type=int, default=SYMPY_TIMEOUT)
    parser.add_argument("--problem-timeout", type=int, default=PROBLEM_TIMEOUT)
    parser.add_argument("--total-timeout", type=int, default=TOTAL_TIMEOUT)
    parser.add_argument("--camera-primary", default="",
                        help="path to the primary camera's rolling JPEG snapshot "
                             "(written by lookcam/run/capture_snapshot.sh)")
    parser.add_argument("--camera-fallback", default="",
                        help="path to the fallback camera's rolling JPEG snapshot")
    parser.add_argument("--camera-fresh-ms", type=int, default=CAMERA_FRESH_MS,
                        help="how old a snapshot can be and still count as live "
                             "before /assignment/camera prefers the other camera")
    args = parser.parse_args()

    SYMPY_TIMEOUT = args.sympy_timeout
    PROBLEM_TIMEOUT = args.problem_timeout
    TOTAL_TIMEOUT = args.total_timeout
    OLLAMA_MODEL = args.ollama_model
    VISION_MODEL = args.vision_model
    CAMERA_SNAPSHOT_PRIMARY = args.camera_primary
    CAMERA_SNAPSHOT_FALLBACK = args.camera_fallback
    CAMERA_FRESH_MS = args.camera_fresh_ms

    if args.serve:
        serve(args.serve_host, args.serve_port, args.llama)
    elif args.once:
        found = solve_assignment(args.server, args.llama, args.token)
        sys.exit(0 if found else 1)
    else:
        poll_loop(args.server, args.llama, args.token, args.poll, args.grace)


if __name__ == "__main__":
    main()
