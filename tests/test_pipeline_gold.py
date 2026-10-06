"""Tests for the Gold pipeline layer."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from brain.config import Settings
from brain.models import CalendarEvent, EmailAlert, Task
from brain.pipeline.gold import GoldLayer
from brain.storage.local import LocalStorageBackend


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def gold(storage):
    return GoldLayer(storage)


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


def _make_tasks():
    return [
        Task(
            id="t1",
            content="Urgent task",
            priority=4,
            due_date=date.today(),
            is_recurring=False,
            due_has_time=False,
            labels=["focus"],
            project_id="p1",
            project_name="Work",
            section_id="",
            section_name="",
            url="",
        ),
        Task(
            id="t2",
            content="Low priority",
            priority=1,
            due_date=date.today() + timedelta(days=7),
            is_recurring=False,
            due_has_time=False,
            labels=[],
            project_id="p1",
            project_name="Work",
            section_id="",
            section_name="",
            url="",
        ),
    ]


class TestGoldScoredTasks:
    def test_produce_scored_tasks(self, gold, settings):
        tasks = _make_tasks()
        scored = gold.produce_scored_tasks("tn_gold01", tasks, settings)
        assert len(scored) == 2
        # Urgent task should score higher
        assert scored[0].score > scored[1].score

    def test_persists_to_storage(self, gold, settings, storage):
        tasks = _make_tasks()
        gold.produce_scored_tasks("tn_gold01", tasks, settings)
        data = storage.read_json("gold/tn_gold01/scored_tasks.json")
        assert data is not None
        assert data["record_count"] == 2
        assert data["layer"] == "gold"

    def test_read_scored_tasks(self, gold, settings):
        tasks = _make_tasks()
        gold.produce_scored_tasks("tn_gold01", tasks, settings)
        data = gold.read_scored_tasks("tn_gold01")
        assert data is not None
        assert len(data["records"]) == 2

    def test_read_missing(self, gold):
        assert gold.read_scored_tasks("nonexistent") is None

    def test_empty_tasks(self, gold, settings):
        scored = gold.produce_scored_tasks("tn_gold01", [], settings)
        assert scored == []


class TestGoldDigest:
    def test_produce_daily_digest(self, gold, settings):
        tasks = _make_tasks()
        digest = gold.produce_daily_digest(
            "tn_gold01", tasks, [], [], settings
        )
        assert digest.subject != ""
        assert digest.body_text != ""

    def test_digest_persisted(self, gold, settings, storage):
        gold.produce_daily_digest("tn_gold01", _make_tasks(), [], [], settings)
        data = storage.read_json("gold/tn_gold01/daily_digest.json")
        assert data is not None
        assert data["digest_type"] == "daily"

    def test_read_daily_digest(self, gold, settings):
        gold.produce_daily_digest("tn_gold01", _make_tasks(), [], [], settings)
        data = gold.read_daily_digest("tn_gold01")
        assert data is not None
        assert "subject" in data
