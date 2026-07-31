#!/usr/bin/env python3
"""
gallery-bridge — the phone's camera roll, over HTTP on localhost.

WHY THIS EXISTS. The companion app and the glasses pages are one web app running
in a WebView on the phone, and the web has no way to read a gallery: a page can
open a file picker, which means picking up the phone and tapping twice. That is
fine for "upload this particular photo" and useless for the thing you actually
want, which is to shoot a sheet of paper and publish it from the glasses without
touching the phone again.

A tiny HTTP server in Termux closes that gap. It runs on the same device as the
WebView, so the page can simply fetch it — browsers treat http://127.0.0.1 as a
secure origin, so this works from an https page without a mixed-content block.

    GET /health          is it up, and which directories is it watching
    GET /recent.json     the newest photos as metadata (no pixels)
    GET /latest.json     just the newest one's metadata
    GET /latest          the newest photo itself, as bytes
    GET /photo?id=…      one specific photo from /recent.json, as bytes

WHAT IT IS NOT. It never deletes, moves or writes anything, and it serves only
files under the configured roots with an image extension — `id` is resolved
against the listing rather than being a path, so it cannot be walked out of.

    python3 gallery.py                 # loopback only, token required
    python3 gallery.py --allow-any     # no token (see the warning at AUTH)

Zero dependencies: Termux has python, and that is the whole install.

STAYING UP is not this script's job — see run.sh, which holds the wake
lock, restarts this on any exit, and is what Termux:Boot runs. What IS this
script's job is to fail in ways a supervisor can act on: a clean exit code for
"someone is already on that port" (retrying that forever is a hot loop, not a
recovery), a non-zero one for anything unexpected, and SIGTERM handled so a
restart is a restart rather than a kill.
"""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import secrets
import signal
import stat as stat_module
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Exit codes the supervisor reads. 78 is EX_CONFIG, and run.sh backs
# right off on it rather than restarting every few seconds — the same contract
# ../termux-run.sh already uses for a config that needs a human.
EX_CONFIG = 78

# Where Android actually puts photos. /sdcard is readable only after you have
# run `termux-setup-storage` once and granted the permission.
DEFAULT_ROOTS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Download",
]

# What Gemini accepts inline, which is what the reader accepts, which is what
# there is any point serving. Kept in step with GEMINI_IMAGE_TYPES in
# ../../assignment/server.ts.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

# A gallery can hold tens of thousands of files; the newest few are the only
# ones anyone is ever going to publish, and stat()ing the lot on every request
# is what would make this feel slow.
MAX_SCAN = 4000
DEFAULT_RECENT = 12

# Every app on the phone keeps its caches and thumbnails in a dot-directory
# under Pictures or DCIM — /sdcard/Pictures/.gs_fs0 (the Google app),
# .thumbnails, and a dozen others. They matter here for two reasons, and both
# of them are bugs if you ignore them:
#
#   they are rewritten constantly, so a 43-byte cache placeholder has a newer
#   mtime than the photo you just took and wins "newest first"
#
#   they hold most of the files on the device, so walking them is most of the
#   time a scan takes — and a scan happens on every single request
#
# Nothing anyone wants to publish lives in a hidden directory, so this skips
# them entirely rather than trying to tell good ones from bad.
SKIP_HIDDEN = True

# A second, cheaper guard for junk that isn't hidden. Real photos are hundreds
# of kilobytes; this only has to be above "obviously a placeholder", and low
# enough that a small screenshot or a webp still counts.
MIN_IMAGE_BYTES = 4096

# ── auth ────────────────────────────────────────────────────────────────────
#
# Bound to loopback, so nothing off the phone can reach this. That still leaves
# every app and every web page ON the phone, and a page you happen to visit
# while this is running could otherwise read your camera roll by fetching
# localhost. So there is a token, generated once and kept in the file below,
# and the companion app is given it as part of the bridge URL.
#
# --allow-any turns the check off. It is a real convenience while you are
# setting this up and a genuinely bad idea to leave on, which is why it says so
# on startup every time.

TOKEN_FILE = Path(os.environ.get("GALLERY_TOKEN_FILE", Path.home() / ".evens-gallery-token"))


def load_or_create_token() -> str:
    override = os.environ.get("GALLERY_TOKEN", "").strip()
    if override:
        return override
    try:
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token + "\n")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        # Android's sdcard-backed home may not support chmod; loopback binding
        # is doing the real work here anyway.
        pass
    return token


# ── the gallery ─────────────────────────────────────────────────────────────


