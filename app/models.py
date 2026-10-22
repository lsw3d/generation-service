import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LedgerKind(str, enum.Enum):
    payment = "payment"
    charge = "charge"
    refund = "refund"


class TaskStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"


class AttemptStatus(str, enum.Enum):
    sending = "sending"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"


class ErrorType(str, enum.Enum):
    technical = "technical"
    moderation = "moderation"
    payload = "payload"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="users_balance_nonnegative"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="user")
    tasks: Mapped[list["Task"]] = relationship(back_populates="user")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[LedgerKind] = mapped_column(
        Enum(LedgerKind, native_enum=False, length=32), nullable=False
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="ledger_entries")


class GenerationModel(Base):
    __tablename__ = "models"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    price_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    legs: Mapped[list["Leg"]] = relationship(
        back_populates="model", order_by="Leg.priority"
    )


class Leg(Base):
    __tablename__ = "legs"
    __table_args__ = (UniqueConstraint("model_slug", "priority", name="uq_leg_priority"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("models.slug"), nullable=False
    )
    provider_name: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    max_ref_images: Mapped[int] = mapped_column(Integer, nullable=False)
    skip_on_moderation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    model: Mapped[GenerationModel] = relationship(back_populates="legs")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    model_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("models.slug"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    ref_images: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False, length=32), nullable=False
    )
    current_leg_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="tasks")
    attempts: Mapped[list["Attempt"]] = relationship(
        back_populates="task", order_by="Attempt.created_at"
    )


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    provider_name: Mapped[str] = mapped_column(String(32), nullable=False)
    leg_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[AttemptStatus] = mapped_column(
        Enum(AttemptStatus, native_enum=False, length=32), nullable=False
    )
    error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    waiting_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    task: Mapped[Task] = relationship(back_populates="attempts")
