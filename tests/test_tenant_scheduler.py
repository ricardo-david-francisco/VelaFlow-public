"""Tests for TenantScheduler — schedule-based job enqueuing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brain.queue.scheduler import TenantScheduler
from brain.queue.tasks import TaskQueue
from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager
from brain.tenant.models import TenantTier


@pytest.fixture()
def env(tmp_path):
    storage = LocalStorageBackend(str(tmp_path / "data"))
    encryptor = FieldEncryptor(FieldEncryptor.generate_master_key())
    mgr = TenantManager(storage, encryptor)
    queue = TaskQueue()
    scheduler = TenantScheduler(mgr, queue)
    return {
        "manager": mgr,
        "queue": queue,
        "scheduler": scheduler,
    }


class TestTenantScheduler:
    def test_no_tenants_no_jobs(self, env):
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)  # Monday 07:00
        count = env["scheduler"].tick(now)
        assert count == 0
        assert env["queue"].depth == 0

    def test_daily_digest_enqueued_at_configured_time(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("Sched", "s@t.com", TenantTier.STANDARD)
        mgr.update_config(tenant.tenant_id, daily_digest_time="07:00")
        # Monday 07:00 — should match "mon" in default daily_digest_days
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)
        count = env["scheduler"].tick(now)
        assert count == 1
        assert env["queue"].depth == 1

    def test_daily_digest_not_enqueued_wrong_time(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("Sched2", "s2@t.com", TenantTier.STANDARD)
        mgr.update_config(tenant.tenant_id, daily_digest_time="08:00")
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)  # 07:00 != 08:00
        count = env["scheduler"].tick(now)
        assert count == 0

    def test_daily_digest_not_enqueued_wrong_day(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("Sched3", "s3@t.com", TenantTier.STANDARD)
        mgr.update_config(
            tenant.tenant_id,
            daily_digest_time="07:00",
            daily_digest_days="tue,wed,thu",
        )
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)  # Monday
        count = env["scheduler"].tick(now)
        assert count == 0

    def test_dedup_prevents_double_enqueue(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("Dedup", "dd@t.com")
        mgr.update_config(tenant.tenant_id, daily_digest_time="07:00")
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)
        env["scheduler"].tick(now)
        # Second tick same minute — should NOT duplicate
        count = env["scheduler"].tick(now)
        assert count == 0
        assert env["queue"].depth == 1

    def test_overdue_alert_fires_at_interval(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("OD", "od@t.com")
        mgr.update_config(
            tenant.tenant_id,
            overdue_alert_enabled=True,
            overdue_alert_interval_hours=4,
            daily_digest_time="99:99",  # Disable daily to isolate test
        )
        # 08:00 — hour % 4 == 0
        now = datetime(2025, 4, 21, 8, 0, tzinfo=timezone.utc)
        count = env["scheduler"].tick(now)
        assert count == 1

    def test_weekend_planner_fires_friday_17(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("WP", "wp@t.com")
        mgr.update_config(
            tenant.tenant_id,
            weekend_planner_enabled=True,
            daily_digest_time="99:99",
        )
        # Friday 17:00 UTC — April 25, 2025 is Friday
        now = datetime(2025, 4, 25, 17, 0, tzinfo=timezone.utc)
        count = env["scheduler"].tick(now)
        assert count == 1

    def test_weekly_review_fires_sunday_20(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("WR", "wr@t.com")
        mgr.update_config(
            tenant.tenant_id,
            weekly_review_enabled=True,
            daily_digest_time="99:99",
        )
        # Sunday 20:00 UTC — April 27, 2025 is Sunday
        now = datetime(2025, 4, 27, 20, 0, tzinfo=timezone.utc)
        count = env["scheduler"].tick(now)
        assert count == 1

    def test_inactive_tenant_skipped(self, env):
        mgr = env["manager"]
        tenant = mgr.create_tenant("Inact", "i@t.com")
        mgr.update_config(tenant.tenant_id, daily_digest_time="07:00")
        mgr.deactivate_tenant(tenant.tenant_id)
        now = datetime(2025, 4, 21, 7, 0, tzinfo=timezone.utc)
        count = env["scheduler"].tick(now)
        assert count == 0
