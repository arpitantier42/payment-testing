# Payment Service — guide for coding sessions

Payment page + backend: the customer picks UPI/card/net banking for an existing order,
the backend creates the payment (idempotently) with the gateway, verifies the gateway's
signed webhook, and is the only source of truth for the final status the page shows.
Design docs (OpenAPI, data model, HLD/LLD) come with each ticket.

## Stack
- Backend: Python 3.12 (>=3.11), FastAPI, SQLAlchemy 2.x ORM, Alembic, Postgres 16 (psycopg 3).
- Frontend: static HTML + vanilla ES modules in `web/public` (no build step), served by the
  API at `/pay/`. Logic lives in DOM-free `web/public/js/payment.js`, tested with Node's
  built-in runner (`node --test`, Node 20, no npm dependencies).
- Tests: `pytest` runs everything — Python tests on SQLite by default (set `TEST_DATABASE_URL`
  for Postgres) and `tests/test_web.py` runs the Node tests (skipped if `node` is missing).

## Layout
```
pyproject.toml                 dependencies, pytest config (pythonpath=src)
alembic.ini                    Alembic config; URL comes from DATABASE_URL
src/payment_service/
  config.py                    Settings.from_env(): DATABASE_URL, PUBLIC_BASE_URL, GATEWAY_WEBHOOK_SECRET, ...
  api/app.py                   create_app(settings, session_factory=, order_service=, gateway=) factory
  api/routes.py                /api/v1 routes: orders, payments, gateway webhook
  api/errors.py                ApiError + handlers; validation errors -> 400
  db/models.py                 ORM: Payment (payments), GatewayWebhookEvent (gateway_webhook_events)
  db/session.py                make_engine / make_sessionmaker (SQLite tweaks for tests)
  db/repository.py             data-access functions + domain errors
  db/migrations/               Alembic env + versions/NNNN_description.py
  integrations/orders.py       OrderService protocol + InMemoryOrderService stub (reads dev/orders.json)
  integrations/gateway.py      PaymentGateway protocol + FakeGateway stub, HMAC sign/verify
web/public/                    index.html (pay page), result.html (confirmed result), js/, styles.css
web/tests/                     node --test unit tests for js/payment.js
dev/orders.json                demo order for the order-service stub
tests/                         conftest.py (migrated DB per test), test_*.py
docker-compose.yml, Dockerfile Postgres, migration runner, API
Makefile                       install / test / test-web / run / migrate / db-up / test-pg
```

## Commands
- Install: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` (or `make install`)
- Test everything: `.venv/bin/pytest` (or `make test`). Frontend only: `cd web && npm test`.
- Run locally on SQLite: `DATABASE_URL=sqlite:///./dev.db GATEWAY_WEBHOOK_SECRET=dev .venv/bin/alembic upgrade head`
  then `make run` with the same env; open `http://localhost:8000/pay/?orderId=6f1c2b8e-3d4a-4c5b-9e7f-112233445566`.
- Postgres via Docker: `docker compose up api` (starts db, runs migrations, serves on :8000).
- Tests on Postgres: `make test-pg` (uses the `payment_test` database created by compose).
- New migration: `.venv/bin/alembic revision -m "..."`, then rename the file to `NNNN_description.py`
  with `revision = "NNNN"`.

## API (see ticket OpenAPI)
- `GET /api/v1/orders/{orderId}` → 200 Order | 404 | 502 (order-service failure).
- `POST /api/v1/payments` (header `Idempotency-Key`, body `order_id, payment_method, amount, currency`)
  → 201 Payment (new or replay) | 400 missing/invalid/unknown field or amount≠order | 404 order | 409 key reused
  for other order/amount | 502 gateway refused (payment stored as failed).
- `GET /api/v1/payments/{paymentId}` → 200 Payment | 404 (unknown or malformed id).
- `POST /api/v1/payments/webhooks/gateway` (header `X-Gateway-Signature` = hex HMAC-SHA256 of raw body;
  `X-API-Key` checked when `GATEWAY_WEBHOOK_API_KEY` is set). Body
  `{"id", "type": "payment.success|payment.failed|payment.pending", "data": {"payment_id"|"gateway_reference", "failure_reason"?}}`
  → 200 applied/duplicate/ignored | 400 unparseable or unknown payment | 401 bad signature (event still recorded).
- Errors are always `{"error": {"code", "message"}}`; the page shows `message`.

## Conventions
- Schema changes go through Alembic only; keep `db/models.py` in step:
  `tests/test_migrations.py::test_models_match_migrations` fails if they drift.
- Constraint names follow Postgres defaults (`<table>_<col>_key`, `<table>_<col>_fkey`,
  `<table>_<name>_check`, `ix_<table>_<col>`). In migrations, wrap check names in `op.f()`
  so the metadata naming convention is not applied twice.
- Types: `sa.Uuid`, `BigInteger` for money (minor units), `DateTime(timezone=True)`,
  JSON with a JSONB variant on Postgres. Timestamps are set by the app in UTC (`models.utcnow`);
  `Payment.to_dict()` always emits offset-aware ISO strings.
- Status values are `PaymentStatus` (initiated, processing, success, failed, pending); methods are
  `PaymentMethod` (upi, card, netbanking). The DB enforces both with CHECK constraints.
- Repository functions take a `Session`, `flush()` but never `commit()`; route handlers commit.
  Errors map to HTTP: `PaymentValidationError`→400, `IdempotencyConflict`→409, `get_payment(...) is None`→404.
- Idempotency: replayed key + same order/amount returns the original payment (no second gateway call);
  the unique index on `idempotency_key` is the final guard against concurrent inserts.
- Status is changed only by the backend (gateway acceptance → processing; verified webhook →
  success/failed/pending). success/failed are terminal. Request bodies use `extra="forbid"`, so a
  client-supplied `status` is a 400. Webhook events dedupe by `gateway_event_id`; an unverified
  stored event is replaced by a later verified delivery of the same id.
- Frontend: never decide success from the gateway redirect (`gateway_status` param is only
  compared and flagged); always show the result of `GET /api/v1/payments/{paymentId}`, using the
  paymentId stored in sessionStorage from the initiation response. Pay Now is guarded by a
  synchronous in-flight flag + disabled button; the Idempotency-Key is kept in sessionStorage per
  order and reused only when the outcome is unknown (network error). Distinct views/test ids
  for validation, network error, API error, failed, pending, success.
- order-service and gateway are external: code against the `OrderService` / `PaymentGateway`
  protocols; the in-repo implementations are dev stubs. There is no `orders` table and no FK
  from `payments.order_id`.
- Secrets only via environment variables (server side); `.env` is git-ignored, `.env.example` is
  the template. Nothing under `web/` may reference a gateway secret (checked in `tests/test_api.py`).
- Not built yet: bearer authentication on the customer endpoints (contract lists `bearer`),
  real order-service/gateway adapters, fallback gateway status polling.
