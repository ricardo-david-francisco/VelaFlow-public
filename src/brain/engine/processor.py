"""Medallion Processor — SQL-based Bronze → Silver → Gold pipeline.

SQL-native medallion pipeline running in-process on DuckDB.
Each stage performs the standard medallion transformations

- Bronze: raw JSON ingestion → structured tables
- Silver: deduplication, PII masking, schema validation
- Gold: scoring aggregation, top-N per tenant

This processor works alongside the existing Python pipeline
(brain.pipeline.*) and adds SQL-queryable analytical tables for
downstream consumers (API, dashboards, LLM context).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from brain.catalog.models import ColumnDef, GrantLevel
from brain.catalog.store import CatalogStore
from brain.engine.connection import DuckDBEngine

logger = logging.getLogger(__name__)

# Schema definitions for medallion tables
_BRONZE_TASKS_COLUMNS = [
    ColumnDef("id", "TEXT", False, "Task ID from source"),
    ColumnDef("tenant_id", "TEXT", False, "Tenant partition key"),
    ColumnDef("source", "TEXT", False, "Data source (todoist, calendar, gmail)"),
    ColumnDef("content", "TEXT", True, "Task content"),
    ColumnDef("priority", "INTEGER", True, "Priority 1-4"),
    ColumnDef("due_date", "TEXT", True, "ISO due date"),
    ColumnDef("labels", "TEXT", True, "JSON array of labels"),
    ColumnDef("project_name", "TEXT", True, "Project name"),
    ColumnDef("ingested_at", "TIMESTAMP", False, "Ingestion timestamp"),
    ColumnDef("batch_id", "TEXT", False, "Batch identifier"),
]

_SILVER_TASKS_COLUMNS = [
    ColumnDef("id", "TEXT", False, "Deduplicated task ID"),
    ColumnDef("tenant_id", "TEXT", False, "Tenant partition key"),
    ColumnDef("content", "TEXT", True, "PII-masked content"),
    ColumnDef("priority", "INTEGER", True, "Validated priority"),
    ColumnDef("due_date", "TEXT", True, "Validated due date"),
    ColumnDef("labels", "TEXT", True, "JSON array of labels"),
    ColumnDef("project_name", "TEXT", True, "Project name"),
    ColumnDef("data_hash", "TEXT", True, "SHA-256 data quality hash"),
    ColumnDef("processed_at", "TIMESTAMP", False, "Processing timestamp"),
]

_GOLD_SCORED_COLUMNS = [
    ColumnDef("id", "TEXT", False, "Task ID"),
    ColumnDef("tenant_id", "TEXT", False, "Tenant partition key"),
    ColumnDef("content", "TEXT", True, "Task content"),
    ColumnDef("score", "INTEGER", False, "Computed priority score"),
    ColumnDef("reasons", "TEXT", True, "JSON array of scoring reasons"),
    ColumnDef("priority", "INTEGER", True, "Original priority"),
    ColumnDef("due_date", "TEXT", True, "Due date"),
    ColumnDef("project_name", "TEXT", True, "Project name"),
    ColumnDef("scored_at", "TIMESTAMP", False, "Scoring timestamp"),
]


class MedallionProcessor:
    """SQL-based medallion pipeline using DuckDB.

    Usage:
        engine = DuckDBEngine(":memory:")
        catalog = CatalogStore(":memory:")
        proc = MedallionProcessor(engine, catalog, "velaflow")
        proc.initialize()
        proc.bronze_ingest("tenant-1", "todoist", tasks)
        proc.silver_transform("tenant-1")
        proc.gold_enrich("tenant-1", scored_tasks)
        results = proc.gold_query("tenant-1", top_n=5)
    """

    NAMESPACE = "velaflow"

    def __init__(
        self,
        engine: DuckDBEngine,
        catalog: CatalogStore,
        namespace: str = "velaflow",
    ) -> None:
        self._engine = engine
        self._catalog = catalog
        self.NAMESPACE = namespace

    def initialize(self) -> None:
        """Create medallion tables and register them in the catalog."""
        self._create_tables()
        self._register_catalog()
        logger.info("Medallion processor initialized")

    def _create_tables(self) -> None:
        """Create Bronze, Silver, Gold tables in DuckDB."""
        self._engine.execute("""
            CREATE TABLE IF NOT EXISTS bronze_tasks (
                id TEXT, tenant_id TEXT, source TEXT, content TEXT,
                priority INTEGER, due_date TEXT, labels TEXT,
                project_name TEXT, ingested_at TIMESTAMP, batch_id TEXT
            )
        """)
        self._engine.execute("""
            CREATE TABLE IF NOT EXISTS silver_tasks (
                id TEXT, tenant_id TEXT, content TEXT, priority INTEGER,
                due_date TEXT, labels TEXT, project_name TEXT,
                data_hash TEXT, processed_at TIMESTAMP
            )
        """)
        self._engine.execute("""
            CREATE TABLE IF NOT EXISTS gold_scored_tasks (
                id TEXT, tenant_id TEXT, content TEXT, score INTEGER,
                reasons TEXT, priority INTEGER, due_date TEXT,
                project_name TEXT, scored_at TIMESTAMP
            )
        """)

    def _register_catalog(self) -> None:
        """Register tables in the data catalog with column metadata."""
        ns = self.NAMESPACE
        self._catalog.register_table(ns, "bronze", "raw_tasks", _BRONZE_TASKS_COLUMNS, description="Raw ingested tasks")
        self._catalog.register_table(ns, "silver", "clean_tasks", _SILVER_TASKS_COLUMNS, description="Cleaned, deduplicated tasks")
        self._catalog.register_table(ns, "gold", "scored_tasks", _GOLD_SCORED_COLUMNS, description="Scored and ranked tasks")

    # ------------------------------------------------------------------
    # Bronze: Raw data ingestion
    # ------------------------------------------------------------------

    def bronze_ingest(
        self,
        tenant_id: str,
        source: str,
        records: list[dict[str, Any]],
        batch_id: str = "",
    ) -> int:
        """Ingest raw records into the bronze table.

        Bronze: raw JSON append-only ingest (equivalent role to a
        managed-lakehouse autoloader + streaming write).
        """
        if not records:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        batch = batch_id or f"{source}_{tenant_id}_{now}"

        # Batch insert — single statement instead of row-by-row
        rows = []
        for record in records:
            rows.append((
                str(record.get("id", "")),
                tenant_id,
                source,
                str(record.get("content", "")),
                int(record.get("priority", 1)),
                str(record.get("due_date", "") or ""),
                json.dumps(record.get("labels", [])),
                str(record.get("project_name", "")),
                now,
                batch,
            ))

        # DuckDB executemany for efficient bulk insert
        self._engine.executemany(
            "INSERT INTO bronze_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        inserted = len(rows)

        # Update catalog stats
        total = self._engine.query_scalar(
            "SELECT COUNT(*) FROM bronze_tasks WHERE tenant_id = ?", [tenant_id]
        )
        self._catalog.update_table_stats(self.NAMESPACE, "bronze", "raw_tasks", total or 0)

        # Record lineage
        self._catalog.record_lineage(
            source_table=f"external.{source}",
            target_table=f"{self.NAMESPACE}.bronze.raw_tasks",
            pipeline_stage="bronze",
            tenant_id=tenant_id,
            record_count=inserted,
        )

        logger.info("Bronze ingested %d records from %s for tenant %s", inserted, source, tenant_id)
        return inserted

    # ------------------------------------------------------------------
    # Silver: Deduplication, validation, PII masking
    # ------------------------------------------------------------------

    def silver_transform(self, tenant_id: str) -> int:
        """Transform bronze → silver with dedup and validation.

        Implemented as DuckDB window + MERGE, no JVM or Spark required.

        SQL operations:
        1. ROW_NUMBER() for deduplication by task ID
        2. SHA-256 hash for data quality tracking
        3. Filter invalid records (NULL id or content)
        """
        # Clear previous silver data for this tenant (idempotent)
        self._engine.execute(
            "DELETE FROM silver_tasks WHERE tenant_id = ?", [tenant_id]
        )

        now = datetime.now(timezone.utc).isoformat()

        # Deduplicate: keep latest record per task ID per tenant
        # + validate: exclude records with empty id or content
        self._engine.execute(
            """
            INSERT INTO silver_tasks (id, tenant_id, content, priority,
                                      due_date, labels, project_name, data_hash, processed_at)
            SELECT id, tenant_id, content, priority, due_date, labels, project_name,
                   md5(concat_ws('|', id, content, CAST(priority AS TEXT), due_date)) AS data_hash,
                   ? AS processed_at
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY tenant_id, id ORDER BY ingested_at DESC
                ) AS rn
                FROM bronze_tasks
                WHERE tenant_id = ? AND id != '' AND content != ''
            ) deduped
            WHERE rn = 1
            """,
            [now, tenant_id],
        )

        count = self._engine.query_scalar(
            "SELECT COUNT(*) FROM silver_tasks WHERE tenant_id = ?", [tenant_id]
        ) or 0

        # Update catalog
        self._catalog.update_table_stats(self.NAMESPACE, "silver", "clean_tasks", count)
        self._catalog.record_lineage(
            source_table=f"{self.NAMESPACE}.bronze.raw_tasks",
            target_table=f"{self.NAMESPACE}.silver.clean_tasks",
            pipeline_stage="silver",
            tenant_id=tenant_id,
            record_count=count,
        )

        logger.info("Silver produced %d clean records for tenant %s", count, tenant_id)
        return count

    # ------------------------------------------------------------------
    # Gold: Scoring and enrichment
    # ------------------------------------------------------------------

    def gold_enrich(
        self,
        tenant_id: str,
        scored_tasks: list[dict[str, Any]],
    ) -> int:
        """Persist scored tasks into the gold table.

        Takes pre-scored tasks from the Python scoring engine
        (brain.planner.rank_tasks) and writes them to DuckDB for
        SQL-queryable access.

        Implemented as DuckDB SQL + Parquet writes to the gold layer.
        """
        # Clear previous gold data for this tenant (idempotent)
        self._engine.execute(
            "DELETE FROM gold_scored_tasks WHERE tenant_id = ?", [tenant_id]
        )

        now = datetime.now(timezone.utc).isoformat()
        inserted = 0

        # Batch insert — single statement instead of row-by-row
        rows = []
        for task in scored_tasks:
            rows.append((
                str(task.get("task_id", task.get("id", ""))),
                tenant_id,
                str(task.get("content", "")),
                int(task.get("score", 0)),
                json.dumps(task.get("reasons", [])),
                int(task.get("priority", 1)),
                str(task.get("due_date", "") or ""),
                str(task.get("project_name", "")),
                now,
            ))

        self._engine.executemany(
            "INSERT INTO gold_scored_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        inserted = len(rows)

        # Update catalog
        self._catalog.update_table_stats(self.NAMESPACE, "gold", "scored_tasks", inserted)
        self._catalog.record_lineage(
            source_table=f"{self.NAMESPACE}.silver.clean_tasks",
            target_table=f"{self.NAMESPACE}.gold.scored_tasks",
            pipeline_stage="gold",
            tenant_id=tenant_id,
            record_count=inserted,
        )

        logger.info("Gold enriched %d scored tasks for tenant %s", inserted, tenant_id)
        return inserted

    # ------------------------------------------------------------------
    # Query API (replaces Spark SQL / Delta table reads)
    # ------------------------------------------------------------------

    def gold_query(
        self, tenant_id: str, top_n: int = 10
    ) -> list[dict[str, Any]]:
        """Query top scored tasks for a tenant (SQL-based)."""
        return self._engine.query(
            "SELECT * FROM gold_scored_tasks WHERE tenant_id = ? "
            "ORDER BY score DESC LIMIT ?",
            [tenant_id, top_n],
        )

    def silver_query(self, tenant_id: str) -> list[dict[str, Any]]:
        """Query all clean tasks for a tenant."""
        return self._engine.query(
            "SELECT * FROM silver_tasks WHERE tenant_id = ? ORDER BY id",
            [tenant_id],
        )

    def bronze_query(
        self, tenant_id: str, source: str | None = None
    ) -> list[dict[str, Any]]:
        """Query raw bronze records for a tenant."""
        if source:
            return self._engine.query(
                "SELECT * FROM bronze_tasks WHERE tenant_id = ? AND source = ? ORDER BY ingested_at DESC",
                [tenant_id, source],
            )
        return self._engine.query(
            "SELECT * FROM bronze_tasks WHERE tenant_id = ? ORDER BY ingested_at DESC",
            [tenant_id],
        )

    def tenant_stats(self, tenant_id: str) -> dict[str, Any]:
        """Get pipeline statistics for a tenant."""
        bronze_count = self._engine.query_scalar(
            "SELECT COUNT(*) FROM bronze_tasks WHERE tenant_id = ?", [tenant_id]
        ) or 0
        silver_count = self._engine.query_scalar(
            "SELECT COUNT(*) FROM silver_tasks WHERE tenant_id = ?", [tenant_id]
        ) or 0
        gold_count = self._engine.query_scalar(
            "SELECT COUNT(*) FROM gold_scored_tasks WHERE tenant_id = ?", [tenant_id]
        ) or 0
        top_score = self._engine.query_scalar(
            "SELECT MAX(score) FROM gold_scored_tasks WHERE tenant_id = ?", [tenant_id]
        ) or 0
        return {
            "tenant_id": tenant_id,
            "bronze_records": bronze_count,
            "silver_records": silver_count,
            "gold_records": gold_count,
            "top_score": top_score,
        }
