"""FastAPI application factory.

Run locally with ``uvicorn --factory payment_service.api.app:create_app``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import sessionmaker

from payment_service.api.errors import install_error_handlers
from payment_service.api.routes import router, webhook_router
from payment_service.config import Settings
from payment_service.db.session import make_engine, make_sessionmaker
from payment_service.integrations.gateway import FakeGateway, PaymentGateway
from payment_service.integrations.orders import InMemoryOrderService, OrderService


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker | None = None,
    order_service: OrderService | None = None,
    gateway: PaymentGateway | None = None,
) -> FastAPI:
    # Refuse to start without the secrets the service needs (raises ConfigError).
    settings = (settings or Settings.from_env()).validate()
    app = FastAPI(title="Payment Service", version="1.0.1")
    app.state.settings = settings
    app.state.session_factory = session_factory or make_sessionmaker(make_engine(settings.database_url))
    # Dev stubs until the real order-service and gateway adapters exist.
    app.state.order_service = order_service or InMemoryOrderService.from_file(settings.orders_file)
    app.state.gateway = gateway or FakeGateway()

    install_error_handlers(app)
    app.include_router(webhook_router)
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    def _healthz() -> dict:
        # Liveness for compose/orchestrators; unauthenticated and reveals no configuration.
        return {"status": "ok"}

    if settings.web_dir.is_dir():
        app.mount("/pay", StaticFiles(directory=settings.web_dir, html=True), name="web")

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse("/pay/")

    return app
