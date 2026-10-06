"""Tests for the Tenant Manager and models."""

from __future__ import annotations

import pytest

from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager
from brain.tenant.models import Tenant, TenantConfig, TenantQuota, TenantTier
from tests._fakes import fake_api_key


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def encryptor():
    return FieldEncryptor(FieldEncryptor.generate_master_key())


@pytest.fixture()
def manager(storage, encryptor):
    return TenantManager(storage, encryptor)


class TestTenantModels:
    def test_tier_enum(self):
        assert TenantTier.FREE.value == "free"
        assert TenantTier.PREMIUM.value == "premium"
        assert TenantTier.VIP.value == "vip"

    def test_quota_for_free(self):
        q = TenantQuota.for_tier(TenantTier.FREE)
        assert q.max_pipeline_runs_per_day == 3
        assert q.max_tasks == 100
        assert not q.premium_llm_enabled

    def test_quota_for_vip(self):
        q = TenantQuota.for_tier(TenantTier.VIP)
        assert q.max_pipeline_runs_per_day == 999
        assert q.max_tasks == 50000
        assert q.max_llm_calls_per_day == 999
        assert q.premium_llm_enabled

    def test_quota_for_standard(self):
        q = TenantQuota.for_tier(TenantTier.STANDARD)
        assert q.max_pipeline_runs_per_day == 20
        assert q.max_tasks == 1000

    def test_quota_for_premium(self):
        q = TenantQuota.for_tier(TenantTier.PREMIUM)
        assert q.max_pipeline_runs_per_day == 100
        assert q.premium_llm_enabled

    def test_tenant_post_init_aligns_role(self):
        t = Tenant(
            tenant_id="tn_x",
            name="X",
            email="x@x.com",
            tier=TenantTier.STANDARD,
        )
        assert t.role == "standard"

    def test_tenant_tier_from_string(self):
        t = Tenant(
            tenant_id="tn_x",
            name="X",
            email="x@x.com",
            tier="premium",  # type: ignore
        )
        assert t.tier == TenantTier.PREMIUM
        assert t.role == "premium"


