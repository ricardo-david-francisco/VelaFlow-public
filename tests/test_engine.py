"""Tests for brain.engine — DuckDB Processing Engine (Spark replacement)."""

import pytest

from brain.catalog.store import CatalogStore
from brain.engine.connection import DuckDBEngine, DUCKDB_AVAILABLE
from brain.engine.processor import MedallionProcessor


pytestmark = pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="DuckDB not installed")


@pytest.fixture
def engine():
    """Create an in-memory DuckDB engine."""
    eng = DuckDBEngine(db_path=None, memory_limit="128MB")
    yield eng
    eng.close()


@pytest.fixture
def catalog(tmp_path):
    """Create a temporary catalog store."""
    store = CatalogStore(tmp_path / "test.db")
    store.bootstrap_velaflow()
    yield store
    store.close()


@pytest.fixture
def processor(engine, catalog):
    """Create a medallion processor with DuckDB engine and catalog."""
    proc = MedallionProcessor(engine, catalog)
    proc.initialize()
    return proc


# ── DuckDB Engine Tests ────────────────────────────────────────────────

class TestDuckDBEngine:
    def test_create_table(self, engine):
        engine.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        assert engine.table_exists("t")

    def test_table_not_exists(self, engine):
        assert not engine.table_exists("nonexistent")

    def test_insert_and_query(self, engine):
        engine.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        engine.execute("INSERT INTO t VALUES (?, ?)", [1, "alice"])
        engine.execute("INSERT INTO t VALUES (?, ?)", [2, "bob"])
        rows = engine.query("SELECT * FROM t ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["name"] == "alice"

    def test_query_scalar(self, engine):
        engine.execute("CREATE TABLE t (id INTEGER)")
        engine.execute("INSERT INTO t VALUES (1)")
        engine.execute("INSERT INTO t VALUES (2)")
        count = engine.query_scalar("SELECT COUNT(*) FROM t")
        assert count == 2

    def test_query_scalar_empty(self, engine):
        engine.execute("CREATE TABLE t (id INTEGER)")
        result = engine.query_scalar("SELECT MAX(id) FROM t")
        assert result is None

    def test_parameterized_query(self, engine):
        engine.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        engine.execute("INSERT INTO t VALUES (1, 'alice')")
        engine.execute("INSERT INTO t VALUES (2, 'bob')")
        rows = engine.query("SELECT * FROM t WHERE name = ?", ["alice"])
        assert len(rows) == 1

    def test_file_based_engine(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        eng = DuckDBEngine(db_path=db_path, memory_limit="64MB")
        eng.execute("CREATE TABLE t (id INTEGER)")
        eng.execute("INSERT INTO t VALUES (1)")
        eng.close()
        assert db_path.exists()


# ── Medallion Processor Tests ──────────────────────────────────────────

class TestMedallionProcessor:
    def test_initialize_creates_tables(self, processor, engine):
        assert engine.table_exists("bronze_tasks")
        assert engine.table_exists("silver_tasks")
        assert engine.table_exists("gold_scored_tasks")

    def test_bronze_ingest(self, processor):
        tasks = [
            {"id": "1", "content": "Buy milk", "priority": 3},
            {"id": "2", "content": "Write report", "priority": 4},
        ]
        count = processor.bronze_ingest("t1", "todoist", tasks)
        assert count == 2

    def test_bronze_ingest_empty(self, processor):
        count = processor.bronze_ingest("t1", "todoist", [])
        assert count == 0

    def test_bronze_query(self, processor):
        tasks = [{"id": "1", "content": "Test task", "priority": 2}]
        processor.bronze_ingest("t1", "todoist", tasks)
        rows = processor.bronze_query("t1", "todoist")
        assert len(rows) == 1
        assert rows[0]["content"] == "Test task"

    def test_bronze_query_tenant_isolation(self, processor):
        processor.bronze_ingest("t1", "todoist", [{"id": "1", "content": "T1 task"}])
        processor.bronze_ingest("t2", "todoist", [{"id": "2", "content": "T2 task"}])
        t1_rows = processor.bronze_query("t1")
        t2_rows = processor.bronze_query("t2")
        assert len(t1_rows) == 1
        assert len(t2_rows) == 1
        assert t1_rows[0]["content"] == "T1 task"

    def test_silver_transform(self, processor):
        tasks = [
            {"id": "1", "content": "Task A", "priority": 3},
            {"id": "2", "content": "Task B", "priority": 4},
            {"id": "1", "content": "Task A updated", "priority": 3},  # Duplicate
        ]
        processor.bronze_ingest("t1", "todoist", tasks)
        count = processor.silver_transform("t1")
        assert count == 2  # Deduplication keeps 2 unique IDs

    def test_silver_transform_excludes_empty(self, processor):
        tasks = [
            {"id": "1", "content": "Valid task"},
            {"id": "", "content": "No ID"},       # Empty ID
            {"id": "3", "content": ""},            # Empty content
        ]
        processor.bronze_ingest("t1", "todoist", tasks)
        count = processor.silver_transform("t1")
        assert count == 1  # Only "Valid task" passes

    def test_silver_idempotent(self, processor):
        tasks = [{"id": "1", "content": "Task"}]
        processor.bronze_ingest("t1", "todoist", tasks)
        processor.silver_transform("t1")
        processor.silver_transform("t1")  # Run again
        rows = processor.silver_query("t1")
        assert len(rows) == 1  # No duplicates

    def test_gold_enrich(self, processor):
        scored = [
            {"task_id": "1", "content": "Buy milk", "score": 85, "reasons": ["urgent"], "priority": 3},
            {"task_id": "2", "content": "Write report", "score": 92, "reasons": ["due today"], "priority": 4},
        ]
        count = processor.gold_enrich("t1", scored)
        assert count == 2

    def test_gold_query_ordered_by_score(self, processor):
        scored = [
            {"task_id": "1", "content": "Low", "score": 30, "priority": 1},
            {"task_id": "2", "content": "High", "score": 95, "priority": 4},
            {"task_id": "3", "content": "Mid", "score": 60, "priority": 2},
        ]
        processor.gold_enrich("t1", scored)
        results = processor.gold_query("t1", top_n=2)
        assert len(results) == 2
        assert results[0]["score"] == 95
        assert results[1]["score"] == 60

    def test_tenant_stats(self, processor):
        processor.bronze_ingest("t1", "todoist", [
            {"id": "1", "content": "A"},
            {"id": "2", "content": "B"},
        ])
        processor.silver_transform("t1")
        processor.gold_enrich("t1", [
            {"task_id": "1", "content": "A", "score": 80, "priority": 3},
        ])
        stats = processor.tenant_stats("t1")
        assert stats["bronze_records"] == 2
        assert stats["silver_records"] == 2
        assert stats["gold_records"] == 1
        assert stats["top_score"] == 80

    def test_full_pipeline(self, processor):
        """End-to-end: ingest → transform → enrich → query."""
        # Bronze
        tasks = [
            {"id": "1", "content": "Review PR", "priority": 4, "project_name": "Engineering"},
            {"id": "2", "content": "Update docs", "priority": 2, "project_name": "Docs"},
            {"id": "3", "content": "Fix bug", "priority": 4, "due_date": "2025-01-15"},
        ]
        assert processor.bronze_ingest("t1", "todoist", tasks) == 3

        # Silver
        silver_count = processor.silver_transform("t1")
        assert silver_count == 3

        # Gold (simulate scoring)
        scored = [
            {"task_id": "1", "content": "Review PR", "score": 85, "reasons": ["high priority"], "priority": 4},
            {"task_id": "3", "content": "Fix bug", "score": 92, "reasons": ["due soon", "high priority"], "priority": 4},
            {"task_id": "2", "content": "Update docs", "score": 40, "reasons": ["low priority"], "priority": 2},
        ]
        assert processor.gold_enrich("t1", scored) == 3

        # Query
        top = processor.gold_query("t1", top_n=2)
        assert len(top) == 2
        assert top[0]["content"] == "Fix bug"
        assert top[0]["score"] == 92


# ── Catalog Integration Tests ──────────────────────────────────────────

class TestCatalogIntegration:
    def test_catalog_tables_registered(self, processor, catalog):
        tables = catalog.list_tables("velaflow", "bronze")
        assert any(t.name == "raw_tasks" for t in tables)

    def test_lineage_recorded_after_ingest(self, processor, catalog):
        processor.bronze_ingest("t1", "todoist", [{"id": "1", "content": "Test"}])
        lineage = catalog.get_lineage("velaflow.bronze.raw_tasks")
        assert len(lineage) >= 1
        assert lineage[0].pipeline_stage == "bronze"

    def test_lineage_full_pipeline(self, processor, catalog):
        processor.bronze_ingest("t1", "todoist", [{"id": "1", "content": "Test"}])
        processor.silver_transform("t1")
        processor.gold_enrich("t1", [{"task_id": "1", "content": "Test", "score": 50, "priority": 1}])
        lineage = catalog.get_full_lineage("t1")
        stages = [r.pipeline_stage for r in lineage]
        assert "bronze" in stages
        assert "silver" in stages
        assert "gold" in stages

    def test_table_stats_updated(self, processor, catalog):
        processor.bronze_ingest("t1", "todoist", [
            {"id": "1", "content": "A"},
            {"id": "2", "content": "B"},
        ])
        table = catalog.get_table("velaflow", "bronze", "raw_tasks")
        assert table.row_count == 2
