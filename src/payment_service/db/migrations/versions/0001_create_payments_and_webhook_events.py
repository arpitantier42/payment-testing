"""Create payments and gateway_webhook_events (WI-001).

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONPayload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("gateway_reference", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="payments_pkey"),
        sa.UniqueConstraint("idempotency_key", name="payments_idempotency_key_key"),
        sa.CheckConstraint(
            "status IN ('initiated', 'processing', 'success', 'failed', 'pending')",
            name=op.f("payments_status_check"),
        ),
        sa.CheckConstraint(
            "payment_method IN ('upi', 'card', 'netbanking')",
            name=op.f("payments_payment_method_check"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("payments_amount_check")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("payments_currency_check")),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index("ix_payments_gateway_reference", "payments", ["gateway_reference"])

    op.create_table(
        "gateway_webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("gateway_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("raw_payload", JSONPayload, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="gateway_webhook_events_pkey"),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name="gateway_webhook_events_payment_id_fkey"
        ),
        sa.UniqueConstraint(
            "gateway_event_id", name="gateway_webhook_events_gateway_event_id_key"
        ),
    )
    op.create_index(
        "ix_gateway_webhook_events_payment_id", "gateway_webhook_events", ["payment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_gateway_webhook_events_payment_id", table_name="gateway_webhook_events")
    op.drop_table("gateway_webhook_events")
    op.drop_index("ix_payments_gateway_reference", table_name="payments")
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")
