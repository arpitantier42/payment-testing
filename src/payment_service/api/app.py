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
from payment_service.integrations.orders import HttpOrderService, InMemoryOrderService, OrderService


def make_order_service(settings: Settings) -> OrderService:
    """HTTP client for order-service when ORDER_SERVICE_URL is set, else the dev stub."""
    if settings.order_service_url:
        return HttpOrderService(
            settings.order_service_url,
            timeout_seconds=settings.order_service_timeout_seconds,
            api_token=settings.order_service_api_token,
        )
    return InMemoryOrderService.from_file(settings.orders_file)


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
    app.state.order_service = order_service or make_order_service(settings)
    # Dev stub until the real gateway adapter exists.
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
