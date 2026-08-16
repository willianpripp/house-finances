"""Income entries: list / create / update / delete.

Each entry is one (year, month, source) tuple — uniqueness enforced by the DB.
On create we attach the latest exchange_rate effective at or before the
entry's month-end, so the historical rate is captured even if the user later
updates the current rate.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, IncomeEntry, IncomeSource


@dataclass(frozen=True)
class IncomeRow:
    id: int
    year: int
    month: int
    source: str
    amount: Decimal
    currency: str
    exchange_rate_id: int | None
    exchange_rate_effective: Decimal | None
    exchange_rate_date: date_type | None


@dataclass(frozen=True)
class IncomeListResult:
    rows: list[IncomeRow]
    sum_by_currency: dict[str, Decimal]


def _row(entry: IncomeEntry) -> IncomeRow:
    rate = entry.exchange_rate
    return IncomeRow(
        id=entry.id,
        year=entry.year,
        month=entry.month,
        source=entry.source.value,
        amount=entry.amount,
        currency=entry.currency.value,
        exchange_rate_id=entry.exchange_rate_id,
        exchange_rate_effective=Decimal(rate.effective) if rate else None,
        exchange_rate_date=rate.rate_date if rate else None,
    )


def list_income(
    session: Session,
    *,
    year: int | None = None,
    month: int | None = None,
    source: IncomeSource | None = None,
) -> IncomeListResult:
    stmt = select(IncomeEntry).order_by(
        IncomeEntry.year.desc(),
        IncomeEntry.month.desc(),
        IncomeEntry.source,
    )
    if year is not None:
        stmt = stmt.where(IncomeEntry.year == year)
    if month is not None:
        stmt = stmt.where(IncomeEntry.month == month)
    if source is not None:
        stmt = stmt.where(IncomeEntry.source == source)
    entries = session.scalars(stmt).all()

    rows = [_row(e) for e in entries]
    sums: dict[str, Decimal] = {}
    for r in rows:
        sums[r.currency] = sums.get(r.currency, Decimal("0")) + r.amount
    return IncomeListResult(rows=rows, sum_by_currency=sums)


def _latest_rate_for_month(session: Session, year: int, month: int) -> ExchangeRate | None:
    last_day = date_type(year, month, calendar.monthrange(year, month)[1])
    return session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date <= last_day)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )


@dataclass
class IncomeCreate:
    year: int
    month: int
    source: IncomeSource
    amount: Decimal
    currency: Currency
    exchange_rate_id: int | None = None  # explicit override


def create_income(session: Session, payload: IncomeCreate) -> IncomeRow:
    if payload.exchange_rate_id is not None:
        rate = session.get(ExchangeRate, payload.exchange_rate_id)
    else:
        rate = _latest_rate_for_month(session, payload.year, payload.month)
    entry = IncomeEntry(
        year=payload.year,
        month=payload.month,
        source=payload.source,
        amount=payload.amount,
        currency=payload.currency,
        exchange_rate_id=rate.id if rate else None,
    )
    session.add(entry)
    try:
        session.flush()
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ValueError(
            f"Income entry already exists for {payload.year}-{payload.month:02d} {payload.source.value}"
        )
    session.refresh(entry)
    return _row(entry)


@dataclass
class IncomePatch:
    year: int | None = None
    month: int | None = None
    source: IncomeSource | None = None
    amount: Decimal | None = None
    currency: Currency | None = None
    exchange_rate_id: int | None = None  # explicit override


def update_income(session: Session, income_id: int, patch: IncomePatch) -> IncomeRow:
    entry = session.get(IncomeEntry, income_id)
    if entry is None:
        raise LookupError(f"Income entry {income_id} not found")

    period_changed = False
    if patch.year is not None and patch.year != entry.year:
        entry.year = patch.year
        period_changed = True
    if patch.month is not None and patch.month != entry.month:
        entry.month = patch.month
        period_changed = True
    if patch.source is not None:
        entry.source = patch.source
    if patch.amount is not None:
        entry.amount = patch.amount
    if patch.currency is not None:
        entry.currency = patch.currency
    if patch.exchange_rate_id is not None:
        # Explicit override from the UI — bypasses the period-driven default.
        entry.exchange_rate_id = patch.exchange_rate_id
    elif period_changed:
        rate = _latest_rate_for_month(session, entry.year, entry.month)
        entry.exchange_rate_id = rate.id if rate else None

    target = (entry.year, entry.month, entry.source.value)
    try:
        session.flush()
        session.commit()
    except IntegrityError:
        session.rollback()
        y, m, s = target
        raise ValueError(f"Income entry already exists for {y}-{m:02d} {s}")
    session.refresh(entry)
    return _row(entry)


def delete_income(session: Session, income_id: int) -> None:
    entry = session.get(IncomeEntry, income_id)
    if entry is None:
        raise LookupError(f"Income entry {income_id} not found")
    session.delete(entry)
    session.commit()
