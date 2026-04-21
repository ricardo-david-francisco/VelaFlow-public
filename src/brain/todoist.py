"""Todoist API v1 client (unified REST + Sync)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import requests

from brain.config import Settings
from brain.models import Task

logger = logging.getLogger(__name__)

API_BASE = "https://api.todoist.com/api/v1"


class TodoistClient:
    """Todoist API client with read and write operations."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.todoist_api_token
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {self._token}"
        self._session.headers["Content-Type"] = "application/json"
        # SSL certificate verification MUST remain True.
        # Never set verify=False — Todoist API tokens would be exposed to MITM attacks.
        self._session.verify = True
        self._timeout = 30

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> TodoistClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> Any:
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, url: str, data: dict | None = None) -> Any:
        resp = self._session.post(url, json=data, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages from a cursor-paginated v1 endpoint."""
        params = dict(params or {})
        params.setdefault("limit", 200)
        results: list[dict] = []
        while True:
            data = self._get(url, params=params)
            results.extend(data.get("results", []))
            cursor = data.get("next_cursor")
            if not cursor:
                break
            params["cursor"] = cursor
        return results

    # ------------------------------------------------------------------
    # Read — Projects & Sections
    # ------------------------------------------------------------------

    def get_projects(self) -> list[dict]:
        """Return list of project dicts."""
        return self._paginate(f"{API_BASE}/projects")

    def get_project_map(self) -> dict[str, str]:
        """Return mapping of project_id → project_name."""
        return {p["id"]: p["name"] for p in self.get_projects()}

    def get_sections(self, project_id: str | None = None) -> list[dict]:
        """Return sections, optionally filtered by project."""
        params = {}
        if project_id:
            params["project_id"] = project_id
        return self._paginate(f"{API_BASE}/sections", params)

    def get_section_map(self, project_id: str | None = None) -> dict[str, str]:
        """Return mapping of section_id → section_name."""
        return {s["id"]: s["name"] for s in self.get_sections(project_id)}

    # ------------------------------------------------------------------
    # Read — Tasks
    # ------------------------------------------------------------------

    def get_tasks(
        self,
        project_id: str | None = None,
        section_id: str | None = None,
        label: str | None = None,
    ) -> list[Task]:
        """Fetch active tasks with optional filters."""
        params: dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        if section_id:
            params["section_id"] = section_id
        if label:
            params["label"] = label
        raw_tasks = self._paginate(f"{API_BASE}/tasks", params)
        project_map = self.get_project_map()
        section_map = self.get_section_map(project_id)
        return [self._parse_task(t, project_map, section_map) for t in raw_tasks]

    def get_filtered_tasks(self, query: str) -> list[Task]:
        """Fetch tasks using Todoist filter syntax via /tasks/filter."""
        params = {"query": query, "limit": 200}
        try:
            data = self._get(f"{API_BASE}/tasks/filter", params=params)
            raw_tasks = data.get("results", [])
        except requests.RequestException:
            logger.warning("Filter endpoint failed for query: %s", query)
            return []
        project_map = self.get_project_map()
        return [self._parse_task(t, project_map) for t in raw_tasks]

    def get_overdue_tasks(self) -> list[Task]:
        """Fetch tasks that are overdue."""
        return self.get_filtered_tasks("overdue")

    def get_today_tasks(self) -> list[Task]:
        """Fetch tasks due today (includes overdue)."""
        return self.get_filtered_tasks("today | overdue")

    def get_upcoming_tasks(self, days: int = 7) -> list[Task]:
        """Fetch tasks due in the next N days."""
        return self.get_filtered_tasks(f"{days} days")

    def get_weekend_tasks(self) -> list[Task]:
        """Fetch tasks due on Saturday or Sunday."""
        return self.get_filtered_tasks("saturday | sunday")

    def get_completed_tasks(self, since: str | None = None) -> list[dict]:
        """Fetch recently completed tasks.

        Args:
            since: ISO date string (e.g. '2026-04-10T00:00:00Z')
        """
        params: dict[str, Any] = {"limit": 200}
        if since:
            params["since"] = since
        try:
            data = self._get(
                f"{API_BASE}/tasks/completed/by_completion_date", params=params
            )
            return data.get("results", [])
        except requests.RequestException:
            logger.warning("Failed to fetch completed tasks.")
            return []

    # ------------------------------------------------------------------
    # Write — Task updates (NEVER deletes or completes tasks)
    # ------------------------------------------------------------------

    # Forbidden fields that must never be sent to the API
    _BLOCKED_FIELDS = frozenset({"is_deleted", "checked", "in_history"})

    def update_task(self, task_id: str, **fields) -> dict:
        """Update task fields (content, labels, priority, due_string, etc).

        Does NOT support moving to another section — use move_task for that.
        SAFETY: blocks any attempt to delete or complete a task.
        """
        blocked = self._BLOCKED_FIELDS & set(fields)
        if blocked:
            raise ValueError(
                f"SAFETY: Refusing to send destructive fields: {blocked}. "
                "This application never deletes or completes tasks."
            )
        resp = self._session.post(
            f"{API_BASE}/tasks/{task_id}",
            json=fields,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_task(self, *_args, **_kwargs):
        """BLOCKED: This application never deletes tasks."""
        raise NotImplementedError(
            "SAFETY: Task deletion is permanently disabled."
        )

    def close_task(self, *_args, **_kwargs):
        """BLOCKED: This application never completes/closes tasks."""
        raise NotImplementedError(
            "SAFETY: Task completion via API is permanently disabled."
        )

    def move_task(self, task_id: str, section_id: str) -> dict:
        """Move a task to a different section."""
        resp = self._session.post(
            f"{API_BASE}/tasks/{task_id}/move",
            json={"section_id": section_id},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def create_section(self, project_id: str, name: str, order: int | None = None) -> dict:
        """Create a new section in a project."""
        body: dict[str, Any] = {"name": name, "project_id": project_id}
        if order is not None:
            body["order"] = order
        return self._post(f"{API_BASE}/sections", body)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_task(
        raw: dict,
        project_map: dict[str, str] | None = None,
        section_map: dict[str, str] | None = None,
    ) -> Task:
        """Parse a raw Todoist API task into our Task model."""
        project_map = project_map or {}
        section_map = section_map or {}

        due = raw.get("due")
        due_date = None
        due_datetime_val = None
        is_recurring = False
        due_has_time = False

        if due:
            is_recurring = due.get("is_recurring", False)
            date_str = due.get("date", "")

            # v1 API: "date" can be "2024-06-15" (date-only) or
            # "2024-06-15T09:00:00" / "2024-06-15T09:00:00Z" (has time)
            if "T" in date_str:
                due_has_time = True
                try:
                    cleaned = date_str.replace("Z", "+00:00")
                    if "+" not in cleaned and "-" not in cleaned[10:]:
                        cleaned += "+00:00"
                    due_datetime_val = datetime.fromisoformat(cleaned)
                    due_date = due_datetime_val.date()
                except ValueError:
                    pass
            elif date_str:
                try:
                    due_date = date.fromisoformat(date_str)
                except ValueError:
                    pass

        duration_minutes = None
        duration_raw = raw.get("duration")
        if duration_raw:
            amount = duration_raw.get("amount", 0)
            unit = duration_raw.get("unit", "minute")
            if unit == "minute":
                duration_minutes = amount
            elif unit == "hour":
                duration_minutes = amount * 60
            elif unit == "day":
                duration_minutes = amount * 480

        added_at = None
        added_str = raw.get("added_at") or raw.get("created_at")
        if added_str:
            try:
                added_at = datetime.fromisoformat(
                    added_str.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        project_id = raw.get("project_id", "")
        section_id = raw.get("section_id", "")

        # Sanitize user-controlled text fields to prevent prompt injection
        from brain.security.sanitization import (
            sanitize_text, sanitize_labels, MAX_TASK_CONTENT_LENGTH,
            MAX_TASK_DESCRIPTION_LENGTH,
        )
        content_result = sanitize_text(
            raw.get("content", ""),
            max_length=MAX_TASK_CONTENT_LENGTH,
            context="todoist_task",
        )
        description_result = sanitize_text(
            raw.get("description", ""),
            max_length=MAX_TASK_DESCRIPTION_LENGTH,
            context="todoist_task_description",
        )
        safe_labels = sanitize_labels(raw.get("labels", []))

        return Task(
            id=str(raw["id"]),
            content=content_result.text,
            description=description_result.text,
            project_id=str(project_id),
            project_name=project_map.get(str(project_id), ""),
            section_id=str(section_id) if section_id else "",
            section_name=section_map.get(str(section_id), "") if section_id else "",
            priority=raw.get("priority", 1),
            labels=safe_labels,
            due_date=due_date,
            due_datetime=due_datetime_val,
            due_has_time=due_has_time,
            duration_minutes=duration_minutes,
            url=(
                raw.get("url")
                or f"https://app.todoist.com/app/task/{raw['id']}"
            ),
            is_recurring=is_recurring,
            added_at=added_at,
            parent_id=raw.get("parent_id"),
        )
