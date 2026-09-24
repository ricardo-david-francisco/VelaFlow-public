"""VelaFlow Security — PII detection, encryption, RBAC, bans, and Google auth."""

from brain.security.pii import PIIDetector
from brain.security.encryption import FieldEncryptor
from brain.security.rbac import RBACPolicy, Permission
from brain.security.ban import BanManager

__all__ = [
    "PIIDetector",
    "FieldEncryptor",
    "RBACPolicy",
    "Permission",
    "BanManager",
]

# Lazy import: google_auth requires google-auth-oauthlib (optional dependency)
def verify_google_id_token(*args, **kwargs):  # type: ignore[no-untyped-def]
    from brain.security.google_auth import verify_google_id_token as _verify
    return _verify(*args, **kwargs)
