"""order-service adapter.

The real order-service contract is not confirmed yet (HLD risk RSK-002), so the
service talks to it through ``OrderService``. ``InMemoryOrderService`` is the
development/test stub; replace it with an HTTP client once the contract is known.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


ALL_METHODS = ("upi", "card", "netbanking")


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
    """order-service failed or timed out (HTTP 502)."""


class OrderService(Protocol):
    def get_order(self, order_id: uuid.UUID) -> Order | None: ...


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
        raw = json.loads(path.read_text())
        return cls(
            [
                Order(
                    id=uuid.UUID(o["id"]),
                    summary=o["summary"],
                    amount_minor=int(o["amount_minor"]),
                    currency=o["currency"],
                    created_at=datetime.fromisoformat(o["created_at"]),
                    available_payment_methods=tuple(o.get("available_payment_methods", ALL_METHODS)),
                )
                for o in raw
            ]
        )
