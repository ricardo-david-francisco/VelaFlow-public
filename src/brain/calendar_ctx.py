"""Google Calendar integration (optional dependency)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from brain.config import Settings
from brain.models import CalendarEvent

logger = logging.getLogger(__name__)


def get_events(
    settings: Settings,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CalendarEvent]:
    """Fetch calendar events for a time range.

    Requires google-api-python-client and google-auth-oauthlib.
    Returns empty list if dependencies are missing or auth fails.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        logger.info("Google Calendar dependencies not installed. Skipping.")
        return []

    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    creds = None
    token_path = settings.google_oauth_token_file
    secrets_path = settings.google_oauth_client_secrets_file

    # Load existing token
    try:
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    except (FileNotFoundError, ValueError):
        pass

    # Refresh or create new token
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            logger.warning("Failed to refresh Google Calendar token.")
            creds = None

    if not creds or not creds.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(secrets_path, scopes)
            creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception:
            logger.warning("Google Calendar OAuth flow failed. Skipping.")
            return []

    # Query events
    now = start or datetime.now(timezone.utc)
    end_time = end or (now + timedelta(days=1))

    try:
        service = build("calendar", "v3", credentials=creds)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end_time.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )
    except Exception:
        logger.warning("Failed to query Google Calendar API.")
        return []

    events: list[CalendarEvent] = []
    for item in result.get("items", []):
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})
        all_day = "date" in start_raw and "dateTime" not in start_raw

        event_start = _parse_datetime(start_raw.get("dateTime") or start_raw.get("date"))
        event_end = _parse_datetime(end_raw.get("dateTime") or end_raw.get("date"))

        events.append(
            CalendarEvent(
                summary=item.get("summary", "(No title)"),
                start=event_start,
                end=event_end,
                all_day=all_day,
            )
        )

    return events


def _parse_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
