"""Refuse to serve when the database is behind the code's migrations.

Exists because of a real outage: the container was built from repo HEAD while
the database stayed two Alembic revisions behind, and nothing noticed. Every
page returned HTTP 200 with no data underneath. This turns that silent failure
into a loud one at deploy time.

Deliberately NOT auto-migrate: an unreviewed migration must never run against
the ledger on a restart nobody is watching (`restart: unless-stopped` fires at
3 a.m. too). The fix is always explicit:

    docker compose run --rm app alembic upgrade head

(`run --rm`, not `exec` — a refusing app crash-loops, so there is no running
container to exec into.)
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import settings

logger = logging.getLogger("uvicorn.error")

# backend/ — where alembic.ini and migrations/ live. Same relative layout when
# running from a checkout and inside the image (WORKDIR /app).
_BACKEND_DIR = Path(__file__).resolve().parents[2]


class SchemaDriftError(RuntimeError):
    """The database revision does not match the code's migration head."""


def _script_heads() -> set[str]:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def _db_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('alembic_version')")).scalar()
        if exists is None:
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def assert_schema_current(engine: Engine) -> None:
    """Raise SchemaDriftError unless the DB sits exactly at the script head.

    `ALLOW_SCHEMA_DRIFT=1` downgrades the refusal to a screaming log line —
    an emergency hatch, not a mode to run in.
    """
    heads = _script_heads()
    current = _db_revision(engine)

    if current in heads:
        logger.info("schema guard: database at head %s", current)
        return

    fix = "docker compose run --rm app alembic upgrade head"
    problem = (
        f"database revision {current!r} != migration head(s) {sorted(heads)!r}. "
        f"The code expects a schema this database does not have; pages would "
        f"return 200 and render no data. Fix: {fix}"
    )

    if settings.allow_schema_drift:
        logger.error("schema guard OVERRIDDEN (ALLOW_SCHEMA_DRIFT=1): %s", problem)
        return

    raise SchemaDriftError(problem)
