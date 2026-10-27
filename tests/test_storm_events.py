from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from models import StormEvent
from services.storms import noaaEri, nwsAlerts, storm_db
from services.storms.nceiStormEvents import parse_storm_events_csv

NCEI_CSV = (
    "BEGIN_YEARMONTH,BEGIN_DAY,END_YEARMONTH,END_DAY,EVENT_ID,STATE,EVENT_TYPE,TOR_F_SCALE,"
    "TOR_LENGTH,TOR_WIDTH,BEGIN_LOCATION,CZ_NAME,BEGIN_LAT,BEGIN_LON,END_LAT,END_LON,"
    "INJURIES_DIRECT,INJURIES_INDIRECT,DEATHS_DIRECT,DEATHS_INDIRECT,DAMAGE_PROPERTY,EVENT_NARRATIVE\n"
    '202405,6,202405,6,111,"MISSISSIPPI","Tornado","EF1","2.51","1100","FERGUSON","PERRY",31.1768,-88.9634,31.1772,-88.9209,0,0,0,0,"50K","Roof damage"\n'
    '202405,6,202405,6,112,"MISSISSIPPI","Heat","","","","X","Y",31.0,-88.0,,,0,0,0,0,"",""\n'
    '202405,7,202405,7,113,"TEXAS","Hail","","","","Z","W",,,,,0,0,0,0,"",""\n'
)


def test_parse_ncei_keeps_only_mappable_damaging_events() -> None:
    events = parse_storm_events_csv(NCEI_CSV.encode())
    assert [e.id for e in events] == ["ncei:111"]
    tornado = events[0]
    assert tornado.rating == "EF1"
    assert tornado.begin_date == "2024-05-06"
    assert tornado.path_length_miles == 2.51
    assert tornado.path_width_yards == 1100
    assert tornado.state == "Mississippi"


def test_parse_tile_bounds_from_filename() -> None:
    bounds = noaaEri.parse_tile_bounds("2020_Hurricane_Laura/20200828a_RGB/20200828aC0914030w293430n.tif")
    assert bounds == (-91.675, 29.55, -91.65, 29.575)
    assert noaaEri.parse_tile_bounds("2020_Hurricane_Laura/downloads/readme.txt") is None


def test_flight_date_and_event_validation() -> None:
    assert noaaEri.flight_date("20240927a_RGB") == "2024-09-27"
    assert noaaEri.flight_date("downloads") is None
    assert noaaEri.is_valid_event_name("2024_Hurricane_Helene")
    assert not noaaEri.is_valid_event_name("../etc")


def test_xyz_bounds_cover_world_at_zoom_zero() -> None:
    minx, miny, maxx, maxy = noaaEri.xyz_to_mercator_bounds(0, 0, 0)
    assert minx < -2.0e7 and maxx > 2.0e7 and miny < -2.0e7 and maxy > 2.0e7
    lonlat = noaaEri.xyz_to_lonlat_bounds(0, 0, 0)
    assert lonlat[0] == -180 and lonlat[2] == 180


def test_parse_nws_alerts_skips_unmappable() -> None:
    payload = {
        "features": [
            {
                "id": "a1",
                "geometry": {"type": "Polygon", "coordinates": [[[-90, 30], [-89, 30], [-89, 31], [-90, 31], [-90, 30]]]},
                "properties": {"id": "urn:1", "event": "Tornado Warning", "severity": "Extreme",
                               "onset": "2026-05-06T19:50:00-05:00", "areaDesc": "Perry, MS"},
            },
            {"id": "a2", "geometry": None, "properties": {"event": "Flood Watch"}},
        ]
    }
    events = nwsAlerts.parse_alerts(payload)
    assert len(events) == 1
    assert events[0].event_type == "Tornado Warning"
    assert events[0].begin_date == "2026-05-06"


def _seed() -> None:
    asyncio.run(storm_db.upsert_events([
        StormEvent(id="ncei:111", source="ncei", event_type="Tornado", rating="EF1",
                   begin_date="2024-05-06", end_date="2024-05-06", state="Mississippi",
                   location="FERGUSON", begin_lat=31.1768, begin_lon=-88.9634,
                   end_lat=31.1772, end_lon=-88.9209),
        StormEvent(id="ncei:222", source="ncei", event_type="Tornado", rating="EF4",
                   begin_date="2024-04-26", begin_lat=41.0, begin_lon=-96.0),
    ]))


def test_history_endpoint_filters(client: TestClient) -> None:
    _seed()
    resp = client.get("/api/v1/storms/history", params={"type": "Tornado", "min_rating": 3})
    assert resp.status_code == 200
    assert [e["id"] for e in resp.json()["events"]] == ["ncei:222"]

    resp = client.get("/api/v1/storms/history", params={"bbox": "-90,30,-88,32"})
    assert [e["id"] for e in resp.json()["events"]] == ["ncei:111"]


def test_history_rejects_bad_params(client: TestClient) -> None:
    assert client.get("/api/v1/storms/history", params={"bbox": "1,2,3"}).status_code == 400
    assert client.get("/api/v1/storms/history", params={"from": "nope"}).status_code == 400
    assert client.get("/api/v1/storms/history", params={"region": "not-a-region"}).status_code == 400


