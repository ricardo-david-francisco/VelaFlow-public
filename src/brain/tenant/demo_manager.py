"""Demo account lifecycle manager.

Creates, monitors, and expires time-limited VIP demo accounts.
Demo accounts allow enterprise prospects and friends to experience
full VIP features with strict guardrails:

- 7-day TTL (auto-expire, no renewal without admin action)
- Cost caps (max pipeline runs, max LLM calls during demo period)
- Full usage analytics (every action logged for admin insights)
- Error forwarding (admin notified immediately on errors)
- Encrypted audit trail (tamper-evident, secure from LXC attackers)

Security model: Demo accounts run with VIP-equivalent features but
are isolated at the tenant level. All demo data is encrypted with
the tenant's derived key and purged on expiry.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from brain.storage.base import StorageBackend
from brain.security.encryption import FieldEncryptor
from brain.tenant.manager import TenantManager
from brain.tenant.models import Tenant, TenantTier, TenantConfig

logger = logging.getLogger(__name__)

# Demo account defaults
DEFAULT_DEMO_DURATION_DAYS = 7
DEFAULT_DEMO_COST_CAP_PIPELINE = 50
DEFAULT_DEMO_COST_CAP_LLM = 100


class DemoManager:
    """Manage demo account lifecycle with cost caps and analytics.

    Usage:
        demo_mgr = DemoManager(tenant_mgr, storage, encryptor)
        tenant = demo_mgr.create_demo(
            name="Enterprise Prospect",
            email="prospect@company.com",
            created_by="admin@velaflow.com",
        )
        # Later...
        demo_mgr.check_expiry(tenant.tenant_id)  # auto-deactivates if expired
    """

    def __init__(
        self,
        tenant_mgr: TenantManager,
        storage: StorageBackend,
        encryptor: FieldEncryptor,
    ) -> None:
        self._tenant_mgr = tenant_mgr
        self._storage = storage
        self._encryptor = encryptor

    def create_demo(
        self,
        name: str,
        email: str,
        created_by: str,
        *,
        duration_days: int = DEFAULT_DEMO_DURATION_DAYS,
        cost_cap_pipeline: int = DEFAULT_DEMO_COST_CAP_PIPELINE,
        cost_cap_llm: int = DEFAULT_DEMO_COST_CAP_LLM,
        litellm_token: str = "",
    ) -> Tenant:
        """Create a time-limited VIP demo account.

        Returns:
            Tenant with VIP tier, demo flags set, and expiry date.
        """
        # Create as VIP tier so demo users get full experience
        tenant = self._tenant_mgr.create_tenant(name, email, TenantTier.VIP)

        # Set demo fields via storage update (Tenant is not frozen but
        # we update through storage to maintain consistency)
        tenant_data = self._load_tenant_data(tenant.tenant_id)
        if tenant_data:
            now = datetime.now(timezone.utc)
            tenant_data["is_demo"] = True
            tenant_data["demo_expires_at"] = (
                now + timedelta(days=duration_days)
            ).isoformat()
            tenant_data["demo_cost_cap_pipeline"] = cost_cap_pipeline
            tenant_data["demo_cost_cap_llm"] = cost_cap_llm
            tenant_data["demo_created_by"] = created_by
            self._save_tenant_data(tenant.tenant_id, tenant_data)

        # Pre-configure LLM proxy if token provided
        if litellm_token:
            # Demo accounts never complete real Google OAuth, so we bind a
            # synthetic owner_sub. The credential is still bound per
            # tenant via HKDF salt; revoking the demo (deactivate +
            # rotate pepper) renders the ciphertext unreadable.
            self._tenant_mgr.bind_owner_sub(
                tenant.tenant_id, f"demo:{tenant.tenant_id}"
            )
            self._tenant_mgr.update_config(
                tenant.tenant_id,
                litellm_proxy_token=litellm_token,
            )

        # Log creation event
        self._log_event(tenant.tenant_id, "demo_created", {
            "created_by": created_by,
            "duration_days": duration_days,
            "cost_cap_pipeline": cost_cap_pipeline,
            "cost_cap_llm": cost_cap_llm,
            "expires_at": tenant_data.get("demo_expires_at", "") if tenant_data else "",
        })

        logger.info(
            "Demo account created: %s (%s) by %s, expires in %d days",
            tenant.tenant_id,
            email,
            created_by,
            duration_days,
        )
        return tenant

    def check_expiry(self, tenant_id: str) -> bool:
        """Check if a demo account has expired. Auto-deactivates if so.

        Returns:
            True if still active, False if expired/deactivated.
        """
        tenant_data = self._load_tenant_data(tenant_id)
        if not tenant_data:
            return False
        if not tenant_data.get("is_demo"):
            return True  # Not a demo — always active

        expires_at_str = tenant_data.get("demo_expires_at", "")
        if not expires_at_str:
            return True  # No expiry set — treat as active

        expires_at = datetime.fromisoformat(expires_at_str)
        if not expires_at.tzinfo:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        if now >= expires_at:
            self._expire_demo(tenant_id, tenant_data)
            return False
        return True

    def check_demo_cost_cap(
        self,
        tenant_id: str,
        usage_type: str,
        current_count: int,
    ) -> bool:
        """Check if a demo account is within its cost cap.

        Args:
            usage_type: "pipeline_run" or "llm_call"
            current_count: Current usage count for this type.

        Returns:
            True if within cap, False if exceeded.
        """
        tenant_data = self._load_tenant_data(tenant_id)
        if not tenant_data or not tenant_data.get("is_demo"):
            return True  # Not a demo — no cap

        if usage_type == "pipeline_run":
            cap = tenant_data.get("demo_cost_cap_pipeline", DEFAULT_DEMO_COST_CAP_PIPELINE)
        elif usage_type == "llm_call":
            cap = tenant_data.get("demo_cost_cap_llm", DEFAULT_DEMO_COST_CAP_LLM)
        else:
            return True

        if current_count >= cap:
            self._log_event(tenant_id, "demo_cost_cap_reached", {
                "usage_type": usage_type,
                "count": current_count,
                "cap": cap,
            })
            logger.warning(
                "Demo cost cap reached for %s: %s %d/%d",
                tenant_id, usage_type, current_count, cap,
            )
            return False
        return True

    def get_usage_analytics(self, tenant_id: str) -> dict[str, Any]:
        """Get usage analytics for a demo account.

        Returns aggregated usage data for admin insight into how
        the demo user interacts with the platform.
        """
        events = self._load_events(tenant_id)
        tenant_data = self._load_tenant_data(tenant_id)

        analytics: dict[str, Any] = {
            "tenant_id": tenant_id,
            "is_demo": tenant_data.get("is_demo", False) if tenant_data else False,
            "created_by": tenant_data.get("demo_created_by", "") if tenant_data else "",
            "expires_at": tenant_data.get("demo_expires_at", "") if tenant_data else "",
            "total_events": len(events),
            "event_types": {},
            "errors": [],
            "last_activity": None,
        }

        for event in events:
            etype = event.get("event_type", "unknown")
            analytics["event_types"][etype] = analytics["event_types"].get(etype, 0) + 1
            if etype == "error":
                analytics["errors"].append(event)
            ts = event.get("timestamp")
            if ts and (analytics["last_activity"] is None or ts > analytics["last_activity"]):
                analytics["last_activity"] = ts

        return analytics

    def log_demo_error(
        self,
        tenant_id: str,
        error_type: str,
        error_detail: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Log an error for a demo account and flag for admin review.

        All demo errors are stored encrypted so the admin can
        diagnose issues and improve the platform.
        """
        self._log_event(tenant_id, "error", {
            "error_type": error_type,
            "error_detail": error_detail[:1000],  # Truncate to prevent log flooding
            "context": context or {},
        })
        logger.error(
            "Demo error [%s] %s: %s",
            tenant_id, error_type, error_detail[:200],
        )

    def list_demo_accounts(self) -> list[dict[str, Any]]:
        """List all demo accounts with their status."""
        tenants = self._tenant_mgr.list_tenants()
        demos = []
        for tenant in tenants:
            tenant_data = self._load_tenant_data(tenant.tenant_id)
            if tenant_data and tenant_data.get("is_demo"):
                expires_at = tenant_data.get("demo_expires_at", "")
                is_expired = False
                if expires_at:
                    exp = datetime.fromisoformat(expires_at)
                    if not exp.tzinfo:
                        exp = exp.replace(tzinfo=timezone.utc)
                    is_expired = datetime.now(timezone.utc) >= exp
                demos.append({
                    "tenant_id": tenant.tenant_id,
                    "name": tenant.name,
                    "email": tenant.email,
                    "created_by": tenant_data.get("demo_created_by", ""),
                    "expires_at": expires_at,
                    "is_expired": is_expired,
                    "is_active": tenant.is_active,
                })
        return demos

    # ── Private helpers ──────────────────────────────────────────────

    def _expire_demo(self, tenant_id: str, tenant_data: dict) -> None:
        """Deactivate an expired demo account."""
        self._log_event(tenant_id, "demo_expired", {
            "expires_at": tenant_data.get("demo_expires_at", ""),
        })
        self._tenant_mgr.deactivate_tenant(tenant_id)
        logger.info("Demo account expired and deactivated: %s", tenant_id)

    def _log_event(
        self,
        tenant_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Store an encrypted audit event for the demo account."""
        event = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        # Encrypt the event payload
        event_json = json.dumps(event)
        encrypted = self._encryptor.encrypt(
            event_json, tenant_id, field_name="demo_audit"
        )

        events_path = f"tenants/{tenant_id}/demo_events.jsonl"
        existing = ""
        try:
            existing = self._storage.read_text(events_path) or ""
        except Exception as exc:
            logger.debug("demo events read suppressed: %s", exc)
        self._storage.write_text(events_path, existing + encrypted + "\n")

    def _load_events(self, tenant_id: str) -> list[dict[str, Any]]:
        """Load and decrypt all audit events for a demo account."""
        events_path = f"tenants/{tenant_id}/demo_events.jsonl"
        try:
            raw = self._storage.read_text(events_path)
            if not raw:
                return []
        except Exception:
            return []

        events = []
        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            try:
                decrypted = self._encryptor.decrypt(
                    line.strip(), tenant_id, field_name="demo_audit"
                )
                events.append(json.loads(decrypted))
            except Exception:
                logger.warning("Failed to decrypt demo event for %s", tenant_id)
        return events

    def _load_tenant_data(self, tenant_id: str) -> dict[str, Any] | None:
        """Load raw tenant JSON data from storage."""
        try:
            return self._storage.read_json(f"tenants/{tenant_id}.json")
        except Exception as exc:
            logger.debug("tenant data load suppressed: %s", exc)
        return None

    def _save_tenant_data(self, tenant_id: str, data: dict[str, Any]) -> None:
        """Save raw tenant JSON data to storage."""
        self._storage.write_json(
            f"tenants/{tenant_id}.json",
            data,
        )
