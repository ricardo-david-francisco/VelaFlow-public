"""Tests for the local LLM client (premium tier)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock

from brain.llm_local import (
    detect_hardware,
    HardwareProfile,
    LocalLLMClient,
    _DEFAULT_MODEL,
)


def _write_cache(tmp_path: Path, payload: dict) -> Path:
    cache = tmp_path / "hardware.json"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    return cache


class TestHardwareDetection:
    """Verify GPU/CPU detection via the installer-written cache.

    The product runtime no longer shells out to ``nvidia-smi``. The
    installer writes ``data/hardware.json`` at install time and
    ``detect_hardware()`` reads it, or falls back to CPU-only when the
    cache is absent.
    """

    def test_no_cache_and_no_nvidia_smi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(tmp_path / "missing.json"))
        with patch("brain.llm_local.shutil.which", return_value=None):
            profile = detect_hardware()
        assert profile.has_gpu is False
        assert profile.gpu_name == "none"
        assert profile.recommended_model == _DEFAULT_MODEL
        assert "CPU" in profile.device_label

    def test_cache_gpu_large(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_cache(
            tmp_path,
            {"has_gpu": True, "gpu_name": "NVIDIA RTX 3080", "gpu_memory_mb": 10240},
        )
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(cache))
        profile = detect_hardware()
        assert profile.has_gpu is True
        assert "3080" in profile.gpu_name
        assert profile.gpu_memory_mb == 10240
        assert profile.recommended_model == "llama3.2:3b"

    def test_cache_gpu_medium(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_cache(
            tmp_path,
            {"has_gpu": True, "gpu_name": "NVIDIA GTX 1650", "gpu_memory_mb": 4096},
        )
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(cache))
        profile = detect_hardware()
        assert profile.has_gpu is True
        assert profile.recommended_model == "phi3:3.8b"

    def test_cache_gpu_small(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_cache(
            tmp_path,
            {"has_gpu": True, "gpu_name": "NVIDIA MX150", "gpu_memory_mb": 2048},
        )
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(cache))
        profile = detect_hardware()
        assert profile.has_gpu is True
        assert profile.recommended_model == _DEFAULT_MODEL

    def test_cache_marks_no_gpu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _write_cache(tmp_path, {"has_gpu": False})
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(cache))
        profile = detect_hardware()
        assert profile.has_gpu is False

    def test_cache_unreadable_falls_back_to_cpu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = tmp_path / "hardware.json"
        cache.write_text("{ not valid json", encoding="utf-8")
        monkeypatch.setenv("VELAFLOW_HARDWARE_JSON", str(cache))
        profile = detect_hardware()
        assert profile.has_gpu is False


class TestHardwareProfile:
    """Verify the HardwareProfile dataclass."""

    def test_gpu_label(self) -> None:
        p = HardwareProfile(
            has_gpu=True,
            gpu_name="RTX 3080",
            gpu_memory_mb=10240,
            recommended_model="llama3.2:3b",
        )
        assert "GPU" in p.device_label
        assert "RTX 3080" in p.device_label

    def test_cpu_label(self) -> None:
        p = HardwareProfile(
            has_gpu=False,
            gpu_name="none",
            gpu_memory_mb=0,
            recommended_model=_DEFAULT_MODEL,
        )
        assert "CPU" in p.device_label


class TestLocalLLMClient:
    """Verify the Ollama client wrapper."""

    def test_default_model(self) -> None:
        with patch("brain.llm_local.detect_hardware") as mock_hw:
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
        assert client.model == _DEFAULT_MODEL

    def test_custom_model(self) -> None:
        with patch("brain.llm_local.detect_hardware") as mock_hw:
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient(model="phi3:3.8b")
        assert client.model == "phi3:3.8b"

    def test_is_available_false_when_unreachable(self) -> None:
        import requests

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.get", side_effect=requests.ConnectionError),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            assert client.is_available() is False

    def test_generate_returns_none_on_error(self) -> None:
        import requests

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", side_effect=requests.ConnectionError),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.generate("test prompt")
            assert result is None

    def test_generate_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"response": "Generated text"}
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", return_value=mock_resp),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.generate("test prompt", system="You are helpful")
            assert result == "Generated text"

    def test_chat_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "message": {"content": "Chat response"},
            "eval_count": 42,
        }
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", return_value=mock_resp),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.chat("Hello", "You are a helpful assistant")
            assert result == "Chat response"

    def test_chat_returns_none_on_error(self) -> None:
        import requests

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", side_effect=requests.ConnectionError),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.chat("test", "system")
            assert result is None

    def test_embed_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"embedding": [0.1, 0.2, 0.3]}
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", return_value=mock_resp),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.embed("test text")
            assert result == [0.1, 0.2, 0.3]

    def test_embed_returns_none_on_error(self) -> None:
        import requests

        with (
            patch("brain.llm_local.detect_hardware") as mock_hw,
            patch("brain.llm_local.requests.post", side_effect=requests.ConnectionError),
        ):
            mock_hw.return_value = HardwareProfile(
                has_gpu=False, gpu_name="none",
                gpu_memory_mb=0, recommended_model=_DEFAULT_MODEL,
            )
            client = LocalLLMClient()
            result = client.embed("test")
            assert result is None
