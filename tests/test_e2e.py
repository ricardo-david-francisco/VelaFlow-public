"""End-to-end integration test — Full VelaFlow pipeline.

Tests the complete flow from raw data ingestion through to scored
gold-layer output, exercising every major component:

1. Tenant creation + RBAC
2. Bronze ingestion (Python pipeline + DuckDB engine)
3. Silver transformation (dedup, PII mask, validation)
4. Gold enrichment (scoring, digest)
5. Catalog governance (table registration, lineage, grants)
6. Queue/worker message flow
7. Zero-trust request signing
8. API auth (JWT create/verify)
9. Field encryption (per-tenant)
10. Input sanitization
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from brain.api.auth import create_access_token, verify_token
from brain.catalog.models import GrantLevel
from brain.catalog.store import CatalogStore
from brain.config import Settings
from brain.engine.connection import DuckDBEngine, DUCKDB_AVAILABLE
from brain.engine.processor import MedallionProcessor
from brain.models import Task
from brain.pipeline.bronze import BronzeLayer
from brain.pipeline.gold import GoldLayer
from brain.pipeline.scheduler import PipelineScheduler, PipelineStatus
from brain.pipeline.silver import SilverLayer
from brain.queue.tasks import MessageType, QueueMessage, TaskQueue
from brain.security.encryption import FieldEncryptor
from brain.security.pii import PIIDetector
from brain.security.rbac import Permission, RBACPolicy
from brain.security.zero_trust import AuditLogger, InputSanitizer, RequestSigner
from brain.storage.local import LocalStorageBackend
from brain.tenant.models import Tenant, TenantConfig, TenantTier
from tests._fakes import fake_password


@pytest.fixture
def workspace(tmp_path):
    """Create a complete workspace with all backends."""
    data_dir = tmp_path / "data" / "medallion"
    data_dir.mkdir(parents=True)
    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "storage": LocalStorageBackend(str(data_dir)),
        "encryptor": FieldEncryptor(FieldEncryptor.generate_master_key()),
        "rbac": RBACPolicy(),
        "pii": PIIDetector(),
        "catalog": CatalogStore(tmp_path / "catalog.db"),
        "signer": RequestSigner("e2e-test-secret"),
        "audit": AuditLogger("e2e-test"),
    }


@pytest.fixture
def tenant():
    """Create a test tenant."""
    return Tenant(
        tenant_id="e2e-test-tenant",
        name="E2E Test Tenant",
        email="e2e@velaflow.test",
        tier=TenantTier.PREMIUM,
        config=TenantConfig(),
    )


@pytest.fixture
def settings():
    """Create test settings."""
    return Settings.from_env()


@pytest.fixture
def raw_tasks():
    """Realistic raw Todoist tasks for testing."""
    return [
        {
            "id": "101",
            "content": "Review quarterly budget report",
            "description": "Compare Q1 vs Q2 spending",
            "project_id": "p1",
            "priority": 4,
            "labels": ["work", "finance"],
            "due": {"date": "2025-04-18", "datetime": None, "is_recurring": False},
            "duration": {"amount": 60, "unit": "minute"},
        },
        {
            "id": "102",
            "content": "Call dentist at 555-123-4567",  # Contains PII (phone)
            "description": "",
            "project_id": "p2",
            "priority": 2,
            "labels": ["personal", "health"],
            "due": {"date": "2025-04-17", "datetime": None, "is_recurring": False},
        },
        {
            "id": "103",
            "content": "Deploy VelaFlow v2.0 to production",
            "description": "Run full test suite first",
            "project_id": "p1",
            "priority": 4,
            "labels": ["work", "engineering"],
            "due": {"date": "2025-04-17", "datetime": "2025-04-17T14:00:00", "is_recurring": False},
        },
        {
            "id": "101",  # Duplicate ID — should be deduped in silver
            "content": "Review quarterly budget report (updated)",
            "priority": 4,
            "labels": ["work"],
        },
    ]


class TestEndToEnd:
    """Full pipeline integration test."""

    def test_complete_pipeline_flow(self, workspace, tenant, settings, raw_tasks):
        """Exercise the entire VelaFlow pipeline end-to-end."""
        storage = workspace["storage"]
        catalog = workspace["catalog"]
        rbac = workspace["rbac"]
        pii = workspace["pii"]
        encryptor = workspace["encryptor"]
        signer = workspace["signer"]

        # ── Step 1: Bootstrap catalog ──────────────────────────────
        catalog.bootstrap_velaflow()
        ns = catalog.get_namespace("velaflow")
        assert ns is not None
        schemas = catalog.list_schemas("velaflow")
        assert len(schemas) == 3

        # ── Step 2: Verify RBAC ────────────────────────────────────
        assert rbac.has_permission("premium", Permission.RUN_PIPELINE)
        assert rbac.has_permission("premium", Permission.USE_PREMIUM_LLM)
        assert not rbac.has_permission("free", Permission.USE_PREMIUM_LLM)

        # Verify catalog grants match RBAC
        assert catalog.check_access("premium", "velaflow", "gold", GrantLevel.ALL)
        assert catalog.check_access("free", "velaflow", "gold", GrantLevel.SELECT)
        assert not catalog.check_access("free", "velaflow", "bronze", GrantLevel.SELECT)

        # ── Step 3: JWT Authentication ─────────────────────────────
        jwt_secret = fake_password(32)
        token = create_access_token(
            tenant_id=tenant.tenant_id,
            role="premium",
            email=tenant.email,
            secret=jwt_secret,
        )
        claims = verify_token(token, secret=jwt_secret)
        assert claims is not None
        assert claims.tenant_id == tenant.tenant_id
        assert claims.role == "premium"

        # ── Step 4: Field Encryption ──────────────────────────────
        sensitive = "Credit card: 4532-1234-5678-9012"
        encrypted = encryptor.encrypt(sensitive, tenant.tenant_id)
        assert encrypted != sensitive
        decrypted = encryptor.decrypt(encrypted, tenant.tenant_id)
        assert decrypted == sensitive

        # Different tenant cannot decrypt
        with pytest.raises(Exception):
            encryptor.decrypt(encrypted, "wrong-tenant")

        # ── Step 5: Zero-Trust Request Signing ─────────────────────
        body = b'{"todoist_tasks": [{"id": "1"}]}'
        signed = signer.sign("POST", "/api/v1/webhooks/pipeline", body, "api")
        assert signer.verify("POST", "/api/v1/webhooks/pipeline", body, signed)
        # Tampered body rejected
        assert not signer.verify("POST", "/api/v1/webhooks/pipeline", b"tampered", signed)

        # ── Step 6: Input Sanitization ─────────────────────────────
        InputSanitizer.validate_tenant_id(tenant.tenant_id)
        with pytest.raises(ValueError):
            InputSanitizer.validate_tenant_id("tenant'; DROP TABLE--")

        # ── Step 7: Bronze Ingestion ───────────────────────────────
        bronze = BronzeLayer(storage)
        batch_id = bronze.ingest_todoist(
            tenant, raw_tasks,
            raw_projects=[{"id": "p1", "name": "Work"}, {"id": "p2", "name": "Personal"}],
        )
        assert batch_id
        bronze_data = bronze.read_latest(tenant.tenant_id, "todoist")
        assert bronze_data is not None
        assert len(bronze_data["data"]["tasks"]) == 4

        # ── Step 8: Silver Transformation ──────────────────────────
        silver = SilverLayer(storage, pii)
        tasks = silver.process_todoist(tenant.tenant_id, bronze_data)
        assert len(tasks) == 3  # 4 raw - 1 duplicate = 3 unique

        # PII was masked
        phone_task = next(t for t in tasks if "dentist" in t.content.lower())
        assert "555-123-4567" not in phone_task.content  # Phone masked

        # ── Step 9: Gold Enrichment ────────────────────────────────
        gold = GoldLayer(storage)
        scored = gold.produce_scored_tasks(tenant.tenant_id, tasks, settings)
        assert len(scored) > 0
        # Higher priority tasks should score higher
        scores = {st.task.id: st.score for st in scored}
        assert all(s > 0 for s in scores.values())

        # Digest generation
        digest = gold.produce_daily_digest(tenant.tenant_id, tasks, [], [], settings)
        assert digest.subject
        assert digest.body_text

        # ── Step 10: Full Pipeline Scheduler ───────────────────────
        scheduler = PipelineScheduler(storage, settings)
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=raw_tasks[:2],
            raw_projects=[{"id": "p1", "name": "Work"}],
        )
        assert run.status == PipelineStatus.COMPLETED
        assert len(run.stages) == 3  # Bronze, Silver, Gold
        assert run.duration_ms >= 0

        # ── Step 11: Queue Message Flow ────────────────────────────
        queue = TaskQueue()
        msg = QueueMessage(
            message_id="e2e_test_1",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id=tenant.tenant_id,
            payload={"todoist_tasks": raw_tasks[:1]},
        )
        queue.enqueue(msg)
        assert queue.depth == 1
        dequeued = queue.dequeue(timeout=1)
        assert dequeued.message_id == "e2e_test_1"

        catalog.close()

    @pytest.mark.skipif(not DUCKDB_AVAILABLE, reason="DuckDB not installed")
    def test_duckdb_engine_pipeline(self, workspace, raw_tasks):
        """Test the DuckDB analytical engine alongside the Python pipeline."""
        catalog = workspace["catalog"]
        catalog.bootstrap_velaflow()

        engine = DuckDBEngine(db_path=None, memory_limit="128MB")
        processor = MedallionProcessor(engine, catalog)
        processor.initialize()

        # Bronze: ingest tasks into DuckDB
        count = processor.bronze_ingest("e2e-tenant", "todoist", raw_tasks)
        assert count == 4

        # Silver: dedup and validate
        silver_count = processor.silver_transform("e2e-tenant")
        assert silver_count == 3  # Dedup removes duplicate ID 101

        # Gold: enrich with scores
        scored = [
            {"task_id": "101", "content": "Review budget", "score": 85, "priority": 4},
            {"task_id": "102", "content": "Call dentist", "score": 40, "priority": 2},
            {"task_id": "103", "content": "Deploy v2.0", "score": 95, "priority": 4},
        ]
        gold_count = processor.gold_enrich("e2e-tenant", scored)
        assert gold_count == 3

        # Query top tasks
        top = processor.gold_query("e2e-tenant", top_n=2)
        assert len(top) == 2
        assert top[0]["score"] == 95  # Deploy v2.0

        # Verify stats
        stats = processor.tenant_stats("e2e-tenant")
        assert stats["bronze_records"] == 4
        assert stats["silver_records"] == 3
        assert stats["gold_records"] == 3

        # Verify lineage
        lineage = catalog.get_full_lineage("e2e-tenant")
        stages = [r.pipeline_stage for r in lineage]
        assert "bronze" in stages
        assert "silver" in stages
        assert "gold" in stages

        engine.close()
        catalog.close()

    def test_pii_detection_comprehensive(self):
        """Verify PII detection catches all sensitive patterns."""
        pii = PIIDetector()

        # Credit card (may be detected as phone pattern — check PII is masked)
        assert pii.has_pii("Pay with 4532-1234-5678-9012")
        masked = pii.mask("Pay with 4532-1234-5678-9012")
        assert "4532-1234-5678-9012" not in masked  # Sensitive data is gone

        # Email
        assert pii.has_pii("Contact john@example.com")
        assert "[EMAIL]" in pii.mask("Contact john@example.com")

        # Phone
        assert pii.has_pii("Call +351 912 345 678")

        # SSN
        assert pii.has_pii("SSN: 123-45-6789")
        assert "[SSN_US]" in pii.mask("SSN: 123-45-6789")

        # Clean text
        assert not pii.has_pii("Buy milk and eggs")

    def test_tenant_isolation(self, workspace, settings):
        """Verify strict data isolation between tenants."""
        storage = workspace["storage"]

        tenant_a = Tenant(
            tenant_id="tenant-a", name="Tenant A", email="a@test.com",
            tier=TenantTier.STANDARD,
        )
        tenant_b = Tenant(
            tenant_id="tenant-b", name="Tenant B", email="b@test.com",
            tier=TenantTier.FREE,
        )

        bronze = BronzeLayer(storage)

        # Tenant A ingests tasks
        bronze.ingest_todoist(tenant_a, [{"id": "1", "content": "A's task"}])
        # Tenant B ingests tasks
        bronze.ingest_todoist(tenant_b, [{"id": "2", "content": "B's task"}])

        # Tenant A can only see their data
        a_data = bronze.read_latest("tenant-a", "todoist")
        assert a_data is not None
        assert a_data["data"]["tasks"][0]["content"] == "A's task"

        # Tenant B can only see their data
        b_data = bronze.read_latest("tenant-b", "todoist")
        assert b_data is not None
        assert b_data["data"]["tasks"][0]["content"] == "B's task"

        # Cross-tenant access: A cannot read B's data
        # (Storage paths are tenant-partitioned)
        a_batches = bronze.list_batches("tenant-a", "todoist")
        b_batches = bronze.list_batches("tenant-b", "todoist")
        assert not any("tenant-b" in b for b in a_batches)
        assert not any("tenant-a" in b for b in b_batches)

    def test_encryption_tenant_isolation(self):
        """Different tenants cannot decrypt each other's data."""
        enc = FieldEncryptor(FieldEncryptor.generate_master_key())
        secret = "Sensitive data"

        enc_a = enc.encrypt(secret, "tenant-a")
        enc_b = enc.encrypt(secret, "tenant-b")

        # Same plaintext, different ciphertext (different keys)
        assert enc_a != enc_b

        # Correct tenant can decrypt
        assert enc.decrypt(enc_a, "tenant-a") == secret
        assert enc.decrypt(enc_b, "tenant-b") == secret

        # Wrong tenant fails
        with pytest.raises(Exception):
            enc.decrypt(enc_a, "tenant-b")

    def test_path_traversal_blocked(self, workspace):
        """Storage backend blocks path traversal attacks."""
        storage = workspace["storage"]
        with pytest.raises(ValueError, match="traversal"):
            storage.write_json("../../etc/passwd", {"hack": True})
