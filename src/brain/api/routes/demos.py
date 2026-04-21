"""Demo account administration routes.

Provides admin-only endpoints for managing time-limited VIP demo
accounts. Requires ADMIN_ALL permission for all operations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_encryptor,
    get_storage,
    get_tenant_manager,
)
from brain.security.rbac import Permission
from brain.tenant.demo_manager import DemoManager
from brain.tenant.manager import TenantManager

router = APIRouter()


class CreateDemoRequest(BaseModel):
    name: str
    email: str
    duration_days: int = 7
    cost_cap_pipeline: int = 50
    cost_cap_llm: int = 100
    litellm_token: str = ""


@router.post(
    "/demos",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
def create_demo(
    body: CreateDemoRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    mgr: TenantManager = Depends(get_tenant_manager),
):
    """Create a time-limited VIP demo account. Admin only."""
    storage = get_storage()
    encryptor = get_encryptor()
    demo_mgr = DemoManager(mgr, storage, encryptor)
    tenant = demo_mgr.create_demo(
        name=body.name,
        email=body.email,
        created_by=tenant_id,
        duration_days=body.duration_days,
        cost_cap_pipeline=body.cost_cap_pipeline,
        cost_cap_llm=body.cost_cap_llm,
        litellm_token=body.litellm_token,
    )
    return {
        "tenant_id": tenant.tenant_id,
        "api_key": tenant.api_key,
        "tier": tenant.tier.value,
        "is_demo": True,
    }


@router.get(
    "/demos",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
def list_demos(
    mgr: TenantManager = Depends(get_tenant_manager),
):
    """List all demo accounts with their status. Admin only."""
    storage = get_storage()
    encryptor = get_encryptor()
    demo_mgr = DemoManager(mgr, storage, encryptor)
    return {"demos": demo_mgr.list_demo_accounts()}


@router.get(
    "/demos/{demo_tenant_id}/analytics",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
def demo_analytics(
    demo_tenant_id: str,
    mgr: TenantManager = Depends(get_tenant_manager),
):
    """Get usage analytics for a specific demo account. Admin only."""
    storage = get_storage()
    encryptor = get_encryptor()
    demo_mgr = DemoManager(mgr, storage, encryptor)
    return demo_mgr.get_usage_analytics(demo_tenant_id)


@router.post(
    "/demos/{demo_tenant_id}/check-expiry",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
def check_demo_expiry(
    demo_tenant_id: str,
    mgr: TenantManager = Depends(get_tenant_manager),
):
    """Check and auto-expire a demo account if TTL exceeded. Admin only."""
    storage = get_storage()
    encryptor = get_encryptor()
    demo_mgr = DemoManager(mgr, storage, encryptor)
    is_active = demo_mgr.check_expiry(demo_tenant_id)
    return {"tenant_id": demo_tenant_id, "is_active": is_active}
