"""WI-2 acceptance, end to end: the payment page's JavaScript against a live payment-service.

Starts the real app (bearer auth on, FakeGateway, migrated DB) with uvicorn on a free local
port, runs ``tests/e2e/pay_flow.test.mjs`` (one test per acceptance criterion, using
``web/public/js/payment.js`` with real HTTP), then checks the database invariants behind
the duplicate-submission and safe-retry criteria.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import pytest
import uvicorn

from payment_service.api.app import create_app
from payment_service.auth import issue_token
from payment_service.config import REPO_ROOT, Settings
from payment_service.db.models import Payment
from payment_service.integrations.gateway import FakeGateway
from payment_service.integrations.orders import Order, OrderServiceError
from tests.test_api import AUTH_SECRET, SECRET

E2E_SCRIPT = REPO_ROOT / "tests" / "e2e" / "pay_flow.test.mjs"
ORDER_NAMES = (
    "display", "validation", "apiError", "redirect", "abandon",
    "confirm", "foreign", "doubleClick", "retry", "secrets",
)  # fmt: skip


class E2EOrderService:
    """One order per scenario; `broken_id` simulates order-service failing."""

    def __init__(self, order_ids: dict[str, uuid.UUID], broken_id: uuid.UUID) -> None:
        self.broken_id = broken_id
        self.orders = {
            oid: Order(
                id=oid,
                summary=f"Order for {name}",
                amount_minor=349900,
                currency="INR",
                created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
            )
            for name, oid in order_ids.items()
        }

    def get_order(self, order_id):
        if order_id == self.broken_id:
            raise OrderServiceError("order-service timed out")
        return self.orders.get(order_id)


@pytest.fixture
def live_server(db_url, session_factory):
    order_ids = {name: uuid.uuid4() for name in ORDER_NAMES}
    broken_id = uuid.uuid4()
    gateway = FakeGateway()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = Settings(
        database_url=db_url,
        public_base_url=base_url,
        gateway_webhook_secret=SECRET,
        auth_token_secret=AUTH_SECRET,
        web_dir=REPO_ROOT / "web" / "public",
    )
    app = create_app(
        settings,
        session_factory=session_factory,
        order_service=E2EOrderService(order_ids, broken_id),
        gateway=gateway,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield {
            "base_url": base_url,
            "order_ids": order_ids,
            "broken_id": broken_id,
            "gateway": gateway,
            "settings": settings,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_payment_page_flow_against_live_service(live_server, session_factory):
    order_ids = live_server["order_ids"]
    orders_env = {name: str(oid) for name, oid in order_ids.items()}
    orders_env.update(broken=str(live_server["broken_id"]), unknown=str(uuid.uuid4()))
    env = {
        **os.environ,
        "E2E_BASE_URL": live_server["base_url"],
        "E2E_TOKEN": issue_token(AUTH_SECRET, subject="customer-e2e"),
        "E2E_OTHER_ORDER_TOKEN": issue_token(AUTH_SECRET, subject="someone-else", order_id=uuid.uuid4()),
        "E2E_WEBHOOK_SECRET": SECRET,
        "E2E_ORDERS": json.dumps(orders_env),
        "E2E_SERVER_SECRETS": json.dumps([SECRET, AUTH_SECRET]),
    }
    result = subprocess.run(
        ["node", "--test", str(E2E_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-4000:]

    with session_factory() as s:
        payments = s.query(Payment).all()
    per_order = Counter(p.order_id for p in payments)
    successes = Counter(p.order_id for p in payments if p.status == "success")

    # AC16: never more than one successful charge per order.
    assert all(n <= 1 for n in successes.values()), successes
    # AC13/AC14: three clicks and a second tab produced one payment row.
    assert per_order[order_ids["doubleClick"]] == 1
    # AC15/AC16: one failed attempt + one successful retry; the post-success replay added nothing.
    retry = sorted((p for p in payments if p.order_id == order_ids["retry"]), key=lambda p: p.created_at)
    assert [p.status for p in retry] == ["failed", "success"]
    assert retry[0].idempotency_key != retry[1].idempotency_key
    # Replays never reach the gateway: one gateway call per stored payment.
    assert len(live_server["gateway"].calls) == len(payments)
    # AC10: rejected initiations (bad key, other customer, no session) stored nothing.
    assert per_order[order_ids["apiError"]] == 0
