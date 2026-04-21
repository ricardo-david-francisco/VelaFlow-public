"""Billing routes — Stripe integration for subscription management.

Provides:
- POST /billing/checkout    → create Stripe checkout session
- POST /billing/portal      → create Stripe customer portal session
- POST /webhooks/stripe     → handle Stripe webhook events
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from brain.api.dependencies import get_current_tenant_id, get_tenant_manager
from brain.tenant.manager import TenantManager
from brain.tenant.models import TenantTier

logger = logging.getLogger(__name__)
router = APIRouter()

_STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
_STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_STRIPE_PRICE_IDS = {
    "standard": os.environ.get("STRIPE_PRICE_STANDARD", ""),
    "premium": os.environ.get("STRIPE_PRICE_PREMIUM", ""),
    "vip": os.environ.get("STRIPE_PRICE_VIP", ""),
}


_ALLOWED_REDIRECT_HOSTS = {"app.velaflow.com", "localhost"}


def _validate_redirect_url(url: str) -> str:
    """Block open-redirect attacks by enforcing allow-listed hosts."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.hostname not in _ALLOWED_REDIRECT_HOSTS:
        raise HTTPException(400, "Redirect URL host not allowed")
    if parsed.scheme not in ("https", "http"):
        raise HTTPException(400, "Only HTTP(S) redirect URLs allowed")
    return url


class CheckoutRequest(BaseModel):
    tier: str
    success_url: str = "https://app.velaflow.com/dashboard?upgraded=true"
    cancel_url: str = "https://app.velaflow.com/dashboard?upgraded=false"


class CheckoutResponse(BaseModel):
    checkout_url: str


@router.post("/billing/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    manager: TenantManager = Depends(get_tenant_manager),
) -> CheckoutResponse:
    """Create a Stripe checkout session for tier upgrade."""
    if body.tier not in _STRIPE_PRICE_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {body.tier}")

    price_id = _STRIPE_PRICE_IDS[body.tier]
    if not price_id or not _STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing not configured")

    tenant = manager.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    try:
        import stripe
    except ImportError:
        raise HTTPException(status_code=503, detail="Stripe SDK not installed")

    stripe.api_key = _STRIPE_SECRET_KEY

    customer_id = tenant.stripe_customer_id
    if not customer_id:
        customer = stripe.Customer.create(
            email=tenant.email,
            metadata={"velaflow_tenant_id": tenant_id},
        )
        customer_id = customer.id
        tenant.stripe_customer_id = customer_id
        manager._save_tenant(tenant)

    safe_success = _validate_redirect_url(body.success_url)
    safe_cancel = _validate_redirect_url(body.cancel_url)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=safe_success,
        cancel_url=safe_cancel,
        metadata={"velaflow_tenant_id": tenant_id},
    )

    return CheckoutResponse(checkout_url=session.url)


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    manager: TenantManager = Depends(get_tenant_manager),
) -> dict:
    """Handle Stripe webhook events (subscription changes)."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not _STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhooks not configured")

    try:
        import stripe
    except ImportError:
        raise HTTPException(status_code=503, detail="Stripe SDK not installed")

    stripe.api_key = _STRIPE_SECRET_KEY

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, _STRIPE_WEBHOOK_SECRET
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
    event_data = event.get("data", {}) if isinstance(event, dict) else getattr(event, "data", {})
    event_obj = event_data.get("object", {}) if isinstance(event_data, dict) else getattr(event_data, "object", {})

    if event_type == "checkout.session.completed":
        tenant_id = None
        metadata = event_obj.get("metadata", {}) if isinstance(event_obj, dict) else {}
        tenant_id = metadata.get("velaflow_tenant_id")
        if tenant_id:
            subscription_id = event_obj.get("subscription", "")
            sub = stripe.Subscription.retrieve(subscription_id)
            sub_items = sub.get("items", {}) if isinstance(sub, dict) else getattr(sub, "items", {})
            sub_data = sub_items.get("data", []) if isinstance(sub_items, dict) else getattr(sub_items, "data", [])
            if sub_data:
                price_id = sub_data[0].get("price", {}).get("id", "")
                tier_map = {v: k for k, v in _STRIPE_PRICE_IDS.items() if v}
                tier_name = tier_map.get(price_id, "standard")
                new_tier = TenantTier(tier_name)
                tenant = manager.update_tier(tenant_id, new_tier)
                if tenant:
                    tenant.stripe_subscription_id = subscription_id
                    manager._save_tenant(tenant)
                    logger.info("Upgraded tenant %s to %s via Stripe", tenant_id, tier_name)

    elif event_type == "customer.subscription.deleted":
        metadata = event_obj.get("metadata", {}) if isinstance(event_obj, dict) else {}
        tenant_id = metadata.get("velaflow_tenant_id")
        if tenant_id:
            manager.update_tier(tenant_id, TenantTier.FREE)
            logger.info("Downgraded tenant %s to FREE (subscription cancelled)", tenant_id)

    elif event_type == "invoice.payment_failed":
        sub_id = event_obj.get("subscription", "")
        for tenant in manager.list_tenants():
            if tenant.stripe_subscription_id == sub_id:
                logger.warning("Payment failed for tenant %s", tenant.tenant_id)
                break

    return {"received": True}
