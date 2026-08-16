"""Pick the right parser based on filename.

Two flavors of imports — credit-card statements (returning ParseResult) and
bank-checking statements (returning CheckingParseResult). Routers call the
matching `detect_*` function and dispatch to the matching service flow.

The filename keywords live in the parser modules themselves, one `SPEC` per
module (`registry.py`), and this module only resolves a filename against
whatever the registry discovered. `app/services/import_hints.py` derives the
user-facing guidance from the same specs, so a new parser never needs a second,
drifting copy of its keywords in a template.

A tree that ships a subset of the parser modules resolves the subset: an absent
module registers nothing and its filenames simply detect as None.

The `source` half of what these return is the spec's own string, written
verbatim to `import_logs.source` (see `services/import_sources.py`).
"""
from __future__ import annotations

from typing import Callable

from app.services.parsers.checking import CheckingParseResult, MatchRules
from app.services.parsers.registry import (
    ParserKind,
    ParserSpec,
    card_specs,
    checking_specs,
    holder_name_aware_parsers,
)
from app.services.parsers.types import ParseResult

__all__ = [
    "ParserKind",
    "ParserSpec",
    "card_specs",
    "checking_specs",
    "detect",
    "detect_checking",
    "run_cc_parser",
]


def _spec_matches_filename(spec: ParserSpec, name: str) -> bool:
    if spec.suffixes and not any(name.endswith(suffix) for suffix in spec.suffixes):
        return False
    if any(word in name for word in spec.excludes):
        return False
    return any(all(word in name for word in pattern) for pattern in spec.patterns)


def run_cc_parser(
    parse_fn: Callable[..., ParseResult],
    content: bytes,
    holder_names: tuple[str, ...] = (),
) -> ParseResult:
    if parse_fn in holder_name_aware_parsers():
        return parse_fn(content, holder_names)
    return parse_fn(content)


def detect(filename: str) -> tuple[str, Callable[[bytes], ParseResult]] | None:
    name = filename.lower()
    if detect_checking(filename) is not None:
        return None  # checking statements are handled by detect_checking
    for spec in card_specs():
        if _spec_matches_filename(spec, name):
            return spec.source, spec.parse
    return None


def detect_checking(
    filename: str,
) -> tuple[str, Callable[[bytes, MatchRules], CheckingParseResult]] | None:
    name = filename.lower()
    for spec in checking_specs():
        if _spec_matches_filename(spec, name):
            return spec.source, spec.parse
    return None
