"""Tenant management routes — Registration, auth, and configuration.

Provides endpoints for tenant CRUD operations and JWT token generation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import hmac as _hmac
import os
import re

from brain.api.auth import create_access_token
from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_tenant_manager,
)
from brain.security.rbac import Permission
from brain.security.resilience import RateLimiter
from brain.tenant.manager import TenantManager
from brain.tenant.models import TenantTier

# Regex for acceptable tenant name (prevents injection / abuse)
_SAFE_NAME = re.compile(r"^[\w\s.@\-]{1,128}$", re.UNICODE)
# RFC 5322 simplified email validation
_VALID_EMAIL = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)
# Terms-of-service agreement flag required at registration
_TOS_VERSION = "2026-04-18"

router = APIRouter()

# ── Rate limiters for unauthenticated endpoints ──────────────────────
_registration_limiter = RateLimiter(max_requests=5, window_seconds=300.0)
_login_limiter = RateLimiter(max_requests=10, window_seconds=300.0)


# ── Request / Response models ────────────────────────────────────────


class TenantCreateRequest(BaseModel):
    name: str
    email: str
    accept_tos: bool = False


class TenantConfigUpdateRequest(BaseModel):
    todoist_token: str | None = None
    notion_token: str | None = None
    litellm_proxy_token: str | None = None
    gmail_imap_password: str | None = None
    google_oauth_token: str | None = None
    gemini_api_key: str | None = None
    timezone: str | None = None
    daily_top_task_limit: int | None = None
    workday_start_hour: int | None = None
    workday_end_hour: int | None = None
    # Schedule
    daily_digest_time: str | None = None
    daily_digest_days: str | None = None
    overdue_alert_enabled: bool | None = None
    overdue_alert_interval_hours: int | None = None
    weekend_planner_enabled: bool | None = None
    weekly_review_enabled: bool | None = None
    # Delivery
    delivery_email: bool | None = None
    delivery_whatsapp: bool | None = None
    delivery_notion: bool | None = None
    whatsapp_phone: str | None = None
    # Sources
    source_todoist: bool | None = None
    source_google_calendar: bool | None = None
    source_gmail: bool | None = None
    # LLM
    use_local_llm: bool | None = None
    # RAG (Premium/VIP tier feature)
    rag_enabled: bool | None = None


class TenantLoginRequest(BaseModel):
    tenant_id: str
    email: str
    api_key: str


class TenantResponse(BaseModel):
    tenant_id: str
    name: str
    email: str
    tier: str
    role: str
    is_active: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    api_key: str | None = None


# ── Routes ───────────────────────────────────────────────────────────


@router.post("/tenants", response_model=TokenResponse)
async def create_tenant(
    body: TenantCreateRequest,
    request: Request,
    manager: TenantManager = Depends(get_tenant_manager),
) -> TokenResponse:
    """Register a new tenant and return an access token.

    In production, open registration can be disabled by setting
    ``VELAFLOW_DISABLE_OPEN_REGISTRATION=true``. Google OAuth remains
    the recommended path — this endpoint is for programmatic/legacy use.
    """
    import os
    if os.environ.get("VELAFLOW_DISABLE_OPEN_REGISTRATION", "").lower() == "true":
        raise HTTPException(
            status_code=403,
            detail="Open registration disabled. Use Google OAuth at /api/v1/auth/google.",
        )

    # IP-based rate limiting to prevent mass tenant creation
    client_ip = request.client.host if request.client else "unknown"
    if not _registration_limiter.allow(client_ip):
        raise HTTPException(
            status_code=429, detail="Too many registration attempts. Try again later."
        )

    # Terms-of-service acceptance required
    if not body.accept_tos:
        raise HTTPException(
            status_code=400,
            detail="You must accept the Terms of Service (accept_tos=true)."
            " VelaFlow prohibits illegal content, abuse, and data exfiltration.",
        )

    # Validate tenant name (prevents injection / abuse)
    if not _SAFE_NAME.match(body.name):
        raise HTTPException(
            status_code=400, detail="Invalid tenant name. Use 1-128 alphanumeric characters."
        )

    # Validate email format
    if not _VALID_EMAIL.match(body.email):
        raise HTTPException(
            status_code=400, detail="Invalid email address format."
        )

    # Always create as FREE — owner promotion handled via Google OAuth
    tier = TenantTier.FREE

    tenant = manager.create_tenant(body.name, body.email, tier)
    token = create_access_token(
        tenant_id=tenant.tenant_id,
        role=tenant.role,
        email=tenant.email,
    )
    return TokenResponse(
        access_token=token,
        tenant_id=tenant.tenant_id,
        api_key=tenant.api_key,
    )


@router.post("/tenants/login", response_model=TokenResponse)
async def login_tenant(
    body: TenantLoginRequest,
    request: Request,
    manager: TenantManager = Depends(get_tenant_manager),
) -> TokenResponse:
    """Authenticate an existing tenant and return a new access token."""
    # IP-based brute-force protection
    client_ip = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(client_ip):
        raise HTTPException(
            status_code=429, detail="Too many login attempts. Try again later."
        )

    tenant = manager.get_tenant(body.tenant_id)
    # Constant-time checks — prevent timing oracles on existence/email/active
    _email_ok = _hmac.compare_digest(
        (tenant.email if tenant else "x").encode(), body.email.encode()
    )
    _active = tenant.is_active if tenant else False
    if tenant is None or not _email_ok or not _active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Verify API key (constant-time comparison)
    stored_key = getattr(tenant, "api_key", None) or ""
    if not stored_key or not _hmac.compare_digest(stored_key, body.api_key):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(
        tenant_id=tenant.tenant_id,
        role=tenant.role,
        email=tenant.email,
    )
    return TokenResponse(
        access_token=token,
        tenant_id=tenant.tenant_id,
    )


@router.get("/tenants/me", response_model=TenantResponse)
async def get_current_tenant(
    tenant_id: str = Depends(get_current_tenant_id),
    manager: TenantManager = Depends(get_tenant_manager),
) -> TenantResponse:
    """Get the current tenant's profile."""
    tenant = manager.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return TenantResponse(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
        email=tenant.email,
        tier=tenant.tier.value,
        role=tenant.role,
        is_active=tenant.is_active,
    )


