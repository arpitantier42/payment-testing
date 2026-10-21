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

# jsonb on Postgres, plain JSON elsewhere (SQLite in tests). Python None is stored as
# SQL NULL (not JSON 'null'), so NOT NULL columns reject a missing value.
JSONPayload = JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")


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


class Order(Base):
    """ENT-002: local, payment-facing projection of an order placed through order-service.

    ``payment_idempotency_key`` is the server-issued token the next payment attempt must
    send as ``Idempotency-Key``; it is rotated only once the attempt made with it failed.
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_minor"),
        CheckConstraint("length(currency) = 3", name="currency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # same id as order-service
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    available_payment_methods: Mapped[list[str]] = mapped_column(JSONPayload, nullable=False)
    payment_idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Serialise in the shape of the OpenAPI ``Order`` schema."""
        return {
            "id": str(self.id),
            "summary": self.summary,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "available_payment_methods": list(self.available_payment_methods),
            "payment_idempotency_key": self.payment_idempotency_key,
            "created_at": _iso_utc(self.created_at),
        }


class Payment(Base):
    """ENT-001: one attempt to pay for an order, from initiation to confirmed outcome."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_in_list("status", PaymentStatus), name="status"),
        CheckConstraint(_in_list("payment_method", PaymentMethod), name="payment_method"),
        CheckConstraint("amount_minor > 0", name="amount_minor"),
        CheckConstraint("length(currency) = 3", name="currency"),
        Index("ix_payments_order_id", "order_id"),
        Index("ix_payments_gateway_session_id", "gateway_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # References the local order projection (see Order); order-service stays the owner.
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orders.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=PaymentStatus.INITIATED.value)
    payment_method: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)  # copied from the order
    currency: Mapped[str] = mapped_column(Text, nullable=False)  # ISO 4217
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Nullable because a payment whose gateway initiation was refused is kept as failed.
    gateway_checkout_url: Mapped[str | None] = mapped_column(Text)
    gateway_session_id: Mapped[str | None] = mapped_column(Text)
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
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "idempotency_key": self.idempotency_key,
            "gateway_session_id": self.gateway_session_id,
            "gateway_checkout_url": self.gateway_checkout_url,
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
