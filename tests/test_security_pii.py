"""Tests for the PII detection and masking engine."""

from __future__ import annotations

import re

import pytest

from brain.security.pii import PIIDetector, PIIMatch


@pytest.fixture()
def detector():
    return PIIDetector()


class TestPIIDetector:
    def test_detect_credit_card(self, detector):
        matches = detector.detect("Pay with 4111-1111-1111-1111 please")
        assert any(m.pattern_name == "credit_card" for m in matches)

    def test_detect_email(self, detector):
        matches = detector.detect("Contact john@example.com for info")
        assert any(m.pattern_name == "email" for m in matches)

    def test_detect_phone(self, detector):
        matches = detector.detect("Call +351 912 345 678 now")
        assert any(m.pattern_name == "phone_intl" for m in matches)

    def test_detect_ssn(self, detector):
        matches = detector.detect("SSN is 123-45-6789")
        assert any(m.pattern_name == "ssn_us" for m in matches)

    def test_detect_iban(self, detector):
        matches = detector.detect("Transfer to PT50 0002 0123 1234 5678 9015 4")
        assert any(m.pattern_name == "iban" for m in matches)

    def test_detect_nif_pt(self, detector):
        matches = detector.detect("NIF: 123456789")
        assert any(m.pattern_name == "nif_pt" for m in matches)

    def test_no_pii(self, detector):
        assert not detector.has_pii("Buy groceries and walk the dog")

    def test_has_pii_true(self, detector):
        assert detector.has_pii("Email me at test@example.com")

    def test_mask_email(self, detector):
        masked = detector.mask("Contact test@example.com for details")
        assert "test@example.com" not in masked
        assert "[EMAIL]" in masked

    def test_mask_credit_card(self, detector):
        masked = detector.mask("Card: 4111-1111-1111-1111")
        assert "4111-1111-1111-1111" not in masked

    def test_mask_multiple(self, detector):
        text = "Email test@x.com or call +351 912 345 678"
        masked = detector.mask(text)
        assert "test@x.com" not in masked
        assert "[EMAIL]" in masked

    def test_empty_text(self, detector):
        assert not detector.has_pii("")
        assert detector.detect("") == []
        assert detector.mask("") == ""

    def test_custom_pattern(self):
        custom = [(
            "custom_id",
            re.compile(r"\bCUST-\d{6}\b"),
        )]
        det = PIIDetector(extra_patterns=custom)
        assert det.has_pii("User CUST-123456 flagged")
        matches = det.detect("User CUST-123456 flagged")
        assert any(m.pattern_name == "custom_id" for m in matches)

    def test_pii_match_dataclass(self, detector):
        matches = detector.detect("test@example.com")
        assert len(matches) >= 1
        m = matches[0]
        assert isinstance(m, PIIMatch)
        assert m.start >= 0
        assert m.end > m.start
        assert m.matched_text == "test@example.com"
