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
  config.py                    Settings.from_env() + validate() (ConfigError); secrets have repr=False
  auth.py                      bearer tokens: issue_token / verify_token -> Principal; dev CLI (python -m)
  secret_scan.py               REQ-017 scan of web/public (python -m payment_service.secret_scan)
  api/app.py                   create_app(settings, session_factory=, order_service=, gateway=) factory; /healthz
  api/routes.py                /api/v1 routes: `router` (customer, bearer) + `webhook_router` (gateway)
  api/security.py              require_principal dependency (router-level on `router`), PrincipalDep
  api/errors.py                ApiError(+headers) + handlers; validation errors -> 400
  db/models.py                 ORM: Order (orders projection), Payment (payments), GatewayWebhookEvent
  db/session.py                make_engine / make_sessionmaker (SQLite tweaks for tests)
  db/repository.py             data-access functions + domain errors
  db/migrations/               Alembic env + versions/NNNN_description.py
  integrations/orders.py       OrderService protocol + InMemoryOrderService stub (reads dev/orders.json)
  integrations/gateway.py      PaymentGateway protocol + FakeGateway stub, HMAC sign/verify
web/public/                    index.html (pay page), result.html (confirmed result), js/, styles.css
web/tests/                     node --test unit tests for js/payment.js
dev/orders.json                demo order for the order-service stub
tests/                         conftest.py (migrated DB per test), test_*.py
tests/e2e/pay_flow.test.mjs    node --test: payment.js against a live server (run by test_e2e_payment_flow.py)
docs/acceptance/WI-<n>.md      acceptance criterion → code → tests map, per ticket
docker-compose.yml, Dockerfile Postgres, migration runner, API (healthcheck on /healthz)
.github/workflows/ci.yml       secret scan, pytest on SQLite and Postgres, docker build
Makefile                       install / test / test-web / scan-web / dev-token / run / migrate / db-up / test-pg
```

## Commands
- Install: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` (or `make install`)
- Test everything: `.venv/bin/pytest` (or `make test`). Frontend only: `cd web && npm test`.
- Run locally on SQLite: `DATABASE_URL=sqlite:///./dev.db GATEWAY_WEBHOOK_SECRET=dev .venv/bin/alembic upgrade head`
  then `make run` with the same env plus `AUTH_TOKEN_SECRET=<16+ chars>`; `make dev-token` (same env) prints a
  pay-page URL with `#token=...`. Or set `AUTH_DISABLED=true` and open
  `http://localhost:8000/pay/?orderId=6f1c2b8e-3d4a-4c5b-9e7f-112233445566`.
- Postgres via Docker: `docker compose up api` (starts db, runs migrations, serves on :8000); token:
  `docker compose exec api python -m payment_service.auth --order-id <uuid>`.
- Secret scan of the page: `make scan-web` (also in CI and `tests/test_secret_scan.py`).
- Tests on Postgres: `make test-pg` (uses the `payment_test` database created by compose).
- New migration: `.venv/bin/alembic revision -m "..."`, then rename the file to `NNNN_description.py`
  with `revision = "NNNN"`.

## API (see ticket OpenAPI)
- Customer endpoints (orders, payments) need `Authorization: Bearer <token>` → else 401 `unauthorized` with
  `WWW-Authenticate: Bearer`. A token scoped to an order (`order_id` claim) gets 404 for any other order and
  its payments. Webhook and `/healthz` take no bearer token.
- `GET /api/v1/orders/{orderId}` → 200 Order `{id, summary, amount_minor, currency, available_payment_methods,
  payment_idempotency_key, created_at}` | 404 | 502 (order-service failure and no cached projection).
  Projects the order into `orders` on first sight (cache; never overwritten) and issues/rotates the key.
- `POST /api/v1/payments` (header `Idempotency-Key` = the order's `payment_idempotency_key`, body
  `order_id, payment_method, amount_minor?, currency?`) → 201 new Payment | 200 replay of the key |
  400 missing/invalid/unknown field, method not available, key not issued for the order, or amount/currency ≠ order |
  404 order never projected | 409 key used for another order | 502 gateway refused (payment stored as failed).
  Amount/currency are always copied from the projection.
- Payment JSON: `id, order_id, status, payment_method, amount_minor, currency, idempotency_key,
  gateway_session_id, gateway_checkout_url, failure_reason, created_at, updated_at`.
- `GET /api/v1/payments/{paymentId}` → 200 Payment | 404 (unknown or malformed id).
- `POST /api/v1/payments/webhook` (contract path; alias `/api/v1/payments/webhooks/gateway`) (header `X-Gateway-Signature` = hex HMAC-SHA256 of raw body;
  `X-API-Key` checked when `GATEWAY_WEBHOOK_API_KEY` is set). Body
  `{"id", "type": "payment.success|payment.failed|payment.pending", "data": {"payment_id"|"gateway_reference", "failure_reason"?}}`
  (`gateway_reference` is matched against `payments.gateway_session_id`)
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
- Idempotency (server-issued, HLD DEC-001): `repository.ensure_payment_idempotency_key` issues the order's key on
  first GET and rotates it only once the payment made with it is `failed` (row locked FOR UPDATE on Postgres).
  A new payment needs exactly the currently issued key; a replayed key for the same order returns the original
  payment (no second gateway call). Hence a success can never be followed by a second charge on the order. The
  unique index on `idempotency_key` is the final guard against concurrent inserts.
