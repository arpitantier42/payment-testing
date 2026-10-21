"""Data access for payment-db.

The functions take a ``Session`` and flush but never commit: the caller (the
API layer) owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payment_service.db.models import (
    GatewayWebhookEvent,
    Payment,
    PaymentMethod,
    PaymentStatus,
    utcnow,
)

TERMINAL_STATUSES = frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED})


class PaymentValidationError(ValueError):
    """Order id, amount, payment method, currency or key is missing or invalid (HTTP 400)."""


class IdempotencyConflict(Exception):
    """The idempotency key was already used with a different order or amount (HTTP 409)."""


class InvalidStatusTransition(Exception):
    """The payment is already in a terminal state, or the update is inconsistent."""


def _parse_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def validate_new_payment(
    order_id: uuid.UUID | str | None,
    payment_method: str | None,
    amount: int | None,
    currency: str | None,
    idempotency_key: str | None,
) -> uuid.UUID:
    parsed_order_id = _parse_uuid(order_id)
    if parsed_order_id is None:
        raise PaymentValidationError("order_id is missing or not a valid uuid")
    if payment_method not in {m.value for m in PaymentMethod}:
        raise PaymentValidationError("payment_method must be one of upi, card, netbanking")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise PaymentValidationError("amount must be a positive integer in minor units")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise PaymentValidationError("currency must be a 3-letter ISO 4217 code")
    if not idempotency_key or not idempotency_key.strip():
        raise PaymentValidationError("idempotency_key is required")
    return parsed_order_id


def get_payment(session: Session, payment_id: uuid.UUID | str | None) -> Payment | None:
    """Return the payment, or None for an unknown or malformed id (HTTP 404)."""
    parsed = _parse_uuid(payment_id)
    if parsed is None:
        return None
    return session.get(Payment, parsed)


def get_payment_by_idempotency_key(session: Session, idempotency_key: str) -> Payment | None:
    return session.scalars(
        select(Payment).where(Payment.idempotency_key == idempotency_key)
    ).one_or_none()


def get_payment_by_gateway_reference(session: Session, gateway_reference: str) -> Payment | None:
    return session.scalars(
        select(Payment).where(Payment.gateway_reference == gateway_reference)
    ).first()


def _replay(existing: Payment, order_id: uuid.UUID, amount: int) -> Payment:
    if existing.order_id != order_id or existing.amount != amount:
        raise IdempotencyConflict(
            "idempotency key already used with a different order or amount"
        )
    return existing


def create_payment(
    session: Session,
    *,
    order_id: uuid.UUID | str | None,
    payment_method: str | None,
    amount: int | None,
    currency: str | None,
    idempotency_key: str | None,
) -> tuple[Payment, bool]:
    """Insert a payment in status ``initiated``, at most once per idempotency key.

    Returns ``(payment, created)``. A replay of the same key for the same order
    and amount returns the original payment with ``created=False``; the unique
    constraint on ``idempotency_key`` settles concurrent inserts.
    """
    parsed_order_id = validate_new_payment(
        order_id, payment_method, amount, currency, idempotency_key
    )
    assert idempotency_key is not None and amount is not None

    existing = get_payment_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        return _replay(existing, parsed_order_id, amount), False

    payment = Payment(
        order_id=parsed_order_id,
        status=PaymentStatus.INITIATED.value,
        payment_method=payment_method,
        amount=amount,
        currency=currency.upper(),
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(payment)
            session.flush()
    except IntegrityError:
        # Another request inserted the same key between our read and write.
        existing = get_payment_by_idempotency_key(session, idempotency_key)
        if existing is None:
            raise
        return _replay(existing, parsed_order_id, amount), False
    return payment, True


def _ensure_not_terminal(payment: Payment) -> None:
    if payment.status in TERMINAL_STATUSES:
        raise InvalidStatusTransition(f"payment {payment.id} is already {payment.status}")


def record_gateway_acceptance(
    session: Session, payment: Payment, *, checkout_url: str, gateway_reference: str
) -> Payment:
    """Store the gateway's checkout destination and transaction id; status -> processing."""
    _ensure_not_terminal(payment)
    payment.checkout_url = checkout_url
    payment.gateway_reference = gateway_reference
    payment.status = PaymentStatus.PROCESSING.value
    session.flush()
    return payment


def update_payment_status(
    session: Session,
    payment: Payment,
    status: PaymentStatus | str,
    *,
    failure_reason: str | None = None,
) -> Payment:
    """Backend-only status change. Terminal states (success, failed) are final.

    ``failure_reason`` is required for ``failed`` and rejected otherwise.
    """
    new_status = PaymentStatus(status)
    _ensure_not_terminal(payment)
    if new_status is PaymentStatus.FAILED and not failure_reason:
        raise InvalidStatusTransition("failure_reason is required when status is failed")
    if new_status is not PaymentStatus.FAILED and failure_reason:
        raise InvalidStatusTransition("failure_reason is only allowed when status is failed")
    payment.status = new_status.value
    payment.failure_reason = failure_reason
    session.flush()
    return payment


def record_webhook_event(
    session: Session,
    *,
    payment_id: uuid.UUID,
    gateway_event_id: str,
    event_type: str,
    signature_valid: bool,
    raw_payload: dict[str, Any],
) -> tuple[GatewayWebhookEvent, bool]:
    """Store an inbound gateway event once per ``gateway_event_id``.

    Returns ``(event, created)``; a redelivered event returns the stored row with
    ``created=False`` so the caller can skip re-applying it. A stored event whose
    signature failed is replaced by a later verified delivery (``created=True``),
    so a forged request cannot block the genuine event by claiming its id first.
    """
    existing = get_webhook_event_by_gateway_id(session, gateway_event_id)
    if existing is not None:
        if signature_valid and not existing.signature_valid:
            existing.payment_id = payment_id
            existing.event_type = event_type
            existing.signature_valid = True
            existing.raw_payload = raw_payload
            existing.received_at = utcnow()
            session.flush()
            return existing, True
        return existing, False

    event = GatewayWebhookEvent(
        payment_id=payment_id,
        gateway_event_id=gateway_event_id,
        event_type=event_type,
        signature_valid=signature_valid,
        raw_payload=raw_payload,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        existing = get_webhook_event_by_gateway_id(session, gateway_event_id)
        if existing is None:
            raise
        return existing, False
    return event, True


def get_webhook_event_by_gateway_id(
    session: Session, gateway_event_id: str
) -> GatewayWebhookEvent | None:
    return session.scalars(
        select(GatewayWebhookEvent).where(
            GatewayWebhookEvent.gateway_event_id == gateway_event_id
        )
    ).one_or_none()


def mark_webhook_event_processed(session: Session, event: GatewayWebhookEvent) -> GatewayWebhookEvent:
    """Set ``processed_at``; only events with a verified signature may be applied."""
    if not event.signature_valid:
        raise InvalidStatusTransition("cannot process a webhook event with an invalid signature")
    event.processed_at = utcnow()
    session.flush()
    return event
