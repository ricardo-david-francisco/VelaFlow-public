"""Abstract storage interface for the medallion architecture.

Provides a backend-agnostic API for reading and writing data across
pipeline layers. Current implementations target the local filesystem
(``LocalStorageBackend``) with an ``EncryptedStorageBackend`` wrapper
for zero-knowledge at-rest encryption. Cloud storage backends
(S3 / GCS / ADLS) are a future extension.
"""

from __future__ import annotations

import abc
from typing import Any


class StorageBackend(abc.ABC):
    """Abstract base class for medallion data storage."""

    @abc.abstractmethod
    def write_json(self, key: str, data: dict[str, Any]) -> None:
        """Write a JSON document to storage.

        Args:
            key: Hierarchical path (e.g., 'bronze/tenant_1/todoist/batch_001.json')
            data: JSON-serializable dictionary
        """

    @abc.abstractmethod
    def read_json(self, key: str) -> dict[str, Any] | None:
        """Read a JSON document from storage.

        Returns None if the key does not exist.
        """

    @abc.abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """List all keys under a given prefix.

        Returns sorted list of full key paths.
        """

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Delete a document from storage.

        Returns True if the key existed and was deleted.
        """

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """Check if a key exists in storage."""

    # ── Raw text I/O (audit logs, encrypted event trails) ──────────
    @abc.abstractmethod
    def write_text(self, key: str, text: str) -> None:
        """Write raw text to storage (for audit logs, encrypted blobs)."""

    @abc.abstractmethod
    def read_text(self, key: str) -> str | None:
        """Read raw text from storage. Returns None if key does not exist."""
