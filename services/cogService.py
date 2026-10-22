"""Durable storage service for generated Cloud-Optimized GeoTIFF assets."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from config import Config
from models.healthModel import StorageHealth

logger = logging.getLogger("geoapi.cog")


class CogService:
    """Manages durable Cloud-Optimized GeoTIFF storage across local filesystem or AWS S3."""

    def __init__(self) -> None:
        self.backend = Config.COG_STORAGE_BACKEND.lower()
        self.local_root = Path(Config.COG_STORAGE_DIR).resolve()
        # Ensure local root directory exists if using local backend
        if self.backend == "local":
            self.local_root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe_local_path(self, key: str) -> Path:
        """Resolves local path and enforces boundary containment to prevent directory traversal."""
        normalized = os.path.normpath(key).lstrip("/\\")
        candidate = (self.local_root / normalized).resolve()
        if not str(candidate).startswith(str(self.local_root)):
            raise ValueError(f"Path traversal detected: {key}")
        return candidate

    async def put(self, key: str, content: bytes, local_url: str | None = None) -> str:
        """Stores a generated COG asset and returns its accessible URL."""
        if self.backend == "s3":
            return await asyncio.to_thread(self._put_s3, key, content)
        if self.backend == "local":
            return await asyncio.to_thread(self._put_local, key, content, local_url)
        raise ValueError(f"Unsupported COG storage backend: {self.backend}")

    async def get_local(self, key: str) -> bytes | None:
        """Retrieves entire file contents from local storage."""
        if self.backend != "local":
            return None
        return await asyncio.to_thread(self._get_local, key)

    async def get_file_size(self, key: str) -> int | None:
        """Returns the file size in bytes if present."""
        if self.backend != "local":
            return None
        return await asyncio.to_thread(self._get_file_size, key)

    async def read_range(self, key: str, start: int, end: int) -> bytes | None:
        """Reads a specific byte range directly without buffering the entire file in RAM."""
        if self.backend != "local":
            return None
        return await asyncio.to_thread(self._read_range, key, start, end)

    async def get_s3_url(self, key: str) -> str | None:
        """Generates a presigned GET URL for an asset stored in S3."""
        if self.backend != "s3":
            return None
        return await asyncio.to_thread(self._generate_s3_presigned_url, key)

    def _get_file_size(self, key: str) -> int | None:
        try:
            path = self._resolve_safe_local_path(key)
            if path.is_file():
                return path.stat().st_size
            return None
        except (ValueError, OSError):
            return None

    def _read_range(self, key: str, start: int, end: int) -> bytes | None:
        try:
            path = self._resolve_safe_local_path(key)
            if not path.is_file():
                return None
            length = end - start + 1
            with path.open("rb") as f:
                f.seek(start)
                return f.read(length)
        except (ValueError, OSError) as exc:
            logger.error("Error reading byte range [%s-%s] from %s: %s", start, end, key, exc)
            return None

    def _put_local(self, key: str, content: bytes, local_url: str | None) -> str:
        destination = self._resolve_safe_local_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return local_url or f"/api/v1/cog/assets/{key}"

    def _get_local(self, key: str) -> bytes | None:
        try:
            path = self._resolve_safe_local_path(key)
            return path.read_bytes()
        except (FileNotFoundError, ValueError):
            return None

    def _generate_s3_presigned_url(self, key: str) -> str:
        session = boto3.Session(
            aws_access_key_id=Config.aws_access_key_id,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            region_name=Config.AWS_REGION,
        )
        client = session.client("s3", region_name=Config.AWS_REGION)
        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": Config.AWS_S3_BUCKET, "Key": key},
                ExpiresIn=Config.COG_URL_EXPIRY_SECONDS,
            )
        finally:
            client.close()

    def _put_s3(self, key: str, content: bytes) -> str:
        session = boto3.Session(
            aws_access_key_id=Config.aws_access_key_id,
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
            region_name=Config.AWS_REGION,
        )
        client = session.client("s3", region_name=Config.AWS_REGION)
        try:
            client.put_object(
                Bucket=Config.AWS_S3_BUCKET,
                Key=key,
                Body=content,
                ContentType="image/tiff",
                ContentDisposition="inline",
            )
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": Config.AWS_S3_BUCKET, "Key": key},
                ExpiresIn=Config.COG_URL_EXPIRY_SECONDS,
            )
        except (BotoCoreError, ClientError, NoCredentialsError) as exc:
            logger.exception("Failed to store generated COG in S3")
            raise RuntimeError(f"Unable to store generated COG in S3: {exc}") from exc
        finally:
            client.close()

    async def check_health(self) -> StorageHealth:
        """Verifies storage backend readiness."""
        if self.backend == "local":
            try:
                self.local_root.mkdir(parents=True, exist_ok=True)
                test_file = self.local_root / ".health_check"
                test_file.write_text("ok")
                test_file.unlink()
                return StorageHealth(backend="local", status="healthy", details="Filesystem writable")
            except Exception as exc:
                return StorageHealth(backend="local", status="unhealthy", details=str(exc))

        if self.backend == "s3":
            try:
                if not Config.aws_access_key_id or not Config.AWS_SECRET_ACCESS_KEY:
                    return StorageHealth(
                        backend="s3",
                        status="degraded",
                        details="AWS credentials not provided in configuration",
                    )
                # Quick bucket head check
                def check_s3() -> None:
                    session = boto3.Session(
                        aws_access_key_id=Config.aws_access_key_id,
                        aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY,
                        region_name=Config.AWS_REGION,
                    )
                    client = session.client("s3", region_name=Config.AWS_REGION)
                    client.head_bucket(Bucket=Config.AWS_S3_BUCKET)
                    client.close()

                await asyncio.to_thread(check_s3)
                return StorageHealth(backend="s3", status="healthy", details=f"Bucket {Config.AWS_S3_BUCKET} accessible")
            except Exception as exc:
                return StorageHealth(backend="s3", status="degraded", details=str(exc))

        return StorageHealth(backend=self.backend, status="unhealthy", details=f"Unknown backend {self.backend}")
