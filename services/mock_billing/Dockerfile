FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8001
# Compose supplies BILLING_DATABASE_URL, BILLING_API_KEY, FAULT_ADMIN_KEY and PERSONAS_PATH.
CMD ["sh", "-c", "alembic upgrade head && python -m mock_billing.seed && exec uvicorn mock_billing.main:build --factory --host 0.0.0.0 --port 8001 --no-access-log"]
