VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: install test test-web scan-web dev-token run migrate downgrade db-up db-down test-pg

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -e '.[dev]'

test:
	$(PY) -m pytest

test-web:
	cd web && npm test

# Fails if the payment page contains gateway credentials or server-side secret names (REQ-017).
scan-web:
	$(PY) -m payment_service.secret_scan web/public

# Prints a customer bearer token and the pay-page URL for ORDER_ID (needs AUTH_TOKEN_SECRET).
ORDER_ID ?= 6f1c2b8e-3d4a-4c5b-9e7f-112233445566
dev-token:
	$(PY) -m payment_service.auth --order-id $(ORDER_ID)

# Requires DATABASE_URL, GATEWAY_WEBHOOK_SECRET and AUTH_TOKEN_SECRET or AUTH_DISABLED=true (see .env.example).
run:
	$(VENV)/bin/uvicorn --factory payment_service.api.app:create_app --reload --port 8000

# Requires DATABASE_URL (see .env.example).
migrate:
	$(VENV)/bin/alembic upgrade head

downgrade:
	$(VENV)/bin/alembic downgrade -1

db-up:
	docker compose up -d db

db-down:
	docker compose down

test-pg:
	TEST_DATABASE_URL=postgresql+psycopg://payment:payment@localhost:5432/payment_test $(PY) -m pytest
