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
import json
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

# ── configuration ───────────────────────────────────────────────────────────

MODEL_NAME = "qwen2.5-math (offline)"
SYMPY_TIMEOUT = 30
PROBLEM_TIMEOUT = 90
TOTAL_TIMEOUT = 600

VISION_MODEL = ""

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
            return self._json(200, {
                "upstream": "disabled", "running": False, "done": False,
                "captures": 0, "max_captures": 0, "reason": None,
                "problems": 0, "problems_complete": 0,
                "full_page_seen": False, "edges_unseen": [],
                "next_target": "", "batch": {"active": False,
                "processing": False, "snapshot_count": 0,
                "max_snapshots": 0}, "feedback": None,
                "error": None, "version": 0, "versions": [],
                "last_capture_at": None,
            })

        self._json(404, {"ok": False, "detail": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/solution/solve":
            return self._handle_solve(body)

        if path == "/solution/cancel":
            return self._handle_cancel()

        if path == "/assignment/markdown":
            with _serve_lock:
                _serve_state["markdown"] = body.get("markdown", "")
            return self._json(200, {"ok": True})

        if path == "/assignment/photo":
            image_b64 = body.get("image", "")
            if not image_b64:
                return self._json(400, {"ok": False, "detail": "no image"})
            text = ocr_image(_serve_llama_url, image_b64)
            if text:
                with _serve_lock:
                    _serve_state["markdown"] = text
                return self._json(200, {"ok": True, "problems": len(parse_problems(text)), "text": text})
            return self._json(500, {"ok": False, "detail": "OCR failed"})

        self._json(404, {"ok": False, "detail": "not found"})

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
    return {
        "state": s["state"],
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
    """Placeholder: return empty tiles. Full rendering will use the WebView."""
    return {"version": 0, "pages": []}


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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[serve] shutting down", file=sys.stderr)
        server.shutdown()


# ── main ────────────────────────────────────────────────────────────────────


def main():
    global SYMPY_TIMEOUT, PROBLEM_TIMEOUT, TOTAL_TIMEOUT, OLLAMA_MODEL, VISION_MODEL

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
    args = parser.parse_args()

    SYMPY_TIMEOUT = args.sympy_timeout
    PROBLEM_TIMEOUT = args.problem_timeout
    TOTAL_TIMEOUT = args.total_timeout
    OLLAMA_MODEL = args.ollama_model
    VISION_MODEL = args.vision_model

    if args.serve:
        serve(args.serve_host, args.serve_port, args.llama)
    elif args.once:
        found = solve_assignment(args.server, args.llama, args.token)
        sys.exit(0 if found else 1)
    else:
        poll_loop(args.server, args.llama, args.token, args.poll, args.grace)


if __name__ == "__main__":
    main()
