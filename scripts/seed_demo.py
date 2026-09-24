"""Seed a demo tenant for the landing page "Try It Free" flow.

Usage:
    python scripts/seed_demo.py

Creates a time-limited VIP demo tenant with:
- Tier: VIP (full features, time-limited)
- 7-day TTL (auto-expires)
- Cost caps (50 pipeline runs, 100 LLM calls)
- Pre-loaded with sample tasks
- Global LLM proxy token (budget-capped)
- Encrypted audit trail for all actions
- Invite code printed to stdout for sharing
"""

from __future__ import annotations

import os
import sys

# Add project to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from brain.security.encryption import FieldEncryptor  # noqa: E402
from brain.storage.local import LocalStorageBackend  # noqa: E402
from brain.tenant.demo_manager import DemoManager  # noqa: E402
from brain.tenant.manager import TenantManager  # noqa: E402
from brain.tenant.user_manager import UserManager  # noqa: E402


def seed_demo() -> dict:
    """Create a demo tenant and return its details."""
    data_dir = os.environ.get("VELAFLOW_DATA_DIR", "data/medallion")
    master_key = os.environ.get("VELAFLOW_MASTER_KEY")
    if not master_key:
        print("ERROR: Set VELAFLOW_MASTER_KEY environment variable")
        sys.exit(1)

    storage = LocalStorageBackend(data_dir)
    encryptor = FieldEncryptor(master_key)
    manager = TenantManager(storage, encryptor)
    user_mgr = UserManager(storage, encryptor)
    demo_mgr = DemoManager(manager, storage, encryptor)

    # Create time-limited VIP demo tenant
    litellm_token = os.environ.get("LITELLM_DEMO_TOKEN", "")
    admin_email = os.environ.get("VELAFLOW_OWNER_EMAIL", "admin@velaflow.com")
    duration_days = int(os.environ.get("DEMO_DURATION_DAYS", "7"))
    cost_cap_pipeline = int(os.environ.get("DEMO_COST_CAP_PIPELINE", "50"))
    cost_cap_llm = int(os.environ.get("DEMO_COST_CAP_LLM", "100"))

    tenant = demo_mgr.create_demo(
        name="VelaFlow Demo",
        email="demo@velaflow.com",
        created_by=admin_email,
        duration_days=duration_days,
        cost_cap_pipeline=cost_cap_pipeline,
        cost_cap_llm=cost_cap_llm,
        litellm_token=litellm_token,
    )

    # Create an invite code for demo users
    invite = user_mgr.create_invite(
        tenant_id=tenant.tenant_id,
        email="*",
        role="demo",
        created_by="system",
    )

    result = {
        "tenant_id": tenant.tenant_id,
        "api_key": tenant.api_key,
        "invite_code": invite["code"] if isinstance(invite, dict) else invite,
    }
    return result


if __name__ == "__main__":
    result = seed_demo()
    # Write secrets to a file — never print raw keys to stdout (CI log safety)
    secrets_file = os.path.join(os.path.dirname(__file__), "..", ".demo_secrets")
    with open(secrets_file, "w") as f:
        f.write(f"TENANT_ID={result['tenant_id']}\n")
        f.write(f"API_KEY={result['api_key']}\n")
        f.write(f"INVITE_CODE={result['invite_code']}\n")
    print(f"Demo tenant created: {result['tenant_id']}")
    print(f"Secrets written to: {os.path.abspath(secrets_file)}")
    print("WARNING: Do not commit .demo_secrets to version control.")
