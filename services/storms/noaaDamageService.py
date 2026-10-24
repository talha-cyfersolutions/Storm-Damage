"""High-Resolution Aerial Damage Detection for NOAA Emergency Response Imagery (ERI).

Analyzes 0.3m RGB aerial photography using high-frequency edge variance, structural
entropy, debris field scattering, and FEMA blue roof / tarp spectral signatures.
Outputs a 4-band Cloud-Optimized GeoTIFF (Bands 1-3: RGB Aerial, Band 4: Damage Class).
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

from config import Config
from models import (
    AnalyzeNoaaDamageRequest,
    ComputedStormDamageResponse,
    StormDamageStatistics,
    StormSymbologyClass,
)
from services.cogService import CogService
from services.storms import noaaEri, storm_db

logger = logging.getLogger("geoapi.noaa_damage")
cog_storage = CogService()


def uniform_filter_2d(arr: np.ndarray, size: int = 5) -> np.ndarray:
    """Fast vectorized 2D box filter using integral images (SAT)."""
    padded = np.pad(arr, ((size, size), (size, size)), mode="reflect")
    sat = np.cumsum(np.cumsum(padded, axis=0, dtype=np.float64), axis=1, dtype=np.float64)
    h, w = arr.shape
    r = size // 2
    y0, y1 = size - r - 1, size - r - 1 + h
    y2, y3 = y0 + size, y1 + size
    x0, x1 = size - r - 1, size - r - 1 + w
    x2, x3 = x0 + size, x1 + size
    res = (sat[y2:y3, x2:x3] - sat[y0:y1, x2:x3] - sat[y2:y3, x0:x1] + sat[y0:y1, x0:x1]) / (size * size)
    return res.astype(np.float32)


def detect_aerial_damage(rgb: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    """
    Classifies 0.3m RGB aerial photography into 4 damage severity classes:
    1: Undamaged Terrain / Intact Buildings
    2: Minor Disturbance / Flooded Ground / Light Debris
    3: Moderate Damage / Roof Disturbance / Blue Tarp
    4: Severe Structural Collapse / Heavy Debris Field
    """
    _, height, width = rgb.shape
    r = rgb[0].astype(np.float32)
    g = rgb[1].astype(np.float32)
    b = rgb[2].astype(np.float32)

    # Valid non-black / non-nodata pixels
    valid = valid_mask & ((r + g + b) > 8)

    # 1. Luminance & Sobel edge gradient magnitude
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    gx = np.zeros_like(lum)
    gy = np.zeros_like(lum)

    gx[1:-1, 1:-1] = (
        lum[0:-2, 2:] + 2 * lum[1:-1, 2:] + lum[2:, 2:]
        - (lum[0:-2, :-2] + 2 * lum[1:-1, :-2] + lum[2:, :-2])
    ) / 8.0
    gy[1:-1, 1:-1] = (
        lum[2:, 0:-2] + 2 * lum[2:, 1:-1] + lum[2:, 2:]
        - (lum[:-2, 0:-2] + 2 * lum[:-2, 1:-1] + lum[:-2, 2:])
    ) / 8.0
    grad_mag = np.sqrt(gx**2 + gy**2)

    # 2. Local texture standard deviation & edge density
    mean_lum = uniform_filter_2d(lum, size=5)
    mean_lum_sq = uniform_filter_2d(lum**2, size=5)
    texture_std = np.sqrt(np.maximum(0.0, mean_lum_sq - mean_lum**2))
    edge_density = uniform_filter_2d(grad_mag, size=5)

    # Debris index: combination of spatial disorder and contrast fluctuations
    debris_index = (edge_density * texture_std) / 80.0

    # 3. Spectral signature: FEMA blue tarps on damaged roofs
    # High blue reflectance relative to red and green
    blue_ratio_r = b / np.maximum(r + 1.0, 1.0)
    blue_ratio_g = b / np.maximum(g + 1.0, 1.0)
    is_blue_tarp = (
        valid
        & (b > 95)
        & (blue_ratio_r > 1.30)
        & (blue_ratio_g > 1.12)
        & ((b - r) > 20)
    )

    # 4. Inundation / standing dark water
    is_inundated = (
        valid
        & (r < 55)
        & (g < 60)
        & (b < 70)
        & (grad_mag < 8.0)
        & (~is_blue_tarp)
    )

    # 5. Assign categorical damage classes
    classification = np.zeros((height, width), dtype=np.uint8)
    classification[valid] = 1  # 1 = Undamaged terrain / intact structure

    # Class 2: Minor surface disturbance / light scatter / standing puddle
    minor_mask = valid & (((debris_index >= 3.5) & (debris_index < 7.5)) | is_inundated)
    classification[minor_mask] = 2

    # Class 3: Moderate damage / structural disorder / blue tarp
    moderate_mask = valid & (((debris_index >= 7.5) & (debris_index < 14.0)) | is_blue_tarp)
    classification[moderate_mask] = 3

    # Class 4: Severe structural collapse / heavy debris field / high fragmentation
    severe_mask = valid & (debris_index >= 14.0)
    classification[severe_mask] = 4

    return classification


def calculate_statistics(classification: np.ndarray, bbox: list[float]) -> StormDamageStatistics:
    height, width = classification.shape
    min_lon, min_lat, max_lon, max_lat = bbox

    latitudes = np.linspace(max_lat, min_lat, height, endpoint=False)
    deg_lat_meters = 111_132.954 - 559.822 * np.cos(np.radians(2 * latitudes))
    deg_lon_meters = 111_412.84 * np.cos(np.radians(latitudes))
    dy = (max_lat - min_lat) / height * deg_lat_meters
    dx = (max_lon - min_lon) / width * deg_lon_meters
    pixel_area_sqm = np.maximum(dy * dx, 0)

    valid = classification > 0
    minor = classification == 2
    moderate = classification == 3
    severe = classification == 4
    damaged = classification >= 2

    def area_km2(mask: np.ndarray) -> float:
        return float(np.sum(np.sum(mask, axis=1) * pixel_area_sqm)) / 1_000_000.0

    analyzed_km2 = area_km2(valid)
    total_damaged_km2 = area_km2(damaged)
    severe_km2 = area_km2(severe)
    moderate_km2 = area_km2(moderate)
    minor_km2 = area_km2(minor)

    pct = (total_damaged_km2 / analyzed_km2 * 100.0) if analyzed_km2 > 0 else 0.0

    return StormDamageStatistics(
        total_analyzed_km2=round(analyzed_km2, 3),
        total_damaged_km2=round(total_damaged_km2, 3),
        severe_collapse_km2=round(severe_km2, 3),
        moderate_damage_km2=round(moderate_km2, 3),
        minor_flooded_km2=round(minor_km2, 3),
        damaged_percent=round(pct, 2),
        valid_pixels=int(np.count_nonzero(valid)),
        damaged_pixels=int(np.count_nonzero(damaged)),
    )


def encode_4band_cog(rgb: np.ndarray, classification: np.ndarray, bbox: list[float]) -> bytes:
    height, width = classification.shape
    with MemoryFile() as mem:
        with mem.open(
            driver="COG",
            width=width,
            height=height,
            count=4,
            dtype="uint8",
            crs="EPSG:4326",
            transform=from_bounds(*bbox, width, height),
            compress="DEFLATE",
            blocksize=512,
            overview_resampling="nearest",
        ) as dst:
            dst.write(rgb, indexes=[1, 2, 3])
            dst.write(classification, 4)
            dst.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.undefined,
            )
            dst.set_band_description(1, "NOAA Aerial Red")
            dst.set_band_description(2, "NOAA Aerial Green")
            dst.set_band_description(3, "NOAA Aerial Blue")
            dst.set_band_description(4, "damage_class")
            dst.update_tags(
                4,
                CLASS_0="No Data",
                CLASS_1="Undamaged Terrain",
                CLASS_2="Minor Disturbance / Flooded",
                CLASS_3="Moderate Damage / Blue Tarp",
                CLASS_4="Severe Structural Collapse / Debris",
            )
        return mem.read()


class NoaaDamageService:
    @staticmethod
    def _calculate_bbox(lat: float, lon: float, buffer_km: float) -> list[float]:
        lat_buf = buffer_km / 111.0
        lon_scale = max(abs(math.cos(math.radians(lat))), 0.01)
        lon_buf = min(buffer_km / (111.0 * lon_scale), 180.0)
        return [
            round(max(lon - lon_buf, -180.0), 6),
            round(max(lat - lat_buf, -90.0), 6),
            round(min(lon + lon_buf, 180.0), 6),
            round(min(lat + lat_buf, 90.0), 6),
        ]

    @classmethod
    def _read_noaa_mosaic(
        cls, keys: list[str], bbox: list[float], dimension: int
    ) -> tuple[np.ndarray, np.ndarray]:
        canvas = np.zeros((3, dimension, dimension), dtype=np.uint8)
        valid = np.zeros((dimension, dimension), dtype=bool)
        transform = from_bounds(*bbox, dimension, dimension)

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
                            crs="EPSG:4326",
                            transform=transform,
                            width=dimension,
                            height=dimension,
                            resampling=Resampling.bilinear,
                            add_alpha=not has_alpha,
                            src_nodata=None,
                        ) as vrt:
                            data = vrt.read([1, 2, 3, 4])
                except Exception as exc:
                    logger.warning("Failed reading NOAA tile %s: %s", key, exc)
                    continue

                mask = data[3] > 0
                if np.any(mask):
                    for b in range(3):
                        canvas[b, mask] = data[b, mask]
                    valid |= mask

        return canvas, valid

    @classmethod
    async def analyze(cls, req: AnalyzeNoaaDamageRequest) -> ComputedStormDamageResponse:
        # 1. Resolve bounding box
        if req.bbox:
            bbox = req.bbox
            center_lat = (bbox[1] + bbox[3]) / 2.0
            center_lon = (bbox[0] + bbox[2]) / 2.0
        elif req.latitude is not None and req.longitude is not None:
            center_lat, center_lon = req.latitude, req.longitude
            bbox = cls._calculate_bbox(center_lat, center_lon, req.buffer_km)
        else:
            raise ValueError("Provide either 'bbox' or both 'latitude' and 'longitude'.")

        # 2. Resolve flight
        flight = req.flight
        if not flight:
            flights = await storm_db.flights_in_bbox(bbox)
            event_flights = [f for f in flights if f["event"] == req.event]
            if not event_flights:
                raise ValueError(
                    f"No NOAA flights indexed for event {req.event} in this bounding box. "
                    "Run POST /api/v1/imagery/noaa/sync first."
                )
            # Pick flight with most tiles in area
            event_flights.sort(key=lambda x: x["tile_count"], reverse=True)
            flight = event_flights[0]["flight"]

        # 3. Fetch tile keys
        keys = await storm_db.tiles_for_bbox(req.event, flight, bbox, limit=25)
        if not keys:
            raise ValueError(f"No NOAA aerial tiles found for flight {flight} in this bounding box.")

        # 4. Warp & mosaic aerial RGB
        rgb, valid = await asyncio.to_thread(cls._read_noaa_mosaic, keys, bbox, req.dimension)
        if not np.any(valid):
            raise ValueError("NOAA aerial imagery tiles contain no valid raster data in this bounding box.")

        # 5. Run damage detection
        classification = detect_aerial_damage(rgb, valid)

        # 6. Area statistics
        stats = calculate_statistics(classification, bbox)

        # 7. Encode 4-band COG and save
        cog_bytes = encode_4band_cog(rgb, classification, bbox)
        unique_id = uuid.uuid4().hex[:8]
        event_num = abs(hash(f"{req.event}_{flight}")) % 10_000_000
        cog_key = f"noaa_damage/{req.event}/{flight}_{unique_id}.tif"
        cog_url = f"/api/v1/cog/assets/{event_num}/{unique_id}.tif"
        stored_url = await cog_storage.put(cog_key, cog_bytes, local_url=cog_url)

        flight_date = noaaEri.flight_date(flight) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        min_lon, min_lat, max_lon, max_lat = bbox

        geojson = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [min_lon, min_lat],
                        [max_lon, min_lat],
                        [max_lon, max_lat],
                        [min_lon, max_lat],
                        [min_lon, min_lat],
                    ]
                ],
            },
            "properties": {
                "event": req.event,
                "flight": flight,
                "platform": "NOAA ERI Aerial (0.3m RGB)",
            },
        }

        symbology = [
            StormSymbologyClass(
                class_value=0, label="No Data", color="#000000", opacity=0.0
            ),
            StormSymbologyClass(
                class_value=1, label="Undamaged Terrain", color="#10b981", opacity=0.35
            ),
            StormSymbologyClass(
                class_value=2,
                label="Minor Disturbance / Flooded",
                color="#eab308",
                opacity=0.80,
            ),
            StormSymbologyClass(
                class_value=3,
                label="Moderate Damage / Blue Tarp",
                color="#f97316",
                opacity=0.85,
            ),
            StormSymbologyClass(
                class_value=4,
                label="Severe Structural Collapse",
                color="#dc2626",
                opacity=0.90,
            ),
        ]

        return ComputedStormDamageResponse(
            status="success",
            event_id=event_num,
            incident_name=f"{req.event.replace('_', ' ')} Aerial (Flight {flight})",
            platform="NOAA ERI Aerial (0.3m RGB)",
            pre_storm_date="Pre-Storm Baseline",
            post_storm_date=flight_date,
            statistics=stats,
            cog_url=stored_url,
            symbology=symbology,
            target_coordinates={"latitude": center_lat, "longitude": center_lon},
            bbox=bbox,
            geojson=geojson,
        )

