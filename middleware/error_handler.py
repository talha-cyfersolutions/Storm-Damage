from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from config import Config

logger = logging.getLogger("geoapi.errors")


def register_error_handlers(app: FastAPI) -> None:
    """Registers standardized JSON error handlers for all exceptions."""

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        errors = []
        for error in exc.errors():
            errors.append({
                "loc": list(error.get("loc", [])),
                "msg": error.get("msg", "Invalid parameter"),
                "type": error.get("type", "value_error"),
            })

        logger.warning(
            "Request validation failed on %s %s: %s [req_id=%s]",
            request.method,
            request.url.path,
            errors,
            req_id,
        )

        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Input validation failed. Please check the request parameters.",
                    "details": errors,
                    "request_id": req_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        status_code = exc.status_code

        # Map common status codes to codes
        code_map = {
            400: "BAD_REQUEST",
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            413: "PAYLOAD_TOO_LARGE",
            416: "RANGE_NOT_SATISFIABLE",
            429: "TOO_MANY_REQUESTS",
            500: "INTERNAL_SERVER_ERROR",
            502: "BAD_GATEWAY",
            503: "SERVICE_UNAVAILABLE",
            504: "GATEWAY_TIMEOUT",
        }
        error_code = code_map.get(status_code, f"HTTP_{status_code}")

        headers = getattr(exc, "headers", None) or {}
        if req_id:
            headers["x-request-id"] = req_id

        return JSONResponse(
            status_code=status_code,
            headers=headers,
            content={
                "error": {
                    "code": error_code,
                    "message": str(exc.detail),
                    "details": None,
                    "request_id": req_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        req_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled server exception on %s %s [req_id=%s]: %s", request.method, request.url.path, req_id, exc)

        message = (
            "An unexpected internal server error occurred."
            if Config.APP_ENV == "production"
            else f"Internal server error: {exc!s}"
        )

        return JSONResponse(
            status_code=500,
            headers={"x-request-id": req_id} if req_id else {},
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": message,
                    "details": None,
                    "request_id": req_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            },
        )

