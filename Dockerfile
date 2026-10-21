FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ORDERS_FILE=/app/dev/orders.json WEB_DIR=/app/web/public

COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY web/public ./web/public
COPY dev ./dev

# DATABASE_URL and GATEWAY_WEBHOOK_SECRET must be provided at runtime; no secrets are baked in.
EXPOSE 8000
CMD ["uvicorn", "--factory", "payment_service.api.app:create_app", "--host", "0.0.0.0", "--port", "8000"]
