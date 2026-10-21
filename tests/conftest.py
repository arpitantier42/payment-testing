"""Test database fixtures.

By default each test gets a fresh SQLite file migrated with Alembic. Set
TEST_DATABASE_URL to run the same tests against Postgres (the schema is
upgraded before and downgraded after each test).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from payment_service.db.session import make_engine, make_sessionmaker

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'payment.db'}"


@pytest.fixture
def migrated_engine(db_url: str):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    engine = make_engine(db_url)
    yield engine
    engine.dispose()
    command.downgrade(cfg, "base")


@pytest.fixture
def session_factory(migrated_engine: Engine):
    return make_sessionmaker(migrated_engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s
        s.rollback()
