"""Webhook routes — n8n orchestration integration.

n8n is the primary orchestration layer for VelaFlow. Every pipeline
stage, delivery channel, LLM operation, catalog query, and tenant
customization is triggered via these webhook endpoints.

Users fine-tune their VelaFlow by editing n8n workflows — no code required.
Each endpoint is async: it enqueues work and returns immediately so n8n
can chain downstream steps (delivery, polling, branching).

Available webhooks:
- /webhooks/pipeline       — Trigger Bronze → Silver → Gold pipeline
- /webhooks/digest         — Generate daily digest for delivery
- /webhooks/catalog        — Query catalog metadata (tables, lineage, stats)
- /webhooks/llm            — Trigger LLM text generation (cloud or local)
- /webhooks/tenant         — Tenant provisioning and configuration
- /webhooks/notion-sync    — Trigger Notion ↔ Todoist sync
- /webhooks/board-analysis — Trigger board/section analysis
- /webhooks/scoring-config — Update task scoring weights
- /webhooks/status/{id}    — Poll job completion status
- /webhooks/notebooklm    — Trigger NotebookLM extraction

Security:
- All endpoints require JWT authentication via RequirePermission
- Rate limiting: 20 requests/minute per tenant (configurable)
- Optional HMAC signature verification for n8n-to-API calls
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_task_queue,
)
from brain.queue.tasks import MessageType, QueueMessage, TaskQueue
from brain.security.rbac import Permission
from brain.security.resilience import RateLimiter
from brain.security.moderation import check_bulk_content
from brain.security.sanitization import sanitize_for_llm, has_prompt_injection

router = APIRouter()

# ── Rate Limiter (singleton per process) ───────────────────────────────
_rate_limiter = RateLimiter(
    max_requests=int(os.environ.get("WEBHOOK_RATE_LIMIT", "20")),
    window_seconds=60.0,
)

# ── Webhook signature secret (required in production) ─────────────────
_WEBHOOK_SECRET = os.environ.get("VELAFLOW_WEBHOOK_SECRET", "")
if not _WEBHOOK_SECRET and os.environ.get("VELAFLOW_ENV", "development") == "production":
    raise RuntimeError(
        "VELAFLOW_WEBHOOK_SECRET must be set in production. "
        "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )

# ── Job status tracking (in-memory — lightweight for N95) ─────────────
_job_status: dict[str, dict[str, Any]] = {}
_job_status_lock = threading.Lock()
_MAX_JOB_HISTORY = 1000


def _check_rate_limit(tenant_id: str) -> None:
    """Enforce per-tenant rate limiting on webhook calls."""
    if not _rate_limiter.allow(tenant_id):
        remaining = _rate_limiter.remaining(tenant_id)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({remaining} remaining). Try again in 60s.",
        )


def _verify_webhook_signature(
    request_body: bytes,
    signature: str | None,
) -> None:
    """Verify HMAC-SHA256 signature from n8n (if secret is configured)."""
    if not _WEBHOOK_SECRET:
        return  # Signature verification disabled
    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Webhook-Signature header")
    expected = hmac.new(
        _WEBHOOK_SECRET.encode(), request_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(f"sha256={expected}", signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


async def verify_webhook_signature(
    request: Request,
    x_webhook_signature: str | None = Header(default=None),
) -> None:
    """FastAPI dependency — verify HMAC signature on webhook requests."""
    if not _WEBHOOK_SECRET:
        return
    body = await request.body()
    _verify_webhook_signature(body, x_webhook_signature)


def _track_job(message_id: str, tenant_id: str, job_type: str) -> None:
    """Track job status for polling."""
    with _job_status_lock:
        if len(_job_status) >= _MAX_JOB_HISTORY:
            oldest = sorted(_job_status, key=lambda k: _job_status[k].get("created_at", ""))[:100]
            for k in oldest:
                _job_status.pop(k, None)
        _job_status[message_id] = {
            "status": "queued",
            "tenant_id": tenant_id,
            "type": job_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Request / Response Models ──────────────────────────────────────────

class WebhookPipelineRequest(BaseModel):
    """n8n webhook payload to trigger a pipeline run."""

    todoist_tasks: list[dict] = Field(default=[], max_length=10000)
    todoist_projects: list[dict] = Field(default=[], max_length=1000)
    todoist_sections: list[dict] = Field(default=[], max_length=1000)
    calendar_events: list[dict] = Field(default=[], max_length=10000)
    emails: list[dict] = Field(default=[], max_length=5000)
    weekend_mode: bool = False


class WebhookResponse(BaseModel):
    """Async acknowledgment — the job is queued, not completed."""

    message_id: str
    status: str = "queued"
    message: str = ""


class WebhookCatalogRequest(BaseModel):
    """n8n webhook payload to query the data catalog."""

    action: str = Field(
        ...,
        description="Catalog action: list_tables | get_lineage | table_stats",
    )
    schema_name: str = Field(default="gold", description="Target schema")
    table_name: str = Field(default="", description="Table name (for get_lineage)")


class WebhookCatalogResponse(BaseModel):
    """Catalog query result returned to n8n."""

    action: str
    data: dict[str, Any] = {}


class WebhookLLMRequest(BaseModel):
    """n8n webhook payload to trigger LLM generation."""

    prompt: str = Field(..., min_length=1, max_length=4000)
    system_prompt: str = Field(
        default="You are VelaFlow, an AI productivity assistant.",
        max_length=2000,
    )
    use_local: bool = Field(
        default=False,
        description="Force local LLM (premium tier only)",
    )


class WebhookLLMResponse(BaseModel):
    """LLM generation result returned to n8n."""

    message_id: str
    status: str = "queued"
    message: str = ""


class WebhookTenantRequest(BaseModel):
    """n8n webhook payload for tenant operations."""

    action: str = Field(
        ...,
        description="Tenant action: provision | update_config | get_status",
    )
    config: dict[str, Any] = Field(default_factory=dict)


class WebhookNotionSyncRequest(BaseModel):
    """n8n webhook payload to trigger Notion sync."""

    direction: str = Field(
        default="todoist_to_notion",
        description="Sync direction: todoist_to_notion | notion_to_todoist | bidirectional",
    )
    database_id: str = Field(default="", description="Target Notion database ID")


class WebhookBoardAnalysisRequest(BaseModel):
    """n8n webhook payload to trigger board/section analysis."""

    project_id: str = Field(default="", description="Todoist project ID to analyze")
    include_sections: bool = Field(default=True, description="Include section breakdown")


class WebhookScoringConfigRequest(BaseModel):
    """n8n webhook payload to update task scoring weights."""

    priority_weight: float = Field(default=1.0, ge=0.0, le=5.0)
    due_date_weight: float = Field(default=1.0, ge=0.0, le=5.0)
    label_bonus: float = Field(default=0.5, ge=0.0, le=3.0)
    overdue_penalty: float = Field(default=2.0, ge=0.0, le=10.0)


class WebhookNotebookLMRequest(BaseModel):
    """n8n webhook payload to trigger NotebookLM extraction."""

    source_type: str = Field(
        default="digest",
        description="Source type: digest | tasks | custom",
    )
    content: str = Field(default="", max_length=10000)


# ── Pipeline Webhook ───────────────────────────────────────────────────

@router.post(
    "/webhooks/pipeline",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.RUN_PIPELINE)), Depends(verify_webhook_signature)],
)
async def webhook_trigger_pipeline(
    body: WebhookPipelineRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Async pipeline trigger for n8n workflows.

    n8n calls this to kick off Bronze → Silver → Gold processing.
    The queue worker handles execution asynchronously.
    """
    _check_rate_limit(tenant_id)

    # Content moderation on task data before pipeline processing
    if body.todoist_tasks:
        mod_result = check_bulk_content(
            body.todoist_tasks,
            text_fields=["content", "description"],
            context="webhook_pipeline",
        )
        if not mod_result.is_allowed:
            raise HTTPException(
                status_code=422,
                detail=f"Content blocked: {mod_result.reason}",
            )

    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.PIPELINE_RUN,
        tenant_id=tenant_id,
        payload={
            "todoist_tasks": body.todoist_tasks,
            "todoist_projects": body.todoist_projects,
            "todoist_sections": body.todoist_sections,
            "calendar_events": body.calendar_events,
            "emails": body.emails,
            "weekend_mode": body.weekend_mode,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "pipeline_run")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message="Pipeline run queued for async processing",
    )


