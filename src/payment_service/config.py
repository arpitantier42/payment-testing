"""Settings read from the environment. Secrets never live in code or in the web bundle."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "postgresql+psycopg://payment:payment@localhost:5432/payment"

# Repository root when running from a source checkout (src/payment_service/config.py -> ../../).
REPO_ROOT = Path(__file__).resolve().parents[2]


def database_url() -> str:
    """Database URL from the environment; secrets never live in code."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=database_url)
    # Base URL the browser uses to reach this service; the gateway returns the customer here.
    public_base_url: str = "http://localhost:8000"
    # HMAC-SHA256 secret shared with the gateway for webhook signatures.
    gateway_webhook_secret: str = ""
    # Optional X-API-Key the gateway must send on webhook calls (checked when set).
    gateway_webhook_api_key: str = ""
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
            orders_file=Path(env.get("ORDERS_FILE", str(cls.orders_file))),
            web_dir=Path(env.get("WEB_DIR", str(cls.web_dir))),
        )
