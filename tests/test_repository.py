import threading
import uuid

import pytest
from sqlalchemy import func, select

from payment_service.db import repository as repo
from payment_service.db.models import Order, Payment, PaymentStatus

ORDER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _project(session, order_id=ORDER_ID, **overrides):
    values = dict(
        order_id=order_id,
        summary="1 x Lamp",
        amount_minor=125000,
        currency="inr",
        available_payment_methods=["upi", "card", "netbanking"],
    )
    values.update(overrides)
    return repo.upsert_order_projection(session, **values)


@pytest.fixture(autouse=True)
def order(session_factory):
    """A committed projection whose issued key is ``key-1``."""
    with session_factory() as s:
        projected = _project(s)
        repo.ensure_payment_idempotency_key(s, projected, new_key=lambda: "key-1")
        s.commit()
    return projected


def _create(session, **overrides):
    values = dict(order_id=ORDER_ID, payment_method="card", idempotency_key="key-1")
    values.update(overrides)
    return repo.create_payment(session, **values)


def _count(session):
    return session.scalar(select(func.count()).select_from(Payment))


# --- creation and validation (REQ-008) ---------------------------------------


def test_create_payment_starts_initiated(session):
    payment, created = _create(session)
    session.commit()
    assert created
    assert payment.status == PaymentStatus.INITIATED
    assert payment.currency == "INR" and payment.amount_minor == 125000  # copied from the order
    assert payment.to_dict()["id"] == str(payment.id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_id": None},
        {"order_id": "not-a-uuid"},
        {"payment_method": None},
        {"payment_method": "cash"},
        {"idempotency_key": ""},
        {"idempotency_key": None},
        {"idempotency_key": "not-issued"},
    ],
)
def test_missing_or_invalid_fields_are_rejected_without_insert(session, overrides):
    with pytest.raises(repo.PaymentValidationError):
        _create(session, **overrides)
    assert _count(session) == 0


# --- idempotency (REQ-015) ----------------------------------------------------


def test_replayed_key_returns_original_payment(session):
    first, created_first = _create(session)
    second, created_second = _create(session)
    assert created_first and not created_second
    assert second.id == first.id
    assert _count(session) == 1


def test_reused_key_with_different_order_conflicts(session):
    _create(session)
    with pytest.raises(repo.IdempotencyConflict):
        _create(session, order_id=uuid.uuid4())
    assert _count(session) == 1


def test_unknown_order_is_not_found(session):
    with pytest.raises(repo.OrderNotFound):
        _create(session, order_id=uuid.uuid4(), idempotency_key="fresh")
    assert _count(session) == 0


def test_method_must_be_available_for_the_order(session):
    other = _project(session, uuid.uuid4(), available_payment_methods=["card"])
    key = repo.ensure_payment_idempotency_key(session, other)
    with pytest.raises(repo.PaymentValidationError):
        _create(session, order_id=other.id, payment_method="upi", idempotency_key=key)


# --- order projection and key issuance (REQ-002/005/006, REQ-015/016) ---------------


def test_projection_is_stored_once_and_not_overwritten(session):
    again = _project(session, summary="changed", amount_minor=1)
    assert again.summary == "1 x Lamp" and again.amount_minor == 125000
    assert session.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": None},
        {"amount_minor": 0},
        {"amount_minor": True},
        {"currency": "IN"},
        {"available_payment_methods": []},
        {"available_payment_methods": ["cash"]},
    ],
)
def test_invalid_projection_is_rejected(session, overrides):
    with pytest.raises(repo.PaymentValidationError):
        _project(session, uuid.uuid4(), **overrides)


def test_get_order_handles_unknown_and_malformed_ids(session):
    assert repo.get_order(session, ORDER_ID).id == ORDER_ID
    assert repo.get_order(session, uuid.uuid4()) is None
    assert repo.get_order(session, "nope") is None


def test_key_is_issued_once_and_kept_while_unused_or_in_flight(session, order):
    assert repo.ensure_payment_idempotency_key(session, order) == "key-1"
    payment, _ = _create(session)
    for status in (PaymentStatus.PROCESSING, PaymentStatus.PENDING):
        payment.status = status.value
        assert repo.ensure_payment_idempotency_key(session, order) == "key-1"


def test_key_is_kept_after_success(session, order):
    payment, _ = _create(session)
    repo.update_payment_status(session, payment, PaymentStatus.SUCCESS)
    assert repo.ensure_payment_idempotency_key(session, order) == "key-1"
    replay, created = _create(session)
    assert replay.id == payment.id and not created


def test_key_rotates_after_failure_and_retry_creates_new_attempt(session, order):
    payment, _ = _create(session)
    repo.update_payment_status(session, payment, PaymentStatus.FAILED, failure_reason="declined")
    new_key = repo.ensure_payment_idempotency_key(session, order)
    assert new_key != "key-1"
    # The old key now replays the failed attempt; only the new key can start a new one.
    assert _create(session)[0].id == payment.id
    retry, created = _create(session, idempotency_key=new_key)
    assert created and retry.id != payment.id
    assert _count(session) == 2


