"""Tests for the Bronze pipeline layer."""

from __future__ import annotations

import pytest

from brain.pipeline.bronze import BronzeLayer
from brain.storage.local import LocalStorageBackend
from brain.tenant.models import Tenant, TenantTier


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def bronze(storage):
    return BronzeLayer(storage)


@pytest.fixture()
def tenant():
    return Tenant(
        tenant_id="tn_test001",
        name="Test Corp",
        email="admin@test.com",
        tier=TenantTier.STANDARD,
    )


def _sample_tasks():
    return [
        {"id": "t1", "content": "Buy groceries", "priority": 4},
        {"id": "t2", "content": "Write report", "priority": 2},
    ]


def _sample_events():
    return [
        {"summary": "Team standup", "start": "2024-01-15T09:00:00", "end": "2024-01-15T09:30:00"},
    ]


def _sample_emails():
    return [
        {"subject": "Urgent: Deploy fix", "sender": "ops@company.com"},
    ]


class TestBronzeIngestion:
    def test_ingest_todoist(self, bronze, tenant, storage):
        batch_id = bronze.ingest_todoist(tenant, _sample_tasks())
        assert batch_id is not None
        assert "todoist" in batch_id

        data = bronze.read_latest(tenant.tenant_id, "todoist")
        assert data is not None
        assert data["source"] == "todoist"
        assert len(data["data"]["tasks"]) == 2

    def test_ingest_calendar(self, bronze, tenant):
        batch_id = bronze.ingest_calendar(tenant, _sample_events())
        assert "calendar" in batch_id

        data = bronze.read_latest(tenant.tenant_id, "calendar")
        assert data is not None
        assert len(data["data"]["events"]) == 1

    def test_ingest_gmail(self, bronze, tenant):
        batch_id = bronze.ingest_gmail(tenant, _sample_emails())
        assert "gmail" in batch_id

        data = bronze.read_latest(tenant.tenant_id, "gmail")
        assert data is not None
        assert len(data["data"]["emails"]) == 1

    def test_list_batches(self, bronze, tenant):
        bronze.ingest_todoist(tenant, _sample_tasks())
        batches = bronze.list_batches(tenant.tenant_id, "todoist")
        assert len(batches) == 1

    def test_read_latest_empty(self, bronze):
        assert bronze.read_latest("nonexistent", "todoist") is None

    def test_ingest_with_projects_and_sections(self, bronze, tenant):
        projects = [{"id": "p1", "name": "Work"}]
        sections = [{"id": "s1", "name": "In Progress"}]
        batch_id = bronze.ingest_todoist(
            tenant, _sample_tasks(), projects, sections
        )
        data = bronze.read_latest(tenant.tenant_id, "todoist")
        assert len(data["data"]["projects"]) == 1
        assert len(data["data"]["sections"]) == 1

    def test_tenant_isolation(self, bronze, storage):
        t1 = Tenant(tenant_id="tn_a", name="A", email="a@a.com")
        t2 = Tenant(tenant_id="tn_b", name="B", email="b@b.com")

        bronze.ingest_todoist(t1, [{"id": "1", "content": "Task A"}])
        bronze.ingest_todoist(t2, [{"id": "2", "content": "Task B"}])

        data_a = bronze.read_latest("tn_a", "todoist")
        data_b = bronze.read_latest("tn_b", "todoist")
        assert data_a["data"]["tasks"][0]["content"] == "Task A"
        assert data_b["data"]["tasks"][0]["content"] == "Task B"
