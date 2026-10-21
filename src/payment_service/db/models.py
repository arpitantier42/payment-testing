"""ORM models for payment-db (CMP-004).

Mirrors the tables created by the Alembic migrations in ``db/migrations``;
``tests/test_migrations.py`` checks the two stay in step.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Text,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "%(table_name)s_%(column_0_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}

# jsonb on Postgres, plain JSON elsewhere (SQLite in tests).
JSONPayload = JSON().with_variant(postgresql.JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentStatus(StrEnum):
    INITIATED = "initiated"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


class PaymentMethod(StrEnum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"


def _in_list(column: str, values: type[StrEnum]) -> str:
    quoted = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Payment(Base):
    """ENT-001: one attempt to pay for an order, from initiation to confirmed outcome."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in_list("status", PaymentStatus), name="status"),
        CheckConstraint(_in_list("payment_method", PaymentMethod), name="payment_method"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("length(currency) = 3", name="currency"),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_gateway_reference", "gateway_reference"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Order is owned by the external order-service, so there is no FK to an orders table.
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=PaymentStatus.INITIATED.value)
    payment_method: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # minor units
    currency: Mapped[str] = mapped_column(Text, nullable=False)  # ISO 4217
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    gateway_reference: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise in the shape of the OpenAPI ``Payment`` schema."""
        return {
            "id": str(self.id),
            "order_id": str(self.order_id),
            "status": self.status,
            "payment_method": self.payment_method,
            "amount": self.amount,
            "currency": self.currency,
            "idempotency_key": self.idempotency_key,
            "checkout_url": self.checkout_url,
            "gateway_reference": self.gateway_reference,
            "failure_reason": self.failure_reason,
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }


def _iso_utc(value: datetime) -> str:
    # SQLite returns naive datetimes; every timestamp we store is UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class GatewayWebhookEvent(Base):
    """ENT-003: one inbound gateway notification, kept for verification and audit."""

    __tablename__ = "gateway_webhook_events"
    __table_args__ = (Index("ix_gateway_webhook_events_payment_id", "payment_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("payments.id"), nullable=False)
    gateway_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONPayload, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
