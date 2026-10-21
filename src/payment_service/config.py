import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://payment:payment@localhost:5432/payment"


def database_url() -> str:
    """Database URL from the environment; secrets never live in code."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
