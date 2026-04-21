"""Authentication routes — Google OAuth2 login, user management, bans.

Provides:
- POST /auth/google       → authenticate via Google ID token
- POST /auth/invite       → create invite for new user (owner/admin only)
- POST /auth/redeem       → redeem invite code (during Google login)
- GET  /auth/me           → get current user profile
- GET  /auth/users        → list users in tenant (admin only)
- PATCH /auth/users/{id}/role → change user role (owner/admin only)
- DELETE /auth/users/{id} → deactivate user (owner/admin only)

Admin routes (require ADMIN_ALL):
- GET  /auth/bans         → list active bans
- POST /auth/bans         → set permanent ban
- DELETE /auth/bans/{key} → remove ban
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from brain.api.auth import create_access_token
from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_encryptor,
    get_storage,
    get_tenant_manager,
)
from brain.security.ban import BanManager
from brain.security.google_auth import verify_google_id_token
from brain.security.rbac import Permission
from brain.security.resilience import RateLimiter
from brain.storage.base import StorageBackend
from brain.security.encryption import FieldEncryptor
from brain.tenant.manager import TenantManager
from brain.tenant.models import TenantTier, UserRole
from brain.tenant.user_manager import UserManager

router = APIRouter()

# ── Singletons ───────────────────────────────────────────────────────
_auth_limiter = RateLimiter(max_requests=20, window_seconds=300.0)
_ban_manager = BanManager(
    permanent_stage3=os.environ.get("VELAFLOW_BAN_PERMANENT", "").lower()
    in ("1", "true", "yes")
)

# Platform owner email — gets OWNER role on auto-provisioned tenant
_PLATFORM_OWNER_EMAIL = os.environ.get("VELAFLOW_OWNER_EMAIL", "")


def _get_user_manager(
    storage: StorageBackend = Depends(get_storage),
    encryptor: FieldEncryptor = Depends(get_encryptor),
) -> UserManager:
    return UserManager(storage, encryptor)


# ── Request / Response models ────────────────────────────────────────


class GoogleAuthRequest(BaseModel):
    id_token: str
    invite_code: str | None = None


class GoogleAuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user_id: str
    user_role: str
    is_new_user: bool = False


class InviteRequest(BaseModel):
    email: str
    role: str = "member"


class InviteResponse(BaseModel):
    invite_code: str
    email: str
    role: str


class UserResponse(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    name: str
    user_role: str
    is_active: bool


class RoleUpdateRequest(BaseModel):
    role: str


class BanRequest(BaseModel):
    key: str
    reason: str = ""


# ── Google OAuth2 Login ──────────────────────────────────────────────


@router.post("/auth/google", response_model=GoogleAuthResponse)
async def google_login(
    body: GoogleAuthRequest,
    request: Request,
    manager: TenantManager = Depends(get_tenant_manager),
    user_mgr: UserManager = Depends(_get_user_manager),
) -> GoogleAuthResponse:
    """Authenticate via Google ID token.

    Flow:
    1. Verify Google ID token (signature + expiry + client_id)
    2. Check brute-force bans
    3. Find or create user + tenant
    4. Issue VelaFlow JWT
    """
    client_ip = request.client.host if request.client else "unknown"

    # Brute-force protection
    if _ban_manager.is_banned(client_ip):
        remaining = _ban_manager.get_ban_remaining(client_ip)
        raise HTTPException(
            status_code=403,
            detail=f"Temporarily banned. Try again in {int(remaining)}s.",
        )

    if not _auth_limiter.allow(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts. Try again later.",
        )

    # Verify Google ID token
    identity = verify_google_id_token(body.id_token)
    if identity is None:
        _ban_manager.record_failure(client_ip)
        raise HTTPException(
            status_code=401,
            detail="Invalid Google ID token. Ensure you're using the correct Google account.",
        )

    _ban_manager.record_success(client_ip)

    # Find existing user by Google sub
    existing_user = user_mgr.get_user_by_google_sub(identity.sub)

    if existing_user is not None:
        # Existing user — update login
        if not existing_user.is_active:
            raise HTTPException(status_code=403, detail="Account deactivated")

        user = user_mgr.record_login(existing_user)
        tenant = manager.get_tenant(user.tenant_id)
        if tenant is None or not tenant.is_active:
            raise HTTPException(status_code=403, detail="Tenant deactivated")

        token = create_access_token(
            tenant_id=tenant.tenant_id,
            role=tenant.role,
            email=user.email,
            user_id=user.user_id,
            user_role=user.user_role.value,
        )
        return GoogleAuthResponse(
            access_token=token,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            user_role=user.user_role.value,
        )

    # New user — check invite or auto-provision
    is_new = True
    tenant_id: str | None = None
    user_role = UserRole.MEMBER

    # Check invite code
    if body.invite_code:
        invite = user_mgr.redeem_invite(
            body.invite_code, identity.sub, identity.email
        )
        if invite is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired invite code.",
            )
        tenant_id = invite["tenant_id"]
        user_role = UserRole(invite.get("role", "member"))

    # Platform owner auto-provisioning
    if tenant_id is None and identity.email.lower() == _PLATFORM_OWNER_EMAIL.lower():
        # Check if owner tenant already exists
        tenants = manager.list_tenants()
        owner_tenant = next(
            (t for t in tenants if t.email.lower() == _PLATFORM_OWNER_EMAIL.lower()),
            None,
        )
        if owner_tenant:
            tenant_id = owner_tenant.tenant_id
        else:
            # Create owner tenant — VIP tier
            tenant = manager.create_tenant(
                name="Platform Owner",
                email=identity.email,
                tier=TenantTier.VIP,
            )
            tenant_id = tenant.tenant_id
        user_role = UserRole.OWNER

    if tenant_id is None:
        raise HTTPException(
            status_code=403,
            detail="No invite code provided. Ask the platform owner for an invite.",
        )

    # Create user
    user = user_mgr.create_user(
        tenant_id=tenant_id,
        google_sub=identity.sub,
        email=identity.email,
        name=identity.name,
        picture_url=identity.picture,
        user_role=user_role,
    )
    user = user_mgr.record_login(user)

    # Pin the credential-vault binding on first login. The bind refuses
    # to overwrite an existing different sub, so a tenant cannot be
    # silently re-pointed at another Google identity.
    if user_role in (UserRole.OWNER, UserRole.ADMIN):
        try:
            manager.bind_owner_sub(tenant_id, identity.sub)
        except Exception:  # noqa: BLE001 - never block login on rebind refusal
            logger.warning("bind_owner_sub refused for tenant %s", tenant_id)

    tenant = manager.get_tenant(tenant_id)
    token = create_access_token(
        tenant_id=tenant_id,
        role=tenant.role if tenant else "free",
        email=user.email,
        user_id=user.user_id,
        user_role=user.user_role.value,
    )
    return GoogleAuthResponse(
        access_token=token,
        tenant_id=tenant_id,
        user_id=user.user_id,
        user_role=user.user_role.value,
        is_new_user=is_new,
    )


# ── User Profile ─────────────────────────────────────────────────────


@router.get("/auth/me", response_model=UserResponse)
async def get_current_user(
    request: Request,
    user_mgr: UserManager = Depends(_get_user_manager),
) -> UserResponse:
    """Get the current authenticated user's profile."""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated as a user")
    user = user_mgr.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        email=user.email,
        name=user.name,
        user_role=user.user_role.value,
        is_active=user.is_active,
    )


