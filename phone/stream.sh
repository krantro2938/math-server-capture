#!/data/data/com.termux/files/usr/bin/bash
# SUPERSEDED. The old MJPEG/cam-reverse bridge is gone — lookcam_stream.py pulls
# the camera's H.265 directly, and the phone runs the same pipeline as any other
# capture box. Use phone/termux-run.sh (the always-on supervisor); this wrapper
# just forwards to it.
exec bash "$(dirname "$0")/termux-run.sh" "$@"
