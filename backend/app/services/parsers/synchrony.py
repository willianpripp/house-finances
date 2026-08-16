"""Synchrony Bank credit-card statement (PDF) — shared parser.

Synchrony issues the Amazon Visa along with a family of store cards, and uses
one statement template for all of them: a
"Transaction Detail" section split into Payments / Other Credits /
Purchases and Other Debits, with rows anchored on
`MM/DD <12-char ref> <merchant block> <signed amount>` and 1-2
continuation lines (short ref + optional truncated item title).

Years are not on the row — we resolve them from the billing-cycle header
("30 Day Billing Cycle from MM/DD/YYYY to MM/DD/YYYY") so December/January
rows in a wrap-around cycle land in the right year.

Card-specific wrappers (`amazon.py` and one module per sibling card) just call
`parse_synchrony` with the right `parser_name` so import_logs and
ParseResult.parser show the issuing card, not the bank.
"""
from __future__ import annotations

import io
import re
from datetime import date as date_type
from decimal import Decimal

import pdfplumber

from app.services.parsers.types import (
    EARLIEST_IMPORT_YEAR,
    ParsedTransaction,
    ParseResult,
)

_CYCLE_RE = re.compile(
    r"Billing Cycle from (\d{2})/(\d{2})/(\d{4}) to (\d{2})/(\d{2})/(\d{4})"
)
_ROW_RE = re.compile(
    r"^(\d{2})/(\d{2})\s+([A-Z0-9]{8,})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$"
)
_SHORT_REF_RE = re.compile(r"^[A-Za-z0-9]{8,16}$")
_SECTION_HEADERS = ("Payments", "Other Credits", "Purchases and Other Debits")
_TERMINATORS = ("Total Fees Charged", "Total Interest Charged", "Interest Charge Calculation")
_SKIP_LINE_FRAGMENTS = (
    "Date Reference # Description Amount",
    "(Continued on next page)",
    "Transaction Detail (Continued)",
    "Account Number ending in",
    "Visit us at https://",
    "PAGE ",
)


def _clean_amount(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", ""))


def _resolve_year(month: int, cycle_start: date_type, cycle_end: date_type) -> int:
    if cycle_start.year == cycle_end.year:
        return cycle_end.year
    return cycle_start.year if month == cycle_start.month else cycle_end.year


def _iter_relevant_lines(text: str) -> list[str]:
    out: list[str] = []
    started = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not started:
            if line.startswith("Date Reference # Description Amount"):
                started = True
            continue
        if any(line.startswith(t) for t in _TERMINATORS):
            break
        if any(frag in line for frag in _SKIP_LINE_FRAGMENTS):
            continue
        out.append(line)
    return out


def parse_synchrony(content: bytes, *, parser_name: str) -> ParseResult:
    result = ParseResult(parser=parser_name)

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    cycle_match = _CYCLE_RE.search(full_text)
    if not cycle_match:
        return result
    cs_m, cs_d, cs_y, ce_m, ce_d, ce_y = (int(g) for g in cycle_match.groups())
    cycle_start = date_type(cs_y, cs_m, cs_d)
    cycle_end = date_type(ce_y, ce_m, ce_d)

    lines = _iter_relevant_lines(full_text)

    section: str = ""
    i = 0
    while i < len(lines):
        line = lines[i]

        matched_header = next((h for h in _SECTION_HEADERS if line.startswith(h)), None)
        if matched_header:
            section = matched_header
            i += 1
            continue

        m = _ROW_RE.match(line)
        if not m:
            i += 1
            continue

        month, day, _ref, merchant_block, amount_str = m.groups()
        month_i, day_i = int(month), int(day)
        try:
            year = _resolve_year(month_i, cycle_start, cycle_end)
            txn_date = date_type(year, month_i, day_i)
        except ValueError:
            result.skipped += 1
            i += 1
            continue

        if txn_date.year < EARLIEST_IMPORT_YEAR:
            result.skipped += 1
            i += 1
            continue

        amount = _clean_amount(amount_str)

        j = i + 1
        item_title: str | None = None
        while j < len(lines):
            nxt = lines[j]
            if _ROW_RE.match(nxt):
                break
            if any(nxt.startswith(h) for h in _SECTION_HEADERS):
                break
            if _SHORT_REF_RE.match(nxt):
                j += 1
                continue
            item_title = nxt
            j += 1
            break

        merchant_block = merchant_block.strip()
        description = (
            f"{merchant_block} - {item_title}" if item_title else merchant_block
        )

        is_payment = section == "Payments" or "AUTOMATIC PAYMENT" in merchant_block.upper()

        parsed = ParsedTransaction(
            transaction_date=txn_date,
            description=description,
            amount=amount,
            is_payment=is_payment,
            raw={"section": section, "merchant_block": merchant_block, "item": item_title},
        )

        if is_payment:
            result.payments.append(parsed)
        else:
            result.transactions.append(parsed)

        i = j if j > i + 1 else i + 1

    return result
