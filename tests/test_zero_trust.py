"""Tests for brain.security.zero_trust — Zero-trust security module."""

import time

import pytest

from brain.security.zero_trust import (
    AuditLogger,
    InputSanitizer,
    RequestSigner,
    SignedRequest,
)


# ── Request Signing Tests ──────────────────────────────────────────────

class TestRequestSigner:
    def test_sign_produces_signature(self):
        signer = RequestSigner("test-secret")
        signed = signer.sign("POST", "/api/v1/webhooks/pipeline", b'{"data": 1}')
        assert signed.signature
        assert signed.nonce
        assert signed.timestamp > 0
        assert signed.method == "POST"
        assert signed.component_id == "api"

    def test_verify_valid_signature(self):
        signer = RequestSigner("test-secret")
        body = b'{"pipeline": "run"}'
        signed = signer.sign("POST", "/api/v1/webhooks/pipeline", body)
        assert signer.verify("POST", "/api/v1/webhooks/pipeline", body, signed) is True

    def test_verify_wrong_body_fails(self):
        signer = RequestSigner("test-secret")
        signed = signer.sign("POST", "/path", b"original")
        assert signer.verify("POST", "/path", b"tampered", signed) is False

    def test_verify_wrong_method_fails(self):
        signer = RequestSigner("test-secret")
        body = b"data"
        signed = signer.sign("POST", "/path", body)
        assert signer.verify("GET", "/path", body, signed) is False

    def test_verify_wrong_path_fails(self):
        signer = RequestSigner("test-secret")
        body = b"data"
        signed = signer.sign("POST", "/path1", body)
        assert signer.verify("POST", "/path2", body, signed) is False

    def test_verify_wrong_secret_fails(self):
        signer1 = RequestSigner("secret-a")
        signer2 = RequestSigner("secret-b")
        body = b"data"
        signed = signer1.sign("POST", "/path", body)
        assert signer2.verify("POST", "/path", body, signed) is False

    def test_verify_replay_rejected(self):
        signer = RequestSigner("test-secret")
        body = b"data"
        signed = signer.sign("POST", "/path", body)
        # First verification succeeds
        assert signer.verify("POST", "/path", body, signed) is True
        # Same nonce rejected (replay)
        assert signer.verify("POST", "/path", body, signed) is False

    def test_verify_expired_timestamp_fails(self):
        signer = RequestSigner("test-secret")
        body = b"data"
        signed = signer.sign("POST", "/path", body)
        # Manually expire the timestamp
        expired = SignedRequest(
            method=signed.method,
            path=signed.path,
            timestamp=int(time.time()) - 600,  # 10 minutes ago
            nonce=signed.nonce + "_new",  # Different nonce
            signature=signed.signature,
            component_id=signed.component_id,
        )
        assert signer.verify("POST", "/path", body, expired) is False

    def test_sign_different_components(self):
        signer = RequestSigner("test-secret")
        sig1 = signer.sign("POST", "/path", b"data", component_id="api")
        sig2 = signer.sign("POST", "/path", b"data", component_id="worker")
        assert sig1.component_id == "api"
        assert sig2.component_id == "worker"
        # Different components produce different signatures
        assert sig1.signature != sig2.signature

    def test_no_secret_generates_ephemeral(self):
        """When no secret is configured, an ephemeral key is generated."""
        signer = RequestSigner()
        signed = signer.sign("GET", "/health", b"")
        assert signer.verify("GET", "/health", b"", signed) is True


# ── Audit Logger Tests ─────────────────────────────────────────────────

class TestAuditLogger:
    def test_log_auth_success(self, caplog):
        logger = AuditLogger("api")
        with caplog.at_level("INFO", logger="velaflow.audit.api"):
            logger.log_auth_success("tenant-1", "admin", "/api/v1/tasks")
        assert "AUTH_SUCCESS" in caplog.text
        assert "tenant-1" in caplog.text

    def test_log_auth_failure(self, caplog):
        logger = AuditLogger("api")
        with caplog.at_level("WARNING", logger="velaflow.audit.api"):
            logger.log_auth_failure("expired_token", "/api/v1/tasks", "192.168.1.1")
        assert "AUTH_FAILURE" in caplog.text

    def test_log_permission_denied(self, caplog):
        logger = AuditLogger("api")
        with caplog.at_level("WARNING", logger="velaflow.audit.api"):
            logger.log_permission_denied("t1", "free", "use:premium_llm", "/api/v1/llm")
        assert "PERMISSION_DENIED" in caplog.text

    def test_log_data_access(self, caplog):
        logger = AuditLogger("worker")
        with caplog.at_level("INFO", logger="velaflow.audit.worker"):
            logger.log_data_access("t1", "gold", "SELECT", 42)
        assert "DATA_ACCESS" in caplog.text
        assert "records=42" in caplog.text

    def test_log_pipeline_event(self, caplog):
        logger = AuditLogger("worker")
        with caplog.at_level("INFO", logger="velaflow.audit.worker"):
            logger.log_pipeline_event("t1", "silver", "completed", 150)
        assert "PIPELINE_EVENT" in caplog.text

    def test_log_security_event(self, caplog):
        logger = AuditLogger("api")
        with caplog.at_level("WARNING", logger="velaflow.audit.api"):
            logger.log_security_event("PATH_TRAVERSAL", "attempt on /bronze/../../../etc/passwd")
        assert "SECURITY_EVENT" in caplog.text


# ── Input Sanitizer Tests ──────────────────────────────────────────────

class TestInputSanitizer:
    def test_valid_tenant_id(self):
        assert InputSanitizer.validate_tenant_id("tenant-123") == "tenant-123"
        assert InputSanitizer.validate_tenant_id("my_tenant") == "my_tenant"

    def test_empty_tenant_id_rejected(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_tenant_id("")

    def test_long_tenant_id_rejected(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_tenant_id("a" * 100)

    def test_invalid_chars_in_tenant_id(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_tenant_id("tenant/../../../etc")

    def test_sql_injection_in_tenant_id(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_tenant_id("tenant'; DROP TABLE users;--")

    def test_valid_content(self):
        result = InputSanitizer.validate_content("Buy milk and eggs")
        assert result == "Buy milk and eggs"

    def test_oversized_content_rejected(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_content("x" * 20_000)

    def test_valid_identifier(self):
        assert InputSanitizer.validate_identifier("bronze", "schema") == "bronze"
        assert InputSanitizer.validate_identifier("my_table_123", "table") == "my_table_123"

    def test_invalid_identifier(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_identifier("../etc/passwd", "path")

    def test_validate_labels(self):
        labels = InputSanitizer.validate_labels(["work", "urgent", "home"])
        assert labels == ["work", "urgent", "home"]

    def test_too_many_labels_rejected(self):
        with pytest.raises(ValueError):
            InputSanitizer.validate_labels([f"label-{i}" for i in range(100)])

    def test_dangerous_patterns_detected(self):
        assert InputSanitizer.has_dangerous_patterns("'; DROP TABLE users;--") is True
        assert InputSanitizer.has_dangerous_patterns("<script>alert('xss')</script>") is True
        assert InputSanitizer.has_dangerous_patterns("normal text here") is False

    def test_path_traversal_detected(self):
        assert InputSanitizer.has_dangerous_patterns("../../etc/passwd") is True
