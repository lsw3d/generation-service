import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import provider_url, settings
from app.db import SessionLocal
from app.models import (
    Attempt,
    AttemptStatus,
    ErrorType,
    GenerationModel,
    Leg,
    Task,
    TaskStatus,
    User,
)
from app.services.billing import charge_task, refund_task
from app.services.provider_client import submit_task

class GenerateError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def suitable_legs(legs: list[Leg], ref_count: int) -> list[Leg]:
    return sorted(
        [leg for leg in legs if leg.max_ref_images >= ref_count],
        key=lambda leg: leg.priority,
    )


def next_leg(legs: list[Leg], after_priority: int, error_type: str) -> Leg | None:
    remaining = [leg for leg in legs if leg.priority > after_priority]
    if error_type == ErrorType.moderation.value:
        remaining = [leg for leg in remaining if not leg.skip_on_moderation]
    return remaining[0] if remaining else None


async def load_model(session: AsyncSession, slug: str) -> GenerationModel | None:
    return (
        await session.execute(
            select(GenerationModel)
            .options(selectinload(GenerationModel.legs))
            .where(GenerationModel.slug == slug)
        )
    ).scalar_one_or_none()


async def create_generation(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    model_slug: str,
    prompt: str,
    ref_images: list[str],
) -> uuid.UUID:
    model = await load_model(session, model_slug)
    if model is None:
        raise GenerateError(404, "unknown model")

    legs = suitable_legs(model.legs, len(ref_images))
    if not legs:
        raise GenerateError(400, "no provider leg accepts this many reference images")

    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise GenerateError(402, "insufficient tokens")

    active = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(Task.user_id == user_id, Task.status == TaskStatus.running)
        )
    ).scalar_one()
    if active >= settings.max_active_tasks:
        raise GenerateError(429, "too many active tasks")

    if user.balance < model.price_tokens:
        raise GenerateError(402, "insufficient tokens")

    first = legs[0]
    task = Task(
        user_id=user_id,
        model_slug=model.slug,
        prompt=prompt,
        ref_images=ref_images,
        status=TaskStatus.running,
        current_leg_priority=first.priority,
        price_tokens=model.price_tokens,
    )
    session.add(task)
    await session.flush()
    await charge_task(session, user_id=user_id, task_id=task.id, amount=model.price_tokens)
    session.add(
        Attempt(
            task_id=task.id,
            provider_name=first.provider_name,
            leg_priority=first.priority,
            status=AttemptStatus.sending,
        )
    )
    task_id = task.id
    await session.commit()
    await dispatch_until_waiting_or_done(task_id)
    return task_id


