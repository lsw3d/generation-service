from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import GenerateRequest, GenerateResponse
from app.services.orchestration import GenerateError, create_generation

router = APIRouter()


@router.post("/generate", response_model=GenerateResponse)
async def generate(body: GenerateRequest, session: AsyncSession = Depends(get_session)):
    try:
        task_id = await create_generation(
            session,
            user_id=body.user_id,
            model_slug=body.model_slug,
            prompt=body.prompt,
            ref_images=body.ref_images,
        )
    except GenerateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return GenerateResponse(task_id=task_id)
