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
  python-pillow \
  cmake \
  clang \
  make \
  git \
  curl

# Vulkan is an optimisation, not a requirement — llama.cpp runs CPU-only
# without it. Keep it in its own transaction: `vulkan-loader-android` pulls in
# `vulkan-icd`, which insists on a driver package (mesa swrast, freedreno,
# swiftshader) that some mirrors cannot resolve, and apt then aborts the whole
# install — taking cmake and python-pillow down with it.
VULKAN_OK=0
echo ""
echo "--- Installing Vulkan (optional) ---"
if pkg install -y vulkan-headers vulkan-loader-android 2>&1; then
  VULKAN_OK=1
else
  echo "Vulkan packages unavailable — continuing with a CPU-only build."
  echo "  (Retry later after 'termux-change-repo'; a lagging mirror is the"
  echo "   usual cause. Nothing else in the setup depends on this.)"
fi

# Trust the files, not the exit code: apt can report success while leaving the
# loader out, and a build configured for Vulkan then fails at link time.
if [ "$VULKAN_OK" = 1 ]; then
  prefix="${PREFIX:-/data/data/com.termux/files/usr}"
  if [ ! -f "$prefix/include/vulkan/vulkan.h" ] || [ ! -f "$prefix/lib/libvulkan.so" ]; then
    echo "Vulkan headers/loader missing despite install — falling back to CPU-only."
    VULKAN_OK=0
  fi
fi

# matplotlib is not in termux-main — it was dropped because it needs a full
# C/C++ + freetype build for every Python minor version. It lives in the
# Termux User Repository (tur-repo) instead, under a name that has changed
# between builds; if neither name resolves, build the wheel with pip.
if ! python3 -c "import matplotlib" 2>/dev/null; then
  echo ""
  echo "--- Installing matplotlib ---"
  pkg install -y tur-repo || true
  pkg install -y matplotlib || pkg install -y python-matplotlib || true
fi

if ! python3 -c "import matplotlib" 2>/dev/null; then
  echo "matplotlib not available from pkg — building it with pip (slow, ~10 min)"
  pkg install -y python-numpy freetype libpng pkg-config build-essential || true
  pip install matplotlib
fi

# ── Python dependencies ─────────────────────────────────────────────────────
#
# Pillow and matplotlib are preferred from `pkg` above (precompiled for
# Termux) rather than pip — building their C extensions on-device is slow and
# has a history of failing on Android (missing headers, denied setxattr
# calls). matplotlib is only used for its mathtext module (offline/render.py),
# which renders LaTeX-like math to PNG tiles without needing a browser or a
# LaTeX install.

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

# Verify the tile renderer (Pillow + matplotlib mathtext) works.
python3 -c "
import sys
sys.path.insert(0, '$(cd "$(dirname "$0")" && pwd)')
import render
assert render.RENDER_AVAILABLE, 'Pillow/matplotlib not importable'
pages = render.render_markdown_to_tiles('## 1\n\nSolve \$x^2=4\$: **Ответ: \$x=\\\\pm 2\$**')
assert pages and pages[0]['tiles'], 'render produced no tiles'
print('Renderer OK:', len(pages), 'page(s)')
"

# Verify the camera preview renderer (Pillow only, no matplotlib) works —
# GET /assignment/camera in solver.py, fed by lookcam/run/dual_capture.sh.
python3 -c "
import io, sys
sys.path.insert(0, '$(cd "$(dirname "$0")" && pwd)')
from PIL import Image
import camera_render as cr
assert cr.CAMERA_RENDER_AVAILABLE, 'Pillow not importable'
buf = io.BytesIO()
Image.new('RGB', (640, 480), (150, 150, 150)).save(buf, format='JPEG')
result = cr.render_camera_tiles(buf.getvalue(), size=4, mode='ink')
assert len(result['tiles']) == 4, f'expected 4 tiles, got {len(result[\"tiles\"])}'
print('Camera renderer OK:', len(result['tiles']), 'tiles, contrast=', result['contrast'])
"

# ── llama.cpp ───────────────────────────────────────────────────────────────

LLAMA_DIR="$HOME/llama.cpp"

echo ""
if [ "$VULKAN_OK" = 1 ]; then
  echo "--- Building llama.cpp with Vulkan ---"
else
  echo "--- Building llama.cpp (CPU-only, no Vulkan) ---"
fi

if [ -d "$LLAMA_DIR" ]; then
  echo "llama.cpp already exists at $LLAMA_DIR — pulling latest"
  cd "$LLAMA_DIR"
  git pull --ff-only 2>/dev/null || echo "pull failed, using existing"
else
  git clone --depth 1 https://github.com/ggerganov/llama.cpp "$LLAMA_DIR"
  cd "$LLAMA_DIR"
fi

if [ "$VULKAN_OK" = 1 ]; then
  cmake -B build -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release
else
  cmake -B build -DGGML_VULKAN=OFF -DCMAKE_BUILD_TYPE=Release
fi

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
if [ "$VULKAN_OK" != 1 ]; then
  echo "NOTE: llama.cpp was built without Vulkan — GPU offload is unavailable."
  echo "      Set OCR_GPU_LAYERS=0 and SOLVER_GPU_LAYERS=0 in config.local.env,"
  echo "      or the server will try to offload layers it cannot use."
  echo ""
fi
echo "Next steps:"
echo "  1. Download the two model files to $MODELS_DIR"
echo "  2. Edit $CONFIG_LOCAL (set SOLVER_TOKEN, verify paths)"
echo "  3. Test: bash offline/start.sh --once"
echo "  4. Run: bash offline/start.sh"
echo ""
echo "The solver polls evens/server for pending runs and solves them"
echo "using Phi-4-mini-reasoning + SymPy. No internet needed after setup."
