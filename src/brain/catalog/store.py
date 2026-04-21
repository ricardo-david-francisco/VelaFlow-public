"""SQLite-backed Catalog Store — on-prem data catalog.

All metadata is persisted in a local SQLite database with:
- WAL mode for concurrent read access
- Parameterized queries only (no SQL injection)
- Restricted file permissions on the database file
- RBAC-integrated access checks via the VelaFlow permission model

This provides a self-hosted catalog control plane for namespaces,
schemas, tables, grants, and lineage tracking — functionally similar
to a managed data catalog but implemented as a file-backed SQLite
database with no external dependency.
zero-dependency, zero-cost local alternative suitable for LXC
deployment on constrained hardware.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brain.catalog.models import (
    CatalogGrant,
    CatalogNamespace,
    CatalogSchema,
    CatalogTable,
    ColumnDef,
    GrantLevel,
    LineageRecord,
)

logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS namespaces (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schemas (
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, name),
    FOREIGN KEY (namespace) REFERENCES namespaces(name)
);

CREATE TABLE IF NOT EXISTS tables (
    namespace TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    name TEXT NOT NULL,
    columns_json TEXT DEFAULT '[]',
    row_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    format TEXT DEFAULT 'json',
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, schema_name, name),
    FOREIGN KEY (namespace, schema_name) REFERENCES schemas(namespace, name)
);

CREATE TABLE IF NOT EXISTS grants (
    role TEXT NOT NULL,
    namespace TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    grant_level TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (role, namespace, schema_name, grant_level)
);

CREATE TABLE IF NOT EXISTS lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    target_table TEXT NOT NULL,
    pipeline_stage TEXT NOT NULL,
    tenant_id TEXT DEFAULT '',
    transformed_at TEXT NOT NULL,
    record_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lineage_target ON lineage(target_table);
CREATE INDEX IF NOT EXISTS idx_lineage_tenant ON lineage(tenant_id);
"""


