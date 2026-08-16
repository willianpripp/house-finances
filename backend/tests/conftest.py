"""Test harness.

The tests never touch the real tables. Isolation is a **throwaway Postgres
schema**, not a throwaway database, so the suite also works where the app's
role cannot CREATEDB: every session creates `test_run`, points the
connection's `search_path` at it alone (never `public`, so a missing table
fails loudly instead of silently reading real data), runs the migrations into
it, seeds the fixture household, and drops it at the end.

`DATABASE_URL`-style settings are read by `app.config` at import time, so the
engine is rebuilt here with the schema-scoped connect args before any app
module is imported.
"""
from __future__ import annotations

import os  # noqa: E402

TEST_SCHEMA = "test_run"

# Every libpq connection in this process lands in the test schema — the app's
# engine, Alembic's engine, everything. Set before any app import.
os.environ["PGOPTIONS"] = f"-csearch_path={TEST_SCHEMA}"

# Session-JWT secret, before app.config loads. Tests mint and verify real
# tokens; only the secret is fake.
os.environ.setdefault("AUTH_SECRET", "test-secret-not-production")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402

# The suite runs against a scratch database that sits in the same cluster as
# the real one (`make test` creates it). Schema isolation alone is not enough
# once the two share a cluster: the database NAME is the outer boundary, so
# refuse anything that is not clearly a test database — a mispasted URL must
# never point the schema-dropper at live data.
_dbname = settings.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
assert _dbname.endswith("_test"), (
    f"refusing to run tests against database {_dbname!r}: the name must end in "
    f"'_test' (run via `make test`, which targets a scratch '*_test' database)"
)


def _engine():
    """Engine pinned to the test schema. `public` is deliberately excluded."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
        connect_args={"options": f"-csearch_path={TEST_SCHEMA}"},
    )


# Enum types whose migration calls `.create(..., checkfirst=True)`. Because
# checkfirst honours the search_path, it finds these in `public` and skips —
# leaving the test schema without them and the dependent CREATE TABLE broken.
# Types created unconditionally are NOT listed here: cloning those would make
# their migration fail with DuplicateObject instead.
_CHECKFIRST_ENUMS = ("recurrence_kind", "asset_kind", "plaid_item_status", "receivable_direction")


def _clone_enum_types(admin) -> None:
    """Copy the checkfirst-created enum types from `public` into the test schema."""
    with admin.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) "
                "FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' "
                "GROUP BY t.typname"
            )
        ).all()
        for typname, labels in rows:
            if typname not in _CHECKFIRST_ENUMS:
                continue
            values = ", ".join(f"'{label}'" for label in labels)
            conn.execute(text(f'CREATE TYPE "{TEST_SCHEMA}"."{typname}" AS ENUM ({values})'))


@pytest.fixture(scope="session", autouse=True)
def database():
    assert TEST_SCHEMA.startswith("test_"), "refusing to drop a non-test schema"

    admin = create_engine(
        settings.database_url,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": "-csearch_path=public"},
    )
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))

    engine = _engine()

    # Redirect the app's engine/session at the test schema before anything
    # imports them.
    import app.db as app_db

    app_db.engine = engine
    app_db.SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # Alembic creates its enum types with checkfirst=True, which honours the
    # search_path — it would find the ones already in `public` and skip, leaving
    # the test schema without them. Clone the type definitions up front.
    _clone_enum_types(admin)

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")  # PGOPTIONS puts these tables in TEST_SCHEMA

    with engine.connect() as conn:
        for table in ("users", "transactions", "income_entries", "receivables"):
            assert conn.execute(
                text(f"SELECT to_regclass('{TEST_SCHEMA}.{table}')")
            ).scalar(), f"{table} was not created in {TEST_SCHEMA}"

    from tests.factories import seed_household

    session = app_db.SessionLocal()
    try:
        seed_household(session)
        session.commit()
    finally:
        session.close()

    yield

    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
    admin.dispose()


@pytest.fixture
def db():
    import app.db as app_db

    session = app_db.SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import app.db as app_db
    from app.main import app

    def _get_db():
        session = app_db.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    from app.db import get_db

    app.dependency_overrides[get_db] = _get_db
    try:
        test_client = TestClient(app)
        # The auth middleware guards every route, so the fixture client comes
        # pre-authenticated as the primary user. test_auth.py exercises the
        # unauthenticated paths with its own bare client.
        from app.services.auth import COOKIE_NAME, create_token

        test_client.cookies.set(COOKIE_NAME, create_token(1))
        yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
