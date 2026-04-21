"""Tests for the demo account lifecycle manager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend
from brain.tenant.demo_manager import DemoManager
from brain.tenant.manager import TenantManager


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def encryptor():
    return FieldEncryptor(FieldEncryptor.generate_master_key())


@pytest.fixture()
def tenant_mgr(storage, encryptor):
    return TenantManager(storage, encryptor)


@pytest.fixture()
def demo_mgr(tenant_mgr, storage, encryptor):
    return DemoManager(tenant_mgr, storage, encryptor)


class TestDemoManager:
    """Verify demo account creation, expiry, and cost caps."""

    def test_create_demo_sets_vip_tier(self, demo_mgr, tenant_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Prospect",
            email="prospect@example.com",
            created_by="admin@velaflow.com",
        )
        assert tenant.tier.value == "vip"

    def test_create_demo_sets_demo_flags(self, demo_mgr, storage) -> None:
        tenant = demo_mgr.create_demo(
            name="Test Demo",
            email="demo@example.com",
            created_by="admin@velaflow.com",
            duration_days=3,
            cost_cap_pipeline=25,
            cost_cap_llm=50,
        )
        # Read raw storage to verify demo fields
        data = storage.read_json(f"tenants/{tenant.tenant_id}.json")
        assert data["is_demo"] is True
        assert data["demo_cost_cap_pipeline"] == 25
        assert data["demo_cost_cap_llm"] == 50
        assert data["demo_created_by"] == "admin@velaflow.com"
        assert data["demo_expires_at"]  # Should be non-empty

    def test_check_expiry_active(self, demo_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Active",
            email="active@example.com",
            created_by="admin",
            duration_days=7,
        )
        assert demo_mgr.check_expiry(tenant.tenant_id) is True

    def test_check_expiry_expired(self, demo_mgr, storage) -> None:
        tenant = demo_mgr.create_demo(
            name="Expired",
            email="expired@example.com",
            created_by="admin",
            duration_days=7,
        )
        # Manually set expiry to the past
        raw = storage.read_json(f"tenants/{tenant.tenant_id}.json")
        raw["demo_expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        storage.write_json(
            f"tenants/{tenant.tenant_id}.json",
            raw,
        )
        assert demo_mgr.check_expiry(tenant.tenant_id) is False

    def test_check_expiry_nonexistent_tenant(self, demo_mgr) -> None:
        assert demo_mgr.check_expiry("tn_nonexistent") is False

    def test_cost_cap_within_limit(self, demo_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Cap Test",
            email="cap@example.com",
            created_by="admin",
            cost_cap_pipeline=50,
        )
        assert demo_mgr.check_demo_cost_cap(
            tenant.tenant_id, "pipeline_run", 10,
        ) is True

    def test_cost_cap_exceeded(self, demo_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Cap Exceeded",
            email="cap2@example.com",
            created_by="admin",
            cost_cap_pipeline=5,
        )
        assert demo_mgr.check_demo_cost_cap(
            tenant.tenant_id, "pipeline_run", 5,
        ) is False

    def test_cost_cap_non_demo_always_passes(self, tenant_mgr, demo_mgr) -> None:
        from brain.tenant.models import TenantTier
        tenant = tenant_mgr.create_tenant("Normal", "normal@example.com", TenantTier.FREE)
        assert demo_mgr.check_demo_cost_cap(
            tenant.tenant_id, "pipeline_run", 9999,
        ) is True

    def test_usage_analytics(self, demo_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Analytics",
            email="analytics@example.com",
            created_by="admin",
        )
        analytics = demo_mgr.get_usage_analytics(tenant.tenant_id)
        assert analytics["tenant_id"] == tenant.tenant_id
        assert analytics["is_demo"] is True
        assert analytics["total_events"] >= 1  # At least the creation event
        assert "demo_created" in analytics["event_types"]

    def test_log_demo_error(self, demo_mgr) -> None:
        tenant = demo_mgr.create_demo(
            name="Error Test",
            email="error@example.com",
            created_by="admin",
        )
        demo_mgr.log_demo_error(
            tenant.tenant_id,
            "pipeline_failure",
            "Bronze stage timed out",
            {"stage": "bronze"},
        )
        analytics = demo_mgr.get_usage_analytics(tenant.tenant_id)
        assert len(analytics["errors"]) == 1
        assert analytics["errors"][0]["data"]["error_type"] == "pipeline_failure"

    def test_list_demo_accounts(self, demo_mgr, tenant_mgr) -> None:
        from brain.tenant.models import TenantTier
        # Create one demo and one normal account
        demo_mgr.create_demo(
            name="Demo User",
            email="demo@example.com",
            created_by="admin",
        )
        tenant_mgr.create_tenant("Normal", "normal@example.com", TenantTier.FREE)

        demos = demo_mgr.list_demo_accounts()
        assert len(demos) == 1
        assert demos[0]["name"] == "Demo User"
        assert demos[0]["is_expired"] is False

    def test_create_demo_with_litellm_token(
        self, demo_mgr, tenant_mgr, encryptor
    ) -> None:
        tenant = demo_mgr.create_demo(
            name="Token Demo",
            email="token@example.com",
            created_by="admin",
            litellm_token="sk-test-token-123",
        )
        updated = tenant_mgr.get_tenant(tenant.tenant_id)
        # Token should be stored encrypted via the credential vault
        assert updated is not None
        assert updated.config.litellm_proxy_token_encrypted != ""
        decrypted = tenant_mgr.decrypt_credential(
            updated,
            updated.config.litellm_proxy_token_encrypted,
            "litellm_proxy_token",
        )
        assert decrypted == "sk-test-token-123"
