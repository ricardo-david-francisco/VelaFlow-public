"""Bronze Layer — Raw data ingestion.

Lands raw JSON payloads from external APIs (Todoist, Google Calendar,
Gmail, Notion) into tenant-isolated storage without any transformation.
This is the "land as-is" layer of the medallion architecture.

On-prem engine: DuckDB analytical tables via brain.engine.processor,
with catalog registration via brain.catalog.store.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from brain.storage.base import StorageBackend
from brain.tenant.models import Tenant

logger = logging.getLogger(__name__)


class BronzeLayer:
    """Ingest raw API data into tenant-partitioned bronze storage."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    def ingest_todoist(
        self,
        tenant: Tenant,
        raw_tasks: list[dict[str, Any]],
        raw_projects: list[dict[str, Any]] | None = None,
        raw_sections: list[dict[str, Any]] | None = None,
    ) -> str:
        """Land raw Todoist API response into bronze layer.

        Returns the batch ID for downstream tracking.
        """
        batch_id = self._make_batch_id(tenant.tenant_id, "todoist")
        payload = {
            "source": "todoist",
            "tenant_id": tenant.tenant_id,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "batch_id": batch_id,
            "data": {
                "tasks": raw_tasks,
                "projects": raw_projects or [],
                "sections": raw_sections or [],
            },
        }
        path = self._bronze_path(tenant.tenant_id, "todoist", batch_id)
        self._storage.write_json(path, payload)
        logger.info(
            "Bronze ingested %d Todoist tasks for tenant %s (batch %s)",
            len(raw_tasks),
            tenant.tenant_id,
            batch_id,
        )
        return batch_id

    def ingest_calendar(
        self,
        tenant: Tenant,
        raw_events: list[dict[str, Any]],
    ) -> str:
        """Land raw Google Calendar events into bronze layer."""
        batch_id = self._make_batch_id(tenant.tenant_id, "calendar")
        payload = {
            "source": "calendar",
            "tenant_id": tenant.tenant_id,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "batch_id": batch_id,
            "data": {"events": raw_events},
        }
        path = self._bronze_path(tenant.tenant_id, "calendar", batch_id)
        self._storage.write_json(path, payload)
        logger.info(
            "Bronze ingested %d calendar events for tenant %s",
            len(raw_events),
            tenant.tenant_id,
        )
        return batch_id

    def ingest_gmail(
        self,
        tenant: Tenant,
        raw_emails: list[dict[str, Any]],
    ) -> str:
        """Land raw Gmail alerts into bronze layer."""
        batch_id = self._make_batch_id(tenant.tenant_id, "gmail")
        payload = {
            "source": "gmail",
            "tenant_id": tenant.tenant_id,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "batch_id": batch_id,
            "data": {"emails": raw_emails},
        }
        path = self._bronze_path(tenant.tenant_id, "gmail", batch_id)
        self._storage.write_json(path, payload)
        logger.info(
            "Bronze ingested %d emails for tenant %s",
            len(raw_emails),
            tenant.tenant_id,
        )
        return batch_id

    def read_latest(
        self, tenant_id: str, source: str
    ) -> dict[str, Any] | None:
        """Read the most recent bronze batch for a tenant + source."""
        prefix = f"bronze/{tenant_id}/{source}/"
        batches = self._storage.list_keys(prefix)
        if not batches:
            return None
        latest = sorted(batches)[-1]
        return self._storage.read_json(latest)

    def list_batches(self, tenant_id: str, source: str) -> list[str]:
        """List all bronze batch IDs for a tenant + source."""
        prefix = f"bronze/{tenant_id}/{source}/"
        return sorted(self._storage.list_keys(prefix))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_batch_id(tenant_id: str, source: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{source}_{tenant_id}_{ts}"

    @staticmethod
    def _bronze_path(tenant_id: str, source: str, batch_id: str) -> str:
        return f"bronze/{tenant_id}/{source}/{batch_id}.json"
