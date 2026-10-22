FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY prompts ./prompts

EXPOSE 8000
# Compose supplies SUPPORT_DATABASE_URL, BILLING_*, IDENTIFIER_HASH_KEY, ALLOWED_ORIGINS and PERSONAS_PATH.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn support_api.main:build --factory --host 0.0.0.0 --port 8000 --no-access-log"]
