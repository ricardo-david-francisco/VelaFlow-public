"""VelaFlow Storage Layer — Abstract storage for medallion data."""

from brain.storage.base import StorageBackend
from brain.storage.local import LocalStorageBackend

__all__ = ["StorageBackend", "LocalStorageBackend"]
