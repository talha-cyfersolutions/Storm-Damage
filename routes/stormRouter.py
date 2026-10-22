from __future__ import annotations

import logging
import re
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, Security, status
from fastapi.responses import RedirectResponse

from config import Config
from core.rate_limiter import enforce_rate_limit
from core.security import verify_api_key
from models import ComputedStormDamageResponse, ErrorResponse, StormDamageRequest
from services import StormDamageService, cog_storage

logger = logging.getLogger("geoapi.storm")

storm_router = APIRouter()

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGE_HEADER_REGEX = re.compile(r"^bytes=(\d*)-(\d*)$")


@storm_router.post(
    "/process-damage",
    response_model=ComputedStormDamageResponse,
    summary="Compute Rapid Post-Storm Damage",
    description=(
        "Analyzes pre- and post-storm Sentinel-1 SAR / Sentinel-2 imagery to generate "
        "calibrated structural change detection, flood mapping, and a 4-band Cloud-Optimized GeoTIFF."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid date range or spatial parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized - Missing or invalid API key"},
        404: {"model": ErrorResponse, "description": "No satellite scenes found covering target area and dates"},
        422: {"model": ErrorResponse, "description": "Schema validation failure"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Pipeline execution error"},
    },
)
async def process_storm_damage(
    request_data: StormDamageRequest,
    request: Request,
    _api_key: str | None = Security(verify_api_key),
) -> ComputedStormDamageResponse:
    """
    Computes rapid post-storm damage extents using Sentinel-1 SAR and optical change detection.
    Generates a 4-band Cloud-Optimized GeoTIFF with damage classifications and precise area statistics.
    """
    # Enforce heavy compute rate limiting
    await enforce_rate_limit(request, is_heavy=True)

    if request_data.storm_end_date < request_data.storm_start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="storm_end_date cannot precede storm_start_date.",
        )

    try:
        service = StormDamageService(request_data)
        return await service.execute_analysis()
    except ValueError as exc:
        logger.warning("Storm damage processing parameter error: %s", exc)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Storm damage processing pipeline failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during satellite imagery processing and COG encoding.",
        ) from exc


@storm_router.get(
    "/cog/assets/{event_id}/{acquisition_date}.tif",
    summary="Stream Cloud-Optimized GeoTIFF Asset",
    description="Streams or redirects to the generated COG asset with full HTTP Range request support.",
    responses={
        200: {"description": "Full GeoTIFF binary stream"},
        206: {"description": "Partial byte range stream for MapLibre GL / GIS canvas"},
        307: {"description": "Redirect to S3 presigned asset URL"},
        400: {"model": ErrorResponse, "description": "Invalid parameters"},
        404: {"model": ErrorResponse, "description": "Asset expired or not found"},
        416: {"model": ErrorResponse, "description": "Range Not Satisfiable"},
    },
)
async def get_storm_cog_asset(
    event_id: int,
    acquisition_date: str,
    request: Request,
    _api_key: str | None = Security(verify_api_key),
) -> Response:
    """
    Streams the generated Storm Damage Cloud-Optimized GeoTIFF.
    Supports HTTP Range requests for MapLibre GL and GeoRaster canvas decoding.
    """
    await enforce_rate_limit(request, is_heavy=False)

    # Sanitize acquisition_date to prevent path traversal
    if not DATE_REGEX.match(acquisition_date):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid acquisition_date format. Expected YYYY-MM-DD.",
        )

    key = f"storm-cogs/storm_{event_id}_{acquisition_date}.tif"

    # If backend is S3, redirect directly to S3 presigned URL
    if Config.COG_STORAGE_BACKEND == "s3":
        presigned_url = await cog_storage.get_s3_url(key)
        if presigned_url:
            return RedirectResponse(url=presigned_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storm COG asset not found in S3 storage.",
        )

    # Local storage backend
    total_length = await cog_storage.get_file_size(key)
    if total_length is None or total_length <= 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storm COG asset has expired or does not exist.",
        )

    range_header = request.headers.get("range")
    status_code = status.HTTP_200_OK
    content_range = None

    if range_header:
        match = RANGE_HEADER_REGEX.match(range_header)
        if not match:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="Invalid Range header format.",
                headers={"Content-Range": f"bytes */{total_length}"},
            )

        start_str, end_str = match.groups()
        if not start_str and not end_str:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="Invalid byte range.",
                headers={"Content-Range": f"bytes */{total_length}"},
            )

        if start_str:
            start = int(start_str)
            end = int(end_str) if end_str else total_length - 1
        else:
            suffix_len = int(end_str)
            start = max(0, total_length - suffix_len)
            end = total_length - 1

        if start >= total_length or end < start:
            raise HTTPException(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                detail="Byte range is outside the asset bounds.",
                headers={"Content-Range": f"bytes */{total_length}"},
            )

        end = min(end, total_length - 1)
        response_content = await cog_storage.read_range(key, start, end)
        if response_content is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset read error.")

        content_range = f"bytes {start}-{end}/{total_length}"
        status_code = status.HTTP_206_PARTIAL_CONTENT
    else:
        # Full content
        response_content = await cog_storage.get_local(key)
        if response_content is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset read error.")

    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400, immutable",
        "Content-Disposition": f'inline; filename="storm_{event_id}_{acquisition_date}.tif"',
        "Content-Length": str(len(response_content)),
        "ETag": f'"{event_id}-{acquisition_date}"',
    }
    if content_range:
        headers["Content-Range"] = content_range

    return Response(
        content=response_content,
        status_code=status_code,
        media_type="image/tiff; application=geotiff; profile=cloud-optimized",
        headers=headers,
    )
