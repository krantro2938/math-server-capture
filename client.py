#!/usr/bin/env python3
"""
PPPP LAN client / probe for CS2 "P2P" cameras (LookCam family).

This talks the PPPP protocol directly to a camera on your LAN (no CS2 cloud
servers involved), performs the connection handshake, sends candidate control
commands, ACKs the data channel, reassembles what the camera sends back, and
writes the video payload to disk (and optionally pipes it straight into ffmpeg).

It is deliberately verbose: PPPP has drifted across firmware generations, so the
exact opcode values and the exact "start video" command differ between builds.
Rather than pretend one hard-coded sequence works everywhere, this logs every
byte the camera sends so we can read the real answer and lock the sequence in.

USAGE
    # 1) find the camera first
    python3 discover.py --uid G683009DYDYB

    # 2) probe it (writes session-*.log and, once video flows, capture.h264)
    python3 client.py --ip 192.168.1.50 --uid G683009DYDYB --pwd 12345678

    # 3) once capture.h264 contains a real H.264 stream, play/store it:
    ffmpeg -fflags +genpts -i capture.h264 -c copy -f segment \
           -segment_time 900 -reset_timestamps 1 rec_%03d.mp4

    # or pipe live:
    python3 client.py --ip 192.168.1.50 --uid G683009DYDYB --pwd 12345678 \
        --pipe | ffmpeg -f h264 -i - -c copy -f segment -segment_time 900 rec_%03d.mp4

If a step gets no reply, flip the OPCODE variant (see CONSTANTS below) or adjust
the COMMANDS list — both are meant to be edited as we learn the device.
"""

import argparse
import json
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# CONSTANTS  (PPPP 0xF1 message family — the LookCam/CS2 variant).
# If discover.py showed the camera replying to the "f1 20 00 00" probe instead,
# or the handshake below stalls, switch to the ALT values noted on each line.
# ---------------------------------------------------------------------------
MAGIC = 0xF1

MSG_HELLO      = 0x00
MSG_HELLO_ACK  = 0x01
MSG_P2P_REQ    = 0x20
MSG_LAN_SEARCH = 0x30
MSG_P2P_RDY    = 0x42
MSG_DRW        = 0xD0   # ALT (CS2 docs): 0x60
MSG_DRW_ACK    = 0xD1   # ALT: 0x61
MSG_ALIVE      = 0xE0   # ALT: 0x40
MSG_ALIVE_ACK  = 0xE1   # ALT: 0x41
MSG_CLOSE      = 0xF0

# Control channel used for JSON/ioctl commands; video usually arrives on ch 1.
CMD_CHANNEL = 0

# ---------------------------------------------------------------------------
# Candidate commands to wake the video stream. LookCam firmware takes JSON blobs
# over DRW (Palant, 2025); the password is sent but not actually verified. The
# exact "start video" verb varies, so we try several and watch what responds.
# Edit this list freely as we learn the device from the logs.
# ---------------------------------------------------------------------------
def build_commands(pwd: str) -> list[tuple[str, bytes]]:
    def j(obj) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode()

    return [
        ("LoginDev",        j({"cmd": "LoginDev", "pwd": pwd})),
        ("StartStream",     j({"cmd": "StartStream", "pwd": pwd})),
        ("startVideo",      j({"cmd": "startVideo", "channel": 0, "pwd": pwd})),
        ("SetVideoParam",   j({"cmd": "SetVideoParam", "stream": 0, "pwd": pwd})),
        ("GetParms",        j({"cmd": "GetParms", "pwd": pwd})),
        # Some CS2/TUTK-derived builds want a binary ioctl instead of JSON.
        # IOTYPE_USER_IPCAM_START = 0x01FF, stream index 0.
        ("ioctl_av_start",  struct.pack("<HHI", 0x01FF, 0, 0)),
    ]


