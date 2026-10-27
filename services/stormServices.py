from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from math import cos, radians
from typing import Any
from urllib.parse import quote
import aiohttp
import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

from config import Config
from models import (
    ComputedStormDamageResponse,
    StormDamageAsset,
    StormDamageRequest,
    StormDamageStatistics,
    StormSymbologyClass,
)
from services.cogService import CogService

logger = logging.getLogger("geoapi.storm_service")
cog_storage = CogService()


class StormDamageService:
    DAMAGE_COLORS = {
        0: "#000000",  # No data / transparent
        1: "#10b981",  # Intact / unaffected surface
        2: "#eab308",  # Minor inundation / surface disturbance
        3: "#f97316",  # Moderate structure damage / heavy debris
        4: "#dc2626",  # Severe structural collapse / standing flood
    }

    def __init__(self, request: StormDamageRequest) -> None:
        self.req = request
        self.bbox = self._calculate_bbox(request.latitude, request.longitude, request.buffer_km)
        self.provider = Config.STAC_PROVIDER.lower()
        self.stac_base = (
            Config.EARTH_SEARCH_URL if self.provider == "aws" else Config.PLANETARY_COMPUTER_STAC
        )

    @staticmethod
    def _calculate_bbox(lat: float, lon: float, buffer_km: float) -> list[float]:
        lat_buf = buffer_km / 111.0
        lon_scale = max(abs(cos(radians(lat))), 0.01)
        lon_buf = min(buffer_km / (111.0 * lon_scale), 180.0)
        return [
            max(lon - lon_buf, -180.0),
            max(lat - lat_buf, -90.0),
            min(lon + lon_buf, 180.0),
            min(lat + lat_buf, 90.0),
        ]

    async def _query_stac(
        self,
        session: aiohttp.ClientSession,
        collections: list[str],
        start_date: str,
        end_date: str,
        cloud_cover_limit: float | None = None,
        sort_ascending: bool = True,
    ) -> list[dict[str, Any]]:
        query_payload: dict[str, Any] = {}
        if cloud_cover_limit is not None:
            query_payload["eo:cloud_cover"] = {"lte": cloud_cover_limit}

        payload = {
            "collections": collections,
            "bbox": self.bbox,
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "query": query_payload,
            "sortby": [{"field": "properties.datetime", "direction": "asc" if sort_ascending else "desc"}],
            "limit": 10,
        }

        try:
            async with session.post(f"{self.stac_base}/search", json=payload) as resp:
                if resp.status != 200:
                    logger.warning("STAC query to %s returned HTTP %s for collections %s", self.stac_base, resp.status, collections)
                    return []
                data = await resp.json()
                return data.get("features", [])
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("STAC query network error on %s: %s", self.stac_base, exc)
            return []

    @staticmethod
    async def _sign_href_if_needed(session: aiohttp.ClientSession, href: str) -> str:
        """Signs Planetary Computer Azure Blob Storage hrefs with a SAS token if applicable."""
        if "blob.core.windows.net" not in href or "sig=" in href:
            return href

        sign_url = f"https://planetarycomputer.microsoft.com/api/sas/v1/sign?href={quote(href, safe='')}"
        headers = {}
        if Config.PLANETARY_COMPUTER_API_KEY:
            headers["Ocp-Apim-Subscription-Key"] = Config.PLANETARY_COMPUTER_API_KEY

        try:
            async with session.get(sign_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    signed = data.get("href")
                    if signed:
                        return signed
                logger.warning("Planetary Computer SAS signing returned HTTP %s for %s", resp.status, href)
        except Exception as exc:
            logger.error("Failed to sign asset href %s: %s", href, exc)

        return href

    @classmethod
    def _read_windowed_band(
        cls, href: str, bbox: list[float], dimension: int = 512
    ) -> tuple[np.ndarray, np.ndarray]:
        """Reads a remote COG asset window without downloading the full multi-GB scene."""
        env_vars = {
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
            "GDAL_HTTP_TIMEOUT": "60",
            "GDAL_HTTP_MAX_RETRY": "5",
            "GDAL_HTTP_RETRY_DELAY": "3",
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
            "GDAL_HTTP_MULTIPLEX": "YES",
            "VSI_CACHE": "TRUE",
            "VSI_CACHE_SIZE": "50000000",
            "AWS_NO_SIGN_REQUEST": "YES",
            "AWS_REGION": "eu-central-1",
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with rasterio.Env(**env_vars), rasterio.open(href) as src:
                    vrt_kwargs: dict[str, Any] = {
                        "crs": "EPSG:4326",
                        "transform": from_bounds(*bbox, dimension, dimension),
                        "width": dimension,
                        "height": dimension,
                        "resampling": Resampling.bilinear,
                    }
                    # If raster has Ground Control Points (e.g. Sentinel-1 GRD on AWS), supply GCP transform
                    if src.crs is None and src.gcps and src.gcps[0]:
                        gcps, gcp_crs = src.gcps
                        vrt_kwargs["src_crs"] = gcp_crs or "EPSG:4326"
                        vrt_kwargs["src_transform"] = rasterio.transform.from_gcps(gcps)

                    with WarpedVRT(src, **vrt_kwargs) as vrt:
                        raw = vrt.read(1, masked=True)
                        data = raw.filled(0).astype(np.float32)
                        mask = ~np.ma.getmaskarray(raw) & (data > 0)
                        return data, mask
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "Attempt %s/3 failed reading windowed band from %s: %s",
                    attempt + 1,
                    href,
                    exc,
                )
                time.sleep(1.0)
        if last_err:
            raise last_err

    @classmethod
    def _classify_damage(
        cls,
        pre_vv: np.ndarray,
        post_vv: np.ndarray,
        valid: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Calculates SAR intensity drop (dB) between pre- and post-storm passes.
        Buildings act as double-bounce corner reflectors (high backscatter).
        Structural collapse, debris fields, and standing water lead to signal drop.
        """
        eps = 1e-6
        pre_vv_db = 10.0 * np.log10(np.maximum(pre_vv, eps))
        post_vv_db = 10.0 * np.log10(np.maximum(post_vv, eps))
        diff_vv = post_vv_db - pre_vv_db

        classification = np.zeros_like(pre_vv, dtype=np.uint8)
        classification[valid] = 1

        # Calibrated thresholds for C-band SAR change detection
        minor_change = valid & (diff_vv <= -2.0) & (diff_vv > -3.5)
        moderate_damage = valid & (diff_vv <= -3.5) & (diff_vv > -5.5)
        severe_collapse = valid & (diff_vv <= -5.5)

        classification[minor_change] = 2
        classification[moderate_damage] = 3
        classification[severe_collapse] = 4

        # False-color RGB Composite: Red=Post-VV, Green=Pre-VV, Blue=Difference
        valid_post = post_vv[valid] if np.any(valid) else np.array([0.0, 1.0])
        valid_pre = pre_vv[valid] if np.any(valid) else np.array([0.0, 1.0])

        p2_post, p98_post = np.percentile(valid_post, 2), np.percentile(valid_post, 98)
        p2_pre, p98_pre = np.percentile(valid_pre, 2), np.percentile(valid_pre, 98)

        norm_post = np.clip((post_vv - p2_post) / max(p98_post - p2_post, 1e-5), 0, 1)
        norm_pre = np.clip((pre_vv - p2_pre) / max(p98_pre - p2_pre, 1e-5), 0, 1)
        norm_diff = np.clip((diff_vv + 6.0) / 12.0, 0, 1)

        r = (norm_post * 255).astype(np.uint8)
        g = (norm_pre * 255).astype(np.uint8)
        b = (norm_diff * 255).astype(np.uint8)

        rgb = np.stack([r, g, b], axis=0)
        return classification, rgb

    @classmethod
    def _calculate_statistics(
        cls,
        classification: np.ndarray,
        bbox: list[float],
    ) -> StormDamageStatistics:
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

    @classmethod
    def _encode_cog(
        cls, rgb: np.ndarray, classification: np.ndarray, bbox: list[float]
    ) -> bytes:
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
                dst.set_band_description(1, "SAR Post-Storm VV")
                dst.set_band_description(2, "SAR Pre-Storm VV")
                dst.set_band_description(3, "SAR Relative Change")
                dst.set_band_description(4, "damage_class")
                dst.update_tags(
                    4,
                    CLASS_0="No Data",
                    CLASS_1="Undamaged",
                    CLASS_2="Minor Change / Flooded",
                    CLASS_3="Moderate Damage",
                    CLASS_4="Severe Structural Collapse",
                )
            return mem.read()

    async def execute_analysis(self) -> ComputedStormDamageResponse:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Keep both scene searches within two days of the requested storm dates.
        pre_storm_start = (self.req.storm_start_date - timedelta(days=2)).isoformat()
        pre_storm_end = (self.req.storm_start_date - timedelta(days=1)).isoformat()
        post_storm_start = self.req.storm_end_date.isoformat()
        post_storm_end = min(
            (self.req.storm_end_date + timedelta(days=2)).isoformat(),
            today_str,
        )

        sar_collection = ["sentinel-1-grd"] if self.provider == "aws" else ["sentinel-1-rtc"]
        optical_collections = ["sentinel-2-l2a", "landsat-c2-l2"]

        timeout = aiohttp.ClientTimeout(total=Config.STAC_REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. Discover earliest post-storm scene (try optical if not forced SAR)
            optical_post = []
            if self.req.prefer_sensor in ("auto", "optical", "landsat"):
                optical_query = (
                    ["landsat-c2-l2"]
                    if self.req.prefer_sensor == "landsat"
                    else optical_collections
                )
                optical_post = await self._query_stac(
                    session=session,
                    collections=optical_query,
                    start_date=post_storm_start,
                    end_date=post_storm_end,
                    cloud_cover_limit=25.0,
                    sort_ascending=True,
                )

            selected_post_item = None
            sensor_type = "optical"

            if not optical_post or self.req.prefer_sensor == "sar":
                sar_post = await self._query_stac(
                    session=session,
                    collections=sar_collection,
                    start_date=post_storm_start,
                    end_date=post_storm_end,
                    cloud_cover_limit=None,
                    sort_ascending=True,
                )
                if sar_post:
                    selected_post_item = sar_post[0]
                    sensor_type = "sar"
            else:
                selected_post_item = optical_post[0]

            if not selected_post_item:
                raise ValueError(
                    f"No post-storm satellite scenes found in the target area between {post_storm_start} and {post_storm_end}."
                )

            # 2. Query a baseline within two days before the requested start.
            # Optical baselines may come from either Sentinel-2 or Landsat;
            # this avoids failing just because one provider has a revisit gap.
            if sensor_type == "sar":
                pre_collection = sar_collection
            elif self.req.prefer_sensor == "landsat":
                pre_collection = ["landsat-c2-l2"]
            else:
                pre_collection = optical_collections
            pre_items = await self._query_stac(
                session=session,
                collections=pre_collection,
                start_date=pre_storm_start,
                end_date=pre_storm_end,
                cloud_cover_limit=30.0 if sensor_type == "optical" else None,
                sort_ascending=False,
            )
            if not pre_items and sensor_type == "optical":
                logger.info("No clear optical baseline; retrying the same two-day window with relaxed cloud filtering.")
                pre_items = await self._query_stac(
                    session=session,
                    collections=pre_collection,
                    start_date=pre_storm_start,
                    end_date=pre_storm_end,
                    cloud_cover_limit=None,
                    sort_ascending=False,
                )

            # Fallback to SAR if optical baseline is obscured by clouds
            if not pre_items and sensor_type == "optical" and self.req.prefer_sensor == "auto":
                logger.info("Optical baseline too cloudy; falling back to all-weather Sentinel-1 SAR.")
                sar_post = await self._query_stac(
                    session=session,
                    collections=sar_collection,
                    start_date=post_storm_start,
                    end_date=post_storm_end,
                    cloud_cover_limit=None,
                    sort_ascending=True,
                )
                if sar_post:
                    selected_post_item = sar_post[0]
                    sensor_type = "sar"
                    pre_items = await self._query_stac(
                        session=session,
                        collections=sar_collection,
                        start_date=pre_storm_start,
                        end_date=pre_storm_end,
                        cloud_cover_limit=None,
                        sort_ascending=False,
                    )

            if not pre_items:
                raise ValueError(
                    "No pre-storm baseline scene was found within the allowed two-day window "
                    f"({pre_storm_start} through {pre_storm_end}). "
                    "Try a nearby storm date, a smaller area, or Sentinel-1 SAR."
                )

            pre_item = pre_items[0]

            # 3. Extract asset links (VV amplitude for SAR, Visual for optical)
            post_assets = selected_post_item.get("assets", {})
            pre_assets = pre_item.get("assets", {})

            def select_asset(assets: dict[str, Any], collection: str) -> str | None:
                if collection.startswith("landsat"):
                    # Landsat C2 L2 red reflectance is broadly available and
                    # provides a consistent full-bbox optical intensity signal.
                    return (
                        assets.get("red", {}).get("href")
                        or assets.get("nir08", {}).get("href")
                        or assets.get("visual", {}).get("href")
                    )
                return (
                    assets.get("vv", {}).get("href")
                    or assets.get("visual", {}).get("href")
                    or assets.get("rendered_preview", {}).get("href")
                )

            post_raw_url = select_asset(post_assets, selected_post_item.get("collection", ""))
            pre_raw_url = select_asset(pre_assets, pre_item.get("collection", ""))

            if not post_raw_url or not pre_raw_url:
                raise ValueError("Required spectral/radar bands are missing from STAC assets.")

            # Sign Planetary Computer SAS tokens if needed (AWS S3 needs no signing)
            pre_url, post_url = await asyncio.gather(
                self._sign_href_if_needed(session, pre_raw_url),
                self._sign_href_if_needed(session, post_raw_url),
            )

        # 4. Stream raster windows concurrently
        loop = asyncio.get_running_loop()
        try:
            pre_task = loop.run_in_executor(
                None, self._read_windowed_band, pre_url, self.bbox, 512
            )
            post_task = loop.run_in_executor(
                None, self._read_windowed_band, post_url, self.bbox, 512
            )
            (pre_data, pre_mask), (post_data, post_mask) = await asyncio.gather(pre_task, post_task)
        except Exception as exc:
            if sensor_type == "optical" and self.req.prefer_sensor == "auto":
                logger.warning("Optical scene read failed (%s); falling back to all-weather Sentinel-1 SAR.", exc)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    sar_post = await self._query_stac(
                        session=session,
                        collections=sar_collection,
                        start_date=post_storm_start,
                        end_date=post_storm_end,
                        cloud_cover_limit=None,
                        sort_ascending=True,
                    )
                    pre_sar = await self._query_stac(
                        session=session,
                        collections=sar_collection,
                        start_date=pre_storm_start,
                        end_date=pre_storm_end,
                        cloud_cover_limit=None,
                        sort_ascending=False,
                    )
                    if not sar_post or not pre_sar:
                        raise ValueError(f"Optical scene read failed ({exc}) and no fallback Sentinel-1 SAR scenes found.") from exc
                    selected_post_item = sar_post[0]
                    pre_item = pre_sar[0]
                    sensor_type = "sar"
                    post_raw_url = selected_post_item.get("assets", {}).get("vv", {}).get("href") or selected_post_item.get("assets", {}).get("visual", {}).get("href")
                    pre_raw_url = pre_item.get("assets", {}).get("vv", {}).get("href") or pre_item.get("assets", {}).get("visual", {}).get("href")
                    pre_url, post_url = await asyncio.gather(
                        self._sign_href_if_needed(session, pre_raw_url),
                        self._sign_href_if_needed(session, post_raw_url),
                    )
                    pre_task = loop.run_in_executor(None, self._read_windowed_band, pre_url, self.bbox, 512)
                    post_task = loop.run_in_executor(None, self._read_windowed_band, post_url, self.bbox, 512)
                    (pre_data, pre_mask), (post_data, post_mask) = await asyncio.gather(pre_task, post_task)
            else:
                raise

        valid = pre_mask & post_mask

        # 5. Classify damage & compute statistics
        classification, rgb = self._classify_damage(pre_data, post_data, valid)
        stats = self._calculate_statistics(classification, self.bbox)

        # 6. Encode 4-band Cloud-Optimized GeoTIFF and store
        cog_bytes = await loop.run_in_executor(
            None, self._encode_cog, rgb, classification, self.bbox
        )

        post_date_str = selected_post_item.get("properties", {}).get("datetime", "")[:10]
        pre_date_str = pre_item.get("properties", {}).get("datetime", "")[:10]
        event_id = int(datetime.now(timezone.utc).strftime("%y%m%d%H%M%S"))

        storage_key = f"storm-cogs/storm_{event_id}_{post_date_str}.tif"
        local_url = f"/api/v1/cog/assets/{event_id}/{post_date_str}.tif"
        stored_cog_url = await cog_storage.put(storage_key, cog_bytes, local_url)

        polygon_coords = [[
            [self.bbox[0], self.bbox[1]],
            [self.bbox[2], self.bbox[1]],
            [self.bbox[2], self.bbox[3]],
            [self.bbox[0], self.bbox[3]],
            [self.bbox[0], self.bbox[1]],
        ]]

        symbology = [
            StormSymbologyClass(value=0, label="No Data", color=self.DAMAGE_COLORS[0], opacity=0),
            StormSymbologyClass(value=1, label="Undamaged", color=self.DAMAGE_COLORS[1], opacity=0.3),
            StormSymbologyClass(value=2, label="Minor Inundation / Change", color=self.DAMAGE_COLORS[2], opacity=0.85),
            StormSymbologyClass(value=3, label="Moderate Damage", color=self.DAMAGE_COLORS[3], opacity=0.85),
            StormSymbologyClass(value=4, label="Severe Structural Collapse", color=self.DAMAGE_COLORS[4], opacity=0.9),
        ]

        return ComputedStormDamageResponse(
            status="success",
            event_id=event_id,
            incident_name=self.req.incident_name or f"Storm Event ({self.req.latitude:.2f}, {self.req.longitude:.2f})",
            platform=selected_post_item.get("collection", "sentinel-1-grd"),
            pre_storm_date=pre_date_str,
            post_storm_date=post_date_str,
            statistics=stats,
            cog_url=stored_cog_url,
            symbology=symbology,
            target_coordinates={"latitude": self.req.latitude, "longitude": self.req.longitude},
            bbox=self.bbox,
            geojson={"type": "Polygon", "coordinates": polygon_coords},
        )
