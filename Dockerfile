FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir .

# DATABASE_URL must be provided at runtime; no secrets are baked into the image.
CMD ["alembic", "upgrade", "head"]