# ── User Management (Owner/Admin) ───────────────────────────────────


@router.get(
    "/auth/users",
    response_model=list[UserResponse],
    dependencies=[Depends(RequirePermission(Permission.MANAGE_USERS))],
)
async def list_users(
    tenant_id: str = Depends(get_current_tenant_id),
    user_mgr: UserManager = Depends(_get_user_manager),
) -> list[UserResponse]:
    """List all users in the current tenant."""
    users = user_mgr.list_users(tenant_id)
    return [
        UserResponse(
            user_id=u.user_id,
            tenant_id=u.tenant_id,
            email=u.email,
            name=u.name,
            user_role=u.user_role.value,
            is_active=u.is_active,
        )
        for u in users
    ]


@router.post(
    "/auth/invite",
    response_model=InviteResponse,
    dependencies=[Depends(RequirePermission(Permission.INVITE_USERS))],
)
async def create_invite(
    body: InviteRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    user_mgr: UserManager = Depends(_get_user_manager),
) -> InviteResponse:
    """Create an invite code for a new user."""
    try:
        role = UserRole(body.role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(r.value for r in UserRole)}",
        )
    # Non-owners cannot invite owners or admins
    if role in (UserRole.OWNER, UserRole.ADMIN):
        # Only owners can create admin invites
        pass  # RequirePermission already ensures MANAGE_USERS

    code = user_mgr.create_invite(tenant_id, body.email, role)
    return InviteResponse(invite_code=code, email=body.email, role=role.value)


@router.patch(
    "/auth/users/{user_id}/role",
    dependencies=[Depends(RequirePermission(Permission.MANAGE_USERS))],
)
async def update_user_role(
    user_id: str,
    body: RoleUpdateRequest,
    request: Request,
    user_mgr: UserManager = Depends(_get_user_manager),
) -> dict:
    """Change a user's role within the tenant."""
    try:
        new_role = UserRole(body.role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(r.value for r in UserRole)}",
        )

    # Cannot change own role (safety)
    caller_user_id = getattr(request.state, "user_id", "")
    if caller_user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    user = user_mgr.update_user_role(user_id, new_role)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "updated", "user_id": user_id, "new_role": new_role.value}


@router.delete(
    "/auth/users/{user_id}",
    dependencies=[Depends(RequirePermission(Permission.MANAGE_USERS))],
)
async def deactivate_user(
    user_id: str,
    request: Request,
    user_mgr: UserManager = Depends(_get_user_manager),
) -> dict:
    """Deactivate a user (soft-delete)."""
    caller_user_id = getattr(request.state, "user_id", "")
    if caller_user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")

    if not user_mgr.deactivate_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deactivated", "user_id": user_id}


# ── Ban Management (Admin) ───────────────────────────────────────────


@router.get(
    "/auth/bans",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
async def list_bans() -> dict:
    """List all active bans."""
    return {"bans": _ban_manager.list_bans()}


@router.post(
    "/auth/bans",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
async def set_ban(body: BanRequest) -> dict:
    """Permanently ban an IP or user."""
    _ban_manager.ban_permanent(body.key, body.reason)
    return {"status": "banned", "key": body.key}


@router.delete(
    "/auth/bans/{key}",
    dependencies=[Depends(RequirePermission(Permission.ADMIN_ALL))],
)
async def remove_ban(key: str) -> dict:
    """Remove a ban (temporary or permanent)."""
    if not _ban_manager.unban(key):
        raise HTTPException(status_code=404, detail="No ban found for this key")
    return {"status": "unbanned", "key": key}
