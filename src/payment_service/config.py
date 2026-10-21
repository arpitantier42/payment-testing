"""Settings read from the environment. Secrets never live in code or in the web bundle.

Secrets (webhook secret, gateway API key, bearer-token signing secret, database URL)
are excluded from ``repr`` so they cannot end up in logs or tracebacks, and
``Settings.validate()`` makes the service refuse to start without the ones it needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql+psycopg://payment:payment@localhost:5432/payment"

# Repository root when running from a source checkout (src/payment_service/config.py -> ../../).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Shortest accepted signing secret for bearer tokens (bytes of the UTF-8 string).
MIN_AUTH_SECRET_LENGTH = 16

_TRUE = {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """The service is missing configuration it cannot run safely without."""


def database_url() -> str:
    """Database URL from the environment; secrets never live in code."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=database_url, repr=False)
    # Base URL the browser uses to reach this service; the gateway returns the customer here.
    public_base_url: str = "http://localhost:8000"
    # HMAC-SHA256 secret shared with the gateway for webhook signatures.
    gateway_webhook_secret: str = field(default="", repr=False)
    # Optional X-API-Key the gateway must send on webhook calls (checked when set).
    gateway_webhook_api_key: str = field(default="", repr=False)
    # Server-side API key for the real gateway adapter (unused by the FakeGateway stub).
    gateway_api_key: str = field(default="", repr=False)
    # HMAC secret that customer bearer tokens are signed with (shared with the token issuer).
    auth_token_secret: str = field(default="", repr=False)
    # Local development only: accept customer requests without a bearer token.
    auth_disabled: bool = False
    # Lifetime of tokens minted by the dev issuer (`python -m payment_service.auth`).
    auth_token_ttl_seconds: int = 3600
    # Base URL of the real order-service (e.g. http://orders:8080/api/v1). Empty → dev stub.
    order_service_url: str = ""
    # Timeout for one order-service call; exceeding it is a 502 on getOrder.
    order_service_timeout_seconds: float = 3.0
    # Optional service-to-service bearer token for order-service.
    order_service_api_token: str = field(default="", repr=False)
    # JSON file with the dev order-service stub's orders.
    orders_file: Path = REPO_ROOT / "dev" / "orders.json"
    # Static payment page served at /pay/.
    web_dir: Path = REPO_ROOT / "web" / "public"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            database_url=database_url(),
            public_base_url=env.get("PUBLIC_BASE_URL", cls.public_base_url).rstrip("/"),
            gateway_webhook_secret=env.get("GATEWAY_WEBHOOK_SECRET", ""),
            gateway_webhook_api_key=env.get("GATEWAY_WEBHOOK_API_KEY", ""),
            gateway_api_key=env.get("GATEWAY_API_KEY", ""),
            auth_token_secret=env.get("AUTH_TOKEN_SECRET", ""),
            auth_disabled=_env_flag("AUTH_DISABLED"),
            auth_token_ttl_seconds=_env_int("AUTH_TOKEN_TTL_SECONDS", cls.auth_token_ttl_seconds),
            order_service_url=env.get("ORDER_SERVICE_URL", "").strip().rstrip("/"),
            order_service_timeout_seconds=_env_float(
                "ORDER_SERVICE_TIMEOUT_SECONDS", cls.order_service_timeout_seconds
            ),
            order_service_api_token=env.get("ORDER_SERVICE_API_TOKEN", ""),
            orders_file=Path(env.get("ORDERS_FILE", str(cls.orders_file))),
            web_dir=Path(env.get("WEB_DIR", str(cls.web_dir))),
        )

    def validate(self) -> "Settings":
        """Raise ConfigError listing every missing/unsafe setting; returns self when fine."""
        problems = []
        if not self.gateway_webhook_secret:
            problems.append("GATEWAY_WEBHOOK_SECRET is required (webhooks cannot be verified without it)")
        if not self.auth_disabled:
            if not self.auth_token_secret:
                problems.append("AUTH_TOKEN_SECRET is required (or set AUTH_DISABLED=true for local development)")
            elif len(self.auth_token_secret.encode()) < MIN_AUTH_SECRET_LENGTH:
                problems.append(f"AUTH_TOKEN_SECRET must be at least {MIN_AUTH_SECRET_LENGTH} characters")
        if self.auth_token_ttl_seconds <= 0:
            problems.append("AUTH_TOKEN_TTL_SECONDS must be positive")
        if self.order_service_timeout_seconds <= 0:
            problems.append("ORDER_SERVICE_TIMEOUT_SECONDS must be positive")
        if self.order_service_url and not self.order_service_url.startswith(("http://", "https://")):
            problems.append("ORDER_SERVICE_URL must be an http(s) URL")
        if problems:
            raise ConfigError("Invalid configuration: " + "; ".join(problems))
        return self
