from __future__ import annotations

from starlette.testclient import TestClient

from config import Config


def test_request_id_generated(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) > 10


def test_custom_request_id_preserved(client: TestClient) -> None:
    custom_id = "test-request-id-12345"
    response = client.get("/health", headers={"x-request-id": custom_id})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == custom_id


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "strict-origin-when-cross-origin" in response.headers.get("Referrer-Policy", "")


def test_payload_limit_enforced(client: TestClient) -> None:
    # Temporarily set max payload size to 100 bytes
    original_max = Config.MAX_PAYLOAD_SIZE_BYTES
    Config.MAX_PAYLOAD_SIZE_BYTES = 100
    try:
        huge_payload = {"data": "x" * 500}
        response = client.post("/api/v1/process-damage", json=huge_payload)
        assert response.status_code == 413
        data = response.json()
        assert data["error"]["code"] == "PAYLOAD_TOO_LARGE"
    finally:
        Config.MAX_PAYLOAD_SIZE_BYTES = original_max


def test_api_key_auth_flow(client: TestClient) -> None:
    # Temporarily enable API key requirement
    original_req = Config.API_KEY_REQUIRED
    original_key = Config.API_KEY
    Config.API_KEY_REQUIRED = True
    Config.API_KEY = "secret-super-key"

    try:
        # Request without key
        resp_unauth = client.post(
            "/api/v1/process-damage",
            json={
                "latitude": 28.5,
                "longitude": -81.3,
                "storm_start_date": "2024-09-25",
                "storm_end_date": "2024-09-28",
            },
        )
        assert resp_unauth.status_code == 401
        assert resp_unauth.json()["error"]["code"] == "UNAUTHORIZED"

        # Request with invalid key
        resp_forbidden = client.post(
            "/api/v1/process-damage",
            headers={"X-API-Key": "wrong-key"},
            json={
                "latitude": 28.5,
                "longitude": -81.3,
                "storm_start_date": "2024-09-25",
                "storm_end_date": "2024-09-28",
            },
        )
        assert resp_forbidden.status_code == 403
        assert resp_forbidden.json()["error"]["code"] == "FORBIDDEN"
    finally:
        Config.API_KEY_REQUIRED = original_req
        Config.API_KEY = original_key


def test_trusted_proxy_client_ip_resolution() -> None:
    from unittest.mock import Mock
    from core.security import get_client_ip

    # Request from Docker subnet (e.g. 172.18.0.4) with forwarded public client IP
    req = Mock()
    req.client.host = "172.18.0.4"
    req.headers = {"x-forwarded-for": "203.0.113.195, 172.18.0.4"}

    resolved_ip = get_client_ip(req)
    assert resolved_ip == "203.0.113.195"


