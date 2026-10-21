"""order-service adapter.

The real order-service contract is not confirmed yet (HLD risk RSK-002), so the
service talks to it only through ``OrderService``. Two implementations exist:

* ``InMemoryOrderService`` — development/test stub (reads ``dev/orders.json``).
* ``HttpOrderService`` — client for the *assumed* get-by-id contract written down in
  ``docs/spikes/order-service-contract.md``. Selected when ``ORDER_SERVICE_URL`` is set.

Both turn the wire payload into ``Order`` with ``parse_order_payload``, so if the real
contract differs only that function and ``HttpOrderService`` change.

Error contract towards the routes: ``get_order`` returns ``None`` when the order does
not exist (→ 404) and raises ``OrderServiceError`` for anything else that is not a
well-formed order (→ 502): timeouts, connection errors, non-404 error statuses,
non-JSON or malformed bodies, or a body describing a different order.

Check a live order-service against the assumed contract with
``ORDER_SERVICE_URL=... python -m payment_service.integrations.orders <order-id>``.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


ALL_METHODS = ("upi", "card", "netbanking")

_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


@dataclass(frozen=True)
class Order:
    """An order as order-service reports it; payment-service stores a projection of it."""

    id: uuid.UUID
    summary: str
    amount_minor: int  # minor units
    currency: str  # ISO 4217
    created_at: datetime
    available_payment_methods: tuple[str, ...] = field(default=ALL_METHODS)


class OrderServiceError(Exception):
    """order-service failed, timed out or answered outside the contract (HTTP 502)."""


class OrderPayloadError(OrderServiceError):
    """order-service answered 200 with a body that does not match the assumed contract."""


class OrderService(Protocol):
    def get_order(self, order_id: uuid.UUID) -> Order | None: ...


def parse_order_payload(raw: Any, expected_id: uuid.UUID | None = None) -> Order:
    """Validate one order-service order object and turn it into ``Order``.

    Assumed shape (docs/spikes/order-service-contract.md): ``id`` (uuid), ``summary``
    (non-empty string), ``amount`` (integer minor units, >= 0; ``amount_minor`` is
    accepted as an alias), ``currency`` (ISO 4217 alpha-3), ``created_at`` (ISO 8601
    with offset) and optional ``available_payment_methods`` (unknown methods dropped;
    absent → all methods). Unknown extra fields are ignored. Raises OrderPayloadError.
    """
    if not isinstance(raw, dict):
        raise OrderPayloadError("order payload is not a JSON object")

    try:
        order_id = uuid.UUID(str(raw["id"]))
    except (KeyError, ValueError) as exc:
        raise OrderPayloadError("order id missing or not a uuid") from exc
    if expected_id is not None and order_id != expected_id:
        raise OrderPayloadError("order-service returned a different order than requested")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise OrderPayloadError("order summary missing or empty")

    amount = raw["amount"] if "amount" in raw else raw.get("amount_minor")
    # bool is an int subclass; floats/strings would hide a units mismatch (major vs minor).
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise OrderPayloadError("order amount missing or not a non-negative integer (minor units)")

    currency = raw.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise OrderPayloadError("order currency missing or not an ISO 4217 code")

    try:
        created_at = datetime.fromisoformat(str(raw["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise OrderPayloadError("order created_at missing or not ISO 8601") from exc
    if created_at.tzinfo is None:
        raise OrderPayloadError("order created_at has no UTC offset")

    methods_raw = raw.get("available_payment_methods", ALL_METHODS)
    if not isinstance(methods_raw, (list, tuple)):
        raise OrderPayloadError("available_payment_methods is not a list")
    methods = tuple(m for m in ALL_METHODS if m in methods_raw)
    if not methods:
        raise OrderPayloadError("order has no payment method this service supports")

    return Order(
        id=order_id,
        summary=summary,
        amount_minor=amount,
        currency=currency.upper(),
        created_at=created_at,
        available_payment_methods=methods,
    )


class InMemoryOrderService:
    def __init__(self, orders: list[Order] | None = None) -> None:
        self._orders = {o.id: o for o in orders or []}

    def add(self, order: Order) -> None:
        self._orders[order.id] = order

    def get_order(self, order_id: uuid.UUID) -> Order | None:
        return self._orders.get(order_id)

    @classmethod
    def from_file(cls, path: Path) -> "InMemoryOrderService":
        if not path.exists():
            return cls()
        return cls([parse_order_payload(o) for o in json.loads(path.read_text())])


class HttpOrderService:
    """Client for the assumed order-service contract ``GET {base_url}/orders/{orderId}``.

    200 + order JSON → ``Order``; 404 → ``None``; everything else → ``OrderServiceError``.
    Uses the standard library only (no runtime HTTP dependency). The optional token is
    sent as ``Authorization: Bearer`` (service-to-service credential, never the customer's).
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0, api_token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._api_token = api_token

    def __repr__(self) -> str:  # never show the token
        return f"HttpOrderService({self.base_url!r}, timeout_seconds={self.timeout_seconds})"

    def order_url(self, order_id: uuid.UUID) -> str:
        return f"{self.base_url}/orders/{urllib.parse.quote(str(order_id))}"

    def get_order(self, order_id: uuid.UUID) -> Order | None:
        headers = {"Accept": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        request = urllib.request.Request(self.order_url(order_id), headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise OrderServiceError(f"order-service answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # URLError wraps connection refused/DNS; a read timeout surfaces as TimeoutError.
            raise OrderServiceError(f"order-service unreachable: {exc}") from exc
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise OrderPayloadError("order-service answered 200 with a non-JSON body") from exc
        return parse_order_payload(payload, expected_id=order_id)


def probe(service: OrderService, order_id: uuid.UUID) -> tuple[int, str]:
    """Fetch one order and classify the result the way getOrder would (exit code, message)."""
    try:
        order = service.get_order(order_id)
    except OrderPayloadError as exc:
        return 2, f"CONTRACT MISMATCH (getOrder -> 502): {exc}"
    except OrderServiceError as exc:
        return 1, f"UPSTREAM FAILURE (getOrder -> 502): {exc}"
    if order is None:
        return 0, "NOT FOUND (getOrder -> 404)"
    as_json = {
        "id": str(order.id),
        "summary": order.summary,
        "amount_minor": order.amount_minor,
        "currency": order.currency,
        "created_at": order.created_at.isoformat(),
        "available_payment_methods": list(order.available_payment_methods),
    }
    return 0, "OK (getOrder -> 200): " + json.dumps(as_json)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from payment_service.config import Settings

    parser = argparse.ArgumentParser(description="Check order-service against the assumed get-by-id contract.")
    parser.add_argument("order_id", type=uuid.UUID)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if not settings.order_service_url:
        print("ORDER_SERVICE_URL is not set", file=sys.stderr)
        return 1
    service = HttpOrderService(
        settings.order_service_url,
        timeout_seconds=settings.order_service_timeout_seconds,
        api_token=settings.order_service_api_token,
    )
    code, message = probe(service, args.order_id)
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
