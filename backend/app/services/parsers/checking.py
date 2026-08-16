"""Shared base for bank-checking statement parsers.

Bank-checking statements are richer than credit-card statements:
- They mix several kinds of activities — credit-card payments, internal
  transfers, salary deposits, real spending, interest, taxes — and most lines
  are NOT new transactions (they're already represented elsewhere).
- They carry a period-end balance that should drop into `savings_snapshots`.

This module defines the data shape every checking parser produces, plus
provisional classification by description keywords. The DB-aware refinement
(matching to existing FIXED transactions, dedup against card-statement
payments, withholding adjustment for salaries) happens in the importer service
because it needs DB context the parser doesn't have.

Order of provisional classification (first match wins):
1. CC_PAYMENT     — known credit-card payment keywords
2. SALARY         — known salary-source keywords (per-user)
3. TAX_PAYMENT    — gov/IRS tax payment keywords
4. INTEREST       — "Interest Deposit"
5. INTERNAL_TRANSFER — own-account moves (ATM cash deposits, sibling-account transfers)
6. SPENDING       — fallback (real debit-card purchases, Zelle out, etc)

The keywords are not hardcoded here: they live in the `statement_match_rules`
table, loaded once per import by `services/match_rules.load_match_rules()` and
passed into `classify_description()` as a `MatchRules`. Parsers stay pure:
they never touch the database, they just take the rule set as an argument.
Within a class, rules apply in `sort_order` — earlier rules take priority.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from enum import Enum


# Patterns that bank statements wrap around the actual merchant name. We
# strip these before passing the description to the categorizer so the
# merchant-name fallback isn't a 90-character debit-authorization stub.
_NOISE_PATTERNS = (
    re.compile(r"^Purchase authorized on \d{1,2}/\d{1,2}\s+", re.IGNORECASE),
    re.compile(r"\s*\bP\d{8,}\s*", re.IGNORECASE),
    re.compile(r"\s*\bS\d{8,}\s*", re.IGNORECASE),
    re.compile(r"\s*Card\s+\d{4}\s*$", re.IGNORECASE),
    re.compile(r"\s*ID\s+\d+\s*$", re.IGNORECASE),
    re.compile(r"\s+ACH PMT.*$", re.IGNORECASE),
    re.compile(r"\s*\*+\d+\s*", re.IGNORECASE),
)


def normalize_description(desc: str) -> str:
    """Strip statement-formatting noise so the categorizer sees the merchant
    name. Applied to checking-statement descriptions only."""
    cleaned = desc
    for pat in _NOISE_PATTERNS:
        cleaned = pat.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


class CheckingClass(str, Enum):
    CC_PAYMENT = "CC_PAYMENT"
    SALARY = "SALARY"
    RENT_DEPOSIT = "RENT_DEPOSIT"  # BR rents (partner's share into the primary's BR checking)
    EXTRA_INCOME = "EXTRA_INCOME"  # ad-hoc Pix recebido (family transfers) — IncomeEntry.EXTRA
    TAX_PAYMENT = "TAX_PAYMENT"
    INTEREST = "INTEREST"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    SPENDING = "SPENDING"
    FIXED_MATCH = "FIXED_MATCH"   # set by importer when SPENDING matches an existing FIXED transaction
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MatchRules:
    """The description-matching rule set, loaded from `statement_match_rules`.

    Pair tuples are (KEYWORD, match_hint): the hint is a card name for
    CC_PAYMENT and a household match_key for SALARY / RENT_DEPOSIT. Plain
    tuples are keyword-only classes. `noise_prefixes` are statement
    boilerplate line prefixes that line-oriented parsers drop before
    classification. `holder_names` is kept separate from `noise_prefixes`
    because the two are consumed differently: a holder name terminates a
    continuation block on the card statements, while dropping it outright
    (as `noise_prefixes` does) would swallow the payer line that identifies
    a rent deposit on the BR statements.
    """

    cc_payment: tuple[tuple[str, str], ...] = ()
    salary: tuple[tuple[str, str], ...] = ()
    rent_deposit: tuple[tuple[str, str], ...] = ()
    tax_payment: tuple[str, ...] = ()
    interest: tuple[str, ...] = ()
    internal_transfer: tuple[str, ...] = ()
    extra_income: tuple[str, ...] = ()
    noise_prefixes: tuple[str, ...] = ()
    holder_names: tuple[str, ...] = ()


@dataclass
class CheckingActivity:
    """One row of activity from a checking statement."""

    activity_date: date_type
    description: str
    amount: Decimal                 # signed: + = deposit, - = withdrawal
    running_balance: Decimal | None  # statement-stated EOD balance, if printed
    classification: CheckingClass
    match_hint: str = ""             # e.g. matched card name, owner, etc
    raw_lines: list[str] = field(default_factory=list)
    plaid_transaction_id: str | None = None  # set by the Plaid adapter; dedup key
    pluggy_transaction_id: str | None = None  # set by the Pluggy adapter; dedup key
    pending: bool = False                     # Plaid pending (provisional) row
    pending_transaction_id: str | None = None # set on a posted row; the prior pending id it replaces


@dataclass
class CheckingParseResult:
    parser: str
    account_name: str               # canonical payment_method name
    period_start: date_type
    period_end: date_type
    beginning_balance: Decimal
    ending_balance: Decimal
    activities: list[CheckingActivity] = field(default_factory=list)
    interest_earned: Decimal = Decimal("0")
    skipped: int = 0
    # Per-account opt-out for the SavingsSnapshot side effect. Set True for
    # accounts where the printed balance is not a meaningful "savings" datum:
    # an account swept monthly (no carried balance), or one kept near zero
    # because the real worth sits in invest products tracked elsewhere.
    skip_snapshot: bool = False


def classify_description(
    desc: str,
    amount: Decimal | None = None,
    *,
    rules: MatchRules,
) -> tuple[CheckingClass, str]:
    """Provisional classification by description keyword. Returns
    (class, match_hint). `match_hint` is the matched key (card name,
    owner name) when the class needs DB resolution downstream.

    `amount` is the signed activity amount (+ deposit, - withdrawal).
    When provided, asymmetric classes are gated by sign:
      - RENT_DEPOSIT: only credits (amount > 0). The payer's name on outbound
        debits (the landlord's rent charge, a financing plan, the gym) must
        not look like a deposit.
      - SALARY: only credits. Same logic — owner name on debits isn't pay.
    When `amount` is None (legacy callers), no sign gate is applied.
    """
    upper = desc.upper()

    for kw, card_name in rules.cc_payment:
        if kw in upper:
            return CheckingClass.CC_PAYMENT, card_name

    is_credit = amount is None or amount > 0

    for kw, owner in rules.salary:
        if kw in upper and is_credit:
            return CheckingClass.SALARY, owner

    for kw, owner in rules.rent_deposit:
        if kw in upper and is_credit:
            return CheckingClass.RENT_DEPOSIT, owner

    for kw in rules.tax_payment:
        if kw in upper:
            return CheckingClass.TAX_PAYMENT, ""

    for kw in rules.interest:
        if kw in upper:
            return CheckingClass.INTEREST, ""

    for kw in rules.internal_transfer:
        if kw in upper:
            return CheckingClass.INTERNAL_TRANSFER, ""

    # Ad-hoc Pix recebido — only on credits. Anything left that says
    # "Transferência Recebida" with a positive amount is treated as extra
    # income (the user's rule: recebida = extra, enviada = gasto).
    if is_credit:
        for kw in rules.extra_income:
            if kw in upper:
                return CheckingClass.EXTRA_INCOME, ""

    return CheckingClass.SPENDING, ""
