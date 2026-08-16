"""The Python `ImportSource` and the Postgres `import_source` type must agree.

One label once sat on the Python enum, wired to a real parser, while no
migration ever added it to the Postgres type: the parse succeeded and the
`import_logs` INSERT at commit time was the thing that blew up. Both
directions are pinned here, and both are checked against whatever this tree
actually declares — so the assertions hold in the private repo (all eleven
parsers, their labels merged in from `app/models/import_sources_extra.py`) and
in the public export (three parsers, that module absent, the squashed
`0001_initial` carrying the smaller type). See the public-export decision.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.models import ImportLog, ImportSource


def _postgres_labels(db) -> list[str]:
    return list(
        db.scalars(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'import_source' ORDER BY e.enumsortorder"
            )
        ).all()
    )


@pytest.mark.parametrize("source", list(ImportSource), ids=lambda s: s.name)
def test_every_import_source_can_be_written_to_import_logs(db, source):
    log = ImportLog(
        filename=f"{source.value}.csv",
        source=source,
        transaction_count=0,
        skipped_count=0,
    )
    db.add(log)
    db.flush()

    assert db.scalar(select(ImportLog.source).where(ImportLog.id == log.id)) is source


def test_python_enum_and_postgres_type_hold_the_same_labels(db):
    """Set equality, not order: the type's label ORDER comes from the migration
    chain (a value renamed in place keeps its old position) and differs from
    the model's declaration order by design — `make verify-baseline` is what
    pins order, against the chain. What must never drift is membership: a
    Python member with no label fails at INSERT, and a label with no member
    means the schema still carries something this tree can no longer emit."""
    assert {source.name for source in ImportSource} == set(_postgres_labels(db))
