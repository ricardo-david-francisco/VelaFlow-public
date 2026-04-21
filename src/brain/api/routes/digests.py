"""Digest routes — Per-tenant digest generation and retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_storage,
)
from brain.pipeline.gold import GoldLayer
from brain.security.rbac import Permission
from brain.storage.base import StorageBackend

router = APIRouter()


class DigestResponse(BaseModel):
    tenant_id: str
    digest_type: str
    subject: str
    body_text: str
    produced_at: str | None


@router.get(
    "/digests/daily",
    response_model=DigestResponse,
    dependencies=[Depends(RequirePermission(Permission.GENERATE_DIGEST))],
)
async def get_daily_digest(
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> DigestResponse:
    """Get the latest daily digest for the current tenant."""
    gold = GoldLayer(storage)
    data = gold.read_daily_digest(tenant_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="No digest found. Run the pipeline first.",
        )

    return DigestResponse(
        tenant_id=tenant_id,
        digest_type=data.get("digest_type", "daily"),
        subject=data.get("subject", ""),
        body_text=data.get("body_text", ""),
        produced_at=data.get("produced_at"),
    )
