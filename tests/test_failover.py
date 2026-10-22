import httpx

from tests.helpers import ALPHA_URL, API_URL, BETA_URL, credit_user, poll_task


async def test_first_leg_failure_completes_on_second_with_two_attempts():
    user_id = credit_user(50)
    httpx.put(
        f"{ALPHA_URL}/admin/settings",
        json={
            "fail_rate": 1.0,
            "error_type": "technical",
            "sync_fail": None,
            "webhook_duplicate": False,
            "webhook_never": False,
            "webhook_late": False,
            "webhook_delay_min": 0.2,
            "webhook_delay_max": 0.3,
        },
        timeout=5,
    ).raise_for_status()
    httpx.put(
        f"{BETA_URL}/admin/settings",
        json={
            "fail_rate": 0.0,
            "error_type": "technical",
            "sync_fail": None,
            "webhook_duplicate": False,
            "webhook_never": False,
            "webhook_late": False,
            "webhook_delay_min": 0.2,
            "webhook_delay_max": 0.3,
        },
        timeout=5,
    ).raise_for_status()

    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
        created = await client.post(
            "/generate",
            json={
                "user_id": str(user_id),
                "model_slug": "image-fast",
                "prompt": "a cat",
                "ref_images": [],
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        task = await poll_task(client, task_id, lambda t: t["status"] == "completed", timeout=30)

    assert task["result_url"]
    assert len(task["attempts"]) == 2
    assert task["attempts"][0]["provider"] == "alpha"
    assert task["attempts"][0]["error_type"] == "technical"
    assert task["attempts"][1]["provider"] == "beta"
    assert task["attempts"][1]["status"] == "succeeded"