# ── Digest Webhook ─────────────────────────────────────────────────────

@router.post(
    "/webhooks/digest",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.GENERATE_DIGEST)), Depends(verify_webhook_signature)],
)
async def webhook_trigger_digest(
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Async digest generation for n8n delivery workflows.

    n8n calls this, then polls GET /digests/daily to retrieve the
    result and route it to email, WhatsApp, Notion, or custom webhook.
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.DIGEST_GENERATE,
        tenant_id=tenant_id,
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "digest_generate")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message="Digest generation queued",
    )


# ── Catalog Webhook ────────────────────────────────────────────────────

@router.post(
    "/webhooks/catalog",
    response_model=WebhookCatalogResponse,
    dependencies=[Depends(RequirePermission(Permission.READ_GOLD)), Depends(verify_webhook_signature)],
)
async def webhook_catalog_query(
    body: WebhookCatalogRequest,
    tenant_id: str = Depends(get_current_tenant_id),
) -> WebhookCatalogResponse:
    """Query the data catalog from n8n workflows.

    Enables n8n to check table freshness, row counts, and lineage
    before deciding whether to trigger a pipeline run.

    Actions:
    - list_tables: list all tables in a schema
    - get_lineage: get lineage for a specific table
    - table_stats: get row count and freshness
    """
    _check_rate_limit(tenant_id)
    # Import here to avoid circular imports at module level
    from brain.catalog.store import CatalogStore
    import os
    from functools import lru_cache
    from pathlib import Path

    @lru_cache(maxsize=1)
    def _get_catalog() -> CatalogStore:
        catalog_path = os.environ.get(
            "VELAFLOW_CATALOG_DB",
            str(Path(os.environ.get("VELAFLOW_DATA_DIR", "data/medallion")).parent / "catalog.db"),
        )
        return CatalogStore(catalog_path)

    catalog = _get_catalog()

    if body.action == "list_tables":
        tables = catalog.list_tables("velaflow", body.schema_name)
        return WebhookCatalogResponse(
            action=body.action,
            data={
                "schema": body.schema_name,
                "tables": [
                    {"name": t.name, "row_count": t.row_count, "updated_at": t.updated_at.isoformat()}
                    for t in tables
                ],
            },
        )
    elif body.action == "get_lineage":
        full_name = f"velaflow.{body.schema_name}.{body.table_name}"
        records = catalog.get_lineage(full_name)
        return WebhookCatalogResponse(
            action=body.action,
            data={
                "table": full_name,
                "lineage": [
                    {"source": r.source_table, "stage": r.pipeline_stage, "records": r.record_count}
                    for r in records
                ],
            },
        )
    elif body.action == "table_stats":
        table = catalog.get_table("velaflow", body.schema_name, body.table_name)
        if not table:
            return WebhookCatalogResponse(action=body.action, data={"error": "Table not found"})
        return WebhookCatalogResponse(
            action=body.action,
            data={
                "table": table.full_name,
                "row_count": table.row_count,
                "size_bytes": table.size_bytes,
                "format": table.format,
                "updated_at": table.updated_at.isoformat(),
            },
        )
    else:
        return WebhookCatalogResponse(
            action=body.action,
            data={"error": f"Unknown action: {body.action}"},
        )


