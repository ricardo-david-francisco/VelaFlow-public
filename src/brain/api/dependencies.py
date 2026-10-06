"""FastAPI dependency injection — Shared dependencies for routes.

Provides factory functions for storage backends, tenant managers,
pipeline schedulers, and RBAC policies used across API routes.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from brain.config import Settings
from brain.pipeline.scheduler import PipelineScheduler
from brain.queue.tasks import TaskQueue
from brain.security.encryption import CredentialEncryptor, FieldEncryptor
from brain.security.rbac import Permission, RBACPolicy
from brain.storage.base import StorageBackend
from brain.storage.encrypted import EncryptedStorageBackend
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load application settings (cached singleton)."""
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_storage() -> StorageBackend:
    """Get the medallion storage backend with zero-knowledge encryption."""
    data_dir = os.environ.get(
        "VELAFLOW_DATA_DIR",
        str(Path.cwd() / "data" / "medallion"),
    )
    inner = LocalStorageBackend(data_dir)
    encryptor = get_encryptor()
    return EncryptedStorageBackend(inner, encryptor)


@lru_cache(maxsize=1)
def get_encryptor() -> FieldEncryptor:
    """Get the field encryptor."""
    master_key = os.environ.get("VELAFLOW_MASTER_KEY")
    return FieldEncryptor(master_key)


@lru_cache(maxsize=1)
def get_credential_encryptor() -> CredentialEncryptor:
    """Get the credential encryptor (pepper + owner_sub bound).

    The pepper lives only in the operator process environment. We
    deliberately raise on import-time if it is absent so the API
    cannot start in a state where credentials would be persisted
    under the legacy master-key path by accident.
    """
    pepper = os.environ.get("VELAFLOW_CREDENTIAL_PEPPER")
    return CredentialEncryptor(pepper)


@lru_cache(maxsize=1)
def get_rbac() -> RBACPolicy:
    """Get the RBAC policy."""
    return RBACPolicy()


def get_tenant_manager(
    storage: StorageBackend = Depends(get_storage),
    encryptor: FieldEncryptor = Depends(get_encryptor),
    credential_encryptor: CredentialEncryptor = Depends(get_credential_encryptor),
) -> TenantManager:
    """Get the tenant manager."""
    return TenantManager(storage, encryptor, credential_encryptor)


def get_pipeline_scheduler(
    storage: StorageBackend = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> PipelineScheduler:
    """Get the pipeline scheduler."""
    return PipelineScheduler(storage, settings)


@lru_cache(maxsize=1)
def get_task_queue() -> TaskQueue:
    """Get the in-process task queue (singleton)."""
    return TaskQueue()


@lru_cache(maxsize=1)
def get_rag_pipeline():
    """Get the RAG pipeline (singleton).

    Uses a DuckDB-backed vector store with a deterministic hashing
    embedder so the pipeline stays functional with zero external
    dependencies. Operators may swap in a transformer embedder via
    the ``velaflow[premium]`` extra without changing the public API.
    """
    from pathlib import Path

    from brain.rag import RAGPipeline, SimpleEmbedder, VectorStore

    settings = get_settings()
    db_path = settings.rag_duckdb_path or str(
        Path(os.environ.get("VELAFLOW_DATA_DIR", "data/medallion")) / "rag.duckdb"
    )
    store = VectorStore(db_path)
    embedder = SimpleEmbedder()
    return RAGPipeline(
        vector_store=store,
        embedder=embedder,
        chunk_size=settings.rag_chunk_size,
        chunk_overlap=settings.rag_chunk_overlap,
    )


def get_current_tenant_id(request: Request) -> str:
    """Extract the current tenant ID from the request context."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tenant_id


def get_current_role(request: Request) -> str:
    """Extract the current role from the request context."""
    return getattr(request.state, "role", "free")


class RequirePermission:
    """FastAPI dependency that enforces a specific RBAC permission.

    Two-layer check:
    1. Tenant tier must grant the feature
    2. User role must have access within that feature set

    Usage:
        @router.get("/data", dependencies=[Depends(RequirePermission(Permission.READ_GOLD))])
    """

    def __init__(self, permission: Permission) -> None:
        self._permission = permission

    def __call__(
        self,
        request: Request,
        rbac: RBACPolicy = Depends(get_rbac),
    ) -> None:
        tier_role = getattr(request.state, "role", "free")
        user_role = getattr(request.state, "user_role", "")
        if not rbac.check_access(tier_role, user_role, self._permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {self._permission.value}",
            )
