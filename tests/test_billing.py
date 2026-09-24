"""Tests for billing routes — Stripe integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from brain.api.app import create_app
from tests._fakes import fake_password

_JWT_SECRET = fake_password(32)


@pytest.fixture()
def app(tmp_path):
    with patch.dict("os.environ", {
        "VELAFLOW_DATA_DIR": str(tmp_path / "data"),
        "VELAFLOW_MASTER_KEY": "IWE-SCebecPANl8Fiwke96esCp7bby00008OD6YhBWs=",
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


def _auth_header(tenant_id: str = "tn_test", role: str = "free") -> dict:
    from brain.api.auth import create_access_token
    token = create_access_token(
        tenant_id=tenant_id, role=role, email="t@t.com", secret=_JWT_SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


class TestBillingRoutes:
    def test_checkout_requires_auth(self, client):
        """Checkout endpoint requires authentication."""
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "standard"},
        )
        assert resp.status_code in (401, 403)

    def test_checkout_billing_not_configured(self, client):
        """Without Stripe keys, returns 503."""
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "standard"},
            headers=_auth_header(),
        )
        # Should return 503 (billing not configured) or 404 (tenant not found)
        assert resp.status_code in (404, 503)

    def test_checkout_invalid_tier(self, client):
        """Requesting an invalid tier returns 400."""
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"tier": "ultra_mega_tier"},
            headers=_auth_header(),
        )
        assert resp.status_code == 400

    def test_stripe_webhook_no_signature(self, client):
        """Stripe webhook without signature returns error."""
        resp = client.post(
            "/api/v1/webhooks/stripe",
            content=b'{"type": "checkout.session.completed"}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code in (400, 503)

    def test_stripe_webhook_is_public(self, client):
        """Stripe webhook path should NOT require JWT auth."""
        resp = client.post(
            "/api/v1/webhooks/stripe",
            content=b'{}',
            headers={"content-type": "application/json"},
        )
        # Should NOT be 401/403 — it should be 400 or 503
        assert resp.status_code not in (401, 403)

    def test_billing_portal_not_implemented(self, client):
        """Portal endpoint returns 404 (not implemented yet)."""
        resp = client.post("/api/v1/billing/portal", headers=_auth_header())
        # Portal route doesn't exist yet → 404 or 405
        assert resp.status_code in (404, 405, 422)
