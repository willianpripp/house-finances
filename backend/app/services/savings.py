"""Savings snapshots: list / current / create / update / delete.

A snapshot records one account's balance at a point in time. We never
overwrite — each balance change creates a new row, so the history table
stays queryable for net-worth charts.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, SavingsSnapshot


@dataclass(frozen=True)
class SavingsRow:
    id: int
    account_name: str
    currency: str
    balance: Decimal
    recorded_at: datetime
    # Heatmap + MoM (only populated by current_balances()).
    usd_equivalent: Decimal | None = None
    prev_balance: Decimal | None = None
    mom_pct: Decimal | None = None


@dataclass(frozen=True)
class HeatmapBounds:
    min_usd: Decimal
    max_usd: Decimal


@dataclass(frozen=True)
class SavingsListResult:
    rows: list[SavingsRow]
    sum_by_currency: dict[str, Decimal] = field(default_factory=dict)
    heatmap_bounds: HeatmapBounds | None = None


def _row(snap: SavingsSnapshot) -> SavingsRow:
    return SavingsRow(
        id=snap.id,
        account_name=snap.account_name,
        currency=snap.currency.value,
        balance=snap.balance,
        recorded_at=snap.recorded_at,
    )


def list_snapshots(
    session: Session,
    *,
    account_name: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 1000,
) -> SavingsListResult:
    stmt = select(SavingsSnapshot).order_by(
        desc(SavingsSnapshot.recorded_at), SavingsSnapshot.account_name
    ).limit(limit)
    if account_name:
        stmt = stmt.where(SavingsSnapshot.account_name == account_name)
    if from_date:
        stmt = stmt.where(SavingsSnapshot.recorded_at >= from_date)
    if to_date:
        stmt = stmt.where(SavingsSnapshot.recorded_at <= to_date)
    snaps = session.scalars(stmt).all()
    rows = [_row(s) for s in snaps]
    sums: dict[str, Decimal] = {}
    for r in rows:
        sums[r.currency] = sums.get(r.currency, Decimal("0")) + r.balance
    return SavingsListResult(rows=rows, sum_by_currency=sums)


def _latest_effective_rate(session: Session) -> Decimal:
    """Latest exchange_rates.effective value, used for BRL → USD conversion of
    'current' figures (savings, debts). Falls back to 1 if no rate is loaded —
    this only happens on a brand-new install with no seed data."""
    rate = session.scalar(
        select(ExchangeRate).order_by(desc(ExchangeRate.rate_date)).limit(1)
    )
    if rate is None:
        return Decimal("1")
    return Decimal(rate.effective)


def _prev_month_bounds(today: date) -> tuple[date, date]:
    """[first day, last day] of the calendar month preceding `today`."""
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    last_day = calendar.monthrange(prev_year, prev_month)[1]
    return date(prev_year, prev_month, 1), date(prev_year, prev_month, last_day)


def _latest_prev_month_balance(
    session: Session, account_name: str, today: date | None = None
) -> Decimal | None:
    """Latest savings_snapshots.balance for `account_name` whose recorded_at
    falls inside the previous calendar month. None if no such row exists.
    Used for MoM badges in current_balances()."""
    if today is None:
        today = date.today()
    first, last = _prev_month_bounds(today)
    start = datetime.combine(first, datetime.min.time())
    end = datetime.combine(last, datetime.max.time())
    snap = session.scalar(
        select(SavingsSnapshot)
        .where(
            SavingsSnapshot.account_name == account_name,
            SavingsSnapshot.recorded_at >= start,
            SavingsSnapshot.recorded_at <= end,
        )
        .order_by(desc(SavingsSnapshot.recorded_at))
        .limit(1)
    )
    return Decimal(snap.balance) if snap else None


def current_balances(session: Session) -> SavingsListResult:
    """Latest snapshot per account_name, enriched with USD equivalent, prior-
    month balance (for MoM badge), and a global heatmap range (min/max USD)
    across all accounts. The heatmap range is what the UI maps to a gradient
    from cinza claro (min) to verde escuro (max)."""
    sub = (
        select(
            SavingsSnapshot.account_name,
            func.max(SavingsSnapshot.recorded_at).label("max_at"),
        )
        .group_by(SavingsSnapshot.account_name)
        .subquery()
    )
    stmt = (
        select(SavingsSnapshot)
        .join(
            sub,
            (SavingsSnapshot.account_name == sub.c.account_name)
            & (SavingsSnapshot.recorded_at == sub.c.max_at),
        )
        .order_by(SavingsSnapshot.account_name)
    )
    snaps = session.scalars(stmt).all()
    effective = _latest_effective_rate(session)
    today = date.today()

    enriched: list[SavingsRow] = []
    sums: dict[str, Decimal] = {}
    for s in snaps:
        balance = Decimal(s.balance)
        if s.currency == Currency.USD:
            usd_eq = balance
        else:
            usd_eq = balance / effective if effective else balance
        prev = _latest_prev_month_balance(session, s.account_name, today=today)
        if prev is not None and prev != 0:
            mom_pct = ((balance - prev) / abs(prev)) * Decimal("100")
            mom_pct = mom_pct.quantize(Decimal("0.1"))
        else:
            mom_pct = None
        enriched.append(SavingsRow(
            id=s.id,
            account_name=s.account_name,
            currency=s.currency.value,
            balance=balance,
            recorded_at=s.recorded_at,
            usd_equivalent=usd_eq.quantize(Decimal("0.01")),
            prev_balance=prev,
            mom_pct=mom_pct,
        ))
        sums[s.currency.value] = sums.get(s.currency.value, Decimal("0")) + balance

    bounds: HeatmapBounds | None = None
    usd_values = [r.usd_equivalent for r in enriched if r.usd_equivalent is not None]
    if usd_values:
        bounds = HeatmapBounds(min_usd=min(usd_values), max_usd=max(usd_values))

    return SavingsListResult(rows=enriched, sum_by_currency=sums, heatmap_bounds=bounds)


def list_account_names(session: Session) -> list[str]:
    rows = session.scalars(
        select(SavingsSnapshot.account_name).distinct().order_by(SavingsSnapshot.account_name)
    ).all()
    return list(rows)


@dataclass
class SavingsCreate:
    account_name: str
    currency: Currency
    balance: Decimal
    recorded_at: datetime | None = None


def create_snapshot(session: Session, payload: SavingsCreate) -> SavingsRow:
    snap = SavingsSnapshot(
        account_name=payload.account_name.strip(),
        currency=payload.currency,
        balance=payload.balance,
    )
    if payload.recorded_at is not None:
        snap.recorded_at = payload.recorded_at
    session.add(snap)
    session.flush()
    session.commit()
    session.refresh(snap)
    return _row(snap)


@dataclass
class SavingsPatch:
    account_name: str | None = None
    currency: Currency | None = None
    balance: Decimal | None = None
    recorded_at: datetime | None = None


def update_snapshot(session: Session, snapshot_id: int, patch: SavingsPatch) -> SavingsRow:
    snap = session.get(SavingsSnapshot, snapshot_id)
    if snap is None:
        raise LookupError(f"Savings snapshot {snapshot_id} not found")
    if patch.account_name is not None:
        snap.account_name = patch.account_name.strip()
    if patch.currency is not None:
        snap.currency = patch.currency
    if patch.balance is not None:
        snap.balance = patch.balance
    if patch.recorded_at is not None:
        snap.recorded_at = patch.recorded_at
    session.flush()
    session.commit()
    session.refresh(snap)
    return _row(snap)


def delete_snapshot(session: Session, snapshot_id: int) -> None:
    snap = session.get(SavingsSnapshot, snapshot_id)
    if snap is None:
        raise LookupError(f"Savings snapshot {snapshot_id} not found")
    session.delete(snap)
    session.commit()
