"""Tests for the LLM module fallback logic."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from brain.config import Settings
from brain.llm import polish_digest

# Test-only placeholder values â€” NOT real secrets.
_TEST_GROQ_KEY = os.environ.get("GROQ_API_KEY", "test")
_TEST_GOOGLE_KEY = os.environ.get("GOOGLE_AI_API_KEY", "test")


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        todoist_api_token="test",
        smtp_host="smtp.test.com",
        smtp_port=587,
        smtp_username="test@test.com",
        smtp_password="test",
        digest_from_email="test@test.com",
        digest_to_email="test@test.com",
        groq_api_key=_TEST_GROQ_KEY,
        google_ai_api_key=_TEST_GOOGLE_KEY,
    )


class TestPolishDigest:
    def test_returns_raw_when_no_keys(self) -> None:
        s = Settings(
            todoist_api_token="test",
            smtp_host="smtp.test.com",
            smtp_port=587,
            smtp_username="t@t.com",
            smtp_password="t",
            digest_from_email="t@t.com",
            digest_to_email="t@t.com",
            groq_api_key="",
            google_ai_api_key="",
        )
        result = polish_digest(s, "raw text", "prompt")
        assert result == "raw text"

    @patch("brain.llm._call_groq")
    def test_groq_success(self, mock_groq: MagicMock, settings: Settings) -> None:
        mock_groq.return_value = "polished by groq"
        result = polish_digest(settings, "raw text", "prompt")
        assert result == "polished by groq"
        mock_groq.assert_called_once()

    @patch("brain.llm._call_google_ai")
    @patch("brain.llm._call_groq")
    def test_google_fails_falls_back_to_groq(
        self, mock_groq: MagicMock, mock_google: MagicMock, settings: Settings
    ) -> None:
        mock_google.return_value = None  # Google AI failed
        mock_groq.return_value = "polished by groq"
        result = polish_digest(settings, "raw text", "prompt")
        assert result == "polished by groq"
        mock_google.assert_called()
        mock_groq.assert_called_once()

    @patch("brain.llm._call_google_ai")
    @patch("brain.llm._call_groq")
    def test_both_fail_returns_raw(
        self, mock_groq: MagicMock, mock_google: MagicMock, settings: Settings
    ) -> None:
        mock_groq.return_value = None
        mock_google.return_value = None
        result = polish_digest(settings, "raw text", "prompt")
        assert result == "raw text"