- JSON columns use `JSONPayload` (`none_as_null=True`), so Python `None` is SQL NULL and NOT NULL applies.
- Status is changed only by the backend (gateway acceptance → processing; verified webhook →
  success/failed/pending). success/failed are terminal. Request bodies use `extra="forbid"`, so a
  client-supplied `status` is a 400. Webhook events dedupe by `gateway_event_id`; an unverified
  stored event is replaced by a later verified delivery of the same id.
- Frontend: never decide success from the gateway redirect (`gateway_status` param is only
  compared and flagged); always show the result of `GET /api/v1/payments/{paymentId}`, using the
  paymentId stored in sessionStorage from the initiation response. Pay Now is guarded by a
  synchronous in-flight flag + disabled button; the Idempotency-Key is the order's
  `payment_idempotency_key` (never generated client-side); after an answered error/failed attempt the page
  reloads the order to get the rotated key. An order missing id/amount/currency (`validateOrder`) or an
  invalid `orderId` disables Pay Now and shows an error. Methods shown = `available_payment_methods`.
  Distinct views/test ids for validation, network error, API error, failed, pending, success.
- Never hang (REQ-009 AC2): `createApi` aborts requests after `REQUEST_TIMEOUT_MS` (→ NetworkError);
  after hand-off the Pay Now controller arms a `CHECKOUT_TIMEOUT_MS` timer (injectable `setTimer`) that
  confirms the payment via GET if the page is still there; `controller.resume(orderId)` does the same on
  page load and on `pageshow` from the back/forward cache (Back from the gateway) and renders a
  `confirmed` view (success keeps Pay Now locked). The result page uses `pollConfirmedStatus` (bounded
  re-checks while pending, then settles on Pending + "Check again"). Unit tests inject `setTimer`/`sleep`
  so no real timers keep `node --test` alive.
- order-service and gateway are external: code against the `OrderService` / `PaymentGateway`
  protocols; the in-repo implementations are dev stubs. `orders` is a local projection of
  order-service data (assumed immutable once placed, HLD RSK-002); `payments.order_id` → `orders.id` FK.
  `payments.gateway_session_id`/`gateway_checkout_url` stay nullable because a gateway-refused attempt is
  kept as a failed row (the data model's NOT NULL / "gateway before write" is not adopted yet).
- Secrets only via environment variables (server side); `.env` is git-ignored, `.env.example` is
  the template. `create_app` calls `Settings.validate()`: no `GATEWAY_WEBHOOK_SECRET`, or no
  `AUTH_TOKEN_SECRET` (≥16 chars) unless `AUTH_DISABLED=true`, → `ConfigError` at startup. New secret settings:
  add with `field(repr=False)`, list in `secret_scan.SECRET_SETTINGS`, document in `.env.example`.
  Nothing under `web/` may reference a gateway secret (`secret_scan`, `tests/test_api.py`).
- Auth (HLD RSK-004, placeholder until the real session issuer is known): tokens are
  `b64url(json{sub, exp, order_id?}).b64url(HMAC-SHA256)` keyed by `AUTH_TOKEN_SECRET`. New customer routes go
  on `router` (auth is automatic) and take `principal: PrincipalDep` to check `principal.may_access_order(...)`
  (mismatch → 404, not 403). Gateway-called routes go on `webhook_router`. Tests: `make_client` in
  `tests/test_api.py` sends a valid token by default (`token=None` override for none).
- Frontend auth: the page is opened as `/pay/?orderId=...#token=...`; `readAccessToken` moves the token into
  sessionStorage (`payment:access-token`), the page strips the fragment, and `createApi(..., {getToken})` sends
  `Authorization: Bearer`. The token is the customer's session, never a gateway credential.
- End-to-end acceptance: `tests/test_e2e_payment_flow.py` starts the app with uvicorn on a free port (auth on,
  FakeGateway, one order per scenario), runs `tests/e2e/pay_flow.test.mjs` (real `payment.js`, real HTTP, signs
  webhooks as the gateway; kept outside `web/` because it holds the webhook secret), then checks DB invariants.
  Name each Node test after the criteria it proves (`AC5, AC9: ...`) and keep `docs/acceptance/WI-<n>.md` in step.
- A replayed POST that returns an already-`success` payment is shown as confirmed success (Pay Now locked), never
  redirected to the gateway again.
- Not built yet: the real token issuer (order placement), real order-service/gateway adapters,
  fallback gateway status polling.
