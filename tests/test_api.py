import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from payment_service.api.app import create_app
from payment_service.auth import issue_token
from payment_service.config import REPO_ROOT, Settings
from payment_service.db.models import GatewayWebhookEvent, Payment
from payment_service.integrations.gateway import FakeGateway, sign_payload
from payment_service.integrations.orders import InMemoryOrderService, Order, OrderServiceError

SECRET = "test-webhook-secret"
AUTH_SECRET = "test-auth-token-secret"
ORDER = Order(
    id=uuid.UUID("6f1c2b8e-3d4a-4c5b-9e7f-112233445566"),
    summary="2 x T-shirt",
    amount_minor=349900,
    currency="INR",
    created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
)


class BrokenOrderService:
    def get_order(self, order_id):
        raise OrderServiceError("timeout")


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def make_client(db_url, session_factory, gateway):
    def _make(**overrides):
        settings = Settings(
            database_url=db_url,
            public_base_url="http://testserver",
            gateway_webhook_secret=SECRET,
            auth_token_secret=AUTH_SECRET,
            web_dir=REPO_ROOT / "web" / "public",
        )
        app = create_app(
            settings,
            session_factory=session_factory,
            order_service=overrides.get("order_service", InMemoryOrderService([ORDER])),
            gateway=overrides.get("gateway", gateway),
        )
        # Customer calls carry a bearer token, as the payment page does.
        token = overrides.get("token", issue_token(AUTH_SECRET, subject="customer-1"))
        return TestClient(app, headers={"Authorization": f"Bearer {token}"} if token else None)

    return _make


@pytest.fixture
def client(make_client):
    return make_client()


def _body(**overrides):
    body = {"order_id": str(ORDER.id), "payment_method": "upi", "amount_minor": ORDER.amount_minor, "currency": "INR"}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def _issued_key(client, order_id=ORDER.id):
    r = client.get(f"/api/v1/orders/{order_id}")
    assert r.status_code == 200, r.text
    return r.json()["payment_idempotency_key"]


def _create(client, key=None, **overrides):
    """POST /payments with the key issued by GET /orders (as the page does)."""
    if key is None:
        key = _issued_key(client)
    return client.post("/api/v1/payments", json=_body(**overrides), headers={"Idempotency-Key": key})


def _count(session_factory, model=Payment):
    with session_factory() as s:
        return s.query(model).count()


# --- GET /api/v1/orders/{orderId} -------------------------------------------------


def test_get_order(client):
    r = client.get(f"/api/v1/orders/{ORDER.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["amount_minor"] == 349900 and body["currency"] == "INR" and body["summary"] == "2 x T-shirt"
    assert body["available_payment_methods"] == ["upi", "card", "netbanking"]
    assert body["payment_idempotency_key"]


def test_order_projection_is_cached_and_key_is_stable(make_client, session_factory):
    from payment_service.db.models import Order as OrderProjection

    service = InMemoryOrderService([ORDER])
    client = make_client(order_service=service)
    first = client.get(f"/api/v1/orders/{ORDER.id}").json()
    # order-service going away does not matter once the projection exists.
    client = make_client(order_service=BrokenOrderService())
    second = client.get(f"/api/v1/orders/{ORDER.id}").json()
    assert second == first
    assert _count(session_factory, OrderProjection) == 1


def test_key_is_kept_while_attempt_is_in_flight_and_rotated_after_failure(client):
    key = _issued_key(client)
    payment = _create(client, key=key).json()
    assert _issued_key(client) == key  # processing: same key, so a retry replays it

    _webhook(client, _event(payment, type_="payment.failed", failure_reason="declined"))
    new_key = _issued_key(client)
    assert new_key != key

    retry = _create(client, key=new_key)
    assert retry.status_code == 201
    assert retry.json()["id"] != payment["id"]


def test_key_is_not_rotated_after_success(client):
    key = _issued_key(client)
    payment = _create(client, key=key).json()
    _webhook(client, _event(payment))
    assert _issued_key(client) == key
    # Re-submitting returns the successful payment; no second charge.
    again = _create(client, key=key)
    assert again.status_code == 200 and again.json()["id"] == payment["id"]


def test_key_not_issued_by_backend_is_rejected(client, gateway, session_factory):
    _issued_key(client)
    r = _create(client, key="made-up-by-client")
    assert r.status_code == 400
    assert gateway.calls == [] and _count(session_factory) == 0


@pytest.mark.parametrize("order_id", [str(uuid.uuid4()), "nope"])
def test_get_unknown_order_is_404(client, order_id):
    assert client.get(f"/api/v1/orders/{order_id}").status_code == 404


def test_order_service_failure_is_502(make_client):
    r = make_client(order_service=BrokenOrderService()).get(f"/api/v1/orders/{ORDER.id}")
    assert r.status_code == 502


# --- POST /api/v1/payments (REQ-008, REQ-015) ----------------------------------------


def test_create_payment_returns_201_with_checkout(client, gateway):
    r = _create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "processing"
    assert body["order_id"] == str(ORDER.id)
    assert body["amount_minor"] == 349900 and body["currency"] == "INR"
    assert body["gateway_checkout_url"].startswith(f"http://testserver/pay/result.html?orderId={ORDER.id}&paymentId={body['id']}")
    assert body["gateway_session_id"].startswith("gw_")
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_id": None},
        {"order_id": "not-a-uuid"},
        {"amount_minor": 10.5},
        {"amount_minor": "349900"},
        {"payment_method": None},
        {"payment_method": "cash"},
    ],
)
def test_missing_or_invalid_fields_are_400(client, session_factory, overrides):
    r = _create(client, **overrides)
    assert r.status_code == 400
    assert r.json()["error"]["message"]
    assert _count(session_factory) == 0


