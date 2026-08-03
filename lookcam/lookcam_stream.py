#!/usr/bin/env python3
"""
Working LookCam (GHBB / micro-cam.ru) client — pulls the live H.265 stream.

Reverse-engineered from the LookCam APK + a real packet capture:
  * Transport: PPPP over UDP, via aiopppp, with our 0x43->P2pRdy handshake patch.
  * Commands  (channel 1):  f1 d0 <len> | d1 01 <idx> |
                 a0 af af af <18B hdr> <jsonlen LE> {json} f4 f3 f2 f1
       login  -> {"cmd":"LoginDev","pwd":"<pw>"}
       stream -> {"cmd":"OpenVideo","state":1,"pwd":"<pw>","stream":<n>,"userid":<n>}
  * Video     (channel 0):  d1 00 <idx> + 1024B chunks; frames prefixed by
                 `01 af af af` + header; payload is Annex-B HEVC (H.265).

Emits clean HEVC (gated to start on a keyframe) to --out and/or stdout (--pipe).
Reconnects automatically; a stall watchdog drops a dead session.

    python lookcam_stream.py -a 10.158.133.99 -p 12345678 --seconds 20
    ffmpeg -f hevc -i live.h265 -c copy out.mp4
    # live, forever, piped:
    python lookcam_stream.py -a 10.158.133.99 -p 12345678 --pipe \
      | ffmpeg -f hevc -i - -c copy -f segment -segment_time 900 rec_%03d.mp4

stream index: 0=4K, 1=2K/1080p, 2=1080p (per GetDevStream); default 1.
"""
import argparse, asyncio, collections, json, logging, os, struct, sys, time

from aiopppp.const import CAM_MAGIC, PacketType
from aiopppp.packets import DrwPkt, Packet, parse_packet as _orig_parse
from aiopppp import session as _session
from aiopppp.session import JsonSession, Session
from aiopppp.discover import Discovery

log = logging.getLogger("lookcam")

# --- handshake patch: GHBB firmware's ready byte is 0x43, not 0x42 -----------
def _patched_parse(data):
    if len(data) >= 2 and data[0] == CAM_MAGIC and data[1] == 0x43:
        return Packet(PacketType.P2pRdy, data[4:])
    # Never let aiopppp command-parse channel 0. It sniffs every DRW payload for
    # a "\x11\x0a" command preamble regardless of channel — but on this firmware
    # channel 0 is VIDEO, so compressed bytes hit that pattern by chance roughly
    # once per 65536 packets. Usually it just logs "Failed to parse binary
    # command" and hands the payload back intact (noisy, harmless); but when the
    # following two bytes also happen to be a valid command id, it returns a
    # re-encoded BinaryCmdPkt instead, silently substituting garbage for a video
    # chunk. At ~244 packets/s that lands about once a day — a corrupted frame
    # from nowhere, with nothing in the log to explain it.
    if len(data) >= 8 and data[0] == CAM_MAGIC and data[1] == PacketType.Drw.value:
        body = data[4:]
        if body[0] == 0xD1 and body[1] == 0:          # DRW header, channel 0
            return DrwPkt(0, struct.unpack(">H", body[2:4])[0], body[4:])
    return _orig_parse(data)
_session.parse_packet = _patched_parse

MUX_START = b"\xa0\xaf\xaf\xaf"
MUX_END = b"\xf4\xf3\xf2\xf1"
CMD_HDR = bytes.fromhex("00036714646a") + b"\x00" * 12   # 18B; camera doesn't validate it
VID_MARK = b"\x01\xaf\xaf\xaf"
HEVC_VPS = b"\x00\x00\x00\x01\x40"   # HEVC VPS NAL — marks a keyframe boundary
STALL_SECONDS = 12      # streaming, then nothing downstream this long = dead
ACQUIRE_SECONDS = 30    # connected, but never yet produced anything playable


