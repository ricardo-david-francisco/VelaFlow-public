"""Tests for resilience patterns — circuit breaker, retry, rate limiter."""

from __future__ import annotations

import time

import pytest

from brain.security.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    GracefulDegrader,
    RateLimiter,
    retry_with_backoff,
)


# ── Circuit Breaker Tests ──────────────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

    def test_success_keeps_closed(self):
        cb = CircuitBreaker("test")
        result = cb.call(lambda: 42)
        assert result == 42
        assert cb.state == CircuitState.CLOSED

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3)

        def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            cb.call(fail)
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2)

        def fail():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(fail)

        assert cb.state == CircuitState.OPEN

    def test_open_rejects_calls(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=60)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))

        with pytest.raises(CircuitOpenError):
            cb.call(lambda: 42)

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=0.01)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))

        assert cb.state == CircuitState.OPEN
        time.sleep(0.02)

        # Next call should succeed and close the circuit
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test", failure_threshold=1, reset_timeout=0.01)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("first")))

        time.sleep(0.02)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("second")))

        assert cb.state == CircuitState.OPEN

    def test_reset(self):
        cb = CircuitBreaker("test", failure_threshold=1)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError()))

        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_passes_args_and_kwargs(self):
        cb = CircuitBreaker("test")
        result = cb.call(lambda x, y=0: x + y, 3, y=7)
        assert result == 10


# ── Retry with Backoff Tests ──────────────────────────────────────────

class TestRetryWithBackoff:
    def test_succeeds_first_try(self):
        calls = []

        @retry_with_backoff(max_retries=3, base_delay=0.01)
        def succeed():
            calls.append(1)
            return "ok"

        assert succeed() == "ok"
        assert len(calls) == 1

    def test_retries_on_failure(self):
        attempts = []

        @retry_with_backoff(max_retries=2, base_delay=0.01)
        def fail_then_succeed():
            attempts.append(1)
            if len(attempts) < 2:
                raise ValueError("transient")
            return "recovered"

        assert fail_then_succeed() == "recovered"
        assert len(attempts) == 2

    def test_raises_after_max_retries(self):
        @retry_with_backoff(max_retries=2, base_delay=0.01)
        def always_fail():
            raise RuntimeError("permanent")

        with pytest.raises(RuntimeError, match="permanent"):
            always_fail()

    def test_only_retries_specified_exceptions(self):
        @retry_with_backoff(
            max_retries=3,
            base_delay=0.01,
            retryable_exceptions=(ValueError,),
        )
        def wrong_error():
            raise TypeError("not retryable")

        with pytest.raises(TypeError):
            wrong_error()


# ── Rate Limiter Tests ─────────────────────────────────────────────────

class TestRateLimiter:
    def test_allows_within_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert limiter.allow("tenant-1") is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            limiter.allow("tenant-1")
        assert limiter.allow("tenant-1") is False

    def test_different_keys_independent(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        for _ in range(2):
            limiter.allow("tenant-1")
        assert limiter.allow("tenant-1") is False
        assert limiter.allow("tenant-2") is True

    def test_remaining_count(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        assert limiter.remaining("tenant-1") == 5
        limiter.allow("tenant-1")
        assert limiter.remaining("tenant-1") == 4

    def test_window_expiration(self):
        limiter = RateLimiter(max_requests=1, window_seconds=0.01)
        limiter.allow("tenant-1")
        assert limiter.allow("tenant-1") is False
        time.sleep(0.02)
        assert limiter.allow("tenant-1") is True


# ── Graceful Degrader Tests ───────────────────────────────────────────

class TestGracefulDegrader:
    def test_returns_primary_on_success(self):
        degrader = GracefulDegrader()
        result = degrader.execute(
            primary=lambda: [1, 2, 3],
            fallback=[],
            operation="test",
        )
        assert result == [1, 2, 3]
        assert not degrader.is_degraded

    def test_returns_fallback_on_failure(self):
        degrader = GracefulDegrader()
        result = degrader.execute(
            primary=lambda: (_ for _ in ()).throw(RuntimeError("down")),
            fallback="cached",
            operation="api_call",
        )
        assert result == "cached"
        assert degrader.is_degraded
        assert "api_call" in degrader.degraded_operations

    def test_clears_degraded_on_recovery(self):
        degrader = GracefulDegrader()
        # Fail
        degrader.execute(
            primary=lambda: (_ for _ in ()).throw(RuntimeError("down")),
            fallback=[],
            operation="test",
        )
        assert degrader.is_degraded

        # Recover
        degrader.execute(
            primary=lambda: "ok",
            fallback=[],
            operation="test",
        )
        assert not degrader.is_degraded

    def test_tracks_multiple_degraded_operations(self):
        degrader = GracefulDegrader()
        degrader.execute(
            primary=lambda: (_ for _ in ()).throw(RuntimeError("a")),
            fallback=None,
            operation="todoist",
        )
        degrader.execute(
            primary=lambda: (_ for _ in ()).throw(RuntimeError("b")),
            fallback=None,
            operation="notion",
        )
        assert len(degrader.degraded_operations) == 2