def test_missing_idempotency_key_is_400(client):
    _issued_key(client)
    r = client.post("/api/v1/payments", json=_body())
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["status", "id", "gateway_checkout_url", "amount"])
def test_client_supplied_server_fields_are_rejected(client, session_factory, field):
    r = _create(client, **{field: "success"})
    assert r.status_code == 400
    assert field in r.json()["error"]["message"]
    assert _count(session_factory) == 0


def test_amount_must_match_order(client, session_factory):
    assert _create(client, amount_minor=1).status_code == 400
    assert _create(client, currency="USD").status_code == 400
    assert _count(session_factory) == 0


def test_amount_is_taken_from_the_order_when_not_sent(client):
    r = _create(client, amount_minor=None, currency=None)
    assert r.status_code == 201
    assert r.json()["amount_minor"] == ORDER.amount_minor and r.json()["currency"] == "INR"


def test_method_not_available_for_order_is_400(make_client, session_factory):
    card_only = Order(
        id=ORDER.id, summary="s", amount_minor=100, currency="INR",
        created_at=ORDER.created_at, available_payment_methods=("card",),
    )
    client = make_client(order_service=InMemoryOrderService([card_only]))
    assert _create(client, amount_minor=None, payment_method="upi").status_code == 400
    assert _count(session_factory) == 0


def test_order_never_loaded_is_404(client, gateway):
    r = _create(client, key="some-key", order_id=str(uuid.uuid4()))
    assert r.status_code == 404
    assert gateway.calls == []


def test_replayed_key_returns_same_payment_without_second_gateway_call(client, gateway, session_factory):
    first = _create(client).json()
    second = _create(client)
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert len(gateway.calls) == 1
    assert _count(session_factory) == 1


def test_reused_key_for_different_order_is_409(make_client, session_factory):
    other = Order(id=uuid.uuid4(), summary="o", amount_minor=100, currency="INR", created_at=ORDER.created_at)
    client = make_client(order_service=InMemoryOrderService([ORDER, other]))
    key = _issued_key(client)
    _create(client, key=key)
    _issued_key(client, other.id)
    r = _create(client, key=key, order_id=str(other.id), amount_minor=100)
    assert r.status_code == 409
    assert _count(session_factory) == 1


def test_gateway_rejection_is_502_and_payment_marked_failed(make_client, session_factory):
    client = make_client(gateway=FakeGateway(fail_with="merchant disabled"))
    r = _create(client)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "gateway_error"
    with session_factory() as s:
        payment = s.query(Payment).one()
        assert payment.status == "failed"
        assert "merchant disabled" in payment.failure_reason
        assert payment.gateway_checkout_url is None
    # The failed attempt frees the order for a retry with a fresh key.
    assert _issued_key(client) != payment.idempotency_key


# --- GET /api/v1/payments/{paymentId} (REQ-011, REQ-013) --------------------------------


def test_get_payment_returns_backend_status(client):
    created = _create(client).json()
    r = client.get(f"/api/v1/payments/{created['id']}")
    assert r.status_code == 200
    assert r.json() == created