class TestTenantManager:
    def test_create_tenant(self, manager):
        tenant = manager.create_tenant("Acme", "admin@acme.com", TenantTier.STANDARD)
        assert tenant.name == "Acme"
        assert tenant.email == "admin@acme.com"
        assert tenant.tier == TenantTier.STANDARD
        assert tenant.tenant_id.startswith("tn_")
        assert tenant.is_active

    def test_get_tenant(self, manager):
        created = manager.create_tenant("Test", "t@t.com")
        fetched = manager.get_tenant(created.tenant_id)
        assert fetched is not None
        assert fetched.name == "Test"
        assert fetched.email == "t@t.com"

    def test_get_nonexistent(self, manager):
        assert manager.get_tenant("tn_nonexistent") is None

    def test_list_tenants(self, manager):
        manager.create_tenant("A", "a@a.com")
        manager.create_tenant("B", "b@b.com")
        tenants = manager.list_tenants()
        assert len(tenants) == 2

    def test_update_tier(self, manager):
        created = manager.create_tenant("Up", "u@u.com", TenantTier.FREE)
        updated = manager.update_tier(created.tenant_id, TenantTier.PREMIUM)
        assert updated is not None
        assert updated.tier == TenantTier.PREMIUM
        assert updated.role == "premium"

    def test_update_tier_nonexistent(self, manager):
        assert manager.update_tier("tn_nope", TenantTier.STANDARD) is None

    def test_update_config(self, manager):
        created = manager.create_tenant("Cfg", "c@c.com")
        manager.bind_owner_sub(created.tenant_id, "g-sub-cfg")
        updated = manager.update_config(
            created.tenant_id,
            todoist_token="my-secret-token",
            timezone="America/New_York",
        )
        assert updated is not None
        assert updated.config.timezone == "America/New_York"
        assert updated.config.todoist_api_token_encrypted != ""

    def test_decrypt_token_roundtrip(self, manager):
        created = manager.create_tenant("Dec", "d@d.com")
        manager.bind_owner_sub(created.tenant_id, "g-sub-dec")
        manager.update_config(created.tenant_id, todoist_token="secret-123")
        tenant = manager.get_tenant(created.tenant_id)
        decrypted = manager.decrypt_credential(
            tenant,
            tenant.config.todoist_api_token_encrypted,
            "todoist_api_token",
        )
        assert decrypted == "secret-123"

    def test_update_config_gemini_key_encrypted(self, manager):
        """Per-tenant BYO Gemini key is encrypted at rest and decrypts only
        with the tenant's derived key (zero-trust for platform owner)."""
        created = manager.create_tenant("Gem", "g@g.com")
        manager.bind_owner_sub(created.tenant_id, "g-sub-gem")
        plaintext = fake_api_key("AIzaSy-", 32)
        updated = manager.update_config(created.tenant_id, gemini_api_key=plaintext)
        assert updated is not None
        ciphertext = updated.config.gemini_api_key_encrypted
        assert ciphertext != ""
        assert plaintext not in ciphertext  # encrypted, not plain
        decrypted = manager.decrypt_credential(
            updated, ciphertext, "gemini_api_key"
        )
        assert decrypted == plaintext

    def test_update_config_rag_enabled_toggle(self, manager):
        created = manager.create_tenant("Rag", "r@r.com")
        updated = manager.update_config(created.tenant_id, rag_enabled=True)
        assert updated is not None
        assert updated.config.rag_enabled is True
        # Persisted across reload
        reloaded = manager.get_tenant(created.tenant_id)
        assert reloaded.config.rag_enabled is True

    def test_deactivate_wipes_gemini_key(self, manager):
        """Deactivation wipes per-tenant Gemini encrypted field."""
        created = manager.create_tenant("Wipe", "w@w.com")
        manager.bind_owner_sub(created.tenant_id, "g-sub-wipe")
        manager.update_config(created.tenant_id, gemini_api_key=fake_api_key("AIzaSy-", 16))
        assert manager.get_tenant(created.tenant_id).config.gemini_api_key_encrypted != ""
        manager.deactivate_tenant(created.tenant_id)
        tenant = manager.get_tenant(created.tenant_id)
        assert tenant.config.gemini_api_key_encrypted == ""

    def test_deactivate_tenant(self, manager):
        created = manager.create_tenant("Deact", "d@d.com")
        result = manager.deactivate_tenant(created.tenant_id)
        assert result is True
        tenant = manager.get_tenant(created.tenant_id)
        assert tenant.is_active is False

    def test_deactivate_nonexistent(self, manager):
        assert manager.deactivate_tenant("tn_ghost") is False

    def test_storage_partitions_created(self, manager, storage):
        tenant = manager.create_tenant("Parts", "p@p.com")
        for layer in ("bronze", "silver", "gold", "runs"):
            key = f"{layer}/{tenant.tenant_id}/.partition"
            assert storage.exists(key)

    def test_create_vip_tenant(self, manager):
        tenant = manager.create_tenant("VIP Co", "vip@v.com", TenantTier.VIP)
        assert tenant.tier == TenantTier.VIP
        assert tenant.role == "vip"
        assert tenant.quota.max_pipeline_runs_per_day == 999

    def test_upgrade_to_vip(self, manager):
        tenant = manager.create_tenant("Up2", "u2@u.com", TenantTier.FREE)
        updated = manager.update_tier(tenant.tenant_id, TenantTier.VIP)
        assert updated.tier == TenantTier.VIP
        assert updated.role == "vip"
        assert updated.quota.premium_llm_enabled is True

    def test_stripe_fields_roundtrip(self, manager):
        tenant = manager.create_tenant("Stripe", "s@s.com")
        tenant.stripe_customer_id = "cus_abc"
        tenant.stripe_subscription_id = "sub_xyz"
        manager._save_tenant(tenant)
        loaded = manager.get_tenant(tenant.tenant_id)
        assert loaded.stripe_customer_id == "cus_abc"
        assert loaded.stripe_subscription_id == "sub_xyz"

    def test_update_config_schedule_fields(self, manager):
        tenant = manager.create_tenant("Sched", "sc@c.com")
        updated = manager.update_config(
            tenant.tenant_id,
            daily_digest_time="08:30",
            daily_digest_days="mon,wed,fri",
            overdue_alert_enabled=True,
            overdue_alert_interval_hours=2,
            weekend_planner_enabled=True,
            weekly_review_enabled=True,
            delivery_whatsapp=True,
            whatsapp_phone="+351912345678",
            use_local_llm=True,
        )
        assert updated.config.daily_digest_time == "08:30"
        assert updated.config.daily_digest_days == "mon,wed,fri"
        assert updated.config.overdue_alert_enabled is True
        assert updated.config.overdue_alert_interval_hours == 2
        assert updated.config.weekend_planner_enabled is True
        assert updated.config.weekly_review_enabled is True
        assert updated.config.delivery_whatsapp is True
        assert updated.config.whatsapp_phone == "+351912345678"
        assert updated.config.use_local_llm is True
