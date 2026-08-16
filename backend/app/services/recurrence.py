"""Historical-recurrence lookup used by the importers.

Replaces most of what `rollover.py` did manually. When a fresh debit
lands and there's no FIXED row in the current month to match against,
the importer searches the last few months for a row with the same
`(merchant_id, payment_method_id)` and FIXED category. If found, it
copies the recurrence metadata forward: `recurrence_kind`,
`contract_end_date`, `installment_current+1`, `installment_value`.

Rollover is now scoped narrowly to (a) INSTALLMENT series so the user
can still preview/edit the upcoming installments proactively, and
(b) Taxes placeholders so the salary import has same-month rows to
rebalance. Everything else (CONTRACT, INDEFINITE, EXTRA_PRINCIPAL) is
auto-propagated here.

Contract end-dates and installment series caps are honored: a CONTRACT
past its `contract_end_date`, or an INSTALLMENT past its
`installment_total`, returns no propagation (caller falls back to plain
SPENDING).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, CategoryType, Transaction
from app.models.enums import RecurrenceKind


DEFAULT_LOOKBACK_MONTHS = 3


def find_prior_recurring(
    session: Session,
    *,
    merchant_id: int,
    payment_method_id: int,
    before_date: date,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
) -> Transaction | None:
    """Most recent FIXED transaction for the same merchant+payment_method
    in the lookback window, strictly before `before_date`."""
    window_start = before_date - timedelta(days=lookback_months * 31)
    return session.scalar(
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.merchant_id == merchant_id,
            Transaction.payment_method_id == payment_method_id,
            Transaction.transaction_date >= window_start,
            Transaction.transaction_date < before_date,
            Category.type == CategoryType.FIXED,
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )


@dataclass
class Propagation:
    category_id: int
    recurrence_kind: RecurrenceKind | None
    contract_end_date: date | None
    installment_current: int
    installment_total: int
    installment_value: Decimal | None


AMOUNT_MATCH_TOLERANCE = Decimal("1.00")


def amount_matches_prior(prior: Transaction, activity_magnitude: Decimal) -> bool:
    """Sanity check before propagating: the activity's magnitude must match
    the prior's recurring amount. Matches against `prior.amount` for flat
    monthly bills (Hulu $4.99 -> $4.99) OR `prior.installment_value` for
    installment series (e.g. a phone financed over 24x: total $1,200.00 / per-installment $36
    matches the $36 monthly debit). Tolerance is ±$1.

    Without this check, history propagation mis-tags one-off car-loan extras
    ($300 / $1500 / $2500) as if they were the parcela ($425.00) just
    because they share merchant+payment_method."""
    target = abs(activity_magnitude)
    if abs(Decimal(prior.amount) - target) <= AMOUNT_MATCH_TOLERANCE:
        return True
    if prior.installment_value is not None:
        if abs(Decimal(prior.installment_value) - target) <= AMOUNT_MATCH_TOLERANCE:
            return True
    return False


def propagation_for_new_row(prior: Transaction, new_date: date) -> Propagation | None:
    """Given a prior FIXED row, return metadata for the next-month row.
    Returns None when the series cannot be extended:
    - EXTRA_PRINCIPAL (one-offs by definition)
    - CONTRACT past its `contract_end_date`
    - any row with installment counter at `installment_total` already

    Callers should ALSO check `amount_matches_prior()` against the
    activity magnitude — propagation answers the "what metadata to copy"
    question, not "should we propagate at all". The latter depends on
    whether the activity matches the prior's recurring amount.

    CONTRACT and INSTALLMENT both honor installment counters when present
    (Progressive is CONTRACT 3/6 — a 6-payment contract; FITBOD is
    CONTRACT 7/12). When `installment_total == 1` the row is a flat
    monthly recurring (rent, Netflix) — propagate 1:1."""
    rk = prior.recurrence_kind

    if rk == RecurrenceKind.EXTRA_PRINCIPAL:
        return None

    # CONTRACT past end date → series over.
    if (
        rk == RecurrenceKind.CONTRACT
        and prior.contract_end_date is not None
        and new_date > prior.contract_end_date
    ):
        return None

    # Installment counter: applies to anything with installment_total > 1
    # (INSTALLMENT pure or CONTRACT modeled as N installments).
    if prior.installment_total > 1:
        next_idx = prior.installment_current + 1
        if next_idx > prior.installment_total:
            return None
        return Propagation(
            category_id=prior.category_id,
            recurrence_kind=rk,
            contract_end_date=prior.contract_end_date,
            installment_current=next_idx,
            installment_total=prior.installment_total,
            installment_value=(
                Decimal(prior.installment_value) if prior.installment_value is not None else None
            ),
        )

    # Flat recurring (INDEFINITE / CONTRACT without installments /
    # untagged FIXED with installment_total == 1).
    return Propagation(
        category_id=prior.category_id,
        recurrence_kind=rk,
        contract_end_date=prior.contract_end_date,
        installment_current=1,
        installment_total=1,
        installment_value=None,
    )
