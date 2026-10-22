import asyncio
import os
import uuid

import asyncpg

# Same default as docker-compose's published PostgreSQL port. Inside the API
# container DATABASE_URL is already set and points at the db service.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://gen:gen@localhost:15432/gen"
)


def database_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
        "+asyncpg", ""
    )


async def connect_db() -> asyncpg.Connection:
    return await asyncpg.connect(database_dsn())


async def wait_for_lock_wait(conn: asyncpg.Connection, pid: int, timeout: float = 5.0) -> None:
    """Wait until a backend is blocked on a row lock, not merely slow."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await conn.fetchrow(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1", pid
        )
        if state is not None and state["wait_event_type"] == "Lock":
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"backend {pid} did not block on a lock within {timeout}s")


async def read_balance_and_ledger_sum(user_id: uuid.UUID) -> tuple[int, int]:
    conn = await connect_db()
    try:
        balance = await conn.fetchval("SELECT balance FROM users WHERE id = $1", user_id)
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries WHERE user_id = $1",
            user_id,
        )
        return int(balance), int(total)
    finally:
        await conn.close()
