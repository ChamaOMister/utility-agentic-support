"""SQLAlchemy Core table definitions for the `billing` schema.

The Alembic migration is written by hand and does not import this module, so
later edits here never silently change what an old migration does.
"""

import sqlalchemy as sa

SCHEMA = "billing"
metadata = sa.MetaData(schema=SCHEMA)

customers = sa.Table(
    "customers",
    metadata,
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
)

installations = sa.Table(
    "installations",
    metadata,
    sa.Column("installation_id", sa.Text, primary_key=True),
    sa.Column(
        "customer_ref",
        sa.Text,
        sa.ForeignKey("billing.customers.customer_ref"),
        nullable=False,
    ),
    sa.Column("label", sa.Text, nullable=False),
)

bills = sa.Table(
    "bills",
    metadata,
    sa.Column("bill_id", sa.Text, primary_key=True),
    sa.Column(
        "customer_ref",
        sa.Text,
        sa.ForeignKey("billing.customers.customer_ref"),
        nullable=False,
    ),
    sa.Column(
        "installation_id",
        sa.Text,
        sa.ForeignKey("billing.installations.installation_id"),
        nullable=False,
    ),
    sa.Column("reference_month", sa.String(7), nullable=False),
    sa.Column("issue_date", sa.Date, nullable=False),
    sa.Column("due_date", sa.Date, nullable=False),
    sa.Column("amount_brl", sa.Numeric(12, 2), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("consumption_kwh", sa.Integer, nullable=False),
    sa.Column("tariff_flag", sa.Text, nullable=False),
    sa.CheckConstraint("status IN ('open', 'paid', 'overdue')", name="ck_bills_status"),
)

bill_line_items = sa.Table(
    "bill_line_items",
    metadata,
    sa.Column(
        "bill_id",
        sa.Text,
        sa.ForeignKey("billing.bills.bill_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("amount_brl", sa.Numeric(12, 2), nullable=False),
)
