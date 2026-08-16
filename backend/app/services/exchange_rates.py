"""Exchange rate CRUD with effective auto-computation.

The effective rate is what we apply to BRL→USD conversions in reports;
it bakes in the bank spread and the IOF tax. Defaults are the project's
standard 1.5% spread + 1.1% IOF (matches v1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExchangeRate

DEFAULT_SPREAD = Decimal("0.015")
DEFAULT_IOF = Decimal("0.011")


def compute_effective(commercial: Decimal, spread: Decimal, iof: Decimal) -> Decimal:
    raw = Decimal(commercial) * (Decimal("1") + Decimal(spread)) * (Decimal("1") + Decimal(iof))
    return raw.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass
class ExchangeRateRow:
    id: int
    rate_date: date
    commercial: Decimal
    spread: Decimal
    iof: Decimal
    effective: Decimal


def _to_row(r: ExchangeRate) -> ExchangeRateRow:
    return ExchangeRateRow(
        id=r.id,
        rate_date=r.rate_date,
        commercial=Decimal(r.commercial),
        spread=Decimal(r.spread),
        iof=Decimal(r.iof),
        effective=Decimal(r.effective),
    )


def list_rates(session: Session) -> list[ExchangeRateRow]:
    rows = session.scalars(
        select(ExchangeRate).order_by(ExchangeRate.rate_date.desc())
    ).all()
    return [_to_row(r) for r in rows]


def get_rate(session: Session, rate_id: int) -> ExchangeRateRow:
    r = session.get(ExchangeRate, rate_id)
    if r is None:
        raise LookupError(f"Exchange rate {rate_id} not found")
    return _to_row(r)


def create_rate(
    session: Session,
    *,
    rate_date: date,
    commercial: Decimal,
    spread: Decimal | None = None,
    iof: Decimal | None = None,
) -> ExchangeRateRow:
    spread = DEFAULT_SPREAD if spread is None else Decimal(spread)
    iof = DEFAULT_IOF if iof is None else Decimal(iof)
    existing = session.scalar(select(ExchangeRate).filter_by(rate_date=rate_date))
    if existing is not None:
        raise ValueError(f"Exchange rate for {rate_date} already exists (id={existing.id})")
    effective = compute_effective(commercial, spread, iof)
    r = ExchangeRate(
        rate_date=rate_date,
        commercial=commercial,
        spread=spread,
        iof=iof,
        effective=effective,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return _to_row(r)


def update_rate(
    session: Session,
    rate_id: int,
    *,
    rate_date: date | None = None,
    commercial: Decimal | None = None,
    spread: Decimal | None = None,
    iof: Decimal | None = None,
) -> ExchangeRateRow:
    r = session.get(ExchangeRate, rate_id)
    if r is None:
        raise LookupError(f"Exchange rate {rate_id} not found")

    if rate_date is not None and rate_date != r.rate_date:
        clash = session.scalar(select(ExchangeRate).filter_by(rate_date=rate_date))
        if clash is not None and clash.id != rate_id:
            raise ValueError(f"Exchange rate for {rate_date} already exists (id={clash.id})")
        r.rate_date = rate_date
    if commercial is not None:
        r.commercial = commercial
    if spread is not None:
        r.spread = spread
    if iof is not None:
        r.iof = iof

    r.effective = compute_effective(
        Decimal(r.commercial), Decimal(r.spread), Decimal(r.iof)
    )
    session.commit()
    session.refresh(r)
    return _to_row(r)


def delete_rate(session: Session, rate_id: int) -> None:
    r = session.get(ExchangeRate, rate_id)
    if r is None:
        raise LookupError(f"Exchange rate {rate_id} not found")
    session.delete(r)
    session.commit()
