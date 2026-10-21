VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: install test test-web run migrate downgrade db-up db-down test-pg

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install -e '.[dev]'

test:
	$(PY) -m pytest

test-web:
	cd web && npm test

# Requires DATABASE_URL and GATEWAY_WEBHOOK_SECRET (see .env.example).
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
