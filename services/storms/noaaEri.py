"""NOAA Emergency Response Imagery (ERI) on AWS Open Data (`noaa-eri-pds`).

Tiles are tiled/overviewed EPSG:4326 GeoTIFFs. Each file name carries the upper-left
corner of a 0.025 degree square, e.g. ``20200828aC0914030w293430n.tif`` is
91d40m30s W, 29d34m30s N. That lets us build a spatial index from the key listing
alone, without opening any raster.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import warnings
from typing import Any

import boto3
import numpy as np
import rasterio
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

from config import Config
from models.stormEventModel import ImageryItem, ImagerySearchResponse
from services.storms.db import storm_db

logger = logging.getLogger("geoapi.imagery.noaa")

TILE_SIZE_DEG = 0.025
_TILE_RE = re.compile(r"C(\d{3})(\d{2})(\d{2})([ew])(\d{2})(\d{2})(\d{2})([ns])\.tif$", re.IGNORECASE)
_FLIGHT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})([a-z]+)_RGB$", re.IGNORECASE)
_EVENT_RE = re.compile(r"^\d{4}_[A-Za-z0-9_\-]+$")
WEB_MERCATOR_ORIGIN = 20037508.342789244
MIN_TILE_ZOOM = 12


def parse_tile_bounds(key: str) -> tuple[float, float, float, float] | None:
    """Return (min_lon, min_lat, max_lon, max_lat) from a tile key, or None."""
    match = _TILE_RE.search(key)
    if not match:
        return None
    lon_d, lon_m, lon_s, ew, lat_d, lat_m, lat_s, ns = match.groups()
    lon = int(lon_d) + int(lon_m) / 60 + int(lon_s) / 3600
    lat = int(lat_d) + int(lat_m) / 60 + int(lat_s) / 3600
    if ew.lower() == "w":
        lon = -lon
    if ns.lower() == "s":
        lat = -lat
    # filename holds the upper-left corner
    return (
        round(lon, 6),
        round(lat - TILE_SIZE_DEG, 6),
        round(lon + TILE_SIZE_DEG, 6),
        round(lat, 6),
    )


def flight_date(flight: str) -> str | None:
    match = _FLIGHT_RE.match(flight)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def is_valid_event_name(name: str) -> bool:
    return bool(_EVENT_RE.match(name))


def xyz_to_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * WEB_MERCATOR_ORIGIN / (2**z)
    minx = -WEB_MERCATOR_ORIGIN + x * size
    maxy = WEB_MERCATOR_ORIGIN - y * size
    return minx, maxy - size, minx + size, maxy


def xyz_to_lonlat_bounds(z: int, x: int, y: int) -> list[float]:
    n = 2**z

    def lon(xx: float) -> float:
        return xx / n * 360.0 - 180.0

    def lat(yy: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return [lon(x), lat(y + 1), lon(x + 1), lat(y)]


def _s3_client():
    return boto3.client(
        "s3", region_name="us-east-1", config=BotoConfig(signature_version=UNSIGNED)
    )


def _list_event_prefixes(year: int | None) -> list[str]:
    client = _s3_client()
    prefix = f"{year}" if year else ""
    names: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=Config.NOAA_ERI_BUCKET, Prefix=prefix, Delimiter="/"
    ):
        names += [p["Prefix"].rstrip("/") for p in page.get("CommonPrefixes", [])]
    return [n for n in names if is_valid_event_name(n)]


def _list_event_tiles(event: str) -> list[tuple]:
    client = _s3_client()
    rows: list[tuple] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=Config.NOAA_ERI_BUCKET, Prefix=f"{event}/"
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) != 3 or not _FLIGHT_RE.match(parts[1]):
                continue  # skip downloads/, readmes and anything off-pattern
            bounds = parse_tile_bounds(key)
            if bounds is None:
                continue
            rows.append((event, parts[1], key, *bounds))
    return rows


async def list_events(year: int | None = None) -> list[str]:
    return await asyncio.to_thread(_list_event_prefixes, year)


async def sync_event(event: str) -> tuple[int, int]:
    """Index every tile of one event. Returns (flights, tiles)."""
    if not is_valid_event_name(event):
        raise ValueError(f"Invalid NOAA ERI event name: {event!r}")
    rows = await asyncio.to_thread(_list_event_tiles, event)
    await storm_db.replace_tiles(f"{event}/", rows)
    flights = len({r[1] for r in rows})
    logger.info("Indexed %s: %s flights, %s tiles", event, flights, len(rows))
    return flights, len(rows)


async def search_imagery(bbox: list[float], start: str | None = None, end: str | None = None) -> ImagerySearchResponse:
    flights = await storm_db.flights_in_bbox(bbox)
    items: list[ImageryItem] = []
    for f in flights:
        date = flight_date(f["flight"])
        if date is None or (start and date < start) or (end and date > end):
            continue
        items.append(
            ImageryItem(
                provider="noaa-eri",
                event=f["event"],
                flight=f["flight"],
                date=date,
                tile_count=f["tile_count"],
                bbox=[f["min_lon"], f["min_lat"], f["max_lon"], f["max_lat"]],
                tiles_url=f"/api/v1/imagery/noaa/tiles/{f['event']}/{f['flight']}/{{z}}/{{x}}/{{y}}.png",
            )
        )
    message = None
    if not items:
        message = (
            "No NOAA ERI imagery indexed for this area and period. NOAA flies mainly "
            "hurricanes and major events; run /api/v1/imagery/noaa/sync for newer events."
        )
    return ImagerySearchResponse(count=len(items), items=items, message=message)


def _blank_png(size: int = 256) -> bytes:
    return _encode_png(np.zeros((4, size, size), dtype=np.uint8))


def _encode_png(rgba: np.ndarray) -> bytes:
    _, h, w = rgba.shape
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with MemoryFile() as mem:
            with mem.open(driver="PNG", width=w, height=h, count=4, dtype="uint8") as dst:
                dst.write(rgba)
            return mem.read()


def _render_tile(keys: list[str], z: int, x: int, y: int) -> bytes:
    minx, miny, maxx, maxy = xyz_to_mercator_bounds(z, x, y)
    transform = from_bounds(minx, miny, maxx, maxy, 256, 256)
    canvas = np.zeros((4, 256, 256), dtype=np.uint8)
    env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_TIMEOUT": str(Config.GDAL_HTTP_TIMEOUT),
        "AWS_NO_SIGN_REQUEST": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": "50000000",
    }
    with rasterio.Env(**env):
        for key in keys:
            href = f"/vsis3/{Config.NOAA_ERI_BUCKET}/{key}"
            try:
                with rasterio.open(href) as src:
                    has_alpha = src.count >= 4
                    with WarpedVRT(
                        src,
                        crs="EPSG:3857",
                        transform=transform,
                        width=256,
                        height=256,
                        resampling=Resampling.bilinear,
                        add_alpha=not has_alpha,
                        src_nodata=None,
                    ) as vrt:
                        data = vrt.read([1, 2, 3, 4])
            except Exception as exc:  # one bad tile must not blank the whole map tile
                logger.warning("Skipping NOAA tile %s: %s", key, exc)
                continue
            mask = data[3] > 0
            canvas[:, mask] = data[:, mask]
    return _encode_png(canvas)


async def render_xyz_tile(event: str, flight: str, z: int, x: int, y: int) -> bytes:
    if z < MIN_TILE_ZOOM:
        return await asyncio.to_thread(_blank_png)
    bbox = xyz_to_lonlat_bounds(z, x, y)
    keys = await storm_db.tiles_for_bbox(event, flight, bbox, limit=9)
    if not keys:
        return await asyncio.to_thread(_blank_png)
    return await asyncio.to_thread(_render_tile, keys, z, x, y)

