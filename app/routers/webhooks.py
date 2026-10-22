from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.hmac_util import verify_signature
from app.schemas import PaymentWebhook, ProviderWebhook
from app.services.billing import PaymentConflict, apply_payment
from app.services.orchestration import (
    apply_provider_completed,
    apply_provider_failed,
    dispatch_until_waiting_or_done,
)

router = APIRouter()


def _require_signature(body: bytes, request: Request) -> None:
    signature = request.headers.get("X-Signature")
    if not verify_signature(settings.webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid signature")


@router.post("/webhooks/payments")
async def payments_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    raw = await request.body()
    _require_signature(raw, request)
    try:
        payload = PaymentWebhook.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid payment payload") from exc
    try:
        applied = await apply_payment(
            session,
            payment_id=payload.payment_id,
            user_id=payload.user_id,
            tokens=payload.tokens,
            status=payload.status,
        )
    except PaymentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True, "applied": applied}


@router.post("/webhooks/providers/{name}")
async def provider_webhook(
    name: str, request: Request, background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    if name not in ("alpha", "beta"):
        raise HTTPException(status_code=404, detail="unknown provider")
    raw = await request.body()
    _require_signature(raw, request)
    try:
        payload = ProviderWebhook.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid provider payload") from exc

    if payload.status == "completed":
        await apply_provider_completed(
            session,
            provider_name=name,
            attempt_id=payload.attempt_id,
            external_id=payload.external_id,
            result_url=payload.result_url,
        )
        await session.commit()
        return {"ok": True}
    if payload.status == "failed":
        task_id = await apply_provider_failed(
            session,
            provider_name=name,
            attempt_id=payload.attempt_id,
            external_id=payload.external_id,
            error_type=payload.error_type,
        )
        await session.commit()
        if task_id is not None:
            # Failover HTTP must not run inside the webhook transaction.
            background_tasks.add_task(dispatch_until_waiting_or_done, task_id)
        return {"ok": True}
