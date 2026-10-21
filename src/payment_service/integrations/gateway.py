"""Payment gateway adapter.

Only payment-service talks to the gateway; the browser receives just the
checkout URL. ``FakeGateway`` is the development/test stub: its "checkout"
sends the customer straight back to the return URL with a gateway-reported
status in the query string, which the payment page must ignore in favour of
GET /api/v1/payments/{paymentId}.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode


@dataclass(frozen=True)
class GatewayTransaction:
    gateway_reference: str
    checkout_url: str


class GatewayError(Exception):
    """The gateway rejected or failed to accept the initiation (HTTP 502)."""


class PaymentGateway(Protocol):
    def create_transaction(
        self,
        *,
        payment_id: uuid.UUID,
        amount: int,
        currency: str,
        payment_method: str,
        return_url: str,
    ) -> GatewayTransaction: ...


class FakeGateway:
    def __init__(self, *, fail_with: str | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[dict] = []

    def create_transaction(self, **kwargs) -> GatewayTransaction:
        self.calls.append(kwargs)
        if self.fail_with:
            raise GatewayError(self.fail_with)
        reference = f"gw_{uuid.uuid4().hex[:16]}"
        sep = "&" if "?" in kwargs["return_url"] else "?"
        checkout_url = f"{kwargs['return_url']}{sep}{urlencode({'gateway_status': 'success'})}"
        return GatewayTransaction(gateway_reference=reference, checkout_url=checkout_url)


def sign_payload(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of the raw webhook body."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature:
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature.strip())