def scan(roots: list[Path]) -> list[dict]:
    """Every image under `roots`, newest first, as plain metadata."""
    found: list[tuple[float, Path, int]] = []
    seen: set[Path] = set()

    for root in roots:
        if not root.is_dir():
            continue
        try:
            base = root.expanduser().resolve()
        except OSError:
            continue
        # os.walk rather than rglob because only os.walk lets us decline to
        # descend: rglob would still walk every file in .gs_fs0 and .thumbnails
        # before we got the chance to reject them one at a time.
        for dirpath, dirnames, filenames in os.walk(base, onerror=None):
            if SKIP_HIDDEN:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if len(found) >= MAX_SCAN:
                break
            for name in filenames:
                if len(found) >= MAX_SCAN:
                    break
                if SKIP_HIDDEN and name.startswith("."):
                    continue
                path = Path(dirpath, name)
                if path.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                # One stat for the three things we need — is it a file, how old,
                # how big — instead of is_file() here and another in describe().
                # On FUSE-backed /sdcard the syscall is the expensive part.
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode) or st.st_size < MIN_IMAGE_BYTES:
                    continue
                # DEFAULT_ROOTS overlap on purpose (DCIM/Camera is inside DCIM),
                # so that a phone which uses either layout works with no
                # configuration. The cost is that the same file is reachable
                # twice.
                resolved = path.resolve()
                if resolved in seen:
                    continue
                # A symlink out of the gallery resolves to somewhere admissible()
                # will refuse, and listing what /photo won't serve is worse than
                # not listing it: the app shows a photo that 404s on tap.
                if not resolved.is_relative_to(base):
                    continue
                seen.add(resolved)
                found.append((st.st_mtime, path, st.st_size))

    found.sort(key=lambda item: item[0], reverse=True)
    return [describe(path, mtime, size) for mtime, path, size in found]


def admissible(candidate: Path, roots: list[Path]) -> Path | None:
    """The resolved `candidate` if scan() would have listed it, else None.

    Same predicate as scan(), asked about one path instead of built by walking
    all of them. Resolving before the containment check is the part that
    matters: it collapses `..` and follows symlinks, so a path that ends up
    outside every root cannot talk its way back in."""
    try:
        resolved = candidate.expanduser().resolve()
    except OSError:
        return None

    for root in roots:
        try:
            base = root.expanduser().resolve()
            relative = resolved.relative_to(base)
        except (OSError, ValueError):
            continue  # not under this root
        if SKIP_HIDDEN and any(part.startswith(".") for part in relative.parts):
            continue
        if resolved.suffix.lower() not in IMAGE_SUFFIXES:
            return None
        try:
            st = resolved.stat()
        except OSError:
            return None
        if not stat_module.S_ISREG(st.st_mode) or st.st_size < MIN_IMAGE_BYTES:
            return None
        return resolved
    return None


def mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if path.suffix.lower() in {".heic", ".heif"} and not mime:
        # Not in Python's table on every platform, and the reader needs it to
        # tell the model what it is looking at.
        mime = f"image/{path.suffix.lower().lstrip('.')}"
    return mime or "application/octet-stream"


def describe(path: Path, mtime: float, size: int) -> dict:
    return {
        # The id is the path. A client can send back any path it likes, so
        # /photo re-checks it with admissible() rather than trusting this.
        "id": str(path),
        "name": path.name,
        "mime": mime_for(path),
        "bytes": size,
        "taken_at": int(mtime * 1000),
    }


