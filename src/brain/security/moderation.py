"""Content moderation — Prevent illegal or abusive use of VelaFlow.

Checks user-submitted content (task names, digests, LLM prompts) for
indicators of illegal activity, hate speech, or platform abuse.

This module provides lightweight, local pattern-based detection
(no external API calls required). For production SaaS at scale,
integrate with a cloud moderation API (Azure Content Safety, etc.).

All content moderation decisions are audit-logged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Categories of prohibited content
_ILLEGAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("drugs_trafficking", re.compile(
        r"\b(sell|buy|ship|deliver)\b.{0,30}\b(cocaine|heroin|fentanyl|meth|mdma|lsd|ketamine)\b",
        re.IGNORECASE,
    )),
    ("weapons_trafficking", re.compile(
        r"\b(sell|buy|ship|smuggle|manufacture)\b.{0,30}\b(firearm|gun|explosive|bomb|grenade|silencer)\b",
        re.IGNORECASE,
    )),
    ("csam", re.compile(
        r"\b(child|minor|underage)\b.{0,20}\b(porn|exploit|abuse|nude)\b",
        re.IGNORECASE,
    )),
    ("human_trafficking", re.compile(
        r"\b(traffic|smuggle|kidnap|enslave)\b.{0,30}\b(person|people|human|women|children)\b",
        re.IGNORECASE,
    )),
    ("fraud", re.compile(
        r"\b(phish|scam|steal identity|credit card fraud|money launder)\b",
        re.IGNORECASE,
    )),
    ("hacking_tools", re.compile(
        r"\b(exploit|ransomware|malware|rootkit|keylogger|ddos|brute.?force)\b.{0,20}\b(attack|deploy|create|build|write)\b",
        re.IGNORECASE,
    )),
    ("terrorism", re.compile(
        r"\b(terrorist|jihad|radicaliz)\b.{0,30}\b(attack|recruit|plan|bomb)\b",
        re.IGNORECASE,
    )),
]

# Maximum content length to scan (prevent ReDoS on huge payloads)
_MAX_SCAN_LENGTH = 50_000


@dataclass
class ModerationResult:
    """Result of content moderation check."""

    is_allowed: bool
    category: str = ""
    reason: str = ""


def check_content(text: str, context: str = "") -> ModerationResult:
    """Check text content for illegal or abusive patterns.

    Args:
        text: Content to scan.
        context: Caller context for audit logging (e.g., 'pipeline_input').

    Returns:
        ModerationResult indicating whether the content is allowed.
    """
    if not text:
        return ModerationResult(is_allowed=True)

    # Truncate to prevent ReDoS on enormous payloads
    scan_text = text[:_MAX_SCAN_LENGTH]

    for category, pattern in _ILLEGAL_PATTERNS:
        if pattern.search(scan_text):
            logger.warning(
                "CONTENT_BLOCKED category=%s context=%s length=%d",
                category, context, len(text),
            )
            return ModerationResult(
                is_allowed=False,
                category=category,
                reason=f"Content violates acceptable use policy ({category}).",
            )

    return ModerationResult(is_allowed=True)


def check_bulk_content(items: list[dict], text_fields: list[str], context: str = "") -> ModerationResult:
    """Check a batch of dict items for prohibited content.

    Scans specified text fields across all items.
    Returns on the first violation found.
    """
    for item in items:
        for field in text_fields:
            value = item.get(field)
            if isinstance(value, str):
                result = check_content(value, context=context)
                if not result.is_allowed:
                    return result
    return ModerationResult(is_allowed=True)
