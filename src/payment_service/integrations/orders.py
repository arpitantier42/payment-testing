"""order-service adapter.

The real order-service contract is not confirmed yet (HLD risk RSK-002), so the
service talks to it through ``OrderService``. ``InMemoryOrderService`` is the
development/test stub; replace it with an HTTP client once the contract is known.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Order:
    id: uuid.UUID
    summary: str
    amount: int  # minor units
    currency: str  # ISO 4217
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "summary": self.summary,
            "amount": self.amount,
            "currency": self.currency,
            "created_at": self.created_at.isoformat(),
        }


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
                    amount=int(o["amount"]),
                    currency=o["currency"],
                    created_at=datetime.fromisoformat(o["created_at"]),
                )
                for o in raw
            ]
        )
