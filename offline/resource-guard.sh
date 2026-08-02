#!/usr/bin/env bash
# Resource monitoring functions for the offline solver.
# Sourced by start.sh — not run directly.
#
# Tuned for Dimensity 8300-Ultra: 4x A715 big + 4x A510 little,
# Mali-G615 MC6, 8 GB physical + 4 GB virtual (zRAM).

# ── memory ──────────────────────────────────────────────────────────────────

free_mb() {
  awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo
}

check_memory() {
  local free
  free=$(free_mb)
  if [ "$free" -lt "${MIN_FREE_MB:-1200}" ]; then
    echo "[resource] ${free}MB free — need ${MIN_FREE_MB:-1200}MB" >&2
    return 1
  fi
  return 0
}

wait_for_memory() {
  while ! check_memory; do
    echo "[resource] waiting for memory..." >&2
    sleep 5
  done
}

# ── thermal ─────────────────────────────────────────────────────────────────
# Thermal zone varies by SoC. On Dimensity 8300, zone 0 is usually the CPU
# cluster. We try several and pick the hottest.

_thermal_zones=""

detect_thermal_zones() {
  _thermal_zones=""
  for z in /sys/class/thermal/thermal_zone*/temp; do
    [ -r "$z" ] && _thermal_zones="$_thermal_zones $z"
  done
}

get_temp() {
  [ -z "$_thermal_zones" ] && detect_thermal_zones
  local max=0
  for z in $_thermal_zones; do
    local t
    t=$(cat "$z" 2>/dev/null) || continue
    # Some zones report in millidegrees, some in degrees.
    [ "$t" -gt 1000 ] && t=$((t / 1000))
    [ "$t" -gt "$max" ] && max=$t
  done
  echo "$max"
}

thermal_gate() {
  local temp pause="${THERMAL_PAUSE:-44}" resume="${THERMAL_RESUME:-39}"
  temp=$(get_temp)
  if [ "$temp" -ge "$pause" ]; then
    echo "[resource] SoC at ${temp}°C — pausing until ${resume}°C" >&2
    while true; do
      sleep 10
      temp=$(get_temp)
      [ "$temp" -lt "$resume" ] && break
    done
    echo "[resource] cooled to ${temp}°C — resuming" >&2
  fi
}

# ── CPU topology ────────────────────────────────────────────────────────────
# Find the big cores (highest max frequency) for taskset.

_big_cores=""

detect_big_cores() {
  local max_freq=0 core freq
  # First pass: find the highest frequency.
  for core in /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do
    [ -r "$core" ] || continue
    freq=$(cat "$core")
    [ "$freq" -gt "$max_freq" ] && max_freq=$freq
  done
  # Second pass: collect cores at that frequency.
  _big_cores=""
  for core in /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do
    [ -r "$core" ] || continue
    freq=$(cat "$core")
    if [ "$freq" -eq "$max_freq" ]; then
      local n
      n=$(echo "$core" | grep -o 'cpu[0-9]*' | grep -o '[0-9]*')
      [ -n "$_big_cores" ] && _big_cores="${_big_cores},"
      _big_cores="${_big_cores}${n}"
    fi
  done
  [ -z "$_big_cores" ] && _big_cores="0-3"
  echo "[resource] big cores: $_big_cores (max freq: ${max_freq}kHz)" >&2
}

big_cores() {
  [ -z "$_big_cores" ] && detect_big_cores
  echo "$_big_cores"
}

# ── process management ──────────────────────────────────────────────────────

# Start a llama-server with resource constraints.
# Usage: start_llama <model_path> <port> <context> <gpu_layers> <pid_var_name>
start_llama() {
  local model="$1" port="$2" ctx="$3" ngl="$4" pid_var="$5"
  local threads="${LLAMA_THREADS:-4}"
  local batch="${LLAMA_BATCH:-256}"
  local nice_level="${NICE_LEVEL:-10}"
  local server="${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"

  if [ ! -f "$model" ]; then
    echo "[resource] model not found: $model" >&2
    return 1
  fi

  if [ ! -x "$server" ]; then
    echo "[resource] llama-server not found: $server" >&2
    return 1
  fi

  wait_for_memory
  thermal_gate

  echo "[resource] loading model on :${port} (ctx=$ctx ngl=$ngl threads=$threads)" >&2

  local cores
  cores=$(big_cores)

  # taskset may not be available in all Termux installs.
  if command -v taskset >/dev/null 2>&1; then
    nice -n "$nice_level" taskset -c "$cores" \
      "$server" \
        -m "$model" \
        -t "$threads" \
        -ngl "$ngl" \
        -c "$ctx" \
        -b "$batch" \
        --mmap \
        --port "$port" \
        --log-disable \
        2>&1 | while IFS= read -r line; do echo "[llama:$port] $line"; done &
  else
    nice -n "$nice_level" \
      "$server" \
        -m "$model" \
        -t "$threads" \
        -ngl "$ngl" \
        -c "$ctx" \
        -b "$batch" \
        --mmap \
        --port "$port" \
        --log-disable \
        2>&1 | while IFS= read -r line; do echo "[llama:$port] $line"; done &
  fi

  local pid=$!
  eval "$pid_var=$pid"

  # Wait for the server to be ready (health endpoint).
  local attempts=0
  while [ $attempts -lt 60 ]; do
    if curl -sf "http://localhost:${port}/health" >/dev/null 2>&1; then
      echo "[resource] llama-server :${port} ready (pid $pid, ${attempts}s)" >&2
      return 0
    fi
    sleep 1
    attempts=$((attempts + 1))
    # Check the process is still alive.
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[resource] llama-server :${port} died during startup" >&2
      return 1
    fi
  done

  echo "[resource] llama-server :${port} failed to start in 60s" >&2
  kill "$pid" 2>/dev/null
  return 1
}

stop_llama() {
  local pid="$1" port="$2"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "[resource] stopping llama-server :${port} (pid $pid)" >&2
    kill "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 2
  fi
}
