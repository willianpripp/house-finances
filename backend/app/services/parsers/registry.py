"""Parser registry: every parser module declares its own spec and is discovered.

Nothing in the package names the parser modules. `discover()` walks the package
with `pkgutil.iter_modules`, imports each submodule, and collects the module
level `SPEC` (or `SPECS`, for a module that serves several filename shapes with
one parse function). A module that is not present in a given tree is simply not
registered: no import fails, no list has to be edited, and a deployment can ship
a subset of the parsers.

Precedence is explicit. `ParserSpec.order` decides which spec is tried first
within a kind, because import order across a package is not something to rely
on: a broader keyword must sort after the narrower ones it would otherwise
swallow. Ties break on the registering module name and the spec's position
inside that module, so the resolved order is identical on every machine.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class ParserKind(str, Enum):
    """Which import flow a spec belongs to.

    The two flows have different parse signatures and different importers, so a
    spec never crosses over: CARD parsers return `ParseResult`, CHECKING parsers
    take the `MatchRules` and return `CheckingParseResult`.
    """

    CARD = "CARD"
    CHECKING = "CHECKING"


@dataclass(frozen=True)
class ParserSpec:
    """One parser and the filename shape that selects it.

    `patterns` is any-of, each inner tuple all-of: `(("wf", "checking"),
    ("wells", "fargo"))` reads "wf and checking, or wells and fargo".

    `source` is the parser's own identity, written verbatim to
    `import_logs.source` (a text column). Owning it here is what makes adding a
    parser a one-file change: there is no enum to extend and no migration.
    Convention, enforced by `register()`: SCREAMING_SNAKE, stable forever,
    because rows already written carry it.

    `order` is the precedence key inside the spec's kind (lower is tried
    first). `excludes` rejects a filename carrying any of those words;
    `suffixes`, when set, requires one of them. `paste_capable` says the same
    layout can also arrive as pasted text. `holder_name_aware` marks a card
    parse function that takes the household holder names as a second argument.
    """

    source: str
    parse: Callable[..., object]
    patterns: tuple[tuple[str, ...], ...]
    formats: tuple[str, ...]
    kind: ParserKind = ParserKind.CARD
    order: int = 1000
    excludes: tuple[str, ...] = field(default=())
    suffixes: tuple[str, ...] = field(default=())
    paste_capable: bool = False
    holder_name_aware: bool = False


@dataclass(frozen=True)
class _Entry:
    spec: ParserSpec
    module: str
    index: int

    @property
    def sort_key(self) -> tuple[int, str, int]:
        return (self.spec.order, self.module, self.index)


_entries: list[_Entry] = []
_discovered = False

# `source` is stored, not just compared, so a typo becomes a permanent value in
# the audit table. Checking the shape at registration fails the whole tree at
# discovery time instead — cheap, and it is the only moment a new parser's
# identity passes through common code.
_SOURCE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def register(spec: ParserSpec, *, module: str, index: int = 0) -> None:
    if not _SOURCE_RE.match(spec.source):
        raise ValueError(
            f"parser module {module!r} declares source {spec.source!r}; it is written "
            f"to import_logs.source verbatim and must be SCREAMING_SNAKE"
        )
    _entries.append(_Entry(spec=spec, module=module, index=index))


def _specs_of(module: object) -> list[ParserSpec]:
    declared = getattr(module, "SPEC", None)
    if isinstance(declared, ParserSpec):
        return [declared]
    many = getattr(module, "SPECS", ())
    return [spec for spec in many if isinstance(spec, ParserSpec)]


def discover() -> None:
    """Import every submodule of this package and register the specs found.

    Idempotent, and the flag is raised before the loop so that a module which
    reaches back into the registry while being imported cannot recurse.
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    package = importlib.import_module(__package__)
    for info in pkgutil.iter_modules(package.__path__):
        if info.ispkg or info.name == __name__.rsplit(".", 1)[-1]:
            continue
        module = importlib.import_module(f"{__package__}.{info.name}")
        for index, spec in enumerate(_specs_of(module)):
            register(spec, module=info.name, index=index)


def specs(kind: ParserKind) -> tuple[ParserSpec, ...]:
    discover()
    entries = sorted((e for e in _entries if e.spec.kind is kind), key=lambda e: e.sort_key)
    return tuple(entry.spec for entry in entries)


def card_specs() -> tuple[ParserSpec, ...]:
    return specs(ParserKind.CARD)


def checking_specs() -> tuple[ParserSpec, ...]:
    return specs(ParserKind.CHECKING)


def holder_name_aware_parsers() -> frozenset[Callable[..., object]]:
    """Card parse functions that take the holder names as a second argument.

    Passing them to a parser that does not accept the argument would be a
    TypeError, so the dispatch in `detect.run_cc_parser` is explicit rather
    than blanket.
    """
    return frozenset(spec.parse for spec in card_specs() if spec.holder_name_aware)


def registered_modules() -> tuple[str, ...]:
    discover()
    return tuple(sorted({entry.module for entry in _entries}))
