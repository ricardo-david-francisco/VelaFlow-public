"""Tests for the Todoist client task parsing."""

from __future__ import annotations

from datetime import date

import pytest

from brain.models import Task
from brain.todoist import TodoistClient


class TestTaskParsing:
    """Test that raw Todoist API responses are correctly parsed into Task objects."""

    def test_parse_task_with_date(self) -> None:
        raw = {
            "id": "123",
            "content": "Buy groceries",
            "priority": 3,
            "due": {"date": "2024-06-15", "is_recurring": False, "datetime": None},
            "labels": ["errands"],
            "project_id": "p1",
            "section_id": None,
            "parent_id": None,
            "url": "https://todoist.com/showTask?id=123",
            "created_at": "2024-01-01T00:00:00Z",
            "duration": None,
        }
        task = TodoistClient._parse_task(raw)
        assert task.id == "123"
        assert task.content == "Buy groceries"
        assert task.due_date == date(2024, 6, 15)
        assert task.priority == 3
        assert task.labels == ["errands"]
        assert task.duration_minutes is None

    def test_parse_task_without_due(self) -> None:
        raw = {
            "id": "456",
            "content": "Someday task",
            "priority": 1,
            "due": None,
            "labels": [],
            "project_id": "p2",
            "section_id": None,
            "parent_id": None,
            "url": "https://todoist.com/showTask?id=456",
            "created_at": "2024-01-01T00:00:00Z",
            "duration": None,
        }
        task = TodoistClient._parse_task(raw)
        assert task.due_date is None
        assert task.is_recurring is False

    def test_parse_task_with_duration(self) -> None:
        raw = {
            "id": "789",
            "content": "Deep work session",
            "priority": 4,
            "due": {"date": "2024-06-15T09:00:00Z", "is_recurring": True, "datetime": "2024-06-15T09:00:00Z"},
            "labels": ["focus"],
            "project_id": "p1",
            "section_id": "s1",
            "parent_id": None,
            "url": "https://todoist.com/showTask?id=789",
            "created_at": "2024-01-01T00:00:00Z",
            "duration": {"amount": 90, "unit": "minute"},
        }
        task = TodoistClient._parse_task(raw)
        assert task.duration_minutes == 90
        assert task.is_recurring is True
        assert task.due_has_time is True
        assert task.priority == 4

    def test_parse_task_duration_hours(self) -> None:
        raw = {
            "id": "101",
            "content": "Long meeting",
            "priority": 2,
            "due": {"date": "2024-06-15", "is_recurring": False, "datetime": None},
            "labels": [],
            "project_id": "p1",
            "section_id": None,
            "parent_id": None,
            "url": "https://todoist.com/showTask?id=101",
            "created_at": "2024-01-01T00:00:00Z",
            "duration": {"amount": 2, "unit": "hour"},
        }
        task = TodoistClient._parse_task(raw)
        assert task.duration_minutes == 120