@pytest.mark.parametrize("payment_id", [str(uuid.uuid4()), "not-a-uuid", "123"])
def test_unknown_or_invalid_payment_id_is_404(client, payment_id):
    r = client.get(f"/api/v1/payments/{payment_id}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "payment_not_found"


# --- webhook (REQ-018) ------------------------------------------------------------------


def _webhook(client, payload, *, secret=SECRET, signature=None, headers=None):
    raw = json.dumps(payload).encode()
    sig = signature if signature is not None else sign_payload(secret, raw)
    return client.post(
        "/api/v1/payments/webhooks/gateway",
        content=raw,
        headers={"Content-Type": "application/json", "X-Gateway-Signature": sig, **(headers or {})},
    )


def _event(payment, event_id="evt_1", type_="payment.success", **data):
    return {"id": event_id, "type": type_, "data": {"gateway_reference": payment["gateway_session_id"], **data}}


def test_verified_webhook_sets_final_status(client, session_factory):
    payment = _create(client).json()
    r = _webhook(client, _event(payment))
    assert r.status_code == 200 and r.json()["result"] == "applied"
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "success"
    with session_factory() as s:
        event = s.query(GatewayWebhookEvent).one()
        assert event.signature_valid and event.processed_at is not None


def test_failed_webhook_records_reason(client):
    payment = _create(client).json()
    _webhook(client, _event(payment, type_="payment.failed", failure_reason="Card declined"))
    body = client.get(f"/api/v1/payments/{payment['id']}").json()
    assert body["status"] == "failed" and body["failure_reason"] == "Card declined"


def test_bad_signature_is_401_recorded_and_not_applied(client, session_factory):
    payment = _create(client).json()
    r = _webhook(client, _event(payment), secret="wrong-secret")
    assert r.status_code == 401
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "processing"
    with session_factory() as s:
        event = s.query(GatewayWebhookEvent).one()
        assert not event.signature_valid and event.processed_at is None

    # The genuine delivery of the same event id is still applied afterwards.
    assert _webhook(client, _event(payment)).json()["result"] == "applied"
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "success"


def test_redelivered_webhook_is_ignored(client, session_factory):
    payment = _create(client).json()
    _webhook(client, _event(payment))
    r = _webhook(client, _event(payment))
    assert r.status_code == 200 and r.json()["result"] == "duplicate"
    assert _count(session_factory, GatewayWebhookEvent) == 1


def test_late_event_cannot_change_a_final_status(client):
    payment = _create(client).json()
    _webhook(client, _event(payment, "evt_1", "payment.success"))
    r = _webhook(client, _event(payment, "evt_2", "payment.failed", failure_reason="late"))
    assert r.json()["result"] == "ignored"
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "success"


@pytest.mark.parametrize("raw", [b"not json", b'{"id": "x"}', b"[]"])
def test_unparseable_webhook_is_400(client, raw):
    r = client.post(
        "/api/v1/payments/webhooks/gateway",
        content=raw,
        headers={"Content-Type": "application/json", "X-Gateway-Signature": sign_payload(SECRET, raw)},
    )
    assert r.status_code == 400


def test_webhook_api_key_enforced_when_configured(db_url, session_factory, gateway):
    settings = Settings(
        database_url=db_url, gateway_webhook_secret=SECRET, gateway_webhook_api_key="k", auth_disabled=True
    )
    client = TestClient(
        create_app(settings, session_factory=session_factory, order_service=InMemoryOrderService([ORDER]), gateway=gateway)
    )
    payment = _create(client).json()
    assert _webhook(client, _event(payment)).status_code == 401
    assert _webhook(client, _event(payment), headers={"X-API-Key": "k"}).status_code == 200


def test_no_api_operation_accepts_a_status_change_from_the_client(client):
    payment = _create(client).json()
    for method in ("put", "patch"):
        r = getattr(client, method)(f"/api/v1/payments/{payment['id']}", json={"status": "success"})
        assert r.status_code == 405
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "processing"


# --- payment page is served --------------------------------------------------------------


def test_payment_pages_are_served_without_secrets(client):
    for path in ("/pay/", "/pay/result.html", "/pay/js/payment.js", "/pay/js/pay-page.js", "/pay/js/result-page.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert SECRET not in r.text
        assert "GATEWAY_WEBHOOK_SECRET" not in r.text
