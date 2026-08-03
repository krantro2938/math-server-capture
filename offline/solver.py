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
import datetime
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

# The prompt for every frame after the first.
#
# THE CAMERA CANNOT SEE A WHOLE LINE. It shows something like a sixth of the
# page, so a line of the assignment is routinely split across two frames — the
# left half in one, the right half in the next. Reading each photograph on its
# own and concatenating the results therefore cannot work: it produces half
# lines, the same corner twice, and problem numbers repeated with different
# fragments under them.
#
# So frames are not read independently. Each one is read AGAINST the
# transcription built so far, and the model returns the whole thing updated —
# which is what lets it recognise that the text in this frame continues a line
# it has already seen, rather than starting a new one. This is the same
# mechanism the online reader uses (`buildPrompt` in
# lookcam/assignment/server.ts, which passes `state.assignment` into every
# frame's prompt); the difference is only that this one accumulates markdown
# rather than a structured JSON assignment, because a 3B model on a phone is
# not going to fill in a schema with per-line evidence keys.
VISION_CONTINUE_PROMPT = """\
You are transcribing ONE assignment sheet that is being photographed a piece \
at a time. This photograph shows only PART of the sheet, and a line of text \
may be cut in half by the edge of the frame.

Here is the transcription so far:

{context}

Look at the photograph and return the COMPLETE updated transcription.

RULES
- Keep every problem already transcribed, even the ones not visible in this \
photograph. Never drop them.
- Match problems by their number. If a problem is already listed, do not add \
it again — improve it in place.
- If this photograph shows the rest of a line that was cut off, join it up \
into one line. If it shows the start of a line you already have the end of, \
put it in front.
- Add any new problem this photograph shows.
- Never guess a character you cannot actually read. Leave it out.
- Use LaTeX for all math: $...$ inline, $$...$$ for display.
- Output ONLY the transcription as markdown, nothing else — no commentary, no \
code fences."""

# How much shorter a re-read may be before it is treated as the model having
# lost the thread rather than tidied up. Same number and the same reasoning as
# the online reader's `mergeAssignment` ("both finished: take the newer
# reading... unless most of the text vanished, which is not a refinement").
MIN_REREAD_RATIO = 0.6


