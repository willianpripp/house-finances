"""Manual paste parser for checking activity.

Lets the user push fresh transactions from internet banking into the same
checking pipeline before the official PDF statement closes at month-end.
Produces a `CheckingParseResult` with `skip_snapshot=True` so the paste
flow never writes a `savings_snapshots` row — those are reserved for
statement-fenced balances coming out of the PDF parsers.

When the PDF lands later in the month, importing it is idempotent:
transactions dedupe via the UNIQUE on (date, merchant, amount,
payment_method, owner); CC payments dedupe via the ±4d/±$1 ledger
heuristic in `_cc_payment_already_applied`.

Two paste formats are auto-detected:

1. **TSV** — one row per line, fields separated by tab or 2+ spaces or
   `|`:
       DATE   DESCRIPTION   AMOUNT

2. **Multi-line stanza** — what you get when copying from a bank web
   activity view that renders one transaction per block. Each transaction
   spans several lines bounded by a date-only line (e.g., "May 19"):
       May 19
       Online Transfer / Payment: Debit
       MERCHANT NAME
       -$14.64
       $585.82

The format is decided by inspecting the first non-empty line: if it's
date-only ("May 19" or "2026-05-19" with nothing else), stanza mode;
otherwise TSV mode. The choice is logged in the result audit notes.

Date accepts: YYYY-MM-DD, MM/DD/YYYY, DD/MM/YYYY, MM/DD (default year =
`default_year` arg), and `Mon DD` (the usual bank web style; year from
`default_year`). Amount accepts signed decimals with optional $/R$
prefix, commas as thousand separators, BR-style "1.234,56" (when comma
is the decimal mark), and parens for negatives. Lines that don't parse
are returned in `errors` so the user can fix them.
"""
from __future__ import annotations

import re
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

from app.services.parsers.checking import (
    CheckingActivity,
    CheckingParseResult,
    MatchRules,
    classify_description,
)


_FIELD_SPLIT = re.compile(r"\t+|\s{2,}|\s*\|\s*")
_DATE_PATTERNS = (
    # ISO
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),
    # US MM/DD/YYYY  or  US MM/DD/YY (4 or 2 digit year)
    re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$"),
    # Short MM/DD (no year)
    re.compile(r"^(\d{1,2})/(\d{1,2})$"),
)

_MONTH_NAMES = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    "January": 1, "February": 2, "March": 3, "April": 4,
    "June": 6, "July": 7, "August": 8, "September": 9,
    "October": 10, "November": 11, "December": 12,
}
_MONTH_DAY_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2})$")
_DATE_ONLY_ISO = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_AMOUNT_LIKE = re.compile(r"^[\-\(]?[\$R]?\s*-?\d[\d,\.\s]*\)?$")

# UI noise that bank web views inject between rows (month-year section
# dividers, "Last N Days" toolbars, transaction-status headers). Stripped
# before stanza detection so the parser sees only date-bounded blocks.
_MONTH_YEAR_RE = re.compile(r"^[A-Za-z]+\s+\d{4}$")
_HEADER_PHRASE_RES = (
    re.compile(r"^Last\s+\d+\s+Days$", re.IGNORECASE),
    re.compile(r"^Posted\s+Transactions?$", re.IGNORECASE),
    re.compile(r"^Pending\s+Transactions?$", re.IGNORECASE),
    re.compile(r"^Recent\s+Activity$", re.IGNORECASE),
)


