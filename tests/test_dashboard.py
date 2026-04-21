"""Tests for dashboard API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from brain.api.app import create_app
from brain.api.auth import create_access_token
from tests._fakes import fake_password

_JWT_SECRET = fake_password(32)


@pytest.fixture()
def app(tmp_path):
    with patch.dict("os.environ", {
        "VELAFLOW_DATA_DIR": str(tmp_path / "data"),
        "VELAFLOW_MASTER_KEY": "IWE-SCebecPANl8Fiwke96esCp7bby00008OD6YhBWs=",
        "JWT_SECRET": _JWT_SECRET,
    }), patch("brain.api.auth._JWT_SECRET", _JWT_SECRET):
        # Clear lru_cache so get_storage/get_encryptor pick up new env vars
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


def _auth_header(tenant_id: str = "tn_test", role: str = "standard") -> dict:
    token = create_access_token(
        tenant_id=tenant_id,
        role=role,
        email="test@test.com",
        secret=_JWT_SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


class TestDashboardAPI:
    def test_dashboard_requires_auth(self, client):
        resp = client.get("/api/v1/dashboard/overview")
        assert resp.status_code in (401, 403)

    def test_dashboard_tenant_not_found(self, client):
        resp = client.get(
            "/api/v1/dashboard/overview",
            headers=_auth_header("tn_nonexistent"),
        )
        assert resp.status_code == 404

    def test_dashboard_returns_overview(self, client, tmp_path):
        """Create a real tenant and fetch dashboard data."""
        from brain.api.dependencies import get_storage, get_encryptor
        from brain.tenant.manager import TenantManager
        from brain.tenant.models import TenantTier

        storage = get_storage()
        enc = get_encryptor()
        mgr = TenantManager(storage, enc)
        tenant = mgr.create_tenant("Dash", "d@t.com", TenantTier.STANDARD)

        resp = client.get(
            "/api/v1/dashboard/overview",
            headers=_auth_header(tenant.tenant_id, "standard"),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "connections" in data
        assert "pipeline" in data
        assert "usage" in data
        assert data["usage"]["tier"] == "standard"

    def test_dashboard_connections_all_false_by_default(self, client, tmp_path):
        """New tenant with no tokens configured has all connections False."""
        from brain.api.dependencies import get_storage, get_encryptor
        from brain.tenant.manager import TenantManager

        storage = get_storage()
        enc = get_encryptor()
        mgr = TenantManager(storage, enc)
        tenant = mgr.create_tenant("Conn", "c@t.com")

        resp = client.get(
            "/api/v1/dashboard/overview",
            headers=_auth_header(tenant.tenant_id, "free"),
        )
        assert resp.status_code == 200
        conns = resp.json()["connections"]
        assert conns["todoist"] is False
        assert conns["notion"] is False
        assert conns["google_calendar"] is False
        assert conns["gmail"] is False
        assert conns["whatsapp"] is False
