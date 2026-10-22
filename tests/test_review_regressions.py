import asyncio
import json
import uuid

import httpx
import pytest

from tests.db_helpers import connect_db, read_balance_and_ledger_sum
from tests.helpers import (
    ALPHA_URL, API_URL, BETA_URL, DEFAULT_PROVIDER_SETTINGS,
    credit_user, poll_task, signed_headers,
)


def configure(url, **changes):
    response = httpx.put(
        f"{url}/admin/settings", json={**DEFAULT_PROVIDER_SETTINGS, **changes}, timeout=5
    )
    response.raise_for_status()


async def generate(client, user_id, model="image-fast", refs=None):
    return await client.post("/generate", json={
        "user_id": str(user_id), "model_slug": model,
        "prompt": "review regression", "ref_images": refs or [],
    })


async def webhook(client, provider, payload):
    raw = json.dumps(payload).encode()
    return await client.post(
        f"/webhooks/providers/{provider}", content=raw, headers=signed_headers(raw)
    )


@pytest.mark.parametrize("model,error,providers", [
    ("image-fast", "payload", ["alpha"]),
    ("video-long", "moderation", ["beta"]),
    ("image-fast", "moderation", ["alpha", "beta"]),
])
async def test_payload_and_moderation_follow_spec(model, error, providers):
    user_id = credit_user(100)
    configure(ALPHA_URL if model == "image-fast" else BETA_URL,
              fail_rate=1, error_type=error, webhook_duplicate=True)
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        response = await generate(client, user_id, model)
        response.raise_for_status()
        task = await poll_task(client, response.json()["task_id"],
                               lambda t: t["status"] != "running", timeout=20)
        balance = (await client.get(f"/users/{user_id}/balance")).json()
    assert [a["provider"] for a in task["attempts"]] == providers
    assert task["attempts"][0]["error_type"] == error
    failed = len(providers) == 1
    assert task["status"] == ("failed" if failed else "completed")
    assert balance["balance"] == (100 if failed else 98)
    assert sum(e["kind"] == "refund" for e in balance["ledger"]) == int(failed)


@pytest.mark.parametrize("sync_fail", ["503", "timeout"])
async def test_sync_failure_fails_over(sync_fail):
    user_id = credit_user(10)
    configure(ALPHA_URL, sync_fail=sync_fail)
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        response = await generate(client, user_id)
        response.raise_for_status()
        task = await poll_task(client, response.json()["task_id"],
                               lambda t: t["status"] != "running", timeout=20)
    assert task["status"] == "completed"
    assert [a["provider"] for a in task["attempts"]] == ["alpha", "beta"]
    assert task["attempts"][0]["error_type"] == "technical"
    assert await read_balance_and_ledger_sum(user_id) == (8, 8)


async def test_ref_limit_rejects_before_charge_and_skips_ineligible_leg():
    user_id = credit_user(10)
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        rejected = await generate(client, user_id, refs=["ref"] * 11)
        assert rejected.status_code == 400
        balance = (await client.get(f"/users/{user_id}/balance")).json()
        assert balance["balance"] == 10
        assert len(balance["ledger"]) == 1
        response = await generate(client, user_id, refs=["ref"] * 5)
        response.raise_for_status()
        task = await poll_task(client, response.json()["task_id"],
                               lambda t: t["status"] != "running", timeout=20)
    assert task["status"] == "completed"
    assert [a["provider"] for a in task["attempts"]] == ["beta"]


@pytest.mark.parametrize("tokens,accepted,rejection", [(100, 3, 429), (2, 1, 402)])
async def test_concurrent_generates_enforce_balance_and_active_limit(tokens, accepted, rejection):
    user_id = credit_user(tokens)
    configure(BETA_URL, webhook_never=True)
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        responses = await asyncio.gather(
            *(generate(client, user_id, refs=["ref"] * 5) for _ in range(10))
        )
        assert sum(r.status_code == 200 for r in responses) == accepted
        assert sum(r.status_code == rejection for r in responses) == 10 - accepted
        assert await read_balance_and_ledger_sum(user_id) == (tokens - accepted * 2,) * 2
        # Complete outstanding tasks before restoring shared provider settings.
        for response in responses:
            if response.status_code == 200:
                task = (await client.get(f"/tasks/{response.json()['task_id']}")).json()
                done = await webhook(client, "beta", {
                    "attempt_id": task["attempts"][0]["id"], "status": "completed",
                    "result_url": "https://files.example/completed",
                })
                done.raise_for_status()


