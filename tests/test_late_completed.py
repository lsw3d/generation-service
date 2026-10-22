import json

import httpx

from tests.helpers import API_URL, BETA_URL, credit_user, poll_task, signed_headers


async def test_late_completed_after_watchdog_does_not_grant_result_and_refund():
    user_id = credit_user(50)
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

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        created = await client.post(
            "/generate",
            json={
                "user_id": str(user_id),
                "model_slug": "image-fast",
                "prompt": "slow render",
                "ref_images": ["a", "b", "c", "d", "e"],
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]

        waiting = await poll_task(
            client,
            task_id,
            lambda t: t["attempts"] and t["attempts"][0]["status"] == "waiting",
            timeout=20,
        )
        attempt = waiting["attempts"][0]

        failed = await poll_task(client, task_id, lambda t: t["status"] == "failed", timeout=90)
        assert failed["result_url"] is None
        assert failed["attempts"][0]["status"] == "timed_out"

        payload = {
            "external_id": attempt["external_id"],
            "attempt_id": attempt["id"],
            "status": "completed",
            "error_type": None,
            "result_url": "https://files.example/late.png",
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await client.post(
            "/webhooks/providers/beta", content=body, headers=signed_headers(body)
        )
        assert response.status_code == 200

        task = (await client.get(f"/tasks/{task_id}")).json()
        balance = (await client.get(f"/users/{user_id}/balance")).json()

    assert task["status"] == "failed"
    assert task["result_url"] is None
    refunds = [row for row in balance["ledger"] if row["kind"] == "refund"]
    assert len(refunds) == 1
    assert balance["balance"] == 50
    assert sum(row["amount"] for row in balance["ledger"]) == balance["balance"]
