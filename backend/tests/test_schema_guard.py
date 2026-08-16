"""The boot-time schema guard: the 2026-08-06 outage, never again."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services.schema_guard import SchemaDriftError, assert_schema_current


@pytest.fixture
def engine():
    import app.db as app_db

    return app_db.engine


def test_guard_passes_when_the_database_is_at_head(engine):
    assert_schema_current(engine)  # must not raise


def test_guard_refuses_a_database_behind_head(engine):
    with engine.begin() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        conn.execute(
            text("UPDATE alembic_version SET version_num = 'f3b6d2c07a15'")
        )
    try:
        with pytest.raises(SchemaDriftError, match="alembic upgrade head"):
            assert_schema_current(engine)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": current}
            )


def test_guard_override_serves_anyway_and_screams(engine, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allow_schema_drift", True)
    with engine.begin() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        conn.execute(text("UPDATE alembic_version SET version_num = 'f3b6d2c07a15'"))
    try:
        assert_schema_current(engine)  # logs instead of raising
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :v"), {"v": current}
            )
