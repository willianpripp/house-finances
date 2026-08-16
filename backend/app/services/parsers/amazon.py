"""Amazon Visa (Synchrony Bank) credit-card statement (PDF).

Thin wrapper over the shared Synchrony parser. See `synchrony.py` for the
statement structure and parsing logic.
"""
from __future__ import annotations

from app.models.enums import ImportSource
from app.services.parsers.registry import ParserKind, ParserSpec
from app.services.parsers.synchrony import parse_synchrony
from app.services.parsers.types import ParseResult


def parse(content: bytes) -> ParseResult:
    return parse_synchrony(content, parser_name="amazon")


SPEC = ParserSpec(
    source=ImportSource.AMAZON,
    parse=parse,
    kind=ParserKind.CARD,
    order=90,
    patterns=(("amazon",),),
    formats=("PDF",),
    paste_capable=True,
)
