from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass
class SubmitResult:
    accepted: bool
    external_id: str | None = None


async def submit_task(base_url: str, payload: dict) -> SubmitResult:
    try:
        async with httpx.AsyncClient(timeout=settings.http_timeout) as client:
            response = await client.post(f"{base_url.rstrip('/')}/tasks", json=payload)
    except (httpx.TimeoutException, httpx.TransportError):
        return SubmitResult(accepted=False)

    if response.status_code == 202:
        data = response.json()
        return SubmitResult(accepted=True, external_id=data["external_id"])
    return SubmitResult(accepted=False)
