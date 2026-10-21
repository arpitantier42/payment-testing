"""The database itself enforces the integrity rules, independent of the repository."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from payment_service.db.models import GatewayWebhookEvent, Order, Payment

ORDER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


def _order(**overrides):
    values = dict(
        id=ORDER_ID,
        summary="1 x Mug",
        amount_minor=49900,
        currency="INR",
        available_payment_methods=["upi", "card", "netbanking"],
    )
    values.update(overrides)
    return Order(**values)


@pytest.fixture(autouse=True)
def order(session):
    o = _order()
    session.add(o)
    session.flush()
    return o


def _payment(**overrides):
    values = dict(
        order_id=ORDER_ID,
        status="initiated",
        payment_method="upi",
        amount_minor=49900,
        currency="INR",
        idempotency_key=str(uuid.uuid4()),
    )
    values.update(overrides)
    return Payment(**values)


def test_valid_payment_persists_with_defaults(session):
    p = _payment()
    session.add(p)
    session.flush()
    assert isinstance(p.id, uuid.UUID)
    assert p.created_at is not None and p.updated_at is not None
    assert p.gateway_checkout_url is None and p.gateway_session_id is None and p.failure_reason is None


def test_duplicate_idempotency_key_is_rejected(session):
    session.add(_payment(idempotency_key="k-1"))
    session.flush()
    session.add(_payment(idempotency_key="k-1"))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "paid"},
        {"payment_method": "cash"},
        {"amount_minor": 0},
        {"amount_minor": -100},
        {"currency": "RUPEE"},
    ],
)
def test_invalid_values_are_rejected(session, overrides):
    session.add(_payment(**overrides))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("column", ["order_id", "payment_method", "amount_minor", "currency", "idempotency_key"])
def test_required_payment_columns_are_not_null(session, column):
    session.add(_payment(**{column: None}))
    with pytest.raises(IntegrityError):
        session.flush()


def _event(payment_id, **overrides):
    values = dict(
        payment_id=payment_id,
        gateway_event_id=str(uuid.uuid4()),
        event_type="payment.success",
        signature_valid=True,
        raw_payload={"id": "evt", "data": {"amount": 49900}},
    )
    values.update(overrides)
    return GatewayWebhookEvent(**values)


def test_webhook_event_stores_json_payload(session):
    p = _payment()
    session.add(p)
    session.flush()
    e = _event(p.id, raw_payload={"nested": {"list": [1, 2]}, "ok": True})
    session.add(e)
    session.commit()
    session.expire_all()
    stored = session.get(GatewayWebhookEvent, e.id)
    assert stored.raw_payload == {"nested": {"list": [1, 2]}, "ok": True}
    assert stored.processed_at is None


def test_webhook_event_requires_existing_payment(session):
    session.add(_event(uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_gateway_event_id_is_rejected(session):
    p = _payment()
    session.add(p)
    session.flush()
    session.add(_event(p.id, gateway_event_id="evt-1"))
    session.flush()
    session.add(_event(p.id, gateway_event_id="evt-1"))
    with pytest.raises(IntegrityError):
        session.flush()


# --- orders projection (ENT-002) -----------------------------------------------------


def test_order_projection_persists_json_methods_and_null_key(session, order):
    session.expire_all()
    stored = session.get(Order, ORDER_ID)
    assert stored.available_payment_methods == ["upi", "card", "netbanking"]
    assert stored.payment_idempotency_key is None
    assert stored.to_dict()["created_at"].endswith("+00:00")


def test_payment_requires_existing_order(session):
    session.add(_payment(order_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.flush()


def test_order_payment_idempotency_key_is_unique(session, order):
    order.payment_idempotency_key = "k-dup"
    session.add(_order(id=uuid.uuid4(), payment_idempotency_key="k-dup"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_orders_without_a_key_may_coexist(session):
    session.add(_order(id=uuid.uuid4()))
    session.flush()


@pytest.mark.parametrize("overrides", [{"amount_minor": 0}, {"currency": "RUPEE"}])
def test_invalid_order_values_are_rejected(session, overrides):
    session.add(_order(id=uuid.uuid4(), **overrides))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("column", ["summary", "amount_minor", "currency", "available_payment_methods"])
def test_required_order_columns_are_not_null(session, column):
    session.add(_order(id=uuid.uuid4(), **{column: None}))
    with pytest.raises(IntegrityError):
        session.flush()
