import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Attempt, AttemptStatus
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
    for _ in range(25):
        async with SessionLocal() as session:
            expired = await claim_expired_attempts(session)
            await session.commit()
        if not expired:
            break

    async with SessionLocal() as session:
        # sending is a durable outbox, including a crash before the first POST
        # or after committing a failover. Active dispatchers hold the row lock.
        task_ids = list((await session.scalars(
            select(Attempt.task_id)
            .where(Attempt.status == AttemptStatus.sending)
            .order_by(Attempt.created_at, Attempt.id)
            .with_for_update(skip_locked=True)
            .limit(25)
        )).all())
        await session.commit()
    results = await asyncio.gather(
        *(dispatch_until_waiting_or_done(task_id) for task_id in task_ids),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            log.error("dispatch failed; sending attempt will be retried", exc_info=result)
