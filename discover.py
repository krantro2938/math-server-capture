#!/usr/bin/env python3
"""
LAN discovery for CS2 / PPPP "P2P" cameras (LookCam family).

Run this on a machine on the SAME LAN as the camera. It broadcasts the CS2
LAN-search probe and prints any device that answers, together with its IP and
the device UID it reports (e.g. GHBB-683009-DYDYB).

Nothing here is device-specific or destructive: it is exactly what the phone app
does on startup to find a camera on the local network.

    python3 discover.py               # ~4s scan, all interfaces' broadcast
    python3 discover.py --timeout 8   # listen longer
    python3 discover.py --uid G683009DYDYB   # highlight your camera
    python3 discover.py --print-ip --uid G683009DYDYB   # just the IP, for scripts
    python3 discover.py --sweep       # skip straight to the unicast subnet sweep
    python3 discover.py -v            # show every UDP reply, parsed or not

Why several probes? The CS2 stack has drifted across firmware generations, so
the LAN-search packet appears in the wild in a few forms. We fire all of them,
each in both plaintext and XOR1-obfuscated form; whichever one your camera
answers also tells us which protocol variant it speaks.

Broadcast is not always enough. On an Android hotspot an unbound socket is
routed onto *mobile data*, and plenty of APs drop client-to-client broadcast
("AP isolation") — in both cases the probe never reaches the camera. So we send
from a socket bound to each interface's own address, and if broadcast comes back
empty we fall back to unicast-probing every host in the attached /24s, which
needs no broadcast support at all.
"""

import argparse
import ipaddress
import re
import select
import socket
import struct
import subprocess
import sys
import time

LAN_SEARCH_PORTS = [32108, 32100, 32760, 32761, 10240]

# Candidate LAN-search payloads. Each tuple is (label, bytes). Every one is sent
# twice, plaintext and XOR1-wrapped — that pair covers both firmware families.
# (The "CS2 magic 0x2CBA5F5D" floating around online is not a separate probe at
# all: it is exactly xor1_encode(f1 30 00 00). Sending both forms subsumes it.)
#   - "f1 30 00 00"  -> PPPP MSG_LAN_SEARCH, what the LookCam app really sends
#   - "f1 32 00 00"  -> MSG_LAN_SEARCH_EXT, the newer variant
#   - "f1 20 00 00"  -> older MSG_LAN_SEARCH opcode seen on some builds
PROBES = [
    ("PPPP MSG_LAN_SEARCH f1300000", bytes.fromhex("f1300000")),
    ("PPPP LAN_SEARCH_EXT f1320000", bytes.fromhex("f1320000")),
    ("PPPP alt f1200000", bytes.fromhex("f1200000")),
    ("PPPP hello f1000000", bytes.fromhex("f1000000")),
]

CAM_MAGIC = 0xF1
# Packet types that carry a device identity. 0x41 (PunchPkt) is the normal
# answer to a LAN search; 0x42 is the stock "ready", and this GHBB firmware
# answers 0x43 instead (same quirk lookcam_stream.py patches around).
ID_BEARING_TYPES = {0x41, 0x40, 0x42, 0x43, 0x30, 0x32}

# ---- XOR1 obfuscation (lifted from aiopppp/encrypt.py) -----------------------
# Some firmware families wrap every packet in this stream cipher. Cheap to try
# both ways, and it costs us nothing when the device speaks plaintext (as the
# GHBB does).
XOR1_KEY_TABLE = bytes.fromhex(
    "7c9ce84a13dedcb22f2123e4307b3d8cbc0b270c3cf79ae7087196009785efc1"
    "1fc4dba1c2ebd901faba3b05b81587832872d18b5ad6da9358feaacc6e1bf0a3"
    "88ab43c00db545384f502266207f075b14981d9ba72ab9a8cbf1fc4947063eb1"
    "0e043a945eee541134dd4df9ecc7c9e3781a6f706ba4bda95dd5f8e5bb26af42"
    "37d8e1020aae5f1cc573094e6924906d12b319ad748a2940f52dbea559e0f479"
    "d24bce8982488425c6912ba2fb8fe9a6b09e3f65f603312eac0f952c5ced39b7"
    "336c567eb4a0fd7a815351868d9f77ff6a80dfe2bf10d775645776f355cdd0c8"
    "18e6364162cf99f2324c67606192cad3ea637d16b68ed46835c3529d46441e17"
)
XOR1_ENC_KEY = (0x69, 0x97, 0xCC, 0x19)


def xor1_decode(data: bytes) -> bytes:
    prev = 0
    out = bytearray(len(data))
    for i, cur in enumerate(data):
        out[i] = cur ^ XOR1_KEY_TABLE[(XOR1_ENC_KEY[prev & 3] + prev) & 0xFF]
        prev = cur
    return bytes(out)


