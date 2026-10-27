"""Active NWS alerts (api.weather.gov)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from config import Config
from models.stormEventModel import StormEvent

logger = logging.getLogger("geoapi.storms.nws")

DEFAULT_EVENTS = (
    "Tornado Warning",
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
    "Hurricane Warning",
    "Tropical Storm Warning",
    "Storm Surge Warning",
)


def _centroid(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    """Mean of the outer ring vertices. Good enough to locate an alert polygon."""
    if not geometry:
        return None
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if gtype == "Polygon":
        ring = coords[0]
    elif gtype == "MultiPolygon":
        ring = coords[0][0]
    else:
        return None
    if not ring:
        return None
    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    return lat, lon


def parse_alerts(payload: dict[str, Any]) -> list[StormEvent]:
    events: list[StormEvent] = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        center = _centroid(feature.get("geometry"))
        if center is None:
            continue  # alerts without polygons (zone-based) cannot be mapped here
        events.append(
            StormEvent(
                id=f"nws:{props.get('id') or feature.get('id')}",
                source="nws",
                event_type=props.get("event", "Alert"),
                rating=props.get("severity"),
                begin_date=(props.get("onset") or props.get("sent") or "")[:10],
                end_date=(props.get("ends") or props.get("expires") or "")[:10] or None,
                state=None,
                location=props.get("areaDesc"),
                begin_lat=center[0],
                begin_lon=center[1],
                narrative=(props.get("headline") or "")[:2000] or None,
            )
        )
    return events


async def fetch_active_alerts(
    event_names: tuple[str, ...] = DEFAULT_EVENTS,
    areas: tuple[str, ...] = ("DC", "MD", "VA"),
) -> list[StormEvent]:
    headers = {"User-Agent": Config.NWS_USER_AGENT, "Accept": "application/geo+json"}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # NWS accepts a comma separated list for `event`
        params = {"event": ",".join(event_names), "area": ",".join(areas), "status": "actual"}
        async with session.get(f"{Config.NWS_API_URL}/alerts/active", params=params) as resp:
            if resp.status != 200:
                logger.warning("NWS alerts returned HTTP %s", resp.status)
                raise RuntimeError(f"NWS alerts API returned HTTP {resp.status}")
            payload = await resp.json(content_type=None)
    return parse_alerts(payload)
