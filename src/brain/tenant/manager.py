"""Tenant Manager — CRUD operations and lifecycle management.

Handles tenant registration, lookup, configuration, and data
isolation enforcement. Persists tenant metadata to storage.

Self-hosted tenant control plane: namespace provisioning, quota
with data access policies per catalog/schema.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from brain.security.encryption import (
    CredentialEncryptor,
    CredentialNotDecryptable,
    FieldEncryptor,
)
from brain.storage.base import StorageBackend
from brain.tenant.models import Tenant, TenantConfig, TenantTier

logger = logging.getLogger(__name__)

_TENANTS_PREFIX = "tenants/"


class TenantManager:
    """Manage tenant lifecycle and data isolation.

    Usage:
        manager = TenantManager(storage, encryptor)
        tenant = manager.create_tenant("acme", "admin@acme.com", TenantTier.STANDARD)
        manager.update_config(tenant.tenant_id, todoist_token="my-token")
    """

    def __init__(
        self,
        storage: StorageBackend,
        encryptor: FieldEncryptor | None = None,
        credential_encryptor: CredentialEncryptor | None = None,
    ) -> None:
        self._storage = storage
        self._encryptor = encryptor
        self._cred = credential_encryptor
        if self._encryptor is None:
            raise RuntimeError(
                "TenantManager requires a FieldEncryptor instance. "
                "Set VELAFLOW_MASTER_KEY environment variable."
            )
        # If the caller did not pass a CredentialEncryptor, auto-construct
        # one from VELAFLOW_CREDENTIAL_PEPPER. This keeps the public
        # constructor signature backwards-compatible while still enforcing
        # the pepper at construction time. The env var must be present;
        # CredentialEncryptor itself will refuse an empty pepper.
        if self._cred is None:
            import os as _os
            self._cred = CredentialEncryptor(_os.environ.get("VELAFLOW_CREDENTIAL_PEPPER"))
        # Any third-party-credential write performed without it will raise.
        # See ``update_config`` and ``decrypt_credential``.

    def create_tenant(
        self,
        name: str,
        email: str,
        tier: TenantTier = TenantTier.FREE,
    ) -> Tenant:
        """Register a new tenant.

        Generates a unique tenant ID, creates isolated storage partitions,
        and returns the new Tenant object.
        """
        tenant_id = self._generate_tenant_id()

        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            email=email,
            tier=tier,
            role=tier.value,
        )

        # Create storage partitions
        for layer in ("bronze", "silver", "gold", "runs"):
            marker_path = f"{layer}/{tenant_id}/.partition"
            self._storage.write_json(marker_path, {
                "tenant_id": tenant_id,
                "layer": layer,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        # Persist tenant metadata
        self._save_tenant(tenant)
        logger.info("Created tenant %s (%s, tier=%s)", tenant_id, name, tier.value)
        return tenant

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        """Look up a tenant by ID."""
        path = f"{_TENANTS_PREFIX}{tenant_id}.json"
        data = self._storage.read_json(path)
        if data is None:
            return None
        return self._from_dict(data)

    def list_tenants(self) -> list[Tenant]:
        """Return all registered tenants."""
        keys = self._storage.list_keys(_TENANTS_PREFIX)
        tenants = []
        for key in keys:
            if key.endswith(".partition"):
                continue
            data = self._storage.read_json(key)
            if data:
                tenants.append(self._from_dict(data))
        return tenants

    def update_tier(self, tenant_id: str, new_tier: TenantTier) -> Tenant | None:
        """Upgrade or downgrade a tenant's subscription tier."""
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            return None
        tenant.tier = new_tier
        tenant.role = new_tier.value
        tenant.quota = tenant.quota.for_tier(new_tier)
        self._save_tenant(tenant)
        logger.info("Updated tenant %s to tier %s", tenant_id, new_tier.value)
        return tenant

    def update_config(
        self,
        tenant_id: str,
        todoist_token: str | None = None,
        notion_token: str | None = None,
        litellm_proxy_token: str | None = None,
        gmail_imap_password: str | None = None,
        google_oauth_token: str | None = None,
        gemini_api_key: str | None = None,
        timezone: str | None = None,
        daily_top_task_limit: int | None = None,
        workday_start_hour: int | None = None,
        workday_end_hour: int | None = None,
        daily_digest_time: str | None = None,
        daily_digest_days: str | None = None,
        overdue_alert_enabled: bool | None = None,
        overdue_alert_interval_hours: int | None = None,
        weekend_planner_enabled: bool | None = None,
        weekly_review_enabled: bool | None = None,
        delivery_email: bool | None = None,
        delivery_whatsapp: bool | None = None,
        delivery_notion: bool | None = None,
        whatsapp_phone: str | None = None,
        source_todoist: bool | None = None,
        source_google_calendar: bool | None = None,
        source_gmail: bool | None = None,
        use_local_llm: bool | None = None,
        rag_enabled: bool | None = None,
    ) -> Tenant | None:
        """Update tenant integration configuration.

        Sensitive tokens are encrypted before storage.
        """
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            return None

        # Encrypted secrets — routed through CredentialEncryptor so the
        # ciphertext is bound to (pepper, tenant_id, owner_google_sub).
        # The platform refuses to accept a third-party credential before
        # the tenant has completed Google OAuth, because without
        # owner_google_sub the ciphertext could not be re-derived later.
        _secret_fields = {
            "todoist_api_token": todoist_token,
            "notion_api_token": notion_token,
            "litellm_proxy_token": litellm_proxy_token,
            "gmail_imap_password": gmail_imap_password,
            "google_oauth_token": google_oauth_token,
            "gemini_api_key": gemini_api_key,
        }
        if any(v is not None for v in _secret_fields.values()):
            if self._cred is None:
                raise RuntimeError(
                    "CredentialEncryptor is not configured; cannot accept "
                    "third-party credentials. Set VELAFLOW_CREDENTIAL_PEPPER."
                )
            if not tenant.owner_google_sub:
                raise RuntimeError(
                    "Tenant has not completed Google OAuth yet; refusing "
                    "to write encrypted credentials without owner_google_sub."
                )
        for field_name, value in _secret_fields.items():
            if value is not None:
                encrypted = self._cred.encrypt(
                    value,
                    tenant_id=tenant_id,
                    owner_sub=tenant.owner_google_sub,
                    field_name=field_name,
                )
                setattr(tenant.config, f"{field_name}_encrypted", encrypted)

        # Plain-text preferences
        _plain_fields: dict[str, Any] = {
            "timezone": timezone,
            "daily_top_task_limit": daily_top_task_limit,
            "workday_start_hour": workday_start_hour,
            "workday_end_hour": workday_end_hour,
            "daily_digest_time": daily_digest_time,
            "daily_digest_days": daily_digest_days,
            "overdue_alert_enabled": overdue_alert_enabled,
            "overdue_alert_interval_hours": overdue_alert_interval_hours,
            "weekend_planner_enabled": weekend_planner_enabled,
            "weekly_review_enabled": weekly_review_enabled,
            "delivery_email": delivery_email,
            "delivery_whatsapp": delivery_whatsapp,
            "delivery_notion": delivery_notion,
            "whatsapp_phone": whatsapp_phone,
            "source_todoist": source_todoist,
            "source_google_calendar": source_google_calendar,
            "source_gmail": source_gmail,
            "use_local_llm": use_local_llm,
            "rag_enabled": rag_enabled,
        }
        for attr, value in _plain_fields.items():
            if value is not None:
                setattr(tenant.config, attr, value)

        self._save_tenant(tenant)
        return tenant

    def deactivate_tenant(self, tenant_id: str) -> bool:
        """Soft-delete a tenant — sets is_active=False and wipes sensitive config."""
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            return False
        tenant.is_active = False
        # Wipe encrypted tokens so they cannot be recovered (loop avoids
        # Bandit B105 false positives on password-like identifiers).
        for _wipe_field in (
            "todoist_api_token_encrypted",
            "notion_api_token_encrypted",
            "google_oauth_token_encrypted",
            "gmail_imap_password_encrypted",
            "litellm_proxy_token_encrypted",
            "gemini_api_key_encrypted",
        ):
            setattr(tenant.config, _wipe_field, "")
        self._save_tenant(tenant)
        # Clean up tenant data partitions
        for layer in ("bronze", "silver", "gold", "runs", "vault", "users", "invites"):
            prefix = f"{layer}/{tenant_id}/"
            for key in self._storage.list_keys(prefix):
                self._storage.delete(key)
        logger.info("Deactivated tenant %s and cleaned up data", tenant_id)
        return True

    def decrypt_token(self, tenant_id: str, encrypted_token: str, field_name: str = "") -> str:
        """Decrypt a tenant's stored API token (legacy path; non-credential)."""
        return self._encryptor.decrypt(encrypted_token, tenant_id, field_name=field_name)

    def decrypt_credential(
        self, tenant: Tenant, encrypted: str, field_name: str
    ) -> str:
        """Decrypt a third-party credential bound to (pepper, tenant, owner_sub).

        Returns an empty string if the ciphertext is empty. Any other
        failure raises ``CredentialNotDecryptable`` to make the failure
        explicit at the call site (worker handlers must treat this as a
        permanent inability to fulfil the request, not a retryable error).
        """
        if not encrypted:
            return ""
        if self._cred is None:
            raise RuntimeError(
                "CredentialEncryptor not configured; cannot decrypt credentials."
            )
        if not tenant.owner_google_sub:
            raise CredentialNotDecryptable(
                f"tenant {tenant.tenant_id} has no owner_google_sub bound"
            )
        return self._cred.decrypt(
            encrypted,
            tenant_id=tenant.tenant_id,
            owner_sub=tenant.owner_google_sub,
            field_name=field_name,
        )

    def bind_owner_sub(self, tenant_id: str, google_sub: str) -> Tenant | None:
        """Pin the Google OIDC subject claim of the tenant owner.

        Idempotent on the same value; refuses to overwrite a different
        sub because doing so would orphan every encrypted credential.
        """
        if not google_sub:
            raise ValueError("google_sub is required")
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            return None
        if tenant.owner_google_sub and tenant.owner_google_sub != google_sub:
            raise RuntimeError(
                f"tenant {tenant_id} already bound to a different owner_sub; "
                "refusing to rebind. Run the credential rotation procedure."
            )
        if not tenant.owner_google_sub:
            tenant.owner_google_sub = google_sub
            self._save_tenant(tenant)
            logger.info("Bound tenant %s to owner_google_sub", tenant_id)
        return tenant

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _save_tenant(self, tenant: Tenant) -> None:
        path = f"{_TENANTS_PREFIX}{tenant.tenant_id}.json"
        self._storage.write_json(path, self._to_dict(tenant))

    @staticmethod
    def _generate_tenant_id() -> str:
        return f"tn_{secrets.token_hex(8)}"

    def _to_dict(self, t: Tenant) -> dict[str, Any]:
        # Encrypt api_key before storage (store hash for verification lookups)
        import hashlib
        api_key_encrypted = ""
        api_key_hash = ""
        if t.api_key:
            api_key_encrypted = self._encryptor.encrypt(
                t.api_key, t.tenant_id, field_name="api_key"
            )
            api_key_hash = hashlib.sha256(t.api_key.encode()).hexdigest()
        return {
            "tenant_id": t.tenant_id,
            "name": t.name,
            "email": t.email,
            "tier": t.tier.value,
            "role": t.role,
            "api_key_encrypted": api_key_encrypted,
            "api_key_hash": api_key_hash,
            "is_active": t.is_active,
            "created_at": t.created_at.isoformat(),
            "config": {
                "todoist_api_token_encrypted": t.config.todoist_api_token_encrypted,
                "notion_api_token_encrypted": t.config.notion_api_token_encrypted,
                "google_oauth_token_encrypted": t.config.google_oauth_token_encrypted,
                "gmail_imap_password_encrypted": t.config.gmail_imap_password_encrypted,
                "litellm_proxy_token_encrypted": t.config.litellm_proxy_token_encrypted,
                "gemini_api_key_encrypted": t.config.gemini_api_key_encrypted,
                "timezone": t.config.timezone,
                "daily_top_task_limit": t.config.daily_top_task_limit,
                "workday_start_hour": t.config.workday_start_hour,
                "workday_end_hour": t.config.workday_end_hour,
                "daily_digest_time": t.config.daily_digest_time,
                "daily_digest_days": t.config.daily_digest_days,
                "overdue_alert_enabled": t.config.overdue_alert_enabled,
                "overdue_alert_interval_hours": t.config.overdue_alert_interval_hours,
                "weekend_planner_enabled": t.config.weekend_planner_enabled,
                "weekly_review_enabled": t.config.weekly_review_enabled,
                "delivery_email": t.config.delivery_email,
                "delivery_whatsapp": t.config.delivery_whatsapp,
                "delivery_notion": t.config.delivery_notion,
                "whatsapp_phone": t.config.whatsapp_phone,
                "source_todoist": t.config.source_todoist,
                "source_google_calendar": t.config.source_google_calendar,
                "source_gmail": t.config.source_gmail,
                "use_local_llm": t.config.use_local_llm,
                "rag_enabled": t.config.rag_enabled,
                "rag_collection": t.config.rag_collection,
            },
            "stripe_customer_id": t.stripe_customer_id,
            "stripe_subscription_id": t.stripe_subscription_id,
            "owner_google_sub": t.owner_google_sub,
            "credential_schema_version": t.credential_schema_version,
            "is_demo": t.is_demo,
            "demo_expires_at": t.demo_expires_at.isoformat() if t.demo_expires_at else "",
            "demo_cost_cap_pipeline": t.demo_cost_cap_pipeline,
            "demo_cost_cap_llm": t.demo_cost_cap_llm,
            "demo_created_by": t.demo_created_by,
        }

    def _from_dict(self, d: dict[str, Any]) -> Tenant:
        cfg = d.get("config", {})
        # Decrypt api_key if stored encrypted; fall back to plaintext for migration
        api_key = ""
        if d.get("api_key_encrypted"):
            try:
                api_key = self._encryptor.decrypt(
                    d["api_key_encrypted"], d["tenant_id"], field_name="api_key"
                )
            except Exception:
                api_key = ""
        elif d.get("api_key"):
            # Legacy plaintext — will be re-encrypted on next save
            api_key = d["api_key"]
        return Tenant(
            tenant_id=d["tenant_id"],
            name=d["name"],
            email=d["email"],
            tier=TenantTier(d.get("tier", "free")),
            role=d.get("role", "free"),
            api_key=api_key,
            is_active=d.get("is_active", True),
            created_at=datetime.fromisoformat(d["created_at"])
            if "created_at" in d
            else datetime.now(timezone.utc),
            stripe_customer_id=d.get("stripe_customer_id", ""),
            stripe_subscription_id=d.get("stripe_subscription_id", ""),
            owner_google_sub=d.get("owner_google_sub", ""),
            credential_schema_version=d.get("credential_schema_version", 2),
            is_demo=d.get("is_demo", False),
            demo_expires_at=(
                datetime.fromisoformat(d["demo_expires_at"])
                if d.get("demo_expires_at")
                else None
            ),
            demo_cost_cap_pipeline=d.get("demo_cost_cap_pipeline", 50),
            demo_cost_cap_llm=d.get("demo_cost_cap_llm", 100),
            demo_created_by=d.get("demo_created_by", ""),
            config=TenantConfig(
                todoist_api_token_encrypted=cfg.get("todoist_api_token_encrypted", ""),
                notion_api_token_encrypted=cfg.get("notion_api_token_encrypted", ""),
                google_oauth_token_encrypted=cfg.get("google_oauth_token_encrypted", ""),
                gmail_imap_password_encrypted=cfg.get("gmail_imap_password_encrypted", ""),
                litellm_proxy_token_encrypted=cfg.get("litellm_proxy_token_encrypted", ""),
                gemini_api_key_encrypted=cfg.get("gemini_api_key_encrypted", ""),
                timezone=cfg.get("timezone", "Europe/Lisbon"),
                daily_top_task_limit=cfg.get("daily_top_task_limit", 5),
                workday_start_hour=cfg.get("workday_start_hour", 9),
                workday_end_hour=cfg.get("workday_end_hour", 18),
                daily_digest_time=cfg.get("daily_digest_time", "07:00"),
                daily_digest_days=cfg.get("daily_digest_days", "mon,tue,wed,thu,fri"),
                overdue_alert_enabled=cfg.get("overdue_alert_enabled", False),
                overdue_alert_interval_hours=cfg.get("overdue_alert_interval_hours", 4),
                weekend_planner_enabled=cfg.get("weekend_planner_enabled", False),
                weekly_review_enabled=cfg.get("weekly_review_enabled", False),
                delivery_email=cfg.get("delivery_email", True),
                delivery_whatsapp=cfg.get("delivery_whatsapp", False),
                delivery_notion=cfg.get("delivery_notion", False),
                whatsapp_phone=cfg.get("whatsapp_phone", ""),
                source_todoist=cfg.get("source_todoist", True),
                source_google_calendar=cfg.get("source_google_calendar", False),
                source_gmail=cfg.get("source_gmail", False),
                use_local_llm=cfg.get("use_local_llm", False),
                rag_enabled=cfg.get("rag_enabled", False),
                rag_collection=cfg.get("rag_collection", ""),
            ),
        )
