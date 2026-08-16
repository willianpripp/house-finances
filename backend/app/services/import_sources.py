"""What `import_logs.source` is allowed to hold, and the check that enforces it.

The column is TEXT. It used to be a Postgres enum, which made the database the
authority on the set of ingestion paths — and therefore made "add a parser"
mean "write a migration", and made the schema itself a published list of every
institution the tree can read. The set is now assembled in the application from
the two places that actually know:

- **parsers** — every registered `ParserSpec.source`, discovered by walking the
  package (`services/parsers/registry.py`). A tree that ships a subset of the
  parser modules accepts exactly that subset, with nothing to keep in step.
- **everything else** — `ImportSource`, the handful of paths no parser owns
  (manual entry, the two bank-sync providers, the car loan).

Validation is write-time only, and lives on the model (`models/import_log.py`
calls `normalize_import_source` from a `@validates` hook). That is the one
place all the write paths converge — card importer, checking importer, manual
paste, Plaid sync, Pluggy commit, the one-off scripts — and no HTTP request
ever carries a source, so a request-body validator would be guarding a boundary
nothing crosses. Reads are deliberately unchecked: a historical row may name a
parser this tree no longer ships, and refusing to load it would break the
audit trail the table exists to be.
"""
from __future__ import annotations

from app.models.enums import ImportSource


def parser_sources() -> frozenset[str]:
    """The `source` of every parser spec registered in THIS tree."""
    # Imported here rather than at module scope: discovery imports every parser
    # module, and those import from `app.models`, which imports this module's
    # caller. Deferring it keeps the package import graph acyclic.
    from app.services.parsers.registry import card_specs, checking_specs

    return frozenset(spec.source for spec in card_specs() + checking_specs())


def known_sources() -> frozenset[str]:
    return frozenset(source.value for source in ImportSource) | parser_sources()


def normalize_import_source(value: object) -> str:
    """Return the plain string to store, or raise `ValueError`.

    Accepts an `ImportSource` member or the string itself, so call sites keep
    reading `ImportSource.PLAID` while the column stays text.
    """
    if isinstance(value, ImportSource):
        return value.value
    if not isinstance(value, str):
        raise ValueError(f"import source must be a string, got {type(value).__name__}")
    if value not in known_sources():
        raise ValueError(
            f"unknown import source {value!r}. Valid values are the registered "
            f"parsers' own plus {sorted(s.value for s in ImportSource)}; a new "
            f"parser declares its source on its ParserSpec."
        )
    return value
