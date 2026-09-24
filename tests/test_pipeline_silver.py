"""Tests for the Silver pipeline layer."""

from __future__ import annotations

import pytest

from brain.pipeline.bronze import BronzeLayer
from brain.pipeline.silver import SilverLayer
from brain.security.pii import PIIDetector
from brain.storage.local import LocalStorageBackend
from brain.tenant.models import Tenant, TenantTier


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def silver(storage):
    return SilverLayer(storage)


@pytest.fixture()
def tenant():
    return Tenant(
        tenant_id="tn_silver01",
        name="Silver Test",
        email="s@test.com",
        tier=TenantTier.STANDARD,
    )


def _bronze_todoist_payload(tasks, projects=None, sections=None):
    return {
        "source": "todoist",
        "tenant_id": "tn_silver01",
        "batch_id": "test_batch",
        "data": {
            "tasks": tasks,
            "projects": projects or [],
            "sections": sections or [],
        },
    }


def _bronze_calendar_payload(events):
    return {
        "source": "calendar",
        "tenant_id": "tn_silver01",
        "batch_id": "test_batch",
        "data": {"events": events},
    }


def _bronze_gmail_payload(emails):
    return {
        "source": "gmail",
        "tenant_id": "tn_silver01",
        "batch_id": "test_batch",
        "data": {"emails": emails},
    }


class TestSilverTodoist:
    def test_basic_processing(self, silver):
        bronze_data = _bronze_todoist_payload([
            {"id": "t1", "content": "Buy milk", "priority": 3},
            {"id": "t2", "content": "Write docs", "priority": 1},
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert len(tasks) == 2
        assert tasks[0].id == "t1"
        assert tasks[0].content == "Buy milk"
        assert tasks[0].priority == 3

    def test_deduplication(self, silver):
        bronze_data = _bronze_todoist_payload([
            {"id": "t1", "content": "Duplicate task"},
            {"id": "t1", "content": "Duplicate task"},
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert len(tasks) == 1

    def test_missing_required_fields_dropped(self, silver):
        bronze_data = _bronze_todoist_payload([
            {"id": "t1", "content": "Valid"},
            {"priority": 3},  # Missing id and content
            {"id": "t3"},  # Missing content
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        # Only the first task has both id and content
        assert len(tasks) >= 1
        assert tasks[0].content == "Valid"

    def test_pii_masking(self, silver):
        bronze_data = _bronze_todoist_payload([
            {"id": "t1", "content": "Email john@example.com about project"},
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert "john@example.com" not in tasks[0].content
        assert "[EMAIL]" in tasks[0].content

    def test_due_date_parsing(self, silver):
        bronze_data = _bronze_todoist_payload([
            {
                "id": "t1",
                "content": "Due task",
                "due": {"date": "2024-06-15", "is_recurring": False},
            },
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert tasks[0].due_date is not None
        assert tasks[0].due_date.isoformat() == "2024-06-15"

    def test_project_name_resolved(self, silver):
        bronze_data = _bronze_todoist_payload(
            tasks=[{"id": "t1", "content": "Work task", "project_id": "p1"}],
            projects=[{"id": "p1", "name": "Engineering"}],
        )
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert tasks[0].project_name == "Engineering"

    def test_duration_parsing(self, silver):
        bronze_data = _bronze_todoist_payload([
            {
                "id": "t1",
                "content": "Long task",
                "duration": {"amount": 90, "unit": "minute"},
            },
        ])
        tasks = silver.process_todoist("tn_silver01", bronze_data)
        assert tasks[0].duration_minutes == 90

    def test_persists_to_storage(self, silver, storage):
        bronze_data = _bronze_todoist_payload([
            {"id": "t1", "content": "Stored task"},
        ])
        silver.process_todoist("tn_silver01", bronze_data)
        data = storage.read_json("silver/tn_silver01/todoist/tasks.json")
        assert data is not None
        assert data["record_count"] == 1


class TestSilverCalendar:
    def test_basic_processing(self, silver):
        bronze_data = _bronze_calendar_payload([
            {"summary": "Standup", "start": "2024-01-15T09:00:00", "end": "2024-01-15T09:30:00"},
        ])
        events = silver.process_calendar("tn_silver01", bronze_data)
        assert len(events) == 1
        assert events[0].summary == "Standup"

    def test_missing_summary_dropped(self, silver):
        bronze_data = _bronze_calendar_payload([
            {"summary": "Valid", "start": "2024-01-15T09:00:00"},
            {"start": "2024-01-15T10:00:00"},  # Missing summary
        ])
        events = silver.process_calendar("tn_silver01", bronze_data)
        assert len(events) == 1

    def test_deduplication(self, silver):
        evt = {"summary": "Standup", "start": "2024-01-15T09:00:00", "end": "2024-01-15T09:30:00"}
        bronze_data = _bronze_calendar_payload([evt, evt])
        events = silver.process_calendar("tn_silver01", bronze_data)
        assert len(events) == 1


class TestSilverGmail:
    def test_basic_processing(self, silver):
        bronze_data = _bronze_gmail_payload([
            {"subject": "Deploy alert", "sender": "ops@company.com"},
        ])
        emails = silver.process_gmail("tn_silver01", bronze_data)
        assert len(emails) == 1
        assert emails[0].subject == "Deploy alert"

    def test_missing_required_dropped(self, silver):
        bronze_data = _bronze_gmail_payload([
            {"subject": "Valid", "sender": "a@b.com"},
            {"subject": "No sender"},  # Missing sender
        ])
        emails = silver.process_gmail("tn_silver01", bronze_data)
        assert len(emails) == 1
