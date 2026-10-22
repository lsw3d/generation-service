import asyncio
import json
import uuid

import httpx

from tests.helpers import API_URL, signed_headers


async def test_twenty_concurrent_payment_webhooks_credit_once():
    user_id = uuid.uuid4()
    payment_id = str(uuid.uuid4())
    payload = {
        "payment_id": payment_id,
        "user_id": str(user_id),
        "tokens": 40,
        "status": "paid",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = signed_headers(body)

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        responses = await asyncio.gather(
            *[client.post("/webhooks/payments", content=body, headers=headers) for _ in range(20)]
        )
        assert all(r.status_code == 200 for r in responses)

        balance = (await client.get(f"/users/{user_id}/balance")).json()

    assert balance["balance"] == 40
    payments = [row for row in balance["ledger"] if row["kind"] == "payment"]
    assert len(payments) == 1
    assert sum(row["amount"] for row in balance["ledger"]) == balance["balance"]
