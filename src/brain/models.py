"""Data models for tasks, events, and digest results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Task:
    """A Todoist task with all relevant metadata."""

    id: str
    content: str
    description: str = ""
    project_id: str = ""
    project_name: str = ""
    section_id: str = ""
    section_name: str = ""
    priority: int = 1  # Todoist: 1=no priority, 4=urgent (p1)
    labels: list[str] = field(default_factory=list)
    due_date: date | None = None
    due_datetime: datetime | None = None
    due_has_time: bool = False
    duration_minutes: int | None = None
    url: str = ""
    is_recurring: bool = False
    added_at: datetime | None = None
    parent_id: str | None = None


@dataclass
class ScoredTask:
    """A task with a computed priority score and reasoning."""

    task: Task
    score: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class CalendarEvent:
    """A Google Calendar event."""

    summary: str
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False

    @property
    def duration_minutes(self) -> int:
        if self.start and self.end:
            return int((self.end - self.start).total_seconds() / 60)
        return 0


@dataclass
class EmailAlert:
    """An unread email flagged as important."""

    subject: str
    sender: str
    sent_at: datetime | None = None


@dataclass
class DigestResult:
    """Output of the digest/planning engine."""

    subject: str
    body_text: str
    body_html: str = ""
