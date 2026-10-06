"""Catalog domain models — Namespace, Schema, Table, Grant, Lineage.

Mirrors the hierarchy of a standard managed data catalog:
  Namespace → Schema (bronze/silver/gold) → Table

On-prem replacement: all metadata lives in a local SQLite database
instead of a cloud control plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class GrantLevel(str, Enum):
    """SQL-style grant levels for table access."""

    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    ALL = "ALL"


@dataclass(frozen=True)
class ColumnDef:
    """Column definition for a catalog table."""

    name: str
    dtype: str  # e.g. "TEXT", "INTEGER", "REAL", "TIMESTAMP"
    nullable: bool = True
    description: str = ""


@dataclass
class CatalogNamespace:
    """Top-level grouping (the 'catalog' level of the namespace hierarchy)."""

    name: str
    owner: str = "system"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CatalogSchema:
    """Schema within a namespace (bronze / silver / gold)."""

    namespace: str
    name: str
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CatalogTable:
    """Registered table with schema and metadata."""

    namespace: str
    schema_name: str
    name: str
    columns: list[ColumnDef] = field(default_factory=list)
    row_count: int = 0
    size_bytes: int = 0
    format: str = "json"  # json | parquet
    description: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def full_name(self) -> str:
        return f"{self.namespace}.{self.schema_name}.{self.name}"


@dataclass
class CatalogGrant:
    """Access grant binding a role to a schema with specific permissions."""

    role: str
    namespace: str
    schema_name: str
    grant_level: GrantLevel = GrantLevel.SELECT
    granted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class LineageRecord:
    """Pipeline lineage — tracks data flow between tables."""

    source_table: str  # full_name of source
    target_table: str  # full_name of target
    pipeline_stage: str  # bronze | silver | gold
    tenant_id: str = ""
    transformed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    record_count: int = 0