@router.patch(
    "/tenants/me/config",
    dependencies=[Depends(RequirePermission(Permission.MANAGE_API_KEYS))],
)
async def update_tenant_config(
    body: TenantConfigUpdateRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    manager: TenantManager = Depends(get_tenant_manager),
) -> dict:
    """Update the current tenant's integration configuration."""
    # Enforce tier for local LLM
    if body.use_local_llm is True:
        tenant = manager.get_tenant(tenant_id)
        if tenant and tenant.tier not in (TenantTier.PREMIUM, TenantTier.VIP):
            raise HTTPException(
                status_code=403, detail="Local LLM requires Premium or VIP tier"
            )

    # Enforce tier for RAG (VIP-only — premium keeps NotebookLM export)
    if body.rag_enabled is True:
        tenant = manager.get_tenant(tenant_id)
        if tenant and tenant.tier != TenantTier.VIP:
            raise HTTPException(
                status_code=403, detail="Native RAG requires VIP tier"
            )

    tenant = manager.update_config(
        tenant_id,
        todoist_token=body.todoist_token,
        notion_token=body.notion_token,
        litellm_proxy_token=body.litellm_proxy_token,
        gmail_imap_password=body.gmail_imap_password,
        google_oauth_token=body.google_oauth_token,
        gemini_api_key=body.gemini_api_key,
        timezone=body.timezone,
        daily_top_task_limit=body.daily_top_task_limit,
        workday_start_hour=body.workday_start_hour,
        workday_end_hour=body.workday_end_hour,
        daily_digest_time=body.daily_digest_time,
        daily_digest_days=body.daily_digest_days,
        overdue_alert_enabled=body.overdue_alert_enabled,
        overdue_alert_interval_hours=body.overdue_alert_interval_hours,
        weekend_planner_enabled=body.weekend_planner_enabled,
        weekly_review_enabled=body.weekly_review_enabled,
        delivery_email=body.delivery_email,
        delivery_whatsapp=body.delivery_whatsapp,
        delivery_notion=body.delivery_notion,
        whatsapp_phone=body.whatsapp_phone,
        source_todoist=body.source_todoist,
        source_google_calendar=body.source_google_calendar,
        source_gmail=body.source_gmail,
        use_local_llm=body.use_local_llm,
        rag_enabled=body.rag_enabled,
    )
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"status": "updated"}
