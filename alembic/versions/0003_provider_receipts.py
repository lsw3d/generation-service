"""Persist provider acceptance so replay after a dispatch crash is idempotent.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_receipts",
        sa.Column("provider_name", sa.String(32), primary_key=True),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.func.gen_random_uuid()),
    )


def downgrade() -> None:
    op.drop_table("provider_receipts")
