# NGINX Reverse Proxy for Storm Damage Imagery API

This directory contains a production-ready NGINX reverse proxy configuration tuned specifically for the Storm Damage Imagery API.

---

## Key Geospatial & Performance Tuning

1. **Extended Timeouts (`300s`)**:
   - The `/api/v1/process-damage` pipeline queries Sentinel-1 SAR / Sentinel-2 scenes, executes GDAL spatial warping, and computes difference indices. This compute workload can take 60–120s. Standard NGINX 60s timeouts would prematurely terminate requests with `504 Gateway Timeout`.
2. **Cloud-Optimized GeoTIFF (COG) Streaming**:
   - `/api/v1/cog/assets/{event_id}/{acquisition_date}.tif` serves 206 Partial Content for byte-range requests used by MapLibre GL, GeoRaster, Leaflet, and QGIS.
   - Preserves `Range` and `If-Range` headers.
   - Disables `proxy_buffering` on COG endpoints to stream raster byte windows directly with zero memory bloat.
3. **Payload Limits (`50M`)**:
   - FastAPI allows up to 10MB (`MAX_PAYLOAD_SIZE_BYTES`). Default NGINX drops bodies above 1MB (`413 Request Entity Too Large`). Configured to `50M` for large GeoJSON Area-of-Interest (AOI) polygons.
4. **Compression Policy**:
   - Gzip is enabled for JSON, GeoJSON, HTML, CSS, JavaScript, and XML.
   - **TIFF / GeoTIFF (`image/tiff`) is strictly excluded** from compression to prevent wasting server CPU and breaking client-side range decoding.
5. **Real IP & Rate Limiting Integration**:
   - Passes `X-Real-IP` and `X-Forwarded-For` headers so the FastAPI sliding-window rate limiter accurately tracks client IPs instead of the reverse proxy container.
6. **Health Probe Log Silence**:
   - Healthcheck routes (`/health`, `/health/live`, `/health/ready`) have `access_log off;` to prevent flooding disk storage from routine container orchestrator polling.

---

## Deployment Options

### Option 1: Docker Compose (Recommended)

Run the API and NGINX together using the provided compose override:

```bash
docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d --build
```

- NGINX listens on ports `80` (HTTP) and `443` (HTTPS).
- The FastAPI container (`storm-damage-api`) is protected inside the Docker bridge network and only exposes port 8000 internally.

To test:
```bash
curl http://localhost/health/live
curl http://localhost/
```

---

### Option 2: Host / Bare-Metal NGINX (Ubuntu / Debian / RHEL)

1. **Install NGINX**:
   ```bash
   sudo apt update && sudo apt install -y nginx
   ```

2. **Copy Configuration**:
   ```bash
   sudo cp nginx/conf.d/storm-damage.conf /etc/nginx/sites-available/storm-damage.conf
   sudo cp nginx/conf.d/storm-api-locations.include /etc/nginx/conf.d/storm-api-locations.include
   ```

3. **Switch Upstream to Localhost**:
   Edit `/etc/nginx/sites-available/storm-damage.conf`:
   ```nginx
   upstream storm_api {
       # Comment out docker container name:
       # server storm-damage-api:8000;
       # Uncomment host:
       server 127.0.0.1:8000;
       keepalive 32;
   }
   ```

4. **Enable Site & Test**:
   ```bash
   sudo ln -sf /etc/nginx/sites-available/storm-damage.conf /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t
   sudo systemctl reload nginx
   ```

---

## Enabling SSL / HTTPS with Let's Encrypt (Certbot)

1. **Obtain Certificate**:
   ```bash
   sudo certbot certonly --webroot -w /var/www/certbot -d api.yourdomain.com
   ```

2. **Enable HTTPS in `storm-damage.conf`**:
   - Uncomment the `return 301 https://$host$request_uri;` block under port 80.
   - Uncomment the `server { listen 443 ssl http2; ... }` block and update `api.yourdomain.com` with your real domain.
   - Test and reload:
     ```bash
     sudo nginx -t && sudo systemctl reload nginx
     ```

