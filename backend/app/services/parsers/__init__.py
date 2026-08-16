"""Statement parsers, discovered rather than enumerated.

The registry walks this directory (`registry.discover()`), imports every
submodule, and collects the `SPEC` / `SPECS` each parser module declares.
Nothing here names a parser module, so a tree that ships only some of them
imports, boots and serves imports for exactly the ones present. Discovery runs
on first use, not on import, so a parser module can import this package's
shared helpers without a cycle.

Adding a parser is dropping a module in this directory with a `SPEC`; removing
one is deleting the file. See `docs/PARSERS.md`.
"""
from app.services.parsers.checking import (
    CheckingActivity,
    CheckingClass,
    CheckingParseResult,
    classify_description,
)
from app.services.parsers.detect import detect, detect_checking, run_cc_parser
from app.services.parsers.registry import (
    ParserKind,
    ParserSpec,
    card_specs,
    checking_specs,
    discover,
    registered_modules,
)
from app.services.parsers.types import ParsedTransaction, ParseResult

__all__ = [
    "CheckingActivity",
    "CheckingClass",
    "CheckingParseResult",
    "ParsedTransaction",
    "ParseResult",
    "ParserKind",
    "ParserSpec",
    "card_specs",
    "checking_specs",
    "classify_description",
    "detect",
    "detect_checking",
    "discover",
    "registered_modules",
    "run_cc_parser",
]