def xor1_encode(data: bytes) -> bytes:
    prev = 0
    out = bytearray(len(data))
    for i, cur in enumerate(data):
        out[i] = cur ^ XOR1_KEY_TABLE[(XOR1_ENC_KEY[prev & 3] + prev) & 0xFF]
        prev = out[i]
    return bytes(out)


# ---- interfaces --------------------------------------------------------------

def _iface_from_ip_cmd() -> list[tuple[str, str, str, int]]:
    """Parse `ip -o -4 addr show`. The good path — on a normal Linux box."""
    found = []
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found

    for line in out.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        try:
            iface = ipaddress.ip_interface(parts[parts.index("inet") + 1])
        except (ValueError, IndexError):
            continue
        if iface.ip.is_loopback:
            continue
        brd = parts[parts.index("brd") + 1] if "brd" in parts else None
        found.append((parts[1], str(iface.ip), brd or str(iface.network.broadcast_address),
                      iface.network.prefixlen))
    return found


def _iface_from_ioctl() -> list[tuple[str, str, str, int]]:
    """Enumerate via SIOCGIFCONF/SIOCGIFNETMASK ioctls instead of netlink.

    This is the one that matters on a phone. Android blocks RTM_GETLINK for
    unprivileged apps, so in Termux `ip addr` exits 0 having printed *nothing* —
    silently leaving us with no interfaces and no error to notice. The old
    connect()-to-8.8.8.8 fallback then reported only the mobile-data address,
    which is the wrong network entirely: the camera is on the hotspot AP
    interface, which never appeared. Plain ioctls on a UDP socket are not
    restricted, so they still see the AP.
    """
    try:
        import array
        import fcntl
    except ImportError:      # not POSIX
        return []

    SIOCGIFCONF, SIOCGIFNETMASK = 0x8912, 0x891B
    ifreq_size = 40 if struct.calcsize("P") == 8 else 32
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        buf = array.array("B", b"\0" * 8192)
        ptr, length = buf.buffer_info()
        res = fcntl.ioctl(s.fileno(), SIOCGIFCONF, struct.pack("iL", length, ptr))
        size = struct.unpack("iL", res)[0]
        data = buf.tobytes()
    except (OSError, struct.error):
        return []

    for off in range(0, size, ifreq_size):
        try:
            name = data[off:off + 16].split(b"\0")[0].decode()
            ip = socket.inet_ntoa(data[off + 20:off + 24])
        except (ValueError, OSError, UnicodeDecodeError):
            continue
        if ipaddress.ip_address(ip).is_loopback:
            continue
        try:
            nm = fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, struct.pack("16s24x", name.encode()))
            mask = socket.inet_ntoa(nm[20:24])
        except OSError:
            mask = "255.255.255.0"
        try:
            iface = ipaddress.ip_interface(f"{ip}/{mask}")
        except ValueError:
            continue
        found.append((name, ip, str(iface.network.broadcast_address), iface.network.prefixlen))
    s.close()
    return found


def interfaces(extra_subnets: list[str] | None = None) -> list[tuple[str, str, str, int]]:
    """[(ifname, local_ip, broadcast, prefixlen)] for every up IPv4 interface.

    A device can sit on several networks at once (WiFi + hotspot AP + VPN +
    docker bridges), so we don't guess a single "primary" one — we take the
    union of what `ip` and the ioctls report, keyed by address. Loopback is
    skipped; the connect() trick is the last resort.
    """
    by_ip: dict[str, tuple[str, str, str, int]] = {}
    for entry in _iface_from_ip_cmd() + _iface_from_ioctl():
        by_ip.setdefault(entry[1], entry)

    for cidr in extra_subnets or []:
        # An explicitly named subnet we may have no address on at all (e.g. the
        # AP subnet when even the ioctls come up empty). Probe it from whatever
        # source address the kernel picks.
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        mine = next((ip for ip in by_ip if ipaddress.ip_address(ip) in net), None)
        by_ip.setdefault(str(net.broadcast_address),
                         ("(--subnet)", mine or "0.0.0.0",
                          str(net.broadcast_address), net.prefixlen))

    if not by_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            by_ip[ip] = ("?", ip, ".".join(ip.split(".")[:3] + ["255"]), 24)
        except OSError:
            pass
    return list(by_ip.values())


# ---- reply parsing -----------------------------------------------------------

