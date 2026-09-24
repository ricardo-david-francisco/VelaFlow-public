"""Content sanitization — Defense against prompt injection, XSS, and payload attacks.

Applied at ALL data ingestion points (Todoist, Notion, webhooks) before data
enters the medallion pipeline or is sent to LLMs.

Defense layers:
1. Control character stripping (C0/C1 block)
2. Length enforcement (configurable per field)
3. Prompt injection detection and neutralization
4. HTML/script tag removal
5. Label validation (alphanumeric + safe punctuation only)

This module is intentionally aggressive. False positives on legitimate
content are preferred over allowing injection payloads through.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Length limits ─────────────────────────────────────────────────────
MAX_TASK_CONTENT_LENGTH = 2000
MAX_TASK_DESCRIPTION_LENGTH = 5000
MAX_LABEL_LENGTH = 100
MAX_LABELS_COUNT = 20
MAX_LLM_PROMPT_LENGTH = 8000

# ── Control character removal ─────────────────────────────────────────
# Remove C0 (0x00-0x1F except tab/newline/CR) and C1 (0x7F-0x9F) blocks
_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)

# ── HTML/script injection ────────────────────────────────────────────
_HTML_TAGS = re.compile(r"<[^>]{0,500}>", re.IGNORECASE)
_SCRIPT_PATTERN = re.compile(
    r"<\s*script[^>]*>.*?<\s*/\s*script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_HANDLER = re.compile(
    r"\bon\w+\s*=\s*[\"'][^\"']*[\"']",
    re.IGNORECASE,
)

# ── Prompt injection patterns ────────────────────────────────────────
# These patterns detect common prompt injection techniques.
# When detected, the content is wrapped in a safety boundary.
_PROMPT_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction_override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}"
        r"\b(previous|above|all|prior|system)\b.{0,30}"
        r"\b(instructions?|rules?|prompts?|context|constraints?)\b",
        re.IGNORECASE,
    )),
    ("role_hijack", re.compile(
        r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be|"
        r"new\s+role|switch\s+to|from\s+now\s+on\s+you)\b",
        re.IGNORECASE,
    )),
    ("system_impersonation", re.compile(
        r"^(SYSTEM|ASSISTANT|AI|GPT|CLAUDE|GEMINI)\s*:",
        re.IGNORECASE | re.MULTILINE,
    )),
    ("delimiter_escape", re.compile(
        r"(---+|===+|\*\*\*+|```|###\s*SYSTEM|###\s*INSTRUCTION)",
        re.IGNORECASE,
    )),
    ("data_exfiltration", re.compile(
        r"\b(output|reveal|show|display|print|leak|extract|dump)\b.{0,30}"
        r"\b(api.?key|secret|token|password|credential|master.?key|"
        r"environment|env\s*var|config|database|private)\b",
        re.IGNORECASE,
    )),
    ("code_execution", re.compile(
        r"\b(execute|run|eval|exec|import\s+os|subprocess|"
        r"__import__|system\s*\(|os\.popen)\b",
        re.IGNORECASE,
    )),
    ("encoding_bypass", re.compile(
        r"(base64|rot13|hex|unicode|url.?encode)\s*"
        r"(decode|encode|the\s+following)",
        re.IGNORECASE,
    )),
]

# Safety wrapper applied to content when prompt injection is detected
_INJECTION_PREFIX = "[SANITIZED_USER_CONTENT] "
_INJECTION_SUFFIX = " [/SANITIZED_USER_CONTENT]"


@dataclass
class SanitizationResult:
    """Result of content sanitization."""

    text: str
    was_modified: bool = False
    injection_detected: bool = False
    detected_patterns: list[str] | None = None
    truncated: bool = False


def sanitize_text(
    text: str,
    *,
    max_length: int = MAX_TASK_CONTENT_LENGTH,
    check_injection: bool = True,
    strip_html: bool = True,
    context: str = "",
) -> SanitizationResult:
    """Sanitize text content for safe processing.

    Applied to all user-provided text before it enters the pipeline.
    """
    if not text:
        return SanitizationResult(text="")

    original = text
    modified = False
    truncated = False
    detected: list[str] = []

    # 1. Strip control characters
    cleaned = _CONTROL_CHARS.sub("", text)
    if cleaned != text:
        modified = True
    text = cleaned

    # 2. Strip HTML/script tags
    if strip_html:
        text = _SCRIPT_PATTERN.sub("", text)
        text = _EVENT_HANDLER.sub("", text)
        text = _HTML_TAGS.sub("", text)
        if text != cleaned:
            modified = True

    # 3. Enforce length limit
    if len(text) > max_length:
        text = text[:max_length]
        truncated = True
        modified = True

    # 4. Detect prompt injection
    injection_found = False
    if check_injection:
        for name, pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                detected.append(name)
                injection_found = True

    if injection_found:
        logger.warning(
            "PROMPT_INJECTION_DETECTED context=%s patterns=%s length=%d",
            context, detected, len(original),
        )
        # Wrap in safety boundary so LLM treats it as opaque user data
        text = _INJECTION_PREFIX + text + _INJECTION_SUFFIX
        modified = True

    return SanitizationResult(
        text=text,
        was_modified=modified,
        injection_detected=injection_found,
        detected_patterns=detected if detected else None,
        truncated=truncated,
    )


def sanitize_label(label: str) -> str:
    """Sanitize a single label/tag.

    Labels must be alphanumeric with limited punctuation.
    """
    if not label:
        return ""
    # Strip control chars and HTML
    label = _CONTROL_CHARS.sub("", label)
    label = _HTML_TAGS.sub("", label)
    # Allow only safe characters: letters, digits, spaces, hyphens, underscores, dots
    label = re.sub(r"[^\w\s\-.]", "", label, flags=re.UNICODE)
    return label[:MAX_LABEL_LENGTH].strip()


def sanitize_labels(labels: list[str]) -> list[str]:
    """Sanitize a list of labels."""
    result = []
    for label in labels[:MAX_LABELS_COUNT]:
        cleaned = sanitize_label(label)
        if cleaned:
            result.append(cleaned)
    return result


def sanitize_for_llm(
    text: str,
    *,
    max_length: int = MAX_LLM_PROMPT_LENGTH,
    context: str = "llm_input",
) -> str:
    """Sanitize text specifically for LLM consumption.

    Applies all sanitization layers and always wraps content
    in clear user-data boundaries for the LLM system prompt.
    """
    result = sanitize_text(
        text,
        max_length=max_length,
        check_injection=True,
        strip_html=True,
        context=context,
    )
    # Always wrap user-originated content in clear boundaries
    if not result.injection_detected:
        return f"[USER_DATA_BEGIN]\n{result.text}\n[USER_DATA_END]"
    return result.text  # Already wrapped by injection handler


def has_prompt_injection(text: str) -> bool:
    """Quick check for prompt injection patterns (no sanitization)."""
    if not text:
        return False
    scan = text[:_MAX_SCAN_LENGTH]
    return any(p.search(scan) for _, p in _PROMPT_INJECTION_PATTERNS)


_MAX_SCAN_LENGTH = 50_000
