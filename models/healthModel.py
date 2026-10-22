from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class StorageHealth(BaseModel):
    backend: Literal["local", "s3"]
    status: Literal["healthy", "degraded", "unhealthy"]
    details: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    storage: StorageHealth
    stac_api: Literal["reachable", "unreachable", "skipped"]


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    app_name: str
    version: str
    environment: str
    uptime_seconds: float
    storage: StorageHealth | None = None

