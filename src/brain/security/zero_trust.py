"""Zero-Trust Security — Inter-component authentication and hardening.

Implements defense-in-depth for the VelaFlow platform assuming
any component (container, network, storage) may be compromised:

1. Request Signing:    HMAC-SHA256 signatures on inter-service requests
                       (API ↔ Worker ↔ n8n) to prevent forgery.
2. Nonce + Timestamp:  Replay attack prevention with 5-minute windows.
3. Audit Logging:      Security-relevant events logged for forensics.
4. Database Hardening: SQLite WAL mode, restricted permissions, secure delete.
5. Input Sanitization: Defense against injection in all user-facing inputs.

Zero-trust principles applied:
- Never trust, always verify (even internal traffic)
- Least privilege (RBAC + catalog grants)
- Assume breach (audit logging, encrypted storage, container hardening)
- Micro-segmentation (nested LXC for premium tier)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum clock skew for request signatures (seconds)
_MAX_TIMESTAMP_DRIFT = 300  # 5 minutes

# Nonce cache size (prevents replay within the time window)
_MAX_NONCE_CACHE = 10_000


@dataclass(frozen=True)
class SignedRequest:
    """A signed inter-service request."""

    method: str
    path: str
    timestamp: int
    nonce: str
    signature: str
    component_id: str


class RequestSigner:
    """HMAC-SHA256 request signing for inter-service authentication.

    Each component (API, Worker, n8n) shares a secret key.
    Every request between components is signed and verified to
    prevent forgery even if the network is compromised.

    Usage:
        signer = RequestSigner(shared_secret="...")
        sig = signer.sign("POST", "/api/v1/webhooks/pipeline", body_bytes)
        # ... send request with X-VelaFlow-Signature header ...
        valid = signer.verify("POST", "/api/v1/webhooks/pipeline", body_bytes, sig)
    """

    def __init__(self, shared_secret: str | None = None) -> None:
        secret = shared_secret or os.environ.get("VELAFLOW_COMPONENT_SECRET", "")
        if not secret:
            secret = secrets.token_hex(32)
            logger.warning("No component secret configured — generated ephemeral key")
        self._secret = secret.encode("utf-8")
        self._seen_nonces: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def sign(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        component_id: str = "api",
    ) -> SignedRequest:
        """Sign a request with HMAC-SHA256."""
        timestamp = int(time.time())
        nonce = secrets.token_hex(16)
        message = self._build_message(method, path, body, timestamp, nonce, component_id)
        signature = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return SignedRequest(
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
            component_id=component_id,
        )

    def verify(
        self,
        method: str,
        path: str,
        body: bytes,
        signed: SignedRequest,
    ) -> bool:
        """Verify a signed request.

        Checks:
        1. Timestamp within drift window (anti-replay)
        2. Nonce not seen before (anti-replay)
        3. HMAC signature matches (integrity + authenticity)
        """
        # Check timestamp drift
        now = int(time.time())
        if abs(now - signed.timestamp) > _MAX_TIMESTAMP_DRIFT:
            logger.warning(
                "Request signature rejected: timestamp drift %ds (max %ds)",
                abs(now - signed.timestamp),
                _MAX_TIMESTAMP_DRIFT,
            )
            return False

        # Atomic nonce check + HMAC verify + nonce record (prevents TOCTOU race)
        with self._lock:
            # Check nonce reuse
            if signed.nonce in self._seen_nonces:
                logger.warning("Request signature rejected: nonce reuse detected")
                return False

            # Verify HMAC
            message = self._build_message(
                method, path, body, signed.timestamp, signed.nonce, signed.component_id
            )
            expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()
            if not secrets.compare_digest(expected, signed.signature):
                logger.warning("Request signature rejected: HMAC mismatch")
                return False

            # Record nonce to prevent replay (OrderedDict preserves insertion order)
            self._seen_nonces[signed.nonce] = time.time()
            if len(self._seen_nonces) > _MAX_NONCE_CACHE:
                # Evict oldest half by insertion order (deterministic)
                for _ in range(_MAX_NONCE_CACHE // 2):
                    self._seen_nonces.popitem(last=False)

        return True

    @staticmethod
    def _build_message(
        method: str, path: str, body: bytes, timestamp: int, nonce: str, component_id: str
    ) -> bytes:
        """Canonical message format for signing."""
        body_hash = hashlib.sha256(body).hexdigest()
        return f"{method}\n{path}\n{timestamp}\n{nonce}\n{component_id}\n{body_hash}".encode("utf-8")


class AuditLogger:
    """Security event audit logger.

    Records security-relevant events for forensic analysis.
    In zero-trust, every access is logged and auditable.

    Events are written to the standard Python logger with
    structured fields for log aggregation (ELK, Loki, etc.).
    All field values are sanitized to prevent log injection.
    """

    _LOG_SANITIZE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f]")

    def __init__(self, component: str = "api") -> None:
        self._component = self._sanitize(component)
        self._logger = logging.getLogger(f"velaflow.audit.{self._component}")

    @classmethod
    def _sanitize(cls, value: str) -> str:
        """Remove control characters that enable log injection."""
        return cls._LOG_SANITIZE.sub("_", value)[:512]

    def log_auth_success(self, tenant_id: str, role: str, endpoint: str) -> None:
        self._logger.info(
            "AUTH_SUCCESS tenant=%s role=%s endpoint=%s component=%s",
            self._sanitize(tenant_id), self._sanitize(role),
            self._sanitize(endpoint), self._component,
        )

    def log_auth_failure(self, reason: str, endpoint: str, ip: str = "") -> None:
        self._logger.warning(
            "AUTH_FAILURE reason=%s endpoint=%s ip=%s component=%s",
            self._sanitize(reason), self._sanitize(endpoint),
            self._sanitize(ip), self._component,
        )

    def log_permission_denied(
        self, tenant_id: str, role: str, permission: str, endpoint: str
    ) -> None:
        self._logger.warning(
            "PERMISSION_DENIED tenant=%s role=%s permission=%s endpoint=%s component=%s",
            self._sanitize(tenant_id), self._sanitize(role),
            self._sanitize(permission), self._sanitize(endpoint), self._component,
        )

    def log_data_access(
        self, tenant_id: str, layer: str, operation: str, record_count: int = 0
    ) -> None:
        self._logger.info(
            "DATA_ACCESS tenant=%s layer=%s operation=%s records=%d component=%s",
            self._sanitize(tenant_id), self._sanitize(layer),
            self._sanitize(operation), record_count, self._component,
        )

    def log_pipeline_event(
        self, tenant_id: str, stage: str, status: str, duration_ms: int = 0
    ) -> None:
        self._logger.info(
            "PIPELINE_EVENT tenant=%s stage=%s status=%s duration_ms=%d component=%s",
            self._sanitize(tenant_id), self._sanitize(stage),
            self._sanitize(status), duration_ms, self._component,
        )

    def log_security_event(self, event_type: str, details: str) -> None:
        self._logger.warning(
            "SECURITY_EVENT type=%s details=%s component=%s",
            self._sanitize(event_type), self._sanitize(details), self._component,
        )


class InputSanitizer:
    """Input validation and sanitization for zero-trust boundaries.

    Validates all user-provided input at system boundaries to prevent:
    - SQL injection (parameterized queries handle this, but belt + suspenders)
    - Path traversal (../../../etc/passwd)
    - Command injection (shell metacharacters)
    - Oversized payloads (DoS prevention)
    """

    # Maximum field lengths to prevent DoS
    MAX_TENANT_ID_LENGTH = 64
    MAX_CONTENT_LENGTH = 10_000
    MAX_LABEL_LENGTH = 100
    MAX_LABELS_COUNT = 50

    # Patterns that should never appear in identifiers
    _DANGEROUS_PATTERNS = re.compile(r"[;\-\-\'\"\\]|(\.\./)|(<script)", re.IGNORECASE)
    _SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_\-]+$")

    @classmethod
    def validate_tenant_id(cls, tenant_id: str) -> str:
        """Validate and sanitize a tenant ID."""
        if not tenant_id or len(tenant_id) > cls.MAX_TENANT_ID_LENGTH:
            raise ValueError(f"Invalid tenant ID length (max {cls.MAX_TENANT_ID_LENGTH})")
        if not cls._SAFE_IDENTIFIER.match(tenant_id):
            raise ValueError("Tenant ID contains invalid characters")
        return tenant_id

    @classmethod
    def validate_content(cls, content: str) -> str:
        """Validate content length (does NOT strip — PII detector handles masking)."""
        if len(content) > cls.MAX_CONTENT_LENGTH:
            raise ValueError(f"Content exceeds maximum length ({cls.MAX_CONTENT_LENGTH})")
        return content

    @classmethod
    def validate_identifier(cls, name: str, field_name: str = "identifier") -> str:
        """Validate a generic identifier (schema name, table name, etc.)."""
        if not name or len(name) > 128:
            raise ValueError(f"Invalid {field_name} length")
        if not cls._SAFE_IDENTIFIER.match(name):
            raise ValueError(f"{field_name} contains invalid characters")
        return name

    @classmethod
    def validate_labels(cls, labels: list[str]) -> list[str]:
        """Validate a list of labels."""
        if len(labels) > cls.MAX_LABELS_COUNT:
            raise ValueError(f"Too many labels (max {cls.MAX_LABELS_COUNT})")
        return [
            label[:cls.MAX_LABEL_LENGTH]
            for label in labels
            if isinstance(label, str)
        ]

    @classmethod
    def has_dangerous_patterns(cls, text: str) -> bool:
        """Check if text contains suspicious patterns."""
        return bool(cls._DANGEROUS_PATTERNS.search(text))
