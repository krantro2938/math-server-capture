#!/data/data/com.termux/files/usr/bin/bash
# What can this phone actually see of its own network?
#
# Android blocks netlink (RTM_GETLINK) for unprivileged apps, so in Termux the
# obvious tools lie by omission: `ip addr` exits 0 having printed nothing. This
# tries every enumeration route in turn and shows which ones answer, so we can
# tell "no hotspot interface" apart from "can't see the hotspot interface".
#
#   bash phone/netdiag.sh
#
# What we're looking for: an interface whose subnet contains the camera. The AP
# is usually ap0 / wlan1 / swlan0 / softap0, NOT wlan0 (that's the client radio,
# and rmnet* is mobile data).
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON="python"

hr() { echo; echo "=== $* ==="; }

hr "ip -o -4 addr show   (expect EMPTY on Android — that's the bug, not an error)"
ip -o -4 addr show 2>&1 | sed 's/^/  /' || echo "  <ip not installed>"
echo "  [exit=$?]"

hr "ifconfig -a"
ifconfig -a 2>&1 | grep -E "^[a-z]|inet " | sed 's/^/  /' || echo "  <no ifconfig>"

hr "/system/bin/ifconfig  (Android's own toybox — often works when Termux's doesn't)"
/system/bin/ifconfig -a 2>&1 | grep -E "^[a-z]|inet " | sed 's/^/  /' || echo "  <unavailable>"

hr "ls /sys/class/net"
ls /sys/class/net 2>&1 | sed 's/^/  /' || echo "  <unreadable>"

hr "SIOCGIFCONF ioctl via python  (THE ONE THAT MATTERS — netlink-free)"
"$PYTHON" -c "
import sys; sys.path.insert(0, '$REPO')
import discover
for n, ip, brd, plen in discover._iface_from_ioctl():
    print(f'  {n:12} {ip}/{plen}  brd {brd}')
" 2>&1 | sed 's/^/  /'

hr "what discover.py will actually probe"
"$PYTHON" -c "
import sys; sys.path.insert(0, '$REPO')
import discover
ifs = discover.interfaces()
print('  interfaces:', ifs or 'NONE')
print('  sweep size:', len(discover.sweep_hosts(ifs)), 'hosts')
" 2>&1 | sed 's/^/  /'

hr "default route source address (this is mobile data if WiFi isn't default)"
"$PYTHON" -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80))
print('  ', s.getsockname()[0])
"

hr "/proc/net/arp  (neighbours we've talked to — the camera may already be here)"
cat /proc/net/arp 2>&1 | sed 's/^/  /' || echo "  <restricted>"

hr "verdict"
echo "  If the ioctl block above shows NO interface on the camera's subnet, this"
echo "  phone is not on the camera's network at all — check the hotspot is ON and"
echo "  the camera has joined it. If it DOES show one, discovery will now find it."
