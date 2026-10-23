"""Money CHECK constraints on the budget ledger: no negative amounts, ever

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

S = "support"
_BUCKETS = ("cap_usd", "spent_usd", "reserved_usd", "uncertain_usd")
CHECKS = {
    "budget_accounts": {
        f"ck_budget_accounts_{c}_nonneg": f"{c} >= 0" for c in _BUCKETS
    },
    "conversation_budgets": {
        f"ck_conversation_budgets_{c}_nonneg": f"{c} >= 0" for c in _BUCKETS
    },
    "llm_calls": {
        "ck_llm_calls_reserved_usd_nonneg": "reserved_usd >= 0",
        "ck_llm_calls_estimated_usd_nonneg": "estimated_usd IS NULL OR estimated_usd >= 0",
        "ck_llm_calls_tokens_nonneg": "(input_tokens IS NULL OR input_tokens >= 0) "
        "AND (output_tokens IS NULL OR output_tokens >= 0)",
    },
}


def upgrade() -> None:
    for table, checks in CHECKS.items():
        for name, condition in checks.items():
            op.create_check_constraint(name, table, condition, schema=S)


def downgrade() -> None:
    for table, checks in CHECKS.items():
        for name in checks:
            op.drop_constraint(name, table, type_="check", schema=S)
