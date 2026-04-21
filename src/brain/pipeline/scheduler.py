"""Pipeline Scheduler — Orchestrates Bronze → Silver → Gold execution.

Provides a DAG-like execution model for the medallion pipeline,
with per-tenant isolation and stage-level error handling.

On-prem orchestration: n8n triggers via webhook endpoints, with
DuckDB analytical processing and catalog lineage tracking.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from brain.config import Settings
from brain.models import CalendarEvent, EmailAlert, ScoredTask, Task
from brain.pipeline.bronze import BronzeLayer
from brain.pipeline.gold import GoldLayer
from brain.pipeline.silver import SilverLayer
from brain.storage.base import StorageBackend
from brain.tenant.models import Tenant

logger = logging.getLogger(__name__)


class PipelineStage(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class StageResult:
    stage: PipelineStage
    status: PipelineStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    record_count: int = 0
    error: str | None = None
    duration_ms: int = 0


@dataclass
class PipelineRun:
    """Tracks a full Bronze → Silver → Gold pipeline execution."""

    run_id: str
    tenant_id: str
    status: PipelineStatus = PipelineStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    stages: list[StageResult] = field(default_factory=list)
    scored_tasks: list[ScoredTask] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return 0


class PipelineScheduler:
    """Orchestrate the full medallion pipeline for a tenant.

    Usage:
        scheduler = PipelineScheduler(storage, settings)
        run = scheduler.execute(tenant, raw_todoist_tasks=tasks)
    """

    def __init__(self, storage: StorageBackend, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._bronze = BronzeLayer(storage)
        self._silver = SilverLayer(storage)
        self._gold = GoldLayer(storage)

    def execute(
        self,
        tenant: Tenant,
        raw_todoist_tasks: list[dict[str, Any]] | None = None,
        raw_projects: list[dict[str, Any]] | None = None,
        raw_sections: list[dict[str, Any]] | None = None,
        raw_calendar_events: list[dict[str, Any]] | None = None,
        raw_emails: list[dict[str, Any]] | None = None,
        weekend_mode: bool = False,
    ) -> PipelineRun:
        """Execute the full medallion pipeline for a tenant.

        Returns a PipelineRun with stage-level results and the final
        scored tasks.
        """
        run_id = self._make_run_id(tenant.tenant_id)
        run = PipelineRun(
            run_id=run_id,
            tenant_id=tenant.tenant_id,
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )

        try:
            # ── Bronze Stage ──────────────────────────────────────
            bronze_result = self._execute_bronze(
                tenant,
                raw_todoist_tasks or [],
                raw_projects,
                raw_sections,
                raw_calendar_events or [],
                raw_emails or [],
            )
            run.stages.append(bronze_result)
            if bronze_result.status == PipelineStatus.FAILED:
                run.status = PipelineStatus.FAILED
                return run

            # ── Silver Stage ──────────────────────────────────────
            silver_result, tasks, events, emails = self._execute_silver(
                tenant.tenant_id
            )
            run.stages.append(silver_result)
            if silver_result.status == PipelineStatus.FAILED:
                run.status = PipelineStatus.FAILED
                return run

            # ── Gold Stage ────────────────────────────────────────
            gold_result, scored = self._execute_gold(
                tenant.tenant_id, tasks, events, emails, weekend_mode
            )
            run.stages.append(gold_result)
            run.scored_tasks = scored

            if gold_result.status == PipelineStatus.FAILED:
                run.status = PipelineStatus.FAILED
            else:
                run.status = PipelineStatus.COMPLETED

        except Exception as exc:
            logger.exception("Pipeline failed for tenant %s", tenant.tenant_id)
            run.status = PipelineStatus.FAILED
            run.stages.append(StageResult(
                stage=PipelineStage.BRONZE,
                status=PipelineStatus.FAILED,
                error=str(exc),
            ))
        finally:
            run.completed_at = datetime.now(timezone.utc)
            self._persist_run(run)

        return run

    # ------------------------------------------------------------------
    # Stage executors
    # ------------------------------------------------------------------

    def _execute_bronze(
        self,
        tenant: Tenant,
        raw_tasks: list[dict],
        raw_projects: list[dict] | None,
        raw_sections: list[dict] | None,
        raw_events: list[dict],
        raw_emails: list[dict],
    ) -> StageResult:
        start = time.monotonic()
        result = StageResult(
            stage=PipelineStage.BRONZE,
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        try:
            total = 0
            if raw_tasks:
                self._bronze.ingest_todoist(
                    tenant, raw_tasks, raw_projects, raw_sections
                )
                total += len(raw_tasks)
            if raw_events:
                self._bronze.ingest_calendar(tenant, raw_events)
                total += len(raw_events)
            if raw_emails:
                self._bronze.ingest_gmail(tenant, raw_emails)
                total += len(raw_emails)

            result.status = PipelineStatus.COMPLETED
            result.record_count = total
        except Exception as exc:
            result.status = PipelineStatus.FAILED
            result.error = str(exc)
            logger.error("Bronze stage failed: %s", exc)
        finally:
            result.completed_at = datetime.now(timezone.utc)
            result.duration_ms = int((time.monotonic() - start) * 1000)
        return result

    def _execute_silver(
        self, tenant_id: str
    ) -> tuple[StageResult, list[Task], list[CalendarEvent], list[EmailAlert]]:
        start = time.monotonic()
        result = StageResult(
            stage=PipelineStage.SILVER,
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        tasks: list[Task] = []
        events: list[CalendarEvent] = []
        emails: list[EmailAlert] = []

        try:
            bronze_todoist = self._bronze.read_latest(tenant_id, "todoist")
            if bronze_todoist:
                tasks = self._silver.process_todoist(tenant_id, bronze_todoist)

            bronze_cal = self._bronze.read_latest(tenant_id, "calendar")
            if bronze_cal:
                events = self._silver.process_calendar(tenant_id, bronze_cal)

            bronze_email = self._bronze.read_latest(tenant_id, "gmail")
            if bronze_email:
                emails = self._silver.process_gmail(tenant_id, bronze_email)

            total = len(tasks) + len(events) + len(emails)
            result.status = PipelineStatus.COMPLETED
            result.record_count = total
        except Exception as exc:
            result.status = PipelineStatus.FAILED
            result.error = str(exc)
            logger.error("Silver stage failed: %s", exc)
        finally:
            result.completed_at = datetime.now(timezone.utc)
            result.duration_ms = int((time.monotonic() - start) * 1000)

        return result, tasks, events, emails

    def _execute_gold(
        self,
        tenant_id: str,
        tasks: list[Task],
        events: list[CalendarEvent],
        emails: list[EmailAlert],
        weekend_mode: bool,
    ) -> tuple[StageResult, list[ScoredTask]]:
        start = time.monotonic()
        result = StageResult(
            stage=PipelineStage.GOLD,
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        scored: list[ScoredTask] = []

        try:
            scored = self._gold.produce_scored_tasks(
                tenant_id, tasks, self._settings, weekend_mode
            )
            self._gold.produce_daily_digest(
                tenant_id, tasks, events, emails, self._settings
            )
            result.status = PipelineStatus.COMPLETED
            result.record_count = len(scored)
        except Exception as exc:
            result.status = PipelineStatus.FAILED
            result.error = str(exc)
            logger.error("Gold stage failed: %s", exc)
        finally:
            result.completed_at = datetime.now(timezone.utc)
            result.duration_ms = int((time.monotonic() - start) * 1000)

        return result, scored

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_run_id(tenant_id: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"run_{tenant_id}_{ts}"

    def _persist_run(self, run: PipelineRun) -> None:
        """Save pipeline run metadata for observability."""
        path = f"runs/{run.tenant_id}/{run.run_id}.json"
        self._storage.write_json(path, {
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "status": run.status.value,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "duration_ms": run.duration_ms,
            "stages": [
                {
                    "stage": s.stage.value,
                    "status": s.status.value,
                    "record_count": s.record_count,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                }
                for s in run.stages
            ],
        })
