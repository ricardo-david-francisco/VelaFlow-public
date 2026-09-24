"""Role-Based Access Control (RBAC) — Tenant-level and user-level permissions.

Implements a two-layer permission model:
1. Tenant tier (free/standard/premium) → determines available features
2. User role (owner/admin/member/viewer/demo) → determines access within features

Integrated with the local data catalog (brain.catalog) for schema-level
grants and the zero-trust module for audit logging.
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field


class Permission(str, Enum):
    """Available permissions in the VelaFlow platform."""

    # Data access
    READ_BRONZE = "read:bronze"
    READ_SILVER = "read:silver"
    READ_GOLD = "read:gold"
    WRITE_BRONZE = "write:bronze"

    # Pipeline operations
    RUN_PIPELINE = "run:pipeline"
    VIEW_PIPELINE_RUNS = "view:pipeline_runs"

    # Tenant management
    MANAGE_TENANT = "manage:tenant"
    VIEW_TENANT = "view:tenant"

    # User management
    MANAGE_USERS = "manage:users"
    INVITE_USERS = "invite:users"

    # API operations
    GENERATE_DIGEST = "generate:digest"
    USE_LLM = "use:llm"
    USE_PREMIUM_LLM = "use:premium_llm"
    USE_RAG = "use:rag"
    USE_LOCAL_LLM = "use:local_llm"

    # API key vault (per-user secure key storage)
    MANAGE_API_KEYS = "manage:api_keys"

    # Admin
    ADMIN_ALL = "admin:all"


# Tier-based role definitions (what features the tenant can access)
_TIER_ROLES: dict[str, set[Permission]] = {
    "free": {
        Permission.READ_GOLD,
        Permission.VIEW_TENANT,
        Permission.RUN_PIPELINE,
        Permission.VIEW_PIPELINE_RUNS,
        Permission.GENERATE_DIGEST,
        Permission.MANAGE_API_KEYS,
    },
    "standard": {
        Permission.READ_BRONZE,
        Permission.READ_SILVER,
        Permission.READ_GOLD,
        Permission.WRITE_BRONZE,
        Permission.VIEW_TENANT,
        Permission.RUN_PIPELINE,
        Permission.VIEW_PIPELINE_RUNS,
        Permission.GENERATE_DIGEST,
        Permission.USE_LLM,
        Permission.MANAGE_API_KEYS,
    },
    "premium": {
        # Premium keeps the NotebookLM export path for RAG-like workflows.
        # Native on-box RAG (/api/v1/rag/*) is reserved for VIP — it is the
        # differentiator that justifies the €18/month tier against e.g.
        # ChatGPT Plus.
        Permission.READ_BRONZE,
        Permission.READ_SILVER,
        Permission.READ_GOLD,
        Permission.WRITE_BRONZE,
        Permission.VIEW_TENANT,
        Permission.MANAGE_TENANT,
        Permission.RUN_PIPELINE,
        Permission.VIEW_PIPELINE_RUNS,
        Permission.GENERATE_DIGEST,
        Permission.USE_LLM,
        Permission.USE_PREMIUM_LLM,
        Permission.USE_LOCAL_LLM,
        Permission.MANAGE_API_KEYS,
        Permission.MANAGE_USERS,
        Permission.INVITE_USERS,
    },
    "vip": {p for p in Permission},
    "demo": {
        # Demo gets VIP features but with external cost/time caps
        Permission.READ_BRONZE,
        Permission.READ_SILVER,
        Permission.READ_GOLD,
        Permission.WRITE_BRONZE,
        Permission.VIEW_TENANT,
        Permission.RUN_PIPELINE,
        Permission.VIEW_PIPELINE_RUNS,
        Permission.GENERATE_DIGEST,
        Permission.USE_LLM,
        Permission.USE_PREMIUM_LLM,
        Permission.USE_RAG,
        Permission.USE_LOCAL_LLM,
        Permission.MANAGE_API_KEYS,
    },
    "admin": {p for p in Permission},
}

# User-role permission caps (what the user can do within their tier's features)
_USER_ROLE_PERMS: dict[str, set[Permission]] = {
    "owner": {p for p in Permission},  # Full access
    "admin": {
        Permission.READ_BRONZE, Permission.READ_SILVER, Permission.READ_GOLD,
        Permission.WRITE_BRONZE,
        Permission.RUN_PIPELINE, Permission.VIEW_PIPELINE_RUNS,
        Permission.MANAGE_TENANT, Permission.VIEW_TENANT,
        Permission.MANAGE_USERS, Permission.INVITE_USERS,
        Permission.GENERATE_DIGEST, Permission.USE_LLM, Permission.USE_PREMIUM_LLM,
        Permission.MANAGE_API_KEYS,
    },
    "member": {
        Permission.READ_BRONZE, Permission.READ_SILVER, Permission.READ_GOLD,
        Permission.WRITE_BRONZE,
        Permission.RUN_PIPELINE, Permission.VIEW_PIPELINE_RUNS,
        Permission.VIEW_TENANT,
        Permission.GENERATE_DIGEST, Permission.USE_LLM,
        Permission.MANAGE_API_KEYS,
    },
    "viewer": {
        Permission.READ_GOLD,
        Permission.VIEW_TENANT,
        Permission.VIEW_PIPELINE_RUNS,
        Permission.GENERATE_DIGEST,
    },
    "demo": {
        Permission.READ_GOLD,
        Permission.VIEW_TENANT,
        Permission.VIEW_PIPELINE_RUNS,
    },
}


@dataclass
class RBACPolicy:
    """Evaluate permissions for a tenant/user based on their role.

    Two-layer check:
    1. Tier role: does the tenant's subscription allow this feature?
    2. User role: does this specific user have access within that feature set?

    Usage:
        policy = RBACPolicy()
        if policy.has_permission("standard", Permission.USE_LLM):
            # allow LLM usage
        if policy.has_user_permission("member", Permission.RUN_PIPELINE):
            # allow pipeline execution
    """

    custom_roles: dict[str, set[Permission]] = field(default_factory=dict)

    def get_permissions(self, role: str) -> set[Permission]:
        """Return the set of permissions for a tier role."""
        if role in self.custom_roles:
            return self.custom_roles[role]
        return _TIER_ROLES.get(role, set())

    def get_user_permissions(self, user_role: str) -> set[Permission]:
        """Return the set of permissions for a user role."""
        return _USER_ROLE_PERMS.get(user_role, set())

    def has_permission(self, role: str, permission: Permission) -> bool:
        """Check if a tier role has a specific permission."""
        permissions = self.get_permissions(role)
        return Permission.ADMIN_ALL in permissions or permission in permissions

    def has_user_permission(self, user_role: str, permission: Permission) -> bool:
        """Check if a user role has a specific permission."""
        perms = self.get_user_permissions(user_role)
        return Permission.ADMIN_ALL in perms or permission in perms

    def check_access(
        self, tier_role: str, user_role: str, permission: Permission
    ) -> bool:
        """Combined check: tier must allow AND user must have permission."""
        if not self.has_permission(tier_role, permission):
            return False
        if not user_role:
            return True  # Legacy: no user_role = tier-only check
        return self.has_user_permission(user_role, permission)

    def require_permission(self, role: str, permission: Permission) -> None:
        """Raise PermissionError if the role lacks the required permission."""
        if not self.has_permission(role, permission):
            raise PermissionError(
                f"Role '{role}' lacks required permission: {permission.value}"
            )

    @staticmethod
    def available_roles() -> list[str]:
        """Return list of built-in tier role names."""
        return list(_TIER_ROLES.keys())

    @staticmethod
    def available_user_roles() -> list[str]:
        """Return list of built-in user role names."""
        return list(_USER_ROLE_PERMS.keys())
