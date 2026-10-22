import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import LedgerEntry, Task, User
from app.schemas import AttemptOut, BalanceOut, CurrentLegOut, LedgerOut, TaskOut

router = APIRouter()


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    task = (
        await session.execute(
            select(Task).options(selectinload(Task.attempts)).where(Task.id == task_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    current = None
    open_attempt = next(
        (a for a in task.attempts if a.status.value in ("sending", "waiting")),
        None,
    )
    if open_attempt is not None:
        current = CurrentLegOut(provider=open_attempt.provider_name, priority=open_attempt.leg_priority)
    elif task.current_leg_priority is not None and task.attempts:
        last = max(task.attempts, key=lambda a: a.leg_priority)
        current = CurrentLegOut(provider=last.provider_name, priority=last.leg_priority)

    attempts = [
        AttemptOut(
            id=a.id,
            provider=a.provider_name,
            status=a.status.value,
            error_type=a.error_type,
            started_at=a.created_at,
            finished_at=a.finished_at,
            external_id=a.external_id,
            leg_priority=a.leg_priority,
        )
        for a in sorted(task.attempts, key=lambda a: a.created_at)
    ]
    return TaskOut(
        id=task.id,
        status=task.status.value,
        model_slug=task.model_slug,
        current_leg=current,
        result_url=task.result_url,
        attempts=attempts,
    )


@router.get("/users/{user_id}/balance", response_model=BalanceOut)
async def get_balance(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    rows = (
        await session.execute(
            select(LedgerEntry)
            .where(LedgerEntry.user_id == user_id)
            .order_by(LedgerEntry.created_at.desc())
            .limit(20)
        )
    ).scalars()
    ledger = [
        LedgerOut(
            id=row.id,
            amount=row.amount,
            kind=row.kind.value,
            task_id=row.task_id,
            payment_id=row.payment_id,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return BalanceOut(user_id=user.id, balance=user.balance, ledger=ledger)
