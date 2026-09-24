"""Google OAuth2 authentication — ID token verification.

Authenticates users via Google Sign-In ID tokens. Google handles MFA
on their end; we verify the token signature and extract user identity.

Flow:
1. Client authenticates with Google (Sign-In button or OAuth2 flow)
2. Client sends Google ID token to POST /api/v1/auth/google
3. We verify the token with Google's public keys
4. We create or look up the User + Tenant
5. We issue a VelaFlow JWT with user_id, tenant_id, and role

Security:
- Token verified against Google's public certificates (no trust on client)
- Client ID must match GOOGLE_OAUTH_CLIENT_ID
- Email must be verified by Google
- Token expiry enforced
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")


@dataclass(frozen=True)
class GoogleIdentity:
    """Verified identity from a Google ID token."""

    sub: str            # Google's unique user ID (stable, never changes)
    email: str
    email_verified: bool
    name: str
    picture: str


def verify_google_id_token(token: str) -> GoogleIdentity | None:
    """Verify a Google ID token and extract user identity.

    Returns None if verification fails for any reason.
    """
    client_id = _GOOGLE_CLIENT_ID
    if not client_id:
        logger.error("GOOGLE_OAUTH_CLIENT_ID not configured")
        return None

    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            client_id,
        )
    except ValueError as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        return None

    # Ensure email is verified (Google guarantees this for most flows,
    # but we enforce it explicitly)
    if not idinfo.get("email_verified", False):
        logger.warning("Google account email not verified: %s", idinfo.get("email"))
        return None

    return GoogleIdentity(
        sub=idinfo["sub"],
        email=idinfo["email"],
        email_verified=idinfo.get("email_verified", False),
        name=idinfo.get("name", ""),
        picture=idinfo.get("picture", ""),
    )
