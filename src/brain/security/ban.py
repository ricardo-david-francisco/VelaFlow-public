"""Brute-force protection — Temporary and permanent IP/user bans.

Tracks failed authentication attempts and temporarily bans offenders.
Bans are in-memory by default (cleared on restart) but can be
persisted to Redis for multi-worker deployments.

Ban Escalation:
- 5 failures in 5 min → 5 min ban
- 10 failures in 15 min → 30 min ban
- 20 failures in 60 min → 24 hour ban
- Permanent ban: manual via admin API (see docs/security.md)

Configuration:
- VELAFLOW_BAN_PERMANENT=true → makes escalation stage 3 permanent
- Geofencing: see docs/deployment.md for Caddy/nginx geo-blocking

Thread-safe: all operations protected by threading.Lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class BanStage(IntEnum):
    """Escalation stages for brute-force bans."""

    NONE = 0
    SOFT = 1      # 5 min ban
    MEDIUM = 2    # 30 min ban
    HARD = 3      # 24h ban (or permanent if configured)


# Escalation thresholds: (failures_in_window, window_seconds, ban_seconds)
_ESCALATION_RULES: list[tuple[int, float, float]] = [
    (5, 300.0, 300.0),       # 5 failures in 5 min → 5 min ban
    (10, 900.0, 1800.0),     # 10 failures in 15 min → 30 min ban
    (20, 3600.0, 86400.0),   # 20 failures in 60 min → 24h ban
]


@dataclass
class _BanRecord:
    """Tracks failures and ban state for a single key (IP or user)."""

    failures: list[float] = field(default_factory=list)
    banned_until: float = 0.0
    stage: BanStage = BanStage.NONE
    is_permanent: bool = False


class BanManager:
    """In-memory brute-force ban manager with escalating penalties.

    Usage:
        ban_mgr = BanManager()

        # Before auth:
        if ban_mgr.is_banned(client_ip):
            raise HTTPException(403, "Temporarily banned")

        # After failed auth:
        ban_mgr.record_failure(client_ip)

        # After successful auth:
        ban_mgr.record_success(client_ip)

        # Admin operations:
        ban_mgr.ban_permanent(client_ip, reason="Automated attack detected")
        ban_mgr.unban(client_ip)
    """

    def __init__(self, permanent_stage3: bool = False, persist_path: str | None = None) -> None:
        self._records: dict[str, _BanRecord] = defaultdict(_BanRecord)
        self._lock = threading.Lock()
        self._permanent_stage3 = permanent_stage3
        # Admin-set permanent bans with reasons
        self._permanent_bans: dict[str, str] = {}
        # Disk persistence for permanent bans (survives restarts)
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        if self._persist_path:
            self._load_persistent_bans()

    def is_banned(self, key: str) -> bool:
        """Check if a key (IP or user ID) is currently banned."""
        with self._lock:
            if key in self._permanent_bans:
                return True
            record = self._records.get(key)
            if record is None:
                return False
            if record.is_permanent:
                return True
            if record.banned_until > 0 and time.monotonic() < record.banned_until:
                return True
            return False

    def get_ban_remaining(self, key: str) -> float:
        """Return seconds remaining on ban, or 0 if not banned."""
        with self._lock:
            if key in self._permanent_bans:
                return float("inf")
            record = self._records.get(key)
            if record is None:
                return 0.0
            if record.is_permanent:
                return float("inf")
            remaining = record.banned_until - time.monotonic()
            return max(0.0, remaining)

    def record_failure(self, key: str) -> None:
        """Record a failed authentication attempt."""
        now = time.monotonic()
        with self._lock:
            record = self._records[key]
            record.failures.append(now)
            self._evaluate_escalation(key, record, now)

    def record_success(self, key: str) -> None:
        """Record a successful authentication (clears failure history)."""
        with self._lock:
            if key in self._records and not self._records[key].is_permanent:
                del self._records[key]

    def ban_permanent(self, key: str, reason: str = "") -> None:
        """Permanently ban a key (admin action)."""
        with self._lock:
            self._permanent_bans[key] = reason or "Manual admin ban"
            logger.warning("Permanent ban set for %s: %s", key, reason)
            self._save_persistent_bans()

    def unban(self, key: str) -> bool:
        """Remove any ban (temporary or permanent) for a key."""
        with self._lock:
            removed = False
            if key in self._permanent_bans:
                del self._permanent_bans[key]
                removed = True
                self._save_persistent_bans()
            if key in self._records:
                del self._records[key]
                removed = True
            if removed:
                logger.info("Ban removed for %s", key)
            return removed

    def list_bans(self) -> dict[str, dict]:
        """List all currently active bans (for admin dashboard)."""
        now = time.monotonic()
        result: dict[str, dict] = {}
        with self._lock:
            for key, reason in self._permanent_bans.items():
                result[key] = {
                    "type": "permanent",
                    "reason": reason,
                    "remaining": float("inf"),
                }
            for key, record in self._records.items():
                if key in result:
                    continue  # already listed as permanent
                if record.is_permanent or (
                    record.banned_until > 0 and now < record.banned_until
                ):
                    remaining = (
                        float("inf")
                        if record.is_permanent
                        else record.banned_until - now
                    )
                    result[key] = {
                        "type": "temporary",
                        "stage": record.stage.name,
                        "remaining": remaining,
                        "failure_count": len(record.failures),
                    }
        return result

    def _evaluate_escalation(
        self, key: str, record: _BanRecord, now: float
    ) -> None:
        """Check if failures trigger escalation to a higher ban stage."""
        for stage_idx, (threshold, window, ban_duration) in enumerate(
            _ESCALATION_RULES
        ):
            stage = BanStage(stage_idx + 1)
            if record.stage >= stage:
                continue
            # Count failures within window
            cutoff = now - window
            recent = [t for t in record.failures if t > cutoff]
            if len(recent) >= threshold:
                record.stage = stage
                if stage == BanStage.HARD and self._permanent_stage3:
                    record.is_permanent = True
                    record.banned_until = 0.0
                    self._permanent_bans[key] = "Stage 3 auto-escalation"
                    self._save_persistent_bans()
                    logger.warning(
                        "PERMANENT ban for %s (stage 3 escalation)", key
                    )
                else:
                    record.banned_until = now + ban_duration
                    logger.warning(
                        "Ban %s: stage=%s, duration=%.0fs, failures=%d",
                        key,
                        stage.name,
                        ban_duration,
                        len(recent),
                    )

    # ── Persistence ────────────────────────────────────────────────

    def _load_persistent_bans(self) -> None:
        """Load permanent bans from disk on startup."""
        if not self._persist_path or not self._persist_path.is_file():
            return
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._permanent_bans.update(data)
                logger.info("Loaded %d persistent bans", len(data))
        except Exception:
            logger.warning("Failed to load persistent bans from %s", self._persist_path, exc_info=True)

    def _save_persistent_bans(self) -> None:
        """Save permanent bans to disk (called under lock)."""
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._permanent_bans, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
        except Exception:
            logger.warning("Failed to save persistent bans", exc_info=True)
