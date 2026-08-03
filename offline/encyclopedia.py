"""The encyclopedia, on the phone, with nothing to render it.

Everything else this server does is work: OCR through llama-server, algebra
through SymPy, tiles through Pillow and matplotlib. This does none of it. The
course was converted, laid out and rendered to PNG tiles once by
evens/tools/enc on a developer machine with a browser, and committed as
content/enc — so the exam-room job is reading a file off the flash and putting
it on a socket.

That is deliberate and it is the whole reason this feature is trustworthy
offline. render.py's scope is narrow by design (headings, paragraphs, bold,
$math$ — see its module docstring), and the course is lists, figures, matrices
and stacked integrals. Rendering it here would mean either a second renderer
that disagrees with the VPS's, or an encyclopedia that looks different
depending on which server answered. Serving bytes cannot disagree with itself.

The files are stored gzipped and go out with `content-encoding: gzip`, so this
never decompresses anything either — `fetch` unwraps them in the client, which
is the one place that was always going to hold the JSON anyway.

Mirrors evens/server/enc.ts exactly: same two routes, same payloads.
"""

import gzip
import json
import os
import re
import threading

# ── where the pack is ───────────────────────────────────────────────────────
#
# Two candidates, tried in order, because this file runs from two places. On a
# development machine `offline/` sits next to `evens/` in the same checkout; on
# the phone, setup-termux.sh copies the pack in beside this script so the
# glasses stack does not need the whole evens repo on the device.

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.environ.get("ENC_DIR", ""),
    os.path.join(_HERE, "content", "enc"),
    os.path.join(_HERE, "..", "evens", "content", "enc"),
]


def _root() -> str:
    for path in _CANDIDATES:
        if path and os.path.isfile(os.path.join(path, "toc.json.gz")):
            return os.path.abspath(path)
    return ""


ROOT = _root()

# A node id becomes a filename, so it is validated rather than sanitised: the
# set of legal ids is small and known, and rejecting what is not one of them
# cannot be got subtly wrong the way stripping "../" can.
_ID = re.compile(r"^[A-Za-z0-9.]{1,40}$")

_lock = threading.Lock()
_cache: dict[str, bytes] = {}


def available() -> bool:
    """Whether there is a pack to serve at all."""
    return bool(ROOT)


def _read(path: str) -> bytes | None:
    """Read once, then from memory.

    The pack is a few megabytes and never changes while this process is up.
    Without the cache, paging a document would hit the filesystem on every
    swipe — which is tolerable on a VPS and is not on a phone whose flash is
    also holding a model file being memory-mapped.
    """
    with _lock:
        hit = _cache.get(path)
        if hit is not None:
            return hit
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        data = handle.read()
    with _lock:
        _cache[path] = data
    return data


def toc() -> bytes | None:
    """The tree: sections, topics, node titles, page counts, search index."""
    if not ROOT:
        return None
    return _read(os.path.join(ROOT, "toc.json.gz"))


def node(node_id: str) -> bytes | None:
    """One node's pages, already rendered."""
    if not ROOT or not _ID.match(node_id or "") or ".." in node_id:
        return None
    return _read(os.path.join(ROOT, "pages", node_id + ".json.gz"))


def describe() -> str:
    """One line for the startup banner."""
    if not ROOT:
        return "Encyclopedia: NOT PACKED - /enc/* returns 503"
    try:
        raw = toc()
        count = len(json.loads(gzip.decompress(raw)).get("nodes", {})) if raw else 0
    except (ValueError, OSError, EOFError):
        count = 0
    return f"Encyclopedia: {count} nodes from {ROOT}"
