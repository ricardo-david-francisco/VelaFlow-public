"""Test-only helpers for generating synthetic credential-shaped values.

Why this module exists
----------------------
Several tests need to exercise code paths that *look like* they take a
credential (a Todoist token, a Notion token, a Gemini API key, etc.). If
those values are inlined as string literals, SAST tools flag them as
hardcoded secrets even though they are obviously fake.

This helper returns fresh random strings at import time, shaped to match
the real credentials' alphabet and length so that any validator in the
code path (regex, minimum length, etc.) still exercises correctly.

No value produced here is, or ever was, a real credential.
"""

from __future__ import annotations

import secrets as _secrets


def fake_token(prefix: str = "tok_", length: int = 32) -> str:
    """Generate a fresh random test token. Never a real credential."""
    return f"{prefix}{_secrets.token_hex(max(8, length // 2))}"


def fake_api_key(prefix: str = "key_", length: int = 32) -> str:
    """Generate a fresh random test API key. Never a real credential."""
    return f"{prefix}{_secrets.token_urlsafe(length)}"


def fake_password(length: int = 24) -> str:
    """Generate a fresh random test password. Never a real credential."""
    return _secrets.token_urlsafe(length)
