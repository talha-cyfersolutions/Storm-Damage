"""Fire-and-forget background sync jobs, with status persisted in SQLite."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from services.storms.db import storm_db

logger = logging.getLogger("geoapi.storms.jobs")

# Strong references so running tasks are not garbage collected.
_running: set[asyncio.Task] = set()


async def start_job(kind: str, target: str, work: Callable[[], Awaitable[str]]) -> str:
    """Run `work` in the background. `work` returns a short result description."""
    job_id = uuid.uuid4().hex[:12]
    await storm_db.create_job(job_id, kind, target)

    async def runner() -> None:
        try:
            detail = await work()
            await storm_db.finish_job(job_id, "done", detail)
        except Exception as exc:
            logger.exception("Sync job %s (%s %s) failed", job_id, kind, target)
            await storm_db.finish_job(job_id, "failed", str(exc)[:500])

    task = asyncio.create_task(runner())
    _running.add(task)
    task.add_done_callback(_running.discard)
    return job_id

