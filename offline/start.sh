#!/usr/bin/env bash
# Offline solver: starts Ollama and the local HTTP server with auto-restart.
#
# Usage:
#   bash offline/start.sh
#   bash offline/start.sh --no-ollama   # Ollama already running externally
#
# Both processes restart automatically on crash. Ctrl-C stops everything.
# For auto-start on boot, install termux-boot and run:
#   bash offline/start.sh --install-boot

set -euo pipefail
cd "$(dirname "$0")"

OLLAMA_URL="http://localhost:11434"
SERVE_PORT=8384
MATH_MODEL="qwen2.5-math:1.5b"
VISION_MODEL="qwen2.5vl:3b"
RESTART_DELAY=3

# Allow overrides from config
[ -f config.local.env ] && source config.local.env

MANAGE_OLLAMA=true
OLLAMA_PID=""
SOLVER_PID=""

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
  esac
done

# ── cleanup ────────────────────────────────────────────────────────────────

cleanup() {
  echo "[start] shutting down..." >&2
  [ -n "$SOLVER_PID" ] && kill "$SOLVER_PID" 2>/dev/null && wait "$SOLVER_PID" 2>/dev/null
  [ -n "$OLLAMA_PID" ] && kill "$OLLAMA_PID" 2>/dev/null && wait "$OLLAMA_PID" 2>/dev/null
  exit 0
}

trap cleanup SIGTERM SIGINT EXIT

# ── Ollama ─────────────────────────────────────────────────────────────────

start_ollama() {
  if [ "$MANAGE_OLLAMA" = false ]; then return; fi

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
      --serve-port "$SERVE_PORT" &
    SOLVER_PID=$!

    wait "$SOLVER_PID" 2>/dev/null || true
    SOLVER_PID=""
    echo "[start] solver exited — restarting in ${RESTART_DELAY}s..." >&2
    sleep "$RESTART_DELAY"
  done
}

# ── main ───────────────────────────────────────────────────────────────────

echo "[start] offline solver stack starting" >&2
echo "[start] math: $MATH_MODEL | vision: $VISION_MODEL" >&2
echo "[start] serve: :$SERVE_PORT | ollama: $OLLAMA_URL" >&2

start_ollama &
start_solver &

# Wait for either to exit (they shouldn't — the loops restart them)
wait
