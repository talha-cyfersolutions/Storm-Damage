"""Import of NOAA NCEI Storm Events history (US) into the local SQLite store."""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import logging
import re

import aiohttp

from config import Config
from models.stormEventModel import StormEvent
from services.storms.db import storm_db

logger = logging.getLogger("geoapi.storms.ncei")

# Event types that cause mappable damage and have a start coordinate.
DAMAGING_TYPES = {
    "Tornado",
    "Hurricane",
    "Hurricane (Typhoon)",
    "Tropical Storm",
    "Storm Surge/Tide",
    "Flash Flood",
    "Flood",
    "Thunderstorm Wind",
    "High Wind",
    "Hail",
}


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except ValueError:
        return 0


def _iso_date(yearmonth: str, day: str) -> str:
    return f"{yearmonth[:4]}-{yearmonth[4:6]}-{int(day):02d}"


def parse_storm_events_csv(raw_csv: bytes, event_types: set[str] | None = None) -> list[StormEvent]:
    """Parse a StormEvents_details CSV (already gunzipped) into StormEvent objects."""
    wanted = event_types or DAMAGING_TYPES
    reader = csv.DictReader(io.StringIO(raw_csv.decode("utf-8", errors="replace")))
    events: list[StormEvent] = []
    for row in reader:
        etype = row.get("EVENT_TYPE", "")
        if etype not in wanted:
            continue
        lat, lon = _float(row.get("BEGIN_LAT")), _float(row.get("BEGIN_LON"))
        if lat is None or lon is None:
            continue
        narrative = (row.get("EVENT_NARRATIVE") or "").strip() or None
        events.append(
            StormEvent(
                id=f"ncei:{row['EVENT_ID']}",
                source="ncei",
                event_type=etype,
                rating=(row.get("TOR_F_SCALE") or None) if etype == "Tornado" else None,
                begin_date=_iso_date(row["BEGIN_YEARMONTH"], row["BEGIN_DAY"]),
                end_date=_iso_date(row["END_YEARMONTH"], row["END_DAY"]),
                state=(row.get("STATE") or "").title() or None,
                location=(row.get("BEGIN_LOCATION") or row.get("CZ_NAME") or None),
                begin_lat=lat,
                begin_lon=lon,
                end_lat=_float(row.get("END_LAT")),
                end_lon=_float(row.get("END_LON")),
                path_length_miles=_float(row.get("TOR_LENGTH")),
                path_width_yards=_float(row.get("TOR_WIDTH")),
                injuries=_int(row.get("INJURIES_DIRECT")) + _int(row.get("INJURIES_INDIRECT")),
                deaths=_int(row.get("DEATHS_DIRECT")) + _int(row.get("DEATHS_INDIRECT")),
                damage_property=row.get("DAMAGE_PROPERTY") or None,
                narrative=narrative[:2000] if narrative else None,
            )
        )
    return events


async def _find_year_filename(session: aiohttp.ClientSession, year: int) -> str:
    async with session.get(Config.NCEI_STORM_EVENTS_URL) as resp:
        resp.raise_for_status()
        listing = await resp.text()
    match = re.search(rf"StormEvents_details-ftp_v1\.0_d{year}_c\d+\.csv\.gz", listing)
    if not match:
        raise ValueError(f"NCEI has no Storm Events file for {year}")
    return match.group(0)


async def list_available_years() -> list[int]:
    """List all years available in NCEI Storm Events repository."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(Config.NCEI_STORM_EVENTS_URL) as resp:
            resp.raise_for_status()
            listing = await resp.text()
    matches = re.findall(r"StormEvents_details-ftp_v1\.0_d(\d{4})_c\d+\.csv\.gz", listing)
    return sorted({int(y) for y in matches}, reverse=True)


async def sync_year(year: int) -> int:
    """Download and import one year of NCEI Storm Events. Returns rows imported."""
    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        name = await _find_year_filename(session, year)
        logger.info("Downloading NCEI %s", name)
        async with session.get(Config.NCEI_STORM_EVENTS_URL + name) as resp:
            resp.raise_for_status()
            payload = await resp.read()
    raw_csv = await asyncio.to_thread(gzip.decompress, payload)
    events = await asyncio.to_thread(parse_storm_events_csv, raw_csv)
    imported = await storm_db.upsert_events(events)
    logger.info("Imported %s NCEI events for %s", imported, year)
    return imported


async def sync_years(years: list[int]) -> tuple[int, int]:
    """Download and import multiple years of NCEI Storm Events. Returns (years_count, total_events)."""
    total = 0
    for y in sorted(years):
        try:
            imported = await sync_year(y)
            total += imported
        except Exception as exc:
            logger.warning("Failed importing NCEI year %s: %s", y, exc)
    return len(years), total

