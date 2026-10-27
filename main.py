from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from config import BASE_DIR, Config
from core.logging import setup_logging
from middleware import (
    AccessLogMiddleware,
    PayloadLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    SelectiveGZipMiddleware,
    register_error_handlers,
)
from routes import health_router, storm_events_router, storm_router

logger = logging.getLogger("geoapi.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages application startup and graceful shutdown."""
    setup_logging()
    logger.info(
        "Starting %s [env=%s, version=%s]",
        Config.APP_NAME,
        Config.APP_ENV,
        Config.APP_VERSION,
    )
    logger.info("Storage backend configured: %s", Config.COG_STORAGE_BACKEND)
    logger.info("Rate limiting enabled: %s (limit: %s/min, heavy: %s/min)",
                Config.RATE_LIMIT_ENABLED, Config.RATE_LIMIT_REQUESTS, Config.HEAVY_RATE_LIMIT_REQUESTS)
    logger.info("Allowed CORS origins: %s", Config.frontend_origins)

    yield

    logger.info("Shutting down %s gracefully...", Config.APP_NAME)


# Build FastAPI app instance
docs_url = "/docs" if Config.DOCS_ENABLED else None
redoc_url = "/redoc" if Config.DOCS_ENABLED else None
openapi_url = "/openapi.json" if Config.DOCS_ENABLED else None

app = FastAPI(
    title=Config.APP_NAME,
    version=Config.APP_VERSION,
    description=(
        "Production-grade geospatial API for rapid post-storm damage mapping, "
        "Sentinel-1 SAR change detection, and Cloud-Optimized GeoTIFF streaming."
    ),
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
    lifespan=lifespan,
    license_info={
        "name": "MIT License",
    },
)

# 1. Custom and Security Middleware (order: outermost to innermost)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(PayloadLimitMiddleware)
app.add_middleware(AccessLogMiddleware)

# 2. Selective GZip compression for responses > 1 KB (excluding pre-compressed GeoTIFFs)
app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)

# 3. CORS Middleware — Allow all frontend origins with credentials
is_wildcard = (
    not Config.FRONTEND_URLS
    or Config.FRONTEND_URLS.strip() == "*"
    or "*" in Config.frontend_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if is_wildcard else Config.frontend_origins,
    allow_origin_regex=".*" if is_wildcard else r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=Config.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Request-ID",
        "Content-Range",
        "Accept-Ranges",
        "Content-Length",
        "ETag",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "Retry-After",
    ],
)

# 4. Standardized Global Error Handlers
register_error_handlers(app)

# 5. Route Registration
# Health & Probes (mounted both at root and under /api)
app.include_router(health_router)
app.include_router(health_router, prefix="/api")

# Primary Versioned API Routes (/api/v1)
app.include_router(storm_router, prefix="/api/v1", tags=["Storm Damage Imagery v1"])
app.include_router(storm_events_router, prefix="/api/v1", tags=["Storm Events & NOAA Imagery"])

# Backwards Compatibility Route (/api)
app.include_router(storm_router, prefix="/api", tags=["Storm Damage Imagery (Legacy)"])


@app.get("/", tags=["Root"], summary="API Root Overview")
async def root_overview() -> dict[str, str | dict[str, str]]:
    """Returns top-level API metadata and navigational endpoints."""
    return {
        "app_name": Config.APP_NAME,
        "version": Config.APP_VERSION,
        "environment": Config.APP_ENV,
        "status": "operational",
        "documentation": "/docs" if Config.DOCS_ENABLED else "disabled",
        "viewer": "/viewer",
        "endpoints": {
            "health": "/health",
            "readiness": "/health/ready",
            "liveness": "/health/live",
            "process_damage": "/api/v1/process-damage",
            "storms_active": "/api/v1/storms/active",
            "storms_history": "/api/v1/storms/history",
            "noaa_imagery_search": "/api/v1/imagery/noaa/search",
            "cog_assets": "/api/v1/cog/assets/{event_id}/{acquisition_date}.tif",
        },
    }


@app.get("/viewer", tags=["Viewer"], summary="Interactive Storm Damage Map Viewer")
async def get_viewer() -> FileResponse:
    """Serves the interactive Mapbox storm damage visualization UI."""
    return FileResponse(BASE_DIR / "index.html", media_type="text/html")


@app.get("/api/v1/viewer-config", tags=["Viewer"], summary="Public viewer configuration")
async def viewer_config() -> dict[str, str | None]:
    """Returns browser-safe configuration without exposing server credentials."""
    return {"mapbox_access_token": Config.MAPBOX_ACCESS_TOKEN}
