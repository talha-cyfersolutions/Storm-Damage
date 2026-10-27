"""SQLite persistence for storm history and the NOAA ERI tile index."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from config import Config
from models.stormEventModel import StormEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS storm_events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    rating TEXT,
    begin_date TEXT NOT NULL,
    end_date TEXT,
    state TEXT,
    location TEXT,
    begin_lat REAL, begin_lon REAL, end_lat REAL, end_lon REAL,
    path_length_miles REAL, path_width_yards REAL,
    injuries INTEGER DEFAULT 0, deaths INTEGER DEFAULT 0,
    damage_property TEXT,
    narrative TEXT
);
CREATE INDEX IF NOT EXISTS idx_storm_date ON storm_events(begin_date);
CREATE INDEX IF NOT EXISTS idx_storm_type ON storm_events(event_type);
CREATE INDEX IF NOT EXISTS idx_storm_geo ON storm_events(begin_lat, begin_lon);

CREATE TABLE IF NOT EXISTS noaa_tiles (
    event TEXT NOT NULL,
    flight TEXT NOT NULL,
    key TEXT PRIMARY KEY,
    min_lon REAL NOT NULL, min_lat REAL NOT NULL,
    max_lon REAL NOT NULL, max_lat REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tiles_flight ON noaa_tiles(event, flight);
CREATE INDEX IF NOT EXISTS idx_tiles_geo ON noaa_tiles(min_lon, max_lon, min_lat, max_lat);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_EVENT_COLS = (
    "id, source, event_type, rating, begin_date, end_date, state, location, "
    "begin_lat, begin_lon, end_lat, end_lon, path_length_miles, path_width_yards, "
    "injuries, deaths, damage_property, narrative"
)


class StormDB:
    """Small thread-safe SQLite wrapper. Calls are pushed to threads from async code."""

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or Config.STORM_DB_PATH)
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        if not self._ready:
            conn.executescript(_SCHEMA)
            self._ready = True
        return conn

    def _run(self, fn, *args: Any):
        with self._lock:
            conn = self._connect()
            try:
                result = fn(conn, *args)
                conn.commit()
                return result
            finally:
                conn.close()

    async def run(self, fn, *args: Any):
        return await asyncio.to_thread(self._run, fn, *args)

    # -- storm events ---------------------------------------------------
    @staticmethod
    def _upsert_events(conn: sqlite3.Connection, events: list[StormEvent]) -> int:
        rows = [
            (
                e.id, e.source, e.event_type, e.rating, e.begin_date, e.end_date, e.state,
                e.location, e.begin_lat, e.begin_lon, e.end_lat, e.end_lon,
                e.path_length_miles, e.path_width_yards, e.injuries, e.deaths,
                e.damage_property, e.narrative,
            )
            for e in events
        ]
        conn.executemany(
            f"INSERT OR REPLACE INTO storm_events ({_EVENT_COLS}) VALUES ({','.join('?' * 18)})",
            rows,
        )
        return len(rows)

    async def upsert_events(self, events: list[StormEvent]) -> int:
        return await self.run(self._upsert_events, events)

    @staticmethod
    def _query_events(
        conn: sqlite3.Connection,
        bbox: list[float] | None,
        start: str | None,
        end: str | None,
        event_type: str | None,
        min_rating: int | None,
        states: list[str] | None,
        limit: int,
    ) -> list[StormEvent]:
        where, params = ["begin_lat IS NOT NULL", "begin_lon IS NOT NULL"], []
        if bbox:
            where += ["begin_lon BETWEEN ? AND ?", "begin_lat BETWEEN ? AND ?"]
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if start:
            where.append("begin_date >= ?")
            params.append(start)
        if end:
            where.append("begin_date <= ?")
            params.append(end)
        if event_type:
            where.append("lower(event_type) = lower(?)")
            params.append(event_type)
        if min_rating is not None:
            # rating looks like 'EF3'; non-EF rows are excluded when a minimum is requested
            where.append("rating LIKE 'EF%' AND CAST(substr(rating, 3) AS INTEGER) >= ?")
            params.append(min_rating)
        if states:
            placeholders = ",".join("?" for _ in states)
            where.append(f"upper(state) IN ({placeholders})")
            params.extend(state.upper() for state in states)
        sql = (
            f"SELECT {_EVENT_COLS} FROM storm_events WHERE {' AND '.join(where)} "
            "ORDER BY begin_date DESC LIMIT ?"
        )
        params.append(limit)
        return [StormEvent(**dict(r)) for r in conn.execute(sql, params).fetchall()]

    async def query_events(
        self,
        bbox: list[float] | None = None,
        start: str | None = None,
        end: str | None = None,
        event_type: str | None = None,
        min_rating: int | None = None,
        states: list[str] | None = None,
        limit: int = 200,
    ) -> list[StormEvent]:
        return await self.run(self._query_events, bbox, start, end, event_type, min_rating, states, limit)

    @staticmethod
    def _get_event(conn: sqlite3.Connection, event_id: str) -> StormEvent | None:
        row = conn.execute(
            f"SELECT {_EVENT_COLS} FROM storm_events WHERE id = ?", (event_id,)
        ).fetchone()
        return StormEvent(**dict(row)) if row else None

    async def get_event(self, event_id: str) -> StormEvent | None:
        return await self.run(self._get_event, event_id)

    # -- NOAA ERI tile index -------------------------------------------
    @staticmethod
    def _replace_tiles(conn: sqlite3.Connection, prefix: str, rows: Iterable[tuple]) -> int:
        conn.execute("DELETE FROM noaa_tiles WHERE key LIKE ?", (prefix + "%",))
        rows = list(rows)
        conn.executemany("INSERT OR REPLACE INTO noaa_tiles VALUES (?,?,?,?,?,?,?)", rows)
        return len(rows)

    async def replace_tiles(self, prefix: str, rows: Iterable[tuple]) -> int:
        return await self.run(self._replace_tiles, prefix, rows)

    @staticmethod
    def _flights_in_bbox(conn: sqlite3.Connection, bbox: list[float]) -> list[dict[str, Any]]:
        cur = conn.execute(
            """
            SELECT event, flight, COUNT(*) AS tile_count,
                   MIN(min_lon) AS min_lon, MIN(min_lat) AS min_lat,
                   MAX(max_lon) AS max_lon, MAX(max_lat) AS max_lat
            FROM noaa_tiles
            WHERE min_lon <= ? AND max_lon >= ? AND min_lat <= ? AND max_lat >= ?
            GROUP BY event, flight
            ORDER BY flight
            """,
            (bbox[2], bbox[0], bbox[3], bbox[1]),
        )
        return [dict(r) for r in cur.fetchall()]

    async def flights_in_bbox(self, bbox: list[float]) -> list[dict[str, Any]]:
        return await self.run(self._flights_in_bbox, bbox)

    @staticmethod
    def _tiles_for_bbox(
        conn: sqlite3.Connection, event: str, flight: str, bbox: list[float], limit: int
    ) -> list[str]:
        cur = conn.execute(
            """
            SELECT key FROM noaa_tiles
            WHERE event = ? AND flight = ?
              AND min_lon <= ? AND max_lon >= ? AND min_lat <= ? AND max_lat >= ?
            LIMIT ?
            """,
            (event, flight, bbox[2], bbox[0], bbox[3], bbox[1], limit),
        )
        return [r["key"] for r in cur.fetchall()]

    async def tiles_for_bbox(self, event: str, flight: str, bbox: list[float], limit: int = 9) -> list[str]:
        return await self.run(self._tiles_for_bbox, event, flight, bbox, limit)

    # -- background sync jobs -------------------------------------------
    @staticmethod
    def _create_job(conn: sqlite3.Connection, job_id: str, kind: str, target: str) -> None:
        conn.execute(
            "INSERT INTO sync_jobs (id, kind, target, status) VALUES (?, ?, ?, 'running')",
            (job_id, kind, target),
        )

    async def create_job(self, job_id: str, kind: str, target: str) -> None:
        await self.run(self._create_job, job_id, kind, target)

    @staticmethod
    def _finish_job(conn: sqlite3.Connection, job_id: str, status: str, detail: str | None) -> None:
        conn.execute(
            "UPDATE sync_jobs SET status = ?, detail = ?, updated_at = datetime('now') WHERE id = ?",
            (status, detail, job_id),
        )

    async def finish_job(self, job_id: str, status: str, detail: str | None = None) -> None:
        await self.run(self._finish_job, job_id, status, detail)

    @staticmethod
    def _get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await self.run(self._get_job, job_id)


storm_db = StormDB()
