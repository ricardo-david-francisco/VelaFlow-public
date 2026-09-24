"""Pipeline routes — Trigger and monitor medallion pipeline runs."""

from __future__ import annotations

import threading
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_pipeline_scheduler,
    get_storage,
    get_tenant_manager,
)
from brain.pipeline.scheduler import PipelineScheduler
from brain.security.moderation import check_bulk_content
from brain.security.rbac import Permission
from brain.storage.base import StorageBackend
from brain.tenant.manager import TenantManager

# Atomic per-tenant daily quota counter (prevents race conditions)
_quota_lock = threading.Lock()
_daily_run_counts: dict[str, tuple[str, int]] = {}  # tenant_id -> (date_str, count)

router = APIRouter()


class PipelineRunRequest(BaseModel):
    """Request to trigger a pipeline run with raw data."""

    todoist_tasks: list[dict] = Field(default=[], max_length=10000)
    todoist_projects: list[dict] = Field(default=[], max_length=1000)
    todoist_sections: list[dict] = Field(default=[], max_length=1000)
    calendar_events: list[dict] = Field(default=[], max_length=10000)
    emails: list[dict] = Field(default=[], max_length=5000)
    weekend_mode: bool = False


class StageResultResponse(BaseModel):
    stage: str
    status: str
    record_count: int
    duration_ms: int
    error: str | None


class PipelineRunResponse(BaseModel):
    run_id: str
    tenant_id: str
    status: str
    duration_ms: int
    stages: list[StageResultResponse]


@router.post(
    "/pipelines/run",
    response_model=PipelineRunResponse,
    dependencies=[Depends(RequirePermission(Permission.RUN_PIPELINE))],
)
async def trigger_pipeline(
    body: PipelineRunRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    manager: TenantManager = Depends(get_tenant_manager),
    scheduler: PipelineScheduler = Depends(get_pipeline_scheduler),
    storage: StorageBackend = Depends(get_storage),
) -> PipelineRunResponse:
    """Trigger a full Bronze → Silver → Gold pipeline run."""
    tenant = manager.get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Atomic quota enforcement (O(1) counter, race-safe)
    today_str = date.today().isoformat()
    with _quota_lock:
        cached_date, count = _daily_run_counts.get(tenant_id, ("", 0))
        if cached_date != today_str:
            count = 0  # new day — reset counter
        if count >= tenant.quota.max_pipeline_runs_per_day:
            raise HTTPException(
                status_code=429,
                detail=f"Daily pipeline run quota exceeded ({tenant.quota.max_pipeline_runs_per_day}/day for {tenant.tier.value} tier).",
            )
        _daily_run_counts[tenant_id] = (today_str, count + 1)

    # Payload item count enforcement
    total_items = (
        len(body.todoist_tasks) + len(body.todoist_projects)
        + len(body.todoist_sections) + len(body.calendar_events)
        + len(body.emails)
    )
    if total_items > tenant.quota.max_tasks:
        raise HTTPException(
            status_code=413,
            detail=f"Payload exceeds task quota ({total_items} items, max {tenant.quota.max_tasks} for {tenant.tier.value} tier).",
        )

    # Content moderation — prevent illegal or abusive content
    for items, label in [
        (body.todoist_tasks, "todoist_tasks"),
        (body.calendar_events, "calendar_events"),
        (body.emails, "emails"),
    ]:
        mod_result = check_bulk_content(
            items, ["content", "subject", "summary", "name", "description"],
            context=f"pipeline:{label}",
        )
        if not mod_result.is_allowed:
            raise HTTPException(
                status_code=451,
                detail=f"Content blocked: {mod_result.reason}",
            )

    run = scheduler.execute(
        tenant=tenant,
        raw_todoist_tasks=body.todoist_tasks,
        raw_projects=body.todoist_projects,
        raw_sections=body.todoist_sections,
        raw_calendar_events=body.calendar_events,
        raw_emails=body.emails,
        weekend_mode=body.weekend_mode,
    )

    return PipelineRunResponse(
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        status=run.status.value,
        duration_ms=run.duration_ms,
        stages=[
            StageResultResponse(
                stage=s.stage.value,
                status=s.status.value,
                record_count=s.record_count,
                duration_ms=s.duration_ms,
                error=s.error,
            )
            for s in run.stages
        ],
    )


@router.get(
    "/pipelines/runs",
    dependencies=[Depends(RequirePermission(Permission.VIEW_PIPELINE_RUNS))],
)
async def list_pipeline_runs(
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> list[dict]:
    """List all pipeline runs for the current tenant."""
    prefix = f"runs/{tenant_id}/"
    keys = storage.list_keys(prefix)
    runs = []
    for key in keys:
        if key.endswith(".partition"):
            continue
        data = storage.read_json(key)
        if data:
            runs.append(data)
    return sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)
