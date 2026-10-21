"""Bearer auth on the customer endpoints, config validation and secret handling (REQ-017)."""

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from payment_service.api.app import create_app
from payment_service.auth import InvalidToken, issue_token, verify_token
from payment_service.config import ConfigError, Settings
from payment_service.integrations.gateway import FakeGateway, sign_payload
from payment_service.integrations.orders import InMemoryOrderService
from tests.test_api import AUTH_SECRET, ORDER, SECRET, _body

OTHER_ORDER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


# --- tokens ----------------------------------------------------------------------------


def test_token_round_trip_with_order_scope():
    token = issue_token(AUTH_SECRET, subject="c1", order_id=ORDER.id)
    principal = verify_token(AUTH_SECRET, token)
    assert principal.subject == "c1"
    assert principal.order_id == ORDER.id
    assert principal.may_access_order(ORDER.id)
    assert not principal.may_access_order(OTHER_ORDER_ID)


def test_unscoped_token_may_access_any_order():
    principal = verify_token(AUTH_SECRET, issue_token(AUTH_SECRET, subject="c1"))
    assert principal.order_id is None
    assert principal.may_access_order(OTHER_ORDER_ID)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nodot",
        ".sig",
        "abc.",
        issue_token("another-secret-entirely", subject="c1"),
        issue_token(AUTH_SECRET, subject="c1", ttl_seconds=-1),
        issue_token(AUTH_SECRET, subject="c1") + "x",
        "é.é",
    ],
)
def test_invalid_tokens_are_rejected(token):
    with pytest.raises(InvalidToken):
        verify_token(AUTH_SECRET, token)


def test_tampered_claims_are_rejected():
    token = issue_token(AUTH_SECRET, subject="c1", order_id=ORDER.id)
    _, sig = token.split(".")
    forged = issue_token(AUTH_SECRET, subject="c1", order_id=OTHER_ORDER_ID).split(".")[0]
    with pytest.raises(InvalidToken):
        verify_token(AUTH_SECRET, f"{forged}.{sig}")


def test_expiry_is_enforced():
    token = issue_token(AUTH_SECRET, subject="c1", ttl_seconds=60, now=1000)
    assert verify_token(AUTH_SECRET, token, now=1059).subject == "c1"
    with pytest.raises(InvalidToken):
        verify_token(AUTH_SECRET, token, now=1060)


# --- config / secret management ---------------------------------------------------------


def test_service_refuses_to_start_without_secrets():
    with pytest.raises(ConfigError) as exc:
        Settings(gateway_webhook_secret="", auth_token_secret="").validate()
    assert "GATEWAY_WEBHOOK_SECRET" in str(exc.value)
    assert "AUTH_TOKEN_SECRET" in str(exc.value)
    with pytest.raises(ConfigError):
        create_app(Settings(gateway_webhook_secret=SECRET), gateway=FakeGateway())


def test_short_auth_secret_is_rejected():
    with pytest.raises(ConfigError):
        Settings(gateway_webhook_secret=SECRET, auth_token_secret="short").validate()


def test_auth_can_be_disabled_explicitly_for_local_dev():
    assert Settings(gateway_webhook_secret=SECRET, auth_disabled=True).validate().auth_disabled


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_WEBHOOK_SECRET", "whs")
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("AUTH_TOKEN_TTL_SECONDS", "120")
    s = Settings.from_env().validate()
    assert (s.gateway_webhook_secret, s.gateway_api_key, s.auth_token_secret) == ("whs", "gk", AUTH_SECRET)
    assert s.auth_disabled is False and s.auth_token_ttl_seconds == 120
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert Settings.from_env().auth_disabled is True


def test_secrets_are_not_in_settings_repr():
    s = Settings(
        database_url="postgresql://u:dbpass@h/db",
        gateway_webhook_secret="whsecret-value",
        gateway_webhook_api_key="webhook-api-key-value",
        gateway_api_key="gateway-api-key-value",
        auth_token_secret="auth-secret-value",
    )
    text = repr(s)
    for value in ("dbpass", "whsecret-value", "webhook-api-key-value", "gateway-api-key-value", "auth-secret-value"):
        assert value not in text


