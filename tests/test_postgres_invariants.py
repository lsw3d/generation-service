import asyncio
import uuid

import asyncpg
import pytest

from tests.db_helpers import connect_db, wait_for_lock_wait


async def _insert_ledger_in_transaction(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    amount: int,
    payment_id: str,
) -> None:
    await conn.execute("BEGIN")
    try:
        await conn.execute(
            """
            INSERT INTO ledger_entries (user_id, amount, kind, payment_id)
            VALUES ($1, $2, 'payment', $3)
            """,
            user_id,
            amount,
            payment_id,
        )
        await conn.execute("COMMIT")
    except BaseException:
        await conn.execute("ROLLBACK")
        raise


async def test_trigger_serializes_concurrent_inserts_without_app_lock():
    """Regression for balance = SUM(ledger): two writers must not lose an update.

    The test deliberately does not take the application's users FOR UPDATE lock.
    The two inserts therefore race on the trigger's own UPDATE of users.balance.
    """
    first = await connect_db()
    second = await connect_db()
    user_id = uuid.uuid4()
    try:
        await first.execute("INSERT INTO users (id) VALUES ($1)", user_id)

        await first.execute("BEGIN")
        await first.execute(
            """
            INSERT INTO ledger_entries (user_id, amount, kind, payment_id)
            VALUES ($1, 50, 'payment', $2)
            """,
            user_id,
            str(uuid.uuid4()),
        )

        blocked = asyncio.create_task(
            _insert_ledger_in_transaction(second, user_id, 30, str(uuid.uuid4()))
        )
        await wait_for_lock_wait(first, second.get_server_pid())

        await first.execute("COMMIT")
        await blocked

        balance = await first.fetchval(
            "SELECT balance FROM users WHERE id = $1", user_id
        )
        total = await first.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE user_id = $1",
            user_id,
        )
        assert total == 80
        assert balance == 80
    finally:
        await first.close()
        await second.close()


async def test_ledger_rejects_update_and_delete():
    conn = await connect_db()
    user_id = uuid.uuid4()
    try:
        await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
        entry_id = await conn.fetchval(
            """
            INSERT INTO ledger_entries (user_id, amount, kind, payment_id)
            VALUES ($1, 40, 'payment', $2)
            RETURNING id
            """,
            user_id,
            str(uuid.uuid4()),
        )

        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute(
                "UPDATE ledger_entries SET amount = 1 WHERE id = $1", entry_id
            )
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute("DELETE FROM ledger_entries WHERE id = $1", entry_id)

        row = await conn.fetchrow(
            "SELECT amount FROM ledger_entries WHERE id = $1", entry_id
        )
        assert row["amount"] == 40
    finally:
        await conn.close()
