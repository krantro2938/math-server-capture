#!/usr/bin/env bash
# Download and register the Ollama models needed by the offline solver.
#
# Run once on a new device:
#   ollama serve &
#   bash offline/setup-models.sh
#
# Downloads:
#   - qwen2.5-math:1.5b  (~940MB GGUF from HuggingFace, not in Ollama registry)
#   - qwen2.5vl:3b       (~3.2GB, pulled from Ollama registry)

set -euo pipefail
cd "$(dirname "$0")"

OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

echo "[setup] checking Ollama at $OLLAMA_URL..." >&2
if ! curl -sf "$OLLAMA_URL" >/dev/null 2>&1; then
  echo "[setup] Ollama not running. Start it first: ollama serve &" >&2
  exit 1
fi

# ── qwen2.5-math:1.5b (manual GGUF) ──────────────────────────────────────

if ollama list 2>/dev/null | grep -q "qwen2.5-math"; then
  echo "[setup] qwen2.5-math:1.5b already registered" >&2
else
  GGUF_URL="https://huggingface.co/bartowski/Qwen2.5-Math-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-Math-1.5B-Instruct-Q4_K_M.gguf"
  GGUF_FILE="/tmp/qwen2.5-math-1.5b-instruct-q4_k_m.gguf"
  MODELFILE="/tmp/Modelfile.qwen25math"

  if [ ! -f "$GGUF_FILE" ]; then
    echo "[setup] downloading Qwen2.5-Math-1.5B GGUF (~940MB)..." >&2
    curl -L --progress-bar -o "$GGUF_FILE" "$GGUF_URL"
  else
    echo "[setup] GGUF already downloaded at $GGUF_FILE" >&2
  fi

  cat > "$MODELFILE" <<'EOF'
FROM /tmp/qwen2.5-math-1.5b-instruct-q4_k_m.gguf
TEMPLATE """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}<|im_start|>user
{{ .Prompt }}<|im_end|>
<|im_start|>assistant
"""
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|endoftext|>"
EOF

  echo "[setup] registering qwen2.5-math:1.5b with Ollama..." >&2
  ollama create qwen2.5-math:1.5b -f "$MODELFILE"
  echo "[setup] qwen2.5-math:1.5b ready" >&2
fi

# ── qwen2.5vl:3b (from Ollama registry) ──────────────────────────────────

if ollama list 2>/dev/null | grep -q "qwen2.5vl"; then
  echo "[setup] qwen2.5vl:3b already registered" >&2
else
  echo "[setup] pulling qwen2.5vl:3b (~3.2GB)..." >&2
  ollama pull qwen2.5vl:3b
  echo "[setup] qwen2.5vl:3b ready" >&2
fi

echo "[setup] all models ready. Run: bash offline/start.sh" >&2
