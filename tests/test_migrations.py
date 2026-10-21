from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic import command
from sqlalchemy import inspect

from payment_service.db.models import Base
from tests.conftest import alembic_config

PAYMENT_COLUMNS = {
    "id": False,
    "order_id": False,
    "status": False,
    "payment_method": False,
    "amount_minor": False,
    "currency": False,
    "idempotency_key": False,
    "gateway_checkout_url": True,
    "gateway_session_id": True,
    "failure_reason": True,
    "created_at": False,
    "updated_at": False,
}

ORDER_COLUMNS = {
    "id": False,
    "summary": False,
    "amount_minor": False,
    "currency": False,
    "available_payment_methods": False,
    "payment_idempotency_key": True,
    "created_at": False,
}

WEBHOOK_COLUMNS = {
    "id": False,
    "payment_id": False,
    "gateway_event_id": False,
    "event_type": False,
    "signature_valid": False,
    "raw_payload": False,
    "received_at": False,
    "processed_at": True,
}


def _nullability(insp, table):
    return {c["name"]: c["nullable"] for c in insp.get_columns(table)}


def _unique_columns(insp, table):
    cols = {tuple(u["column_names"]) for u in insp.get_unique_constraints(table)}
    cols |= {tuple(i["column_names"]) for i in insp.get_indexes(table) if i["unique"]}
    return cols


def test_upgrade_creates_tables_with_expected_columns(migrated_engine):
    insp = inspect(migrated_engine)
    assert {"orders", "payments", "gateway_webhook_events"} <= set(insp.get_table_names())
    assert _nullability(insp, "orders") == ORDER_COLUMNS
    assert _nullability(insp, "payments") == PAYMENT_COLUMNS
    assert _nullability(insp, "gateway_webhook_events") == WEBHOOK_COLUMNS


def test_primary_keys_unique_keys_and_foreign_key(migrated_engine):
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("payments")["constrained_columns"] == ["id"]
    assert insp.get_pk_constraint("gateway_webhook_events")["constrained_columns"] == ["id"]
    assert ("idempotency_key",) in _unique_columns(insp, "payments")
    assert ("gateway_event_id",) in _unique_columns(insp, "gateway_webhook_events")
    assert insp.get_pk_constraint("orders")["constrained_columns"] == ["id"]
    assert ("payment_idempotency_key",) in _unique_columns(insp, "orders")

    payment_fks = insp.get_foreign_keys("payments")
    assert [(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]) for fk in payment_fks] == [
        (["order_id"], "orders", ["id"])
    ]

    fks = insp.get_foreign_keys("gateway_webhook_events")
    assert [(fk["constrained_columns"], fk["referred_table"], fk["referred_columns"]) for fk in fks] == [
        (["payment_id"], "payments", ["id"])
    ]


def test_lookup_indexes_exist(migrated_engine):
    insp = inspect(migrated_engine)
    payment_ix = {i["name"] for i in insp.get_indexes("payments")}
    assert {"ix_payments_order_id", "ix_payments_gateway_session_id"} <= payment_ix
    event_ix = {i["name"] for i in insp.get_indexes("gateway_webhook_events")}
    assert "ix_gateway_webhook_events_payment_id" in event_ix


def test_models_match_migrations(migrated_engine):
    with migrated_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = [d for d in compare_metadata(ctx, Base.metadata) if d[0] != "remove_table" or d[1].name != "alembic_version"]
    assert diff == []


def test_postgres_ddl_matches_data_model():
    """Render the migration as Postgres SQL (no server needed) and check the types and names."""
    import io

    cfg = alembic_config("postgresql+psycopg://user:pass@localhost/db")
    buf = io.StringIO()
    cfg.output_buffer = buf
    command.upgrade(cfg, "head", sql=True)
    ddl = buf.getvalue()

    assert "raw_payload JSONB NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "amount_minor BIGINT NOT NULL" in ddl
    assert "available_payment_methods JSONB NOT NULL" in ddl
    for name in (
        "payments_idempotency_key_key",
        "gateway_webhook_events_gateway_event_id_key",
        "gateway_webhook_events_payment_id_fkey",
        "CONSTRAINT payments_status_check CHECK",
        "CONSTRAINT payments_payment_method_check CHECK",
        "CONSTRAINT payments_amount_check CHECK",
        "CONSTRAINT payments_currency_check CHECK",
        "orders_payment_idempotency_key_key",
        "CONSTRAINT orders_amount_minor_check CHECK",
        "CONSTRAINT orders_currency_check CHECK",
        "payments_amount_minor_check",
        "payments_order_id_fkey",
        "RENAME amount TO amount_minor",
    ):
        assert name in ddl


def test_downgrade_removes_tables_and_upgrade_is_repeatable(db_url, migrated_engine):
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    insp = inspect(migrated_engine)
    assert not {"orders", "payments", "gateway_webhook_events"} & set(insp.get_table_names())
    command.upgrade(cfg, "head")
    insp = inspect(migrated_engine)
    assert {"payments", "gateway_webhook_events"} <= set(insp.get_table_names())


def test_upgrade_from_0001_keeps_existing_payments(db_url):
    """Payments created before 0002 get a placeholder order so the new FK holds."""
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy import text

    from payment_service.db.session import make_engine

    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0001")
    engine = make_engine(db_url)
    order_id, payment_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO payments (id, order_id, status, payment_method, amount, currency,"
                    " idempotency_key, checkout_url, gateway_reference, created_at, updated_at)"
                    " VALUES (:id, :oid, 'processing', 'upi', 1500, 'INR', 'k-old', 'https://gw/x',"
                    " 'gw_1', :now, :now)"
                ),
                {"id": payment_id.hex if engine.dialect.name == "sqlite" else payment_id,
                 "oid": order_id.hex if engine.dialect.name == "sqlite" else order_id, "now": now},
            )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT amount_minor, gateway_session_id, gateway_checkout_url FROM payments")
            ).one()
            assert tuple(row) == (1500, "gw_1", "https://gw/x")
            order = conn.execute(text("SELECT amount_minor, currency, payment_idempotency_key FROM orders")).one()
            assert tuple(order) == (1500, "INR", None)
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")