# --- middleware on the HTTP API ---------------------------------------------------------


@pytest.fixture
def app(db_url, session_factory):
    settings = Settings(database_url=db_url, gateway_webhook_secret=SECRET, auth_token_secret=AUTH_SECRET)
    return create_app(
        settings,
        session_factory=session_factory,
        order_service=InMemoryOrderService([ORDER]),
        gateway=FakeGateway(),
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-token"},
        _auth(issue_token(AUTH_SECRET, subject="c1", ttl_seconds=-5)),
        _auth(issue_token("a-different-signing-secret", subject="c1")),
    ],
)
def test_customer_endpoints_require_a_valid_bearer_token(app, headers):
    client = TestClient(app)
    calls = [
        ("get", f"/api/v1/orders/{ORDER.id}", {}),
        ("get", f"/api/v1/payments/{uuid.uuid4()}", {}),
        ("post", "/api/v1/payments", {"json": _body(), "headers": {"Idempotency-Key": "k"}}),
    ]
    for method, path, kwargs in calls:
        kw = dict(kwargs)
        kw["headers"] = {**kw.get("headers", {}), **headers}
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 401, (path, r.text)
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.json()["error"]["message"]
        assert r.headers["WWW-Authenticate"].startswith("Bearer")


def test_token_scoped_to_another_order_sees_not_found(app):
    owner = TestClient(app, headers=_auth(issue_token(AUTH_SECRET, subject="c1", order_id=ORDER.id)))
    stranger = TestClient(app, headers=_auth(issue_token(AUTH_SECRET, subject="c2", order_id=OTHER_ORDER_ID)))

    assert stranger.get(f"/api/v1/orders/{ORDER.id}").status_code == 404
    key = owner.get(f"/api/v1/orders/{ORDER.id}").json()["payment_idempotency_key"]

    r = stranger.post("/api/v1/payments", json=_body(), headers={"Idempotency-Key": key})
    assert r.status_code == 404
    created = owner.post("/api/v1/payments", json=_body(), headers={"Idempotency-Key": key})
    assert created.status_code == 201

    payment_id = created.json()["id"]
    assert owner.get(f"/api/v1/payments/{payment_id}").status_code == 200
    assert stranger.get(f"/api/v1/payments/{payment_id}").status_code == 404
    # A replay of the owner's key by the stranger does not leak the payment either.
    assert stranger.post("/api/v1/payments", json=_body(), headers={"Idempotency-Key": key}).status_code == 404


def test_auth_disabled_allows_anonymous_calls(db_url, session_factory):
    settings = Settings(database_url=db_url, gateway_webhook_secret=SECRET, auth_disabled=True)
    app = create_app(settings, session_factory=session_factory, order_service=InMemoryOrderService([ORDER]))
    assert TestClient(app).get(f"/api/v1/orders/{ORDER.id}").status_code == 200


def test_webhook_does_not_use_bearer_auth_and_contract_path_works(app):
    client = TestClient(app, headers=_auth(issue_token(AUTH_SECRET, subject="c1")))
    key = client.get(f"/api/v1/orders/{ORDER.id}").json()["payment_idempotency_key"]
    payment = client.post("/api/v1/payments", json=_body(), headers={"Idempotency-Key": key}).json()

    raw = json.dumps(
        {"id": "evt_contract", "type": "payment.success", "data": {"gateway_reference": payment["gateway_session_id"]}}
    ).encode()
    gateway_client = TestClient(app)  # no Authorization header
    r = gateway_client.post(
        "/api/v1/payments/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "X-Gateway-Signature": sign_payload(SECRET, raw)},
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/api/v1/payments/{payment['id']}").json()["status"] == "success"


def test_healthz_is_public(app):
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_dev_token_cli(monkeypatch, capsys):
    from payment_service.auth import main

    monkeypatch.setenv("AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    main(["--order-id", str(ORDER.id), "--ttl", "60"])
    token, url = capsys.readouterr().out.split()
    assert verify_token(AUTH_SECRET, token, now=time.time()).order_id == ORDER.id
    assert url == f"http://localhost:8000/pay/?orderId={ORDER.id}#token={token}"
