"""Create tables, catalog seed, and money invariants in Postgres.

Revision ID: 0001
Revises:
Create Date: 2026-10-21

Exactly-once credits and refunds are unique indexes, not application memory.
The balance trigger makes users.balance a projection of the ledger.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("balance >= 0", name="users_balance_nonnegative"),
    )

    op.create_table(
        "models",
        sa.Column("slug", sa.String(64), primary_key=True),
        sa.Column("price_tokens", sa.Integer(), nullable=False),
    )

    op.create_table(
        "legs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_slug", sa.String(64), sa.ForeignKey("models.slug"), nullable=False),
        sa.Column("provider_name", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("max_ref_images", sa.Integer(), nullable=False),
        sa.Column("skip_on_moderation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("model_slug", "priority", name="uq_leg_priority"),
    )

    op.create_table(
        "tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("model_slug", sa.String(64), sa.ForeignKey("models.slug"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("ref_images", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_leg_priority", sa.Integer(), nullable=True),
        sa.Column("result_url", sa.Text(), nullable=True),
        sa.Column("price_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "ledger_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("payment_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ledger_entries_user_id", "ledger_entries", ["user_id"])

    op.create_table(
        "attempts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("provider_name", sa.String(32), nullable=False),
        sa.Column("leg_priority", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_type", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_attempts_task_id", "attempts", ["task_id"])

    # Concurrent workers may all see the same webhook; uniqueness is the gate.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ledger_payment
        ON ledger_entries (payment_id)
        WHERE payment_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ledger_charge
        ON ledger_entries (task_id)
        WHERE kind = 'charge' AND task_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ledger_refund
        ON ledger_entries (task_id)
        WHERE kind = 'refund' AND task_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_open_attempt
        ON attempts (task_id)
        WHERE status IN ('sending', 'waiting')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_attempt_external
        ON attempts (provider_name, external_id)
        WHERE external_id IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE FUNCTION sync_user_balance() RETURNS trigger AS $$
        BEGIN
            UPDATE users
            SET balance = COALESCE((
                SELECT SUM(amount) FROM ledger_entries WHERE user_id = NEW.user_id
            ), 0)
            WHERE id = NEW.user_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_sync_balance
        AFTER INSERT ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION sync_user_balance()
        """
    )

    op.execute(
        """
        INSERT INTO models (slug, price_tokens) VALUES
            ('image-fast', 2),
            ('video-long', 25)
        """
    )
    op.execute(
        """
        INSERT INTO legs (model_slug, provider_name, priority, max_ref_images, skip_on_moderation)
        VALUES
            ('image-fast', 'alpha', 1, 4, false),
            ('image-fast', 'beta', 2, 10, false),
            ('video-long', 'beta', 1, 2, false),
            ('video-long', 'alpha', 2, 1, true)
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_sync_balance ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS sync_user_balance()")
    op.drop_table("attempts")
    op.drop_table("ledger_entries")
    op.drop_table("tasks")
    op.drop_table("legs")
    op.drop_table("models")
    op.drop_table("users")
