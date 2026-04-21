"""Tests for the JWT authentication module."""

from __future__ import annotations

import time

import pytest

from brain.api.auth import TokenClaims, create_access_token, verify_token
from tests._fakes import fake_password


_TEST_SECRET = fake_password(32)


class TestJWTAuth:
    def test_create_and_verify(self):
        token = create_access_token(
            tenant_id="tn_001",
            role="standard",
            email="user@test.com",
            secret=_TEST_SECRET,
        )
        claims = verify_token(token, secret=_TEST_SECRET)
        assert claims is not None
        assert claims.tenant_id == "tn_001"
        assert claims.role == "standard"
        assert claims.email == "user@test.com"

    def test_expired_token(self):
        token = create_access_token(
            tenant_id="tn_exp",
            role="free",
            email="exp@test.com",
            expiry_seconds=-1,  # Already expired
            secret=_TEST_SECRET,
        )
        claims = verify_token(token, secret=_TEST_SECRET)
        assert claims is None

    def test_wrong_secret(self):
        token = create_access_token(
            tenant_id="tn_ws",
            role="free",
            email="ws@test.com",
            secret=_TEST_SECRET,
        )
        claims = verify_token(token, secret=fake_password(32))
        assert claims is None

    def test_tampered_token(self):
        token = create_access_token(
            tenant_id="tn_tam",
            role="admin",
            email="tam@test.com",
            secret=_TEST_SECRET,
        )
        parts = token.split(".")
        # Tamper the payload
        tampered = parts[0] + "." + parts[1] + "x" + "." + parts[2]
        claims = verify_token(tampered, secret=_TEST_SECRET)
        assert claims is None

    def test_invalid_format(self):
        assert verify_token("not.a.valid.jwt", secret=_TEST_SECRET) is None
        assert verify_token("", secret=_TEST_SECRET) is None
        assert verify_token("single", secret=_TEST_SECRET) is None

    def test_claims_fields(self):
        token = create_access_token(
            tenant_id="tn_f",
            role="premium",
            email="f@f.com",
            secret=_TEST_SECRET,
        )
        claims = verify_token(token, secret=_TEST_SECRET)
        assert isinstance(claims, TokenClaims)
        assert claims.iat > 0
        assert claims.exp > claims.iat
