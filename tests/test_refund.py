import asyncio
import json

import httpx

from tests.helpers import API_URL, BETA_URL, credit_user, poll_task, signed_headers


async def test_ten_concurrent_last_leg_failures_refund_once():
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

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        created = await client.post(
            "/generate",
            json={
                "user_id": str(user_id),
                "model_slug": "video-long",
                "prompt": "a long take",
                "ref_images": ["r1", "r2"],
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]

        task = await poll_task(
            client,
            task_id,
            lambda t: t["attempts"] and t["attempts"][0]["status"] == "waiting",
            timeout=20,
        )
        attempt = task["attempts"][0]
        payload = {
            "external_id": attempt["external_id"],
            "attempt_id": attempt["id"],
            "status": "failed",
            "error_type": "technical",
            "result_url": None,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = signed_headers(body)

        responses = await asyncio.gather(
            *[
                client.post("/webhooks/providers/beta", content=body, headers=headers)
                for _ in range(10)
            ]
        )
        assert all(r.status_code == 200 for r in responses)

        task = await poll_task(client, task_id, lambda t: t["status"] == "failed", timeout=20)
        balance = (await client.get(f"/users/{user_id}/balance")).json()

    assert task["result_url"] is None
    refunds = [row for row in balance["ledger"] if row["kind"] == "refund"]
    charges = [row for row in balance["ledger"] if row["kind"] == "charge"]
    assert len(charges) == 1
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 25
    assert balance["balance"] == 100
    assert sum(row["amount"] for row in balance["ledger"]) == balance["balance"]
