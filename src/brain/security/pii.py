"""PII Detection — Identify and mask personally identifiable information.

Scans text for common PII patterns (credit cards, emails, phone numbers,
SSN, IBAN) and provides masking capabilities before data enters the
silver layer or is sent to LLM APIs.

Applied in both the Python pipeline (brain.pipeline.silver) and the
DuckDB engine (brain.engine.processor) silver transformation stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PIIMatch:
    """A detected PII pattern in text."""

    pattern_name: str
    matched_text: str
    start: int
    end: int


# Compiled patterns for performance
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "credit_card",
        re.compile(
            r"\b(?:\d{4}[-\s]?){3}\d{4}\b"
        ),
    ),
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
        ),
    ),
    (
        "phone_intl",
        re.compile(
            r"(?<!\d[.\-/])"           # not preceded by digit-separator (dates, versions)
            r"\+\d{1,3}[-.\s]?"        # must start with + country code
            r"\(?\d{2,4}\)?[-.\s]?"    # area code
            r"\d{3,4}[-.\s]?"          # subscriber part 1
            r"\d{3,4}"                 # subscriber part 2
            r"(?![.\-/]\d)"            # not followed by separator-digit (dates, versions)
        ),
    ),
    (
        "phone_us",
        re.compile(
            r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"  # US format: 555-123-4567
        ),
    ),
    (
        "ssn_us",
        re.compile(
            r"\b\d{3}-\d{2}-\d{4}\b"
        ),
    ),
    (
        "iban",
        re.compile(
            r"\b[A-Z]{2}\d{2}[\s]?[\dA-Z]{4}[\s]?(?:[\dA-Z]{4}[\s]?){1,7}[\dA-Z]{1,4}\b"
        ),
    ),
    (
        "nif_pt",
        re.compile(
            r"\b[123569]\d{8}\b"
        ),
    ),
]


class PIIDetector:
    """Detect and mask PII in text fields.

    Usage:
        detector = PIIDetector()
        if detector.has_pii("Call me at 555-123-4567"):
            masked = detector.mask("Call me at 555-123-4567")
            # "Call me at [PHONE_INTL]"
    """

    def __init__(self, extra_patterns: list[tuple[str, re.Pattern]] | None = None) -> None:
        self._patterns = list(_PATTERNS)
        if extra_patterns:
            self._patterns.extend(extra_patterns)

    def detect(self, text: str) -> list[PIIMatch]:
        """Return all PII matches found in text."""
        matches: list[PIIMatch] = []
        for name, pattern in self._patterns:
            for m in pattern.finditer(text):
                matches.append(PIIMatch(
                    pattern_name=name,
                    matched_text=m.group(),
                    start=m.start(),
                    end=m.end(),
                ))
        return matches

    def has_pii(self, text: str) -> bool:
        """Check if text contains any PII patterns."""
        for _, pattern in self._patterns:
            if pattern.search(text):
                return True
        return False

    def mask(self, text: str) -> str:
        """Replace all PII occurrences with [PATTERN_NAME] tokens."""
        result = text
        # Process from end to start to preserve positions
        all_matches = sorted(self.detect(text), key=lambda m: m.start, reverse=True)
        for match in all_matches:
            placeholder = f"[{match.pattern_name.upper()}]"
            result = result[:match.start] + placeholder + result[match.end:]
        return result
