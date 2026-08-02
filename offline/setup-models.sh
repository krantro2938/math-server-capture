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

# ── qwen2.5:1.5b (from Ollama registry) ───────────────────────────────────

if ollama list 2>/dev/null | grep -q "qwen2.5:1.5b\|qwen2.5:latest"; then
  echo "[setup] qwen2.5:1.5b already registered" >&2
else
  echo "[setup] pulling qwen2.5:1.5b (~940MB)..." >&2
  ollama pull qwen2.5:1.5b
  echo "[setup] qwen2.5:1.5b ready" >&2
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
