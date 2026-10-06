"""JWT Authentication — Token generation and validation.

Implements JWT-based authentication for the multi-tenant API.
Tokens encode the tenant_id and role for downstream RBAC checks.

Security model:
- HS256 signing with a server-side secret
- Short-lived access tokens (configurable expiry)
- Tenant ID embedded in token claims for isolation enforcement
"""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default expiry: 1 hour (SaaS-hardened — short-lived access tokens)
_DEFAULT_EXPIRY_SECONDS = 3600
_JWT_SECRET = os.environ.get("JWT_SECRET", "")
_JWT_ALGORITHM = "HS256"
_JWT_ISSUER = "velaflow-api"
_JWT_AUDIENCE = "velaflow-api"


def _get_secret(override: str | None = None) -> str:
    """Return the JWT signing secret, failing loudly if not configured."""
    secret = override or _JWT_SECRET
    if not secret:
        raise RuntimeError(
            "JWT_SECRET environment variable is required. "
            "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
        )
    return secret


@dataclass
class TokenClaims:
    """Decoded JWT claims."""

    tenant_id: str
    role: str
    email: str
    exp: int
    iat: int
    user_id: str = ""
    user_role: str = ""


def create_access_token(
    tenant_id: str,
    role: str,
    email: str,
    expiry_seconds: int = _DEFAULT_EXPIRY_SECONDS,
    secret: str | None = None,
    user_id: str = "",
    user_role: str = "",
) -> str:
    """Create a signed JWT access token.

    Implements a minimal JWT encoder to avoid external dependencies.
    For production, replace with PyJWT or python-jose.
    """
    secret_key = _get_secret(secret).encode("utf-8")
    now = int(time.time())

    header = {"alg": _JWT_ALGORITHM, "typ": "JWT"}
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "role": role,
        "email": email,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": now,
        "exp": now + expiry_seconds,
    }
    if user_id:
        payload["user_id"] = user_id
    if user_role:
        payload["user_role"] = user_role

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")))

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(secret_key, signing_input, hashlib.sha256).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    return f"{header_b64}.{payload_b64}.{signature_b64}"


def verify_token(
    token: str,
    secret: str | None = None,
) -> TokenClaims | None:
    """Verify and decode a JWT token.

    Returns TokenClaims if valid, None if invalid or expired.
    """
    try:
        secret_key = _get_secret(secret).encode("utf-8")
    except RuntimeError:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    header_b64, payload_b64, signature_b64 = parts

    # Verify signature
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(secret_key, signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode_bytes(signature_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        logger.warning("JWT signature verification failed")
        return None

    # Decode payload
    try:
        payload_json = _b64url_decode(payload_b64)
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, ValueError):
        return None

    # Check expiry
    exp = payload.get("exp", 0)
    if time.time() > exp:
        logger.debug("JWT expired")
        return None

    # Validate issuer/audience claims (prevents cross-environment token reuse)
    if payload.get("iss") != _JWT_ISSUER or payload.get("aud") != _JWT_AUDIENCE:
        logger.warning("JWT rejected: invalid issuer/audience")
        return None

    return TokenClaims(
        tenant_id=payload.get("tenant_id", ""),
        role=payload.get("role", ""),
        email=payload.get("email", ""),
        exp=exp,
        iat=payload.get("iat", 0),
        user_id=payload.get("user_id", ""),
        user_role=payload.get("user_role", ""),
    )


def _b64url_encode(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> str:
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode("utf-8")


def _b64url_decode_bytes(data: str) -> bytes:
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)
