"""Add the orders projection, align payment columns with the data model (WI-1).

- ``orders``: local, payment-facing projection of an order from order-service,
  carrying the server-issued ``payment_idempotency_key``.
- ``payments``: ``amount`` -> ``amount_minor``, ``checkout_url`` ->
  ``gateway_checkout_url``, ``gateway_reference`` -> ``gateway_session_id``,
  and ``order_id`` now references ``orders.id``. Payments that already exist get
  a placeholder projection so the foreign key can be added.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONPayload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("available_payment_methods", JSONPayload, nullable=False),
        sa.Column("payment_idempotency_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="orders_pkey"),
        sa.UniqueConstraint(
            "payment_idempotency_key", name="orders_payment_idempotency_key_key"
        ),
        sa.CheckConstraint("amount_minor > 0", name=op.f("orders_amount_minor_check")),
        sa.CheckConstraint("length(currency) = 3", name=op.f("orders_currency_check")),
    )

    # Placeholder projections for payments created before this table existed.
    op.execute(
        """
        INSERT INTO orders (id, summary, amount_minor, currency, available_payment_methods, created_at)
        SELECT order_id, '', MAX(amount), MAX(currency), '[]', MIN(created_at)
        FROM payments GROUP BY order_id
        """
    )

    op.drop_index("ix_payments_gateway_reference", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint(op.f("payments_amount_check"), type_="check")
        batch.alter_column("amount", new_column_name="amount_minor", existing_type=sa.BigInteger(), existing_nullable=False)
        batch.alter_column("checkout_url", new_column_name="gateway_checkout_url", existing_type=sa.Text(), existing_nullable=True)
        batch.alter_column("gateway_reference", new_column_name="gateway_session_id", existing_type=sa.Text(), existing_nullable=True)
        batch.create_check_constraint(op.f("payments_amount_minor_check"), "amount_minor > 0")
        batch.create_foreign_key("payments_order_id_fkey", "orders", ["order_id"], ["id"])
    op.create_index("ix_payments_gateway_session_id", "payments", ["gateway_session_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_gateway_session_id", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("payments_order_id_fkey", type_="foreignkey")
        batch.drop_constraint(op.f("payments_amount_minor_check"), type_="check")
        batch.alter_column("amount_minor", new_column_name="amount", existing_type=sa.BigInteger(), existing_nullable=False)
        batch.alter_column("gateway_checkout_url", new_column_name="checkout_url", existing_type=sa.Text(), existing_nullable=True)
        batch.alter_column("gateway_session_id", new_column_name="gateway_reference", existing_type=sa.Text(), existing_nullable=True)
        batch.create_check_constraint(op.f("payments_amount_check"), "amount > 0")
    op.create_index("ix_payments_gateway_reference", "payments", ["gateway_reference"])
    op.drop_table("orders")
