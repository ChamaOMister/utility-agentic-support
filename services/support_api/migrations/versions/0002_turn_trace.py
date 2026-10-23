"""turns.trace: stored trace metadata so a duplicate submission replays the same response

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Metadata only (tool names, status, error codes, attempts, timings, counts): no payloads.
    op.add_column(
        "turns", sa.Column("trace", pg.JSONB, nullable=True), schema="support"
    )


def downgrade() -> None:
    op.drop_column("turns", "trace", schema="support")