class Dev:
    """A device identity as it appears on the wire: GHBB + u64 + DYDYB."""

    def __init__(self, prefix: str, serial: int, suffix: str):
        self.prefix, self.serial, self.suffix = prefix, serial, suffix

    def __str__(self) -> str:
        return f"{self.prefix}-{self.serial}-{self.suffix}"

    def variants(self) -> set[str]:
        """Every spelling of this UID a human might type.

        The label on the camera and the app show the long form
        (GHBB-683009-DYDYB), while the LookCam UI and our own config use the
        short one (G683009DYDYB) — first letter of the prefix only. Neither is a
        substring of the other, so we enumerate instead of guessing.
        """
        p, n, s = self.prefix, self.serial, self.suffix
        return {
            f"{p}-{n}-{s}", f"{p}{n}{s}", f"{p[:1]}{n}{s}", f"{n}{s}", str(n),
        }


def parse_device(data: bytes) -> tuple[Dev | None, int | None, bytes]:
    """(device, pppp_type, decoded_bytes) for a PPPP reply, else (None, ..).

    Tries plaintext first, then XOR1. The identity payload is *binary*, not
    ASCII: 4-byte prefix, big-endian u64 serial, 5-byte suffix, NUL padding.
    (Scanning for an ASCII serial finds nothing — the digits are never on the
    wire.)
    """
    for decode in (lambda x: x, xor1_decode):
        try:
            buf = decode(data)
        except (ValueError, IndexError):
            continue
        if len(buf) < 4 or buf[0] != CAM_MAGIC:
            continue
        ptype, plen = buf[1], struct.unpack(">H", buf[2:4])[0]
        body = buf[4:4 + plen] if plen else buf[4:]
        if ptype not in ID_BEARING_TYPES or len(body) < 17:
            return None, ptype, buf
        try:
            prefix = body[:4].decode("ascii")
            suffix = body[12:20].rstrip(b"\x00").decode("ascii")
        except UnicodeDecodeError:
            return None, ptype, buf
        if not prefix.strip("\x00").isalnum() or not suffix.isalnum():
            return None, ptype, buf
        serial = struct.unpack(">Q", body[4:12])[0]
        return Dev(prefix.rstrip("\x00"), serial, suffix), ptype, buf
    return None, None, data


def uid_matches(wanted: str, dev: Dev | None) -> bool:
    """Does `wanted` name this device? Tolerant of dashes, case and short form."""
    if dev is None:
        return False
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())
    return norm(wanted) in {norm(v) for v in dev.variants()}


def hexdump(data: bytes, limit: int = 96) -> str:
    chunk = data[:limit]
    hexs = " ".join(f"{b:02x}" for b in chunk)
    asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    tail = " ..." if len(data) > limit else ""
    return f"{hexs}{tail}\n      | {asci}"


# ---- probing -----------------------------------------------------------------

def open_sockets(ifaces) -> list[tuple[socket.socket, str | None]]:
    """One socket bound per interface, plus one unbound.

    Binding to the interface's own address is what makes this work on an Android
    hotspot: an unbound UDP socket there is routed onto the default network
    (mobile data), so a broadcast aimed at the AP subnet silently leaves via the
    wrong NIC and nothing ever answers.
    """
    socks: list[tuple[socket.socket, str | None]] = []
    for name, ip, _brd, _plen in ifaces:
        if ip == "0.0.0.0":      # a --subnet we hold no address on; unbound covers it
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((ip, 0))
            s.setblocking(False)
            socks.append((s, name))
        except OSError:
            continue
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 0))
    s.setblocking(False)
    socks.append((s, None))
    return socks


def send_probes(socks, targets: list[str], ports=LAN_SEARCH_PORTS) -> None:
    for _label, payload in PROBES:
        for wire in (payload, xor1_encode(payload)):
            for sock, _name in socks:
                for addr in targets:
                    for port in ports:
                        try:
                            sock.sendto(wire, (addr, port))
                        except OSError:
                            pass


