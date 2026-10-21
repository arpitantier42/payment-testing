from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

from payment_service.config import database_url


def make_engine(url: str | None = None) -> Engine:
    engine = create_engine(url or database_url())
    if engine.dialect.name == "sqlite":
        _configure_sqlite(engine)
    return engine


def _configure_sqlite(engine: Engine) -> None:
    """SQLite (tests only): enforce foreign keys and let SAVEPOINTs work.

    pysqlite's own transaction handling breaks SAVEPOINT, so we disable it and
    emit BEGIN ourselves (recipe from the SQLAlchemy SQLite dialect docs).
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN")


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
