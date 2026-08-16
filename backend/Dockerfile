# python:3.13-slim-bookworm, NOT alpine. Alpine is musl, and pandas and psycopg
# have no musl wheels, so an alpine build compiles them from source: a slow build
# and a fragile image for no benefit.
FROM python:3.14-slim-bookworm

# psycopg[binary] bundles libpq, so no postgresql-client is needed at runtime.
# pdfplumber needs nothing beyond what pillow's wheel already carries.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first, as their own layer: application code changes far more often
# than dependencies, so this keeps rebuilds to seconds instead of minutes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

# Runs as a non-root user. The app writes nothing to its own filesystem: uploads
# are parsed in memory and everything durable lives in Postgres.
RUN useradd --create-home --uid 10001 finances \
 && chown -R finances:finances /app
USER finances

EXPOSE 8002

# --host 0.0.0.0 is correct HERE and only here: it is the container's own
# namespace. What (if anything) publishes the port to a network is the compose
# file's decision; the app's own login is the auth layer either way.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
