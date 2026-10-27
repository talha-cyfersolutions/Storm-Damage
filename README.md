# Storm Damage Imagery API

A high-performance, production-ready geospatial FastAPI service for rapid post-storm damage detection using Sentinel-1 SAR, Sentinel-2, and Landsat optical imagery, returning analytical statistics and 4-band Cloud-Optimized GeoTIFFs (COGs).

---

## Production Features

- **Robust Architecture**:
  - Modular layered structure (`core/`, `middleware/`, `models/`, `routes/`, `services/`).
  - Production-ready `lifespan` context manager for startup validation and graceful shutdowns.
- **Security & Protection**:
  - OWASP security headers (`nosniff`, `DENY`, `XSS-Protection`, `strict-origin-when-cross-origin`, and `HSTS` in production).
  - API Key authentication (`X-API-Key` or `Authorization: Bearer <key>`) with constant-time cryptographic verification (`secrets.compare_digest`).
  - Request body payload size limits (`MAX_PAYLOAD_SIZE_BYTES`) to prevent denial-of-service.
  - Sliding-window rate limiting with dedicated quotas for compute-heavy SAR processing.
  - Reverse proxy IP resolution honoring `TRUSTED_PROXIES`.
  - Directory traversal prevention on file streaming endpoints.
- **Cloud-Optimized GeoTIFF Streaming**:
  - Zero-RAM byte-range streaming via `f.seek()` on local storage.
  - Transparent S3 presigned URL redirection (`HTTP 307`) when running with AWS backend.
  - Proper caching headers (`ETag`, `Accept-Ranges`, `Cache-Control: public, max-age=86400, immutable`).
  - Selective GZip compression that compresses JSON / GeoJSON while preserving raw GeoTIFF binaries.
- **Observability & Error Handling**:
  - Request correlation IDs (`X-Request-ID`) attached to every request, response, and log entry.
  - Structured JSON logging for cloud log aggregators (Datadog, CloudWatch, Loki).
  - Standardized error response envelopes (`VALIDATION_ERROR`, `BAD_REQUEST`, `UNAUTHORIZED`, etc.).
  - Production sanitization of unhandled 500 exceptions to prevent leaking internal stack traces.
  - Kubernetes / container health probes:
    - `/health`: Overall system metrics and storage backend status.
    - `/health/live`: Lightweight liveness probe.
    - `/health/ready`: Readiness probe verifying storage and STAC provider reachability.
- **Containerization & Deployment**:
  - Multi-stage `Dockerfile` with GDAL/PROJ runtime libraries and unprivileged `appuser`.
  - `docker-compose.yml` for unified local stack orchestration.
  - `gunicorn_conf.py` tuned for multi-worker async ASGI workloads with auto worker recycling.

---

## Getting Started

### 1. Installation

```bash
# Clone the repository and enter the directory
cd Storm-Damage

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Environment Configuration

Copy the sample environment file and configure settings:

```bash
cp .env.example .env
```

Key environment variables:
| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | Deployment environment (`development`, `staging`, `production`) |
| `API_KEY` | `None` | Secret key for authenticating incoming API requests |
| `API_KEY_REQUIRED` | `false` | If `true`, unauthenticated requests return `401 Unauthorized` |
| `COG_STORAGE_BACKEND` | `local` | `local` (filesystem) or `s3` (AWS S3) |
| `COG_STORAGE_DIR` | `./cog-storage` | Local directory for storing generated GeoTIFFs |
| `AWS_S3_BUCKET` | `burnscar` | S3 bucket name when backend is `s3` |
| `RATE_LIMIT_ENABLED` | `true` | Sliding window rate limiting toggle |
| `FRONTEND_URLS` | `http://localhost:3000...` | Comma-separated list of allowed CORS origins |
| `MAPBOX_ACCESS_TOKEN` | `None` | Optional public Mapbox token served to the browser viewer; enables Dark and Satellite base styles |

The viewer is scoped to the National Capital Region: District of Columbia, Virginia,
and Maryland. NCEI sync jobs download the selected years but persist only damaging
events in those three jurisdictions. Historical queries from the viewer include
`region=mid_atlantic`; API consumers can use the same filter explicitly.

Satellite scene selection is limited to three days before/after the requested storm
dates. `auto` checks Sentinel-2 and Landsat Collection 2 Level-2 optical scenes,
then falls back to Sentinel-1 SAR when optical imagery is unavailable or cannot be
read. The generated COG contains the complete requested analysis bounding box; it
does not download an entire satellite swath.

The viewer provides a **Dark** and **Satellite** base-map switcher. Without a
Mapbox token it uses CARTO Dark and Esri World Imagery raster tiles as a local
fallback; with `MAPBOX_ACCESS_TOKEN` it uses Mapbox's `dark-v11` and
`satellite-streets-v12` styles.

---

## Running the API

### Development Mode

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Production Mode (Gunicorn + Uvicorn Workers)

```bash
gunicorn -c gunicorn_conf.py main:app
```

### Docker Deployment

```bash
# Build and run with Docker
docker build -t storm-damage-api .
docker run -p 8000:8000 --env-file .env storm-damage-api

# Or with Docker Compose
docker compose up -d

# Or with Production NGINX Reverse Proxy
docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d --build
```

---

## API Endpoints

Interactive Swagger UI documentation is available at `/docs` (and ReDoc at `/redoc`).

### Health & Probes
- `GET /health`: Overall health, environment, uptime, and storage status.
- `GET /health/live`: Liveness probe for Kubernetes.
- `GET /health/ready`: Readiness probe checking storage and Planetary STAC connectivity.

### Core Geospatial Processing
- `POST /api/v1/process-damage`
  - **Payload**:
    ```json
    {
      "latitude": 28.5383,
      "longitude": -81.3792,
      "storm_start_date": "2024-09-24",
      "storm_end_date": "2024-09-28",
      "buffer_km": 10.0,
      "prefer_sensor": "auto",
      "incident_name": "Hurricane Helene Impact"
    }
    ```
  - **Response**: Classification statistics, damaged area in $\text{km}^2$, severity breakdown, GeoJSON boundary, symbology classes, and asset URL.

- `GET /api/v1/cog/assets/{event_id}/{acquisition_date}.tif`
  - Supports HTTP Range requests (`Range: bytes=0-1023`) for MapLibre GL, OpenLayers, QGIS, and Leaflet canvas streaming.
- `GET /api/v1/viewer-config`
  - Returns browser-safe viewer configuration, including the optional Mapbox public token.

---

## Running Automated Tests

Run the test suite using `pytest`:

```bash
pytest -v
```
