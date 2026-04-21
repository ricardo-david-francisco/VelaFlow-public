"""Tests for the RAG API routes (VIP-only)."""

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
    with patch.dict(
        "os.environ",
        {
            "VELAFLOW_DATA_DIR": str(tmp_path / "data"),
            "VELAFLOW_MASTER_KEY": "IWE-SCebecPANl8Fiwke96esCp7bby00008OD6YhBWs=",
            "RAG_DUCKDB_PATH": str(tmp_path / "rag.duckdb"),
            "JWT_SECRET": _JWT_SECRET,
        },
    ), patch("brain.api.auth._JWT_SECRET", _JWT_SECRET):
        from brain.api.dependencies import (
            get_encryptor,
            get_rag_pipeline,
            get_settings,
            get_storage,
        )

        for fn in (get_storage, get_encryptor, get_settings, get_rag_pipeline):
            fn.cache_clear()
        application = create_app()
        yield application
        for fn in (get_storage, get_encryptor, get_settings, get_rag_pipeline):
            fn.cache_clear()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _auth(tenant_id: str, role: str = "vip") -> dict[str, str]:
    token = create_access_token(
        tenant_id=tenant_id,
        role=role,
        email="vip@test.com",
        secret=_JWT_SECRET,
    )
    return {"Authorization": f"Bearer {token}"}


def _create_tenant(tier_value: str):
    from brain.api.dependencies import get_encryptor, get_storage
    from brain.tenant.manager import TenantManager
    from brain.tenant.models import TenantTier

    mgr = TenantManager(get_storage(), get_encryptor())
    tenant = mgr.create_tenant("Rag", "r@t.com", TenantTier(tier_value))
    return tenant


class TestRAGRBAC:
    def test_ingest_requires_auth(self, client):
        resp = client.post("/api/v1/rag/ingest", json={"document_id": "d1", "text": "x"})
        assert resp.status_code in (401, 403)

    def test_ingest_denied_for_free_tier(self, client):
        tenant = _create_tenant("free")
        resp = client.post(
            "/api/v1/rag/ingest",
            headers=_auth(tenant.tenant_id, "free"),
            json={"document_id": "d1", "text": "hello"},
        )
        assert resp.status_code == 403

    def test_ingest_denied_for_standard_tier(self, client):
        tenant = _create_tenant("standard")
        resp = client.post(
            "/api/v1/rag/ingest",
            headers=_auth(tenant.tenant_id, "standard"),
            json={"document_id": "d1", "text": "hello"},
        )
        assert resp.status_code == 403

    def test_ingest_denied_for_premium_tier(self, client):
        """Premium keeps NotebookLM export — native RAG is VIP-only."""
        tenant = _create_tenant("premium")
        resp = client.post(
            "/api/v1/rag/ingest",
            headers=_auth(tenant.tenant_id, "premium"),
            json={"document_id": "d1", "text": "hello"},
        )
        assert resp.status_code == 403

    def test_query_denied_for_premium_tier(self, client):
        tenant = _create_tenant("premium")
        resp = client.post(
            "/api/v1/rag/query",
            headers=_auth(tenant.tenant_id, "premium"),
            json={"query": "anything", "top_k": 3},
        )
        assert resp.status_code == 403


class TestRAGRoundTrip:
    def test_vip_can_ingest_and_query(self, client):
        tenant = _create_tenant("vip")
        # Ingest
        text = (
            "VelaFlow is a self-hosted productivity platform. "
            "It runs entirely on a home server with zero cloud dependency. "
            "All retrieval is tenant-isolated inside a DuckDB vector store."
        )
        resp = client.post(
            "/api/v1/rag/ingest",
            headers=_auth(tenant.tenant_id, "vip"),
            json={"document_id": "velaflow-intro", "text": text},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["chunks_stored"] >= 1

        # Query
        resp = client.post(
            "/api/v1/rag/query",
            headers=_auth(tenant.tenant_id, "vip"),
            json={"query": "Where does retrieval happen?", "top_k": 3},
        )
        assert resp.status_code == 200
        hits = resp.json()["hits"]
        assert len(hits) >= 1
        assert hits[0]["document_id"] == "velaflow-intro"

    def test_stats_and_delete(self, client):
        tenant = _create_tenant("vip")
        client.post(
            "/api/v1/rag/ingest",
            headers=_auth(tenant.tenant_id, "vip"),
            json={"document_id": "doc-a", "text": "alpha bravo charlie delta"},
        )
        resp = client.get(
            "/api/v1/rag/stats", headers=_auth(tenant.tenant_id, "vip")
        )
        assert resp.status_code == 200
        assert resp.json()["documents"] == 1

        resp = client.delete(
            "/api/v1/rag/documents/doc-a",
            headers=_auth(tenant.tenant_id, "vip"),
        )
        assert resp.status_code == 200
        assert resp.json()["document_id"] == "doc-a"


class TestRAGTenantIsolation:
    def test_query_does_not_leak_across_tenants(self, client):
        t1 = _create_tenant("vip")
        t2 = _create_tenant("vip")

        client.post(
            "/api/v1/rag/ingest",
            headers=_auth(t1.tenant_id, "vip"),
            json={"document_id": "secret-1", "text": "tenant one private data"},
        )

        resp = client.post(
            "/api/v1/rag/query",
            headers=_auth(t2.tenant_id, "vip"),
            json={"query": "private data", "top_k": 5},
        )
        assert resp.status_code == 200
        hits = resp.json()["hits"]
        assert all(h["document_id"] != "secret-1" for h in hits)
