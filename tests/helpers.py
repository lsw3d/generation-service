import json
import os
import time
import uuid

import httpx

from app.hmac_util import sign_body

API_URL = os.environ.get("API_URL", "http://localhost:18000")
ALPHA_URL = os.environ.get("ALPHA_URL", "http://localhost:18001")
BETA_URL = os.environ.get("BETA_URL", "http://localhost:18002")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "test-secret")

DEFAULT_PROVIDER_SETTINGS = {
    "fail_rate": 0.0,
    "error_type": "technical",
    "sync_fail": None,
    "webhook_duplicate": False,
    "webhook_never": False,
    "webhook_late": False,
    "webhook_delay_min": 0.2,
    "webhook_delay_max": 0.4,
}


def signed_headers(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Signature": sign_body(WEBHOOK_SECRET, body),
    }


def credit_user(tokens: int = 100) -> uuid.UUID:
    user_id = uuid.uuid4()
    payload = {
        "payment_id": str(uuid.uuid4()),
        "user_id": str(user_id),
        "tokens": tokens,
        "status": "paid",
    }
    body = json.dumps(payload).encode("utf-8")
    response = httpx.post(
        f"{API_URL}/webhooks/payments", content=body, headers=signed_headers(body), timeout=10
    )
    response.raise_for_status()
    return user_id


async def poll_task(client: httpx.AsyncClient, task_id: str, predicate, timeout: float = 90):
    import asyncio

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = await client.get(f"/tasks/{task_id}")
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(0.4)
    raise AssertionError(f"timed out waiting for task {task_id}: {last}")
