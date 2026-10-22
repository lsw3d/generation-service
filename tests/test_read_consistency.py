import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import User
from app.routers.queries import get_balance
from tests.db_helpers import DATABASE_URL, connect_db, read_balance_and_ledger_sum
from tests.helpers import API_URL, credit_user


async def test_balance_and_ledger_use_one_snapshot_during_payment():
    user_id = credit_user(40)
    conn = await connect_db()
    engine = create_async_engine(DATABASE_URL)

    class PaymentBetweenSelects(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            user = await super().get(entity, ident, **kwargs)
            if entity is User:
                # Commit exactly between the endpoint's user and ledger reads.
                await conn.execute(
                    """INSERT INTO ledger_entries (user_id, amount, kind, payment_id)
                    VALUES ($1, 10, 'payment', $2)""", user_id, str(uuid.uuid4()),
                )
            return user

    try:
        async with PaymentBetweenSelects(engine) as session:
            result = await get_balance(user_id, session)
        assert result.balance == 40
        assert sum(row.amount for row in result.ledger) == 40
        assert await read_balance_and_ledger_sum(user_id) == (50, 50)
    finally:
        await conn.close()
        await engine.dispose()


async def test_ledger_ties_have_stable_order_and_limit():
    conn = await connect_db()
    user_id = uuid.uuid4()
    entry_ids = [uuid.uuid4() for _ in range(25)]
    try:
        async with conn.transaction():
            await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
            for entry_id in entry_ids:
                # now() is identical for every insert within this transaction.
                await conn.execute(
                    """INSERT INTO ledger_entries (id, user_id, amount, kind, payment_id)
                    VALUES ($1, $2, 1, 'payment', $3)""",
                    entry_id, user_id, str(uuid.uuid4()),
                )
        async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
            response = await client.get(f"/users/{user_id}/balance")
        response.raise_for_status()
        result = response.json()
        assert result["balance"] == 25
        assert [row["id"] for row in result["ledger"]] == [
            str(entry_id) for entry_id in sorted(entry_ids, reverse=True)[:20]
        ]
    finally:
        await conn.close()
