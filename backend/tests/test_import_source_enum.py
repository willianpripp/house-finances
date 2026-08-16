"""`import_logs.source` is text, and the application is what validates it.

It used to be a Postgres enum, and the failure that justified the type was
real: a label sat on the Python enum, wired to a live parser, while no
migration ever added it to the type — the parse succeeded and the INSERT blew
up. But the type could only ever know the labels someone remembered to
migrate, which made adding a parser a schema change and made the schema itself
a list of every institution the tree can read.

So the contract moved to where the answer actually lives: a source is valid if
a registered parser declares it (`ParserSpec.source`) or it is one of the paths
no parser owns (`ImportSource`). These tests pin that contract against whatever
this tree ships, so they hold in the private repo (eleven parsers) and in the
public export (three) without either knowing which one it is.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.models import ImportLog, ImportSource
from app.services import import_sources
from app.services.parsers.registry import card_specs, checking_specs


def _write(db, source) -> ImportLog:
    log = ImportLog(
        filename=f"{source}.csv",
        source=source,
        transaction_count=0,
        skipped_count=0,
    )
    db.add(log)
    db.flush()
    return log


@pytest.mark.parametrize(
    "spec",
    list(card_specs() + checking_specs()),
    ids=lambda spec: spec.source,
)
def test_every_registered_parser_source_can_be_written(db, spec):
    """The direction the old enum kept breaking: a parser this tree ships
    whose source the storage layer will not accept."""
    log = _write(db, spec.source)
    assert db.scalar(select(ImportLog.source).where(ImportLog.id == log.id)) == spec.source


@pytest.mark.parametrize("source", list(ImportSource), ids=lambda s: s.name)
def test_every_non_parser_source_can_be_written(db, source):
    log = _write(db, source)
    stored = db.scalar(select(ImportLog.source).where(ImportLog.id == log.id))
    assert stored == source.value
    assert type(stored) is str


def test_an_unknown_source_is_rejected_before_it_reaches_the_database(db):
    """Text columns take anything, so the check has to be ours. It fires on
    assignment, which is why nothing is flushed here."""
    with pytest.raises(ValueError, match="unknown import source"):
        ImportLog(filename="x.csv", source="BANCO_INVENTADO", transaction_count=0, skipped_count=0)

    # A source whose parser is not in this tree is exactly as unknown: the
    # public export ships fewer parsers and must reject the rest.
    absent = "DEFINITELY_NOT_A_PARSER_IN_THIS_TREE"
    assert absent not in import_sources.known_sources()
    with pytest.raises(ValueError):
        ImportLog(filename="x.csv", source=absent, transaction_count=0, skipped_count=0)


def test_a_historical_row_reads_back_even_when_no_parser_claims_it(db):
    """Validation is write-time only. `import_logs` is an audit trail, and a
    row written by a parser the tree has since dropped must still load."""
    db.execute(
        text(
            "INSERT INTO import_logs (filename, source, transaction_count, skipped_count) "
            "VALUES ('legacy.csv', 'A_RETIRED_PARSER', 0, 0)"
        )
    )
    db.flush()
    log = db.scalars(select(ImportLog).where(ImportLog.filename == "legacy.csv")).one()
    assert log.source == "A_RETIRED_PARSER"


def test_the_import_source_enum_type_is_gone_from_the_database(db):
    """The simplification itself: no type to extend, so no migration when a
    parser is added and nothing institution-shaped in the schema."""
    assert db.scalar(text("SELECT to_regtype('import_source')")) is None


def test_known_sources_is_the_union_of_the_two_halves():
    known = import_sources.known_sources()
    assert known == import_sources.parser_sources() | {s.value for s in ImportSource}
    assert import_sources.parser_sources(), "no parser registered a source"
