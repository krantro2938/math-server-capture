#!/usr/bin/env bash
# One-time Termux setup for the offline solver.
#
# Installs: Python + SymPy, llama.cpp with Vulkan, model download helpers.
# Run once while you have internet. Everything after this works offline.
#
# Usage:
#   pkg install git && git clone <repo> ~/utils
#   bash ~/utils/offline/setup-termux.sh

set -euo pipefail

echo "=== Offline solver setup for Termux ==="
echo ""

# ── system packages ─────────────────────────────────────────────────────────

echo "--- Installing system packages ---"
pkg update -y
pkg install -y \
  python \
  cmake \
  clang \
  make \
  git \
  curl \
  vulkan-headers \
  vulkan-loader-android

# ── Python dependencies ─────────────────────────────────────────────────────

echo ""
echo "--- Installing Python packages ---"
pip install --upgrade pip
pip install sympy mpmath requests

# Verify SymPy works.
python3 -c "
from sympy import *
x = Symbol('x')
result = solve(x**2 - 4, x)
assert result == [-2, 2], f'SymPy test failed: {result}'
print('SymPy OK: solve(x²-4) =', result)
"

# ── llama.cpp ───────────────────────────────────────────────────────────────

LLAMA_DIR="$HOME/llama.cpp"

echo ""
echo "--- Building llama.cpp with Vulkan ---"

if [ -d "$LLAMA_DIR" ]; then
  echo "llama.cpp already exists at $LLAMA_DIR — pulling latest"
  cd "$LLAMA_DIR"
  git pull --ff-only 2>/dev/null || echo "pull failed, using existing"
else
  git clone --depth 1 https://github.com/ggerganov/llama.cpp "$LLAMA_DIR"
  cd "$LLAMA_DIR"
fi

cmake -B build \
  -DGGML_VULKAN=ON \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build -j4 --target llama-server

if [ -x build/bin/llama-server ]; then
  echo "llama-server built successfully"
  build/bin/llama-server --version 2>/dev/null || true
else
  echo "ERROR: llama-server not found after build"
  echo "Check the build output above for errors."
  exit 1
fi

# ── hardware detection ──────────────────────────────────────────────────────

echo ""
echo "--- Hardware detection ---"

# CPU topology.
echo "CPU cores:"
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do
  [ -r "$cpu" ] || continue
  n=$(echo "$cpu" | grep -o 'cpu[0-9]*')
  freq=$(cat "$cpu")
  echo "  $n: $((freq / 1000)) MHz"
done

# GPU.
if vulkaninfo --summary 2>/dev/null | head -5; then
  echo "Vulkan: available"
else
  echo "Vulkan: not detected (GPU offload disabled, CPU-only inference)"
  echo "  Set OCR_GPU_LAYERS=0 and SOLVER_GPU_LAYERS=0 in config.local.env"
fi

# RAM.
total_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
free_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
echo "RAM: ${total_mb}MB total, ${free_mb}MB available"

# Thermal.
echo "Thermal zones:"
for z in /sys/class/thermal/thermal_zone*/type; do
  [ -r "$z" ] || continue
  dir=$(dirname "$z")
  type=$(cat "$z")
  temp=$(cat "$dir/temp" 2>/dev/null || echo "?")
  [ "$temp" != "?" ] && [ "$temp" -gt 1000 ] && temp=$((temp / 1000))
  echo "  $(basename "$dir"): $type = ${temp}°C"
done

# ── model directory ─────────────────────────────────────────────────────────

MODELS_DIR="$HOME/models"
mkdir -p "$MODELS_DIR"

echo ""
echo "--- Model directory: $MODELS_DIR ---"
echo ""
echo "Download your models while you have internet. You need two GGUF files:"
echo ""
echo "  1. OCR model (Qwen2.5-VL-3B Q4_K_M):"
echo "     Place at: $MODELS_DIR/qwen2.5-vl-3b-instruct-q4_k_m.gguf"
echo ""
echo "  2. Solver model (Phi-4-mini-reasoning Q4_K_M):"
echo "     Place at: $MODELS_DIR/phi-4-mini-reasoning-q4_k_m.gguf"
echo ""
echo "  Download from huggingface.co — search for the GGUF quantized versions."
echo "  Total: ~4 GB of storage."
echo ""

# ── config ──────────────────────────────────────────────────────────────────

CONFIG_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_LOCAL="$CONFIG_DIR/config.local.env"

if [ ! -f "$CONFIG_LOCAL" ]; then
  echo "--- Creating config.local.env ---"
  cp "$CONFIG_DIR/config.env" "$CONFIG_LOCAL"
  echo "Edit $CONFIG_LOCAL to set your SOLVER_TOKEN and verify model paths."
else
  echo "config.local.env already exists — not overwriting"
fi

# ── done ────────────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Download the two model files to $MODELS_DIR"
echo "  2. Edit $CONFIG_LOCAL (set SOLVER_TOKEN, verify paths)"
echo "  3. Test: bash offline/start.sh --once"
echo "  4. Run: bash offline/start.sh"
echo ""
echo "The solver polls evens/server for pending runs and solves them"
echo "using Phi-4-mini-reasoning + SymPy. No internet needed after setup."
