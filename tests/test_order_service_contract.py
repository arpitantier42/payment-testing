"""Contract tests for the order-service adapter (WI-002 spike, HLD RSK-002).

A local HTTP server plays order-service and answers with the assumed contract
(docs/spikes/order-service-contract.md) and with each failure the spike identified.
The getOrder tests run the real app with the HTTP adapter against that server.
"""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from payment_service.api.app import create_app, make_order_service
from payment_service.auth import issue_token
from payment_service.config import REPO_ROOT, ConfigError, Settings
from payment_service.db.models import Order as OrderProjection
from payment_service.integrations.orders import (
    HttpOrderService,
    InMemoryOrderService,
    OrderPayloadError,
    OrderServiceError,
    parse_order_payload,
    probe,
)
from payment_service.secret_scan import SECRET_SETTINGS

ORDER_ID = uuid.UUID("6f1c2b8e-3d4a-4c5b-9e7f-112233445566")
UPSTREAM_TOKEN = "svc-token-for-order-service"
AUTH_SECRET = "test-auth-token-secret"


def order_body(**changes):
    body = {
        "id": str(ORDER_ID),
        "summary": "2 x Cotton T-shirt",
        "amount": 349900,
        "currency": "INR",
        "created_at": "2026-10-21T09:00:00Z",
        "available_payment_methods": ["upi", "card", "netbanking"],
        "status": "placed",  # extra fields are ignored
    }
    body.update(changes)
    return {k: v for k, v in body.items() if v is not ...}


