# Payment Service — guide for coding sessions

Backend that owns payment records, idempotent payment initiation and the gateway
webhook log. Design docs (OpenAPI, data model, HLD/LLD) come with each ticket.

## Stack
- Python 3.12 (>=3.11), SQLAlchemy 2.x ORM, Alembic migrations, Postgres 16 (psycopg 3).
- pytest for tests. Tests run on SQLite by default (no server needed); set
  `TEST_DATABASE_URL` to run them against Postgres.
- No web framework yet. Later tickets that add the HTTP API (`/api/v1/...`) should
  add one (FastAPI is the intended choice) under `src/payment_service/api/`.

## Layout
```
pyproject.toml                 dependencies, pytest config (pythonpath=src)
alembic.ini                    Alembic config; URL comes from DATABASE_URL
src/payment_service/
  config.py                    env-based settings (DATABASE_URL)
  db/models.py                 ORM models: Payment (payments), GatewayWebhookEvent (gateway_webhook_events)
  db/session.py                make_engine / make_sessionmaker (SQLite tweaks for tests)
  db/repository.py             data-access functions + domain errors
  db/migrations/               Alembic env + versions/NNNN_description.py
tests/                         conftest.py (migrated DB per test), test_*.py
docker-compose.yml, Dockerfile local Postgres + one-shot migration runner
Makefile                       install / test / migrate / db-up / test-pg
```

## Commands
- Install: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` (or `make install`)
- Test: `.venv/bin/pytest` (or `make test`). With deps installed, plain `pytest` also works.
- Postgres locally: `cp .env.example .env`, `docker compose up -d db`, then
  `DATABASE_URL=... .venv/bin/alembic upgrade head` (or `docker compose run --rm migrate`).
- Tests on Postgres: `make test-pg` (uses the `payment_test` database created by compose).
- New migration: `.venv/bin/alembic revision -m "..."`, then rename the file to `NNNN_description.py`
  with `revision = "NNNN"`.

## Conventions
- Schema changes go through Alembic only; keep `db/models.py` in step:
  `tests/test_migrations.py::test_models_match_migrations` fails if they drift.
- Constraint names follow Postgres defaults (`<table>_<col>_key`, `<table>_<col>_fkey`,
  `<table>_<name>_check`, `ix_<table>_<col>`). In migrations, wrap check names in `op.f()`
  so the metadata naming convention is not applied twice.
- Types: `sa.Uuid`, `BigInteger` for money (minor units), `DateTime(timezone=True)`,
  JSON with a JSONB variant on Postgres. Timestamps are set by the app in UTC (`models.utcnow`).
- Status values are `PaymentStatus` (initiated, processing, success, failed, pending); methods are
  `PaymentMethod` (upi, card, netbanking). The DB enforces both with CHECK constraints.
- Repository functions take a `Session`, `flush()` but never `commit()`: the caller owns the
  transaction. Errors map to HTTP: `PaymentValidationError`→400, `IdempotencyConflict`→409,
  `get_payment(...) is None`→404 (also for malformed ids).
- Idempotency: `create_payment` returns `(payment, created)`; a replayed key with the same
  order+amount returns the original row, a different order/amount raises `IdempotencyConflict`.
  The unique index on `idempotency_key` is the final guard against concurrent inserts.
- Payment status is changed only by the backend (`record_gateway_acceptance`,
  `update_payment_status`); success/failed are terminal; `failure_reason` only with failed.
  Webhook events are deduplicated by `gateway_event_id`; only signature-valid events may be
  marked processed.
- `orders` is owned by the external order-service, so there is no `orders` table and no FK from
  `payments.order_id`; validate order ids against order-service instead.
- Secrets only via environment variables; `.env` is git-ignored, `.env.example` is the template.
