"""Resilience patterns — circuit breaker, retry with backoff, rate limiter.

Designed for N95 CPU + 8 GB RAM constraints: lightweight, no external
dependencies, minimal memory footprint.

Components:
- CircuitBreaker: prevents cascading failures on external API calls
- retry_with_backoff: decorator for transient failure recovery
- RateLimiter: in-memory sliding window rate limiter for webhooks
- GracefulDegrader: ensures the system always delivers something
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ── Circuit Breaker ────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Failing — reject calls immediately
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreaker:
    """Lightweight circuit breaker for external API calls.

    Prevents cascading failures when Todoist, Notion, or LLM APIs are
    down. Instead of hanging for 30s per call, the circuit opens after
    `failure_threshold` consecutive failures and rejects calls instantly
    for `reset_timeout` seconds.

    Usage:
        cb = CircuitBreaker("todoist", failure_threshold=3, reset_timeout=60)
        try:
            result = cb.call(todoist_client.get_tasks)
        except CircuitOpenError:
            # Use cached data or return empty
            result = []
    """

    name: str
    failure_threshold: int = 3
    reset_timeout: float = 60.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute function through the circuit breaker."""
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.reset_timeout:
                    self._state = CircuitState.HALF_OPEN
                    logger.info("Circuit %s: HALF_OPEN — testing recovery", self.name)
                else:
                    raise CircuitOpenError(
                        f"Circuit '{self.name}' is OPEN — rejecting call "
                        f"(resets in {self.reset_timeout - (time.monotonic() - self._last_failure_time):.0f}s)"
                    )

        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self._record_failure(exc)
            raise
        else:
            self._record_success()
            return result

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit %s: OPEN after %d failures (last: %s)",
                    self.name, self._failure_count, exc,
                )

    def _record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                logger.info("Circuit %s: CLOSED — recovered", self.name)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = 0.0


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open."""


# ── Retry with Backoff ─────────────────────────────────────────────────

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator: retry function with exponential backoff.

    Designed for transient API failures (429, 5xx, network timeouts).
    Backoff is capped to avoid long waits on N95 hardware.

    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0)
        def fetch_tasks():
            return requests.get(...)
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    logger.warning(
                        "%s attempt %d/%d failed: %s — retrying in %.1fs",
                        func.__name__, attempt + 1, max_retries + 1, exc, delay,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# ── Rate Limiter ───────────────────────────────────────────────────────

class RateLimiter:
    """In-memory sliding window rate limiter.

    Tracks requests per key (tenant_id) in a time window. Returns
    True if allowed, False if rate exceeded. Thread-safe.

    Memory: ~100 bytes per request timestamp. At 100 req/min per tenant,
    with 50 tenants = ~500 KB. Negligible for 8 GB.

    Usage:
        limiter = RateLimiter(max_requests=20, window_seconds=60)
        if not limiter.allow("tenant-123"):
            raise HTTPException(429, "Rate limit exceeded")
    """

    def __init__(self, max_requests: int = 20, window_seconds: float = 60.0) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self._last_prune: float = 0.0
        self._prune_interval: float = window_seconds * 5  # prune idle keys every 5 windows

    def _prune_idle_keys(self, now: float) -> None:
        """Remove keys with no recent activity (called under lock)."""
        if now - self._last_prune < self._prune_interval:
            return
        self._last_prune = now
        cutoff = now - self._window
        idle = [k for k, ts in self._requests.items() if not ts or ts[-1] <= cutoff]
        for k in idle:
            del self._requests[k]

    def allow(self, key: str) -> bool:
        """Check if a request is allowed for the given key."""
        now = time.monotonic()
        with self._lock:
            self._prune_idle_keys(now)
            # Prune expired timestamps
            cutoff = now - self._window
            timestamps = self._requests[key]
            self._requests[key] = [t for t in timestamps if t > cutoff]

            if len(self._requests[key]) >= self._max_requests:
                return False

            self._requests[key].append(now)
            return True

    def remaining(self, key: str) -> int:
        """Return remaining requests for a key in the current window."""
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            active = [t for t in self._requests[key] if t > cutoff]
            return max(0, self._max_requests - len(active))


# ── Graceful Degradation ──────────────────────────────────────────────

class GracefulDegrader:
    """Ensures the system always delivers something.

    Wraps operations with fallback values. If the primary function
    fails (even after retries), the fallback is returned instead of
    raising. The system never crashes — it degrades gracefully.

    Usage:
        degrader = GracefulDegrader()
        tasks = degrader.execute(
            primary=lambda: todoist_client.get_tasks(),
            fallback=[],
            operation="fetch_todoist_tasks",
        )
    """

    def __init__(self) -> None:
        self._degraded_operations: dict[str, str] = {}

    def execute(
        self,
        primary: Callable[[], Any],
        fallback: Any,
        operation: str = "unknown",
    ) -> Any:
        """Execute primary function, return fallback on any failure."""
        try:
            result = primary()
            # Clear degraded state on success
            self._degraded_operations.pop(operation, None)
            return result
        except Exception as exc:
            self._degraded_operations[operation] = str(exc)
            logger.error(
                "Degraded: %s failed (%s) — using fallback",
                operation, exc,
            )
            return fallback

    @property
    def degraded_operations(self) -> dict[str, str]:
        """Return currently degraded operations and their errors."""
        return dict(self._degraded_operations)

    @property
    def is_degraded(self) -> bool:
        return len(self._degraded_operations) > 0