class FakeOrderServer:
    """order-service double: `routes[order_id] = (status, body_or_bytes, delay_seconds)`."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append((self.path, dict(self.headers)))
                order_id = self.path.rsplit("/", 1)[-1]
                status, body, delay = server.routes.get(order_id, (404, {"error": "not found"}, 0))
                if delay:
                    time.sleep(delay)
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # client gave up (timeout test)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def upstream():
    server = FakeOrderServer()
    yield server
    server.close()


@pytest.fixture
def adapter(upstream):
    return HttpOrderService(upstream.base_url, timeout_seconds=0.5, api_token=UPSTREAM_TOKEN)


# --- 1. success shape -------------------------------------------------------------


def test_200_order_is_mapped_to_order(upstream, adapter):
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 0)
    order = adapter.get_order(ORDER_ID)
    assert order.id == ORDER_ID
    assert order.summary == "2 x Cotton T-shirt"
    assert order.amount_minor == 349900
    assert order.currency == "INR"
    assert order.created_at.utcoffset().total_seconds() == 0
    assert order.available_payment_methods == ("upi", "card", "netbanking")


def test_request_is_get_by_id_with_service_token(upstream, adapter):
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 0)
    adapter.get_order(ORDER_ID)
    path, headers = upstream.requests[-1]
    assert path == f"/api/v1/orders/{ORDER_ID}"
    assert headers["Authorization"] == f"Bearer {UPSTREAM_TOKEN}"
    assert headers["Accept"] == "application/json"
    assert UPSTREAM_TOKEN not in repr(adapter)


def test_amount_minor_alias_and_missing_methods_default():
    order = parse_order_payload(order_body(amount=..., amount_minor=500, available_payment_methods=...))
    assert order.amount_minor == 500
    assert order.available_payment_methods == ("upi", "card", "netbanking")


def test_unknown_methods_are_dropped_and_currency_upper_cased():
    order = parse_order_payload(order_body(currency="inr", available_payment_methods=["card", "wallet"]))
    assert order.currency == "INR"
    assert order.available_payment_methods == ("card",)


# --- 2. error cases -----------------------------------------------------------------


def test_404_means_unknown_order(upstream, adapter):
    assert adapter.get_order(ORDER_ID) is None


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 502, 503])
def test_other_error_statuses_are_upstream_failures(upstream, adapter, status):
    upstream.routes[str(ORDER_ID)] = (status, {"error": "boom"}, 0)
    with pytest.raises(OrderServiceError):
        adapter.get_order(ORDER_ID)


def test_timeout_is_upstream_failure(upstream, adapter):
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 1.5)
    started = time.monotonic()
    with pytest.raises(OrderServiceError):
        adapter.get_order(ORDER_ID)
    assert time.monotonic() - started < 1.4


def test_connection_refused_is_upstream_failure(upstream):
    dead = HttpOrderService(upstream.base_url, timeout_seconds=0.5)
    upstream.close()
    with pytest.raises(OrderServiceError):
        dead.get_order(ORDER_ID)
    upstream.close = lambda: None  # already closed


def test_non_json_body_is_contract_mismatch(upstream, adapter):
    upstream.routes[str(ORDER_ID)] = (200, b"<html>gateway</html>", 0)
    with pytest.raises(OrderPayloadError):
        adapter.get_order(ORDER_ID)


def test_different_order_in_body_is_contract_mismatch(upstream, adapter):
    upstream.routes[str(ORDER_ID)] = (200, order_body(id=str(uuid.uuid4())), 0)
    with pytest.raises(OrderPayloadError):
        adapter.get_order(ORDER_ID)


@pytest.mark.parametrize(
    "changes",
    [
        {"summary": ...},
        {"summary": "  "},
        {"amount": ...},
        {"amount": 3499.0},  # major units / float
        {"amount": "349900"},
        {"amount": True},
        {"amount": -1},
        {"currency": ...},
        {"currency": "RUPEES"},
        {"created_at": ...},
        {"created_at": "yesterday"},
        {"created_at": "2026-10-21T09:00:00"},  # no offset
        {"available_payment_methods": "upi"},
        {"available_payment_methods": ["wallet"]},
        {"id": "not-a-uuid"},
    ],
)
def test_malformed_order_is_contract_mismatch(changes):
    with pytest.raises(OrderPayloadError):
        parse_order_payload(order_body(**changes))


def test_non_object_payload_is_contract_mismatch():
    with pytest.raises(OrderPayloadError):
        parse_order_payload([order_body()])


# --- 3. getOrder over the HTTP adapter ------------------------------------------------


@pytest.fixture
def make_api(upstream, db_url, session_factory):
    from fastapi.testclient import TestClient

    def _make():
        settings = Settings(
            database_url=db_url,
            gateway_webhook_secret="test-webhook-secret",
            auth_token_secret=AUTH_SECRET,
            order_service_url=upstream.base_url,
            order_service_timeout_seconds=0.5,
            web_dir=REPO_ROOT / "web" / "public",
        )
        app = create_app(settings, session_factory=session_factory)
        assert isinstance(app.state.order_service, HttpOrderService)
        token = issue_token(AUTH_SECRET, subject="customer-1")
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return _make


def test_get_order_200_through_http_adapter(upstream, make_api, session_factory):
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 0)
    r = make_api().get(f"/api/v1/orders/{ORDER_ID}")
    assert r.status_code == 200
    body = r.json()
    assert (body["id"], body["amount_minor"], body["currency"]) == (str(ORDER_ID), 349900, "INR")
    with session_factory() as s:
        assert s.get(OrderProjection, ORDER_ID) is not None


@pytest.mark.parametrize(
    "route, expected",
    [
        (None, 404),
        ((503, {"error": "down"}, 0), 502),
        ((200, order_body(), 1.5), 502),
        ((200, order_body(amount=3499.0), 0), 502),
        ((200, b"not json", 0), 502),
    ],
)
def test_get_order_error_mapping_through_http_adapter(upstream, make_api, session_factory, route, expected):
    if route is not None:
        upstream.routes[str(ORDER_ID)] = route
    r = make_api().get(f"/api/v1/orders/{ORDER_ID}")
    assert r.status_code == expected
    assert set(r.json()["error"]) == {"code", "message"}
    with session_factory() as s:  # nothing is cached from a failed fetch
        assert s.get(OrderProjection, ORDER_ID) is None


def test_cached_projection_survives_order_service_outage(upstream, make_api):
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 0)
    client = make_api()
    assert client.get(f"/api/v1/orders/{ORDER_ID}").status_code == 200
    upstream.routes[str(ORDER_ID)] = (503, {"error": "down"}, 0)
    assert client.get(f"/api/v1/orders/{ORDER_ID}").status_code == 200


# --- 4. configuration, probe and stub --------------------------------------------------


def test_settings_select_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDER_SERVICE_URL", "http://orders:8080/api/v1/")
    monkeypatch.setenv("ORDER_SERVICE_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("ORDER_SERVICE_API_TOKEN", UPSTREAM_TOKEN)
    settings = Settings.from_env()
    assert settings.order_service_url == "http://orders:8080/api/v1"
    assert UPSTREAM_TOKEN not in repr(settings)
    service = make_order_service(settings)
    assert isinstance(service, HttpOrderService)
    assert service.order_url(ORDER_ID) == f"http://orders:8080/api/v1/orders/{ORDER_ID}"
    assert service.timeout_seconds == 1.5
    monkeypatch.delenv("ORDER_SERVICE_URL")
    assert isinstance(make_order_service(Settings.from_env()), InMemoryOrderService)


@pytest.mark.parametrize("env", [{"ORDER_SERVICE_TIMEOUT_SECONDS": "0"}, {"ORDER_SERVICE_URL": "orders:8080"}])
def test_invalid_order_service_settings_are_rejected(monkeypatch, env):
    monkeypatch.setenv("GATEWAY_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError):
        Settings.from_env().validate()


def test_order_service_token_is_a_scanned_secret():
    assert "ORDER_SERVICE_API_TOKEN" in SECRET_SETTINGS


def test_probe_classifies_responses(upstream, adapter):
    assert probe(adapter, ORDER_ID)[0] == 0 and "404" in probe(adapter, ORDER_ID)[1]
    upstream.routes[str(ORDER_ID)] = (200, order_body(), 0)
    code, message = probe(adapter, ORDER_ID)
    assert code == 0 and message.startswith("OK")
    upstream.routes[str(ORDER_ID)] = (200, order_body(amount="1"), 0)
    assert probe(adapter, ORDER_ID)[0] == 2
    upstream.routes[str(ORDER_ID)] = (500, {}, 0)
    assert probe(adapter, ORDER_ID)[0] == 1


def test_dev_stub_file_follows_the_contract():
    orders = InMemoryOrderService.from_file(REPO_ROOT / "dev" / "orders.json")
    assert orders.get_order(ORDER_ID).amount_minor == 349900
