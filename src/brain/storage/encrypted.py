"""Encrypted storage backend — Zero-knowledge data-at-rest encryption.

Wraps any StorageBackend and encrypts all JSON payloads using
per-tenant AES-256-GCM keys derived from the master key.

The operator/admin CANNOT read tenant data — only the tenant's
derived key can decrypt their records. This provides true
zero-knowledge architecture where even infrastructure owners
cannot access user content.

Tenant ID extraction:
    The tenant_id is extracted from the storage key path.
    Keys follow the convention: {layer}/{tenant_id}/...
    (e.g., 'bronze/tn_abc123/todoist/batch_001.json')
    If tenant_id cannot be extracted, data is stored unencrypted
    (for system metadata like partition markers).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from brain.security.encryption import FieldEncryptor
from brain.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# Pattern to extract tenant_id from storage keys
# Matches: {layer}/{tenant_id}/... or tenants/{tenant_id}.json
# Also covers vault/, users/, invites/ paths for full encryption coverage
_TENANT_ID_PATTERN = re.compile(
    r"^(?:bronze|silver|gold|runs|vault|users|invites)/([^/]+)/|^tenants/([^/.]+)\.json$"
)


class EncryptedStorageBackend(StorageBackend):
    """Zero-knowledge encrypted wrapper around any StorageBackend.

    All tenant data is encrypted before being persisted and decrypted
    on read. System metadata (partition markers) is stored in plaintext.

    Usage:
        inner = LocalStorageBackend("/opt/velaflow/data/medallion")
        encryptor = FieldEncryptor(master_key)
        storage = EncryptedStorageBackend(inner, encryptor)
    """

    def __init__(self, inner: StorageBackend, encryptor: FieldEncryptor) -> None:
        self._inner = inner
        self._encryptor = encryptor

    @staticmethod
    def _extract_tenant_id(key: str) -> str | None:
        """Extract tenant_id from a storage key path."""
        m = _TENANT_ID_PATTERN.match(key)
        if m:
            return m.group(1) or m.group(2)
        return None

    def write_json(self, key: str, data: dict[str, Any]) -> None:
        tenant_id = self._extract_tenant_id(key)
        if tenant_id and not key.endswith(".partition"):
            # Encrypt the entire JSON payload as a single blob
            plaintext = json.dumps(data, separators=(",", ":"), default=str)
            ciphertext = self._encryptor.encrypt(
                plaintext, tenant_id, field_name=f"storage:{key}"
            )
            envelope = {
                "_encrypted": True,
                "_version": 1,
                "_tenant_id": tenant_id,
                "_ciphertext": ciphertext,
            }
            self._inner.write_json(key, envelope)
        else:
            # System metadata — store unencrypted
            self._inner.write_json(key, data)

    def read_json(self, key: str) -> dict[str, Any] | None:
        raw = self._inner.read_json(key)
        if raw is None:
            return None
        if raw.get("_encrypted"):
            tenant_id = raw.get("_tenant_id") or self._extract_tenant_id(key)
            if not tenant_id:
                logger.warning("Cannot decrypt %s — no tenant_id", key)
                return None
            try:
                plaintext = self._encryptor.decrypt(
                    raw["_ciphertext"], tenant_id, field_name=f"storage:{key}"
                )
                return json.loads(plaintext)
            except Exception:
                logger.error("Failed to decrypt %s", key, exc_info=True)
                return None
        # Unencrypted (system metadata or legacy data)
        return raw

    def list_keys(self, prefix: str) -> list[str]:
        return self._inner.list_keys(prefix)

    def delete(self, key: str) -> bool:
        return self._inner.delete(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def write_text(self, key: str, text: str) -> None:
        self._inner.write_text(key, text)

    def read_text(self, key: str) -> str | None:
        return self._inner.read_text(key)
