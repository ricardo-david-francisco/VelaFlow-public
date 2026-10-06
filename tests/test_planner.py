"""Tests for the scoring engine and digest builders."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from brain.config import Settings
from brain.models import CalendarEvent, EmailAlert, Task
from brain.planner import (
    build_daily_digest,
    build_overdue_alert,
    build_weekend_digest,
    build_weekly_review,
    rank_tasks,
    score_task,
)


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        todoist_api_token="test-token",
        smtp_host="smtp.test.com",
        smtp_port=587,
        smtp_username="test@test.com",
        smtp_password="test",
        digest_from_email="test@test.com",
        digest_to_email="test@test.com",
    )


def _make_task(
    content: str = "Test task",
    due_date: date | None = None,
    priority: int = 1,
    labels: list[str] | None = None,
    duration_minutes: int | None = None,
    project_name: str | None = None,
    section_name: str = "",
) -> Task:
    return Task(
        id="t-1",
        content=content,
        priority=priority,
        due_date=due_date,
        is_recurring=False,
        due_has_time=False,
        labels=labels or [],
        project_id="p-1",
        project_name=project_name or "Inbox",
        section_id="",
        section_name=section_name,
        parent_id=None,
        url="https://todoist.com/showTask?id=t-1",
        duration_minutes=duration_minutes,
    )


# ========== score_task ==========


class TestScoreTask:
    def test_overdue_1_day(self, settings: Settings) -> None:
        task = _make_task(due_date=date.today() - timedelta(days=1))
        scored = score_task(task, settings)
        assert scored.score >= 20
        assert any("Overdue" in r for r in scored.reasons)

    def test_overdue_7_day_cap(self, settings: Settings) -> None:
        task = _make_task(due_date=date.today() - timedelta(days=30))
        scored = score_task(task, settings)
        # Cap at 7 * 20 = 140
        overdue_points = 140
        assert scored.score >= overdue_points

    def test_due_today(self, settings: Settings) -> None:
        task = _make_task(due_date=date.today())
        scored = score_task(task, settings)
        assert scored.score >= 25
        assert any("Due today" in r for r in scored.reasons)

    def test_due_tomorrow(self, settings: Settings) -> None:
        task = _make_task(due_date=date.today() + timedelta(days=1))
        scored = score_task(task, settings)
        assert scored.score >= 16

    def test_priority_p1(self, settings: Settings) -> None:
        task = _make_task(priority=4)  # Todoist: 4 = p1 (urgent)
        scored = score_task(task, settings)
        assert scored.score >= 18
        assert any("Priority" in r for r in scored.reasons)

    def test_focus_label(self, settings: Settings) -> None:
        task = _make_task(labels=["focus"])
        scored = score_task(task, settings)
        assert scored.score >= 14
        assert any("@focus" in r for r in scored.reasons)

    def test_weekend_label_in_weekend_mode(self, settings: Settings) -> None:
        task = _make_task(labels=["weekend"])
        scored = score_task(task, settings, weekend_mode=True)
        assert scored.score >= 10
        assert any("@weekend" in r for r in scored.reasons)

    def test_weekend_label_ignored_in_normal_mode(self, settings: Settings) -> None:
        task = _make_task(labels=["weekend"])
        scored = score_task(task, settings, weekend_mode=False)
        # Should not get weekend bonus
        assert not any("@weekend" in r for r in scored.reasons)

    def test_quick_win(self, settings: Settings) -> None:
        task = _make_task(duration_minutes=15)
        scored = score_task(task, settings)
        assert any("Quick win" in r for r in scored.reasons)

    def test_long_task_penalty(self, settings: Settings) -> None:
        task = _make_task(duration_minutes=180)
        scored = score_task(task, settings)
        assert any("Long task" in r for r in scored.reasons)

    def test_no_date_penalty(self, settings: Settings) -> None:
        task = _make_task(duration_minutes=60)  # avoid quick-win bonus
        scored = score_task(task, settings)
        assert scored.score < 0
        assert any("No due date" in r for r in scored.reasons)

    def test_combined_scores(self, settings: Settings) -> None:
        """Overdue + p1 + @focus should stack."""
        task = _make_task(
            due_date=date.today() - timedelta(days=2),
            priority=4,
            labels=["focus"],
        )
        scored = score_task(task, settings)
        expected_min = 40 + 18 + 14  # overdue + p1 + focus
        assert scored.score >= expected_min


# ========== rank_tasks ==========


class TestRankTasks:
    def test_higher_score_first(self, settings: Settings) -> None:
        t1 = _make_task(content="Low", priority=1)
        t2 = _make_task(content="High", due_date=date.today(), priority=4)
        ranked = rank_tasks([t1, t2], settings)
        assert ranked[0].task.content == "High"

    def test_empty_list(self, settings: Settings) -> None:
        ranked = rank_tasks([], settings)
        assert ranked == []


# ========== build_daily_digest ==========


class TestBuildDailyDigest:
    def test_contains_header(self, settings: Settings) -> None:
        tasks = [_make_task(content="Buy milk", due_date=date.today())]
        digest = build_daily_digest(tasks, [], [], settings)
        assert "Daily Briefing" in digest.subject
        assert "Buy milk" in digest.body_text

    def test_includes_overdue_section(self, settings: Settings) -> None:
        tasks = [_make_task(content="Old task", due_date=date.today() - timedelta(days=3))]
        digest = build_daily_digest(tasks, [], [], settings)
        assert "OVERDUE" in digest.body_text

    def test_includes_calendar(self, settings: Settings) -> None:
        from datetime import datetime, timezone
        events = [CalendarEvent(
            summary="Team standup",
            start=datetime.now(timezone.utc),
            end=None,
            all_day=False,
        )]
        digest = build_daily_digest([], events, [], settings)
        assert "Team standup" in digest.body_text

    def test_includes_emails(self, settings: Settings) -> None:
        emails = [EmailAlert(
            subject="Urgent from boss",
            sender="boss@company.com",
            sent_at=None,
        )]
        digest = build_daily_digest([], [], emails, settings)
        assert "Urgent from boss" in digest.body_text


# ========== build_overdue_alert ==========


class TestBuildOverdueAlert:
    def test_no_overdue_returns_none(self, settings: Settings) -> None:
        tasks = [_make_task(due_date=date.today())]
        assert build_overdue_alert(tasks, settings) is None

    def test_overdue_returns_message(self, settings: Settings) -> None:
        tasks = [_make_task(content="Pay rent", due_date=date.today() - timedelta(days=1))]
        result = build_overdue_alert(tasks, settings)
        assert result is not None
        assert "Pay rent" in result
        assert "OVERDUE" in result

    def test_truncates_at_10(self, settings: Settings) -> None:
        tasks = [
            _make_task(
                content=f"Task {i}",
                due_date=date.today() - timedelta(days=i),
            )
            for i in range(1, 15)
        ]
        result = build_overdue_alert(tasks, settings)
        assert result is not None
        assert "and 4 more" in result


# ========== build_weekly_review ==========


class TestBuildWeeklyReview:
    def test_contains_header(self, settings: Settings) -> None:
        digest = build_weekly_review([], [], settings)
        assert "Weekly Review" in digest.subject

    def test_shows_completed_count(self, settings: Settings) -> None:
        completed = [{"content": f"Done {i}"} for i in range(5)]
        digest = build_weekly_review([], completed, settings)
        assert "5" in digest.body_text
        assert "Completed" in digest.body_text
