import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LedgerEntry, LedgerKind, User


class PaymentConflict(Exception):
    """A payment identifier cannot describe two different credits."""


async def ensure_user_locked(session: AsyncSession, user_id: uuid.UUID) -> User:
    await session.execute(pg_insert(User).values(id=user_id, balance=0).on_conflict_do_nothing())
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one()
    return user


async def apply_payment(
    session: AsyncSession,
    *,
    payment_id: str,
    user_id: uuid.UUID,
    tokens: int,
    status: str,
) -> bool:
    """Credit tokens once per payment_id. Duplicates (including concurrent) are no-ops."""
    if status != "paid" or tokens <= 0:
        return False

    await ensure_user_locked(session, user_id)
    inserted = await session.scalar(
        pg_insert(LedgerEntry)
        .values(user_id=user_id, amount=tokens, kind=LedgerKind.payment, payment_id=payment_id)
        .on_conflict_do_nothing(
            index_elements=[LedgerEntry.payment_id],
            index_where=LedgerEntry.payment_id.is_not(None),
        )
        .returning(LedgerEntry.id)
    )
    if inserted is not None:
        return True
    existing = (await session.scalars(
        select(LedgerEntry).where(LedgerEntry.payment_id == payment_id)
    )).one()
    if existing.user_id != user_id or existing.amount != tokens:
        raise PaymentConflict("payment_id already belongs to a different credit")
    return False


async def charge_task(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    amount: int,
) -> None:
    session.add(
        LedgerEntry(
            user_id=user_id,
            amount=-amount,
            kind=LedgerKind.charge,
            task_id=task_id,
        )
    )
    await session.flush()


async def refund_task(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    amount: int,
) -> bool:
    """Insert the single refund row. Unique index makes a second insert a no-op."""
    inserted = await session.scalar(
        pg_insert(LedgerEntry)
        .values(user_id=user_id, amount=amount, kind=LedgerKind.refund, task_id=task_id)
        .on_conflict_do_nothing(
            index_elements=[LedgerEntry.task_id],
            # Keep the partial-index predicate literal: a generic prepared
            # plan cannot prove that a parameter always means 'refund'.
            index_where=text("kind = 'refund' AND task_id IS NOT NULL"),
        )
        .returning(LedgerEntry.id)
    )
    return inserted is not None