def encode(msg_type: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BBH", MAGIC, msg_type, len(payload)) + payload


def parse(buf: bytes):
    """Yield (msg_type, payload) for each PPPP frame in buf."""
    off = 0
    while off + 4 <= len(buf):
        magic, mtype, length = struct.unpack_from(">BBH", buf, off)
        if magic != MAGIC:
            off += 1
            continue
        payload = buf[off + 4 : off + 4 + length]
        yield mtype, payload
        off += 4 + length


def drw(channel: int, index: int, data: bytes) -> bytes:
    # DRW payload = 'D'-style channel/index header then the data.
    # Layout used by the 0xF1 family: [d1][channel][index:2 BE][data]
    body = struct.pack(">BBH", 0xD1, channel, index) + data
    return encode(MSG_DRW, body)


def drw_ack(channel: int, index: int) -> bytes:
    body = struct.pack(">BBHH", 0xD1, channel, 1, index)
    return encode(MSG_DRW_ACK, body)


def hexdump(data: bytes, limit: int = 64) -> str:
    chunk = data[:limit]
    h = " ".join(f"{b:02x}" for b in chunk)
    a = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return f"{h}{' ...' if len(data) > limit else ''}\n        | {a}"


def looks_like_h264(data: bytes) -> bool:
    return b"\x00\x00\x00\x01" in data or b"\x00\x00\x01" in data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ip", required=True, help="camera LAN IP (from discover.py)")
    ap.add_argument("--port", type=int, default=32108, help="camera UDP port")
    ap.add_argument("--uid", required=True, help="device UID, e.g. G683009DYDYB")
    ap.add_argument("--pwd", default="12345678", help="access password")
    ap.add_argument("--pipe", action="store_true",
                    help="write captured video bytes to stdout (for ffmpeg -)")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="how long to stay connected and capture")
    args = ap.parse_args()

    log_path = f"session-{int(time.time())}.log"
    log_f = open(log_path, "w")
    vid_f = open("capture.h264", "wb")

    def log(msg: str):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, file=sys.stderr)
        log_f.write(line + "\n")
        log_f.flush()

    def out_video(data: bytes):
        vid_f.write(data)
        vid_f.flush()
        if args.pipe:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    dst = (args.ip, args.port)

    log(f"[*] target {dst}  uid={args.uid}  log={log_path}")

    # --- Handshake -----------------------------------------------------------
    # On the LAN we can go straight to the device. Announce ourselves, then
    # signal we're ready for the P2P session.
    sock.sendto(encode(MSG_HELLO), dst)
    sock.sendto(encode(MSG_P2P_RDY, args.uid.encode()), dst)

    commands = build_commands(args.pwd)
    cmd_i = 0
    tx_index = 0          # our outgoing DRW sequence
    next_cmd_at = time.time() + 0.5
    next_alive_at = time.time() + 1.0
    deadline = time.time() + args.seconds
    video_bytes = 0

    while time.time() < deadline:
        # Keepalive so the device doesn't drop us.
        if time.time() >= next_alive_at:
            sock.sendto(encode(MSG_ALIVE), dst)
            next_alive_at = time.time() + 1.0

        # Walk through candidate start commands until video appears.
        if video_bytes == 0 and time.time() >= next_cmd_at and cmd_i < len(commands):
            name, blob = commands[cmd_i]
            sock.sendto(drw(CMD_CHANNEL, tx_index, blob), dst)
            log(f"[>] cmd '{name}' (drw idx {tx_index}) {blob[:60]!r}")
            tx_index = (tx_index + 1) & 0xFFFF
            cmd_i += 1
            next_cmd_at = time.time() + 1.2

        try:
            data, src = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError as e:
            log(f"[!] socket error: {e}")
            break

        for mtype, payload in parse(data):
            if mtype == MSG_HELLO_ACK:
                log("[<] HELLO_ACK")
            elif mtype == MSG_ALIVE_ACK:
                pass  # too chatty to log
            elif mtype == MSG_P2P_RDY:
                log("[<] P2P_RDY — session up")
            elif mtype == MSG_DRW:
                # [d1][channel][index:2][data]
                if len(payload) >= 4:
                    channel = payload[1]
                    index = struct.unpack_from(">H", payload, 2)[0]
                    body = payload[4:]
                    sock.sendto(drw_ack(channel, index), dst)  # must ACK
                    if looks_like_h264(body) or channel == 1:
                        video_bytes += len(body)
                        out_video(body)
                        if video_bytes and video_bytes - len(body) == 0:
                            log(f"[<] *** VIDEO on ch{channel}: H.264 detected ***")
                    else:
                        text = body[:80]
                        log(f"[<] DRW ch{channel} idx{index}: {text!r}")
            elif mtype == MSG_CLOSE:
                log("[<] CLOSE — device hung up")
                deadline = 0
            else:
                log(f"[<] type 0x{mtype:02x} len {len(payload)}\n        {hexdump(payload)}")

    sock.sendto(encode(MSG_CLOSE), dst)
    sock.close()
    vid_f.close()
    log(f"[*] done. captured {video_bytes} video bytes -> capture.h264")
    if video_bytes == 0:
        log("[-] no video yet. Check the log for what the camera *did* send,")
        log("    then adjust OPCODE variant / COMMANDS and re-run.")
    log_f.close()
    return 0 if video_bytes else 2


if __name__ == "__main__":
    sys.exit(main())
