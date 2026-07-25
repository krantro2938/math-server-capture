#!/usr/bin/env python3
"""
LAN discovery for CS2 / PPPP "P2P" cameras (LookCam family).

Run this on a machine on the SAME LAN as the camera. It broadcasts every known
CS2 LAN-search probe and prints any device that answers, together with its IP
and the ASCII device UID it reports (e.g. GHBB-683009-DYDYB).

Nothing here is device-specific or destructive: it is exactly what the phone app
does on startup to find a camera on the local network.

    python3 discover.py               # ~4s scan, all interfaces' broadcast
    python3 discover.py --timeout 8   # listen longer
    python3 discover.py --uid G683009DYDYB   # highlight your camera
    python3 discover.py --print-ip --uid G683009DYDYB   # just the IP, for scripts

Why several probes? The CS2 stack has drifted across firmware generations, so
the LAN-search packet appears in the wild in a few forms. We fire all of them;
whichever one your camera answers also tells us which protocol variant it speaks
(useful for the next step, client.py).
"""

import argparse
import socket
import struct
import subprocess
import sys
import time

LAN_SEARCH_PORTS = [32108, 32100, 32760, 32761, 10240]

# Candidate LAN-search payloads. Each tuple is (label, bytes).
#   - "f1 30 00 00"  -> PPPP MSG_LAN_SEARCH in the 0xF1 message family
#   - 0x2CBA5F5D LE  -> the 4-byte CS2 broadcast magic documented for port 32108
#   - "f1 20 00 00"  -> older MSG_LAN_SEARCH opcode seen on some builds
PROBES = [
    ("PPPP MSG_LAN_SEARCH f1300000", bytes.fromhex("f1300000")),
    ("CS2 magic 0x2CBA5F5D", struct.pack("<I", 0x2CBA5F5D)),
    ("PPPP alt f1200000", bytes.fromhex("f1200000")),
    ("PPPP hello f1000000", bytes.fromhex("f1000000")),
]


def broadcast_addrs() -> list[str]:
    """255.255.255.255 plus the directed broadcast of EVERY up IPv4 interface.

    A device can sit on several networks at once (WiFi + VPN + docker bridges),
    so we don't guess a single "primary" one — we pull the broadcast address the
    kernel already assigned to each interface from `ip -o -4 addr` and probe them
    all. Falls back to the connect()-trick if `ip` isn't available.
    """
    addrs = {"255.255.255.255"}
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if "brd" in parts:
                brd = parts[parts.index("brd") + 1]
                if brd and brd != "127.255.255.255":
                    addrs.add(brd)
    except (OSError, subprocess.SubprocessError):
        pass

    if len(addrs) == 1:  # `ip` gave us nothing usable; fall back to the trick.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            addrs.add(".".join(ip.split(".")[:3] + ["255"]))
        except OSError:
            pass
    return sorted(addrs)


def find_uid(data: bytes) -> str | None:
    """Devices echo their UID as ASCII somewhere in the reply; pull it out."""
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    import re

    # Full form  GHBB-683009-DYDYB  or shortened form  G683009DYDYB
    m = re.search(r"[A-Z]{3,4}-?\d{6}-?[A-Z0-9]{5}", text)
    return m.group(0) if m else None


def hexdump(data: bytes, limit: int = 96) -> str:
    chunk = data[:limit]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    tail = " ..." if len(data) > limit else ""
    return f"{hexs}{tail}\n      | {asci}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=4.0, help="listen seconds")
    ap.add_argument("--uid", help="highlight this UID if it answers")
    ap.add_argument("--broadcast", action="append", default=[], metavar="ADDR",
                    help="extra broadcast address to probe (repeatable). Useful on "
                         "Android, where the hotspot interface may not show up in "
                         "`ip addr` — e.g. --broadcast 192.168.43.255")
    ap.add_argument("--print-ip", action="store_true",
                    help="machine mode: print ONLY the camera's IP (first match, "
                         "filtered by --uid if given) and exit 0; exit 1 if not found")
    args = ap.parse_args()

    # In --print-ip mode stdout must stay clean (scripts capture it), so all the
    # human chatter goes to stderr.
    def say(*a):
        print(*a, file=sys.stderr if args.print_ip else sys.stdout)

    def matches(uid: str | None) -> bool:
        if not args.uid:
            return True
        return bool(uid) and args.uid.replace("-", "") in uid.replace("-", "")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    sock.settimeout(0.4)

    targets = sorted(set(broadcast_addrs()) | set(args.broadcast))
    say(f"[*] broadcasting {len(PROBES)} probe types to {targets}")
    say(f"[*] ports: {LAN_SEARCH_PORTS}   listening {args.timeout:.0f}s\n")

    for _, payload in PROBES:
        for addr in targets:
            for port in LAN_SEARCH_PORTS:
                try:
                    sock.sendto(payload, (addr, port))
                except OSError:
                    pass

    seen: dict[str, dict] = {}
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            data, (src_ip, src_port) = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        uid = find_uid(data)
        key = src_ip
        if key not in seen:
            seen[key] = {"uid": uid, "port": src_port, "raw": data}
            hit = matches(uid)
            say(f"[+] {src_ip}:{src_port}  uid={uid or '?'}"
                f"{'   <<< THIS IS YOUR CAMERA' if args.uid and hit else ''}")
            say(f"      {hexdump(data)}\n")
            if args.print_ip and hit:
                sock.close()
                print(src_ip)          # the only thing on stdout
                return 0

    sock.close()
    if not seen:
        say("[-] no CS2/PPPP devices answered.")
        say("    - confirm this host and the camera are on the same subnet/AP")
        say("    - some APs isolate WiFi clients (\"AP isolation\"); try wired")
        say("    - re-run with --timeout 10")
        return 1
    if args.print_ip:                  # devices answered, but none was our UID
        say(f"[-] {len(seen)} device(s) answered, none matching uid={args.uid}")
        return 1
    say(f"[*] done. {len(seen)} device(s). Feed the IP to client.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
