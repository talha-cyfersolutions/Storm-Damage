from datetime import date
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class StormDamageRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90, description="Target Latitude")
    longitude: float = Field(..., ge=-180, le=180, description="Target Longitude")
    storm_start_date: date = Field(..., description="Storm start date")
    storm_end_date: date = Field(..., description="Storm conclusion date")
    buffer_km: float = Field(default=10.0, ge=1.0, le=50.0, description="Analysis buffer radius in km")
    prefer_sensor: Literal["auto", "sar", "optical"] = Field(
        default="auto",
        description="'auto' selects earliest clear scene, preferring SAR during cloudy post-storm periods",
    )
    incident_name: str | None = None
    model_config = ConfigDict(extra="forbid")


class StormDamageStatistics(BaseModel):
    total_analyzed_km2: float
    total_damaged_km2: float
    severe_collapse_km2: float
    moderate_damage_km2: float
    minor_flooded_km2: float
    damaged_percent: float
    valid_pixels: int
    damaged_pixels: int


class StormDamageAsset(BaseModel):
    sensor_type: Literal["sar", "optical"]
    platform: str
    scene_id: str
    acquisition_date: str
    cloud_cover_percent: float | None = None
    primary_visual_url: str
    all_bands: dict[str, str] = Field(default_factory=dict)


class StormSymbologyClass(BaseModel):
    value: int
    label: str
    color: str
    opacity: float = Field(ge=0, le=1)


class ComputedStormDamageResponse(BaseModel):
    status: str
    event_id: int
    incident_name: str
    platform: str
    pre_storm_date: str
    post_storm_date: str
    statistics: StormDamageStatistics
    cog_url: str
    symbology: list[StormSymbologyClass]
    target_coordinates: dict[str, float]
    bbox: list[float]
    geojson: dict[str, Any]
    model_config = ConfigDict(extra="forbid")