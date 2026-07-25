#!/usr/bin/env python3
"""
Live command-probe: reuse aiopppp's (patched) working transport/handshake, but
after connecting, fire a batch of candidate LookCam-native command payloads and
log EVERY inbound DRW so we can see which verb/framing the camera actually
answers. Read-only reconnaissance against a camera we own.

    python probe_cmds.py -a 10.158.133.99
"""
import argparse
import asyncio
import json
import logging

from aiopppp.const import CAM_MAGIC, PacketType
from aiopppp.types import Channel
from aiopppp import session as S
from aiopppp.packets import DrwPkt, JsonCmdPkt, Packet, parse_packet as _orig_parse
from aiopppp.session import JsonSession, Session
import aiopppp.__main__ as M

log = logging.getLogger("probe")


def _patched_parse(data):
    if len(data) >= 2 and data[0] == CAM_MAGIC and data[1] == 0x43:
        return Packet(PacketType.P2pRdy, data[4:])
    return _orig_parse(data)


S.parse_packet = _patched_parse


class RawCmdPkt(DrwPkt):
    """Send an arbitrary command-channel payload (no aiopppp framing)."""
    def __init__(self, cmd_idx, raw):
        super().__init__(Channel.Command.value, cmd_idx, raw)

    def get_drw_payload(self):
        return self._payload


class ProbeSession(JsonSession):
    def _next_idx(self):
        i = self.outgoing_command_idx
        self.outgoing_command_idx += 1
        return i

    async def _send_json_framed(self, obj):
        pkt = JsonCmdPkt(self._next_idx(), obj)
        log.info("SEND framed  %s", obj)
        await self.send(pkt)

    async def _send_raw(self, raw, label):
        pkt = RawCmdPkt(self._next_idx(), raw)
        log.info("SEND raw     %-22s %s", label, raw[:60])
        await self.send(pkt)

    async def handle_drw(self, drw_pkt):
        # Ack + log every inbound DRW, whatever channel it's on.
        await Session.handle_drw(self, drw_pkt)
        p = drw_pkt.get_drw_payload()
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in p[:100])
        log.warning("INBOUND DRW  chn=%s idx=%s len=%d | %s | %s",
                    drw_pkt._channel, drw_pkt._cmd_idx, len(p), p[:60].hex(" "), ascii_)

    async def loop_step(self):
        await Session.loop_step(self)  # keepalive only; no video-stale disconnect

    async def setup_device(self):
        # Don't run the stock login (we know it gets ignored). Fire candidates.
        self.device_is_ready.set()
        log.info("connected; probing candidate commands...")

        framed = [
            {"cmd": "LoginDev", "pwd": "12345678"},
            {"cmd": "LoginDev", "user": "admin", "pwd": "12345678"},
            {"cmd": "CheckUser", "user": "admin", "pwd": "12345678"},
            {"cmd": "GetParms", "pwd": "12345678"},
            {"cmd": "StartStream", "pwd": "12345678"},
            {"cmd": "LiveStreamStart", "pwd": "12345678", "video": 1},
            {"cmd": "streamctrl", "pwd": "12345678", "video": 1},
        ]
        for obj in framed:
            await self._send_json_framed(obj)
            await asyncio.sleep(1.5)

        # Also try raw JSON with no preamble, in case LookCam doesn't use it.
        for obj in ({"cmd": "LoginDev", "pwd": "12345678"},
                    {"cmd": "StartStream", "pwd": "12345678"}):
            await self._send_raw(json.dumps(obj).encode(), "noprefix:" + obj["cmd"])
            await asyncio.sleep(1.5)

        log.info("probe batch sent; listening 8s more for any late replies...")
        await asyncio.sleep(8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--addr", required=True)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=logging.getLevelName(args.log_level.upper()))

    # Force our ProbeSession + is_json so the JSON transport path is used.
    def make(device, on_device_lost, login="", password=""):
        device.is_json = True
        return ProbeSession(device, on_disconnect=on_device_lost, login=login, password=password)

    M.make_session = make
    asyncio.run(M.amain(remote_addr=args.addr, local_port=0, username="admin", password="12345678"))


if __name__ == "__main__":
    main()
