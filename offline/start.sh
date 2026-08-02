#!/usr/bin/env bash
# Offline solver orchestrator.
#
# Manages the llama-server lifecycle and runs solver.py in a loop.
# Designed for Termux on the Poco X6 Pro (Dimensity 8300-Ultra, 8+4 GB).
#
# Usage:
#   bash offline/start.sh              # uses config.env defaults
#   bash offline/start.sh --once       # solve one run and exit
#   bash offline/start.sh --parallel   # keep both models loaded (12GB only)
#
# The solver model (Phi-4-mini-reasoning) is loaded on demand. In sequential
# mode (default for 8GB), it is loaded when a run is found and unloaded after.
# In parallel mode, it stays up and the solver polls continuously.

set -euo pipefail
cd "$(dirname "$0")"

# ── configuration ───────────────────────────────────────────────────────────

# Load defaults, then user overrides.
# shellcheck source=config.env
source config.env
[ -f config.local.env ] && source config.local.env

# CLI overrides.
ONCE=false
for arg in "$@"; do
  case "$arg" in
    --once)      ONCE=true ;;
    --parallel)  LOAD_MODE=parallel ;;
    --sequential) LOAD_MODE=sequential ;;
  esac
done

# shellcheck source=resource-guard.sh
source resource-guard.sh

# ── cleanup ─────────────────────────────────────────────────────────────────

SOLVER_PID=""

cleanup() {
  echo "[start] shutting down..." >&2
  stop_llama "$SOLVER_PID" "$SOLVER_PORT"
  exit 0
}

trap cleanup SIGTERM SIGINT EXIT

# ── health checks ──────────────────────────────────────────────────────────

wait_for_server() {
  local url="$1" name="$2" attempts=0
  echo "[start] waiting for $name at $url..." >&2
  while [ $attempts -lt 30 ]; do
    if curl -sf "${url}/health" >/dev/null 2>&1 || \
       curl -sf "${url}/solution/status" >/dev/null 2>&1; then
      echo "[start] $name is up" >&2
      return 0
    fi
    sleep 2
    attempts=$((attempts + 1))
  done
  echo "[start] $name not reachable at $url after 60s" >&2
  return 1
}

# ── the solve cycle ─────────────────────────────────────────────────────────

has_pending_run() {
  local status
  status=$(curl -sf "${EVENS_URL}/solution/status" 2>/dev/null) || return 1
  local state
  state=$(echo "$status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
  [ "$state" = "queued" ] || [ "$state" = "idle" ]
}

solve_one() {
  # In sequential mode, load the solver model, run, unload.
  if [ "$LOAD_MODE" = "sequential" ]; then
    echo "[start] loading solver model..." >&2
    thermal_gate
    if ! start_llama "$SOLVER_MODEL" "$SOLVER_PORT" "$SOLVER_CONTEXT" "$SOLVER_GPU_LAYERS" SOLVER_PID; then
      echo "[start] failed to load solver model" >&2
      return 1
    fi
  fi

  python3 solver.py \
    --server "$EVENS_URL" \
    --llama "http://localhost:${SOLVER_PORT}" \
    --token "$SOLVER_TOKEN" \
    --once \
    --sympy-timeout "$SYMPY_TIMEOUT" \
    --problem-timeout "$SOLVER_PROBLEM_TIMEOUT" \
    --total-timeout "$TOTAL_TIMEOUT"
  local rc=$?

  if [ "$LOAD_MODE" = "sequential" ]; then
    stop_llama "$SOLVER_PID" "$SOLVER_PORT"
    SOLVER_PID=""
  fi

  return $rc
}

# ── main ────────────────────────────────────────────────────────────────────

echo "[start] offline solver starting" >&2
echo "[start] mode: $LOAD_MODE | model: $SOLVER_MODEL" >&2
echo "[start] evens: $EVENS_URL | poll: ${POLL_INTERVAL}s" >&2
echo "[start] RAM: $(free_mb)MB free | temp: $(get_temp)°C" >&2

detect_big_cores
detect_thermal_zones

# In parallel mode, load the solver model once and leave it up.
if [ "$LOAD_MODE" = "parallel" ]; then
  echo "[start] parallel mode — loading solver model permanently" >&2
  if ! start_llama "$SOLVER_MODEL" "$SOLVER_PORT" "$SOLVER_CONTEXT" "$SOLVER_GPU_LAYERS" SOLVER_PID; then
    echo "[start] failed to load solver model — exiting" >&2
    exit 1
  fi
fi

if [ "$ONCE" = true ]; then
  # One-shot: solve a single run and exit.
  if ! wait_for_server "$EVENS_URL" "evens/server"; then
    echo "[start] evens/server not reachable — exiting" >&2
    exit 1
  fi
  solve_one
  exit $?
fi

# Poll loop: check for pending runs and solve them.
echo "[start] entering poll loop (every ${POLL_INTERVAL}s)" >&2

while true; do
  # Check the evens server is up before doing anything.
  if ! curl -sf "${EVENS_URL}/solution/status" >/dev/null 2>&1; then
    echo "[start] evens/server unreachable — waiting..." >&2
    sleep "$POLL_INTERVAL"
    continue
  fi

  # Resource check before attempting a solve.
  thermal_gate

  if has_pending_run; then
    echo "[start] pending run found — solving" >&2
    # Grace period depends on the mode setting.
    grace=$(python3 -c "
import sys; sys.path.insert(0, '$(dirname "$0")')
from solver import effective_grace
print(effective_grace('$EVENS_URL', $GRACE_PERIOD))
" 2>/dev/null || echo "$GRACE_PERIOD")
    [ "$grace" -gt 0 ] 2>/dev/null && sleep "$grace"

    # Re-check — the cloud solver may have claimed it during the grace period.
    if has_pending_run; then
      solve_one || echo "[start] solve failed" >&2
    else
      echo "[start] run claimed by someone else during grace period" >&2
    fi
  fi

  sleep "$POLL_INTERVAL"
done