def test_viewer_config_is_available(client: TestClient) -> None:
    response = client.get("/api/v1/viewer-config")
    assert response.status_code == 200
    assert "mapbox_access_token" in response.json()


def test_analyze_unknown_event_404(client: TestClient) -> None:
    resp = client.post("/api/v1/storms/ncei:does-not-exist/analyze", json={})
    assert resp.status_code == 404


def test_noaa_search_empty_returns_message(client: TestClient) -> None:
    resp = client.get("/api/v1/imagery/noaa/search", params={"bbox": "-88.99,31.15,-88.9,31.2"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["message"]


def test_noaa_tile_validation_and_blank(client: TestClient) -> None:
    assert client.get("/api/v1/imagery/noaa/tiles/bad..name/20240927a_RGB/12/1/1.png").status_code in (400, 404)
    assert client.get("/api/v1/imagery/noaa/tiles/2024_Hurricane_Helene/nope/12/1/1.png").status_code == 400
    resp = client.get("/api/v1/imagery/noaa/tiles/2024_Hurricane_Helene/20240927a_RGB/5/1/1.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert client.get("/api/v1/imagery/noaa/tiles/2024_Hurricane_Helene/20240927a_RGB/3/99/1.png").status_code == 400



def test_sync_runs_as_background_job(client: TestClient, monkeypatch) -> None:
    import time

    from services.storms import nceiStormEvents

    async def fake_sync(year: int) -> int:
        return 7

    monkeypatch.setattr(nceiStormEvents, "sync_year", fake_sync)
    resp = client.post("/api/v1/storms/sync", params={"year": 2024})
    assert resp.status_code == 202
    job_id = resp.json()["id"]

    for _ in range(50):
        job = client.get(f"/api/v1/sync/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert "imported 7 events" in job["detail"]
    assert client.get("/api/v1/sync/jobs/nope").status_code == 404


def test_noaa_sync_requires_exactly_one_target(client: TestClient) -> None:
    assert client.post("/api/v1/imagery/noaa/sync").status_code == 400
    assert client.post("/api/v1/imagery/noaa/sync", params={"event": "../x"}).status_code == 400


def test_noaa_aerial_damage_detector_logic() -> None:
    import numpy as np
    from services.storms.noaaDamageService import (
        detect_aerial_damage,
        uniform_filter_2d,
        calculate_statistics,
        encode_4band_cog,
    )

    # 1. Uniform filter
    ones = np.ones((64, 64), dtype=np.float32)
    smoothed = uniform_filter_2d(ones, size=5)
    assert np.allclose(smoothed, 1.0)

    # 2. Smooth undamaged image
    smooth_rgb = np.full((3, 64, 64), 120, dtype=np.uint8)
    valid = np.ones((64, 64), dtype=bool)
    classes = detect_aerial_damage(smooth_rgb, valid)
    assert np.all(classes == 1)

    # 3. Blue tarp pixel
    smooth_rgb[0, 10, 10] = 30   # R low
    smooth_rgb[1, 10, 10] = 40   # G low
    smooth_rgb[2, 10, 10] = 210  # B high
    classes_with_tarp = detect_aerial_damage(smooth_rgb, valid)
    assert classes_with_tarp[10, 10] == 3  # Class 3: Blue tarp / moderate damage

    # 4. Inundated dark pixel
    smooth_rgb[0, 20, 20] = 20
    smooth_rgb[1, 20, 20] = 25
    smooth_rgb[2, 20, 20] = 30
    classes_with_water = detect_aerial_damage(smooth_rgb, valid)
    assert classes_with_water[20, 20] == 2  # Class 2: Minor / Inundated

    # 5. Severe chaos / fragmentation
    rng = np.random.default_rng(42)
    chaotic_rgb = rng.integers(0, 255, (3, 64, 64), dtype=np.uint8)
    classes_chaotic = detect_aerial_damage(chaotic_rgb, valid)
    assert np.any(classes_chaotic == 4)  # Severe collapse / heavy debris

    # 6. Statistics
    stats = calculate_statistics(classes_chaotic, [-90.0, 29.0, -89.9, 29.1])
    assert stats.total_analyzed_km2 > 0
    assert stats.total_damaged_km2 > 0

    # 7. COG Encoding
    cog_bytes = encode_4band_cog(smooth_rgb, classes, [-90.0, 29.0, -89.9, 29.1])
    assert len(cog_bytes) > 1000
    assert cog_bytes[:4] in (b"II*\x00", b"MM\x00*")  # TIFF header


def test_analyze_noaa_damage_endpoint_validation(client: TestClient) -> None:
    # Invalid event name
    resp = client.post("/api/v1/imagery/noaa/analyze-damage", json={"event": "../etc"})
    assert resp.status_code == 400

    # Missing coordinates and bbox
    resp = client.post("/api/v1/imagery/noaa/analyze-damage", json={"event": "2024_Hurricane_Francine"})
    assert resp.status_code == 400
    msg = resp.json().get("detail") or resp.json().get("error", {}).get("message", "")
    assert "Provide either 'bbox'" in msg
