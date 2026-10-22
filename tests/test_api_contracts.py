import json
import uuid

import httpx

from tests.helpers import API_URL, credit_user, signed_headers


async def test_generate_without_prior_payment_returns_402():
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        response = await client.post(
            "/generate",
            json={
                "user_id": str(uuid.uuid4()),
                "model_slug": "image-fast",
                "prompt": "no wallet",
                "ref_images": [],
            },
        )
    assert response.status_code == 402


async def test_reused_payment_id_with_other_amount_returns_409():
    user_id = credit_user(40)
    payment_id = str(uuid.uuid4())
    first = {
        "payment_id": payment_id,
        "user_id": str(user_id),
        "tokens": 10,
        "status": "paid",
    }
    body = json.dumps(first).encode("utf-8")
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        ok = await client.post("/webhooks/payments", content=body, headers=signed_headers(body))
        assert ok.status_code == 200
        conflict_payload = {
            "payment_id": payment_id,
            "user_id": str(user_id),
            "tokens": 99,
            "status": "paid",
        }
        other = json.dumps(conflict_payload).encode("utf-8")
        conflict = await client.post(
            "/webhooks/payments", content=other, headers=signed_headers(other)
        )
    assert conflict.status_code == 409


async def test_provider_webhook_rejects_unknown_error_type_and_bad_json():
    async with httpx.AsyncClient(base_url=API_URL, timeout=10) as client:
        broken = b"{not-json"
        bad_json = await client.post(
            "/webhooks/providers/alpha", content=broken, headers=signed_headers(broken)
        )
        assert bad_json.status_code == 400

        payload = {
            "attempt_id": str(uuid.uuid4()),
            "external_id": "x",
            "status": "failed",
            "error_type": "no-such-type",
        }
        raw = json.dumps(payload).encode("utf-8")
        unknown = await client.post(
            "/webhooks/providers/alpha", content=raw, headers=signed_headers(raw)
        )
    assert unknown.status_code == 400
