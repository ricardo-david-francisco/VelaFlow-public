"""Health check routes — Kubernetes liveness and readiness probes.

Provides /health, /health/live, and /health/ready endpoints for
container orchestration health monitoring.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from brain.security.circuit_breaker import get_health_registry

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Basic health check."""
    return {"status": "healthy", "service": "velaflow-enterprise"}


@router.get("/health/live")
async def liveness() -> dict:
    """Kubernetes liveness probe — is the process alive?"""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Kubernetes readiness probe — is the service ready to accept traffic?

    Aggregates health status from all registered services and
    circuit breakers. Returns 503 if any critical service is down.
    """
    registry = get_health_registry()
    status = registry.get_status()
    is_ready = status["ready"]
    payload = {"status": "ready" if is_ready else "degraded", **status}
    if not is_ready:
        return JSONResponse(status_code=503, content=payload)
    return payload
