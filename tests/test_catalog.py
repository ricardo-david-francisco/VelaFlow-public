"""Tests for brain.catalog — on-prem Local Data Catalog."""

import os
import tempfile
from pathlib import Path

import pytest

from brain.catalog.models import (
    CatalogGrant,
    CatalogNamespace,
    CatalogSchema,
    CatalogTable,
    ColumnDef,
    GrantLevel,
    LineageRecord,
)
from brain.catalog.store import CatalogStore


@pytest.fixture
def catalog(tmp_path):
    """Create a temporary catalog store."""
    db_path = tmp_path / "test_catalog.db"
    store = CatalogStore(db_path)
    yield store
    store.close()


@pytest.fixture
def bootstrapped_catalog(catalog):
    """Catalog with default VelaFlow schemas and grants."""
    catalog.bootstrap_velaflow()
    return catalog


# ── Model Tests ────────────────────────────────────────────────────────

class TestCatalogModels:
    def test_column_def_frozen(self):
        col = ColumnDef("id", "TEXT", False, "Primary key")
        assert col.name == "id"
        assert col.dtype == "TEXT"
        assert col.nullable is False

    def test_catalog_table_full_name(self):
        table = CatalogTable(namespace="ns", schema_name="bronze", name="raw_tasks")
        assert table.full_name == "ns.bronze.raw_tasks"

    def test_grant_level_values(self):
        assert GrantLevel.SELECT.value == "SELECT"
        assert GrantLevel.ALL.value == "ALL"

    def test_lineage_record_defaults(self):
        record = LineageRecord(
            source_table="a.b.c",
            target_table="a.b.d",
            pipeline_stage="silver",
        )
        assert record.tenant_id == ""
        assert record.record_count == 0


# ── Namespace Tests ────────────────────────────────────────────────────

class TestNamespaces:
    def test_create_namespace(self, catalog):
        ns = catalog.create_namespace("test_ns", owner="admin")
        assert ns.name == "test_ns"
        assert ns.owner == "admin"

    def test_get_namespace(self, catalog):
        catalog.create_namespace("ns1")
        result = catalog.get_namespace("ns1")
        assert result is not None
        assert result.name == "ns1"

    def test_get_nonexistent_namespace(self, catalog):
        assert catalog.get_namespace("nope") is None

    def test_list_namespaces(self, catalog):
        catalog.create_namespace("a")
        catalog.create_namespace("b")
        result = catalog.list_namespaces()
        assert len(result) == 2
        assert result[0].name == "a"

    def test_create_duplicate_namespace_no_error(self, catalog):
        catalog.create_namespace("dup")
        catalog.create_namespace("dup")  # INSERT OR IGNORE
        assert len(catalog.list_namespaces()) == 1


# ── Schema Tests ───────────────────────────────────────────────────────

class TestSchemas:
    def test_create_schema(self, catalog):
        catalog.create_namespace("ns")
        schema = catalog.create_schema("ns", "bronze", "Raw data")
        assert schema.namespace == "ns"
        assert schema.name == "bronze"
        assert schema.description == "Raw data"

    def test_list_schemas(self, catalog):
        catalog.create_namespace("ns")
        catalog.create_schema("ns", "bronze")
        catalog.create_schema("ns", "silver")
        catalog.create_schema("ns", "gold")
        schemas = catalog.list_schemas("ns")
        assert len(schemas) == 3
        assert [s.name for s in schemas] == ["bronze", "gold", "silver"]


# ── Table Tests ────────────────────────────────────────────────────────