# ── LLM Webhook ────────────────────────────────────────────────────────

@router.post(
    "/webhooks/llm",
    response_model=WebhookLLMResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.USE_LLM)), Depends(verify_webhook_signature)],
)
async def webhook_trigger_llm(
    body: WebhookLLMRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookLLMResponse:
    """Trigger LLM text generation from n8n workflows.

    n8n can use this to polish digest text, generate summaries,
    or create custom AI-powered outputs. Routes to cloud or local
    LLM based on tenant tier and the use_local flag.
    """
    _check_rate_limit(tenant_id)

    # Sanitize prompt and system_prompt before LLM processing
    safe_prompt = sanitize_for_llm(body.prompt, context="webhook_llm_prompt")
    safe_system = sanitize_for_llm(body.system_prompt, context="webhook_llm_system")

    # Block if system_prompt contains injection attempts
    if has_prompt_injection(body.system_prompt):
        raise HTTPException(
            status_code=422,
            detail="System prompt contains disallowed content",
        )

    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.LLM_GENERATE,
        tenant_id=tenant_id,
        payload={
            "prompt": safe_prompt,
            "system_prompt": safe_system,
            "use_local": body.use_local,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "llm_generate")
    return WebhookLLMResponse(
        message_id=message_id,
        status="queued",
        message="LLM generation queued",
    )


# ── Tenant Webhook ─────────────────────────────────────────────────────

@router.post(
    "/webhooks/tenant",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.MANAGE_TENANT)), Depends(verify_webhook_signature)],
)
async def webhook_tenant_operation(
    body: WebhookTenantRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Tenant provisioning and config from n8n workflows.

    Enables n8n admin workflows to provision tenants, update
    configurations, and check tenant health — all without code.

    Actions:
    - provision: create storage directories, catalog entries
    - update_config: update tenant settings
    - get_status: check tenant pipeline health
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.TENANT_OPERATION,
        tenant_id=tenant_id,
        payload={
            "action": body.action,
            "config": body.config,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "tenant_operation")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message=f"Tenant operation '{body.action}' queued",
    )


