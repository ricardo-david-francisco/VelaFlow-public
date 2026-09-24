"""Gmail IMAP polling for unread important emails."""

from __future__ import annotations

import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from typing import Any

from brain.config import Settings
from brain.models import EmailAlert

logger = logging.getLogger(__name__)

MAX_ALERTS = 25


def get_unread_alerts(
    settings: Settings,
    hours: int = 24,
) -> list[EmailAlert]:
    """Fetch unread important emails from Gmail via IMAP.

    Returns empty list if IMAP credentials are not configured.
    """
    if not settings.gmail_imap_username or not settings.gmail_imap_password:
        logger.info("Gmail IMAP not configured. Skipping email alerts.")
        return []

    try:
        conn = imaplib.IMAP4_SSL(
            settings.gmail_imap_host, settings.gmail_imap_port
        )
        conn.login(settings.gmail_imap_username, settings.gmail_imap_password)
        conn.select("INBOX", readonly=True)
    except Exception:
        logger.warning("Failed to connect to Gmail IMAP.")
        return []

    try:
        # Try Gmail-specific search first, fall back to standard IMAP
        query = settings.gmail_important_query
        try:
            status, msg_ids = conn.search("UTF-8", query)
        except imaplib.IMAP4.error:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
                "%d-%b-%Y"
            )
            status, msg_ids = conn.search(None, f"UNSEEN SINCE {cutoff}")

        if status != "OK" or not msg_ids[0]:
            return []

        ids = msg_ids[0].split()[-MAX_ALERTS:]
        alerts: list[EmailAlert] = []

        for msg_id in ids:
            try:
                _, data = conn.fetch(msg_id, "(BODY.PEEK[HEADER])")
                if not data or not data[0]:
                    continue
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_header(msg.get("Subject", ""))
                sender = _decode_header(msg.get("From", ""))
                date_str = msg.get("Date", "")
                sent_at = _parse_email_date(date_str)

                alerts.append(
                    EmailAlert(subject=subject, sender=sender, sent_at=sent_at)
                )
            except Exception as exc:  # noqa: BLE001 — skip unparseable individual messages
                logger.debug("skipped unparseable IMAP message: %s", exc)
                continue

        return alerts
    finally:
        try:
            conn.close()
        except Exception as exc:
            logger.debug("IMAP close suppressed: %s", exc)
        try:
            conn.logout()
        except Exception as exc:
            logger.debug("IMAP logout suppressed: %s", exc)


def _decode_header(value: str) -> str:
    """Decode MIME-encoded header value."""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _parse_email_date(date_str: str) -> datetime | None:
    """Parse email Date header into datetime."""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(date_str)
    except Exception:
        return None
