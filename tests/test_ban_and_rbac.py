"""Tests for BanManager brute-force protection."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from brain.security.ban import BanManager, BanStage


class TestBanManager:
    """Tests for BanManager escalation and permanent bans."""

    def test_no_ban_initially(self) -> None:
        mgr = BanManager()
        assert mgr.is_banned("1.2.3.4") is False
        assert mgr.get_ban_remaining("1.2.3.4") == 0.0

    def test_record_success_clears_failures(self) -> None:
        mgr = BanManager()
        for _ in range(4):
            mgr.record_failure("1.2.3.4")
        mgr.record_success("1.2.3.4")
        assert mgr.is_banned("1.2.3.4") is False

    def test_stage1_ban_after_5_failures(self) -> None:
        mgr = BanManager()
        for _ in range(5):
            mgr.record_failure("1.2.3.4")
        assert mgr.is_banned("1.2.3.4") is True
        remaining = mgr.get_ban_remaining("1.2.3.4")
        assert remaining > 0 and remaining <= 300.0

    def test_permanent_ban(self) -> None:
        mgr = BanManager()
        mgr.ban_permanent("bad-actor", reason="automated attack")
        assert mgr.is_banned("bad-actor") is True
        assert mgr.get_ban_remaining("bad-actor") == float("inf")

    def test_unban(self) -> None:
        mgr = BanManager()
        mgr.ban_permanent("1.2.3.4")
        assert mgr.is_banned("1.2.3.4") is True
        mgr.unban("1.2.3.4")
        assert mgr.is_banned("1.2.3.4") is False

    def test_unban_nonexistent_returns_false(self) -> None:
        mgr = BanManager()
        assert mgr.unban("nobody") is False

    def test_list_bans(self) -> None:
        mgr = BanManager()
        mgr.ban_permanent("attacker-1", reason="test")
        for _ in range(5):
            mgr.record_failure("attacker-2")
        bans = mgr.list_bans()
        assert "attacker-1" in bans
        assert bans["attacker-1"]["type"] == "permanent"
        assert "attacker-2" in bans
        assert bans["attacker-2"]["type"] == "temporary"

    def test_permanent_stage3_config(self) -> None:
        mgr = BanManager(permanent_stage3=True)
        # Need 20 failures in 60 min for stage 3
        for _ in range(20):
            mgr.record_failure("target")
        assert mgr.is_banned("target") is True
        assert mgr.get_ban_remaining("target") == float("inf")


class TestBanManagerRBACIntegration:
    """Test RBAC + user role permission model."""

    def test_owner_has_admin_all(self) -> None:
        from brain.security.rbac import RBACPolicy, Permission
        policy = RBACPolicy()
        assert policy.has_user_permission("owner", Permission.ADMIN_ALL) is True

    def test_demo_limited_permissions(self) -> None:
        from brain.security.rbac import RBACPolicy, Permission
        policy = RBACPolicy()
        assert policy.has_user_permission("demo", Permission.READ_GOLD) is True
        assert policy.has_user_permission("demo", Permission.WRITE_BRONZE) is False
        assert policy.has_user_permission("demo", Permission.MANAGE_USERS) is False

    def test_check_access_two_layer(self) -> None:
        from brain.security.rbac import RBACPolicy, Permission
        policy = RBACPolicy()
        # Free tier + member user: can read gold (both allow)
        assert policy.check_access("free", "member", Permission.READ_GOLD) is True
        # Free tier + member: cannot use LLM (tier doesn't allow)
        assert policy.check_access("free", "member", Permission.USE_LLM) is False
        # Standard tier + viewer: can read gold but not write
        assert policy.check_access("standard", "viewer", Permission.READ_GOLD) is True
        assert policy.check_access("standard", "viewer", Permission.WRITE_BRONZE) is False

    def test_legacy_no_user_role(self) -> None:
        from brain.security.rbac import RBACPolicy, Permission
        policy = RBACPolicy()
        # Empty user_role falls back to tier-only check
        assert policy.check_access("standard", "", Permission.USE_LLM) is True
