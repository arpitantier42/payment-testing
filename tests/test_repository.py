import threading
import uuid

import pytest
from sqlalchemy import func, select

from payment_service.db import repository as repo
from payment_service.db.models import Payment, PaymentStatus

ORDER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


def _create(session, **overrides):
    values = dict(
        order_id=ORDER_ID,
        payment_method="card",
        amount=125000,
        currency="inr",
        idempotency_key="key-1",
    )
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
    assert payment.currency == "INR"
    assert payment.to_dict()["id"] == str(payment.id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_id": None},
        {"order_id": "not-a-uuid"},
        {"amount": None},
        {"amount": 0},
        {"amount": 10.5},
        {"amount": True},
        {"payment_method": None},
        {"payment_method": "cash"},
        {"currency": "IN"},
        {"idempotency_key": ""},
        {"idempotency_key": None},
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


def test_reused_key_with_different_order_or_amount_conflicts(session):
    _create(session)
    with pytest.raises(repo.IdempotencyConflict):
        _create(session, amount=1)
    with pytest.raises(repo.IdempotencyConflict):
        _create(session, order_id=uuid.uuid4())
    assert _count(session) == 1


def test_new_key_for_same_order_creates_a_new_attempt(session):
    _create(session, idempotency_key="a")
    _create(session, idempotency_key="b")
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
        session, payment, checkout_url="https://gw.example/checkout/abc", gateway_reference="gw_abc"
    )
    assert payment.status == PaymentStatus.PROCESSING
    assert repo.get_payment_by_gateway_reference(session, "gw_abc").id == payment.id


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
