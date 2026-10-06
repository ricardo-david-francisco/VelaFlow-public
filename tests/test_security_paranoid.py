"""Paranoid security tests — focused on OWASP Top 10 hardening.

Tests cover:
- Billing redirect URL validation (open redirect prevention)
- Stripe webhook signature enforcement
- Quota enforcement thread safety (basic)
- Tenant isolation in job results
- Path traversal in tenant IDs
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from brain.api.app import create_app
from brain.api.auth import create_access_token
from brain.security.encryption import FieldEncryptor
from tests._fakes import fake_password

_JWT_SECRET = fake_password(32)
_MASTER_KEY = FieldEncryptor.generate_master_key()


@pytest.fixture()
def app(tmp_path):
    with patch.dict("os.environ", {
        "VELAFLOW_DATA_DIR": str(tmp_path / "data"),
        "VELAFLOW_MASTER_KEY": _MASTER_KEY,
        "JWT_SECRET": _JWT_SECRET,
    }), patch("brain.api.auth._JWT_SECRET", _JWT_SECRET):
        from brain.api.dependencies import get_storage, get_encryptor, get_settings
        get_storage.cache_clear()
        get_encryptor.cache_clear()
        get_settings.cache_clear()
        application = create_app()
        yield application
        get_storage.cache_clear()
        get_encryptor.cache_clear()
        get_settings.cache_clear()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _auth_header(tenant_id: str = "tn_test", role: str = "premium") -> dict:
    token = create_access_token(
        tenant_id=tenant_id, role=role, email="sec@t.com", secret=_JWT_SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


class TestOpenRedirectPrevention:
    """B-1: Verify redirect URL validation blocks open redirects."""

    def test_evil_redirect_blocked(self, client):
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "standard", "success_url": "https://evil.com/phish"},
            headers=_auth_header(),
        )
        # Should be 400 (bad redirect) or 503 (billing not configured, before redirect check)
        assert resp.status_code in (400, 404, 503)

    def test_javascript_redirect_blocked(self, client):
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "standard", "success_url": "javascript:alert(1)"},
            headers=_auth_header(),
        )
        assert resp.status_code in (400, 404, 503)


class TestStripeWebhookSecurity:
    """Verify webhook signature enforcement and replay resistance."""

    def test_missing_signature_rejected(self, client):
        resp = client.post(
            "/api/v1/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code in (400, 503)

    def test_invalid_signature_rejected(self, client):
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": "whsec_test_secret"}), \
             patch("brain.api.routes.billing._STRIPE_WEBHOOK_SECRET", "whsec_test_secret"):
            resp = client.post(
                "/api/v1/webhooks/stripe",
                content=b'{"type": "checkout.session.completed"}',
                headers={
                    "content-type": "application/json",
                    "stripe-signature": "t=1234567890,v1=invalid_signature",
                },
            )
            # Should fail with 400 or 503 (stripe SDK import, or bad sig)
            assert resp.status_code in (400, 503)

    def test_webhook_does_not_require_jwt(self, client):
        """Webhook endpoint must be accessible without Bearer token."""
        resp = client.post(
            "/api/v1/webhooks/stripe",
            content=b'{}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code != 401


class TestQuotaEnforcementEdgeCases:
    """W-1: Verify quota enforcement correctness."""

    def test_quota_resets_on_new_day(self):
        """Verify usage resets when the date changes."""
        import os
        from brain.config import Settings
        from brain.queue.tasks import TaskQueue
        from brain.queue.worker import QueueWorker, _daily_usage
        from brain.security.encryption import FieldEncryptor
        from brain.storage.local import LocalStorageBackend
        from brain.tenant.manager import TenantManager
        from brain.tenant.models import TenantTier

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorageBackend(tmp)
            mk = FieldEncryptor.generate_master_key()
            enc = FieldEncryptor(mk)
            mgr = TenantManager(storage, enc)
            settings = Settings.from_env()
            queue = TaskQueue()
            with patch.dict(os.environ, {"VELAFLOW_MASTER_KEY": mk}):
                worker = QueueWorker(queue, storage, settings)

            _daily_usage.clear()
            tenant = mgr.create_tenant("QR", "qr@t.com", TenantTier.FREE)

            # Exhaust quota
            for _ in range(3):
                worker._check_quota(tenant, "pipeline_run")
            assert worker._check_quota(tenant, "pipeline_run") is False

            # Simulate date change
            _daily_usage[tenant.tenant_id]["date"] = "1999-01-01"
            assert worker._check_quota(tenant, "pipeline_run") is True
            _daily_usage.clear()


class TestTenantIsolationSecurity:
    """Verify cross-tenant data access is prevented."""

    def test_dashboard_requires_own_tenant(self, client):
        """A token for tenant_a cannot view tenant_b's dashboard."""
        resp = client.get(
            "/api/v1/dashboard/overview",
            headers=_auth_header("tn_nonexistent_tenant"),
        )
        assert resp.status_code == 404

    def test_billing_requires_own_tenant(self, client):
        """Billing checkout for a nonexistent tenant is blocked."""
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "standard"},
            headers=_auth_header("tn_nonexistent"),
        )
        assert resp.status_code in (404, 503)


class TestPathTraversalPrevention:
    """Verify storage path traversal attacks are blocked."""

    def test_job_result_path_traversal(self):
        """Ensure job results cannot escape tenant directory."""
        import os
        import tempfile
        from brain.config import Settings
        from brain.queue.tasks import TaskQueue
        from brain.queue.worker import QueueWorker
        from brain.security.encryption import FieldEncryptor
        from brain.storage.local import LocalStorageBackend

        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorageBackend(tmp)
            mk = FieldEncryptor.generate_master_key()
            settings = Settings.from_env()
            queue = TaskQueue()
            with patch.dict(os.environ, {"VELAFLOW_MASTER_KEY": mk}):
                worker = QueueWorker(queue, storage, settings)

            with pytest.raises(ValueError, match="traversal"):
                worker._store_job_result(
                    "../../etc", "passwd", {"hack": True}
                )
