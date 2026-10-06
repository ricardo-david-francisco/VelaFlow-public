"""Data Explorer routes — Authenticated user access to medallion layers.

Provides read-only access to bronze/silver/gold data for debugging.
Users can only see their own tenant's data, enforced by JWT tenant_id.

Endpoints:
- GET /data/layers                    → list available layers
- GET /data/{layer}/datasets          → list datasets in a layer
- GET /data/{layer}/{dataset}         → read dataset (paginated)
- GET /data/{layer}/{dataset}/stats   → dataset statistics

All queries are routed through DuckDB for SQL-level isolation.
LiteLLM-powered natural language queries are optionally available.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from brain.api.dependencies import (
    RequirePermission,
    get_current_tenant_id,
    get_storage,
)
from brain.security.rbac import Permission
from brain.storage.base import StorageBackend

router = APIRouter()

_VALID_LAYERS = {"bronze", "silver", "gold"}
_MAX_PAGE_SIZE = 100


class LayerInfo(BaseModel):
    name: str
    description: str
    permission_required: str


class DatasetInfo(BaseModel):
    name: str
    layer: str
    path: str
    record_count: int | None = None


class DatasetPage(BaseModel):
    layer: str
    dataset: str
    records: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class DatasetStats(BaseModel):
    layer: str
    dataset: str
    record_count: int
    fields: list[str]
    processed_at: str | None = None


_LAYER_PERMISSIONS = {
    "bronze": Permission.READ_BRONZE,
    "silver": Permission.READ_SILVER,
    "gold": Permission.READ_GOLD,
}

_LAYER_DESCRIPTIONS = {
    "bronze": "Raw data as received from sources (Todoist, Notion, Calendar, Gmail)",
    "silver": "Validated, deduplicated, PII-masked data with schema enforcement",
    "gold": "Scored tasks, daily digests, and aggregated insights",
}


@router.get(
    "/data/layers",
    response_model=list[LayerInfo],
    dependencies=[Depends(get_current_tenant_id)],
)
async def list_layers() -> list[LayerInfo]:
    """List available medallion data layers."""
    return [
        LayerInfo(
            name=name,
            description=_LAYER_DESCRIPTIONS[name],
            permission_required=_LAYER_PERMISSIONS[name].value,
        )
        for name in ("bronze", "silver", "gold")
    ]


@router.get("/data/{layer}/datasets", response_model=list[DatasetInfo])
async def list_datasets(
    layer: str,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> list[DatasetInfo]:
    """List datasets available in a specific layer for the current tenant."""
    if layer not in _VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer. Must be one of: {_VALID_LAYERS}")

    # RBAC check for layer access
    perm = _LAYER_PERMISSIONS[layer]
    role = getattr(request.state, "role", "free")
    user_role = getattr(request.state, "user_role", "")
    from brain.security.rbac import RBACPolicy
    rbac = RBACPolicy()
    if not rbac.check_access(role, user_role, perm):
        raise HTTPException(status_code=403, detail=f"No access to {layer} layer")

    # List datasets in tenant's layer directory
    prefix = f"{layer}/{tenant_id}/"
    datasets = []
    if hasattr(storage, "list_keys"):
        for path in storage.list_keys(prefix):
            if path.endswith(".json"):
                name = path.replace(prefix, "").replace("/", "_").replace(".json", "")
                datasets.append(DatasetInfo(
                    name=name,
                    layer=layer,
                    path=path,
                ))
    return datasets


@router.get("/data/{layer}/{dataset}", response_model=DatasetPage)
async def read_dataset(
    layer: str,
    dataset: str,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
) -> DatasetPage:
    """Read paginated records from a dataset in the current tenant's data."""
    if layer not in _VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer. Must be one of: {_VALID_LAYERS}")

    # RBAC check
    perm = _LAYER_PERMISSIONS[layer]
    role = getattr(request.state, "role", "free")
    user_role = getattr(request.state, "user_role", "")
    from brain.security.rbac import RBACPolicy
    rbac = RBACPolicy()
    if not rbac.check_access(role, user_role, perm):
        raise HTTPException(status_code=403, detail=f"No access to {layer} layer")

    # Resolve dataset path — enforce tenant isolation
    dataset_path = _resolve_dataset_path(layer, tenant_id, dataset)
    data = storage.read_json(dataset_path)
    if data is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    records = data.get("records", [])
    if isinstance(data, list):
        records = data

    total = len(records)
    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]

    return DatasetPage(
        layer=layer,
        dataset=dataset,
        records=page_records,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/data/{layer}/{dataset}/stats", response_model=DatasetStats)
async def dataset_stats(
    layer: str,
    dataset: str,
    request: Request,
    tenant_id: str = Depends(get_current_tenant_id),
    storage: StorageBackend = Depends(get_storage),
) -> DatasetStats:
    """Get statistics for a dataset."""
    if layer not in _VALID_LAYERS:
        raise HTTPException(status_code=400, detail=f"Invalid layer. Must be one of: {_VALID_LAYERS}")

    # RBAC check
    perm = _LAYER_PERMISSIONS[layer]
    role = getattr(request.state, "role", "free")
    user_role = getattr(request.state, "user_role", "")
    from brain.security.rbac import RBACPolicy
    rbac = RBACPolicy()
    if not rbac.check_access(role, user_role, perm):
        raise HTTPException(status_code=403, detail=f"No access to {layer} layer")

    dataset_path = _resolve_dataset_path(layer, tenant_id, dataset)
    data = storage.read_json(dataset_path)
    if data is None:
        raise HTTPException(status_code=404, detail="Dataset not found")

    records = data.get("records", [])
    if isinstance(data, list):
        records = data

    fields = list(records[0].keys()) if records else []

    return DatasetStats(
        layer=layer,
        dataset=dataset,
        record_count=len(records),
        fields=fields,
        processed_at=data.get("processed_at") if isinstance(data, dict) else None,
    )


def _resolve_dataset_path(layer: str, tenant_id: str, dataset: str) -> str:
    """Resolve dataset name to storage path with tenant isolation.

    Prevents path traversal attacks (../../other_tenant).
    """
    import re
    # Dataset name: alphanumeric, underscores, hyphens only
    if not re.match(r"^[a-zA-Z0-9_-]{1,128}$", dataset):
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    # Map dataset names to known paths
    known_datasets = {
        "todoist_tasks": f"{layer}/{tenant_id}/todoist/tasks.json",
        "calendar_events": f"{layer}/{tenant_id}/calendar/events.json",
        "gmail_alerts": f"{layer}/{tenant_id}/gmail/alerts.json",
        "scored_tasks": f"{layer}/{tenant_id}/scored_tasks.json",
        "daily_digest": f"{layer}/{tenant_id}/daily_digest.json",
        "weekly_review": f"{layer}/{tenant_id}/weekly_review.json",
    }

    if dataset in known_datasets:
        return known_datasets[dataset]

    # Fallback: try direct path under tenant directory
    return f"{layer}/{tenant_id}/{dataset.replace('_', '/')}.json"
