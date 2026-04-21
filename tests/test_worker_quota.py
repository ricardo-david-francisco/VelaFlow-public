"""Tests for QueueWorker quota enforcement — _check_quota, _persist_usage, _load_usage."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from brain.config import Settings
from brain.queue.tasks import TaskQueue
from brain.queue.worker import QueueWorker, _daily_usage
from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager
from brain.tenant.models import TenantTier


@pytest.fixture()
def workspace(tmp_path):
    storage = LocalStorageBackend(str(tmp_path / "data"))
    master_key = FieldEncryptor.generate_master_key()
    encryptor = FieldEncryptor(master_key)
    mgr = TenantManager(storage, encryptor)
    settings = Settings.from_env()
    queue = TaskQueue()
    with patch.dict(os.environ, {"VELAFLOW_MASTER_KEY": master_key}):
        worker = QueueWorker(queue, storage, settings)
    return {
        "storage": storage,
        "manager": mgr,
        "worker": worker,
    }


@pytest.fixture(autouse=True)
def _clear_usage():
    """Clear module-level usage between tests."""
    _daily_usage.clear()
    yield
    _daily_usage.clear()


class TestQuotaEnforcement:
    def test_first_call_allowed(self, workspace):
        tenant = workspace["manager"].create_tenant("Q1", "q1@t.com", TenantTier.FREE)
        assert workspace["worker"]._check_quota(tenant, "pipeline_run") is True

    def test_quota_exhausted_blocked(self, workspace):
        tenant = workspace["manager"].create_tenant("Q2", "q2@t.com", TenantTier.FREE)
        # FREE tier: 3 pipeline runs / day
        for _ in range(3):
            assert workspace["worker"]._check_quota(tenant, "pipeline_run") is True
        assert workspace["worker"]._check_quota(tenant, "pipeline_run") is False

    def test_llm_quota_exhausted(self, workspace):
        tenant = workspace["manager"].create_tenant("Q3", "q3@t.com", TenantTier.FREE)
        # FREE tier: 5 LLM calls / day
        for _ in range(5):
            assert workspace["worker"]._check_quota(tenant, "llm_call") is True
        assert workspace["worker"]._check_quota(tenant, "llm_call") is False

    def test_premium_has_higher_quota(self, workspace):
        tenant = workspace["manager"].create_tenant("Q4", "q4@t.com", TenantTier.PREMIUM)
        # PREMIUM tier: 100 pipeline runs / day
        for _ in range(50):
            assert workspace["worker"]._check_quota(tenant, "pipeline_run") is True
        assert workspace["worker"]._check_quota(tenant, "pipeline_run") is True

    def test_persist_and_load_usage(self, workspace):
        tenant = workspace["manager"].create_tenant("Q5", "q5@t.com", TenantTier.FREE)
        # Use some quota
        workspace["worker"]._check_quota(tenant, "pipeline_run")
        workspace["worker"]._check_quota(tenant, "llm_call")
        workspace["worker"]._persist_usage(tenant.tenant_id)

        # Clear in-memory and reload
        _daily_usage.clear()
        workspace["worker"]._load_usage(tenant.tenant_id)
        usage = _daily_usage.get(tenant.tenant_id)
        assert usage is not None
        assert usage["pipeline_runs"] == 1
        assert usage["llm_calls"] == 1

    def test_store_job_result(self, workspace):
        tenant = workspace["manager"].create_tenant("Q6", "q6@t.com")
        workspace["worker"]._store_job_result(
            tenant.tenant_id, "msg-001", {"status": "ok", "digest": "Hello"}
        )
        data = workspace["storage"].read_json(
            f"tenants/{tenant.tenant_id}/job_results/msg-001.json"
        )
        assert data["status"] == "ok"
        assert data["digest"] == "Hello"
