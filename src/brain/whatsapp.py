"""WhatsApp message delivery via CallMeBot API."""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

import requests

from brain.config import Settings

logger = logging.getLogger(__name__)

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


def send_whatsapp(
    phone: str,
    api_key: str,
    message: str,
) -> bool:
    """Send a WhatsApp message via CallMeBot.

    Returns True on success, False on failure.
    """
    if not phone or not api_key:
        return False

    # CallMeBot has a 4000 character limit
    if len(message) > 3900:
        message = message[:3900] + "\n\n... (truncated)"

    params = {
        "phone": phone,
        "apikey": api_key,
        "text": message,
    }

    try:
        resp = requests.get(CALLMEBOT_URL, params=params, timeout=30)
        if resp.status_code == 200:
            logger.info("WhatsApp sent to %s", phone[-4:].rjust(len(phone), "*"))
            return True
        logger.warning(
            "CallMeBot returned %d for %s", resp.status_code, phone[-4:]
        )
        return False
    except requests.RequestException:
        logger.exception("Failed to send WhatsApp to %s", phone[-4:])
        return False


def send_to_user(settings: Settings, message: str) -> bool:
    """Send WhatsApp to the primary user."""
    return send_whatsapp(
        settings.callmebot_phone, settings.callmebot_api_key, message
    )


def send_to_secondary(settings: Settings, message: str) -> bool:
    """Send WhatsApp to the secondary recipient."""
    return send_whatsapp(
        settings.callmebot_secondary_phone, settings.callmebot_secondary_api_key, message
    )
