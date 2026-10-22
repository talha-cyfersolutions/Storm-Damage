from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


class SelectiveGZipMiddleware:
    """
    Applies GZip compression to text, JSON, and GeoJSON responses while preserving
    raw binary streaming and Content-Length for pre-compressed GeoTIFF and COG assets.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1024) -> None:
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Don't compress raster assets which are already compressed COGs
        if "/cog/assets/" in path or path.endswith((".tif", ".tiff")):
            await self.app(scope, receive, send)
            return

        await self.gzip_app(scope, receive, send)

