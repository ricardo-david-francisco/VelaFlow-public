"""Local Data Catalog — on-prem, SQLite-backed catalog for namespaces,

Provides namespace → schema → table governance with RBAC-integrated
access grants and pipeline lineage tracking, all backed by SQLite.
"""

from brain.catalog.models import (
    CatalogGrant,
    CatalogNamespace,
    CatalogSchema,
    CatalogTable,
    ColumnDef,
    LineageRecord,
)
from brain.catalog.store import CatalogStore

__all__ = [
    "CatalogGrant",
    "CatalogNamespace",
    "CatalogSchema",
    "CatalogStore",
    "CatalogTable",
    "ColumnDef",
    "LineageRecord",
]
