import asyncio
import json

import httpx

from tests.db_helpers import read_balance_and_ledger_sum
from tests.helpers import API_URL, BETA_URL, credit_user, poll_task, signed_headers


async def test_two_tasks_of_one_user_refund_once_each_and_balance_matches_ledger():
    user_id = credit_user(100)
    httpx.put(
        f"{BETA_URL}/admin/settings",
        json={
            "fail_rate": 0.0,
            "error_type": "technical",
            "sync_fail": None,
            "webhook_duplicate": False,
            "webhook_never": True,
            "webhook_late": False,
            "webhook_delay_min": 0.2,
            "webhook_delay_max": 0.4,
        },
        timeout=5,
    ).raise_for_status()

    payload = {
        "user_id": str(user_id),
        "model_slug": "image-fast",
        "prompt": "race",
        "ref_images": ["a", "b", "c", "d", "e"],
    }

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        first = await client.post("/generate", json=payload)
        second = await client.post("/generate", json=payload)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        task_ids = [first.json()["task_id"], second.json()["task_id"]]

        waiting = []
        for task_id in task_ids:
            task = await poll_task(
                client,
                task_id,
                lambda t: t["attempts"] and t["attempts"][0]["status"] == "waiting",
                timeout=20,
            )
            waiting.append(task)

        requests = []
        for task in waiting:
            attempt = task["attempts"][0]
            body = json.dumps(
                {
                    "external_id": attempt["external_id"],
                    "attempt_id": attempt["id"],
                    "status": "failed",
                    "error_type": "technical",
                    "result_url": None,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            requests.append(
                client.post("/webhooks/providers/beta", content=body, headers=signed_headers(body))
            )
        responses = await asyncio.gather(*requests)
        assert all(r.status_code == 200 for r in responses)

        for task_id in task_ids:
            await poll_task(client, task_id, lambda t: t["status"] == "failed", timeout=20)

        balance = (await client.get(f"/users/{user_id}/balance")).json()

    assert balance["balance"] == 100
    kinds = [row["kind"] for row in balance["ledger"]]
    assert kinds.count("charge") == 2
    assert kinds.count("refund") == 2
    assert kinds.count("payment") == 1
    assert sum(row["amount"] for row in balance["ledger"]) == balance["balance"]

    db_balance, ledger_sum = await read_balance_and_ledger_sum(user_id)
    assert db_balance == balance["balance"]
    assert ledger_sum == db_balance
