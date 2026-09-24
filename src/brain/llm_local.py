"""Local LLM client — Ollama integration for premium tier.

Provides privacy-first, API-limit-free LLM inference via a locally
hosted Ollama instance. Designed for the premium tier where tenant
data never leaves the infrastructure.

GPU/CPU Detection:
    - Checks for NVIDIA GPU via nvidia-smi
    - Falls back to CPU-only inference if no GPU is present
    - Model selection adapts to available resources

Default Model: qwen2:1.5b (934 MB)
    - Fits comfortably in 4 GB RAM allocation
    - Good multilingual quality for its size
    - Easy to scale up: swap to phi3:3.8b, llama3.2:3b, or larger
      when infrastructure allows

Self-hosted on Ollama in the LXC (no managed model-serving dependency).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "qwen2:1.5b"
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
# Hardware profile cache written by the installer (scripts/installer.py)
# at install time. src/ never invokes nvidia-smi directly; it only reads
# the cached profile. This keeps subprocess usage out of the product
# runtime entirely.
_HARDWARE_CACHE_ENV = "VELAFLOW_HARDWARE_JSON"
_HARDWARE_CACHE_DEFAULT = "data/hardware.json"


@dataclass(frozen=True)
class HardwareProfile:
    """Detected hardware capabilities for LLM inference."""

    has_gpu: bool
    gpu_name: str
    gpu_memory_mb: int
    recommended_model: str

    @property
    def device_label(self) -> str:
        if self.has_gpu:
            return f"GPU ({self.gpu_name}, {self.gpu_memory_mb} MB)"
        return "CPU-only"


def detect_hardware() -> HardwareProfile:
    """Return hardware profile from the installer-written cache.

    The installer (``scripts/installer.py``) probes ``nvidia-smi`` at
    install time and writes the result to ``data/hardware.json``. The
    product runtime never shells out — it reads the cache, or falls
    back to CPU-only if the cache is missing. This keeps subprocess
    usage entirely out of ``src/``.

    Callers can override the cache location via the
    ``VELAFLOW_HARDWARE_JSON`` environment variable.
    """
    cache_path = Path(os.environ.get(_HARDWARE_CACHE_ENV, _HARDWARE_CACHE_DEFAULT))
    cpu_profile = HardwareProfile(
        has_gpu=False,
        gpu_name="none",
        gpu_memory_mb=0,
        recommended_model=_DEFAULT_MODEL,
    )

    if not cache_path.exists():
        # Best-effort hint for operators: if nvidia-smi exists on PATH
        # but no cache was ever written, re-run the installer detect
        # step. We do NOT shell out from the product runtime.
        if shutil.which("nvidia-smi") is not None:
            logger.info(
                "nvidia-smi present but %s missing; re-run installer "
                "--detect-hardware to populate the cache",
                cache_path,
            )
        else:
            logger.info("no hardware cache at %s — CPU-only mode", cache_path)
        return cpu_profile

    try:
        raw = cache_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
    except (OSError, ValueError) as exc:
        logger.warning("hardware cache %s unreadable (%s) — CPU-only", cache_path, exc)
        return cpu_profile

    has_gpu = bool(data.get("has_gpu", False))
    if not has_gpu:
        return cpu_profile

    gpu_name = str(data.get("gpu_name", "unknown"))
    try:
        gpu_mem = int(data.get("gpu_memory_mb", 0))
    except (TypeError, ValueError):
        gpu_mem = 0

    if gpu_mem >= 8000:
        model = "llama3.2:3b"
    elif gpu_mem >= 4000:
        model = "phi3:3.8b"
    else:
        model = _DEFAULT_MODEL

    logger.info(
        "GPU from cache: %s (%d MB) — recommending %s", gpu_name, gpu_mem, model
    )
    return HardwareProfile(
        has_gpu=True,
        gpu_name=gpu_name,
        gpu_memory_mb=gpu_mem,
        recommended_model=model,
    )


class LocalLLMClient:
    """Ollama-based local LLM client for premium-tier tenants.

    Usage:
        client = LocalLLMClient()
        if client.is_available():
            result = client.generate("Polish this digest: ...")
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("PREMIUM_LLM_URL", _DEFAULT_OLLAMA_URL)
        ).rstrip("/")
        # Security: only allow localhost HTTP or any HTTPS endpoint
        from urllib.parse import urlparse
        parsed = urlparse(self._base_url)
        _is_local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme == "http" and not _is_local:
            raise ValueError(
                f"Plain HTTP to non-local LLM endpoint is forbidden: {self._base_url}. "
                "Use HTTPS or connect to localhost only."
            )
        self._model = (
            model
            or os.environ.get("PREMIUM_LLM_MODEL", _DEFAULT_MODEL)
        )
        self._hardware = detect_hardware()
        self._timeout = 120  # LLM generation can be slow on CPU

    @property
    def hardware(self) -> HardwareProfile:
        return self._hardware

    @property
    def model(self) -> str:
        return self._model

    def is_available(self) -> bool:
        """Check if the Ollama server is reachable and the model is loaded."""
        try:
            resp = requests.get(
                f"{self._base_url}/api/tags",
                timeout=5,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            # Check if our model (or a variant) is available
            return any(self._model.split(":")[0] in m for m in models)
        except (requests.RequestException, ValueError):
            return False

    def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
    ) -> str | None:
        """Generate text using the local LLM.

        Returns None if the Ollama server is unreachable or errors.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": 2048,
            },
        }
        if system:
            payload["system"] = system

        try:
            resp = requests.post(
                f"{self._base_url}/api/generate",
                json=payload,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
        except requests.RequestException as exc:
            logger.error("Local LLM generation failed: %s", exc)
            return None

    def pull_model(self) -> bool:
        """Pull the configured model if not already present."""
        try:
            resp = requests.post(
                f"{self._base_url}/api/pull",
                json={"name": self._model, "stream": False},
                timeout=600,
            )
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.error("Model pull failed: %s", exc)
            return False

    def chat(
        self,
        user_text: str,
        system_prompt: str = "",
        temperature: float = 0.4,
    ) -> str | None:
        """Chat-compatible generation (OpenAI-style messages).

        Used by the worker for per-tenant LLM calls that require
        system + user message format.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 2500,
            },
        }
        try:
            resp = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                logger.warning("Local LLM chat returned %d", resp.status_code)
                return None
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            if content:
                logger.info(
                    "Local LLM chat response from %s (%s, %d eval tokens)",
                    self._model,
                    self._hardware.device_label,
                    data.get("eval_count", 0),
                )
            return content or None
        except requests.RequestException as exc:
            logger.error("Local LLM chat failed: %s", exc)
            return None

    def embed(self, text: str) -> list[float] | None:
        """Generate embeddings for RAG using the local Ollama model.

        Falls back to None if Ollama is unavailable — caller should
        use SimpleEmbedder as fallback.
        """
        try:
            resp = requests.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self._model, "prompt": text},
                timeout=30,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data.get("embedding")
        except requests.RequestException:
            logger.exception("Ollama embedding failed")
            return None
