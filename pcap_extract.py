#!/usr/bin/env python3
"""Extract PPPP/PPCS framing from a PCAPdroid capture: find the DRW packets that
carry LookCam JSON commands and dump the exact bytes wrapped around the JSON."""
import struct, sys

path = sys.argv[1]
data = open(path, "rb").read()

magic = struct.unpack("<I", data[:4])[0]
if magic in (0xA1B2C3D4, 0xA1B23C4D):
    end = "<"
elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
    end = ">"
else:
    print("unknown pcap magic", hex(magic)); sys.exit(1)
linktype = struct.unpack(end + "I", data[20:24])[0]
print(f"pcap magic={hex(magic)} linktype={linktype}")

off = 24
pkts = []
while off + 16 <= len(data):
    ts_s, ts_u, incl, orig = struct.unpack(end + "IIII", data[off:off+16])
    off += 16
    raw = data[off:off+incl]
    off += incl
    # strip link layer
    if linktype == 1:       # Ethernet
        ip = raw[14:]
    elif linktype in (101, 12, 14):  # raw IP
        ip = raw
    elif linktype == 113:   # linux SLL
        ip = raw[16:]
    else:
        ip = raw
    if not ip or (ip[0] >> 4) != 4:
        continue
    ihl = (ip[0] & 0xF) * 4
    if ip[9] != 17:         # not UDP
        continue
    src = ".".join(map(str, ip[12:16])); dst = ".".join(map(str, ip[16:20]))
    udp = ip[ihl:]
    if len(udp) < 8:
        continue
    sport, dport = struct.unpack(">HH", udp[0:4])
    payload = udp[8:]
    pkts.append((src, sport, dst, dport, payload))

print(f"{len(pkts)} UDP packets")

def hexdump(b, n=160):
    b = b[:n]
    return " ".join(f"{x:02x}" for x in b)

# Find packets whose payload contains a JSON command verb.
verbs = [b"LoginDev", b"OpenVideo", b"OpenAudio", b'"cmd"', b"GetParms", b"TimeFlag"]
seen = 0
for src, sp, dst, dp, pl in pkts:
    if any(v in pl for v in verbs):
        seen += 1
        i = pl.find(b"{")
        jend = pl.rfind(b"}")
        pre = pl[:i] if 0 <= i else b""
        print("\n" + "="*90)
        print(f"{src}:{sp} -> {dst}:{dp}   len={len(pl)}")
        print(f"  FULL   : {hexdump(pl)}")
        print(f"  PREFIX ({len(pre)}B before '{{'): {pre.hex(' ')}")
        if 0 <= i <= jend:
            print(f"  JSON   : {pl[i:jend+1].decode('utf-8','replace')}")
        if seen >= 25:
            break

if not seen:
    print("\nNo JSON verbs found in payloads. Sample of f1-magic packets:")
    for src, sp, dst, dp, pl in pkts:
        if pl[:1] == b"\xf1":
            print(f"  {src}:{sp}->{dst}:{dp} type=0x{pl[1]:02x} len={len(pl)}: {hexdump(pl,48)}")
