from __future__ import annotations

import ipaddress
import secrets
from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from config import Config

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def is_trusted_proxy(ip_str: str) -> bool:
    """Checks whether an IP address belongs to the trusted proxy list or subnets."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return ip_str in Config.trusted_proxy_list

    for proxy in Config.trusted_proxy_list:
        if proxy == "*":
            return True
        try:
            if "/" in proxy:
                if ip in ipaddress.ip_network(proxy, strict=False):
                    return True
            else:
                if ip == ipaddress.ip_address(proxy):
                    return True
        except ValueError:
            if ip_str == proxy:
                return True
    return False


def get_client_ip(request: Request) -> str:
    """
    Safely resolves the client's IP address, accounting for reverse proxies
    (e.g., Cloudflare, AWS ALB, NGINX) only if the immediate peer is in TRUSTED_PROXIES.
    """
    direct_ip = request.client.host if request.client else "127.0.0.1"

    if not is_trusted_proxy(direct_ip):
        return direct_ip

    # If the direct peer is trusted, evaluate forwarded headers
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Take the leftmost public IP in X-Forwarded-For chain
        ips = [ip.strip() for ip in forwarded_for.split(",")]
        for candidate in ips:
            try:
                ip_obj = ipaddress.ip_address(candidate)
                # First non-private IP is the originating client
                if not ip_obj.is_private and not ip_obj.is_loopback:
                    return candidate
            except ValueError:
                continue
        # If all are private or parsing fails, return first in header
        return ips[0]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return direct_ip


async def verify_api_key(
    request: Request,
    api_key_header: str | None = Security(API_KEY_HEADER),
) -> str | None:
    """
    Validates API key if Config.API_KEY_REQUIRED is True or Config.API_KEY is configured.
    Supports both X-API-Key header and Authorization: Bearer <key>.
    """
    if not Config.API_KEY_REQUIRED:
        return None

    expected_key = Config.API_KEY
    if not expected_key:
        return None

    # Check X-API-Key header first
    provided_key = api_key_header

    # Fallback to Authorization: Bearer <token>
    if not provided_key:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided_key = auth_header[7:].strip()

    if not provided_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API authentication key. Provide via 'X-API-Key' or 'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant time comparison to prevent timing attacks
    if not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired API authentication key.",
        )

    return provided_key

