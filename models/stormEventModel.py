from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class StormEvent(BaseModel):
    """A historical or active US storm event normalised from NCEI / NWS."""

    id: str = Field(..., description="Stable id, e.g. 'ncei:1155977' or 'nws:urn:oid:...'")
    source: Literal["ncei", "nws"]
    event_type: str
    rating: str | None = Field(default=None, description="EF scale for tornadoes, or alert severity")
    begin_date: str = Field(..., description="ISO date (UTC for NWS, local for NCEI)")
    end_date: str | None = None
    state: str | None = None
    location: str | None = None
    begin_lat: float | None = None
    begin_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    path_length_miles: float | None = None
    path_width_yards: float | None = None
    injuries: int = 0
    deaths: int = 0
    damage_property: str | None = None
    narrative: str | None = None


class StormEventList(BaseModel):
    count: int
    events: list[StormEvent]


class StormSyncResult(BaseModel):
    year: int
    imported: int


class SyncJob(BaseModel):
    id: str
    kind: str
    target: str
    status: Literal["running", "done", "failed"]
    detail: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ImageryItem(BaseModel):
    provider: Literal["noaa-eri"]
    event: str
    flight: str
    date: str
    tile_count: int
    bbox: list[float]
    tiles_url: str = Field(..., description="XYZ template, usable as a Mapbox raster source")
    resolution_m: float | None = None


class ImagerySearchResponse(BaseModel):
    count: int
    items: list[ImageryItem]
    message: str | None = None


class NoaaSyncResult(BaseModel):
    prefix: str
    flights: int
    tiles: int


class AnalyzeStormRequest(BaseModel):
    buffer_km: float = Field(default=10.0, ge=1.0, le=50.0)
    prefer_sensor: Literal["auto", "sar", "optical"] = "auto"
    days_after: int = Field(default=1, ge=0, le=30, description="Days after the event end to start the post-scene search")
    model_config = ConfigDict(extra="forbid")


class AnalyzeNoaaDamageRequest(BaseModel):
    event: str = Field(..., description="NOAA ERI event identifier, e.g. 2024_Hurricane_Francine")
    flight: str | None = Field(default=None, description="NOAA flight id, e.g. 20240913a_RGB. Auto-selected if omitted")
    bbox: list[float] | None = Field(default=None, description="minLon,minLat,maxLon,maxLat")
    latitude: float | None = None
    longitude: float | None = None
    buffer_km: float = Field(default=2.0, ge=0.5, le=20.0)
    dimension: int = Field(default=512, ge=256, le=1024)
    model_config = ConfigDict(extra="forbid")


class StormAnalysisResponse(BaseModel):
    event: StormEvent
    analysis: dict[str, Any] | None = None
    imagery: ImagerySearchResponse
    message: str | None = None

