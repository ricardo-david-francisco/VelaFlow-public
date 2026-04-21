"""Task routes — Per-tenant task scoring and management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_storage,
)
from brain.pipeline.gold import GoldLayer
from brain.security.rbac import Permission
from brain.storage.base import StorageBackend

router = APIRouter()


class ScoredTaskResponse(BaseModel):
    task_id: str
    content: str
    project_name: str
    section_name: str
    priority: int
    score: int
    reasons: list[str]
    due_date: str | None
    labels: list[str]
    duration_minutes: int | None


class ScoredTasksListResponse(BaseModel):
    tenant_id: str
    record_count: int
    weekend_mode: bool
    tasks: list[ScoredTaskResponse]


@router.get(
    "/tasks/scored",
    response_model=ScoredTasksListResponse,
    dependencies=[Depends(RequirePermission(Permission.READ_GOLD))],
)
async def get_scored_tasks(
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> ScoredTasksListResponse:
    """Get the latest scored and ranked tasks for the current tenant."""
    gold = GoldLayer(storage)
    data = gold.read_scored_tasks(tenant_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="No scored tasks found. Run the pipeline first.",
        )

    tasks = [
        ScoredTaskResponse(
            task_id=r["task_id"],
            content=r["content"],
            project_name=r.get("project_name", ""),
            section_name=r.get("section_name", ""),
            priority=r.get("priority", 1),
            score=r["score"],
            reasons=r.get("reasons", []),
            due_date=r.get("due_date"),
            labels=r.get("labels", []),
            duration_minutes=r.get("duration_minutes"),
        )
        for r in data.get("records", [])
    ]

    return ScoredTasksListResponse(
        tenant_id=tenant_id,
        record_count=len(tasks),
        weekend_mode=data.get("weekend_mode", False),
        tasks=tasks,
    )
