"""Multi-tenant context middleware.

Extracts tenant information from the JWT token in the Authorization
header and injects it into the request state for downstream route
handlers to use.

Unauthenticated routes (health checks) bypass this middleware.
API docs are conditionally public based on ENVIRONMENT setting.
"""

from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from brain.api.auth import verify_token

logger = logging.getLogger(__name__)

_IS_PRODUCTION = os.environ.get("ENVIRONMENT", "").lower() == "production"

# Routes that don't require authentication
_PUBLIC_PATHS = {
    "/health",
    "/health/ready",
    "/health/live",
    "/metrics",
    "/status",
    "/api/v1/auth/google",
    "/api/v1/tenants",
    "/api/v1/tenants/login",
    "/api/v1/webhooks/stripe",
}

# Docs paths are public only in non-production environments
_DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}


class TenantContextMiddleware(BaseHTTPMiddleware):
    """Extract tenant context from JWT and inject into request state."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Let CORS preflight through — CORSMiddleware handles OPTIONS
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip auth for public endpoints (normalize trailing slashes)
        path = request.url.path.rstrip("/") or "/"
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        if not _IS_PRODUCTION and path in _DOCS_PATHS:
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # Strip "Bearer "
        claims = verify_token(token)
        if claims is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        # Inject tenant context into request state
        request.state.tenant_id = claims.tenant_id
        request.state.role = claims.role
        request.state.email = claims.email
        request.state.user_id = claims.user_id
        request.state.user_role = claims.user_role

        return await call_next(request)
