import uuid

import httpx
import pytest

from tests.db_helpers import connect_db, read_balance_and_ledger_sum
from tests.helpers import ALPHA_URL, API_URL, BETA_URL, credit_user, poll_task
from tests.test_review_regressions import configure, webhook


async def test_watchdog_redispatches_stale_sending_attempt_without_refund():
    """A crash after COMMIT and before POST must not turn sending into timed_out.

    The row is created in the same shape the process leaves it after committing
    task + charge + attempt, with an old created_at so the watchdog must pick it.
    """
    user_id = credit_user(100)
    conn = await connect_db()
    try:
        task_id = await conn.fetchval(
            """
            INSERT INTO tasks (
                user_id, model_slug, prompt, ref_images, status,
                current_leg_priority, price_tokens, created_at, updated_at
            )
            VALUES (
                $1, 'image-fast', 'stale sending', '[]'::jsonb, 'running',
                1, 2, now() - interval '1 hour', now()
            )
            RETURNING id
            """,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO ledger_entries (user_id, amount, kind, task_id)
            VALUES ($1, -2, 'charge', $2)
            """,
            user_id,
            task_id,
        )
        attempt_id = await conn.fetchval(
            """
            INSERT INTO attempts (
                task_id, provider_name, leg_priority, status, created_at
            )
            VALUES ($1, 'alpha', 1, 'sending', now() - interval '1 hour')
            RETURNING id
            """,
            task_id,
        )
    finally:
        await conn.close()

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        task = await poll_task(
            client,
            str(task_id),
            lambda t: t["status"] in ("completed", "failed"),
            timeout=40,
        )
        balance = (await client.get(f"/users/{user_id}/balance")).json()

    assert task["status"] == "completed", task
    assert task["result_url"]
    # The same attempt must be re-sent, not closed as timed_out and failed over.
    assert [a["id"] for a in task["attempts"]] == [str(attempt_id)]
    assert task["attempts"][0]["status"] == "succeeded"

    refunds = [row for row in balance["ledger"] if row["kind"] == "refund"]
    assert refunds == []
    assert balance["balance"] == 98

    db_balance, ledger_sum = await read_balance_and_ledger_sum(user_id)
    assert db_balance == 98
    assert ledger_sum == 98


@pytest.mark.parametrize("priority,provider,url", [(1, "alpha", ALPHA_URL), (2, "beta", BETA_URL)])
async def test_recovery_after_provider_accepts_before_local_commit(priority, provider, url):
    """Reproduce the durable state after a dispatcher dies before saving 202.

    The provider has accepted the attempt, but the local transaction rolled
    back. A retry must reuse that external_id, including on the fallback leg.
    """
    user_id = credit_user(10)
    configure(url, webhook_never=True)
    attempt_id = uuid.uuid4()
    conn = await connect_db()
    try:
        async with conn.transaction():
            task_id = await conn.fetchval(
                """INSERT INTO tasks
                (user_id, model_slug, prompt, ref_images, status, current_leg_priority, price_tokens)
                VALUES ($1, 'image-fast', 'accepted before crash', '[]', 'running', $2, 2)
                RETURNING id""", user_id, priority,
            )
            await conn.execute(
                """INSERT INTO ledger_entries (user_id, amount, kind, task_id)
                VALUES ($1, -2, 'charge', $2)""", user_id, task_id,
            )
            if priority == 2:
                await conn.execute(
                    """INSERT INTO attempts
                    (task_id, provider_name, leg_priority, status, error_type, finished_at)
                    VALUES ($1, 'alpha', 1, 'failed', 'technical', now())""", task_id,
                )
            await conn.execute(
                """INSERT INTO attempts (id, task_id, provider_name, leg_priority, status)
                VALUES ($1, $2, $3, $4, 'sending')""", attempt_id, task_id, provider, priority,
            )
            async with httpx.AsyncClient(timeout=10) as client:
                accepted = await client.post(f"{url}/tasks", json={
                    "attempt_id": str(attempt_id), "prompt": "accepted before crash",
                    "ref_images": [],
                })
            assert accepted.status_code == 202
            external_id = accepted.json()["external_id"]
            # Do not persist the response: exactly the state left by a crash.
    finally:
        await conn.close()

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        task = await poll_task(client, str(task_id),
                               lambda t: t["attempts"][-1]["status"] == "waiting", timeout=20)
        assert len(task["attempts"]) == priority
        assert task["attempts"][-1]["id"] == str(attempt_id)
        assert task["attempts"][-1]["external_id"] == external_id
        response = await webhook(client, provider, {
            "external_id": external_id, "status": "completed",
            "result_url": "https://files.example/recovered",
        })
        response.raise_for_status()
        task = (await client.get(f"/tasks/{task_id}")).json()
    assert task["status"] == "completed"
    assert await read_balance_and_ledger_sum(user_id) == (8, 8)