def sweep_hosts(ifaces, limit: int = 1024) -> list[str]:
    """Every unicast host address on our attached subnets (small ones only).

    Derived from the broadcast address rather than our own, so a subnet named
    with --subnet that we hold no address on still gets swept.
    """
    hosts: list[str] = []
    ours = {ip for _n, ip, _b, _p in ifaces}
    for _name, _ip, brd, plen in ifaces:
        try:
            net = ipaddress.ip_network(f"{brd}/{plen}", strict=False)
        except ValueError:
            continue
        if net.num_addresses > limit:
            continue
        hosts.extend(str(h) for h in net.hosts() if str(h) not in ours)
    return sorted(set(hosts), key=lambda h: tuple(int(o) for o in h.split(".")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=4.0, help="listen seconds")
    ap.add_argument("--uid", help="highlight this UID if it answers")
    ap.add_argument("--broadcast", action="append", default=[], metavar="ADDR",
                    help="extra broadcast address to probe (repeatable). Useful on "
                         "Android, where the hotspot interface may not show up in "
                         "`ip addr` — e.g. --broadcast 192.168.43.255")
    ap.add_argument("--subnet", action="append", default=[], metavar="CIDR",
                    help="a whole network to probe AND unicast-sweep, even if we "
                         "hold no address on it (repeatable). The escape hatch when "
                         "interface enumeration comes up empty on Android — "
                         "e.g. --subnet 10.158.133.0/24")
    ap.add_argument("--sweep", action="store_true",
                    help="unicast-probe every host on the attached subnets instead "
                         "of waiting for broadcast to fail first")
    ap.add_argument("--no-sweep", action="store_true",
                    help="broadcast only; never fall back to the unicast sweep")
    ap.add_argument("--print-ip", action="store_true",
                    help="machine mode: print ONLY the camera's IP (first match, "
                         "filtered by --uid if given) and exit 0; exit 1 if not found")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show every UDP reply, including ones we can't parse")
    args = ap.parse_args()

    # In --print-ip mode stdout must stay clean (scripts capture it), so all the
    # human chatter goes to stderr.
    def say(*a):
        print(*a, file=sys.stderr if args.print_ip else sys.stdout)

    ifaces = interfaces(args.subnet)
    socks = open_sockets(ifaces)
    targets = sorted({b for _n, _i, b, _p in ifaces} | {"255.255.255.255"} | set(args.broadcast))

    say("[*] interfaces: " + (", ".join(f"{n}={i}/{p} brd {b}" for n, i, b, p in ifaces) or "none found!"))
    say(f"[*] broadcasting {len(PROBES)} probe types (plain + xor1) to {targets}")
    say(f"[*] ports: {LAN_SEARCH_PORTS}   listening {args.timeout:.0f}s\n")

    seen: dict[str, Dev | None] = {}
    result_ip: str | None = None

    def listen(deadline: float) -> bool:
        """Drain replies until `deadline`. True = we found what --uid asked for."""
        nonlocal result_ip
        while time.time() < deadline:
            ready, _, _ = select.select([s for s, _ in socks], [], [],
                                        min(0.4, max(0.0, deadline - time.time())))
            for sock in ready:
                try:
                    data, (src_ip, src_port) = sock.recvfrom(2048)
                except OSError:
                    continue
                dev, ptype, decoded = parse_device(data)
                if src_ip in seen:
                    continue
                if dev is None and not args.verbose:
                    continue  # some other UDP noise on the wire
                seen[src_ip] = dev
                hit = dev is not None and (not args.uid or uid_matches(args.uid, dev))
                kind = f"  type=0x{ptype:02x}" if ptype is not None else ""
                mark = "   <<< THIS IS YOUR CAMERA" if hit and args.uid else ""
                say(f"[+] {src_ip}:{src_port}  uid={dev or '?'}{kind}{mark}")
                say(f"      {hexdump(decoded)}\n")
                if hit:
                    result_ip = result_ip or src_ip
                    if args.print_ip:
                        return True
        return False

    # Broadcast phase: re-send every second rather than once, so a camera that
    # is still associating with the AP still gets asked.
    if not args.sweep:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            send_probes(socks, targets)
            if listen(min(time.time() + 1.0, deadline)):
                break

    # Unicast fallback: works through AP isolation and Android's broadcast
    # routing, both of which swallow the phase above without a trace.
    if result_ip is None and not args.no_sweep:
        hosts = sweep_hosts(ifaces)
        if hosts:
            say(f"[*] broadcast found nothing — unicast-sweeping {len(hosts)} hosts...")
            send_probes(socks, hosts, ports=[32108])
            listen(time.time() + max(args.timeout, 3.0))

    for sock, _ in socks:
        sock.close()

    if args.print_ip and result_ip:
        print(result_ip)               # the only thing on stdout
        return 0

    if not seen:
        say("[-] no CS2/PPPP devices answered.")
        say("    - confirm this host and the camera are on the same subnet/AP")
        say("    - on an Android hotspot, check the AP interface is listed above;")
        say("      if it isn't, pass --broadcast <hotspot-subnet>.255")
        say("    - re-run with --timeout 10 -v")
        return 1
    if args.print_ip:                  # devices answered, but none was our UID
        say(f"[-] {len(seen)} device(s) answered, none matching uid={args.uid}: "
            + ", ".join(f"{ip}={dev or '?'}" for ip, dev in seen.items()))
        return 1
    say(f"[*] done. {len(seen)} device(s). Feed the IP to lookcam_stream.py next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