def test_concurrent_inserts_with_same_key_create_one_row(session_factory, migrated_engine):
    """Two requests that both miss the pre-check still yield exactly one payment."""
    results, errors = [], []
    barrier = threading.Barrier(2)
    original_lookup = repo.get_payment_by_idempotency_key
    calls = threading.local()

    def lookup_then_wait(session, key):
        found = original_lookup(session, key)
        if not getattr(calls, "waited", False):
            calls.waited = True
            barrier.wait(timeout=5)  # both requests pass the pre-check before inserting
        return found

    def worker():
        try:
            with session_factory() as s:
                payment, created = _create(s)
                s.commit()
                results.append((payment.id, created))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    repo.get_payment_by_idempotency_key = lookup_then_wait
    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        repo.get_payment_by_idempotency_key = original_lookup

    # SQLite serialises writers, so the losing thread may see "database is locked"
    # instead of the unique violation; either way no second row may exist.
    with session_factory() as s:
        assert _count(s) == 1
    assert len(results) + len(errors) == 2
    assert sum(created for _, created in results) == 1
    assert len({pid for pid, _ in results}) == 1


# --- lookup (REQ-011, REQ-013) -------------------------------------------------


def test_get_payment_by_id(session):
    payment, _ = _create(session)
    session.commit()
    assert repo.get_payment(session, payment.id).id == payment.id
    assert repo.get_payment(session, str(payment.id)).id == payment.id


@pytest.mark.parametrize("bad_id", [None, "", "not-a-uuid", str(uuid.uuid4())])
def test_get_unknown_or_invalid_payment_returns_none(session, bad_id):
    assert repo.get_payment(session, bad_id) is None


# --- backend-owned status (REQ-018) ---------------------------------------------


def test_gateway_acceptance_sets_processing_and_correlation(session):
    payment, _ = _create(session)
    repo.record_gateway_acceptance(
        session, payment, gateway_checkout_url="https://gw.example/checkout/abc", gateway_session_id="gw_abc"
    )
    assert payment.status == PaymentStatus.PROCESSING
    assert repo.get_payment_by_gateway_session_id(session, "gw_abc").id == payment.id


def test_failed_requires_reason_and_reason_only_for_failed(session):
    payment, _ = _create(session)
    with pytest.raises(repo.InvalidStatusTransition):
        repo.update_payment_status(session, payment, PaymentStatus.FAILED)
    with pytest.raises(repo.InvalidStatusTransition):
        repo.update_payment_status(session, payment, PaymentStatus.SUCCESS, failure_reason="x")
    repo.update_payment_status(session, payment, "failed", failure_reason="card declined")
    assert payment.status == "failed" and payment.failure_reason == "card declined"


def test_terminal_status_is_final(session):
    payment, _ = _create(session)
    repo.update_payment_status(session, payment, PaymentStatus.SUCCESS)
    with pytest.raises(repo.InvalidStatusTransition):
        repo.update_payment_status(session, payment, PaymentStatus.FAILED, failure_reason="late")
    with pytest.raises(repo.InvalidStatusTransition):
        repo.update_payment_status(session, payment, PaymentStatus.PENDING)
    assert payment.status == PaymentStatus.SUCCESS


def test_unknown_status_value_is_rejected(session):
    payment, _ = _create(session)
    with pytest.raises(ValueError):
        repo.update_payment_status(session, payment, "paid")


def test_updated_at_moves_on_change(session):
    payment, _ = _create(session)
    session.commit()
    before = payment.updated_at
    repo.update_payment_status(session, payment, PaymentStatus.PENDING)
    session.commit()
    assert payment.updated_at >= before
    assert payment.created_at <= payment.updated_at


# --- webhook event log ------------------------------------------------------------


def _record(session, payment_id, **overrides):
    values = dict(
        payment_id=payment_id,
        gateway_event_id="evt_1",
        event_type="payment.success",
        signature_valid=True,
        raw_payload={"id": "evt_1", "status": "captured"},
    )
    values.update(overrides)
    return repo.record_webhook_event(session, **values)


def test_redelivered_webhook_is_stored_once(session):
    payment, _ = _create(session)
    first, created_first = _record(session, payment.id)
    second, created_second = _record(session, payment.id, raw_payload={"different": True})
    assert created_first and not created_second
    assert second.id == first.id
    assert second.raw_payload == {"id": "evt_1", "status": "captured"}


def test_only_verified_events_can_be_marked_processed(session):
    payment, _ = _create(session)
    bad, _ = _record(session, payment.id, gateway_event_id="evt_bad", signature_valid=False)
    with pytest.raises(repo.InvalidStatusTransition):
        repo.mark_webhook_event_processed(session, bad)
    assert bad.processed_at is None

    good, _ = _record(session, payment.id, gateway_event_id="evt_good")
    repo.mark_webhook_event_processed(session, good)
    assert good.processed_at is not None
