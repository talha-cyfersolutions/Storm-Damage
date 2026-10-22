from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    loc: list[str | int] = Field(..., description="Location path of the invalid field")
    msg: str = Field(..., description="Human-readable description of the error")
    type: str = Field(..., description="Error identifier code")


class ErrorPayload(BaseModel):
    code: str = Field(..., description="Standardized machine-readable error code")
    message: str = Field(..., description="Human-readable error explanation")
    details: list[Any] | None = Field(default=None, description="Detailed field errors or contextual info")
    request_id: str | None = Field(default=None, description="Unique correlation ID for tracing")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp of error occurrence",
    )


class ErrorResponse(BaseModel):
    error: ErrorPayload

