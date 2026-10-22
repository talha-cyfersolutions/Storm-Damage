from __future__ import annotations

from pathlib import Path
from starlette.testclient import TestClient

from config import Config


def test_process_damage_date_sequence_validation(client: TestClient) -> None:
    # storm_end_date precedes storm_start_date
    payload = {
        "latitude": 28.5,
        "longitude": -81.3,
        "storm_start_date": "2024-09-30",
        "storm_end_date": "2024-09-20",
    }
    response = client.post("/api/v1/process-damage", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "BAD_REQUEST"
    assert "precede" in data["error"]["message"]


def test_process_damage_coordinate_bounds_validation(client: TestClient) -> None:
    # Latitude > 90
    payload = {
        "latitude": 95.0,
        "longitude": -81.3,
        "storm_start_date": "2024-09-20",
        "storm_end_date": "2024-09-25",
    }
    response = client.post("/api/v1/process-damage", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert len(data["error"]["details"]) > 0


def test_process_damage_extra_forbidden(client: TestClient) -> None:
    # Extra field forbidden by model_config = ConfigDict(extra="forbid")
    payload = {
        "latitude": 28.5,
        "longitude": -81.3,
        "storm_start_date": "2024-09-20",
        "storm_end_date": "2024-09-25",
        "unrecognized_field": "injected",
    }
    response = client.post("/api/v1/process-damage", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"


def test_cog_asset_invalid_date_format(client: TestClient) -> None:
    response = client.get("/api/v1/cog/assets/12345/not-a-date.tif")
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["code"] == "BAD_REQUEST"
    assert "Invalid acquisition_date format" in data["error"]["message"]


def test_cog_asset_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/cog/assets/99999/2024-09-28.tif")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"


def test_cog_asset_streaming_and_byte_ranges(client: TestClient) -> None:
    # Seed a dummy COG file into the test storage directory
    event_id = 999999
    date_str = "2024-09-28"
    filename = f"storm_{event_id}_{date_str}.tif"
    storage_dir = Path(Config.COG_STORAGE_DIR) / "storm-cogs"
    storage_dir.mkdir(parents=True, exist_ok=True)
    file_path = storage_dir / filename

    test_bytes = b"MOCK_COG_HEADER_BYTES_AND_RASTER_DATA_PAYLOAD_FOR_TESTING"
    file_path.write_bytes(test_bytes)
    total_len = len(test_bytes)

    try:
        # 1. Full content request (200 OK)
        resp_full = client.get(f"/api/v1/cog/assets/{event_id}/{date_str}.tif")
        assert resp_full.status_code == 200
        assert resp_full.content == test_bytes
        assert resp_full.headers["Content-Length"] == str(total_len)
        assert resp_full.headers["Accept-Ranges"] == "bytes"

        # 2. Byte range request (206 Partial Content)
        resp_range = client.get(
            f"/api/v1/cog/assets/{event_id}/{date_str}.tif",
            headers={"Range": "bytes=0-15"},
        )
        assert resp_range.status_code == 206
        assert resp_range.content == test_bytes[0:16]
        assert resp_range.headers["Content-Range"] == f"bytes 0-15/{total_len}"
        assert resp_range.headers["Content-Length"] == "16"

        # 3. Suffix range request (bytes=-10)
        resp_suffix = client.get(
            f"/api/v1/cog/assets/{event_id}/{date_str}.tif",
            headers={"Range": "bytes=-10"},
        )
        assert resp_suffix.status_code == 206
        assert resp_suffix.content == test_bytes[-10:]
        assert resp_suffix.headers["Content-Length"] == "10"

        # 4. Out of bounds range request (416 Range Not Satisfiable)
        resp_oob = client.get(
            f"/api/v1/cog/assets/{event_id}/{date_str}.tif",
            headers={"Range": f"bytes={total_len + 100}-{total_len + 200}"},
        )
        assert resp_oob.status_code == 416
    finally:
        if file_path.exists():
            file_path.unlink()

