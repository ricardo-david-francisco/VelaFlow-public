"""Gold Layer — Scoring, enrichment, and AI-ready data preparation.

Reads validated silver data and produces enriched, scored, and
AI-consumption-ready datasets per tenant. This is the final
materialization layer before downstream consumers (API, LLM, dashboard).

On-prem engine: DuckDB gold tables queryable via SQL, with scoring
persisted for API serving and n8n delivery workflows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from brain.config import Settings
from brain.models import CalendarEvent, DigestResult, EmailAlert, ScoredTask, Task
from brain.planner import build_daily_digest, rank_tasks
from brain.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class GoldLayer:
    """Produce scored, enriched, AI-ready datasets from silver data."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    def produce_scored_tasks(
        self,
        tenant_id: str,
        tasks: list[Task],
        settings: Settings,
        weekend_mode: bool = False,
    ) -> list[ScoredTask]:
        """Score and rank tasks using the deterministic scoring engine.

        Reuses the existing VelaFlow scoring algorithm (planner.rank_tasks)
        and persists the gold-layer output for downstream consumption.
        """
        scored = rank_tasks(tasks, settings, weekend_mode=weekend_mode)

        gold_records = [
            {
                "task_id": st.task.id,
                "content": st.task.content,
                "project_name": st.task.project_name,
                "section_name": st.task.section_name,
                "priority": st.task.priority,
                "score": st.score,
                "reasons": st.reasons,
                "due_date": st.task.due_date.isoformat() if st.task.due_date else None,
                "labels": st.task.labels,
                "duration_minutes": st.task.duration_minutes,
            }
            for st in scored
        ]

        path = f"gold/{tenant_id}/scored_tasks.json"
        self._storage.write_json(path, {
            "tenant_id": tenant_id,
            "layer": "gold",
            "produced_at": datetime.now(timezone.utc).isoformat(),
            "weekend_mode": weekend_mode,
            "record_count": len(gold_records),
            "records": gold_records,
        })

        logger.info(
            "Gold produced %d scored tasks for tenant %s (top score: %d)",
            len(scored),
            tenant_id,
            scored[0].score if scored else 0,
        )
        return scored

    def produce_daily_digest(
        self,
        tenant_id: str,
        tasks: list[Task],
        events: list[CalendarEvent],
        emails: list[EmailAlert],
        settings: Settings,
    ) -> DigestResult:
        """Produce a daily digest from silver data.

        Reuses the existing VelaFlow digest builder and persists the
        gold-layer output as a consumable document.
        """
        digest = build_daily_digest(tasks, events, emails, settings)

        path = f"gold/{tenant_id}/daily_digest.json"
        self._storage.write_json(path, {
            "tenant_id": tenant_id,
            "layer": "gold",
            "produced_at": datetime.now(timezone.utc).isoformat(),
            "digest_type": "daily",
            "subject": digest.subject,
            "body_text": digest.body_text,
        })

        logger.info("Gold produced daily digest for tenant %s", tenant_id)
        return digest

    def read_scored_tasks(self, tenant_id: str) -> dict[str, Any] | None:
        """Read the latest gold scored tasks for a tenant."""
        path = f"gold/{tenant_id}/scored_tasks.json"
        return self._storage.read_json(path)

    def read_daily_digest(self, tenant_id: str) -> dict[str, Any] | None:
        """Read the latest gold daily digest for a tenant."""
        path = f"gold/{tenant_id}/daily_digest.json"
        return self._storage.read_json(path)
