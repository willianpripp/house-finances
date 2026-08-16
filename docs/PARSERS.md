# Statement parsers

Every statement format this app can read is one module in
`backend/app/services/parsers/`. Nothing enumerates those modules: the package
is a **registry**. A module declares what it can parse, the registry discovers
it, and the detector resolves an uploaded filename against whatever is present.

Dropping a module into that directory adds a format. Deleting one removes it.
Neither touches any other file.

## What ships here

This repository ships **five bank-specific modules covering three ingestion
paths**, rather than the full set the author runs privately. Four of the five
declare a `SPEC` and are therefore registered parsers; the fifth,
`synchrony.py`, is a shared statement reader that other modules wrap and
declares no spec of its own. (The two paste readers described further down,
`cc_paste.py` and `checking_paste.py`, are not in that count: they are generic
layout readers, not tied to any institution, and they carry no spec either.)

| Ingestion path | Modules | Formats |
|---|---|---|
| A card whose transactions normally arrive through the bank-sync provider, with the statement reader kept as the manual fallback. | `citi.py` | CSV, PDF |
| The manual statement path, which exists precisely because no aggregator covers that issuer. The card module is a thin wrapper; the issuer's shared statement layout lives in `synchrony.py`, so its sibling cards are one small module each. | `amazon.py` on `synchrony.py` | PDF |
| The other bank-sync region, plus the checking side. `nubank_conta.py` is the interesting one: it shows classification, the match-rules table and the checking result shape. | `nubank.py`, `nubank_conta.py` | CSV, PDF |

Every one of those readers that accepts both formats dispatches on the content's
magic bytes rather than on the file extension: `parse()` checks for `%PDF` and
routes to the PDF reader, and everything else goes to the CSV reader. The
`formats` tuple on the spec exists for the user-facing hint, not for dispatch.

That is deliberate. The set of parsers a repository ships is the set of
accounts its author holds, and a public repository does not need to publish
that. The pattern generalizes: everything below is what the private tree does,
with more modules.

## How the registry works

`registry.py` holds three things: the `ParserSpec` dataclass, the list of
registered specs, and `discover()`.

`discover()` walks the package with `pkgutil.iter_modules`, imports every
submodule, and collects the module-level `SPEC` (a single `ParserSpec`) or
`SPECS` (a tuple, for a module whose one parse function serves several filename
shapes). It runs once, on first use, and is idempotent.

Two consequences worth stating plainly:

- **A module that is absent registers nothing.** No import error, no list to
  edit. `detect()` returns `None` for its filenames, `/imports` renders, the
  suite passes. That is what lets this tree ship a subset.
- **Precedence is declared, not inherited from import order.** The order a
  package's modules import in is not something to rely on, so each spec carries
  an explicit `order`. Ties break on module name then position within the
  module, so the resolved sequence is identical on every machine.

## `ParserSpec` fields

| Field | Meaning |
|---|---|
| `source` | This parser's own identity, recorded verbatim on the `import_logs` row. A plain string the module owns: SCREAMING_SNAKE, and stable forever, because rows already written carry it. |
| `parse` | The parse callable. Card parsers take `(content: bytes)` and return `ParseResult`. Checking parsers take `(content: bytes, rules: MatchRules)` and return `CheckingParseResult`. |
| `patterns` | Filename keywords, any-of over the outer tuple, all-of inside each inner one. `(("wf", "checking"), ("wells", "fargo"))` reads "wf and checking, or wells and fargo". |
| `formats` | The file formats this parser accepts, for the user-facing hint ("CSV", "PDF"). |
| `kind` | `ParserKind.CARD` or `ParserKind.CHECKING`. The two flows have different signatures and different importers, so a spec never crosses over. |
| `order` | Precedence inside the kind, lower first. Leave gaps (10, 20, 30) so a new parser can slot between two existing ones. |
| `excludes` | Words that disqualify a filename even when a pattern matched. |
| `suffixes` | When set, the filename must end in one of them. |
| `paste_capable` | The same layout can also be pasted as text, so the UI offers the paste box for accounts that resolve to this parser. |
| `holder_name_aware` | The parse function takes the household holder names as a second argument (some statements print cardholders as section headers, and those names are household data rather than code). |

## How `detect` resolves a filename

`detect.py` lowercases the filename and then:

1. Tries `detect_checking` first. Checking patterns beat card patterns, so a
   file both flows could claim goes to the checking importer. `detect` returns
   `None` for it, which is how the router routes.
2. Walks the checking specs, then the card specs, in `order`.
3. For each spec: the filename must end in one of `suffixes` when the spec sets
   any, must contain none of `excludes`, and must satisfy at least one
   `patterns` entry in full.
4. Returns `(source, parse)` for the first spec that matches, or `None`.

