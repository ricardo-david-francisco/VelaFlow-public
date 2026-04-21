"""RAG API — VIP-only endpoints for the local RAG pipeline.

Backs the ``brain.rag`` module with a tenant-isolated HTTP surface. All
routes enforce the ``USE_RAG`` permission, which is granted exclusively
to the ``vip`` tier (plus ``demo`` / ``admin`` for evaluation and ops —
see ``brain.security.rbac``). The ``premium`` tier keeps the NotebookLM
export workflow and does **not** receive native RAG — this is the
deliberate differentiator that justifies the VIP subscription.

The pipeline is a 100 % local, zero-cost RAG (DuckDB vector store + a
deterministic hashing embedder). Operators may plug in a transformer
embedder via the optional ``velaflow[premium]`` extra without changing
the HTTP contract.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_rag_pipeline,
)
from brain.security.rbac import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Tier → per-tenant document quota ────────────────────────────────────────
# Plain data, not security critical — RBAC gate above has already verified
# the tenant is allowed to use RAG at all.
_DOC_QUOTA_BY_TIER: dict[str, int] = {
    "demo": 5,
    "vip": 1000,
    "admin": 0,  # 0 = unlimited
}


def _tenant_quota(role: str) -> int:
    return _DOC_QUOTA_BY_TIER.get(role, 0)


# ── Request / response models ────────────────────────────────────────────────
class IngestRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    document_id: str
    chunks_stored: int


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=20)


class QueryHit(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    hits: list[QueryHit]


class StatsResponse(BaseModel):
    documents: int
    quota: int  # 0 = unlimited


class DeleteResponse(BaseModel):
    document_id: str
    chunks_deleted: int


# ── Routes ───────────────────────────────────────────────────────────────────
@router.post(
    "/rag/ingest",
    response_model=IngestResponse,
    dependencies=[Depends(RequirePermission(Permission.USE_RAG))],
)
def ingest_document(
    body: IngestRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    pipeline=Depends(get_rag_pipeline),
) -> IngestResponse:
    """Ingest a document into the tenant-scoped vector store."""
    from fastapi import Request  # noqa: F401 — kept for typing clarity elsewhere
    role = _current_role_for_quota(tenant_id)
    quota = _tenant_quota(role)
    try:
        stored = pipeline.ingest(
            text=body.text,
            document_id=body.document_id,
            tenant_id=tenant_id,
            metadata=body.metadata,
            max_documents=quota,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IngestResponse(document_id=body.document_id, chunks_stored=stored)


@router.post(
    "/rag/query",
    response_model=QueryResponse,
    dependencies=[Depends(RequirePermission(Permission.USE_RAG))],
)
def query_documents(
    body: QueryRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    pipeline=Depends(get_rag_pipeline),
) -> QueryResponse:
    """Run a semantic query, tenant-scoped."""
    results = pipeline.query(
        query_text=body.query, tenant_id=tenant_id, top_k=body.top_k
    )
    hits = [
        QueryHit(
            chunk_id=r.chunk_id,
            document_id=r.document_id,
            content=r.content,
            score=r.score,
            metadata=r.metadata,
        )
        for r in results
    ]
    return QueryResponse(hits=hits)


@router.get(
    "/rag/stats",
    response_model=StatsResponse,
    dependencies=[Depends(RequirePermission(Permission.USE_RAG))],
)
def rag_stats(
    tenant_id: str = Depends(get_current_tenant_id),
    pipeline=Depends(get_rag_pipeline),
) -> StatsResponse:
    """Return the tenant's current RAG document count and quota."""
    role = _current_role_for_quota(tenant_id)
    documents = pipeline._store.count_documents(tenant_id)  # noqa: SLF001
    return StatsResponse(documents=documents, quota=_tenant_quota(role))


@router.delete(
    "/rag/documents/{document_id}",
    response_model=DeleteResponse,
    dependencies=[Depends(RequirePermission(Permission.USE_RAG))],
)
def delete_document(
    document_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    pipeline=Depends(get_rag_pipeline),
) -> DeleteResponse:
    """Delete all chunks of a single document, tenant-scoped."""
    deleted = pipeline.delete_document(tenant_id, document_id)
    return DeleteResponse(document_id=document_id, chunks_deleted=deleted)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _current_role_for_quota(tenant_id: str) -> str:
    """Read the tenant's tier from the tenant manager for quota sizing.

    Kept intentionally simple — the security decision has already been
    taken by ``RequirePermission``; this lookup is just for dimensioning.
    """
    from brain.api.dependencies import get_tenant_manager

    # Resolve manager without FastAPI injection to avoid an extra Depends
    # chain that would not add any security value at this point.
    try:
        mgr = get_tenant_manager(  # type: ignore[call-arg]
            storage=_get_storage(),
            encryptor=_get_encryptor(),
            credential_encryptor=_get_credential_encryptor(),
        )
        tenant = mgr.get_tenant(tenant_id)
        if tenant is None:
            return "free"
        tier = getattr(tenant.config, "tier", None)
        return tier.value if tier is not None else "free"
    except Exception:  # noqa: BLE001 — quota sizing must never break a request
        logger.warning("rag quota lookup fallback", exc_info=True)
        return "premium"


def _get_storage():
    from brain.api.dependencies import get_storage

    return get_storage()


def _get_encryptor():
    from brain.api.dependencies import get_encryptor

    return get_encryptor()


def _get_credential_encryptor():
    from brain.api.dependencies import get_credential_encryptor

    return get_credential_encryptor()
