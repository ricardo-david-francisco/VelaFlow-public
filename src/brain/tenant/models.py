"""Tenant data models — Multi-tenant subscription and configuration.

Namespace hierarchy for isolation: tenant → catalog → schema
hierarchy with per-tenant data access policies.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class TenantTier(str, Enum):
    """Subscription tiers controlling feature access and resource limits."""

    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"
    VIP = "vip"


class UserRole(str, Enum):
    """Per-user roles within a tenant (independent of tier)."""

    OWNER = "owner"        # Full admin — can manage users, billing, config
    ADMIN = "admin"        # Can manage users and config, cannot delete tenant
    MEMBER = "member"      # Standard access — run pipelines, view data
    VIEWER = "viewer"      # Read-only — view gold layer, digests
    DEMO = "demo"          # Restricted demo access — limited features, read-only


@dataclass
class TenantConfig:
    """Per-tenant integration configuration.

    Stores API tokens and preferences for each tenant's connected
    services. All secrets are encrypted at rest using the tenant's
    derived encryption key.
    """

    todoist_api_token_encrypted: str = ""
    notion_api_token_encrypted: str = ""
    google_oauth_token_encrypted: str = ""
    gmail_imap_password_encrypted: str = ""
    litellm_proxy_token_encrypted: str = ""
    # Per-tenant BYO Gemini key (zero-trust: platform never sees plaintext).
    # When set, overrides the platform default in Settings.google_ai_api_key.
    gemini_api_key_encrypted: str = ""

    # Preferences
    timezone: str = "Europe/Lisbon"
    daily_top_task_limit: int = 5
    workday_start_hour: int = 9
    workday_end_hour: int = 18

    # Pipeline schedule preferences
    daily_digest_time: str = "07:00"
    daily_digest_days: str = "mon,tue,wed,thu,fri"
    overdue_alert_enabled: bool = False
    overdue_alert_interval_hours: int = 4
    weekend_planner_enabled: bool = False
    weekly_review_enabled: bool = False

    # Delivery channel toggles
    delivery_email: bool = True
    delivery_whatsapp: bool = False
    delivery_notion: bool = False
    whatsapp_phone: str = ""

    # Source toggles
    source_todoist: bool = True
    source_google_calendar: bool = False
    source_gmail: bool = False

    # LLM preference (VIP/Premium only)
    use_local_llm: bool = False

    # RAG settings (VIP only — premium keeps NotebookLM export)
    rag_enabled: bool = False
    rag_collection: str = ""  # tenant-scoped vector collection name


@dataclass
class TenantQuota:
    """Resource quotas per tier.

    Controls pipeline runs, API calls, and LLM usage per billing cycle.
    """

    max_pipeline_runs_per_day: int = 10
    max_tasks: int = 500
    max_llm_calls_per_day: int = 20
    max_storage_mb: int = 100
    premium_llm_enabled: bool = False
    rag_enabled: bool = False
    local_llm_enabled: bool = False
    max_rag_documents: int = 0
    max_rag_queries_per_day: int = 0

    @classmethod
    def for_tier(cls, tier: TenantTier) -> TenantQuota:
        if tier == TenantTier.FREE:
            return cls(
                max_pipeline_runs_per_day=3,
                max_tasks=100,
                max_llm_calls_per_day=5,
                max_storage_mb=50,
                premium_llm_enabled=False,
                rag_enabled=False,
                local_llm_enabled=False,
                max_rag_documents=0,
                max_rag_queries_per_day=0,
            )
        if tier == TenantTier.STANDARD:
            return cls(
                max_pipeline_runs_per_day=20,
                max_tasks=1000,
                max_llm_calls_per_day=50,
                max_storage_mb=500,
                premium_llm_enabled=False,
                rag_enabled=False,
                local_llm_enabled=False,
                max_rag_documents=0,
                max_rag_queries_per_day=0,
            )
        if tier == TenantTier.PREMIUM:
            return cls(
                max_pipeline_runs_per_day=100,
                max_tasks=10000,
                max_llm_calls_per_day=200,
                max_storage_mb=5000,
                premium_llm_enabled=True,
                rag_enabled=True,
                local_llm_enabled=True,
                max_rag_documents=500,
                max_rag_queries_per_day=50,
            )
        # VIP
        return cls(
            max_pipeline_runs_per_day=999,
            max_tasks=50000,
            max_llm_calls_per_day=999,
            max_storage_mb=10000,
            premium_llm_enabled=True,
            rag_enabled=True,
            local_llm_enabled=True,
            max_rag_documents=5000,
            max_rag_queries_per_day=500,
        )


@dataclass
class Tenant:
    """A registered tenant in the VelaFlow platform.

    Each tenant has isolated storage partitions (bronze/silver/gold)
    and their own set of encrypted API credentials.
    """

    tenant_id: str
    name: str
    email: str
    tier: TenantTier = TenantTier.FREE
    role: str = "free"
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    config: TenantConfig = field(default_factory=TenantConfig)
    quota: TenantQuota = field(default_factory=TenantQuota)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    stripe_customer_id: str = ""
    stripe_subscription_id: str = ""

    # Credential vault binding (Round 18).
    # ``owner_google_sub`` is the Google OIDC subject claim of the first
    # OAuth login that created this tenant. It is pinned once and never
    # rewritten; rotating it would invalidate every encrypted credential.
    # Until it is set, the platform refuses to accept encrypted
    # third-party credentials for this tenant.
    owner_google_sub: str = ""
    credential_schema_version: int = 2

    # Demo account fields
    is_demo: bool = False
    demo_expires_at: datetime | None = None
    demo_cost_cap_pipeline: int = 50
    demo_cost_cap_llm: int = 100
    demo_created_by: str = ""  # admin who created the demo

    def __post_init__(self) -> None:
        if isinstance(self.tier, str):
            self.tier = TenantTier(self.tier)
        self.quota = TenantQuota.for_tier(self.tier)
        # Align role with tier
        if self.role == "free" and self.tier != TenantTier.FREE:
            self.role = self.tier.value


@dataclass
class User:
    """A user within a tenant.

    Users authenticate via Google OAuth2. Each user belongs to exactly
    one tenant and has a role that determines their permissions.
    The owner (first user) has full admin access.
    """

    user_id: str
    tenant_id: str
    google_sub: str                         # Google's unique subject identifier
    email: str
    name: str = ""
    picture_url: str = ""
    user_role: UserRole = UserRole.MEMBER
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login: datetime | None = None
    login_count: int = 0