def _strip_fences(text: str) -> str:
    """Drop a ```markdown wrapper if the model added one anyway."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def ocr_image(
    ollama_url: str,
    image_b64: str,
    timeout: int = 120,
    context: str = "",
) -> str | None:
    """Send a base64 image to the VL model, return extracted text.

    With `context`, this is a continuing read: the model gets the transcription
    so far and returns it updated. See VISION_CONTINUE_PROMPT for why that is
    the only thing that works with a frame narrower than a line of text.
    """
    if not VISION_MODEL:
        return None
    prompt = (
        VISION_CONTINUE_PROMPT.format(context=context.strip())
        if context.strip()
        else VISION_PROMPT
    )
    payload = {
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "keep_alive": "10s",
        # A continuing read has to re-emit everything it already knows, so the
        # ceiling has to grow with the sheet rather than sit at one frame's
        # worth of text — an answer cut off mid-transcription is indistinguishable
        # from the model having dropped the rest of the page.
        "options": {
            "num_predict": 2048 + 2 * len(context) // 3,
            "temperature": 0.1,
        },
    }
    try:
        resp = requests.post(
            f"{ollama_url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
        return _strip_fences(resp.json().get("response", ""))
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

# TWO THINGS ARE CALLED A VERSION HERE, and they are not the same thing.
#
#   content_version   bumps whenever the transcription TEXT changes. It is what
#                     `stale` on the AI page compares — "the answer on screen
#                     was written for a different sheet" — and it stands in for
#                     the online server's content hash of the markdown.
#   scan_version      the attempt number. A new one starts when you say this is
#                     a different sheet (a reset, a restart, a photo published
#                     with reset=1); adding another photo to the sheet you are
#                     already reading does not start one. This is what the
#                     Assignment page's picker is built from.
#
# Conflating them gets you either a picker with an entry per photo or a
# solution that never notices the paper changed.
_serve_state: dict = {
    "state": "idle",
    # The live scan: the transcription the solve button will send.
    "markdown": "",
    "content_version": 1,
    "scan_version": 1,
    "scan_created_at": 0,
    "scan_updated_at": 0,
    # How many photographs went into the live scan, for the footer's "c3".
    "captures": 0,
    # Finished scans, NEWEST FIRST. The live one is not in here.
    "scans": [],
    # Which scan the solve button sends, None while it follows the live one.
    # Set from the Assignment page's "Point the AI here".
    "active_scan": None,
    # Every solution this server still holds, NEWEST FIRST. The head is "the"
    # solution; the rest are what the AI page's version picker is built from.
    "solutions": [],
    # Counts from the first solution ever made here, so a version number means
    # the same thing tomorrow as it does today.
    "next_solution_id": 1,
    # The manual batch: photographs taken now, read all at once later. Mirrors
    # AssignmentStatus["batch"] in evens/test/src/state.ts.
    "batch": {"active": False, "processing": False, "snapshots": [], "read_state": None},
    "run": None,
    "error": None,
}
_serve_llama_url = ""
_serve_lock = threading.Lock()

# How many solutions to keep. Each is a few KB of markdown, so this is about
# keeping the state file honest rather than about disk.
MAX_SOLUTIONS = 20
# Same, for scans.
MAX_SCANS = 20
# How many photographs one batch may hold. Each is OCR'd separately by the
# vision model at the end, and that is ~10-20s apiece on the phone.
MAX_BATCH_SNAPSHOTS = 40

# ── keeping it across a restart ─────────────────────────────────────────────
#
# This used to be memory and nothing else, which made the offline mode quietly
# useless: solver.py restarting — a crash, `start.sh` cycling it, Android
# reaping Termux in your pocket — took the assignment and every solution with
# it, and the glasses came back to "NOTHING TO SOLVE" with no way to tell that
# from never having scanned anything. There is no other copy. The phone-side
# tile cache (evens/test/src/render/tileCache.ts) holds *pictures* of a
# document for redrawing; it is never read back as an assignment, and nothing
# uploads it here.
#
# So the state that took work to produce — the transcription, and the answers —
# is written to disk on every change and read back at startup. `run` is
# deliberately not persisted: a solve cannot survive the process that was doing
# it, and restoring one would leave the page waiting forever on a thread that
# does not exist.

STATE_FILE = ""


_RENDER_CACHE_KEYS = ("tiles", "tiles_version", "scan_tiles", "scan_tiles_version")


def _persistable(items: list) -> list:
    """Documents without their render cache — several hundred KB of base64 PNG
    apiece, rebuildable in under a second. See _build_tiles."""
    return [
        {k: v for k, v in item.items() if k not in _RENDER_CACHE_KEYS}
        for item in items
    ]


def _state_snapshot() -> dict:
    """The parts of `_serve_state` worth keeping. Caller holds the lock.

    The batch is not among them: it is a handful of JPEGs held in memory
    mid-gesture, and a batch you were halfway through photographing does not
    survive the process any more than a solve does.
    """
    return {
        "markdown": _serve_state["markdown"],
        "content_version": _serve_state["content_version"],
        "scan_version": _serve_state["scan_version"],
        "scan_created_at": _serve_state["scan_created_at"],
        "scan_updated_at": _serve_state["scan_updated_at"],
        "captures": _serve_state["captures"],
        "active_scan": _serve_state["active_scan"],
        "scans": _persistable(_serve_state["scans"]),
        "next_solution_id": _serve_state["next_solution_id"],
        "solutions": _persistable(_serve_state["solutions"]),
    }


def _save_state():
    """Write the state file. Caller holds `_serve_lock`.

    Via a temporary file and os.replace, so a process killed mid-write leaves
    the previous state intact rather than a truncated file that fails to parse
    on the next boot — which would lose exactly what this is here to protect.
    """
    if not STATE_FILE:
        return
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state_snapshot(), f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        # Never fatal. A server that can't write its state is still a server.
        print(f"[serve] could not save state: {e}", file=sys.stderr)


def _load_state():
    """Read the state file back at startup, if there is one."""
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[serve] ignoring unreadable state file: {e}", file=sys.stderr)
        return

    _serve_state["markdown"] = saved.get("markdown", "") or ""
    _serve_state["content_version"] = saved.get("content_version", 1)
    _serve_state["scan_version"] = saved.get("scan_version", 1)
    _serve_state["scan_created_at"] = saved.get("scan_created_at", 0)
    _serve_state["scan_updated_at"] = saved.get("scan_updated_at", 0)
    _serve_state["captures"] = saved.get("captures", 0)
    _serve_state["active_scan"] = saved.get("active_scan")
    _serve_state["scans"] = [
        s for s in saved.get("scans", []) if s.get("markdown")
    ][:MAX_SCANS]
    _serve_state["solutions"] = [
        s for s in saved.get("solutions", []) if s.get("markdown")
    ][:MAX_SOLUTIONS]
    # Ahead of whatever was saved, so a state file written by an older build
    # (or hand-edited) can't hand out an id that is already taken.
    highest = max((s.get("id", 0) for s in _serve_state["solutions"]), default=0)
    _serve_state["next_solution_id"] = max(saved.get("next_solution_id", 1), highest + 1)
    # Same for the scan number, which the archive also has a claim on.
    top_scan = max((s.get("version", 0) for s in _serve_state["scans"]), default=0)
    _serve_state["scan_version"] = max(_serve_state["scan_version"], top_scan + 1)
    # Whatever it was doing when it died, it isn't doing it now.
    _serve_state["state"] = "solved" if _serve_state["solutions"] else "idle"
    _serve_state["run"] = None
    _serve_state["batch"] = {
        "active": False, "processing": False, "snapshots": [], "read_state": None,
    }

    print(
        f"[serve] restored {len(_serve_state['solutions'])} solution(s), "
        f"{len(_serve_state['scans'])} archived scan(s), live scan v"
        f"{_serve_state['scan_version']}: "
        f"{len(parse_problems(_serve_state['markdown']))} problems",
        file=sys.stderr,
    )


def _pick_solution(solution_id: int | None) -> dict | None:
    """The solution the client asked for, or the newest. Caller holds the lock.

    An id that no longer exists falls back to the newest rather than to
    nothing: the alternative is a blank page on the glasses for a version that
    has aged out, which reads as a broken reader.
    """
    saved = _serve_state["solutions"]
    if not saved:
        return None
    if solution_id is not None:
        for s in saved:
            if s["id"] == solution_id:
                return s
    return saved[0]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _set_assignment(markdown: str, captures: int = 0):
    """Put a transcription in front of the solver. Caller holds the lock.

    `content_version` bumps only when the text actually changes, so re-posting
    the same sheet doesn't age every solution that answers it. A different
    sheet does age them, which is the point: it is what makes `stale` mean
    something and brings the solve button back over an answer to the previous
    page.
    """
    if captures:
        _serve_state["captures"] += captures
        _serve_state["scan_updated_at"] = _now_ms()
    if not _serve_state["scan_created_at"]:
        _serve_state["scan_created_at"] = _now_ms()
    if markdown == _serve_state["markdown"]:
        return
    _serve_state["markdown"] = markdown
    _serve_state["content_version"] += 1
    _serve_state["scan_updated_at"] = _now_ms()
    # A changed assignment asks a different question than whatever was last
    # solved — bring the button back rather than leaving it on "solved".
    if _serve_state["state"] != "solving":
        _serve_state["state"] = "idle"


def _new_scan():
    """Archive the live scan and start an empty one. Caller holds the lock.

    What "this is a different sheet" means: a reset, a restart, or a photo
    published with reset=1. The old transcription is kept rather than
    overwritten — it is minutes of the phone's vision model, the solutions on
    file were written FOR it, and the Assignment page has a picker that exists
    to go back and read it.
    """
    live = _serve_state["markdown"]
    if live:
        _serve_state["scans"].insert(0, {
            "version": _serve_state["scan_version"],
            "markdown": live,
            "created_at": _serve_state["scan_created_at"] or _now_ms(),
            "updated_at": _serve_state["scan_updated_at"] or _now_ms(),
            "captures": _serve_state["captures"],
            "content_version": _serve_state["content_version"],
        })
        del _serve_state["scans"][MAX_SCANS:]
        _serve_state["scan_version"] += 1

    _serve_state["markdown"] = ""
    _serve_state["captures"] = 0
    _serve_state["scan_created_at"] = _now_ms()
    _serve_state["scan_updated_at"] = _now_ms()
    # The new sheet is unread, so there is nothing to solve on it yet — and
    # anything that was is now answering the scan we just archived.
    _serve_state["content_version"] += 1
    if _serve_state["state"] != "solving":
        _serve_state["state"] = "idle"
    # Following the live scan again: the picker's pin pointed at a scan that is
    # no longer the one in front of you.
    _serve_state["active_scan"] = None


def _live_scan() -> dict:
    """The live scan in the same shape as an archived one."""
    return {
        "version": _serve_state["scan_version"],
        "markdown": _serve_state["markdown"],
        "created_at": _serve_state["scan_created_at"] or _now_ms(),
        "updated_at": _serve_state["scan_updated_at"] or _now_ms(),
        "captures": _serve_state["captures"],
        "content_version": _serve_state["content_version"],
    }


def _pick_scan(version: int | None) -> dict:
    """The scan the client asked for, or the live one. Caller holds the lock.

    Like `_pick_solution`, an unknown version falls back to the live scan
    rather than to nothing: a blank page reads as a broken reader.
    """
    if version is not None and version != _serve_state["scan_version"]:
        for s in _serve_state["scans"]:
            if s["version"] == version:
                return s
    return _live_scan()


def _solve_source() -> dict:
    """The scan the solve button sends: the pinned one, or the live one."""
    return _pick_scan(_serve_state["active_scan"])


def _query_int(path: str, key: str) -> int | None:
    """?key=N from a request path, if it is there and is a number."""
    raw = parse_qs(urlparse(path).query).get(key, [""])[0]
    try:
        return int(raw)
    except ValueError:
        return None


def _query_solution_id(path: str) -> int | None:
    """?solution_id=N from a request path, if it is there and is a number."""
    return _query_int(path, "solution_id")


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
    """An AssignmentStatus (see evens/test/src/state.ts). Caller holds the lock.

    Offline mode has no CONTINUOUS reader — no per-frame OCR as you look, no
    framing advice, no edge-coverage gate, so `running` is always false and
    `feedback` is always null. What it does have is the manual batch: photograph
    the sheet a few times, then read the lot at once (see _handle_control), and
    one-shot publishing via POST /assignment/photo.
    """
    _, which, age_ms = _pick_camera_snapshot()
    if not which:
        upstream = "disabled"
    elif age_ms <= CAMERA_STALL_MS:
        upstream = "open"
    else:
        upstream = "error"

    markdown = _serve_state["markdown"]
    batch = _serve_state["batch"]
    # Newest first, live scan at the head — the Assignment page's picker.
    versions = [
        {
            "version": s["version"],
            # ISO strings: the online reader stores timestamps and the page
            # only ever hands these to `new Date(...)`.
            "created_at": _iso(s["created_at"]),
            "updated_at": _iso(s["updated_at"]),
            "capture_count": s.get("captures", 0),
            "done": bool(s["markdown"]),
            "problems": len(parse_problems(s["markdown"])) if s["markdown"] else 0,
            "title": f"Scan v{s['version']}",
            "archived": archived,
        }
        for s, archived in (
            [(_live_scan(), False)] + [(s, True) for s in _serve_state["scans"]]
        )
    ]

    return {
        "upstream": upstream,
        "running": False,
        "done": bool(markdown),
        "captures": _serve_state["captures"],
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
        "version": _serve_state["scan_version"],
        "active_version": _serve_state["active_scan"],
        "versions": versions,
        "last_capture_at": _serve_state["scan_updated_at"] or None,
        "batch": {
            "active": batch["active"],
            "processing": batch["processing"],
            "snapshot_count": len(batch["snapshots"]),
            "max_snapshots": MAX_BATCH_SNAPSHOTS,
            # Who is reading it. Offline that is never a routine or Gemini —
            # it is the vision model on this phone, and saying so is the
            # difference between a wait you understand and one you don't.
            "reader": f"local/{VISION_MODEL}" if batch["processing"] else None,
            "read_state": batch["read_state"],
        },
    }


def _iso(ms: int) -> str:
    """Milliseconds since the epoch as an ISO string, which is what the
    Assignment page's `versions` entries carry online."""
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000).isoformat()


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
                return self._json(200, _build_tiles(_query_solution_id(self.path)))

        if path == "/markdown":
            # JSON {content, version}, matching the VPS (evens/server/index.ts's
            # GET /markdown) — NOT the text/markdown body this used to return.
            # The client's fetchSnapshot parses this as JSON, so a plain-text
            # answer threw on every call: the poll fallback and the text-mode
            # fallback were both broken offline, which is how a page with no
            # tiles ended up with nothing left to try.
            with _serve_lock:
                sol = _pick_solution(_query_solution_id(self.path))
                return self._json(200, {
                    "content": sol["markdown"] if sol else "",
                    "version": sol["created_at"] if sol else 0,
                })

        if path.startswith("/events"):
            return self._handle_events(_query_solution_id(self.path))

        if path == "/assignment/status":
            with _serve_lock:
                return self._json(200, _build_camera_status())

        # The Assignment page reading the local store. Both were missing, so
        # that page had nothing to show offline at all: it is an ordinary
        # document page (docPage.ts) and these are the two endpoints it reads.
        if path == "/assignment/markdown":
            with _serve_lock:
                scan = _pick_scan(_query_int(self.path, "version"))
                return self._json(200, {
                    "content": scan["markdown"],
                    "version": scan["content_version"],
                })

        if path == "/assignment/tiles":
            with _serve_lock:
                return self._json(200, _build_scan_tiles(
                    _query_int(self.path, "version"),
                    parse_qs(urlparse(self.path).query).get("overlay", [""])[0],
                ))

        if path == "/assignment/camera":
            return self._handle_camera_preview()

        if path == "/assignment/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()
            sent_version = None
            try:
                while True:
                    with _serve_lock:
                        data = json.dumps(_build_camera_status())
                        scan = _pick_scan(_query_int(self.path, "version"))
                        version = scan["content_version"]
                    self.wfile.write(f"event: status\ndata: {data}\n\n".encode())
                    # Same reason as the solution stream: without this the page
                    # never learns a batch has landed, because the poll that
                    # would notice only runs while this stream is down.
                    if version != sent_version:
                        sent_version = version
                        payload = json.dumps({"version": version})
                        self.wfile.write(f"event: markdown\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                return

        self._json(404, {"ok": False, "detail": "not found"})

    def _handle_events(self, solution_id: int | None):
        """The AI page's live stream: status, and when the document changed.

        The `markdown` event is the half that was missing. The client only
        refetches tiles when it is told the document changed (docPage.ts's SSE
        listener); the poll that would otherwise catch up runs ONLY while the
        stream is down, and this stream is up. So a solve finishing offline
        moved the status to `solved` — taking the button away — and left the
        old tiles, or none at all, on the glasses until you walked off the page
        and back on.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()

        sent_version = None
        try:
            while True:
                with _serve_lock:
                    status = json.dumps(_build_status())
                    sol = _pick_solution(solution_id)
                    version = sol["created_at"] if sol else 0
                self.wfile.write(f"event: status\ndata: {status}\n\n".encode())
                if version != sent_version:
                    sent_version = version
                    payload = json.dumps({"version": version})
                    self.wfile.write(f"event: markdown\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError):
            return

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
                _set_assignment(body.get("markdown", ""))
                _save_state()
            return self._json(200, {"ok": True})

        if path == "/assignment/control":
            return self._handle_control(body.get("action", ""))

        if path == "/assignment/active":
            # Which scan the solve button sends. The Assignment page's "Point
            # the AI here"; the answer is the selection actually settled on,
            # which the status stream then carries to the AI page.
            with _serve_lock:
                wanted = body.get("version")
                known = {s["version"] for s in _serve_state["scans"]}
                known.add(_serve_state["scan_version"])
                if wanted is not None and wanted not in known:
                    return self._json(404, {"ok": False, "detail": f"no scan v{wanted}"})
                # Pinning the live scan IS following it: keep it null so a new
                # batch doesn't leave the button aimed at yesterday's sheet.
                if wanted == _serve_state["scan_version"]:
                    wanted = None
                _serve_state["active_scan"] = wanted
                _save_state()
            return self._json(200, {"ok": True, "version": wanted})

        self._json(404, {"ok": False, "detail": "not found"})

    def _handle_control(self, action: str):
        """POST /assignment/control — the Camera page's menu.

        Offline there is no continuous reader to start, stop or extend: nothing
        is watching the camera and grading frames as you aim. What there is is
        the MANUAL BATCH, which is the same gesture from the wearer's side —
        photograph the sheet a few times, then send the lot — and the only one
        of these actions that has anything behind it here.

        The rest are refused by name rather than with a generic "not found".
        The menu offers them because it is drawn from the same status shape as
        online, and "couldn't start reading: no continuous reader offline" is a
        sentence you can act on; "refused" is not.
        """
        batch = _serve_state["batch"]

        if action == "batch_start":
            with _serve_lock:
                if batch["processing"]:
                    return self._json(409, {
                        "ok": False, "action": "failed",
                        "detail": "still reading the last batch",
                    })
                batch["active"] = True
                batch["snapshots"] = []
                batch["read_state"] = None
            return self._json(200, {"ok": True, "action": "batch_started"})

        if action == "batch_snapshot":
            if not batch["active"]:
                return self._json(409, {
                    "ok": False, "action": "failed", "detail": "no batch started",
                })
            # Outside the lock: reading the JPEG off disk is I/O, and the
            # status stream should not stall behind it.
            jpeg, which, age_ms = _pick_camera_snapshot()
            if jpeg is None:
                return self._json(503, {
                    "ok": False, "action": "failed", "detail": "no camera configured",
                })
            if age_ms > CAMERA_STALL_MS:
                return self._json(503, {
                    "ok": False, "action": "failed",
                    "detail": f"{which} camera stalled ({round(age_ms / 1000)}s)",
                })
            with _serve_lock:
                if len(batch["snapshots"]) >= MAX_BATCH_SNAPSHOTS:
                    return self._json(409, {
                        "ok": False, "action": "failed",
                        "detail": f"batch full at {MAX_BATCH_SNAPSHOTS}",
                    })
                batch["snapshots"].append(jpeg)
                count = len(batch["snapshots"])
            return self._json(200, {
                "ok": True, "action": "batch_snapshot", "detail": f"{count} held",
            })

        if action == "batch_finish":
            if not VISION_MODEL:
                return self._json(500, {
                    "ok": False, "action": "failed",
                    "detail": "no vision model configured",
                })
            with _serve_lock:
                if not batch["snapshots"]:
                    return self._json(409, {
                        "ok": False, "action": "failed", "detail": "no snapshots",
                    })
                if batch["processing"]:
                    return self._json(409, {
                        "ok": False, "action": "failed", "detail": "already reading",
                    })
                batch["active"] = False
                batch["processing"] = True
                batch["read_state"] = "claimed"
                count = len(batch["snapshots"])
            threading.Thread(target=_batch_read_thread, daemon=True).start()
            return self._json(200, {
                "ok": True, "action": "batch_finished", "detail": f"reading {count}",
            })

        if action == "batch_cancel":
            with _serve_lock:
                batch["active"] = False
                batch["snapshots"] = []
                batch["read_state"] = None
            return self._json(200, {"ok": True, "action": "batch_cancelled"})

        if action in ("reset", "restart"):
            with _serve_lock:
                _new_scan()
                _save_state()
            return self._json(200, {"ok": True, "action": action})

        if action == "complete":
            # `done` offline is just "is there anything transcribed", so this
            # is already true if it can be. Answering ok keeps the menu entry
            # from reading as broken.
            return self._json(200, {"ok": True, "action": "complete"})

        if action in ("start", "stop", "extend"):
            return self._json(200, {
                "ok": False, "action": "failed",
                "detail": "no continuous reader offline - use Batch snapshots",
            })

        return self._json(400, {
            "ok": False, "action": "failed", "detail": f'unknown action "{action}"',
        })

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

        # A different sheet starts a new scan BEFORE the read, so the read is
        # not handed the previous sheet as context to continue.
        if reset:
            with _serve_lock:
                _new_scan()
                _save_state()

        with _serve_lock:
            transcript = _serve_state["markdown"]

        image_b64 = base64.b64encode(raw).decode()
        # Continues the transcription rather than being appended to it, for the
        # reason in VISION_CONTINUE_PROMPT: this camera cannot fit a line of
        # the sheet in one frame, so a second photograph is usually the other
        # half of something already half-read, not a separate piece of text.
        text = ocr_image(_serve_llama_url, image_b64, context=transcript)
        if not text:
            return self._json(500, {"ok": False, "detail": "OCR failed"})

        with _serve_lock:
            if transcript and len(text) < len(transcript) * MIN_REREAD_RATIO:
                # See _batch_read_thread: a re-read that came back far shorter
                # lost the thread, and taking it would discard what earlier
                # photographs got right.
                problems = len(parse_problems(_serve_state["markdown"]))
                return self._json(200, {
                    "ok": True, "problems": problems, "done": True,
                    "detail": "kept the longer transcription",
                })
            _set_assignment(text, captures=1)
            problems = len(parse_problems(_serve_state["markdown"]))
            _save_state()

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
                _set_assignment(body["markdown"])
            if not _solve_source()["markdown"]:
                return self._json(400, {"ok": False, "detail": "no assignment"})
            _serve_state["state"] = "solving"
            _serve_state["error"] = None
            _save_state()

        self._json(200, {"ok": True, "action": "queued"})
        threading.Thread(target=_solve_thread, daemon=True).start()

    def _handle_cancel(self):
        with _serve_lock:
            # Back to whatever there is to read, not to a blank page: an
            # earlier answer is still on file and the reader should have it.
            _serve_state["state"] = "solved" if _serve_state["solutions"] else "idle"
            _serve_state["run"] = None
            _save_state()
        self._json(200, {"ok": True, "action": "cancelled"})

    def log_message(self, fmt, *args):
        print(f"[serve] {args[0]}", file=sys.stderr)


def _build_status() -> dict:
    s = _serve_state
    newest = s["solutions"][0] if s["solutions"] else None
    solution = None
    # What the solve button would send: the pinned scan, or the live one.
    source = _solve_source()
    if newest:
        solution = {
            "created_at": newest["created_at"],
            "age_ms": max(0, _now_ms() - newest["created_at"]),
            "model": MODEL_NAME,
            "assignment_version": newest.get("content_version", 1),
            # It answers an earlier sheet than the one the button is aimed at.
            # Now that solutions outlive the process this is a real state
            # offline and not the constant False it used to be — it is what
            # puts "(shown: earlier scan)" under the solve button.
            "stale": newest.get("content_version", 1) != source["content_version"],
            "chars": len(newest.get("markdown", "")),
        }
    # The client's button only invites a tap for `idle`/`failed`; without this
    # a fresh server with nothing transcribed yet still reports "idle" and the
    # AI page shows "SOLVE WITH CLAUDE / TAP TO RUN" over zero problems, which
    # then 400s "no assignment" the moment you tap it. See server/solver.ts's
    # getSolverStatus for the online equivalent of this gate.
    state = s["state"]
    if state == "idle" and not source["markdown"]:
        state = "no_assignment"

    return {
        "state": state,
        "assignment": {
            "available": bool(source["markdown"]),
            "version": source["content_version"] if source["markdown"] else None,
            "problems": len(parse_problems(source["markdown"])) if source["markdown"] else 0,
            "done": bool(source["markdown"]),
            # Which scan the button would send, so the AI page's footer can say
            # "Tap to solve scan v2" rather than implying it is the live one.
            "active_version": s["active_scan"],
        },
        "solution": solution,
        "run": s["run"],
        "trigger": {"configured": True, "detail": "local solver"},
        "mode": "offline",
        "solutions": len(s["solutions"]),
        # The version picker's list (see ai.ts buildVersionMenu). This was a
        # hardcoded empty array, which meant the whole of the AI page's history
        # UI — "Open a version...", "Back to latest", "Get quick solution" —
        # was dead offline no matter how many solves you had run.
        "solution_history": [
            {
                "id": item["id"],
                "version": item["version"],
                "created_at": item["created_at"],
                "model": MODEL_NAME,
                "assignment_version": item.get("content_version", 1),
                "chars": len(item.get("markdown", "")),
            }
            for item in s["solutions"]
        ],
    }


def _build_tiles(solution_id: int | None = None) -> dict:
    """Render a solution to tile PNGs (see render.py).

    Cached on the solution dict and keyed by its created_at timestamp, so
    repeated polling (the client hits /tiles on every status change) doesn't
    re-render. If render.py's deps aren't installed, falls back to empty
    pages — the client then falls back to text mode (see docPage.ts's
    showTextFallback).

    `solution_id` is the version picker's pin: the page asks for one solution
    by name and must get that one, not whatever is newest.
    """
    sol = _pick_solution(solution_id)
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


def _build_scan_tiles(version: int | None, overlay: str) -> dict:
    """The same, for a transcription rather than a solution.

    `overlay=menu` is the variant render with the action menu's rectangle
    reserved, so the Assignment page can open a menu without taking the
    transcription off the screen. render.py cannot reserve a rect, so the
    honest answer is to render the plain document and NOT claim an overlay:
    the client checks that field, and a server that quietly ignores the query
    leaves the menu unreadable over text it failed to cover (see docPage.ts's
    loadVariant). Saying nothing puts it on the plain backdrop instead, which
    is uglier and legible.
    """
    scan = _pick_scan(version)
    if not scan["markdown"] or render is None or not render.RENDER_AVAILABLE:
        return {"version": 0, "pages": [], "overlay": None}

    # The live scan's dict is rebuilt on every call, so its render cache has to
    # live somewhere that persists — hence keying off the state itself.
    cache = _serve_state if scan["version"] == _serve_state["scan_version"] else scan
    key = scan["content_version"]
    if cache.get("scan_tiles_version") != key:
        try:
            cache["scan_tiles"] = render.render_markdown_to_tiles(scan["markdown"])
        except Exception as e:
            print(f"[serve] assignment tile render failed: {e}", file=sys.stderr)
            cache["scan_tiles"] = []
        cache["scan_tiles_version"] = key

    return {"version": key, "pages": cache.get("scan_tiles", []), "overlay": None}


def _batch_read_thread():
    """Read every photograph in the batch into ONE transcription.

    The offline half of "Send batch to AI". Online the images go to a Claude
    routine and fall back to a chain of hosted models (see
    lookcam/assignment/server.ts); here it is the phone's own vision model, and
    the images are read STRICTLY IN ORDER, each one against everything read so
    far — see VISION_CONTINUE_PROMPT. A frame from this camera holds about a
    sixth of the page and routinely cuts a line in half, so the only thing that
    can join those halves is a reader that can see both at once.

    Hence a thread, and hence the Camera page being told `processing` for the
    whole of it: the reads are sequential by necessity, not by choice, and a
    batch of six is minutes.
    """
    with _serve_lock:
        images = list(_serve_state["batch"]["snapshots"])
        # Photographs of a sheet already partly transcribed continue THAT
        # transcription — publishing three more frames of the sheet you are
        # halfway through is the normal way to finish it.
        transcript = _serve_state["markdown"]

    read = 0
    failures = 0
    for index, jpeg in enumerate(images, 1):
        try:
            text = ocr_image(
                _serve_llama_url,
                base64.b64encode(jpeg).decode(),
                context=transcript,
            )
        except Exception as e:
            print(f"[serve] batch image {index} failed: {e}", file=sys.stderr)
            text = ""

        if not text:
            failures += 1
            print(f"[serve] batch {index}/{len(images)}: nothing read", file=sys.stderr)
            continue

        # The model was asked to return everything it already knew plus what
        # this frame adds. A much SHORTER answer means it lost the thread —
        # a 3B model re-emitting a growing document does sometimes stop early
        # or start over — and accepting it would throw away frames that were
        # read correctly. Keep what we had; this frame simply contributed
        # nothing. Losing one frame beats losing the sheet.
        if transcript and len(text) < len(transcript) * MIN_REREAD_RATIO:
            failures += 1
            print(f"[serve] batch {index}/{len(images)}: re-read came back "
                  f"{len(text)} chars against {len(transcript)} - keeping the "
                  f"longer transcription", file=sys.stderr)
            continue

        transcript = text
        read += 1
        print(f"[serve] batch {index}/{len(images)}: {len(text)} chars", file=sys.stderr)

    with _serve_lock:
        batch = _serve_state["batch"]
        if read:
            _set_assignment(transcript, captures=read)
        batch["processing"] = False
        batch["snapshots"] = []
        # Kept rather than cleared: the Camera page shows the read state, and
        # "every image failed" is the one outcome worth still being on screen
        # after the spinner stops.
        batch["read_state"] = "submitted" if read else "failed"
        problems = len(parse_problems(_serve_state["markdown"]))
        _save_state()

    print(f"[serve] batch read: {read} of {len(images)} images used, "
          f"{failures} dropped, assignment now {problems} problems", file=sys.stderr)


def _solve_thread():
    """Run the solver in a background thread, updating shared state."""
    global _serve_state
    with _serve_lock:
        # Whatever the button was aimed at when it was pressed, held for the
        # length of the solve: a photo landing mid-run must not change which
        # sheet the answer is about.
        source = _solve_source()
        markdown = source["markdown"]
        answering = source["content_version"]
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
        solution_id = _serve_state["next_solution_id"]
        _serve_state["next_solution_id"] = solution_id + 1
        _serve_state["state"] = "solved"
        # Onto the front of the list rather than over the top of the last one:
        # a re-solve of the same sheet is a second opinion, and the page has a
        # picker for exactly that.
        _serve_state["solutions"].insert(0, {
            "id": solution_id,
            "version": solution_id,
            "markdown": solution_md,
            "created_at": _now_ms(),
            "content_version": answering,
        })
        del _serve_state["solutions"][MAX_SOLUTIONS:]
        _serve_state["run"] = None
        _save_state()

    print(f"[serve] solved: {len(solution_md)} chars (v{solution_id})", file=sys.stderr)


def serve(host: str, port: int, llama_url: str):
    """Run the local HTTP server."""
    global _serve_llama_url
    _serve_llama_url = llama_url

    _load_state()
    print(f"[serve] state file: {STATE_FILE or '(none - nothing will survive a restart)'}",
          file=sys.stderr)

    # THREADING IS NOT OPTIONAL HERE, and this was a plain HTTPServer.
    #
    # `/events` and `/assignment/events` are SSE streams: their handlers loop
    # forever by design. On a single-threaded server the first one to connect
    # takes the only thread and never gives it back, so from the moment the
    # glasses subscribe — which docPage.ts does immediately after asking for
    # the document — this server answers NOTHING else. No /tiles, no /markdown,
    # no /solution/status, not even after the stream is closed, because the
    # handler is asleep between beats and the backlog never drains.
    #
    # That is the whole of "the AI page just sits on Loading..." in offline
    # mode: the page was not slow and the model was not thinking, the server
    # had stopped answering the request it was waiting on. Two pages, each with
    # a stream and a poll, never stood a chance.
    #
    # daemon_threads so a shutdown isn't held up by streams that never end.
    server = http.server.ThreadingHTTPServer((host, port), _LocalHandler)
    server.daemon_threads = True
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
    global STATE_FILE

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
    parser.add_argument("--state-file",
                        default=os.path.expanduser("~/.evens/solver-state.json"),
                        help="where --serve keeps the assignment and its solutions "
                             "so they survive a restart; empty to keep nothing")
    args = parser.parse_args()

    SYMPY_TIMEOUT = args.sympy_timeout
    PROBLEM_TIMEOUT = args.problem_timeout
    TOTAL_TIMEOUT = args.total_timeout
    OLLAMA_MODEL = args.ollama_model
    VISION_MODEL = args.vision_model
    CAMERA_SNAPSHOT_PRIMARY = args.camera_primary
    CAMERA_SNAPSHOT_FALLBACK = args.camera_fallback
    CAMERA_FRESH_MS = args.camera_fresh_ms
    STATE_FILE = args.state_file

    if args.serve:
        serve(args.serve_host, args.serve_port, args.llama)
    elif args.once:
        found = solve_assignment(args.server, args.llama, args.token)
        sys.exit(0 if found else 1)
    else:
        poll_loop(args.server, args.llama, args.token, args.poll, args.grace)


if __name__ == "__main__":
    main()
