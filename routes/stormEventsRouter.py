from __future__ import annotations

import logging
from datetime import date, timedelta
from math import hypot
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, Security, status

from core.rate_limiter import enforce_rate_limit
from core.security import verify_api_key
from models import (
    AnalyzeNoaaDamageRequest,
    AnalyzeStormRequest,
    ComputedStormDamageResponse,
    ErrorResponse,
    ImagerySearchResponse,
    StormAnalysisResponse,
    StormDamageRequest,
    StormEvent,
    StormEventList,
    SyncJob,
)
from services import StormDamageService
from services.storms import NoaaDamageService, storm_db
from services.storms import jobs, nceiStormEvents, noaaEri, nwsAlerts

logger = logging.getLogger("geoapi.storm_events")

storm_events_router = APIRouter()


def _parse_bbox(value: str | None) -> list[float] | None:
    if not value:
        return None
    try:
        parts = [float(p) for p in value.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'") from exc
    if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise HTTPException(status_code=400, detail="bbox must be 'minLon,minLat,maxLon,maxLat'")
    if not (-180 <= parts[0] and parts[2] <= 180 and -90 <= parts[1] and parts[3] <= 90):
        raise HTTPException(status_code=400, detail="bbox is outside valid lon/lat ranges")
    return parts


def _parse_date(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from exc


def _parse_region(value: str | None) -> list[str] | None:
    if not value:
        return None
    if value.lower() in {"mid_atlantic", "dc-va-md", "national_capital_region"}:
        return sorted(nceiStormEvents.TARGET_STATES)
    raise HTTPException(status_code=400, detail="region must be 'mid_atlantic'")


# ---------------------------------------------------------------- storms
@storm_events_router.get(
    "/storms/active",
    response_model=StormEventList,
    summary="Active NWS warnings (tornado, severe thunderstorm, flash flood, tropical)",
    responses={502: {"model": ErrorResponse}},
)
async def active_storms(request: Request, _k: str | None = Security(verify_api_key)) -> StormEventList:
    await enforce_rate_limit(request)
    try:
        events = await nwsAlerts.fetch_active_alerts()
    except Exception as exc:
        logger.error("NWS active alerts failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="NWS alerts service unavailable") from exc
    if events:
        await storm_db.upsert_events(events)  # so /storms/{id}/analyze can find them
    return StormEventList(count=len(events), events=events)


@storm_events_router.get(
    "/storms/history",
    response_model=StormEventList,
    summary="Past US storm events from NOAA NCEI Storm Events",
)
async def storm_history(
    request: Request,
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
    date_from: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: str | None = Query(None, alias="to", description="YYYY-MM-DD"),
    event_type: str | None = Query(None, alias="type", description="e.g. Tornado, Hurricane, Flash Flood"),
    min_rating: int | None = Query(None, ge=0, le=5, description="Minimum EF rating (tornadoes only)"),
    region: str | None = Query(None, description="Named region filter: mid_atlantic (DC, Virginia, Maryland)"),
    limit: int = Query(100, ge=1, le=500),
    _k: str | None = Security(verify_api_key),
) -> StormEventList:
    await enforce_rate_limit(request)
    events = await storm_db.query_events(
        bbox=_parse_bbox(bbox),
        start=_parse_date(date_from, "from"),
        end=_parse_date(date_to, "to"),
        event_type=event_type,
        min_rating=min_rating,
        states=_parse_region(region),
        limit=limit,
    )
    return StormEventList(count=len(events), events=events)


@storm_events_router.get("/storms/years", summary="List available years in NCEI Storm Events archive")
async def list_storm_years(request: Request, _k: str | None = Security(verify_api_key)) -> dict[str, Any]:
    await enforce_rate_limit(request)
    years = await nceiStormEvents.list_available_years()
    return {"available_years": years}


@storm_events_router.post(
    "/storms/sync",
    response_model=SyncJob,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start importing NCEI Storm Events for one year or multiple years (background)",
)
async def sync_storm_year(
    request: Request,
    year: int | None = Query(None, ge=1990, le=date.today().year),
    years: str | None = Query(None, description="Comma-separated or range, e.g. '2020-2024' or 'all'"),
    _k: str | None = Security(verify_api_key),
) -> SyncJob:
    await enforce_rate_limit(request, is_heavy=True)
    if not year and not years:
        raise HTTPException(status_code=400, detail="Provide 'year' (e.g. 2024) or 'years' (e.g. '2020-2024' or 'all').")

    target_years: list[int] = []
    if year:
        target_years = [year]
    elif years:
        if years.lower() == "all":
            avail = await nceiStormEvents.list_available_years()
            target_years = [y for y in avail if y >= 2018]
        elif "-" in years:
            parts = years.split("-", 1)
            try:
                y_start, y_end = int(parts[0].strip()), int(parts[1].strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid year range. Format: YYYY-YYYY") from exc
            if y_start > y_end:
                y_start, y_end = y_end, y_start
            target_years = list(range(y_start, y_end + 1))
        else:
            try:
                target_years = [int(p.strip()) for p in years.split(",") if p.strip()]
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid years list. Format: 2022,2023,2024") from exc

    if not target_years:
        raise HTTPException(status_code=400, detail="No valid target years specified.")

    target_label = f"{target_years[0]}" if len(target_years) == 1 else f"{target_years[0]}-{target_years[-1]} ({len(target_years)} yrs)"

    async def work() -> str:
        nyears, total = await nceiStormEvents.sync_years(target_years)
        return f"imported {total} events (DC/VA/MD only) across {nyears} years: {target_years}"

    job_id = await jobs.start_job("ncei", target_label, work)
    return SyncJob(**await storm_db.get_job(job_id))


@storm_events_router.get(
    "/sync/jobs/{job_id}",
    response_model=SyncJob,
    summary="Status of a background sync job",
    responses={404: {"model": ErrorResponse}},
)
async def get_sync_job(job_id: str, request: Request, _k: str | None = Security(verify_api_key)) -> SyncJob:
    await enforce_rate_limit(request)
    job = await storm_db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Sync job not found.")
    return SyncJob(**job)


@storm_events_router.post(
    "/storms/{event_id}/analyze",
    response_model=StormAnalysisResponse,
    summary="Run damage analysis and look up aerial imagery for a stored storm event",
    responses={404: {"model": ErrorResponse}},
)
async def analyze_storm(
    event_id: str,
    body: AnalyzeStormRequest,
    request: Request,
    _k: str | None = Security(verify_api_key),
) -> StormAnalysisResponse:
    await enforce_rate_limit(request, is_heavy=True)
    event: StormEvent | None = await storm_db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Storm event not found. Sync history or call /storms/active first.")

    lat0, lon0 = event.begin_lat, event.begin_lon
    lat1, lon1 = (event.end_lat or lat0), (event.end_lon or lon0)
    center_lat, center_lon = (lat0 + lat1) / 2, (lon0 + lon1) / 2
    # cover the whole damage path: half its length plus a margin
    path_km = hypot((lat1 - lat0) * 111.0, (lon1 - lon0) * 111.0 * 0.85)
    buffer_km = min(50.0, max(body.buffer_km, path_km / 2 + 2.0))

    begin = date.fromisoformat(event.begin_date)
    end = date.fromisoformat(event.end_date) if event.end_date else begin
    req = StormDamageRequest(
        latitude=center_lat,
        longitude=center_lon,
        storm_start_date=begin,
        storm_end_date=end + timedelta(days=body.days_after),
        buffer_km=buffer_km,
        prefer_sensor=body.prefer_sensor,
        incident_name=f"{event.event_type} - {event.location or event.state or event.id}",
    )

    analysis, message = None, None
    try:
        result = await StormDamageService(req).execute_analysis()
        analysis = result.model_dump()
    except ValueError as exc:
        message = str(exc)  # e.g. no satellite scenes yet; imagery lookup is still useful
    except Exception as exc:
        logger.warning("Analysis failed for %s: %s", event_id, exc)
        message = f"Satellite damage analysis unavailable: {exc}"

    bbox = StormDamageService._calculate_bbox(center_lat, center_lon, buffer_km)
    imagery = await noaaEri.search_imagery(bbox, start=begin.isoformat())
    return StormAnalysisResponse(event=event, analysis=analysis, imagery=imagery, message=message)


# --------------------------------------------------------------- imagery
@storm_events_router.get(
    "/imagery/noaa/search",
    response_model=ImagerySearchResponse,
    summary="Find indexed NOAA Emergency Response Imagery flights over an area",
)
async def search_noaa_imagery(
    request: Request,
    bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    _k: str | None = Security(verify_api_key),
) -> ImagerySearchResponse:
    await enforce_rate_limit(request)
    return await noaaEri.search_imagery(_parse_bbox(bbox), _parse_date(date_from, "from"), _parse_date(date_to, "to"))


@storm_events_router.get("/imagery/noaa/events", summary="List NOAA ERI event folders (optionally by year)")
async def list_noaa_events(
    request: Request,
    year: int | None = Query(None, ge=2000, le=date.today().year),
    _k: str | None = Security(verify_api_key),
) -> dict[str, list[str]]:
    await enforce_rate_limit(request)
    return {"events": await noaaEri.list_events(year)}


@storm_events_router.post(
    "/imagery/noaa/sync",
    response_model=SyncJob,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start indexing NOAA ERI tiles for one event, or every event in a year (background)",
)
async def sync_noaa_imagery(
    request: Request,
    event: str | None = Query(None, description="e.g. 2024_Hurricane_Helene"),
    year: int | None = Query(None, ge=2000, le=date.today().year),
    _k: str | None = Security(verify_api_key),
) -> SyncJob:
    await enforce_rate_limit(request, is_heavy=True)
    if bool(event) == bool(year):
        raise HTTPException(status_code=400, detail="Provide exactly one of 'event' or 'year'.")
    if event and not noaaEri.is_valid_event_name(event):
        raise HTTPException(status_code=400, detail=f"Invalid NOAA ERI event name: {event!r}")

    async def work() -> str:
        names = [event] if event else await noaaEri.list_events(year)
        flights = tiles = 0
        for name in names:
            f, t = await noaaEri.sync_event(name)
            flights, tiles = flights + f, tiles + t
        return f"indexed {len(names)} events, {flights} flights, {tiles} tiles"

    job_id = await jobs.start_job("noaa-eri", event or str(year), work)
    return SyncJob(**await storm_db.get_job(job_id))


@storm_events_router.get(
    "/imagery/noaa/tiles/{event}/{flight}/{z}/{x}/{y}.png",
    summary="XYZ map tiles rendered from NOAA ERI GeoTIFFs",
    responses={200: {"content": {"image/png": {}}}, 404: {"model": ErrorResponse}},
)
async def noaa_tile(
    event: str,
    flight: str,
    z: int,
    x: int,
    y: int,
    request: Request,
    _k: str | None = Security(verify_api_key),
) -> Response:
    await enforce_rate_limit(request)
    if not noaaEri.is_valid_event_name(event) or noaaEri.flight_date(flight) is None:
        raise HTTPException(status_code=400, detail="Invalid event or flight name.")
    if not (0 <= z <= 22) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        raise HTTPException(status_code=400, detail="Tile coordinates out of range.")
    png = await noaaEri.render_xyz_tile(event, flight, z, x, y)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@storm_events_router.post(
    "/imagery/noaa/analyze-damage",
    response_model=ComputedStormDamageResponse,
    summary="Run high-resolution debris and structural damage detection on NOAA aerial photography",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def analyze_noaa_aerial_damage(
    body: AnalyzeNoaaDamageRequest,
    request: Request,
    _k: str | None = Security(verify_api_key),
) -> ComputedStormDamageResponse:
    await enforce_rate_limit(request, is_heavy=True)
    if not noaaEri.is_valid_event_name(body.event):
        raise HTTPException(status_code=400, detail=f"Invalid NOAA ERI event name: {body.event!r}")
    try:
        return await NoaaDamageService.analyze(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("NOAA aerial damage detection failed for event %s", body.event)
        raise HTTPException(status_code=500, detail="NOAA aerial damage analysis failed.") from exc
