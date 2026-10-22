from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from config import Config


class PayloadLimitMiddleware(BaseHTTPMiddleware):
    """Rejects incoming requests exceeding Config.MAX_PAYLOAD_SIZE_BYTES with 413 Payload Too Large."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length_header = request.headers.get("content-length")
        if content_length_header:
            try:
                content_length = int(content_length_header)
                if content_length > Config.MAX_PAYLOAD_SIZE_BYTES:
                    req_id = getattr(request.state, "request_id", None)
                    return JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "PAYLOAD_TOO_LARGE",
                                "message": f"Payload size {content_length} bytes exceeds maximum allowed {Config.MAX_PAYLOAD_SIZE_BYTES} bytes.",
                                "details": None,
                                "request_id": req_id,
                            }
                        },
                    )
            except ValueError:
                pass

        return await call_next(request)

