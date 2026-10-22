"""billing schema: customers, installations, bills, bill_line_items

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = "billing"


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("customer_ref", sa.Text, primary_key=True),
        sa.Column("first_name", sa.Text, nullable=False),
        sa.Column("full_name", sa.Text, nullable=False),
        sa.Column("cpf", sa.String(11), nullable=False, unique=True),
        sa.Column("account_number", sa.String(10), nullable=False, unique=True),
        sa.Column("birth_date", sa.Date, nullable=False),
        sa.Column("phone", sa.Text, nullable=False),
        sa.Column("street", sa.Text, nullable=False),
        sa.Column("neighborhood", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("state", sa.String(2), nullable=False),
        sa.Column("tariff_class", sa.Text, nullable=False),
        sa.Column("account_status", sa.Text, nullable=False),
        schema=S,
    )
    op.create_table(
        "installations",
        sa.Column("installation_id", sa.Text, primary_key=True),
        sa.Column(
            "customer_ref",
            sa.Text,
            sa.ForeignKey(f"{S}.customers.customer_ref"),
            nullable=False,
        ),
        sa.Column("label", sa.Text, nullable=False),
        schema=S,
    )
    op.create_table(
        "bills",
        sa.Column("bill_id", sa.Text, primary_key=True),
        sa.Column(
            "customer_ref",
            sa.Text,
            sa.ForeignKey(f"{S}.customers.customer_ref"),
            nullable=False,
        ),
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey(f"{S}.installations.installation_id"),
            nullable=False,
        ),
        sa.Column("reference_month", sa.String(7), nullable=False),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("amount_brl", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("consumption_kwh", sa.Integer, nullable=False),
        sa.Column("tariff_flag", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'paid', 'overdue')", name="ck_bills_status"
        ),
        schema=S,
    )
    op.create_index(
        "ix_bills_customer_month",
        "bills",
        ["customer_ref", sa.text("reference_month DESC")],
        schema=S,
    )
    op.create_table(
        "bill_line_items",
        sa.Column(
            "bill_id",
            sa.Text,
            sa.ForeignKey(f"{S}.bills.bill_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("amount_brl", sa.Numeric(12, 2), nullable=False),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("bill_line_items", schema=S)
    op.drop_index("ix_bills_customer_month", table_name="bills", schema=S)
    op.drop_table("bills", schema=S)
    op.drop_table("installations", schema=S)
    op.drop_table("customers", schema=S)
