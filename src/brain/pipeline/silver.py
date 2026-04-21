"""Silver Layer — Data cleaning, deduplication, and schema validation.

Reads raw bronze data and produces cleaned, normalized, deduplicated
records with enforced schema contracts. This is where data quality
gates live.

On-prem engine: DuckDB SQL with ROW_NUMBER() dedup, MD5 hashing for
data quality, and PII masking via brain.security.pii.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from brain.models import CalendarEvent, EmailAlert, Task
from brain.security.pii import PIIDetector
from brain.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# Schema contracts — required fields per source
_TASK_REQUIRED_FIELDS = {"id", "content"}
_EVENT_REQUIRED_FIELDS = {"summary"}
_EMAIL_REQUIRED_FIELDS = {"subject", "sender"}


class SilverLayer:
    """Clean, validate, deduplicate, and normalize bronze data."""

    def __init__(
        self,
        storage: StorageBackend,
        pii_detector: PIIDetector | None = None,
    ) -> None:
        self._storage = storage
        self._pii = pii_detector or PIIDetector()

    def process_todoist(
        self, tenant_id: str, bronze_data: dict[str, Any]
    ) -> list[Task]:
        """Transform raw Todoist JSON into validated Task objects.

        Steps:
        1. Schema validation — drop records missing required fields
        2. Deduplication — by task ID
        3. Null handling — default empty strings for optional fields
        4. PII scanning — flag sensitive content
        5. Type normalization — parse dates, coerce priorities
        """
        raw_tasks = bronze_data.get("data", {}).get("tasks", [])
        raw_projects = bronze_data.get("data", {}).get("projects", [])
        raw_sections = bronze_data.get("data", {}).get("sections", [])

        project_map = {p["id"]: p.get("name", "") for p in raw_projects}
        section_map = {s["id"]: s.get("name", "") for s in raw_sections}

        # Step 1 + 2: Validate and deduplicate
        seen_ids: set[str] = set()
        valid_tasks: list[dict] = []
        dropped = 0

        for raw in raw_tasks:
            if not _TASK_REQUIRED_FIELDS.issubset(raw.keys()):
                dropped += 1
                continue
            task_id = str(raw["id"])
            if task_id in seen_ids:
                dropped += 1
                continue
            seen_ids.add(task_id)
            valid_tasks.append(raw)

        if dropped:
            logger.info(
                "Silver dropped %d invalid/duplicate tasks for tenant %s",
                dropped,
                tenant_id,
            )

        # Step 3-5: Normalize into Task objects
        tasks: list[Task] = []
        for raw in valid_tasks:
            content = str(raw.get("content", ""))
            description = str(raw.get("description", ""))

            # Content length enforcement (defense-in-depth)
            content = content[:2000]
            description = description[:5000]

            # Sanitize labels
            raw_labels = raw.get("labels", [])
            if isinstance(raw_labels, list):
                safe_labels = [
                    str(l)[:100] for l in raw_labels[:20]
                    if isinstance(l, str)
                ]
            else:
                safe_labels = []

            # PII scan — mask if detected
            if self._pii.has_pii(content):
                content = self._pii.mask(content)
            if self._pii.has_pii(description):
                description = self._pii.mask(description)

            task = Task(
                id=str(raw["id"]),
                content=content,
                description=description,
                project_id=str(raw.get("project_id", "")),
                project_name=project_map.get(raw.get("project_id", ""), ""),
                section_id=str(raw.get("section_id", "")),
                section_name=section_map.get(raw.get("section_id", ""), ""),
                priority=_safe_int(raw.get("priority"), default=1),
                labels=safe_labels,
                due_date=_parse_date(raw.get("due", {}).get("date"))
                if raw.get("due")
                else None,
                due_datetime=_parse_datetime(
                    raw.get("due", {}).get("datetime")
                )
                if raw.get("due")
                else None,
                due_has_time=bool(
                    raw.get("due", {}).get("datetime")
                )
                if raw.get("due")
                else False,
                duration_minutes=_parse_duration(raw.get("duration")),
                url=str(raw.get("url", "")),
                is_recurring=bool(
                    raw.get("due", {}).get("is_recurring", False)
                )
                if raw.get("due")
                else False,
                parent_id=str(raw["parent_id"]) if raw.get("parent_id") else None,
            )
            tasks.append(task)

        # Persist silver output
        silver_records = [_task_to_dict(t) for t in tasks]
        path = f"silver/{tenant_id}/todoist/tasks.json"
        self._storage.write_json(path, {
            "tenant_id": tenant_id,
            "source": "todoist",
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(silver_records),
            "records": silver_records,
        })

        logger.info(
            "Silver produced %d clean tasks for tenant %s",
            len(tasks),
            tenant_id,
        )
        return tasks

    def process_calendar(
        self, tenant_id: str, bronze_data: dict[str, Any]
    ) -> list[CalendarEvent]:
        """Transform raw calendar JSON into validated CalendarEvent objects."""
        raw_events = bronze_data.get("data", {}).get("events", [])
        seen: set[str] = set()
        events: list[CalendarEvent] = []
        dropped = 0

        for raw in raw_events:
            if not _EVENT_REQUIRED_FIELDS.issubset(raw.keys()):
                dropped += 1
                continue
            dedup_key = _event_dedup_key(raw)
            if dedup_key in seen:
                dropped += 1
                continue
            seen.add(dedup_key)

            summary = str(raw.get("summary", ""))
            if self._pii.has_pii(summary):
                summary = self._pii.mask(summary)

            events.append(CalendarEvent(
                summary=summary,
                start=_parse_datetime(raw.get("start")),
                end=_parse_datetime(raw.get("end")),
                all_day=bool(raw.get("all_day", False)),
            ))

        if dropped:
            logger.info("Silver dropped %d calendar events for tenant %s", dropped, tenant_id)

        path = f"silver/{tenant_id}/calendar/events.json"
        self._storage.write_json(path, {
            "tenant_id": tenant_id,
            "source": "calendar",
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(events),
            "records": [_event_to_dict(e) for e in events],
        })
        return events

    def process_gmail(
        self, tenant_id: str, bronze_data: dict[str, Any]
    ) -> list[EmailAlert]:
        """Transform raw Gmail JSON into validated EmailAlert objects."""
        raw_emails = bronze_data.get("data", {}).get("emails", [])
        seen: set[str] = set()
        alerts: list[EmailAlert] = []
        dropped = 0

        for raw in raw_emails:
            if not _EMAIL_REQUIRED_FIELDS.issubset(raw.keys()):
                dropped += 1
                continue
            dedup_key = _email_dedup_key(raw)
            if dedup_key in seen:
                dropped += 1
                continue
            seen.add(dedup_key)

            subject = str(raw.get("subject", ""))
            if self._pii.has_pii(subject):
                subject = self._pii.mask(subject)

            alerts.append(EmailAlert(
                subject=subject,
                sender=str(raw.get("sender", "")),
                sent_at=_parse_datetime(raw.get("sent_at")),
            ))

        if dropped:
            logger.info("Silver dropped %d emails for tenant %s", dropped, tenant_id)

        path = f"silver/{tenant_id}/gmail/emails.json"
        self._storage.write_json(path, {
            "tenant_id": tenant_id,
            "source": "gmail",
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(alerts),
            "records": [_email_to_dict(e) for e in alerts],
        })
        return alerts


# =============================================================================
# Serialization helpers
# =============================================================================

def _task_to_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "content": t.content,
        "description": t.description,
        "project_id": t.project_id,
        "project_name": t.project_name,
        "section_id": t.section_id,
        "section_name": t.section_name,
        "priority": t.priority,
        "labels": t.labels,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "due_datetime": t.due_datetime.isoformat() if t.due_datetime else None,
        "due_has_time": t.due_has_time,
        "duration_minutes": t.duration_minutes,
        "url": t.url,
        "is_recurring": t.is_recurring,
        "parent_id": t.parent_id,
    }


def _event_to_dict(e: CalendarEvent) -> dict[str, Any]:
    return {
        "summary": e.summary,
        "start": e.start.isoformat() if e.start else None,
        "end": e.end.isoformat() if e.end else None,
        "all_day": e.all_day,
    }


def _email_to_dict(e: EmailAlert) -> dict[str, Any]:
    return {
        "subject": e.subject,
        "sender": e.sender,
        "sent_at": e.sent_at.isoformat() if e.sent_at else None,
    }


# =============================================================================
# Parsing / normalization helpers
# =============================================================================

def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(val[:10])
    except ValueError:
        return None


def _parse_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


def _parse_duration(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, dict):
        amount = val.get("amount")
        unit = val.get("unit", "minute")
        if amount is None:
            return None
        minutes = int(amount)
        if unit == "day":
            minutes *= 1440
        return minutes
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _event_dedup_key(raw: dict) -> str:
    parts = f"{raw.get('summary', '')}|{raw.get('start', '')}|{raw.get('end', '')}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _email_dedup_key(raw: dict) -> str:
    parts = f"{raw.get('subject', '')}|{raw.get('sender', '')}|{raw.get('sent_at', '')}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]