class CatalogStore:
    """SQLite-backed data catalog with RBAC and lineage.

    Usage:
        store = CatalogStore("/opt/velaflow/data/catalog.db")
        store.create_namespace("velaflow")
        store.create_schema("velaflow", "bronze", "Raw ingested data")
        store.register_table("velaflow", "bronze", "raw_tasks", columns=[...])
        store.grant_access("standard", "velaflow", "bronze", GrantLevel.SELECT)
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._conn.row_factory = sqlite3.Row
        self._harden()
        self._init_schema()
        logger.info("Catalog store initialized at %s", self._db_path)

    def _harden(self) -> None:
        """Apply database security hardening."""
        self._execute("PRAGMA journal_mode=WAL")
        self._execute("PRAGMA foreign_keys=ON")
        self._execute("PRAGMA secure_delete=ON")
        self._execute("PRAGMA busy_timeout=5000")
        # Restrict file permissions (owner read/write only) on POSIX
        if os.name != "nt":
            try:
                os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def _init_schema(self) -> None:
        """Create catalog tables if they don't exist."""
        self._executescript(_SCHEMA_DDL)
        self._commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute wrapper."""
        with self._lock:
            return self._conn.execute(sql, params)

    def _executescript(self, sql: str) -> None:
        """Thread-safe executescript wrapper."""
        with self._lock:
            self._conn.executescript(sql)

    def _commit(self) -> None:
        """Thread-safe commit wrapper."""
        with self._lock:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Namespace operations
    # ------------------------------------------------------------------

    def create_namespace(self, name: str, owner: str = "system") -> CatalogNamespace:
        """Create a top-level namespace (catalog)."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT OR IGNORE INTO namespaces (name, owner, created_at) VALUES (?, ?, ?)",
            (name, owner, now),
        )
        self._commit()
        return CatalogNamespace(name=name, owner=owner, created_at=datetime.fromisoformat(now))

    def get_namespace(self, name: str) -> CatalogNamespace | None:
        """Look up a namespace by name."""
        row = self._execute(
            "SELECT name, owner, created_at FROM namespaces WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return CatalogNamespace(
            name=row["name"],
            owner=row["owner"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def list_namespaces(self) -> list[CatalogNamespace]:
        """Return all namespaces."""
        rows = self._execute(
            "SELECT name, owner, created_at FROM namespaces ORDER BY name"
        ).fetchall()
        return [
            CatalogNamespace(
                name=r["name"],
                owner=r["owner"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Schema operations
    # ------------------------------------------------------------------

    def create_schema(
        self, namespace: str, name: str, description: str = ""
    ) -> CatalogSchema:
        """Create a schema within a namespace."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT OR IGNORE INTO schemas (namespace, name, description, created_at) "
            "VALUES (?, ?, ?, ?)",
            (namespace, name, description, now),
        )
        self._commit()
        return CatalogSchema(
            namespace=namespace,
            name=name,
            description=description,
            created_at=datetime.fromisoformat(now),
        )

    def list_schemas(self, namespace: str) -> list[CatalogSchema]:
        """List all schemas in a namespace."""
        rows = self._execute(
            "SELECT namespace, name, description, created_at "
            "FROM schemas WHERE namespace = ? ORDER BY name",
            (namespace,),
        ).fetchall()
        return [
            CatalogSchema(
                namespace=r["namespace"],
                name=r["name"],
                description=r["description"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Table operations
    # ------------------------------------------------------------------

    def register_table(
        self,
        namespace: str,
        schema_name: str,
        name: str,
        columns: list[ColumnDef] | None = None,
        row_count: int = 0,
        size_bytes: int = 0,
        fmt: str = "json",
        description: str = "",
    ) -> CatalogTable:
        """Register or update a table in the catalog."""
        now = datetime.now(timezone.utc).isoformat()
        cols_json = json.dumps(
            [{"name": c.name, "dtype": c.dtype, "nullable": c.nullable, "description": c.description}
             for c in (columns or [])],
        )
        self._execute(
            "INSERT INTO tables "
            "(namespace, schema_name, name, columns_json, row_count, size_bytes, format, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(namespace, schema_name, name) DO UPDATE SET "
            "columns_json=excluded.columns_json, row_count=excluded.row_count, "
            "size_bytes=excluded.size_bytes, updated_at=excluded.updated_at",
            (namespace, schema_name, name, cols_json, row_count, size_bytes, fmt, description, now, now),
        )
        self._commit()
        return CatalogTable(
            namespace=namespace,
            schema_name=schema_name,
            name=name,
            columns=columns or [],
            row_count=row_count,
            size_bytes=size_bytes,
            format=fmt,
            description=description,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    def get_table(
        self, namespace: str, schema_name: str, name: str
    ) -> CatalogTable | None:
        """Look up a table by fully qualified name."""
        row = self._execute(
            "SELECT * FROM tables WHERE namespace=? AND schema_name=? AND name=?",
            (namespace, schema_name, name),
        ).fetchone()
        if row is None:
            return None
        cols_raw = json.loads(row["columns_json"])
        columns = [
            ColumnDef(name=c["name"], dtype=c["dtype"], nullable=c.get("nullable", True), description=c.get("description", ""))
            for c in cols_raw
        ]
        return CatalogTable(
            namespace=row["namespace"],
            schema_name=row["schema_name"],
            name=row["name"],
            columns=columns,
            row_count=row["row_count"],
            size_bytes=row["size_bytes"],
            format=row["format"],
            description=row["description"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_tables(self, namespace: str, schema_name: str) -> list[CatalogTable]:
        """List all tables in a schema."""
        rows = self._execute(
            "SELECT * FROM tables WHERE namespace=? AND schema_name=? ORDER BY name",
            (namespace, schema_name),
        ).fetchall()
        result = []
        for row in rows:
            cols_raw = json.loads(row["columns_json"])
            columns = [
                ColumnDef(name=c["name"], dtype=c["dtype"], nullable=c.get("nullable", True))
                for c in cols_raw
            ]
            result.append(CatalogTable(
                namespace=row["namespace"],
                schema_name=row["schema_name"],
                name=row["name"],
                columns=columns,
                row_count=row["row_count"],
                size_bytes=row["size_bytes"],
                format=row["format"],
                description=row["description"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            ))
        return result

    def update_table_stats(
        self, namespace: str, schema_name: str, name: str, row_count: int, size_bytes: int = 0
    ) -> None:
        """Update row count and size after a pipeline run."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE tables SET row_count=?, size_bytes=?, updated_at=? "
            "WHERE namespace=? AND schema_name=? AND name=?",
            (row_count, size_bytes, now, namespace, schema_name, name),
        )
        self._commit()

    # ------------------------------------------------------------------
    # Grant operations (RBAC integration)
    # ------------------------------------------------------------------

    def grant_access(
        self, role: str, namespace: str, schema_name: str, level: GrantLevel
    ) -> CatalogGrant:
        """Grant a role access to a schema at a specific level."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT OR IGNORE INTO grants (role, namespace, schema_name, grant_level, granted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, namespace, schema_name, level.value, now),
        )
        self._commit()
        return CatalogGrant(
            role=role,
            namespace=namespace,
            schema_name=schema_name,
            grant_level=level,
            granted_at=datetime.fromisoformat(now),
        )

    def check_access(
        self, role: str, namespace: str, schema_name: str, level: GrantLevel
    ) -> bool:
        """Check if a role has the specified access level on a schema."""
        row = self._execute(
            "SELECT 1 FROM grants "
            "WHERE role=? AND namespace=? AND schema_name=? AND (grant_level=? OR grant_level='ALL')",
            (role, namespace, schema_name, level.value),
        ).fetchone()
        return row is not None

    def list_grants(self, namespace: str, schema_name: str) -> list[CatalogGrant]:
        """List all grants for a schema."""
        rows = self._execute(
            "SELECT * FROM grants WHERE namespace=? AND schema_name=? ORDER BY role",
            (namespace, schema_name),
        ).fetchall()
        return [
            CatalogGrant(
                role=r["role"],
                namespace=r["namespace"],
                schema_name=r["schema_name"],
                grant_level=GrantLevel(r["grant_level"]),
                granted_at=datetime.fromisoformat(r["granted_at"]),
            )
            for r in rows
        ]

    def revoke_access(
        self, role: str, namespace: str, schema_name: str, level: GrantLevel
    ) -> bool:
        """Revoke a specific grant. Returns True if a grant was removed."""
        cursor = self._execute(
            "DELETE FROM grants WHERE role=? AND namespace=? AND schema_name=? AND grant_level=?",
            (role, namespace, schema_name, level.value),
        )
        self._commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Lineage operations
    # ------------------------------------------------------------------

    def record_lineage(
        self,
        source_table: str,
        target_table: str,
        pipeline_stage: str,
        tenant_id: str = "",
        record_count: int = 0,
    ) -> LineageRecord:
        """Record a lineage entry for a pipeline transformation."""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "INSERT INTO lineage (source_table, target_table, pipeline_stage, tenant_id, transformed_at, record_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_table, target_table, pipeline_stage, tenant_id, now, record_count),
        )
        self._commit()
        return LineageRecord(
            source_table=source_table,
            target_table=target_table,
            pipeline_stage=pipeline_stage,
            tenant_id=tenant_id,
            transformed_at=datetime.fromisoformat(now),
            record_count=record_count,
        )

    def get_lineage(self, table_name: str) -> list[LineageRecord]:
        """Get all lineage records where the given table is a target."""
        rows = self._execute(
            "SELECT * FROM lineage WHERE target_table=? ORDER BY transformed_at DESC",
            (table_name,),
        ).fetchall()
        return [
            LineageRecord(
                source_table=r["source_table"],
                target_table=r["target_table"],
                pipeline_stage=r["pipeline_stage"],
                tenant_id=r["tenant_id"],
                transformed_at=datetime.fromisoformat(r["transformed_at"]),
                record_count=r["record_count"],
            )
            for r in rows
        ]

    def get_full_lineage(self, tenant_id: str) -> list[LineageRecord]:
        """Get all lineage records for a tenant."""
        rows = self._execute(
            "SELECT * FROM lineage WHERE tenant_id=? ORDER BY transformed_at",
            (tenant_id,),
        ).fetchall()
        return [
            LineageRecord(
                source_table=r["source_table"],
                target_table=r["target_table"],
                pipeline_stage=r["pipeline_stage"],
                tenant_id=r["tenant_id"],
                transformed_at=datetime.fromisoformat(r["transformed_at"]),
                record_count=r["record_count"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Convenience: bootstrap the default VelaFlow catalog
    # ------------------------------------------------------------------

    def bootstrap_velaflow(self) -> None:
        """Create the default namespace, schemas, and grants.

        Creates default namespaces, grants the owner full permissions,
        bronze/silver/gold schemas and role-based grants.
        """
        ns = "velaflow"
        self.create_namespace(ns, owner="system")

        self.create_schema(ns, "bronze", "Raw ingested data from external APIs")
        self.create_schema(ns, "silver", "Cleaned, deduplicated, PII-masked data")
        self.create_schema(ns, "gold", "Scored, enriched, AI-ready datasets")

        # Role grants — mirrors the RBAC policy in brain.security.rbac
        # free: read gold only
        self.grant_access("free", ns, "gold", GrantLevel.SELECT)

        # standard: read all, write bronze
        for schema in ("bronze", "silver", "gold"):
            self.grant_access("standard", ns, schema, GrantLevel.SELECT)
        self.grant_access("standard", ns, "bronze", GrantLevel.INSERT)

        # premium: full access to all schemas
        for schema in ("bronze", "silver", "gold"):
            self.grant_access("premium", ns, schema, GrantLevel.ALL)

        # admin: full access
        for schema in ("bronze", "silver", "gold"):
            self.grant_access("admin", ns, schema, GrantLevel.ALL)

        logger.info("VelaFlow catalog bootstrapped with default schemas and grants")
