#!/bin/bash
# Entrypoint for the premium tier container.
# Starts Ollama (local LLM) and the VelaFlow API server.
# Detects GPU availability and selects an appropriate model.

set -e

# ── GPU Detection ─────────────────────────────────────────────────
if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
    echo "[VelaFlow Premium] GPU detected (${GPU_MEM:-?} MB VRAM)"

    if [ "${GPU_MEM:-0}" -ge 8000 ]; then
        DEFAULT_MODEL="llama3.2:3b"
    elif [ "${GPU_MEM:-0}" -ge 4000 ]; then
        DEFAULT_MODEL="phi3:3.8b"
    else
        DEFAULT_MODEL="qwen2:1.5b"
    fi
else
    echo "[VelaFlow Premium] No GPU detected — running on CPU"
    DEFAULT_MODEL="qwen2:1.5b"
fi

PREMIUM_LLM_MODEL="${PREMIUM_LLM_MODEL:-$DEFAULT_MODEL}"
echo "[VelaFlow Premium] Selected model: ${PREMIUM_LLM_MODEL}"

# ── Start Ollama ──────────────────────────────────────────────────
echo "[VelaFlow Premium] Starting Ollama local LLM server..."
ollama serve &
OLLAMA_PID=$!

# Wait for Ollama to be ready
for i in $(seq 1 30); do
    if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "[VelaFlow Premium] Ollama ready"
        break
    fi
    sleep 2
done

# Pull model if not already present
echo "[VelaFlow Premium] Pulling model: ${PREMIUM_LLM_MODEL}"
ollama pull "${PREMIUM_LLM_MODEL}" || true

# ── Start VelaFlow API ───────────────────────────────────────────
echo "[VelaFlow Premium] Starting VelaFlow API server..."
exec python -m uvicorn brain.api.app:create_app \
    --factory --host 0.0.0.0 --port 8000 \
    --workers 1 --loop uvloop --http httptools
