from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from config import Config


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies OWASP-recommended HTTP security headers to all responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        headers = response.headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["X-XSS-Protection"] = "1; mode=block"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        if Config.APP_ENV == "production":
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

        return response

