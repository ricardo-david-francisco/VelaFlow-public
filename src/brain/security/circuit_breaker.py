"""Resilience — Circuit breakers, health checks, and fallback orchestration.

Ensures VelaFlow continues operating even when external services fail:
- Circuit breaker pattern for Todoist/Notion/Google AI/Groq APIs
- Health check aggregation for /health/ready endpoint
- Automatic retry with exponential backoff
- Graceful degradation: serve cached data when APIs are down

Thread-safe: all operations protected by threading.Lock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failures exceeded threshold, rejecting calls
    HALF_OPEN = "half_open" # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for a circuit breaker instance."""
    failure_threshold: int = 5        # Failures before opening
    recovery_timeout: float = 60.0    # Seconds before trying half-open
    half_open_max_calls: int = 1      # Calls allowed in half-open state
    success_threshold: int = 2        # Successes to close from half-open


class CircuitBreaker:
    """Circuit breaker pattern for external service calls.

    Usage:
        cb = CircuitBreaker("todoist")

        if cb.can_execute():
            try:
                result = call_todoist_api()
                cb.record_success()
            except Exception:
                cb.record_failure()
                result = get_cached_data()
        else:
            result = get_cached_data()  # Graceful degradation
    """

    def __init__(
        self, name: str, config: CircuitBreakerConfig | None = None
    ) -> None:
        self.name = name
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._half_open_calls = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_recovery()
            return self._state

    def can_execute(self) -> bool:
        """Check if a call should be attempted."""
        with self._lock:
            self._check_recovery()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self._config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            return False  # OPEN

    def record_success(self) -> None:
        """Record a successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    logger.info("Circuit breaker %s: CLOSED (recovered)", self.name)
            else:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                self._success_count = 0
                logger.warning(
                    "Circuit breaker %s: OPEN (half-open test failed)", self.name
                )
            elif self._failure_count >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker %s: OPEN (failures=%d)",
                    self.name, self._failure_count,
                )

    def _check_recovery(self) -> None:
        """Check if enough time has passed to try recovery."""
        if self._state != CircuitState.OPEN:
            return
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self._config.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            self._success_count = 0
            logger.info("Circuit breaker %s: HALF_OPEN (testing recovery)", self.name)

    def get_status(self) -> dict[str, Any]:
        """Get current circuit breaker status for health reporting."""
        with self._lock:
            self._check_recovery()
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "last_failure_age": (
                    time.monotonic() - self._last_failure_time
                    if self._last_failure_time > 0
                    else None
                ),
            }


# ── Global circuit breakers for external services ─────────────────────
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_circuit_breaker(name: str) -> CircuitBreaker:
    """Get or create a named circuit breaker (thread-safe singleton)."""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name)
        return _breakers[name]


def get_all_circuit_breaker_status() -> list[dict[str, Any]]:
    """Get status of all circuit breakers for health endpoint."""
    with _breakers_lock:
        return [cb.get_status() for cb in _breakers.values()]


# ── Service Health Registry ───────────────────────────────────────────

@dataclass
class ServiceHealth:
    """Health status for an external service."""
    name: str
    healthy: bool = True
    last_check: float = 0.0
    error: str = ""
    latency_ms: float = 0.0


class HealthRegistry:
    """Aggregates health status across all services.

    Used by /health/ready to report overall system readiness.
    """

    def __init__(self) -> None:
        self._services: dict[str, ServiceHealth] = {}
        self._lock = threading.RLock()

    def update(
        self, name: str, healthy: bool, error: str = "", latency_ms: float = 0.0
    ) -> None:
        with self._lock:
            self._services[name] = ServiceHealth(
                name=name,
                healthy=healthy,
                last_check=time.monotonic(),
                error=error,
                latency_ms=latency_ms,
            )

    def is_ready(self) -> bool:
        """System is ready if all critical services are healthy."""
        with self._lock:
            for svc in self._services.values():
                if not svc.healthy:
                    return False
            return True

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self.is_ready(),
                "services": {
                    name: {
                        "healthy": svc.healthy,
                        "error": svc.error,
                        "latency_ms": svc.latency_ms,
                    }
                    for name, svc in self._services.items()
                },
                "circuit_breakers": get_all_circuit_breaker_status(),
            }


# Global health registry singleton
_health_registry = HealthRegistry()


def get_health_registry() -> HealthRegistry:
    return _health_registry


# ── Retry with exponential backoff ────────────────────────────────────

def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    circuit_breaker: CircuitBreaker | None = None,
    fallback: Callable[[], T] | None = None,
) -> T:
    """Execute a function with retry and exponential backoff.

    If a circuit breaker is provided, it's checked before each attempt
    and updated after success/failure.

    If all retries fail and a fallback is provided, the fallback is called.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if circuit_breaker and not circuit_breaker.can_execute():
            logger.warning(
                "Circuit breaker %s is open, skipping attempt %d",
                circuit_breaker.name, attempt + 1,
            )
            break

        try:
            result = fn()
            if circuit_breaker:
                circuit_breaker.record_success()
            return result
        except Exception as e:
            last_error = e
            if circuit_breaker:
                circuit_breaker.record_failure()
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    "Attempt %d/%d failed: %s. Retrying in %.1fs...",
                    attempt + 1, max_retries + 1, e, delay,
                )
                time.sleep(delay)

    if fallback is not None:
        logger.info("All retries exhausted, using fallback")
        return fallback()

    raise last_error or RuntimeError("All retries exhausted with no error captured")
