"""Dashboard API — endpoints for the user-facing dashboard.

Provides data for:
- Connection status (which services are connected)
- Pipeline schedule configuration
- Usage statistics
- Tenant overview
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brain.api.dependencies import get_current_tenant_id, get_tenant_manager
from brain.tenant.manager import TenantManager

router = APIRouter()


class ConnectionStatus(BaseModel):
    todoist: bool
    notion: bool
    google_calendar: bool
    gmail: bool
    whatsapp: bool


class PipelineConfig(BaseModel):
    daily_digest_time: str
    daily_digest_days: str
    overdue_alert_enabled: bool
    overdue_alert_interval_hours: int
    weekend_planner_enabled: bool
    weekly_review_enabled: bool
    delivery_email: bool
    delivery_whatsapp: bool
    delivery_notion: bool
    source_todoist: bool
    source_google_calendar: bool
    source_gmail: bool
    use_local_llm: bool


class UsageStats(BaseModel):
    pipeline_runs_today: int
    pipeline_runs_limit: int
    llm_calls_today: int
    llm_calls_limit: int
    tasks_count: int
    tasks_limit: int
    tier: str


class DashboardOverview(BaseModel):
    connections: ConnectionStatus
    pipeline: PipelineConfig
    usage: UsageStats


@router.get("/dashboard/overview", response_model=DashboardOverview)
async def get_dashboard(
    tenant_id: str = Depends(get_current_tenant_id),
    manager: TenantManager = Depends(get_tenant_manager),
) -> DashboardOverview:
    """Get full dashboard data for the current tenant."""
    tenant = manager.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    cfg = tenant.config

    connections = ConnectionStatus(
        todoist=bool(cfg.todoist_api_token_encrypted),
        notion=bool(cfg.notion_api_token_encrypted),
        google_calendar=bool(cfg.google_oauth_token_encrypted),
        gmail=bool(cfg.gmail_imap_password_encrypted),
        whatsapp=bool(cfg.whatsapp_phone),
    )

    pipeline = PipelineConfig(
        daily_digest_time=cfg.daily_digest_time,
        daily_digest_days=cfg.daily_digest_days,
        overdue_alert_enabled=cfg.overdue_alert_enabled,
        overdue_alert_interval_hours=cfg.overdue_alert_interval_hours,
        weekend_planner_enabled=cfg.weekend_planner_enabled,
        weekly_review_enabled=cfg.weekly_review_enabled,
        delivery_email=cfg.delivery_email,
        delivery_whatsapp=cfg.delivery_whatsapp,
        delivery_notion=cfg.delivery_notion,
        source_todoist=cfg.source_todoist,
        source_google_calendar=cfg.source_google_calendar,
        source_gmail=cfg.source_gmail,
        use_local_llm=cfg.use_local_llm,
    )

    # Load usage from worker module
    from brain.queue.worker import _daily_usage
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage_data = _daily_usage.get(tenant_id, {})
    if usage_data.get("date") != today:
        usage_data = {"pipeline_runs": 0, "llm_calls": 0}

    usage = UsageStats(
        pipeline_runs_today=usage_data.get("pipeline_runs", 0),
        pipeline_runs_limit=tenant.quota.max_pipeline_runs_per_day,
        llm_calls_today=usage_data.get("llm_calls", 0),
        llm_calls_limit=tenant.quota.max_llm_calls_per_day,
        tasks_count=0,
        tasks_limit=tenant.quota.max_tasks,
        tier=tenant.tier.value,
    )

    return DashboardOverview(
        connections=connections,
        pipeline=pipeline,
        usage=usage,
    )
