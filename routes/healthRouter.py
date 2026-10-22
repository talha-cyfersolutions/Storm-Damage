from __future__ import annotations

import time
import aiohttp
from fastapi import APIRouter, Response, status

from config import Config
from models import HealthResponse, ReadinessResponse
from services import cog_storage

health_router = APIRouter(tags=["Health & Probes"])

SERVER_START_TIME = time.time()


@health_router.get(
    "/health",
    response_model=HealthResponse,
    summary="System Health Status",
    description="Returns service uptime, environment, version, and storage backend status.",
)
async def get_health() -> HealthResponse:
    storage_health = await cog_storage.check_health()
    overall_status = "healthy" if storage_health.status == "healthy" else "degraded"

    return HealthResponse(
        status=overall_status,
        app_name=Config.APP_NAME,
        version=Config.APP_VERSION,
        environment=Config.APP_ENV,
        uptime_seconds=round(time.time() - SERVER_START_TIME, 2),
        storage=storage_health,
    )


@health_router.get(
    "/health/live",
    summary="Liveness Probe",
    description="Returns 200 OK if the application process is running and responsive.",
)
async def liveness_probe() -> dict[str, str]:
    return {"status": "live"}


@health_router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness Probe",
    description="Checks whether the application is fully ready to accept incoming traffic.",
)
async def readiness_probe(response: Response) -> ReadinessResponse:
    storage_health = await cog_storage.check_health()

    # Ping Planetary Computer STAC endpoint with a short timeout
    stac_status = "reachable"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(Config.PLANETARY_COMPUTER_STAC) as resp:
                if resp.status >= 500:
                    stac_status = "unreachable"
    except Exception:
        stac_status = "unreachable"

    is_ready = storage_health.status != "unhealthy" and stac_status != "unreachable"
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        storage=storage_health,
        stac_api=stac_status,
    )