def _is_section_header(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if _MONTH_YEAR_RE.match(s) and not _MONTH_DAY_RE.match(s):
        return True
    return any(r.match(s) for r in _HEADER_PHRASE_RES)

# Bank web UI labels that wrap the description in the stanza layout.
# Skipped when joining the desc lines so the categorizer sees the merchant name.
_NOISE_LABELS = (
    "Online Transfer / Payment: Debit",
    "Online Transfer / Payment: Credit",
    "Online Transfer: Debit",
    "Online Transfer: Credit",
    "Online Payment: Debit",
    "Online Payment: Credit",
    "Online Payment",
    "Online Transfer",
)


def _parse_date(token: str, default_year: int, date_format: str = "us") -> date_type | None:
    """Parse a date token. `date_format` is "us" (MM/DD) or "br" (DD/MM)
    for ambiguous slash formats."""
    t = token.strip()
    try:
        m = _DATE_PATTERNS[0].match(t)
        if m:
            return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _DATE_PATTERNS[1].match(t)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                # 2-digit year: 00-69 = 2000-2069, 70-99 = 1970-1999.
                y += 2000 if y < 70 else 1900
            if date_format == "br":
                return date_type(y, b, a)
            return date_type(y, a, b)
        m = _DATE_PATTERNS[2].match(t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if date_format == "br":
                return date_type(default_year, b, a)
            return date_type(default_year, a, b)
        m = _MONTH_DAY_RE.match(t)
        if m and m.group(1) in _MONTH_NAMES:
            return date_type(default_year, _MONTH_NAMES[m.group(1)], int(m.group(2)))
    except ValueError:
        return None
    return None


def _is_date_only_line(line: str, default_year: int) -> bool:
    """True if the line stands alone as a date (no description / amount
    co-located). Used to detect stanza boundaries in multi-line pastes."""
    t = line.strip()
    if _DATE_ONLY_ISO.match(t):
        return True
    m = _MONTH_DAY_RE.match(t)
    if m and m.group(1) in _MONTH_NAMES:
        return True
    return False


_AMOUNT_CLEAN = re.compile(r"[^0-9\.,\-()]")


def _parse_amount(token: str, decimal_mark: str = "us") -> Decimal | None:
    """Parse a signed amount. `decimal_mark` is "us" (1,234.56) or "br"
    (1.234,56). Parens are treated as negative."""
    raw = token.strip()
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    cleaned = _AMOUNT_CLEAN.sub("", raw)
    if not cleaned:
        return None
    if decimal_mark == "br":
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    return value


def _looks_like_amount(line: str) -> bool:
    """True if line resembles a signed dollar/real amount (with optional
    $/R$ prefix, commas, parens). Used to find the amount line inside a
    stanza."""
    return bool(_AMOUNT_LIKE.match(line.strip()))


def _parse_tsv(text: str,
    *,
    default_year: int,
    date_format: str,
    decimal_mark: str,
    rules: MatchRules,
) -> tuple[list[CheckingActivity], list[str]]:
    """TSV-ish line parser. Supports three column shapes:

    - 3 fields: `DATE  DESC  AMOUNT` (canonical TSV)
    - 4 fields (with balance): `DATE  DESC  AMOUNT  BALANCE` — last field
      treated as running balance and discarded.
    - 5 fields (split-column web view): `DATE  DESC  DEPOSIT  WITHDRAWAL  BALANCE`
      where Deposit and Withdrawal are mutually-exclusive amount columns
      (one is empty, the other carries the signed magnitude). Whichever
      column has the amount decides the sign: deposit → positive, withdrawal
      → negative. Empty intermediate fields are preserved by splitting on
      single tabs first; only then are header/blank rows discarded.

    Header-style rows ("Posted Transactions", "Pending Transactions", etc.)
    are silently skipped — they don't carry a parseable date.
    """
    activities: list[CheckingActivity] = []
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        # Split on single-tab FIRST so empty columns (WF deposit/withdrawal)
        # are preserved by position. If the line has no tabs, fall back to
        # the legacy multi-separator split.
        if "\t" in raw:
            tab_parts = [p.strip() for p in raw.split("\t")]
        else:
            tab_parts = [p.strip() for p in _FIELD_SPLIT.split(line) if p.strip()]

        non_empty = [p for p in tab_parts if p]
        # Skip headers (no parseable date in first column).
        if not non_empty or _parse_date(non_empty[0], default_year, date_format=date_format) is None:
            # Don't surface as an error if there's clearly no date and no
            # amount — these are visual headers like "Posted Transactions".
            looks_like_header = (
                len(non_empty) <= 2 and not any(_looks_like_amount(p) for p in non_empty)
            )
            if looks_like_header:
                continue
            errors.append(
                f"line {lineno}: expected 3+ fields (date, description, amount) — "
                f"got {len(non_empty)}: {line[:80]}"
            )
            continue

        date_tok = non_empty[0]
        dt = _parse_date(date_tok, default_year, date_format=date_format)
        if dt is None:
            errors.append(f"line {lineno}: unrecognized date '{date_tok}'")
            continue

        # WF web shape: 5 tab-separated columns including blanks.
        # tab_parts = [date, description, deposit, withdrawal, balance].
        amt: Decimal | None = None
        desc: str | None = None
        running_balance: Decimal | None = None
        if len(tab_parts) >= 5 and "\t" in raw:
            desc = tab_parts[1]
            deposit_tok = tab_parts[2]
            withdrawal_tok = tab_parts[3]
            balance_tok = tab_parts[4]
            if deposit_tok and not withdrawal_tok:
                amt = _parse_amount(deposit_tok, decimal_mark=decimal_mark)  # positive
            elif withdrawal_tok and not deposit_tok:
                neg = _parse_amount(withdrawal_tok, decimal_mark=decimal_mark)
                amt = -neg if neg is not None else None
            else:
                errors.append(
                    f"line {lineno}: ambiguous deposit/withdrawal columns "
                    f"(deposit={deposit_tok!r}, withdrawal={withdrawal_tok!r})"
                )
                continue
            if balance_tok and _looks_like_amount(balance_tok):
                running_balance = _parse_amount(balance_tok, decimal_mark=decimal_mark)
        else:
            # Canonical: last non-empty = amount, second-to-last could be balance.
            # When 4+ fields, treat penultimate as amount and last as balance.
            if len(non_empty) >= 4 and _looks_like_amount(non_empty[-1]) and _looks_like_amount(non_empty[-2]):
                amount_tok = non_empty[-2]
                desc = " ".join(non_empty[1:-2]).strip()
                running_balance = _parse_amount(non_empty[-1], decimal_mark=decimal_mark)
            else:
                amount_tok = non_empty[-1]
                desc = " ".join(non_empty[1:-1]).strip()
            amt = _parse_amount(amount_tok, decimal_mark=decimal_mark)
            if amt is None:
                errors.append(f"line {lineno}: unrecognized amount '{amount_tok}'")
                continue

        if amt is None:
            errors.append(f"line {lineno}: unrecognized amount")
            continue
        if not desc:
            errors.append(f"line {lineno}: empty description")
            continue

        cls, hint = classify_description(desc, amt, rules=rules)
        activities.append(
            CheckingActivity(
                activity_date=dt,
                description=desc,
                amount=amt,
                running_balance=running_balance,
                classification=cls,
                match_hint=hint,
                raw_lines=[raw],
            )
        )
    return activities, errors


def _parse_stanza(
    text: str,
    *,
    default_year: int,
    decimal_mark: str,
    rules: MatchRules,
) -> tuple[list[CheckingActivity], list[str]]:
    """Multi-line stanza format. Each transaction starts at a date-only
    line and ends at the next date-only line (or end of input).

    Within a stanza:
    - Line 0 = date.
    - Last "amount-like" line = transaction amount.
    - The line immediately after the amount (if any) is treated as
      running balance and skipped.
    - Lines between date and amount form the description. Bank-UI labels
      from `_NOISE_LABELS` are stripped before joining.
    """
    raw_lines = [l for l in text.splitlines()]
    # Build a list of (orig_lineno, stripped_text), keeping only non-empty
    # and dropping bank-UI section dividers ("May 2026", "Last 30 Days").
    indexed = [
        (i + 1, l.strip())
        for i, l in enumerate(raw_lines)
        if l.strip() and not _is_section_header(l)
    ]

    if not indexed:
        return [], []

    activities: list[CheckingActivity] = []
    errors: list[str] = []

    # Find date-only line indices (within `indexed`).
    date_positions = [
        idx for idx, (_, l) in enumerate(indexed) if _is_date_only_line(l, default_year)
    ]
    if not date_positions:
        return [], ["stanza format: no date-only line found"]

    # Anything before the first date-only line is a header (skip silently
    # — banks sometimes paste a column header at the top).

    for k, start in enumerate(date_positions):
        end = date_positions[k + 1] if k + 1 < len(date_positions) else len(indexed)
        stanza = indexed[start:end]
        first_lineno = stanza[0][0]
        if len(stanza) < 2:
            errors.append(f"line {first_lineno}: stanza has only the date line — no description or amount")
            continue

        dt = _parse_date(stanza[0][1], default_year)
        if dt is None:
            errors.append(f"line {first_lineno}: unrecognized date '{stanza[0][1]}'")
            continue

        # Find the LAST amount-like line in the stanza. The line right
        # after it (if exists) is the running balance — skip it.
        amount_idx = None
        for i in range(len(stanza) - 1, 0, -1):
            if _looks_like_amount(stanza[i][1]):
                amount_idx = i
                break
        if amount_idx is None:
            errors.append(f"line {first_lineno}: no amount line found in stanza")
            continue

        # Heuristic: if the LAST line is amount-like AND there's another
        # amount-like line before it, treat the last as balance and the
        # earlier one as the transaction amount.
        if amount_idx == len(stanza) - 1 and amount_idx >= 2:
            for j in range(amount_idx - 1, 0, -1):
                if _looks_like_amount(stanza[j][1]):
                    # The earlier line is the amount; the last one is balance.
                    amount_idx = j
                    break

        amt = _parse_amount(stanza[amount_idx][1], decimal_mark=decimal_mark)
        if amt is None:
            errors.append(f"line {stanza[amount_idx][0]}: bad amount '{stanza[amount_idx][1]}'")
            continue

        # Running balance lives one line after the amount in the stanza layout.
        running_balance: Decimal | None = None
        if amount_idx + 1 < len(stanza):
            tail = stanza[amount_idx + 1][1]
            if _looks_like_amount(tail):
                running_balance = _parse_amount(tail, decimal_mark=decimal_mark)

        desc_parts: list[str] = []
        for i in range(1, amount_idx):
            t = stanza[i][1]
            if t in _NOISE_LABELS:
                continue
            desc_parts.append(t)
        desc = " ".join(desc_parts).strip()
        if not desc:
            errors.append(f"line {first_lineno}: empty description after stripping bank labels")
            continue

        cls, hint = classify_description(desc, amt, rules=rules)
        activities.append(
            CheckingActivity(
                activity_date=dt,
                description=desc,
                amount=amt,
                running_balance=running_balance,
                classification=cls,
                match_hint=hint,
                raw_lines=[l for _, l in stanza],
            )
        )

    return activities, errors


def parse_paste(
    text: str,
    *,
    account_name: str,
    default_year: int,
    rules: MatchRules,
    date_format: str = "us",
    decimal_mark: str = "us",
) -> tuple[CheckingParseResult, list[str]]:
    """Parse pasted text into a `CheckingParseResult` plus a list of
    human-readable errors. Auto-detects TSV vs multi-line stanza by
    looking at the first non-empty line: if it stands alone as a date
    (e.g., "May 19" or "2026-05-19"), the paste is in stanza mode."""
    # Skip leading bank-UI noise (section dividers, "Last 30 Days" toolbars)
    # when deciding between stanza and TSV. The first *real* non-header line
    # being date-only signals stanza mode.
    first_real = ""
    for l in text.splitlines():
        s = l.strip()
        if not s or _is_section_header(s):
            continue
        first_real = s
        break
    use_stanza = _is_date_only_line(first_real, default_year)

    if use_stanza:
        activities, errors = _parse_stanza(
            text, default_year=default_year, decimal_mark=decimal_mark, rules=rules
        )
    else:
        activities, errors = _parse_tsv(
            text,
            default_year=default_year,
            date_format=date_format,
            decimal_mark=decimal_mark,
            rules=rules,
        )

    if not activities:
        period_start = period_end = date_type(default_year, 1, 1)
    else:
        period_start = min(a.activity_date for a in activities)
        period_end = max(a.activity_date for a in activities)

    # Auto-snapshot: if any pasted row carried a running balance (stanza
    # balance line / tabular last column), use the latest one as
    # the ending balance for SavingsSnapshot. Picks the activity with the
    # max date — when several share that date, the first encountered
    # (pasted topmost in date-DESC banking views = the most up-to-date one).
    ending_balance = Decimal("0")
    skip_snapshot = True
    latest_with_balance: CheckingActivity | None = None
    for a in activities:
        if a.running_balance is None:
            continue
        if latest_with_balance is None or a.activity_date > latest_with_balance.activity_date:
            latest_with_balance = a
    if latest_with_balance is not None:
        ending_balance = latest_with_balance.running_balance
        period_end = latest_with_balance.activity_date
        skip_snapshot = False

    return (
        CheckingParseResult(
            parser="manual_paste_stanza" if use_stanza else "manual_paste",
            account_name=account_name,
            period_start=period_start,
            period_end=period_end,
            beginning_balance=Decimal("0"),
            ending_balance=ending_balance,
            activities=activities,
            interest_earned=Decimal("0"),
            skipped=0,
            skip_snapshot=skip_snapshot,
        ),
        errors,
    )
