"""Data access for payments-db (orders projection, payments, webhook events).

The functions take a ``Session`` and flush but never commit: the caller (the
API layer) owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payment_service.db.models import (
    GatewayWebhookEvent,
    Order,
    Payment,
    PaymentMethod,
    PaymentStatus,
    utcnow,
)

TERMINAL_STATUSES = frozenset({PaymentStatus.SUCCESS, PaymentStatus.FAILED})


class PaymentValidationError(ValueError):
    """Order id, payment method or key is missing or invalid, or order data is malformed (HTTP 400)."""


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different order (HTTP 409)."""


class OrderNotFound(LookupError):
    """No order projection exists for the id (HTTP 404)."""


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


def _new_key() -> str:
    return f"idem_{uuid.uuid4().hex}"


# --- order projection (ENT-002) --------------------------------------------------------


def validate_order_projection(
    summary: str | None,
    amount_minor: int | None,
    currency: str | None,
    available_payment_methods: list[str] | None,
) -> None:
    if not isinstance(summary, str):
        raise PaymentValidationError("order summary is missing")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
        raise PaymentValidationError("order amount_minor must be a positive integer")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise PaymentValidationError("order currency must be a 3-letter ISO 4217 code")
    methods = available_payment_methods or []
    if not methods or not set(methods) <= {m.value for m in PaymentMethod}:
        raise PaymentValidationError("available_payment_methods must list upi, card and/or netbanking")


def get_order(session: Session, order_id: uuid.UUID | str | None, *, for_update: bool = False) -> Order | None:
    """Return the cached projection, or None for an unknown or malformed id."""
    parsed = _parse_uuid(order_id)
    if parsed is None:
        return None
    return session.get(Order, parsed, with_for_update=for_update or None)


def upsert_order_projection(
    session: Session,
    *,
    order_id: uuid.UUID,
    summary: str,
    amount_minor: int,
    currency: str,
    available_payment_methods: list[str],
) -> Order:
    """Store the projection the first time an order is seen; an existing one is kept.

    Orders are assumed immutable once placed (HLD RSK-002), so a cached projection is
    never overwritten. Concurrent first fetches resolve to one row via the primary key.
    """
    existing = get_order(session, order_id)
    if existing is not None:
        return existing
    validate_order_projection(summary, amount_minor, currency, available_payment_methods)
    order = Order(
        id=order_id,
        summary=summary,
        amount_minor=amount_minor,
        currency=currency.upper(),
        available_payment_methods=list(available_payment_methods),
    )
    try:
        with session.begin_nested():
            session.add(order)
            session.flush()
    except IntegrityError:
        existing = get_order(session, order_id)
        if existing is None:
            raise
        return existing
    return order


def ensure_payment_idempotency_key(
    session: Session, order: Order, *, new_key: Callable[[], str] = _new_key
) -> str:
    """Issue the order's payment key, or rotate it once the attempt made with it failed.

    - no key yet -> issue one;
    - a payment exists for the key and is ``failed`` -> issue a fresh key (safe retry);
    - otherwise (unused, in flight, pending or succeeded) -> keep the key, so a repeat
      submission resolves to the same payment and a success is never charged twice.

    The row is locked (``FOR UPDATE`` on Postgres) so concurrent calls agree on one key.
    """
    locked = get_order(session, order.id, for_update=True) or order
    key = locked.payment_idempotency_key
    if key is not None:
        payment = get_payment_by_idempotency_key(session, key)
        if payment is None or payment.status != PaymentStatus.FAILED.value:
            return key
    locked.payment_idempotency_key = new_key()
    session.flush()
    return locked.payment_idempotency_key


# --- payments (ENT-001) -------------------------------------------------------------


def validate_new_payment(
    order_id: uuid.UUID | str | None,
    payment_method: str | None,
    idempotency_key: str | None,
) -> uuid.UUID:
    parsed_order_id = _parse_uuid(order_id)
    if parsed_order_id is None:
        raise PaymentValidationError("order_id is missing or not a valid uuid")
    if payment_method not in {m.value for m in PaymentMethod}:
        raise PaymentValidationError("payment_method must be one of upi, card, netbanking")
    if not idempotency_key or not idempotency_key.strip():
        raise PaymentValidationError("Idempotency-Key is required")
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


def get_payment_by_gateway_session_id(session: Session, gateway_session_id: str) -> Payment | None:
    return session.scalars(
        select(Payment).where(Payment.gateway_session_id == gateway_session_id)
    ).first()


def _replay(existing: Payment, order_id: uuid.UUID) -> Payment:
    if existing.order_id != order_id:
        raise IdempotencyConflict("idempotency key already used for a different order")
    return existing


def create_payment(
    session: Session,
    *,
    order_id: uuid.UUID | str | None,
    payment_method: str | None,
    idempotency_key: str | None,
) -> tuple[Payment, bool]:
    """Insert a payment in status ``initiated``, at most once per idempotency key.

    Amount and currency are copied from the order projection, never taken from the
    caller. A new payment needs the key currently issued on the order
    (``ensure_payment_idempotency_key``); a replay of a key already used for this order
    returns the original payment with ``created=False``. The unique constraint on
    ``idempotency_key`` settles concurrent inserts.
    """
    parsed_order_id = validate_new_payment(order_id, payment_method, idempotency_key)
    assert idempotency_key is not None

    existing = get_payment_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        return _replay(existing, parsed_order_id), False

    order = get_order(session, parsed_order_id)
    if order is None:
        raise OrderNotFound(f"order {parsed_order_id} has no projection")
    if payment_method not in order.available_payment_methods:
        raise PaymentValidationError("payment_method is not available for this order")
    if order.payment_idempotency_key is None or idempotency_key != order.payment_idempotency_key:
        raise PaymentValidationError(
            "Idempotency-Key is not the one issued for this order; reload the order and try again"
        )

    payment = Payment(
        order_id=parsed_order_id,
        status=PaymentStatus.INITIATED.value,
        payment_method=payment_method,
        amount_minor=order.amount_minor,
        currency=order.currency,
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
        return _replay(existing, parsed_order_id), False
    return payment, True


def _ensure_not_terminal(payment: Payment) -> None:
    if payment.status in TERMINAL_STATUSES:
        raise InvalidStatusTransition(f"payment {payment.id} is already {payment.status}")


def record_gateway_acceptance(
    session: Session, payment: Payment, *, gateway_checkout_url: str, gateway_session_id: str
) -> Payment:
    """Store the gateway's checkout destination and session id; status -> processing."""
    _ensure_not_terminal(payment)
    payment.gateway_checkout_url = gateway_checkout_url
    payment.gateway_session_id = gateway_session_id
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
