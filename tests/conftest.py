"""Test-suite global fixtures.

Sets the credential-vault pepper so the new ``CredentialEncryptor``
can be instantiated by ``TenantManager`` and ``QueueWorker`` during
unit tests. The pepper used here is ephemeral and never written to
disk; it exists only for the lifetime of the pytest process.
"""

from __future__ import annotations

import base64
import os
import secrets


def _ensure_env(name: str, value_factory) -> None:
    if not os.environ.get(name):
        os.environ[name] = value_factory()


def _force_strong_b64(name: str, n_bytes: int = 32) -> None:
    """Reset env var if it does not decode to at least ``n_bytes``."""
    raw = os.environ.get(name, "")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        decoded = b""
    if len(decoded) < n_bytes:
        os.environ[name] = base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).decode("ascii")


_force_strong_b64("VELAFLOW_CREDENTIAL_PEPPER")
_force_strong_b64("VELAFLOW_MASTER_KEY")
_ensure_env("JWT_SECRET", lambda: secrets.token_urlsafe(48))