class StreamSession(JsonSession):
    def __init__(self, *a, stream=1, on_video=None, **k):
        super().__init__(*a, **k)
        self._idx = 0
        self._vbuf = bytearray()
        self._recent = collections.deque(maxlen=2048)   # video DRW idxs seen
        self._recent_set = set()                          # for O(1) dedup lookups
        self._stream = stream
        self._on_video = on_video       # callback(bytes) for each HEVC frame
        self._started = False           # gated until first keyframe (VPS)
        self._emitted = False           # has this session EVER emitted a frame
        self._dropped = False           # watchdog already fired
        self.userid = 0
        self.frames = 0
        self.vbytes = 0
        self.gaps = 0                   # lost-packet resyncs
        self._expect = None             # next expected video DRW idx
        self.last_video = time.monotonic()   # last video DRW packet from the cam
        self.last_emit = time.monotonic()    # last frame handed downstream

    async def _send_cmd(self, obj):
        js = json.dumps(obj, separators=(",", ":")).encode()
        mux = MUX_START + CMD_HDR + struct.pack("<I", len(js)) + js + MUX_END
        await self.send(DrwPkt(1, self._idx, mux))   # channel 1 = commands
        self._idx = (self._idx + 1) & 0xFFFF
        log.info("SENT %s", obj)

    async def setup_device(self):
        self.device_is_ready.set()
        await self._send_cmd({"cmd": "LoginDev", "pwd": self.auth_password})
        await asyncio.sleep(0.6)
        await self._send_cmd({
            "cmd": "OpenVideo", "state": 1, "pwd": self.auth_password,
            "stream": self._stream, "userid": self.userid,
        })
        log.info("login + OpenVideo(stream=%d) sent; waiting for keyframe...", self._stream)

    async def loop_step(self):
        await Session.loop_step(self)                 # PPPP keepalive only

        # The only liveness check anywhere in the pipeline, so it must not be
        # gated on self._started. That flag is False until the first keyframe
        # AND is cleared again by every packet-loss resync, and while it was
        # part of the condition a session that connected but never produced
        # playable video was completely undetected: OpenVideo refused, wrong
        # password, another client already holding the camera (connectNum: 2),
        # or a resync the camera never completed. We would sit in the P2PAlive
        # loop forever, and because Session.loop_step (not JsonSession's) runs
        # here, nothing else was watching either. Nothing above notices a mute
        # session: ffmpeg blocks reading the FIFO, capture.sh blocks in `wait
        # -n`, and the supervisor blocks on capture.sh — still holding the wake
        # lock and the pidfile, so even a plain restart is refused. --give-up
        # does not cover it either; that only counts discovery timeouts, i.e. an
        # unreachable IP, and this camera answers discovery fine.
        #
        # Time from the last frame handed DOWNSTREAM, not the last packet from
        # the camera: a camera that talks while emitting nothing decodable is
        # just as dead to the pipeline. Before anything has ever been emitted we
        # allow longer, because login + OpenVideo + first keyframe legitimately
        # takes a few seconds; after that a resync only has to wait one GOP.
        limit = STALL_SECONDS if self._emitted else ACQUIRE_SECONDS
        idle = time.monotonic() - self.last_emit
        if self._dropped or idle <= limit:
            return
        self._dropped = True
        log.warning("no video downstream for %.0fs (last camera packet %.0fs "
                    "ago, %d frames this session) — dropping session",
                    idle, time.monotonic() - self.last_video, self.frames)
        # Say goodbye before going. Our own abandoned sessions otherwise linger
        # as connections on the camera, and enough of them is itself a reason
        # the next OpenVideo comes back refused.
        try:
            await self.send_close_pkt()
        except Exception as exc:
            log.debug("close packet failed on the way out: %s", exc)
        self._on_device_lost()

    async def handle_drw(self, drw):
        await Session.handle_drw(self, drw)           # send DRW ack
        ch = drw._channel.value
        payload = drw.get_drw_payload()
        if ch == 0:
            self._feed_video(drw._cmd_idx, payload)
        elif ch == 1:
            self._handle_response(payload)

    def _handle_response(self, payload):
        i, j = payload.find(b"{"), payload.rfind(b"}")
        if 0 <= i <= j:
            try:
                obj = json.loads(payload[i:j + 1])
            except Exception:
                return
            log.info("RESP %s", obj)
            if obj.get("cmd") == "LoginDev" and obj.get("result") == 0:
                self.userid = obj.get("connectNum", 0)

    def _feed_video(self, idx, chunk):
        self.last_video = time.monotonic()
        # The camera retransmits some DRW packets; skip idxs we've already taken
        # (a 16-bit idx is unique within this 2048-packet window) to avoid
        # duplicated frames ("Duplicate POC" in the decoder).
        if idx in self._recent_set:
            return
        if len(self._recent) == self._recent.maxlen:
            self._recent_set.discard(self._recent[0])
        self._recent.append(idx)
        self._recent_set.add(idx)

        # Gap detection. This transport is UDP with no recovery of its own: if a
        # video DRW packet is simply lost, the byte stream gets a hole, and
        # splicing across it yields NALUs with nonsense headers ("Failed to
        # parse header of NALU (type 43)") that the decoder never resyncs from.
        # The result is a permanently black feed from a single lost packet, so
        # drop the partial frame and re-gate on the next keyframe instead —
        # ~1 GOP of black rather than an unbounded one.
        if self._expect is not None:
            ahead = (idx - self._expect) & 0xFFFF
            if ahead and ahead < 1024:                 # forward jump = lost packets
                self.gaps += 1
                log.warning("lost %d video packet(s) at idx %d — resyncing on "
                            "next keyframe (%d gaps this session)",
                            ahead, idx, self.gaps)
                self._vbuf.clear()
                self._started = False
            elif ahead >= 1024:                        # far behind = late/reordered
                return                                 # too stale to splice in
        self._expect = (idx + 1) & 0xFFFF
        self._vbuf += chunk
        while True:
            m1 = self._vbuf.find(VID_MARK)
            if m1 < 0:
                break
            m2 = self._vbuf.find(VID_MARK, m1 + 4)
            if m2 < 0:
                break                                 # frame incomplete; wait
            frame = bytes(self._vbuf[m1:m2])
            del self._vbuf[:m2]
            k = frame.find(b"\x00\x00\x00\x01")
            if k < 0:
                k = frame.find(b"\x00\x00\x01")
            if k >= 0:
                self._emit(frame[k:])

    def _emit(self, hevc):
        # Gate output until the first keyframe so the decoder gets VPS/SPS/PPS
        # before any P-frame (kills "PPS id out of range" and gray starts).
        if not self._started:
            if HEVC_VPS not in hevc:
                return
            self._started = True
            log.info("keyframe acquired — streaming")
        self._emitted = True
        self.last_emit = time.monotonic()
        self.frames += 1
        self.vbytes += len(hevc)
        if self._on_video:
            self._on_video(hevc)


