"""Tests for QueueWorker._build_tenant_settings — per-tenant secret decryption."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from brain.config import Settings
from brain.queue.tasks import TaskQueue
from brain.queue.worker import QueueWorker
from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager
from brain.tenant.models import Tenant, TenantConfig, TenantTier


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
        "encryptor": encryptor,
        "manager": mgr,
        "settings": settings,
        "worker": worker,
    }


class TestBuildTenantSettings:
    """Verify that _build_tenant_settings correctly decrypts per-tenant tokens."""

    def test_empty_config_falls_back_to_global(self, workspace):
        tenant = workspace["manager"].create_tenant("Test", "t@t.com", TenantTier.FREE)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert isinstance(result, Settings)
        # Should inherit global todoist token (or empty if env unset)
        assert result.todoist_api_token == workspace["settings"].todoist_api_token

    def test_todoist_token_decrypted(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("Todo", "td@t.com", TenantTier.STANDARD)
        mgr.bind_owner_sub(tenant.tenant_id, "g-sub-todo")
        mgr.update_config(tenant.tenant_id, todoist_token="secret-todoist-abc")
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.todoist_api_token == "secret-todoist-abc"

    def test_notion_token_decrypted(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("Not", "n@t.com", TenantTier.STANDARD)
        mgr.bind_owner_sub(tenant.tenant_id, "g-sub-not")
        mgr.update_config(tenant.tenant_id, notion_token="notion-secret-xyz")
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.notion_api_token == "notion-secret-xyz"

    def test_litellm_token_decrypted(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("LLM", "l@t.com", TenantTier.PREMIUM)
        mgr.bind_owner_sub(tenant.tenant_id, "g-sub-llm")
        mgr.update_config(tenant.tenant_id, litellm_proxy_token="llm-proxy-key")
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.litellm_proxy_token == "llm-proxy-key"

    def test_demo_mode_disabled_when_tenant_has_todoist(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("DM", "dm@t.com", TenantTier.STANDARD)
        mgr.bind_owner_sub(tenant.tenant_id, "g-sub-dm")
        mgr.update_config(tenant.tenant_id, todoist_token="my-real-token")
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.demo_mode is False

    def test_timezone_from_tenant_config(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("TZ", "tz@t.com")
        mgr.update_config(tenant.tenant_id, timezone="America/New_York")
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.tz == "America/New_York"

    def test_workday_hours_from_tenant_config(self, workspace):
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("WH", "wh@t.com")
        mgr.update_config(tenant.tenant_id, workday_start_hour=8, workday_end_hour=20)
        tenant = mgr.get_tenant(tenant.tenant_id)
        result = workspace["worker"]._build_tenant_settings(tenant)
        assert result.workday_start_hour == 8
        assert result.workday_end_hour == 20

    def test_frozen_settings_not_mutated(self, workspace):
        """Ensure original global settings are not mutated."""
        mgr = workspace["manager"]
        tenant = mgr.create_tenant("FR", "fr@t.com")
        original_tz = workspace["settings"].tz
        mgr.update_config(tenant.tenant_id, timezone="Asia/Tokyo")
        tenant = mgr.get_tenant(tenant.tenant_id)
        workspace["worker"]._build_tenant_settings(tenant)
        assert workspace["settings"].tz == original_tz
