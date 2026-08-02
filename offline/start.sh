#!/usr/bin/env bash
# Offline solver: starts Ollama and the local HTTP server with auto-restart.
#
# Usage:
#   bash offline/start.sh
#   bash offline/start.sh --no-ollama   # Ollama already running externally
#   bash offline/start.sh --force       # take over from a copy already running
#
# Both processes restart automatically on crash. Ctrl-C stops everything.
# For auto-start on boot, install termux-boot and run:
#   bash offline/start.sh --install-boot

set -euo pipefail
cd "$(dirname "$0")"

OLLAMA_URL="http://localhost:11434"
SERVE_PORT=8384
MATH_MODEL="hf.co/bartowski/Qwen2.5-Math-1.5B-Instruct-GGUF:Q4_K_M"
VISION_MODEL="qwen2.5vl:3b"
RESTART_DELAY=3
# Where ../lookcam/run/dual_capture.sh writes its two rolling JPEGs — same
# default as SNAPSHOT_DIR in lookcam/run/config.env. Missing files are fine
# (GET /assignment/camera just 503s until dual_capture.sh is running).
SNAPSHOT_DIR="$HOME/lookcam-snapshots"

# Allow overrides from config
[ -f config.local.env ] && source config.local.env

MANAGE_OLLAMA=true
OLLAMA_PID=""
SOLVER_PID=""
FORCE=0
PIDFILE="${PIDFILE:-$HOME/.offline-solver.pid}"

# ── boot installer ─────────────────────────────────────────────────────────

install_boot() {
  pkg install -y termux-boot 2>/dev/null || true
  local boot_dir="$HOME/.termux/boot"
  mkdir -p "$boot_dir"
  local script="$boot_dir/start-solver.sh"
  local here
  here="$(pwd)"
  cat > "$script" <<BOOT
#!/usr/bin/env bash
termux-wake-lock
cd "$here"
exec bash start.sh >> "\$HOME/solver.log" 2>&1
BOOT
  chmod +x "$script"
  echo "[start] boot script installed at $script" >&2
  echo "[start] open Termux:Boot once to activate, then it runs on every boot" >&2
}

for arg in "$@"; do
  case "$arg" in
    --no-ollama)    MANAGE_OLLAMA=false ;;
    --install-boot) install_boot; exit 0 ;;
    --force)        FORCE=1 ;;
  esac
done

# ---- single-instance guard --------------------------------------------------
# Two copies here means two Ollama servers and two solver.py --serve both
# trying to bind :8384 — the second one just fails to start, confusingly, and
# whichever answers first wins for reasons that have nothing to do with which
# one you meant to keep. Refuse outright instead, the way termux-run.sh does
# for the camera stream.
source "$(cd "$(dirname "$0")/.." && pwd)/scripts/singleton-guard.sh"
singleton_guard "offline solver" "$PIDFILE" "start.sh" \
  "offline/start\.sh" "solver\.py --serve"
echo $$ > "$PIDFILE"

# ── cleanup ────────────────────────────────────────────────────────────────

STOPPING=false

cleanup() {
  STOPPING=true
  echo "[start] shutting down..." >&2
  # Kill all child processes
  kill 0 2>/dev/null
  wait 2>/dev/null
  [ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"
  exit 0
}

trap cleanup SIGTERM SIGINT

# ── Ollama ─────────────────────────────────────────────────────────────────

start_ollama() {
  if [ "$MANAGE_OLLAMA" = false ]; then return; fi

  # If Ollama is already running, don't start another one
  if curl -sf "$OLLAMA_URL" >/dev/null 2>&1; then
    echo "[start] Ollama already running" >&2
    return
  fi

  while true; do
    echo "[start] starting Ollama..." >&2
    ollama serve 2>&1 | sed 's/^/[ollama] /' &
    OLLAMA_PID=$!

    # Wait for it to be ready
    for i in $(seq 1 30); do
      if curl -sf "$OLLAMA_URL" >/dev/null 2>&1; then
        echo "[start] Ollama is up" >&2
        break
      fi
      sleep 1
    done

    wait "$OLLAMA_PID" 2>/dev/null || true
    OLLAMA_PID=""
    [ "$STOPPING" = true ] && return
    echo "[start] Ollama exited — restarting in ${RESTART_DELAY}s..." >&2
    sleep "$RESTART_DELAY"
  done
}

# ── solver server ──────────────────────────────────────────────────────────

start_solver() {
  # Wait for Ollama to be reachable before starting the solver
  echo "[start] waiting for Ollama..." >&2
  while ! curl -sf "$OLLAMA_URL" >/dev/null 2>&1; do
    sleep 1
  done

  while true; do
    echo "[start] starting solver on :${SERVE_PORT}..." >&2
    python3 solver.py --serve \
      --llama "$OLLAMA_URL" \
      --ollama-model "$MATH_MODEL" \
      --vision-model "$VISION_MODEL" \
      --serve-port "$SERVE_PORT" \
      --camera-primary "$SNAPSHOT_DIR/primary.jpg" \
      --camera-fallback "$SNAPSHOT_DIR/fallback.jpg" &
    SOLVER_PID=$!

    wait "$SOLVER_PID" 2>/dev/null || true
    SOLVER_PID=""
    [ "$STOPPING" = true ] && return
    echo "[start] solver exited — restarting in ${RESTART_DELAY}s..." >&2
    sleep "$RESTART_DELAY"
  done
}

# ── model check ────────────────────────────────────────────────────────────

check_models() {
  # Wait for Ollama to be up
  for i in $(seq 1 15); do
    curl -sf "$OLLAMA_URL" >/dev/null 2>&1 && break
    sleep 1
  done

  local missing=false
  if ! ollama list 2>/dev/null | grep -q "Qwen2.5-Math-1.5B-Instruct"; then
    echo "[start] $MATH_MODEL not found" >&2
    missing=true
  fi
  if ! ollama list 2>/dev/null | grep -q "qwen2.5vl"; then
    echo "[start] qwen2.5vl:3b not found" >&2
    missing=true
  fi

  if [ "$missing" = true ]; then
    echo "[start] models missing — run: bash offline/setup-models.sh" >&2
    exit 1
  fi
}

# ── main ───────────────────────────────────────────────────────────────────

echo "[start] offline solver stack starting" >&2
echo "[start] math: $MATH_MODEL | vision: $VISION_MODEL" >&2
echo "[start] serve: :$SERVE_PORT | ollama: $OLLAMA_URL" >&2

start_ollama &

# Give Ollama a moment, then verify models exist before starting the solver
if [ "$MANAGE_OLLAMA" = true ]; then
  check_models
fi

start_solver &

# Wait for either to exit (they shouldn't — the loops restart them)
wait