`GET /api/imports/detect?filename=...` exposes exactly that, which is why no
template carries a keyword list. `services/import_hints.py` reads the same specs
from the registry to render per-account guidance ("CSV or PDF, filename must
contain ..."), matching a payment method's name against the specs' keywords. An
account with no matching parser gets a neutral hint, and so does every account
in a tree that does not ship a parser for it.

## Adding your own parser

A card parser, end to end:

```python
"""Statement reader for <your format>."""
from __future__ import annotations

from decimal import Decimal

from app.services.parsers.registry import ParserKind, ParserSpec
from app.services.parsers.types import ParsedTransaction, ParseResult


def parse(content: bytes) -> ParseResult:
    result = ParseResult(parser="my_bank")
    for row in _rows(content):
        result.transactions.append(
            ParsedTransaction(
                transaction_date=row.date,
                description=row.description,
                amount=Decimal(row.amount),   # positive charge, negative refund
                is_payment=False,             # True routes the row to result.payments
            )
        )
    return result


SPEC = ParserSpec(
    source="MY_BANK",
    parse=parse,
    kind=ParserKind.CARD,
    order=500,
    patterns=(("mybank",),),
    formats=("CSV",),
)
```

That is the whole integration. No registration call, no import anywhere else.

Three notes:

- **`amount` is signed and positive means a charge.** Payment and autopay rows
  are marked `is_payment=True`; the importer surfaces them in the preview as a
  sanity check and does not store them as transactions, because card balances
  come from the checking side (money leaving an account) and never from the
  card statement itself.
- **`source` is yours to name, and no migration is involved.**
  `import_logs.source` is a text column. The valid values are the registered
  parsers' own sources plus the handful of paths no parser owns (`MANUAL`, the
  two bank-sync providers, the car loan), assembled at runtime in
  `services/import_sources.py` and checked when the row is written, so an
  unrecognised string is still rejected. Pick it once and never change it: rows
  already written carry the string.
- **Optionally return `statement_close_date` and `due_date`** on the
  `ParseResult` if the statement header carries them. The importer updates the
  payment method's close and due day on commit when they are present.

## What the checking base gives you

Bank statements are richer than card statements: most lines are not new
spending, and the file carries balances worth recording. `checking.py` is the
shared base, and a checking parser is mostly a reader that fills in its shapes.

**`CheckingActivity`** is one row: date, description, signed amount (positive is
a deposit), the printed running balance when there is one, a `CheckingClass` and
a `match_hint`.

**`CheckingClass`** is what the row means: `CC_PAYMENT`, `SALARY`,
`RENT_DEPOSIT`, `EXTRA_INCOME`, `TAX_PAYMENT`, `INTEREST`,
`INTERNAL_TRANSFER`, `SPENDING`, or `FIXED_MATCH`. The importer acts on the
class: spending becomes a transaction, a card payment reduces that card's
balance, a salary reconciles the month's withholding rows, internal transfers
and interest are skipped.

**`classify_description(desc, amount, rules=...)`** is the provisional
classification, first match wins, and it is pure. The keywords are not in the
code: they live in the `statement_match_rules` table and arrive as a
`MatchRules` loaded once per import by `services/match_rules.load_match_rules`.
Rules apply in `sort_order`, so "earlier keyword wins" is configuration. Some
classes are gated by sign, because a person's name appears on outbound payments
as well as on their deposits, and only a credit can be a salary.

That table, `statement_match_rules`, is the extension point that matters as much
as the parser: it maps a keyword to a class and a `match_hint` (a card name for
a card payment, a household member's match key for a salary or a rent deposit).
Point it at your own descriptions and the classification follows, with no code
change. `HOLDER_NAME` and `NOISE` rows feed the parsers' line handling rather
than the classifier.

**Running balance and reconciliation.** A parser reports `beginning_balance`,
`ending_balance` and, per row, the statement's own running balance when it is
printed. Two things use it. First, a parser that walks a printed balance column
can check its own arithmetic row by row, which is how a layout change is caught
instead of silently dropping a line. Second, the committed import appends a
`savings_snapshots` row for the period end, so account balances are a side
effect of importing rather than something typed in. Set
`skip_snapshot=True` on the result when the printed balance is not a meaningful
savings figure, for instance an account swept to zero every month, and no
snapshot is written.

**Pasted activity.** `checking_paste.py` and `cc_paste.py` cover the same two
flows for text copied out of a bank web view, so activity can be imported before
the statement closes. They auto-detect a tab-separated layout and a multi-line
stanza layout, produce the same result shapes, and set `skip_snapshot=True`
because a paste has no statement-fenced period. Re-importing the official
statement later is idempotent: transactions deduplicate on the database unique
constraint, and card payments deduplicate on an amount and date window in the
ledger.

A checking spec differs from a card one only in `kind` and the parse signature:

```python
def parse(content: bytes, rules: MatchRules) -> CheckingParseResult:
    ...


SPEC = ParserSpec(
    source="CHECKING_MY_BANK",
    parse=parse,
    kind=ParserKind.CHECKING,
    order=500,
    patterns=(("mybank", "checking"),),
    formats=("PDF",),
    paste_capable=True,
)
```

## Tests to copy

`tests/test_parser_registry.py` pins the registry contract: discovery finds
parsers without naming them, precedence follows `order`, checking wins over
card, an unknown filename resolves to `None`, and a module that is not in the
tree is simply not registered. The last one is what makes a partial tree
supported rather than accidental.