async def _open_attempt(session: AsyncSession, task_id: uuid.UUID) -> Attempt | None:
    return (
        await session.execute(
            select(Attempt)
            .where(
                Attempt.task_id == task_id,
                Attempt.status.in_([AttemptStatus.sending, AttemptStatus.waiting]),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def dispatch_until_waiting_or_done(task_id: uuid.UUID) -> None:
    """One dispatcher owns the row until POST is recorded or rolled back.

    A process crash leaves a durable sending row for another watchdog to retry.
    The provider deduplicates retries by attempt_id if it already accepted POST.
    """
    while True:
        async with SessionLocal() as session:
            attempt = await _open_attempt(session, task_id)
            if attempt is None or attempt.status == AttemptStatus.waiting:
                return
            payload = await _provider_payload(session, attempt)
            base = provider_url(attempt.provider_name)
            result = await submit_task(base, payload)
            if result.accepted and result.external_id:
                attempt.external_id = result.external_id
                attempt.status = AttemptStatus.waiting
                attempt.waiting_since = _now()
                await session.commit()
                return
            await fail_attempt(session, attempt, ErrorType.technical.value, timed_out=False)
            await session.commit()


async def _provider_payload(session: AsyncSession, attempt: Attempt) -> dict:
    task = await session.get(Task, attempt.task_id)
    assert task is not None
    return {
        "attempt_id": str(attempt.id),
        "prompt": task.prompt,
        "ref_images": task.ref_images,
    }


async def fail_attempt(
    session: AsyncSession,
    attempt: Attempt,
    error_type: str,
    *,
    timed_out: bool,
) -> None:
    if attempt.status not in (AttemptStatus.sending, AttemptStatus.waiting):
        return

    attempt.status = AttemptStatus.timed_out if timed_out else AttemptStatus.failed
    attempt.error_type = error_type
    attempt.finished_at = _now()
    await session.flush()

    task = await session.get(Task, attempt.task_id, with_for_update=True)
    if task is None or task.status != TaskStatus.running:
        return

    if error_type == ErrorType.payload.value:
        await _fail_and_refund(session, task)
        return

    model = await load_model(session, task.model_slug)
    assert model is not None
    legs = suitable_legs(model.legs, len(task.ref_images))
    nxt = next_leg(legs, attempt.leg_priority, error_type)
    if nxt is None:
        await _fail_and_refund(session, task)
        return

    session.add(
        Attempt(
            task_id=task.id,
            provider_name=nxt.provider_name,
            leg_priority=nxt.priority,
            status=AttemptStatus.sending,
        )
    )
    task.current_leg_priority = nxt.priority
    task.updated_at = _now()
    await session.flush()


async def _fail_and_refund(session: AsyncSession, task: Task) -> None:
    # First writer of the terminal status wins; refund rides in the same transaction.
    if task.status != TaskStatus.running:
        return
    task.status = TaskStatus.failed
    task.updated_at = _now()
    await refund_task(
        session, user_id=task.user_id, task_id=task.id, amount=task.price_tokens
    )


async def apply_provider_completed(
    session: AsyncSession,
    *,
    provider_name: str,
    attempt_id: uuid.UUID | None,
    external_id: str | None,
    result_url: str | None,
) -> None:
    attempt = await _lock_attempt(session, provider_name, attempt_id, external_id)
    if attempt is None:
        return

    if attempt.status in (AttemptStatus.succeeded, AttemptStatus.failed, AttemptStatus.timed_out):
        # Duplicate or late webhook after the watchdog already closed this attempt.
        return

    task = await session.get(Task, attempt.task_id, with_for_update=True)
    assert task is not None

    if task.status != TaskStatus.running:
        # Rule 6: terminal state is immutable. Do not attach a result after a refund.
        if attempt.status in (AttemptStatus.sending, AttemptStatus.waiting):
            attempt.status = AttemptStatus.failed
            attempt.error_type = ErrorType.technical.value
            attempt.finished_at = _now()
        return

    attempt.status = AttemptStatus.succeeded
    attempt.finished_at = _now()
    task.status = TaskStatus.completed
    task.result_url = result_url
    task.updated_at = _now()


async def apply_provider_failed(
    session: AsyncSession,
    *,
    provider_name: str,
    attempt_id: uuid.UUID | None,
    external_id: str | None,
    error_type: str,
) -> uuid.UUID | None:
    attempt = await _lock_attempt(session, provider_name, attempt_id, external_id)
    if attempt is None:
        return None
    if attempt.status not in (AttemptStatus.sending, AttemptStatus.waiting):
        return None

    task_id = attempt.task_id
    await fail_attempt(session, attempt, error_type, timed_out=False)
    return task_id


async def _lock_attempt(
    session: AsyncSession,
    provider_name: str,
    attempt_id: uuid.UUID | None,
    external_id: str | None,
) -> Attempt | None:
    stmt = select(Attempt)
    if attempt_id is not None:
        stmt = stmt.where(Attempt.id == attempt_id)
    elif external_id:
        stmt = stmt.where(Attempt.provider_name == provider_name, Attempt.external_id == external_id)
    else:
        return None
    attempt = (await session.execute(stmt.with_for_update())).scalar_one_or_none()
    if attempt is None or attempt.provider_name != provider_name:
        return None
    return attempt


async def claim_expired_attempts(session: AsyncSession) -> list[uuid.UUID]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.provider_timeout)
    attempts = (
        await session.execute(
            select(Attempt)
            .where(
                Attempt.status == AttemptStatus.waiting,
                Attempt.waiting_since < cutoff,
            )
            .order_by(Attempt.waiting_since, Attempt.id)
            .with_for_update(skip_locked=True)
            # One refund per transaction avoids cross-user lock inversions
            # between watchdog batches in different processes.
            .limit(1)
        )
    ).scalars()
    all_attempts = list(attempts)
    task_ids: list[uuid.UUID] = []
    for attempt in all_attempts:
        await fail_attempt(session, attempt, ErrorType.technical.value, timed_out=True)
        task_ids.append(attempt.task_id)
    return task_ids
