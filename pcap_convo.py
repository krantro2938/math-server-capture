#!/usr/bin/env python3
"""Dump the LookCam command conversation: for every DRW packet carrying JSON,
show direction, channel/idx, the 18-byte mux header, and the JSON. Also profile
the non-JSON DRW (video) packets."""
import struct, sys, re

data = open(sys.argv[1], "rb").read()
end = "<" if struct.unpack("<I", data[:4])[0] in (0xA1B2C3D4, 0xA1B23C4D) else ">"
linktype = struct.unpack(end + "I", data[20:24])[0]
off = 24
pkts = []
while off + 16 <= len(data):
    _, _, incl, _ = struct.unpack(end + "IIII", data[off:off+16]); off += 16
    raw = data[off:off+incl]; off += incl
    ip = raw if linktype in (101,12,14) else raw[14:] if linktype==1 else raw[16:] if linktype==113 else raw
    if not ip or (ip[0]>>4)!=4 or ip[9]!=17: continue
    ihl=(ip[0]&0xF)*4; src=".".join(map(str,ip[12:16])); dst=".".join(map(str,ip[16:20]))
    udp=ip[ihl:]
    if len(udp)<8: continue
    pkts.append((src, dst, udp[8:]))

CAM = "10.158.133.99"

def split_frames(drw_body):
    """A DRW payload may pack several a0afafaf...f4f3f2f1 frames back to back."""
    frames = []
    for m in re.finditer(rb"\xa0\xaf\xaf\xaf", drw_body):
        s = m.start()
        e = drw_body.find(b"\xf4\xf3\xf2\xf1", s)
        if e != -1:
            frames.append(drw_body[s:e+4])
    return frames

print("=== JSON command conversation ===")
for src, dst, pl in pkts:
    if pl[:2] != b"\xf1\xd0":  # DRW only
        continue
    dirn = "APP->CAM" if dst == CAM else "CAM->APP" if src == CAM else "?"
    chan = pl[5] if len(pl) > 5 else -1
    idx = struct.unpack(">H", pl[6:8])[0] if len(pl) >= 8 else -1
    body = pl[8:]
    for fr in split_frames(body):
        # fr = a0afafaf + 18B header + len(4 LE) + json + f4f3f2f1
        hdr = fr[4:22]
        jlen = struct.unpack("<I", fr[22:26])[0]
        js = fr[26:26+jlen]
        if b'"cmd"' in js or b"cmd" in js:
            cmd = re.search(rb'"cmd":\s*"([^"]+)"', js)
            cmd = cmd.group(1).decode() if cmd else "?"
            js1 = b" ".join(js.split())  # collapse whitespace
            print(f"{dirn} ch{chan} idx{idx:<4} hdr={hdr.hex()} {js1.decode('utf-8','replace')[:120]}")

print("\n=== non-JSON DRW packets (candidate video), first 12 ===")
n = 0
for src, dst, pl in pkts:
    if pl[:2] != b"\xf1\xd0": continue
    if src != CAM: continue
    body = pl[8:]
    if b"\xa0\xaf\xaf\xaf" in body:  # it's a cmd frame, skip
        continue
    chan = pl[5]; idx = struct.unpack(">H", pl[6:8])[0]
    print(f"CAM->APP ch{chan} idx{idx} len={len(pl)} head={pl[:24].hex(' ')}")
    n += 1
    if n >= 12: break
