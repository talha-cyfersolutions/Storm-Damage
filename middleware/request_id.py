from __future__ import annotations

import uuid
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from core.logging import request_id_ctx


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Extracts or generates an X-Request-ID for every incoming request,
    stores it in contextvars and request.state, and includes it in response headers.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming_id = request.headers.get("x-request-id")
        request_id = incoming_id if incoming_id and len(incoming_id) <= 64 else str(uuid.uuid4())

        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id

        try:
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)

