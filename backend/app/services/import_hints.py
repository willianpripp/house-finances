"""Per-account import guidance, derived from the parser specs.

The filename keywords are code (each parser module's `SPEC`, collected by
`services/parsers/registry.py`); the accounts are the household's data
(`payment_methods`). The UI must not carry a list of institutions, so the server
resolves each payment method to the parser whose keywords match its name and
hands the template a hint keyed by payment method id. An account with no
matching parser gets a neutral hint, which is also what every account gets in a
tree that ships no parser for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentMethod, PaymentMethodType
from app.services.parsers.registry import ParserSpec, card_specs, checking_specs

NO_PARSER_HINT = "No statement parser for this account. Add its rows on /transactions."

# Prefix matching lets an account name carry a shortened form of a keyword
# ("Nubank Credit" for `nubank_credito`). Six characters is the floor that
# still rejects a short common word swallowed by a longer keyword
# ("conta" inside a longer institution keyword that merely starts the same way).
_MIN_PREFIX_LEN = 6


@dataclass(frozen=True)
class ImportHint:
    text: str
    paste_capable: bool


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[^a-z0-9]+", text.lower()) if part)


def _token_matches(part: str, token: str) -> bool:
    if part == token:
        return True
    if len(part) < _MIN_PREFIX_LEN or len(token) < _MIN_PREFIX_LEN:
        return False
    return part.startswith(token) or token.startswith(part)


def _keyword_parts(keyword: str) -> tuple[str, ...]:
    return tuple(part for part in keyword.split("_") if part)


def _pattern_score(pattern: tuple[str, ...], tokens: tuple[str, ...]) -> int:
    """How much of a pattern the account name carries, 0 when the leading
    keyword (the distinctive one) is absent. Requiring the lead keeps a generic
    word shared with an unrelated account name from selecting a parser."""
    parts = _keyword_parts(pattern[0])
    if not all(any(_token_matches(part, token) for token in tokens) for part in parts):
        return 0
    return sum(
        1
        for keyword in pattern
        for part in _keyword_parts(keyword)
        if any(_token_matches(part, token) for token in tokens)
    )


def _spec_for_name(specs: tuple[ParserSpec, ...], name: str) -> ParserSpec | None:
    tokens = _tokens(name)
    best: ParserSpec | None = None
    best_score = 0
    for spec in specs:
        if any(_token_matches(word, token) for word in spec.excludes for token in tokens):
            continue
        score = max(_pattern_score(pattern, tokens) for pattern in spec.patterns)
        if score > best_score:
            best, best_score = spec, score
    return best


def hint_text(spec: ParserSpec) -> str:
    formats = " or ".join(spec.formats)
    patterns = " or ".join(
        " and ".join(f'"{word}"' for word in pattern) for pattern in spec.patterns
    )
    text = f"{formats}, filename must contain {patterns}"
    if spec.excludes:
        text += " and not " + " or ".join(f'"{word}"' for word in spec.excludes)
    if spec.paste_capable:
        text += ". Pasting the activity works too"
    return f"{text}."


def hint_for_payment_method(pm: PaymentMethod) -> ImportHint:
    if pm.type is PaymentMethodType.CREDIT_CARD:
        specs = card_specs()
    elif pm.type is PaymentMethodType.CHECKING:
        specs = checking_specs()
    else:
        return ImportHint(text=NO_PARSER_HINT, paste_capable=False)

    spec = _spec_for_name(specs, pm.name)
    if spec is None:
        return ImportHint(text=NO_PARSER_HINT, paste_capable=False)
    return ImportHint(text=hint_text(spec), paste_capable=spec.paste_capable)


def hints_for_payment_methods(db: Session) -> dict[int, dict[str, object]]:
    """Template payload: `{payment_method_id: {"text": ..., "paste_capable": ...}}`."""
    pms = db.scalars(select(PaymentMethod).order_by(PaymentMethod.id)).all()
    payload: dict[int, dict[str, object]] = {}
    for pm in pms:
        hint = hint_for_payment_method(pm)
        payload[pm.id] = {"text": hint.text, "paste_capable": hint.paste_capable}
    return payload
