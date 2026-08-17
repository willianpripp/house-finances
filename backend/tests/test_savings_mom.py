"""The savings MoM badge must not report its baseline's noise as movement.

A percentage is only meaningful when the baseline it divides by is. On
2026-08-17 /savings rendered "▲ 79900.0%" for an account whose previous-month
snapshot was R$1.00 against a current balance of R$800.00 — arithmetically
correct, and pure noise. That baseline was a bad row written by an importer
(its parser-level regression test is private, alongside the parser), but the
badge should degrade gracefully whatever put the number there.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import Currency, SavingsSnapshot
from app.services.savings import current_balances


def _prev_month_day(today: date) -> date:
    """A date inside the calendar month before `today`."""
    return today.replace(day=1) - timedelta(days=1)


def _snapshot(account: str, balance: str, when: datetime) -> SavingsSnapshot:
    return SavingsSnapshot(
        account_name=account,
        currency=Currency.BRL,
        balance=Decimal(balance),
        recorded_at=when,
    )


def _row_for(db, account: str):
    return next(r for r in current_balances(db).rows if r.account_name == account)


def _seed(db, account: str, prev: str, current: str) -> None:
    today = date.today()
    midnight = datetime.min.time()
    db.add(_snapshot(account, prev, datetime.combine(_prev_month_day(today), midnight)))
    db.add(_snapshot(account, current, datetime.combine(today, midnight)))
    db.flush()


def test_mom_pct_is_suppressed_when_the_baseline_is_negligible(db):
    """1.00 -> 800.00 is +79900%, and it is noise: a baseline under 1% of the
    current balance carries no information about the account's movement."""
    account = "MoM Guard Negligible"
    _seed(db, account, "1.00", "800.00")

    row = _row_for(db, account)
    assert row.balance == Decimal("800.00")
    assert row.prev_balance == Decimal("1.00")
    assert row.mom_pct is None


def test_mom_pct_survives_a_meaningful_baseline(db):
    """The guard must not swallow real movement: 400.00 -> 800.00 is +0.2%,
    which is what the badge should have shown all along."""
    account = "MoM Guard Meaningful"
    _seed(db, account, "400.00", "800.00")

    assert _row_for(db, account).mom_pct == Decimal("0.2")


def test_a_large_but_real_move_still_reports(db):
    """The guard keys on the baseline's size, not on the percentage, so a
    genuine order-of-magnitude jump is still reported rather than hidden."""
    account = "MoM Guard Large Real"
    _seed(db, account, "100.00", "800.00")

    assert _row_for(db, account).mom_pct == Decimal("813.4")
