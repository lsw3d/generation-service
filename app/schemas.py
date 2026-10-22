import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class GenerateRequest(BaseModel):
    user_id: uuid.UUID
    model_slug: str
    prompt: str
    ref_images: list[str] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    task_id: uuid.UUID


class PaymentWebhook(BaseModel):
    payment_id: str = Field(min_length=1, max_length=64)
    user_id: uuid.UUID
    tokens: int = Field(strict=True, gt=0, le=2147483647)
    status: str = Field(min_length=1)


class ProviderWebhook(BaseModel):
    attempt_id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["completed", "failed"]
    error_type: Literal["technical", "moderation", "payload"] | None = None
    result_url: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.attempt_id is None and self.external_id is None:
            raise ValueError("attempt_id or external_id is required")
        if self.status == "completed" and not self.result_url:
            raise ValueError("completed requires result_url")
        if self.status == "failed" and self.error_type is None:
            raise ValueError("failed requires error_type")
        return self


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
