import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    user_id: uuid.UUID
    model_slug: str
    prompt: str
    ref_images: list[str] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    task_id: uuid.UUID


class PaymentWebhook(BaseModel):
    payment_id: str
    user_id: uuid.UUID
    tokens: int
    status: str


class AttemptOut(BaseModel):
    id: uuid.UUID
    provider: str
    status: str
    error_type: str | None
    started_at: datetime
    finished_at: datetime | None
    external_id: str | None
    leg_priority: int


class CurrentLegOut(BaseModel):
    provider: str
    priority: int


class TaskOut(BaseModel):
    id: uuid.UUID
    status: str
    model_slug: str
    current_leg: CurrentLegOut | None
    result_url: str | None
    attempts: list[AttemptOut]


class LedgerOut(BaseModel):
    id: uuid.UUID
    amount: int
    kind: str
    task_id: uuid.UUID | None
    payment_id: str | None
    created_at: datetime


class BalanceOut(BaseModel):
    user_id: uuid.UUID
    balance: int
    ledger: list[LedgerOut]
