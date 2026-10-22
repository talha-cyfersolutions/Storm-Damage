from __future__ import annotations

import logging
import time
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from core.security import get_client_ip

logger = logging.getLogger("geoapi.access")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Logs incoming HTTP requests, response status codes, and execution latencies."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start_time = time.monotonic()
        client_ip = get_client_ip(request)
        method = request.method
        path = request.url.path

        # Process the request
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            status_code = 500
            raise
        finally:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            req_id = getattr(request.state, "request_id", "-")

            # Attach rate limit headers if available on request.state
            if hasattr(request.state, "rate_limit"):
                rl = request.state.rate_limit
                if "response" in locals() and response is not None:
                    response.headers["X-RateLimit-Limit"] = str(rl.limit)
                    response.headers["X-RateLimit-Remaining"] = str(rl.remaining)
                    response.headers["X-RateLimit-Reset"] = str(rl.reset_seconds)

            # Avoid logging noisy health checks during routine heartbeat probes
            if path in ("/health/live", "/health/ready") and status_code < 400:
                pass
            else:
                extra = {
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "client_ip": client_ip,
                    "request_id": req_id,
                }
                logger.info(
                    "%s %s -> %s (%sms) [ip=%s, req_id=%s]",
                    method,
                    path,
                    status_code,
                    duration_ms,
                    client_ip,
                    req_id,
                    extra={"extra_fields": extra},
                )

