from __future__ import annotations

from starlette.testclient import TestClient


def test_root_endpoint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "operational"
    assert "version" in data
    assert "endpoints" in data


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert "uptime_seconds" in data
    assert data["storage"]["backend"] == "local"


def test_liveness_endpoint(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_readiness_endpoint(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data
    assert "storage" in data