# ── the server ──────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "gallery-bridge/1.0"
    roots: list[Path] = []
    token: str = ""
    allow_any: bool = False

    # One line per request, on stderr, without the default's noisy address.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[gallery] %s\n" % (fmt % args))
        sys.stderr.flush()

    def handle_one_request(self) -> None:
        """One bad request must not be able to end the process.

        BaseHTTPRequestHandler already isolates most of this, but a handler that
        raises after the client has gone (a photo fetch abandoned mid-download
        is the common one) surfaces as a broken pipe here. That is a normal
        event on a phone, not a reason for the supervisor to see an exit."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:  # noqa: BLE001 - the point is to survive it
            self.log_message("handler failed: %s", err)
            self.close_connection = True

    # ── plumbing ────────────────────────────────────────────────────────────

    def _cors(self) -> None:
        # The WebView's origin is not something this can know ahead of time —
        # a dev build is http://localhost:5173, a packed one is whatever the
        # Even Hub app serves from, and some hosts send `null`. The token is
        # what actually guards this; the origin is not a credential.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, path: Path, mime: str) -> None:
        try:
            data = path.read_bytes()
        except OSError as err:
            return self._json({"error": f"cannot read {path.name}: {err}"}, 500)
        self.send_response(200)
        self.send_header("content-type", mime)
        self.send_header("content-length", str(len(data)))
        # The newest photo changes the moment you take one; a cached answer here
        # is the difference between publishing the sheet you just shot and the
        # one before it.
        self.send_header("cache-control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _authed(self, query: dict) -> bool:
        if self.allow_any:
            return True
        header = self.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        # Also accepted in the query string: an <img src> cannot set a header,
        # and showing the photo you are about to publish is worth that.
        supplied = supplied or (query.get("t", [""])[0])
        return secrets.compare_digest(supplied, self.token)

    # ── routes ──────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/health":
            # Deliberately outside the auth check: "is the bridge running" is
            # the one question the companion app must be able to answer before
            # it has been given a token.
            return self._json(
                {
                    "ok": True,
                    "roots": [str(r) for r in self.roots if r.is_dir()],
                    "needs_token": not self.allow_any,
                }
            )

        if not self._authed(query):
            return self._json({"error": "unauthorized"}, 401)

        if url.path == "/recent.json":
            try:
                limit = max(1, min(60, int(query.get("n", [DEFAULT_RECENT])[0])))
            except ValueError:
                limit = DEFAULT_RECENT
            return self._json({"photos": scan(self.roots)[:limit]})

        if url.path == "/latest.json":
            photos = scan(self.roots)
            if not photos:
                return self._json({"error": "no photos found", "roots": [str(r) for r in self.roots]}, 404)
            return self._json(photos[0])

        if url.path in ("/latest", "/latest.jpg"):
            photos = scan(self.roots)
            if not photos:
                return self._json({"error": "no photos found"}, 404)
            return self._bytes(Path(photos[0]["id"]), photos[0]["mime"])

        if url.path == "/photo":
            wanted = query.get("id", [""])[0]
            # `id` came from a listing we produced, but it arrives over the wire
            # and is therefore a client-supplied path — it has to be re-checked
            # here, not trusted. admissible() applies exactly the rules scan()
            # applies, containment included, without walking the gallery a
            # second time to do it: this runs right after /latest.json, and on
            # FUSE-backed /sdcard that second walk was most of the wait.
            resolved = admissible(Path(wanted), self.roots)
            if not resolved:
                return self._json({"error": "no such photo"}, 404)
            return self._bytes(resolved, mime_for(resolved))

        self._json(
            {
                "error": "not found",
                "endpoints": ["/health", "/recent.json", "/latest.json", "/latest", "/photo?id="],
            },
            404,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the phone's newest photos on localhost.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("GALLERY_PORT", 8790)))
    parser.add_argument(
        "--host",
        default=os.environ.get("GALLERY_HOST", "127.0.0.1"),
        help="loopback by default; anything else exposes your camera roll to the network",
    )
    parser.add_argument(
        "--roots",
        nargs="*",
        default=os.environ.get("GALLERY_DIRS", "").split(":") if os.environ.get("GALLERY_DIRS") else DEFAULT_ROOTS,
    )
    parser.add_argument("--allow-any", action="store_true", help="serve without a token (unsafe)")
    args = parser.parse_args()

    roots = [Path(r).expanduser() for r in args.roots if r]
    live = [r for r in roots if r.is_dir()]

    Handler.roots = roots
    Handler.allow_any = args.allow_any
    Handler.token = "" if args.allow_any else load_or_create_token()

    print(f"gallery-bridge on http://{args.host}:{args.port}", flush=True)
    for root in roots:
        print(f"  {'✓' if root.is_dir() else '✗'} {root}", flush=True)
    if not live:
        # Not fatal, and deliberately not EX_CONFIG: storage permission can be
        # granted while this is running, and the directories appear underneath
        # it. /health reports how many exist, so the companion app can say so.
        print("  [!] none of those directories exist — run `termux-setup-storage` and grant it", flush=True)
    if args.allow_any:
        print("  [!] --allow-any: any app or web page on this phone can read your camera roll", flush=True)
    else:
        print("\n  paste this into the companion app's Photo tab:\n", flush=True)
        print(f"    http://{args.host}:{args.port}?t={Handler.token}\n", flush=True)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as err:
        if err.errno == errno.EADDRINUSE:
            # Almost always a second copy of this script, which the supervisor
            # must not fight with: restarting into the same collision every few
            # seconds is a hot loop that fills the log and fixes nothing.
            print(f"[!] port {args.port} is already in use — is a bridge already running?", file=sys.stderr)
            return EX_CONFIG
        raise

    # A restart has to be a restart. Without this, SIGTERM kills the process
    # mid-response and the next start finds the socket in TIME_WAIT.
    def stop(signum, _frame):
        print(f"gallery-bridge stopping on signal {signum}", flush=True)
        # From a signal handler, so it must not block: shutdown() waits for the
        # serve_forever loop, which is the thread we are interrupting.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
