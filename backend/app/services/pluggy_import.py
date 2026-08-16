"""Adapt Pluggy transactions into v2's parser output structures so they flow
through the EXISTING import preview/commit (importer.py for cards,
checking_importer.py for checking). Mirror of plaid_import.py: Pluggy is just
another source producing a ParseResult / CheckingParseResult, never a
parallel ingest engine.

Sign convention: the sandbox connector inverts BOTH the sign and the
DEBIT/CREDIT type of real accounts, so nothing here trusts the raw sign.
Amounts are normalized from the `type` field alone (DEBIT = money out,
CREDIT = money in), and mapping any account is gated on a read-only preview
against one real account first.

PENDING rows are dropped, not ingested: unlike Plaid there is no
pending->posted id link, so a committed pending row would duplicate when it
posts under a new id. They appear once POSTED on the next review.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.config import settings
from app.models import PaymentMethod
from app.services import pluggy_client
from app.services.parsers.checking import (
    CheckingActivity,
    CheckingClass,
    CheckingParseResult,
    MatchRules,
    classify_description,
)
from app.services.parsers.types import ParsedTransaction, ParseResult

# Pluggy category fragments that mark a card payment (not spending). Checked
# lowercase-substring against the `category` field.
_CARD_PAYMENT_FRAGMENTS = ("credit card payment",)
# Description fragments seen on Nubank card payment rows.
_CARD_PAYMENT_DESCRIPTIONS = ("pagamento recebido",)
# Category fragments meaning "the user moving their own money" on checking.
_SAME_PERSON_FRAGMENTS = ("same person transfer",)


def clean_start() -> date:
    return date.fromisoformat(settings.pluggy_start_date)


def fetch_account_transactions(
    pluggy_account_id: str, since: date, until: date
) -> list[dict[str, Any]]:
    return pluggy_client.list_transactions(pluggy_account_id, since, until)


def _tx_date(tx: dict[str, Any]) -> date:
    # `date` is an ISO datetime string ("2026-08-03T00:00:00.000Z").
    return date.fromisoformat(str(tx.get("date", ""))[:10])


def _signed_from_type(tx: dict[str, Any], *, money_in_positive: bool) -> Decimal:
    """Normalize the amount using only the DEBIT/CREDIT type (see module
    docstring). money_in_positive=True gives bank convention (+ deposit),
    False gives v2 card convention (+ charge)."""
    magnitude = abs(Decimal(str(tx.get("amount", 0))))
    is_credit = (tx.get("type") or "").upper() == "CREDIT"
    if money_in_positive:
        return magnitude if is_credit else -magnitude
    return -magnitude if is_credit else magnitude


def _category(tx: dict[str, Any]) -> str:
    return (tx.get("category") or "").lower()


def _is_pending(tx: dict[str, Any]) -> bool:
    return (tx.get("status") or "").upper() == "PENDING"


def to_card_parseresult(txns: list[dict[str, Any]], pm: PaymentMethod) -> ParseResult:
    """Pluggy card tx → ParseResult (positive = charge). Explicit payment
    rows go to `payments` (surfaced, never persisted — balance comes from
    the balance refresh); other CREDIT rows stay as negative transactions
    (refunds/estornos belong in the ledger)."""
    transactions: list[ParsedTransaction] = []
    payments: list[ParsedTransaction] = []
    skipped = 0
    for tx in txns:
        if _is_pending(tx):
            skipped += 1
            continue
        amount = _signed_from_type(tx, money_in_positive=False)
        desc = (tx.get("description") or "").strip() or "(no description)"
        cat = _category(tx)
        is_payment = amount < 0 and (
            any(f in cat for f in _CARD_PAYMENT_FRAGMENTS)
            or any(f in desc.lower() for f in _CARD_PAYMENT_DESCRIPTIONS)
        )
        row = ParsedTransaction(
            transaction_date=_tx_date(tx),
            description=desc,
            amount=amount,
            is_payment=is_payment,
            raw={"pluggy_transaction_id": tx.get("id")},
        )
        (payments if is_payment else transactions).append(row)
    return ParseResult(
        transactions=transactions, payments=payments, parser="pluggy_card", skipped=skipped
    )


def to_checking_parseresult(
    txns: list[dict[str, Any]],
    pm: PaymentMethod,
    *,
    since: date,
    until: date,
    rules: MatchRules,
) -> CheckingParseResult:
    """Pluggy checking tx → CheckingParseResult (bank convention: + deposit).
    Provisional class via v2's keyword rules (they were written for BR
    statement wording, which Pluggy descriptions resemble). Two tweaks on
    the generic SPENDING fallback only, mirroring the Plaid adapter's use of
    personal_finance_category:
    - Pluggy category "same person transfer" → INTERNAL_TRANSFER
    - remaining unmatched CREDIT → EXTRA_INCOME (user rule: "recebida = extra")
    skip_snapshot=True since balances are refreshed separately."""
    activities: list[CheckingActivity] = []
    for tx in txns:
        if _is_pending(tx):
            continue
        amount = _signed_from_type(tx, money_in_positive=True)
        desc = (tx.get("description") or "").strip() or "(no description)"
        cls, hint = classify_description(desc, amount, rules=rules)
        if cls == CheckingClass.SPENDING:
            if any(f in _category(tx) for f in _SAME_PERSON_FRAGMENTS):
                cls = CheckingClass.INTERNAL_TRANSFER
            elif amount > 0:
                cls = CheckingClass.EXTRA_INCOME
        activities.append(
            CheckingActivity(
                activity_date=_tx_date(tx),
                description=desc,
                amount=amount,
                running_balance=None,
                classification=cls,
                match_hint=hint,
                raw_lines=[str(tx.get("id"))],
                pluggy_transaction_id=tx.get("id"),
            )
        )
    return CheckingParseResult(
        parser="pluggy_checking",
        account_name=pm.name,
        period_start=since,
        period_end=until,
        beginning_balance=Decimal("0"),
        ending_balance=Decimal("0"),
        activities=activities,
        skip_snapshot=True,
    )
