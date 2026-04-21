"""Tests for the full pipeline scheduler (Bronze â†’ Silver â†’ Gold)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from brain.config import Settings
from brain.pipeline.scheduler import PipelineScheduler, PipelineStatus, PipelineStage
from brain.storage.local import LocalStorageBackend
from brain.tenant.models import Tenant, TenantTier


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def settings():
    return Settings(
        todoist_api_token="test-token",
        smtp_host="smtp.test.com",
        smtp_port=587,
        smtp_username="test@test.com",
        smtp_password="test",
        digest_from_email="test@test.com",
        digest_to_email="test@test.com",
    )


@pytest.fixture()
def scheduler(storage, settings):
    return PipelineScheduler(storage, settings)


@pytest.fixture()
def tenant():
    return Tenant(
        tenant_id="tn_sched01",
        name="Scheduler Test",
        email="sched@test.com",
        tier=TenantTier.STANDARD,
    )


def _sample_raw_tasks():
    return [
        {
            "id": "t1",
            "content": "Write unit tests",
            "priority": 4,
            "due": {"date": date.today().isoformat()},
            "labels": ["focus"],
        },
        {
            "id": "t2",
            "content": "Review PR",
            "priority": 2,
            "labels": [],
        },
    ]


class TestPipelineScheduler:
    def test_full_pipeline_completes(self, scheduler, tenant):
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=_sample_raw_tasks(),
        )
        assert run.status == PipelineStatus.COMPLETED
        assert len(run.stages) == 3
        assert run.stages[0].stage == PipelineStage.BRONZE
        assert run.stages[1].stage == PipelineStage.SILVER
        assert run.stages[2].stage == PipelineStage.GOLD

    def test_scored_tasks_returned(self, scheduler, tenant):
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=_sample_raw_tasks(),
        )
        assert len(run.scored_tasks) == 2
        # Focus + due today task should score higher
        assert run.scored_tasks[0].score >= run.scored_tasks[1].score

    def test_run_id_generated(self, scheduler, tenant):
        run = scheduler.execute(tenant, raw_todoist_tasks=[])
        assert run.run_id.startswith("run_")
        assert tenant.tenant_id in run.run_id

    def test_run_persisted(self, scheduler, tenant, storage):
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=_sample_raw_tasks(),
        )
        path = f"runs/{tenant.tenant_id}/{run.run_id}.json"
        data = storage.read_json(path)
        assert data is not None
        assert data["status"] == "completed"

    def test_duration_tracked(self, scheduler, tenant):
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=_sample_raw_tasks(),
        )
        assert run.duration_ms >= 0
        for stage in run.stages:
            assert stage.duration_ms >= 0

    def test_empty_input(self, scheduler, tenant):
        run = scheduler.execute(tenant)
        assert run.status == PipelineStatus.COMPLETED

    def test_calendar_and_email_input(self, scheduler, tenant):
        run = scheduler.execute(
            tenant,
            raw_todoist_tasks=_sample_raw_tasks(),
            raw_calendar_events=[
                {"summary": "Standup", "start": "2024-01-15T09:00:00"},
            ],
            raw_emails=[
                {"subject": "Alert", "sender": "ops@co.com"},
            ],
        )
        assert run.status == PipelineStatus.COMPLETED
        # Bronze should have ingested all three sources
        assert run.stages[0].record_count >= 3
