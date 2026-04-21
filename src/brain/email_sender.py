"""SMTP email delivery for digests."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from brain.config import Settings
from brain.models import DigestResult

logger = logging.getLogger(__name__)


def send_digest(
    settings: Settings,
    digest: DigestResult,
    recipient: str | None = None,
) -> bool:
    """Send a digest email via SMTP (Gmail).

    Returns True on success, False on failure.
    """
    if not settings.smtp_username or not settings.smtp_password:
        logger.warning("SMTP not configured. Cannot send email.")
        return False

    to_addr = recipient or settings.digest_to_email
    if not to_addr:
        logger.warning("No recipient email configured.")
        return False

    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = settings.digest_from_email or settings.smtp_username
    msg["To"] = to_addr
    msg.set_content(digest.body_text)

    if digest.body_html:
        msg.add_alternative(digest.body_html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            resp_code, _ = server.starttls()
            if resp_code != 220:
                logger.error("STARTTLS failed (code %d). Aborting to protect credentials.", resp_code)
                return False
            server.ehlo()
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        logger.info("Email sent: %s → %s", digest.subject, to_addr)
        return True
    except Exception:
        logger.exception("Failed to send email: %s", digest.subject)
        return False
