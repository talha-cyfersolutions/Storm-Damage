from __future__ import annotations

from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    # Application Info
    APP_NAME: str = "Storm Damage Imagery API"
    APP_VERSION: str = "1.0.0"
    APP_ENV: Literal["development", "staging", "production", "testing"] = "development"
    DEBUG: bool = False
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # API Documentation
    DOCS_ENABLED: bool = True

    # AWS & S3 Storage
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_KEY_ACCESS_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "eu-north-1"
    AWS_S3_BUCKET: str = "burnscar"
    COG_STORAGE_BACKEND: Literal["local", "s3"] = "local"
    COG_STORAGE_DIR: str = str(BASE_DIR / "cog-storage")
    COG_URL_EXPIRY_SECONDS: int = 7200

    # STAC & Earth Observation Endpoints
    STAC_PROVIDER: Literal["aws", "planetary_computer"] = "aws"
    EARTH_SEARCH_URL: str = "https://earth-search.aws.element84.com/v1"
    PLANETARY_COMPUTER_STAC: str = "https://planetarycomputer.microsoft.com/api/stac/v1"
    PLANETARY_COMPUTER_API_KEY: str | None = None
    MAX_CLOUD_COVER: float = 15.0
    STAC_REQUEST_TIMEOUT_SECONDS: int = 30
    GDAL_HTTP_TIMEOUT: int = 30
    NASA_FIRMS_MAP_KEY: str | None = None
    NASA_FIRMS_URL: str = "https://firms.modaps.eosdis.nasa.gov/api"

    # CORS — Allow all frontend origins
    FRONTEND_URLS: str = "*"
    CORS_ALLOW_CREDENTIALS: bool = True

    # Security & API Protection
    API_KEY: str | None = None
    API_KEY_REQUIRED: bool = False
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_REQUESTS: int = 60
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    HEAVY_RATE_LIMIT_REQUESTS: int = 15
    MAX_PAYLOAD_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    TRUSTED_PROXIES: str = "127.0.0.1,::1"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "json"

    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / ".env"),),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def aws_access_key_id(self) -> str | None:
        return self.AWS_ACCESS_KEY_ID or self.AWS_KEY_ACCESS_ID

    @property
    def frontend_origins(self) -> list[str]:
        if not self.FRONTEND_URLS or self.FRONTEND_URLS.strip() == "*":
            return ["*"]
        origins = [origin.strip() for origin in self.FRONTEND_URLS.split(",") if origin.strip()]
        if "*" in origins:
            return ["*"]
        return origins

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [ip.strip() for ip in self.TRUSTED_PROXIES.split(",") if ip.strip()]


Config = Settings()
