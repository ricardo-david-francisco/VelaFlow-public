"""Field-Level Encryption — AES-256-GCM authenticated encryption at rest.

Two encryption surfaces are offered:

1. ``FieldEncryptor`` — legacy master-key encryption, retained for
   non-credential payloads (for example cached intermediate artefacts)
   and for the platform-owner vault before any tenant OAuth has run.

2. ``CredentialEncryptor`` — the hardened surface used for every
   third-party credential a tenant entrusts to the platform.

   The key is derived with HKDF-SHA-256 from two inputs, neither of
   which alone is enough to recover plaintext:

   - ``VELAFLOW_CREDENTIAL_PEPPER`` — a 32-byte base64 secret that
     lives only in the operator process environment. It is never
     written to disk, never logged, and is rotated with a
     credential re-wrap procedure.

   - ``owner_google_sub`` concatenated with ``tenant_id`` — the
     Google OAuth subject claim of the tenant owner. The server
     never sees the user's Google password or refresh token; the
     sub is supplied by Google's OIDC response at login time and
     pinned to the tenant record.

   The net effect: an attacker with shell access inside the host can
   only read plaintext credentials if they also (a) can read the
   operator process environment (pepper), (b) know the tenant's
   owner sub, and (c) possess the ciphertext. Losing any one input
   prevents decryption. The operator can revoke all credentials by
   rotating the pepper.

   This module explicitly refuses to operate without a pepper and
   without an owner_sub — no silent fallback to master-key encryption
   is permitted.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class FieldEncryptor:
    """Encrypt and decrypt individual field values using AES-256-GCM.

    Uses PBKDF2 key derivation to produce per-tenant keys from
    a master secret. AES-256-GCM provides authenticated encryption
    with integrity verification.

    Args:
        master_key: Base64-encoded master encryption key.
                    Required in production — raises RuntimeError if absent.
    """

    def __init__(self, master_key: str | None = None) -> None:
        if not master_key:
            raise RuntimeError(
                "VELAFLOW_MASTER_KEY is required. "
                "Generate with: python -c "
                "'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
            )
        self._master = base64.urlsafe_b64decode(master_key)

    def derive_tenant_key(self, tenant_id: str) -> bytes:
        """Derive a tenant-specific encryption key from the master key."""
        return hashlib.pbkdf2_hmac(
            "sha256",
            self._master,
            tenant_id.encode("utf-8"),
            iterations=100_000,
            dklen=32,
        )

    def encrypt(self, plaintext: str, tenant_id: str, field_name: str = "") -> str:
        """Encrypt a plaintext string for a specific tenant.

        Returns a base64-encoded string containing nonce + ciphertext + tag.
        Uses AES-256-GCM authenticated encryption with associated data
        (field_name) to prevent ciphertext relocation attacks.
        """
        key = self.derive_tenant_key(tenant_id)
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        aad = field_name.encode("utf-8") if field_name else None
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
        # Pack: nonce (12) + ciphertext+tag (var)
        packed = nonce + ciphertext
        return base64.urlsafe_b64encode(packed).decode("ascii")

    def decrypt(self, encrypted: str, tenant_id: str, field_name: str = "") -> str:
        """Decrypt a base64-encoded encrypted string for a specific tenant."""
        key = self.derive_tenant_key(tenant_id)
        packed = base64.urlsafe_b64decode(encrypted)
        nonce = packed[:12]
        ciphertext = packed[12:]
        aesgcm = AESGCM(key)
        aad = field_name.encode("utf-8") if field_name else None
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad)
        return plaintext_bytes.decode("utf-8")

    @staticmethod
    def generate_master_key() -> str:
        """Generate a new random master key (base64-encoded)."""
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


# ── Credential-grade encryption (Round 18) ──────────────────────────────────

# Versioning so a future key-rotation procedure can distinguish wrappers.
_CREDENTIAL_SCHEMA_V2 = b"\x02"
_CREDENTIAL_INFO = b"velaflow-credential-v2"


class CredentialNotDecryptable(RuntimeError):
    """Raised when a credential cannot be decrypted with the current material."""


class CredentialEncryptor:
    """Per-tenant credential encryption bound to pepper + owner google_sub.

    The key derivation is deliberately split so no single secret is
    sufficient on its own:

        KEK = HKDF-SHA256(
            ikm   = pepper,                             # operator env only
            salt  = sha256(tenant_id || owner_sub),     # per tenant
            info  = "velaflow-credential-v2",
            length= 32
        )

    The ciphertext format is::

        schema(1) || nonce(12) || ciphertext+tag

    AEAD associated data is the field name, preventing an attacker from
    relocating (for example) a ``todoist_token`` ciphertext into a
    ``gemini_api_key`` field.
    """

    def __init__(self, pepper: str | None) -> None:
        if not pepper:
            raise RuntimeError(
                "VELAFLOW_CREDENTIAL_PEPPER is required for the credential "
                "vault. Generate with: python -c "
                "'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
            )
        try:
            material = base64.urlsafe_b64decode(pepper + "=" * (-len(pepper) % 4))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"VELAFLOW_CREDENTIAL_PEPPER is not valid base64url: {e}")
        if len(material) < 32:
            raise RuntimeError(
                f"VELAFLOW_CREDENTIAL_PEPPER decoded to {len(material)} bytes; need >=32."
            )
        self._pepper = material

    @staticmethod
    def generate_pepper() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

    def _derive(self, tenant_id: str, owner_sub: str) -> bytes:
        if not tenant_id or not owner_sub:
            raise CredentialNotDecryptable(
                "tenant_id and owner_google_sub must both be set before "
                "credentials can be encrypted or decrypted."
            )
        salt = hashlib.sha256(f"{tenant_id}\x1f{owner_sub}".encode("utf-8")).digest()
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=_CREDENTIAL_INFO,
        )
        return hkdf.derive(self._pepper)

    def encrypt(
        self,
        plaintext: str,
        tenant_id: str,
        owner_sub: str,
        field_name: str,
    ) -> str:
        if not field_name:
            raise ValueError("field_name is mandatory for credential encryption (AAD).")
        key = self._derive(tenant_id, owner_sub)
        nonce = os.urandom(12)
        aad = field_name.encode("utf-8")
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return base64.urlsafe_b64encode(_CREDENTIAL_SCHEMA_V2 + nonce + ct).decode("ascii")

    def decrypt(
        self,
        encrypted: str,
        tenant_id: str,
        owner_sub: str,
        field_name: str,
    ) -> str:
        if not encrypted:
            return ""
        try:
            packed = base64.urlsafe_b64decode(encrypted + "=" * (-len(encrypted) % 4))
        except Exception as e:  # noqa: BLE001
            raise CredentialNotDecryptable(f"ciphertext is not valid base64url: {e}")
        if len(packed) < 1 + 12 + 16 or packed[:1] != _CREDENTIAL_SCHEMA_V2:
            raise CredentialNotDecryptable("unrecognised credential schema version")
        nonce = packed[1:13]
        ct = packed[13:]
        key = self._derive(tenant_id, owner_sub)
        aad = field_name.encode("utf-8") if field_name else None
        try:
            pt = AESGCM(key).decrypt(nonce, ct, aad)
        except Exception as e:  # noqa: BLE001 — cryptography raises InvalidTag subclass
            raise CredentialNotDecryptable(f"authenticated decryption failed: {e}")
        return pt.decode("utf-8")