class TestTables:
    def test_register_table(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cols = [ColumnDef("id", "TEXT"), ColumnDef("content", "TEXT")]
        table = cat.register_table("velaflow", "bronze", "raw_tasks", columns=cols, row_count=100)
        assert table.full_name == "velaflow.bronze.raw_tasks"
        assert table.row_count == 100
        assert len(table.columns) == 2

    def test_get_table(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.register_table("velaflow", "bronze", "raw_tasks", row_count=50)
        result = cat.get_table("velaflow", "bronze", "raw_tasks")
        assert result is not None
        assert result.row_count == 50

    def test_get_nonexistent_table(self, bootstrapped_catalog):
        assert bootstrapped_catalog.get_table("velaflow", "bronze", "nope") is None

    def test_list_tables(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.register_table("velaflow", "bronze", "raw_tasks")
        cat.register_table("velaflow", "bronze", "raw_events")
        tables = cat.list_tables("velaflow", "bronze")
        assert len(tables) == 2

    def test_update_table_stats(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.register_table("velaflow", "gold", "scored_tasks", row_count=10)
        cat.update_table_stats("velaflow", "gold", "scored_tasks", 42, 1024)
        table = cat.get_table("velaflow", "gold", "scored_tasks")
        assert table.row_count == 42
        assert table.size_bytes == 1024

    def test_register_table_upsert(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.register_table("velaflow", "bronze", "t1", row_count=10)
        cat.register_table("velaflow", "bronze", "t1", row_count=20)
        table = cat.get_table("velaflow", "bronze", "t1")
        assert table.row_count == 20  # Updated, not duplicated


# ── Grant Tests ────────────────────────────────────────────────────────

class TestGrants:
    def test_grant_access(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        grant = cat.grant_access("analyst", "velaflow", "gold", GrantLevel.SELECT)
        assert grant.role == "analyst"
        assert grant.grant_level == GrantLevel.SELECT

    def test_check_access_granted(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        # free has SELECT on gold (from bootstrap)
        assert cat.check_access("free", "velaflow", "gold", GrantLevel.SELECT) is True

    def test_check_access_denied(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        # free does NOT have access to bronze
        assert cat.check_access("free", "velaflow", "bronze", GrantLevel.SELECT) is False

    def test_check_access_all_grant(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        # premium has ALL on gold (from bootstrap)
        assert cat.check_access("premium", "velaflow", "gold", GrantLevel.SELECT) is True
        assert cat.check_access("premium", "velaflow", "gold", GrantLevel.INSERT) is True

    def test_list_grants(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        grants = cat.list_grants("velaflow", "gold")
        assert len(grants) >= 3  # free, standard, premium, admin

    def test_revoke_access(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.grant_access("test_role", "velaflow", "bronze", GrantLevel.SELECT)
        assert cat.check_access("test_role", "velaflow", "bronze", GrantLevel.SELECT)
        revoked = cat.revoke_access("test_role", "velaflow", "bronze", GrantLevel.SELECT)
        assert revoked is True
        assert not cat.check_access("test_role", "velaflow", "bronze", GrantLevel.SELECT)


# ── Lineage Tests ──────────────────────────────────────────────────────

class TestLineage:
    def test_record_lineage(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        record = cat.record_lineage(
            source_table="external.todoist",
            target_table="velaflow.bronze.raw_tasks",
            pipeline_stage="bronze",
            tenant_id="t1",
            record_count=50,
        )
        assert record.source_table == "external.todoist"
        assert record.record_count == 50

    def test_get_lineage(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.record_lineage("a", "velaflow.bronze.raw_tasks", "bronze", "t1", 10)
        cat.record_lineage("b", "velaflow.bronze.raw_tasks", "bronze", "t1", 20)
        lineage = cat.get_lineage("velaflow.bronze.raw_tasks")
        assert len(lineage) == 2

    def test_get_full_lineage(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        cat.record_lineage("ext", "vf.bronze.tasks", "bronze", "t1", 10)
        cat.record_lineage("vf.bronze.tasks", "vf.silver.tasks", "silver", "t1", 8)
        cat.record_lineage("vf.silver.tasks", "vf.gold.scored", "gold", "t1", 8)
        lineage = cat.get_full_lineage("t1")
        assert len(lineage) == 3
        assert lineage[0].pipeline_stage == "bronze"
        assert lineage[2].pipeline_stage == "gold"


# ── Bootstrap Tests ────────────────────────────────────────────────────

class TestBootstrap:
    def test_bootstrap_creates_namespace(self, bootstrapped_catalog):
        ns = bootstrapped_catalog.get_namespace("velaflow")
        assert ns is not None

    def test_bootstrap_creates_schemas(self, bootstrapped_catalog):
        schemas = bootstrapped_catalog.list_schemas("velaflow")
        names = {s.name for s in schemas}
        assert names == {"bronze", "silver", "gold"}

    def test_bootstrap_creates_grants(self, bootstrapped_catalog):
        cat = bootstrapped_catalog
        assert cat.check_access("free", "velaflow", "gold", GrantLevel.SELECT)
        assert not cat.check_access("free", "velaflow", "bronze", GrantLevel.SELECT)
        assert cat.check_access("admin", "velaflow", "bronze", GrantLevel.ALL)

    def test_bootstrap_idempotent(self, bootstrapped_catalog):
        bootstrapped_catalog.bootstrap_velaflow()  # Call again
        schemas = bootstrapped_catalog.list_schemas("velaflow")
        assert len(schemas) == 3  # Not duplicated
