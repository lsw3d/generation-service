import asyncio
import logging

from app.config import settings
from app.db import SessionLocal
from app.services.orchestration import claim_expired_attempts, dispatch_until_waiting_or_done

log = logging.getLogger(__name__)


async def watchdog_loop() -> None:
    # Every worker runs this loop. SKIP LOCKED on expired attempts is what
    # prevents double failover when four processes tick at once.
    while True:
        try:
            await watchdog_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watchdog tick failed")
        await asyncio.sleep(settings.watchdog_interval)


async def watchdog_tick() -> None:
    async with SessionLocal() as session:
        task_ids = await claim_expired_attempts(session)
        await session.commit()
    seen: set = set()
    for task_id in task_ids:
        if task_id in seen:
            continue
        seen.add(task_id)
        asyncio.create_task(dispatch_until_waiting_or_done(task_id))
