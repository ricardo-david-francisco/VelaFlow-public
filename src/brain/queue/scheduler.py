"""Multi-tenant pipeline scheduler.

Scans all tenants and enqueues pipeline runs based on each tenant's
schedule configuration (stored in TenantConfig). Runs as a background
thread in the worker process.

Replaces per-user n8n workflows for scheduling.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timezone

from brain.queue.tasks import MessageType, QueueMessage, TaskQueue
from brain.tenant.manager import TenantManager

logger = logging.getLogger(__name__)


class TenantScheduler:
    """Scan tenants and enqueue pipeline runs per their schedule config."""

    def __init__(
        self,
        tenant_mgr: TenantManager,
        task_queue: TaskQueue,
    ) -> None:
        self._tenant_mgr = tenant_mgr
        self._queue = task_queue
        self._running = False
        self._last_check: dict[str, dict[str, str]] = {}

    def start(self) -> threading.Thread:
        """Start the scheduler loop in a daemon thread."""
        self._running = True
        thread = threading.Thread(target=self._run_loop, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        logger.info("Tenant scheduler started")
        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            time.sleep(60)

    def tick(self, now: datetime | None = None) -> int:
        """Public tick for testing. Returns number of jobs enqueued."""
        return self._tick(now=now)

    def _tick(self, now: datetime | None = None) -> int:
        """Check all tenants and enqueue due jobs."""
        if now is None:
            now = datetime.now(timezone.utc)
        tenants = self._tenant_mgr.list_tenants()
        count = 0
        for tenant in tenants:
            if not tenant.is_active:
                continue
            count += self._check_tenant(tenant, now)
        return count

    def _check_tenant(self, tenant, now: datetime) -> int:
        cfg = tenant.config
        tid = tenant.tenant_id
        current_time = now.strftime("%H:%M")
        current_day = now.strftime("%a").lower()
        today_key = now.strftime("%Y-%m-%d")
        count = 0

        if tid not in self._last_check:
            self._last_check[tid] = {}

        # Daily digest
        if cfg.daily_digest_time == current_time:
            days = [d.strip().lower() for d in cfg.daily_digest_days.split(",")]
            if current_day in days:
                check_key = f"daily_{today_key}"
                if self._last_check[tid].get("daily") != check_key:
                    self._last_check[tid]["daily"] = check_key
                    self._enqueue(tid, MessageType.PIPELINE_RUN, {"source": "scheduled_daily"})
                    count += 1

        # Overdue alerts
        if cfg.overdue_alert_enabled:
            hour = now.hour
            interval = cfg.overdue_alert_interval_hours
            if interval > 0 and hour % interval == 0 and now.minute == 0:
                check_key = f"overdue_{today_key}_{hour}"
                if self._last_check[tid].get("overdue") != check_key:
                    self._last_check[tid]["overdue"] = check_key
                    self._enqueue(tid, MessageType.PIPELINE_RUN, {"source": "scheduled_overdue"})
                    count += 1

        # Weekend planner (Friday at 17:00)
        if cfg.weekend_planner_enabled and current_day == "fri" and current_time == "17:00":
            check_key = f"weekend_{today_key}"
            if self._last_check[tid].get("weekend") != check_key:
                self._last_check[tid]["weekend"] = check_key
                self._enqueue(
                    tid, MessageType.PIPELINE_RUN,
                    {"source": "scheduled_weekend", "weekend_mode": True},
                )
                count += 1

        # Weekly review (Sunday at 20:00)
        if cfg.weekly_review_enabled and current_day == "sun" and current_time == "20:00":
            check_key = f"weekly_{today_key}"
            if self._last_check[tid].get("weekly") != check_key:
                self._last_check[tid]["weekly"] = check_key
                self._enqueue(
                    tid, MessageType.DIGEST_GENERATE,
                    {"source": "scheduled_weekly"},
                )
                count += 1

        return count

    def _enqueue(self, tenant_id: str, msg_type: MessageType, payload: dict) -> None:
        msg = QueueMessage(
            message_id=f"sched_{secrets.token_hex(8)}",
            message_type=msg_type,
            tenant_id=tenant_id,
            payload=payload,
        )
        self._queue.enqueue(msg)
        logger.info("Scheduled %s for tenant %s", msg_type.value, tenant_id)
