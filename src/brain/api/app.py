"""FastAPI application factory — Multi-tenant VelaFlow API.

Creates and configures the FastAPI application with:
- JWT-based authentication
- Multi-tenant context middleware
- RBAC permission enforcement
- Security response headers (HSTS, CSP, X-Frame-Options)
- Health/readiness probes for Kubernetes
- CORS for web frontend integration

Deployment targets:
- LXC container (primary): uvicorn behind Caddy/nginx reverse proxy
- Docker: containerized with docker-compose
- Kubernetes: deployed via Helm chart with KEDA auto-scaling
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from brain.api.routes import auth, health, tenants, tasks, digests, pipelines, webhooks, vault, data_explorer, billing, dashboard, demos, metrics, rag
from brain.api.middleware import TenantContextMiddleware

logger = logging.getLogger(__name__)

_IS_PRODUCTION = os.environ.get("ENVIRONMENT", "").lower() == "production"


class HTTPSOnlyMiddleware(BaseHTTPMiddleware):
    """Refuse plain-HTTP requests; redirect with 308 to the HTTPS scheme.

    Active in every environment, not gated on ``ENVIRONMENT``. The user
    requirement is HTTPS from day zero, dev included. The localhost
    health-check exception below is intentionally narrow: it allows the
    operator's loopback liveness probe (``curl http://127.0.0.1/health``)
    only when the request did not arrive through a reverse proxy that
    already terminated TLS upstream (``X-Forwarded-Proto`` absent).
    """

    _LOOPBACK_HEALTH_PATHS = {"/health", "/health/live", "/health/ready"}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
        scheme = forwarded_proto or request.url.scheme
        if scheme == "https":
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        is_loopback = client_host in {"127.0.0.1", "::1", "localhost"}
        if (
            is_loopback
            and not forwarded_proto
            and request.url.path in self._LOOPBACK_HEALTH_PATHS
        ):
            return await call_next(request)

        new_url = request.url.replace(scheme="https")
        return RedirectResponse(url=str(new_url), status_code=308)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security response headers on every response."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> FastAPI:
    """Build the FastAPI application with all routes and middleware."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info("VelaFlow Enterprise API starting up")
        # Install global crash handler for impenetrable logging
        from brain.security.action_ledger import install_crash_handler, get_action_ledger, ActionCategory
        from brain.security.memlock import lock_process_memory
        install_crash_handler()
        # Pin API pages in RAM so decrypted credentials in the request path
        # cannot be paged to swap. Best-effort, silent on non-Linux.
        lock_process_memory()
        get_action_ledger().log(ActionCategory.SYSTEM, "api_startup")
        yield
        get_action_ledger().log(ActionCategory.SYSTEM, "api_shutdown")
        logger.info("VelaFlow Enterprise API shutting down")

    app = FastAPI(
        title="VelaFlow Enterprise API",
        description=(
            "Multi-tenant AI productivity platform with on-prem medallion architecture. "
            "DuckDB engine, local data catalog, n8n orchestration, and zero-trust security."
        ),
        version="2.0.0",
        docs_url=None if _IS_PRODUCTION else "/docs",
        redoc_url=None if _IS_PRODUCTION else "/redoc",
        openapi_url=None if _IS_PRODUCTION else "/openapi.json",
        lifespan=lifespan,
    )

    # Action ledger request logging middleware
    from brain.security.action_ledger import get_action_ledger, ActionCategory
    from brain.api.routes.metrics import inc as metrics_inc

    class ActionLedgerMiddleware(BaseHTTPMiddleware):
        """Log every HTTP request to the action ledger + update metrics."""

        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            import time as _time
            start = _time.monotonic()
            response = await call_next(request)
            elapsed_ms = (_time.monotonic() - start) * 1000
            tenant_id = getattr(request.state, "tenant_id", "")
            user_id = getattr(request.state, "user_id", "")
            # Update Prometheus-style counters
            metrics_inc("http_requests_total")
            if response.status_code >= 400:
                metrics_inc("http_requests_error_total")
            # Log to action ledger (skip high-frequency health checks)
            path = request.url.path.rstrip("/") or "/"
            if path not in ("/health", "/health/live", "/health/ready", "/metrics", "/status"):
                try:
                    get_action_ledger().log_api_request(
                        method=request.method,
                        path=path,
                        status_code=response.status_code,
                        duration_ms=round(elapsed_ms, 1),
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                except Exception as exc:  # Never break the request on logging failure
                    logger.debug("action_ledger middleware suppressed: %s", exc)
            return response

    app.add_middleware(ActionLedgerMiddleware)

    # Security response headers
    app.add_middleware(SecurityHeadersMiddleware)

    # CORS — configurable origins for web frontend (restricted methods/headers)
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
        ).split(",")
        if origin.strip()
    ]
    # Security: wildcard + credentials is a CSRF vulnerability
    if "*" in allowed_origins:
        allowed_origins = ["*"]
        _allow_credentials = False
    else:
        _allow_credentials = True
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    # Multi-tenant context middleware
    app.add_middleware(TenantContextMiddleware)

    # HTTPS-only enforcement (always-on, not gated on ENVIRONMENT).
    # Added last so it is the OUTERMOST middleware: every request is
    # scheme-checked before any auth / tenant / CORS work runs.
    app.add_middleware(HTTPSOnlyMiddleware)

    # Register route modules
    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, prefix="/api/v1", tags=["auth"])
    app.include_router(tenants.router, prefix="/api/v1", tags=["tenants"])
    app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
    app.include_router(digests.router, prefix="/api/v1", tags=["digests"])
    app.include_router(pipelines.router, prefix="/api/v1", tags=["pipelines"])
    app.include_router(webhooks.router, prefix="/api/v1", tags=["webhooks"])
    app.include_router(vault.router, prefix="/api/v1", tags=["vault"])
    app.include_router(data_explorer.router, prefix="/api/v1", tags=["data"])
    app.include_router(billing.router, prefix="/api/v1", tags=["billing"])
    app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])
    app.include_router(rag.router, prefix="/api/v1", tags=["rag"])
    app.include_router(demos.router, prefix="/api/v1/admin", tags=["demos"])
    app.include_router(metrics.router, tags=["metrics"])

    return app
