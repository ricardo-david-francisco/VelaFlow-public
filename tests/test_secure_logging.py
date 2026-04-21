"""Tests for secure logging system — redaction, rotation, HMAC chain, export."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from brain.security.secure_logging import (
    SecureLogger,
    redact,
    setup_logging,
)


class TestRedaction:
    """Verify that secrets and PII are stripped from log output."""

    def test_redact_api_token(self):
        text = "TODOIST_API_TOKEN=abc123secret456"
        result = redact(text)
        assert "abc123secret456" not in result
        assert "REDACTED" in result

    def test_redact_bearer_token(self):
        text = "Authorization: Bearer sk-proj-very-long-token-here"
        result = redact(text)
        assert "sk-proj-very-long-token-here" not in result
        assert "REDACTED" in result

    def test_redact_email(self):
        text = "User logged in: john.doe@example.com"
        result = redact(text)
        assert "john.doe@example.com" not in result
        assert "EMAIL" in result

    def test_redact_phone(self):
        text = "SMS sent to +351912345678"
        result = redact(text)
        assert "912345678" not in result
        assert "PHONE" in result

    def test_redact_ip_address(self):
        text = "Request from 192.168.1.100"
        result = redact(text)
        assert "192.168.1.100" not in result
        assert "IP" in result

    def test_redact_jwt_secret(self):
        text = "JWT_SECRET=my_super_secret_key_here"
        result = redact(text)
        assert "my_super_secret_key_here" not in result

    def test_redact_multiple_patterns(self):
        text = (
            "GOOGLE_AI_API_KEY=AIzaSyD_FAKE_KEY "
            "user@example.com called from 10.0.0.1"
        )
        result = redact(text)
        assert "AIzaSyD_FAKE_KEY" not in result
        assert "user@example.com" not in result
        assert "10.0.0.1" not in result

    def test_safe_text_unchanged(self):
        text = "Task scoring completed: 42 tasks processed in 150ms"
        result = redact(text)
        assert result == text

    def test_redact_ssn(self):
        text = "SSN: 123-45-6789"
        result = redact(text)
        assert "123-45-6789" not in result
        assert "SSN" in result

    def test_redact_sk_prefix_token(self):
        text = "Using token sk-1234567890abcdef"
        result = redact(text)
        assert "1234567890abcdef" not in result

    def test_redact_master_key(self):
        text = "VELAFLOW_MASTER_KEY=very_secret_master_key_value"
        result = redact(text)
        assert "very_secret_master_key_value" not in result


class TestSecureLogger:
    """Verify SecureLogger writes redacted, structured logs."""

    def test_logger_creates_log_file(self, tmp_path):
        logger = SecureLogger(log_dir=tmp_path, level="DEBUG", enable_hmac=False)
        logger.info("Test message")
        log_file = tmp_path / "velaflow.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Test message" in content

    def test_logger_redacts_in_file(self, tmp_path):
        logger = SecureLogger(log_dir=tmp_path, level="DEBUG", enable_hmac=False)
        logger.info("Token is TODOIST_API_TOKEN=secret123value")
        content = (tmp_path / "velaflow.log").read_text()
        assert "secret123value" not in content
        assert "REDACTED" in content

    def test_logger_json_format(self, tmp_path):
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=True, enable_hmac=False,
        )
        logger.info("Test JSON")
        content = (tmp_path / "velaflow.log").read_text().strip()
        # Should be valid JSON
        entry = json.loads(content)
        assert entry["level"] == "INFO"
        assert "Test JSON" in entry["msg"]

    def test_logger_audit_event(self, tmp_path):
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=True, enable_hmac=False,
        )
        logger.audit("user.login", user_id="u123", tenant_id="t456")
        content = (tmp_path / "velaflow.log").read_text().strip()
        entry = json.loads(content)
        assert "AUDIT" in entry["msg"]
        assert entry["action"] == "user.login"
        assert entry["user_id"] == "u123"
        assert entry["tenant_id"] == "t456"

    def test_logger_hmac_chain(self, tmp_path):
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=False, enable_hmac=True,
        )
        logger.info("First message")
        logger.info("Second message")
        content = (tmp_path / "velaflow.log").read_text()
        lines = [l for l in content.strip().split("\n") if l.strip()]
        # Each line should have a chain hash
        for line in lines:
            assert "[chain:" in line

    def test_logger_multiple_levels(self, tmp_path):
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=True, enable_hmac=False,
        )
        logger.debug("Debug msg")
        logger.info("Info msg")
        logger.warning("Warn msg")
        logger.error("Error msg")
        content = (tmp_path / "velaflow.log").read_text().strip()
        lines = content.split("\n")
        levels = [json.loads(l)["level"] for l in lines if l.strip()]
        assert "DEBUG" in levels
        assert "INFO" in levels
        assert "WARNING" in levels
        assert "ERROR" in levels


class TestExport:
    """Verify safe log export for Copilot debugging."""

    def test_export_creates_file(self, tmp_path):
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=True, enable_hmac=False,
        )
        logger.info("Test export entry")
        logger.error("Error with token TODOIST_API_TOKEN=secret_value")

        output = tmp_path / "export.md"
        result = logger.export_sanitised(output)
        assert result.exists()
        content = result.read_text()
        assert "Sanitised Debug Log Export" in content
        assert "secret_value" not in content

    def test_export_double_redacts(self, tmp_path):
        # Write a log file manually with a "leaked" secret
        log_file = tmp_path / "velaflow.log"
        log_file.write_text(
            '{"ts":"2026-01-01","level":"ERROR","msg":"key=AIzaSyDFAKEKEY123456"}\n'
        )
        logger = SecureLogger(
            log_dir=tmp_path, level="DEBUG",
            json_format=True, enable_hmac=False,
        )
        output = tmp_path / "export.md"
        result = logger.export_sanitised(output)
        content = result.read_text()
        assert "AIzaSyDFAKEKEY123456" not in content


class TestSetupLogging:
    """Verify the setup_logging convenience function."""

    def test_setup_from_defaults(self, tmp_path):
        os.environ["LOG_DIR"] = str(tmp_path)
        os.environ["LOG_LEVEL"] = "WARNING"
        try:
            logger = setup_logging()
            assert logger._logger.level == 30  # WARNING
        finally:
            os.environ.pop("LOG_DIR", None)
            os.environ.pop("LOG_LEVEL", None)

    def test_setup_with_arguments(self, tmp_path):
        logger = setup_logging(
            log_dir=str(tmp_path), level="DEBUG", max_size_mb=10,
        )
        assert logger._logger.level == 10  # DEBUG
        logger.info("Setup test")
        assert (tmp_path / "velaflow.log").exists()
