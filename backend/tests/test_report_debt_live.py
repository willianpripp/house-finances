"""The monthly report's debt figure must use the same live derivation /debts does.

`credit_card_balances` records payment events and manual snapshots; new charges
land in `transactions` and never push the row up. /debts and /warnings have
derived past that since 2026-06-04, but `reports._compute_debt_at` kept reading
the raw row, so the two disagreed by exactly the un-derived spending — caught on
2026-08-17 as $42.00 between `total_debt_usd` (4,000.00) and
`/api/debts/cards/current` + car loan (4,000.00).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    Category,
    CreditCardBalance,
    Currency,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    Transaction,
)
from app.services.reports import _compute_debt_at


RATE = Decimal("1")


def _usd_card(db) -> PaymentMethod:
    pm = PaymentMethod(
        name="Debt Derivation Card",
        type=PaymentMethodType.CREDIT_CARD,
        currency=Currency.USD,
        active=True,
    )
    db.add(pm)
    db.flush()
    return pm


def _charge(db, pm: PaymentMethod, when: date, amount: str) -> None:
    merchant = db.scalar(select(Merchant).limit(1))
    category = db.scalar(select(Category).limit(1))
    db.add(
        Transaction(
            transaction_date=when,
            merchant_id=merchant.id,
            category_id=category.id,
            payment_method_id=pm.id,
            amount=Decimal(amount),
            currency=Currency.USD,
        )
    )
    db.flush()


def test_debt_includes_charges_posted_after_the_balance_row(db):
    """The regression: a charge posted after the last recorded balance was
    invisible to the report while /debts already counted it."""
    today = date.today()
    pm = _usd_card(db)
    db.add(
        CreditCardBalance(
            payment_method_id=pm.id,
            balance=Decimal("100.00"),
            recorded_at=datetime.combine(today - timedelta(days=10),
                                         datetime.min.time(), tzinfo=timezone.utc),
        )
    )
    db.flush()

    as_of = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc)
    before = _compute_debt_at(db, as_of, RATE)

    _charge(db, pm, today - timedelta(days=2), "42.00")
    after = _compute_debt_at(db, as_of, RATE)

    assert after - before == Decimal("42.00")


def test_future_dated_charges_do_not_inflate_the_current_month(db):
    """For the current month `as_of` is a future end-of-month. Future-dated
    FIXED projections must stay excluded, exactly as /debts clamps them."""
    today = date.today()
    pm = _usd_card(db)
    db.add(
        CreditCardBalance(
            payment_method_id=pm.id,
            balance=Decimal("100.00"),
            recorded_at=datetime.combine(today - timedelta(days=10),
                                         datetime.min.time(), tzinfo=timezone.utc),
        )
    )
    db.flush()

    end_of_month = datetime.combine(today + timedelta(days=20),
                                    datetime.max.time(), tzinfo=timezone.utc)
    before = _compute_debt_at(db, end_of_month, RATE)

    _charge(db, pm, today + timedelta(days=5), "500.00")
    after = _compute_debt_at(db, end_of_month, RATE)

    assert after == before
