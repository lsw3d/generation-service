"""Increment user balance; ledger is append-only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22

SUM(ledger) in a trigger races: the second transaction recomputes from a snapshot
that does not yet include the first insert. Adding NEW.amount under the users row
lock serializes credits and refunds. UPDATE/DELETE on the ledger are forbidden so
every balance change stays an insert.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_user_balance() RETURNS trigger AS $$
        BEGIN
            UPDATE users
            SET balance = balance + NEW.amount
            WHERE id = NEW.user_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        UPDATE users
        SET balance = COALESCE((
            SELECT SUM(amount) FROM ledger_entries WHERE user_id = users.id
        ), 0)
        """
    )
    op.execute(
        """
        CREATE FUNCTION forbid_ledger_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger_entries are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER ledger_no_update
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_no_update ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS forbid_ledger_mutation()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_user_balance() RETURNS trigger AS $$
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
