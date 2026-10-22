from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from fastapi import HTTPException, Request, status

from config import Config
from core.security import get_client_ip


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter in memory with periodic GC."""

    def __init__(self, window_seconds: int = 60) -> None:
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._last_gc = time.monotonic()

    async def check(self, key: str, limit: int) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - self.window_seconds

        async with self._lock:
            # Periodic garbage collection every 5 minutes
            if now - self._last_gc > 300:
                self._gc(cutoff)
                self._last_gc = now

            timestamps = self._history[key]
            # Prune entries older than current window
            valid_timestamps = [t for t in timestamps if t > cutoff]
            self._history[key] = valid_timestamps

            count = len(valid_timestamps)
            if count >= limit:
                oldest_in_window = valid_timestamps[0]
                reset_seconds = max(1, int(oldest_in_window + self.window_seconds - now))
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_seconds=reset_seconds,
                )

            valid_timestamps.append(now)
            remaining = max(0, limit - len(valid_timestamps))
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=remaining,
                reset_seconds=self.window_seconds,
            )

    def _gc(self, cutoff: float) -> None:
        keys_to_remove = [k for k, v in self._history.items() if not v or v[-1] <= cutoff]
        for k in keys_to_remove:
            del self._history[k]


# Global rate limiter singletons
standard_limiter = SlidingWindowRateLimiter(window_seconds=Config.RATE_LIMIT_WINDOW_SECONDS)
heavy_limiter = SlidingWindowRateLimiter(window_seconds=Config.RATE_LIMIT_WINDOW_SECONDS)


async def enforce_rate_limit(request: Request, is_heavy: bool = False) -> None:
    """Dependency or middleware helper to enforce rate limiting on endpoints."""
    if not Config.RATE_LIMIT_ENABLED:
        return

    client_ip = get_client_ip(request)
    limiter = heavy_limiter if is_heavy else standard_limiter
    limit = Config.HEAVY_RATE_LIMIT_REQUESTS if is_heavy else Config.RATE_LIMIT_REQUESTS
    key = f"{'heavy' if is_heavy else 'std'}:{client_ip}"

    result = await limiter.check(key, limit)

    # Attach rate limit metadata to request state so middleware can add headers
    request.state.rate_limit = result

    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {limit} requests per {limiter.window_seconds}s.",
            headers={
                "Retry-After": str(result.reset_seconds),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(result.reset_seconds),
            },
        )

