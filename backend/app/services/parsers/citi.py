"""Citi credit card — both CSV export and full statement PDF.

CSV (columns Date, Description, Debit, Credit) is the original export from
citicards.com. The PDF is the full monthly statement the cardholder keeps on file.

`parse(content)` peeks the magic bytes and dispatches:
    - `%PDF` → `_parse_pdf` (statement format)
    - anything else → `_parse_csv` (legacy CSV)

Both shapes emit the same `ParseResult`. Rows whose description contains
AUTOPAY or PAYMENT are treated as card payments.
"""
from __future__ import annotations

import io
import re
from datetime import date as date_type
from decimal import Decimal
from io import BytesIO

import pandas as pd
import pdfplumber

from app.models.enums import ImportSource
from app.services.parsers.registry import ParserKind, ParserSpec
from app.services.parsers.types import (
    EARLIEST_IMPORT_YEAR,
    ParsedTransaction,
    ParseResult,
)

PAYMENT_KEYWORDS = ("AUTOPAY", "PAYMENT")


# --------- PDF (full statement) ---------

_CITI_CYCLE_RE = re.compile(
    r"Billing Period:\s*(\d{2})/(\d{2})/(\d{2})-(\d{2})/(\d{2})/(\d{2})"
)
# `MM/DD MM/DD desc $amount` (two dates), OR `MM/DD desc $amount` (one date —
# AUTOPAY rows omit the sale date). Amount can be -$X.XX or $X.XX.
_CITI_ROW_TWO_DATES = re.compile(
    r"^(\d{2})/(\d{2})\s+(\d{2})/(\d{2})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$"
)
_CITI_ROW_ONE_DATE = re.compile(
    r"^(\d{2})/(\d{2})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$"
)
_CITI_SECTION_HEADERS = (
    "Payments, Credits and Adjustments",
    "Standard Purchases",
    "Fees Charged",
    "Interest Charged",
)
_CITI_TERMINATORS = (
    "TOTAL FEES FOR THIS PERIOD",
    "TOTAL INTEREST FOR THIS PERIOD",
    "Interest charge calculation",
    "Account messages",
)


def _clean_amount(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", ""))


def _resolve_year(month: int, cycle_start: date_type, cycle_end: date_type) -> int:
    if cycle_start.year == cycle_end.year:
        return cycle_end.year
    return cycle_start.year if month == cycle_start.month else cycle_end.year


def _parse_pdf(content: bytes) -> ParseResult:
    result = ParseResult(parser="citi")
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    cycle_match = _CITI_CYCLE_RE.search(text)
    if not cycle_match:
        return result
    cs_m, cs_d, cs_y, ce_m, ce_d, ce_y = (int(g) for g in cycle_match.groups())
    cycle_start = date_type(2000 + cs_y, cs_m, cs_d)
    cycle_end = date_type(2000 + ce_y, ce_m, ce_d)

    in_summary = False
    section: str = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Don't begin parsing rows until we've passed the second "ACCOUNT
        # SUMMARY" header (page 3) which is the per-charge table, not the
        # page-1 high-level totals.
        if line == "ACCOUNT SUMMARY":
            in_summary = True
            continue
        if not in_summary:
            continue

        if any(line.startswith(t) for t in _CITI_TERMINATORS):
            break

        matched = next((h for h in _CITI_SECTION_HEADERS if line.startswith(h)), None)
        if matched:
            section = matched
            continue

        m = _CITI_ROW_TWO_DATES.match(line) or _CITI_ROW_ONE_DATE.match(line)
        if not m:
            continue

        groups = m.groups()
        if len(groups) == 6:
            # two dates: sale + post — use sale (the transaction date)
            month, day = int(groups[0]), int(groups[1])
            desc = groups[4].strip()
            amount_str = groups[5]
        else:
            month, day = int(groups[0]), int(groups[1])
            desc = groups[2].strip()
            amount_str = groups[3]

        try:
            year = _resolve_year(month, cycle_start, cycle_end)
            txn_date = date_type(year, month, day)
        except ValueError:
            result.skipped += 1
            continue

        if txn_date.year < EARLIEST_IMPORT_YEAR:
            result.skipped += 1
            continue

        amount = _clean_amount(amount_str)
        is_payment = (
            section == "Payments, Credits and Adjustments"
            or any(kw in desc.upper() for kw in PAYMENT_KEYWORDS)
        )
        parsed = ParsedTransaction(
            transaction_date=txn_date,
            description=desc,
            amount=amount,
            is_payment=is_payment,
            raw={"section": section, "desc": desc, "amount": str(amount)},
        )
        if is_payment:
            result.payments.append(parsed)
        else:
            result.transactions.append(parsed)

    return result


# --------- CSV (legacy export) ---------

def _parse_csv(content: bytes) -> ParseResult:
    df = pd.read_csv(BytesIO(content))
    result = ParseResult(parser="citi")

    for _, row in df.iterrows():
        try:
            desc = str(row["Description"]).strip() if pd.notna(row["Description"]) else ""
            txn_date = pd.to_datetime(row["Date"]).date()

            debit = float(row["Debit"]) if pd.notna(row["Debit"]) else 0.0
            credit = float(row["Credit"]) if pd.notna(row["Credit"]) else 0.0
        except (KeyError, ValueError, TypeError):
            result.skipped += 1
            continue

        if txn_date.year < EARLIEST_IMPORT_YEAR:
            result.skipped += 1
            continue

        is_payment = any(kw in desc.upper() for kw in PAYMENT_KEYWORDS)

        if is_payment:
            amount = Decimal(str(round(credit, 2)))
            result.payments.append(ParsedTransaction(
                transaction_date=txn_date, description=desc,
                amount=amount, is_payment=True,
            ))
            continue

        if debit > 0:
            amount = Decimal(str(round(debit, 2)))
        elif credit > 0:
            amount = Decimal(str(round(-credit, 2)))
        else:
            result.skipped += 1
            continue

        result.transactions.append(ParsedTransaction(
            transaction_date=txn_date, description=desc, amount=amount,
        ))

    return result


# --------- Entry point ---------

def parse(content: bytes) -> ParseResult:
    if content[:4] == b"%PDF":
        return _parse_pdf(content)
    return _parse_csv(content)


SPEC = ParserSpec(
    source=ImportSource.CITI,
    parse=parse,
    kind=ParserKind.CARD,
    order=10,
    patterns=(("citi",),),
    formats=("CSV", "PDF"),
)
