#!/usr/bin/env bash
# Measure the phone->VPS round-trip time, to size SRT_LATENCY in
# run/config.local.env (rule of thumb: 4x the median, floor ~300000us).
#
# ping is not usable here: the VPS drops ICMP echo, so it reports 100% packet
# loss on a link that is carrying video perfectly well. A TCP handshake to a
# port that IS open costs one round trip, which is what we actually want.
#
#   bash phone/rtt.sh                 # host + port from run/config*.env
#   bash phone/rtt.sh 1.2.3.4 443     # or name them
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="${1:-}"
if [ -z "$HOST" ]; then
  CONF="run/config.env"; [ -f run/config.local.env ] && CONF="run/config.local.env"
  # shellcheck source=../run/config.env
  [ -f "$CONF" ] && . "$CONF"
  HOST="${VPS_HOST:-}"
fi
[ -n "$HOST" ] || { echo "[!] no host — pass one, or set VPS_HOST in run/config.local.env"; exit 1; }
# 443 (Caddy) is the reliable choice: the SRT port is UDP, so it cannot be
# handshaked, and 1935/RTMP is normally left closed.
PORT="${2:-443}"

PYTHON="./.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python"

"$PYTHON" - "$HOST" "$PORT" <<'PY'
import socket, statistics, sys, time

host, port = sys.argv[1], int(sys.argv[2])
samples, failed = [], 0
for _ in range(15):
    s = socket.socket()
    s.settimeout(3)
    t0 = time.perf_counter()
    try:
        s.connect((host, port))
        samples.append((time.perf_counter() - t0) * 1000)
    except Exception:
        failed += 1
    finally:
        s.close()
    time.sleep(0.2)

if not samples:
    print(f"[!] no connection to {host}:{port} — wrong host, or the port is closed?")
    raise SystemExit(1)

samples.sort()
med = statistics.median(samples)
print(f"{host}:{port}  n={len(samples)} failed={failed}")
print(f"  min {samples[0]:.0f} ms | median {med:.0f} ms | max {samples[-1]:.0f} ms")

# Everything above is in MILLISECONDS; SRT_LATENCY is in MICROseconds. Convert
# once, explicitly, and do the rounding in the target unit -- mixing the two is
# an easy mistake that silently pins every suggestion to the floor.
# Jitter matters as much as the median on mobile data: SRT has to cover the late
# tail, not the typical case, so size off the worst sample as well as the median.
need_ms = max(med * 4, samples[-1] * 2)
suggested_us = int(round(need_ms * 1000 / 50000.0)) * 50000   # nearest 50ms
suggested_us = max(300000, suggested_us)                      # never below 300ms
print(f"\n  SRT_LATENCY=\"{suggested_us}\"   # {suggested_us // 1000} ms"
      f" -- in run/config.local.env, then restart capture")
if samples[-1] > med * 3:
    print("  (high jitter — if grey frames persist, go one step higher)")
PY