@pytest.mark.parametrize("raw", [
    b"{not-json", b"[]", b"{}",
    b'{"status":"completed","attempt_id":"invalid"}',
    json.dumps({"status": "completed", "attempt_id": str(uuid.uuid4())}).encode(),
    json.dumps({"status": "failed", "attempt_id": str(uuid.uuid4()),
                "error_type": "unknown"}).encode(),
])
async def test_invalid_provider_payload_is_400(raw):
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        response = await client.post("/webhooks/providers/alpha", content=raw,
                                     headers=signed_headers(raw))
    assert response.status_code == 400


async def test_invalid_payment_and_signature_do_not_credit():
    raw = b'{"payment_id":"invalid","tokens":"not-an-int"}'
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        response = await client.post("/webhooks/payments", content=raw,
                                     headers=signed_headers(raw))
        assert response.status_code == 400
        unsigned = await client.post("/webhooks/payments", content=raw)
        assert unsigned.status_code == 401
        non_ascii = await client.post(
            "/webhooks/payments", content=raw, headers={b"X-Signature": b"\xff" * 64}
        )
        assert non_ascii.status_code == 401


async def test_payment_id_cannot_credit_another_user():
    users = [credit_user(10), credit_user(10)]
    payment_id = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        responses = []
        for user_id in users:
            raw = json.dumps({"payment_id": payment_id, "user_id": str(user_id),
                              "tokens": 5, "status": "paid"}).encode()
            responses.append(await client.post("/webhooks/payments", content=raw,
                                                headers=signed_headers(raw)))
    assert [r.status_code for r in responses] == [200, 409]
    assert await read_balance_and_ledger_sum(users[0]) == (15, 15)
    assert await read_balance_and_ledger_sum(users[1]) == (10, 10)


async def test_provider_deduplicates_concurrent_posts_by_attempt_id():
    configure(ALPHA_URL, webhook_never=True)
    payload = {"attempt_id": str(uuid.uuid4()), "prompt": "retry", "ref_images": []}
    async with httpx.AsyncClient(timeout=15) as client:
        responses = await asyncio.gather(
            *(client.post(f"{ALPHA_URL}/tasks", json=payload) for _ in range(10))
        )
        assert all(r.status_code == 202 for r in responses)
        external_ids = {r.json()["external_id"] for r in responses}
        assert len(external_ids) == 1
        # A retry remains accepted even if new work is now being rejected.
        configure(ALPHA_URL, sync_fail="503")
        retry = await client.post(f"{ALPHA_URL}/tasks", json=payload)
        assert retry.status_code == 202
        assert retry.json()["external_id"] in external_ids


async def test_late_first_leg_cannot_replace_current_leg_result():
    user_id = credit_user(10)
    configure(ALPHA_URL, webhook_never=True)
    configure(BETA_URL, webhook_never=True)
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        response = await generate(client, user_id)
        response.raise_for_status()
        task_id = response.json()["task_id"]
        first = (await client.get(f"/tasks/{task_id}")).json()["attempts"][0]
        conn = await connect_db()
        try:
            await conn.execute(
                "UPDATE attempts SET waiting_since = now() - interval '1 hour' WHERE id = $1",
                uuid.UUID(first["id"]),
            )
        finally:
            await conn.close()
        task = await poll_task(client, task_id,
                               lambda t: len(t["attempts"]) == 2 and
                               t["attempts"][1]["status"] == "waiting", timeout=20)
        late = await webhook(client, "alpha", {
            "attempt_id": first["id"], "status": "completed",
            "result_url": "https://files.example/late",
        })
        late.raise_for_status()
        unchanged = (await client.get(f"/tasks/{task_id}")).json()
        assert unchanged["status"] == "running"
        assert unchanged["result_url"] is None
        done = await webhook(client, "beta", {
            "attempt_id": task["attempts"][1]["id"], "status": "completed",
            "result_url": "https://files.example/current",
        })
        done.raise_for_status()
        completed = (await client.get(f"/tasks/{task_id}")).json()
    assert completed["status"] == "completed"
    assert completed["result_url"] == "https://files.example/current"
    assert await read_balance_and_ledger_sum(user_id) == (8, 8)
