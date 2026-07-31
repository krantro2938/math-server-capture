#!/usr/bin/env python3
"""
Runner for aiopppp adapted to the LookCam GHBB firmware.

Two tweaks over stock aiopppp, both reproducible (no editing the installed lib):

  1. The GHBB camera sends a 0x43 "ready" packet where stock aiopppp expects
     P2pRdy (0x42); stock code raises `ValueError: 67 is not a valid PacketType`
     and never connects. We map 0x43 -> P2pRdy at parse time.

  2. aiopppp guesses JSON-vs-binary command protocol from encryption
     (is_json = encrypted). LookCam is unencrypted yet uses JSON commands, so
     that guess can be wrong. --mode lets us force it and see which works.

Usage:
    python run_aiopppp.py -a 10.158.133.99 -u admin -p 12345678 --mode binary
    python run_aiopppp.py -a 10.158.133.99 -u admin -p 12345678 --mode json
Then open http://localhost:4000
"""
import argparse
import asyncio
import logging

from aiopppp.const import CAM_MAGIC, PacketType
from aiopppp import session as _session
from aiopppp.packets import Packet, parse_packet as _orig_parse_packet
import aiopppp.__main__ as _m

UNKNOWN_READY = 0x43


def _patched_parse_packet(data):
    # Only special-case the GHBB ready byte; everything else is stock parsing.
    if len(data) >= 2 and data[0] == CAM_MAGIC and data[1] == UNKNOWN_READY:
        return Packet(PacketType.P2pRdy, data[4:])
    return _orig_parse_packet(data)


# session.on_receive() calls parse_packet imported into the session module.
_session.parse_packet = _patched_parse_packet

FORCE_JSON = None  # set from --mode
_orig_make_session = _m.make_session


def _make_session_override(device, on_device_lost, login="", password=""):
    if FORCE_JSON is not None:
        device.is_json = FORCE_JSON
    logging.getLogger("run").info(
        "device %s -> is_json=%s", device.dev_id, device.is_json
    )
    return _orig_make_session(device, on_device_lost, login=login, password=password)


_m.make_session = _make_session_override


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--addr", required=True)
    ap.add_argument("-u", "--username", default="admin")
    ap.add_argument("-p", "--password", default="12345678")
    ap.add_argument("--mode", choices=["auto", "json", "binary"], default="auto")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    global FORCE_JSON
    FORCE_JSON = {"json": True, "binary": False, "auto": None}[args.mode]

    logging.basicConfig(level=logging.getLevelName(args.log_level.upper()))
    asyncio.run(_m.amain(
        remote_addr=args.addr,
        local_port=0,
        username=args.username,
        password=args.password,
    ))


if __name__ == "__main__":
    main()