# ── Notion Sync Webhook ───────────────────────────────────────────────

@router.post(
    "/webhooks/notion-sync",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.RUN_PIPELINE)), Depends(verify_webhook_signature)],
)
async def webhook_notion_sync(
    body: WebhookNotionSyncRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Trigger Notion ↔ Todoist synchronization from n8n.

    Supports unidirectional and bidirectional sync. n8n can schedule
    this on a cron or trigger it after pipeline completion.
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.NOTION_SYNC,
        tenant_id=tenant_id,
        payload={
            "direction": body.direction,
            "database_id": body.database_id,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "notion_sync")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message=f"Notion sync ({body.direction}) queued",
    )


# ── Board Analysis Webhook ────────────────────────────────────────────

@router.post(
    "/webhooks/board-analysis",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.READ_GOLD)), Depends(verify_webhook_signature)],
)
async def webhook_board_analysis(
    body: WebhookBoardAnalysisRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Trigger board/section analysis from n8n.

    Analyzes Todoist projects and sections, producing insights
    about workload distribution, bottlenecks, and completion rates.
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.BOARD_ANALYSIS,
        tenant_id=tenant_id,
        payload={
            "project_id": body.project_id,
            "include_sections": body.include_sections,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "board_analysis")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message="Board analysis queued",
    )


# ── Scoring Config Webhook ────────────────────────────────────────────

@router.post(
    "/webhooks/scoring-config",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.MANAGE_TENANT)), Depends(verify_webhook_signature)],
)
async def webhook_scoring_config(
    body: WebhookScoringConfigRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Update task scoring weights from n8n.

    Allows users to customize how tasks are prioritized without
    touching code. n8n can expose sliders/forms for these weights.
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.SCORING_CONFIG,
        tenant_id=tenant_id,
        payload={
            "priority_weight": body.priority_weight,
            "due_date_weight": body.due_date_weight,
            "label_bonus": body.label_bonus,
            "overdue_penalty": body.overdue_penalty,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "scoring_config")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message="Scoring configuration update queued",
    )


# ── Job Status Polling Endpoint ───────────────────────────────────────

@router.get(
    "/webhooks/status/{message_id}",
    dependencies=[Depends(RequirePermission(Permission.VIEW_PIPELINE_RUNS))],
)
async def webhook_job_status(
    message_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
) -> dict[str, Any]:
    """Poll job completion status from n8n.

    n8n can use this to wait for async jobs to finish before
    proceeding with downstream steps (delivery, notification).
    """
    job = None
    with _job_status_lock:
        job = _job_status.get(message_id)
    if not job or job.get("tenant_id") != tenant_id:
        return {"message_id": message_id, "status": "unknown", "message": "Job not found or expired"}
    return {"message_id": message_id, **job}


# ── NotebookLM Webhook ────────────────────────────────────────────────

@router.post(
    "/webhooks/notebooklm",
    response_model=WebhookResponse,
    status_code=202,
    dependencies=[Depends(RequirePermission(Permission.USE_LLM)), Depends(verify_webhook_signature)],
)
async def webhook_notebooklm(
    body: WebhookNotebookLMRequest,
    tenant_id: str = Depends(get_current_tenant_id),
    queue: TaskQueue = Depends(get_task_queue),
) -> WebhookResponse:
    """Trigger NotebookLM extraction from n8n.

    Feeds digest or task data into NotebookLM for audio/podcast
    generation. n8n handles the delivery of the output.
    """
    _check_rate_limit(tenant_id)
    message_id = f"wh_{secrets.token_hex(8)}"
    msg = QueueMessage(
        message_id=message_id,
        message_type=MessageType.NOTEBOOKLM_EXTRACT,
        tenant_id=tenant_id,
        payload={
            "source_type": body.source_type,
            "content": body.content,
        },
    )
    queue.enqueue(msg)
    _track_job(message_id, tenant_id, "notebooklm")
    return WebhookResponse(
        message_id=message_id,
        status="queued",
        message="NotebookLM extraction queued",
    )