async def discover_once(addr, timeout=15):
    fut = asyncio.get_running_loop().create_future()
    disc = Discovery(remote_addr=addr)
    def cb(dev):
        if not fut.done():
            fut.set_result(dev)
    task = asyncio.create_task(disc.discover(cb))
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def amain(args, sink):
    deadline = (time.monotonic() + args.seconds) if args.seconds > 0 else None
    missing_since = None
    while deadline is None or time.monotonic() < deadline:
        try:
            dev = await discover_once(args.addr, timeout=15)
        except asyncio.TimeoutError:
            # We probe --addr directly, so if the camera took a new DHCP lease we
            # would sit here forever on a dead IP. Give up instead and let the
            # supervisor (run/capture.sh) re-broadcast and find the new address.
            missing_since = missing_since or time.monotonic()
            gone = time.monotonic() - missing_since
            if args.give_up and gone >= args.give_up:
                log.error("camera unreachable at %s for %.0fs — exiting so the "
                          "caller can re-discover it", args.addr, gone)
                return 3
            log.warning("camera not found; retrying...")
            continue
        missing_since = None
        dev.is_json = True
        log.info("found %s at %s", dev.dev_id, dev.addr)
        lost = asyncio.Event()
        sess = StreamSession(dev, on_disconnect=lambda d: lost.set(),
                             login="admin", password=args.password,
                             stream=args.stream, on_video=sink)
        sess.start()
        try:
            if deadline:
                await asyncio.wait_for(lost.wait(), timeout=max(0.1, deadline - time.monotonic()))
            else:
                await lost.wait()
        except asyncio.TimeoutError:
            pass
        try:
            sess.stop()
        except Exception:
            pass
        log.info("captured %d frames / %d HEVC bytes / %d packet-loss resyncs "
                 "this session", sess.frames, sess.vbytes, sess.gaps)
        if deadline and time.monotonic() >= deadline:
            break
        log.info("reconnecting in 3s...")
        await asyncio.sleep(3)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--addr", required=True, help="camera IP (or 255.255.255.255)")
    ap.add_argument("-p", "--password", default="12345678")
    ap.add_argument("--stream", type=int, default=1, help="0=4K 1=2K/1080p 2=1080p")
    ap.add_argument("--seconds", type=float, default=0, help="0 = run forever w/ reconnect")
    ap.add_argument("--out", default="", help="write HEVC to this file")
    ap.add_argument("--pipe", action="store_true", help="write HEVC to stdout (for ffmpeg -)")
    ap.add_argument("--give-up", type=float, default=60,
                    help="exit(3) after this many seconds of not reaching --addr, "
                         "so a supervisor can re-discover a moved camera (0 = never)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)

    out = open(args.out, "wb") if args.out else None
    def sink(hevc):
        if out:
            out.write(hevc); out.flush()
        if args.pipe:
            try:
                sys.stdout.buffer.write(hevc); sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError) as exc:
                # ffmpeg downstream has gone (its SRT socket died, it was OOM
                # killed, the VPS moved...). Nothing here can revive it, and
                # staying alive is actively harmful: this runs inside an asyncio
                # callback, so the exception gets swallowed and the session sails
                # on sending P2PAlive forever while no video reaches anyone. The
                # shell pipeline in run/capture.sh only completes when BOTH ends
                # exit, so a surviving producer wedges the supervisor for good —
                # which is exactly how a VPN flap turned into a dead stream that
                # never came back. Exit and let the supervisor rebuild the pipe.
                log.error("downstream closed the pipe (%s) — exiting so the "
                          "supervisor can restart the pipeline", exc)
                if out:
                    try:
                        out.close()
                    except OSError:
                        pass
                sys.stderr.flush()
                os._exit(4)
    rc = 0
    try:
        rc = asyncio.run(amain(args, sink)) or 0
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
