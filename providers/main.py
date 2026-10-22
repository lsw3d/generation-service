import asyncio
import json
import os
import random
import uuid
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.hmac_util import sign_body

PROVIDER_NAME = os.environ.get("PROVIDER_NAME", "alpha")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "http://localhost:8000/webhooks/providers/alpha")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "test-secret")
PROVIDER_TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT", "60"))

app = FastAPI(title=f"fake-provider-{PROVIDER_NAME}")


class Settings(BaseModel):
    fail_rate: float = 0.0
    error_type: Literal["technical", "moderation", "payload"] = "technical"
    sync_fail: Literal["503", "timeout"] | None = None
    webhook_duplicate: bool = False
    webhook_never: bool = False
    webhook_late: bool = False
    webhook_delay_min: float = 2.0
    webhook_delay_max: float = 20.0


class TaskIn(BaseModel):
    attempt_id: str
    prompt: str
    ref_images: list[str] = Field(default_factory=list)


settings = Settings()
_lock = asyncio.Lock()


@app.put("/admin/settings")
async def put_settings(body: Settings):
    global settings
    async with _lock:
        settings = body
    return settings.model_dump()


@app.get("/admin/settings")
async def get_settings():
    return settings.model_dump()


@app.post("/tasks", status_code=202)
async def create_task(body: TaskIn):
    current = settings
    if current.sync_fail == "503":
        raise HTTPException(status_code=503, detail="unavailable")
    if current.sync_fail == "timeout":
        await asyncio.sleep(3600)

    external_id = str(uuid.uuid4())
    asyncio.create_task(_deliver_webhook(body.attempt_id, external_id, current.model_copy()))
    return {"external_id": external_id}


async def _deliver_webhook(attempt_id: str, external_id: str, cfg: Settings) -> None:
    if cfg.webhook_never:
        return
    if cfg.webhook_late:
        delay = PROVIDER_TIMEOUT + 10
    else:
        low, high = cfg.webhook_delay_min, cfg.webhook_delay_max
        if high < low:
            high = low
        delay = random.uniform(low, high)
    await asyncio.sleep(delay)

    if random.random() < cfg.fail_rate:
        payload = {
            "external_id": external_id,
            "attempt_id": attempt_id,
            "status": "failed",
            "error_type": cfg.error_type,
            "result_url": None,
        }
    else:
        payload = {
            "external_id": external_id,
            "attempt_id": attempt_id,
            "status": "completed",
            "error_type": None,
            "result_url": f"https://files.example/{PROVIDER_NAME}/{external_id}",
        }

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Signature": sign_body(WEBHOOK_SECRET, raw),
    }

    async def once() -> None:
        for _ in range(8):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(WEBHOOK_URL, content=raw, headers=headers)
                if response.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)

    if cfg.webhook_duplicate:
        await asyncio.gather(once(), once())
    else:
        await once()
