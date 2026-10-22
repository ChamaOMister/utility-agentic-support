"""support schema: sessions, conversations, identity, turns, items, tool calls, budgets, llm calls

Revision ID: 0001
Revises:
"""

import os
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

S = "support"
UUID_DEFAULT = sa.text("gen_random_uuid()")
NOW = sa.text("now()")
MONEY = sa.Numeric(12, 6)
ZERO = sa.text("0")


def _ts(name: str, *, nullable: bool = True, default=None) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=nullable, server_default=default
    )


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=UUID_DEFAULT
    )


def _money(name: str, *, default=ZERO) -> sa.Column:
    return sa.Column(name, MONEY, nullable=False, server_default=default)


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        _uuid_pk(),
        sa.Column("token_sha256", pg.BYTEA, nullable=False, unique=True),
        _ts("created_at", nullable=False, default=NOW),
        _ts("expires_at", nullable=False),
        schema=S,
    )
    op.create_table(
        "conversations",
        _uuid_pk(),
        sa.Column(
            "browser_session_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.browser_sessions.id"),
            nullable=False,
        ),
        _ts("created_at", nullable=False, default=NOW),
        sa.Column("turn_count", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("active_turn_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("claim_version", sa.BigInteger, nullable=False, server_default=ZERO),
        _ts("active_turn_lease_expires_at"),
        sa.Column("live_blocked_reason", sa.Text, nullable=True),
        schema=S,
    )
    op.create_index(
        "ix_conversations_browser_session",
        "conversations",
        ["browser_session_id"],
        schema=S,
    )
    op.create_table(
        "conversation_auth",
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            primary_key=True,
        ),
        sa.Column("assurance_level", sa.Text, nullable=False, server_default="none"),
        sa.Column("customer_ref", sa.Text, nullable=True),
        sa.Column("first_name", sa.Text, nullable=True),
        _ts("verified_at"),
        _ts("verified_expires_at"),
        sa.Column("failed_attempts", sa.Integer, nullable=False, server_default=ZERO),
        _ts("window_started_at"),
        _ts("locked_until"),
        sa.CheckConstraint(
            "assurance_level IN ('none', 'kba', 'step_up')",
            name="ck_conversation_auth_assurance",
        ),
        schema=S,
    )
    op.create_table(
        "identifier_lockouts",
        sa.Column("identifier_hash", sa.Text, primary_key=True),
        sa.Column("failed_count", sa.Integer, nullable=False, server_default=ZERO),
        _ts("window_started_at"),
        _ts("locked_until"),
        schema=S,
    )
    op.create_table(
        "turns",
        _uuid_pk(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            nullable=False,
        ),
        sa.Column("client_message_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("text_sha256", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="in_progress"),
        sa.Column("reply", sa.Text, nullable=True),
        sa.Column("correlation_id", sa.Text, nullable=False),
        _ts("created_at", nullable=False, default=NOW),
        _ts("finished_at"),
        sa.UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_turns_conversation_client_message",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'interrupted', 'failed')",
            name="ck_turns_status",
        ),
        schema=S,
    )
    op.create_table(
        "verification_attempts",
        _uuid_pk(),
        sa.Column("identifier_hash", sa.Text, nullable=False),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.turns.id"),
            nullable=True,
        ),
        sa.Column("status", sa.Text, nullable=False),
        _ts("created_at", nullable=False, default=NOW),
        _ts("expires_at", nullable=False),
        sa.CheckConstraint(
            "status IN ('in_flight', 'failed', 'verified', 'upstream_error', 'abandoned')",
            name="ck_verification_attempts_status",
        ),
        schema=S,
    )
    op.create_index(
        "ix_verification_attempts_identifier", "verification_attempts",
        ["identifier_hash", "status", "expires_at"], schema=S,
    )  # fmt: skip
    op.create_index(
        "ix_verification_attempts_conversation", "verification_attempts",
        ["conversation_id", "status", "expires_at"], schema=S,
    )  # fmt: skip
    op.create_table(
        "conversation_items",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.turns.id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.UniqueConstraint("turn_id", "seq", name="uq_conversation_items_turn_seq"),
        sa.CheckConstraint(
            "kind IN ('user_message', 'assistant_message', 'function_call', 'function_call_output', 'reasoning')",
            name="ck_conversation_items_kind",
        ),
        schema=S,
    )
    op.create_index(
        "ix_conversation_items_conversation",
        "conversation_items",
        ["conversation_id", "id"],
        schema=S,
    )
    op.create_table(
        "tool_calls",
        _uuid_pk(),
        sa.Column(
            "turn_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.turns.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=ZERO),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("record_count", sa.Integer, nullable=True),
        _ts("created_at", nullable=False, default=NOW),
        schema=S,
    )
    op.create_index("ix_tool_calls_turn", "tool_calls", ["turn_id"], schema=S)
    op.create_table(
        "budget_accounts",
        sa.Column("id", sa.Text, primary_key=True),
        _money("cap_usd", default=None),
        _money("spent_usd"),
        _money("reserved_usd"),
        _money("uncertain_usd"),
        schema=S,
    )
    op.create_table(
        "conversation_budgets",
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            primary_key=True,
        ),
        _money("cap_usd", default=None),
        _money("spent_usd"),
        _money("reserved_usd"),
        _money("uncertain_usd"),
        schema=S,
    )
    op.create_table(
        "llm_calls",
        _uuid_pk(),
        sa.Column(
            "conversation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.conversations.id"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.turns.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("service_tier", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("outcome_class", sa.Text, nullable=True),
        sa.Column("reserved_usd", MONEY, nullable=False, server_default=ZERO),
        sa.Column("estimated_usd", MONEY, nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("provider_request_id", sa.Text, nullable=True),
        _ts("created_at", nullable=False, default=NOW),
        sa.CheckConstraint(
            "kind IN ('generation', 'token_count')", name="ck_llm_calls_kind"
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'settled', 'released', 'uncertain')",
            name="ck_llm_calls_status",
        ),
        schema=S,
    )
    op.create_index(
        "ix_llm_calls_conversation",
        "llm_calls",
        ["conversation_id", "status"],
        schema=S,
    )

    cap = Decimal(os.environ.get("PROJECT_BUDGET_USD", "20"))
    op.execute(
        sa.text(
            f"INSERT INTO {S}.budget_accounts (id, cap_usd) VALUES ('project', :cap)"
        ).bindparams(cap=cap)
    )


def downgrade() -> None:
    for table in (
        "llm_calls", "conversation_budgets", "budget_accounts", "tool_calls", "conversation_items",
        "verification_attempts", "turns", "identifier_lockouts", "conversation_auth", "conversations",
        "browser_sessions",
    ):  # fmt: skip
        op.drop_table(table, schema=S)
