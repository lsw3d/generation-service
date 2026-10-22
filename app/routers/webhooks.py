import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.hmac_util import verify_signature
from app.schemas import PaymentWebhook
from app.services.billing import apply_payment
from app.services.orchestration import apply_provider_completed, apply_provider_failed, dispatch_until_waiting_or_done

log = logging.getLogger(__name__)
router = APIRouter()


def _require_signature(body: bytes, request: Request) -> None:
    signature = request.headers.get("X-Signature")
    if not verify_signature(settings.webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid signature")


@router.post("/webhooks/payments")
async def payments_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    raw = await request.body()
    _require_signature(raw, request)
    payload = PaymentWebhook.model_validate_json(raw)
    applied = await apply_payment(
        session,
        payment_id=payload.payment_id,
        user_id=payload.user_id,
        tokens=payload.tokens,
        status=payload.status,
    )
    await session.commit()
    return {"ok": True, "applied": applied}


@router.post("/webhooks/providers/{name}")
async def provider_webhook(name: str, request: Request, session: AsyncSession = Depends(get_session)):
    if name not in ("alpha", "beta"):
        raise HTTPException(status_code=404, detail="unknown provider")
    raw = await request.body()
    _require_signature(raw, request)
    data = json.loads(raw)

    attempt_id = uuid.UUID(data["attempt_id"]) if data.get("attempt_id") else None
    external_id = data.get("external_id")
    status = data.get("status")

    if status == "completed":
        await apply_provider_completed(
            session,
            provider_name=name,
            attempt_id=attempt_id,
            external_id=external_id,
            result_url=data.get("result_url"),
        )
        await session.commit()
        return {"ok": True}

    if status == "failed":
        error_type = data.get("error_type") or "technical"
        task_id = await apply_provider_failed(
            session,
            provider_name=name,
            attempt_id=attempt_id,
            external_id=external_id,
            error_type=error_type,
        )
        await session.commit()
        if task_id is not None:
            # Failover HTTP must not run inside the webhook transaction.
            asyncio.create_task(dispatch_until_waiting_or_done(task_id))
        return {"ok": True}

    raise HTTPException(status_code=400, detail="unknown status")
