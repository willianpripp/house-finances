"""EXTRA_USD/EXTRA_BRL must not inflate when a provider re-pulls its window.

Plaid and Pluggy both re-fetch from the clean-start anchor on every commit
(`routers/pluggy.py`: `since, until = clean_start(), _date.today()`), so any
deposit inside the window is presented again on each sync. The guard in
`_record_extra_income` originally tested `plaid_transaction_id` alone; a Pluggy
activity carries `pluggy_transaction_id` instead and fell through to the
manual-paste ACCUMULATE branch, so every re-sync added the window's deposits
on top of the total it had already recorded.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models import Currency, IncomeEntry, IncomeSource, PaymentMethod, PaymentMethodType
from app.services.checking_importer import _record_extra_income
from app.services.parsers.checking import CheckingActivity, CheckingClass


YEAR, MONTH = 2031, 3  # far from any seeded fixture data


def _brl_checking(db, name: str) -> PaymentMethod:
    pm = PaymentMethod(
        name=name,
        type=PaymentMethodType.CHECKING,
        currency=Currency.BRL,
        active=True,
    )
    db.add(pm)
    db.flush()
    return pm


def _deposit(amount: str, **ids) -> CheckingActivity:
    return CheckingActivity(
        activity_date=date(YEAR, MONTH, 2),
        description="Transferência Recebida|Someone",
        amount=Decimal(amount),
        running_balance=None,
        classification=CheckingClass.EXTRA_INCOME,
        **ids,
    )


def _existing(db) -> IncomeEntry:
    return db.scalar(
        select(IncomeEntry).filter_by(
            year=YEAR, month=MONTH, source=IncomeSource.EXTRA_BRL
        )
    )


def test_pluggy_resync_leaves_the_month_total_untouched(db):
    pm = _brl_checking(db, "Pluggy Idempotency Checking")
    _record_extra_income(db, _deposit("900.00", pluggy_transaction_id="pg-1"), pm)
    db.flush()
    assert Decimal(_existing(db).amount) == Decimal("900.00")

    # The same deposit comes back on the next sync, as it always will.
    note = _record_extra_income(db, _deposit("900.00", pluggy_transaction_id="pg-1"), pm)
    db.flush()

    assert Decimal(_existing(db).amount) == Decimal("900.00")
    assert "Pluggy, idempotent" in note


def test_plaid_resync_is_still_guarded(db):
    pm = _brl_checking(db, "Plaid Idempotency Checking")
    _record_extra_income(db, _deposit("250.00", plaid_transaction_id="pl-1"), pm)
    db.flush()

    note = _record_extra_income(db, _deposit("250.00", plaid_transaction_id="pl-1"), pm)
    db.flush()

    assert Decimal(_existing(db).amount) == Decimal("250.00")
    assert "Plaid, idempotent" in note


def test_manual_paste_still_accumulates(db):
    """Two Pix hand-pasted in one month legitimately sum — the guard must not
    swallow that (user feedback, 2026-05-22 session)."""
    pm = _brl_checking(db, "Manual Accumulate Checking")
    _record_extra_income(db, _deposit("100.00"), pm)
    db.flush()
    _record_extra_income(db, _deposit("65.00"), pm)
    db.flush()

    assert Decimal(_existing(db).amount) == Decimal("165.00")
