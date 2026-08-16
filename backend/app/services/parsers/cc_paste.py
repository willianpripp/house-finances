"""Manual paste parser for credit-card recent-activity views.

The Synchrony web view (the Amazon Visa and its sibling store cards) shows
recent activity in a vertical layout: each transaction renders as a labeled stanza of the
form `Type / value / Date+value / Description / [product detail] /
Status / value / Amount / value`. This parser walks that layout and
emits a `ParseResult` reusable by the CC importer pipeline.

Notable shapes in the wild:

- Stanza header row injected by the bank UI between groups (e.g. between
  Pending/Scheduled and Completed): a single line containing `Type`,
  `Date`, `Description`, `Status`, `Amount` separated by tabs. Filtered
  before stanza scanning.
- The `Date` line is `Date<MonthName DD YYYY>\tDescription` — `Date`
  and `Description` are labels, the month/day/year sits between them.
- Description spans 1 or 2 non-empty lines after the date line, ending
  at the next blank line or the `Status` label.
- `Status` ∈ {Completed, Pending, Scheduled}. Pending/Scheduled rows
  are skipped (they aren't posted to the ledger yet — re-importing the
  PDF later would otherwise duplicate them).
- `Type` ∈ {Purchase, Payment, Autopay, Refund}. Payment / Autopay are
  routed to `ParseResult.payments` (reconciliation-only); Refund is
  treated as a transaction with negative amount (sign preserved from
  the `-$X.XX` token).

The parser is filename-less by design — the calling endpoint binds the
result to a specific `payment_method_id`.
"""
from __future__ import annotations

import re
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

from app.services.parsers.types import ParsedTransaction, ParseResult


_MONTH_NAMES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    "January": 1, "February": 2, "March": 3, "April": 4,
    "June": 6, "July": 7, "August": 8, "September": 9,
    "October": 10, "November": 11, "December": 12,
}
_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b"
)
_AMOUNT_RE = re.compile(r"^-?\$[\d,]+\.\d{2}$")
_HEADER_ROW_RE = re.compile(r"^Type\b.*\bDate\b.*\bDescription\b.*\bStatus\b.*\bAmount\b", re.IGNORECASE)

_PURCHASE_TYPES = {"Purchase", "Refund"}
_PAYMENT_TYPES = {"Payment", "Autopay"}
_SKIP_STATUSES = {"Pending", "Scheduled"}


def _parse_amount(token: str) -> Decimal | None:
    t = token.strip()
    if not _AMOUNT_RE.match(t):
        return None
    negative = t.startswith("-")
    cleaned = t.lstrip("-").lstrip("$").replace(",", "")
    try:
        v = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -v if negative else v


def _parse_date(token: str) -> date_type | None:
    # Strip "Date" prefix the bank UI glues to the value (e.g. `DateJun 2 2026`).
    s = token
    if s.startswith("Date"):
        s = s[4:]
    m = _DATE_RE.search(s)
    if not m:
        return None
    month_name, day, year = m.group(1), m.group(2), m.group(3)
    if month_name not in _MONTH_NAMES:
        return None
    try:
        return date_type(int(year), _MONTH_NAMES[month_name], int(day))
    except ValueError:
        return None


def _preprocess(text: str) -> list[str]:
    """Strip CRs and bank-UI header rows. Preserve blank lines (they mark
    desc → status transitions inside a stanza)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r").rstrip("\t").rstrip()
        if _HEADER_ROW_RE.match(line.replace("\t", " ")):
            continue
        out.append(line)
    return out


def parse_cc_paste(text: str) -> tuple[ParseResult, list[str]]:
    """Parse Synchrony recent-activity paste into a `ParseResult` plus a
    list of human-readable errors. Skips Pending/Scheduled rows. Refunds
    land in `transactions` with negative amount; Payments/Autopays land
    in `payments`."""
    lines = _preprocess(text)
    n = len(lines)
    result = ParseResult(parser="manual_cc_paste")
    errors: list[str] = []

    i = 0
    while i < n:
        # Find next stanza start: a bare "Type" line.
        if lines[i].strip() != "Type":
            i += 1
            continue

        stanza_start_lineno = i + 1
        # TYPE VALUE
        if i + 1 >= n:
            errors.append(f"line {stanza_start_lineno}: dangling 'Type' label with no value")
            break
        type_val = lines[i + 1].strip()
        i += 2

        # DATE LINE — starts with "Date" prefix, contains the month/day/year.
        # In real pastes it's `DateMay 5 2026\tDescription`.
        if i >= n or not lines[i].strip().startswith("Date"):
            errors.append(f"line {stanza_start_lineno}: missing Date line after Type={type_val!r}")
            continue
        date_line = lines[i]
        dt = _parse_date(date_line)
        if dt is None:
            errors.append(f"line {i + 1}: unrecognized date in {date_line!r}")
            # advance past this stanza by scanning to next "Type"
            i += 1
            continue
        i += 1

        # DESCRIPTION — every non-empty line until the 'Status' label, with
        # blank lines acting as soft separators (merchant / product detail
        # are typically split by a blank line in the Synchrony web view).
        desc_parts: list[str] = []
        while i < n and lines[i].strip() != "Status":
            t = lines[i].strip()
            if t:
                desc_parts.append(t)
            i += 1
        description = " ".join(desc_parts).strip()
        if i >= n:
            errors.append(f"line {stanza_start_lineno}: stanza missing 'Status' label")
            continue
        # Status value on next line.
        if i + 1 >= n:
            errors.append(f"line {i + 1}: dangling 'Status' label")
            break
        status_val = lines[i + 1].strip()
        i += 2

        # AMOUNT label + value. Pending/Scheduled rows sometimes have an
        # empty Amount value followed by a stray "Completed" filter line —
        # we resolve by skipping ahead to the next 'Type' / EOF whenever
        # status says to drop the stanza.
        skip_stanza = status_val in _SKIP_STATUSES

        # Find Amount label.
        while i < n and lines[i].strip() != "Amount":
            # Defensive: if we hit a new 'Type' before Amount, abandon.
            if lines[i].strip() == "Type":
                if not skip_stanza:
                    errors.append(f"line {stanza_start_lineno}: stanza missing Amount")
                break
            i += 1
        if i >= n or lines[i].strip() != "Amount":
            continue
        i += 1  # past 'Amount' label

        if skip_stanza:
            result.skipped += 1
            continue

        # Read amount value: first non-empty line that looks like an amount.
        amount: Decimal | None = None
        amount_lineno = i + 1
        scanned = 0
        while i < n and scanned < 4:
            t = lines[i].strip()
            if t and t != "Type":
                amount = _parse_amount(t)
                if amount is not None:
                    i += 1
                    break
                # Not a recognized amount and not a new stanza — break to error.
                break
            i += 1
            scanned += 1

        if amount is None:
            errors.append(
                f"line {amount_lineno}: unrecognized amount for stanza starting at line {stanza_start_lineno}"
            )
            continue

        if not description:
            errors.append(f"line {stanza_start_lineno}: stanza has empty description")
            continue

        is_payment = type_val in _PAYMENT_TYPES
        parsed = ParsedTransaction(
            transaction_date=dt,
            description=description[:500],
            amount=amount,
            is_payment=is_payment,
            raw={"type": type_val, "status": status_val},
        )
        if type_val in _PURCHASE_TYPES:
            result.transactions.append(parsed)
        elif is_payment:
            result.payments.append(parsed)
        else:
            errors.append(
                f"line {stanza_start_lineno}: unknown stanza Type={type_val!r}"
            )

    return result, errors
