"""Material-assets service (Total Worth).

Assets are tracked independently of savings/debts. Total in USD is computed
on demand using the most recent exchange rate ≤ a given date (or current
month for the live monthly report).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, AssetKind, Currency


@dataclass(frozen=True)
class AssetRow:
    id: int
    name: str
    kind: str
    location: str | None
    acquired_date: date_type | None
    current_value: Decimal
    currency: str
    last_valued_date: date_type | None
    last_service_date: date_type | None
    next_service_due_date: date_type | None
    notes: str | None


@dataclass
class AssetCreate:
    name: str
    kind: str
    current_value: Decimal
    currency: str
    location: str | None = None
    acquired_date: date_type | None = None
    last_valued_date: date_type | None = None
    last_service_date: date_type | None = None
    next_service_due_date: date_type | None = None
    notes: str | None = None


@dataclass
class AssetPatch:
    name: str | None = None
    kind: str | None = None
    location: str | None = None
    acquired_date: date_type | None = None
    current_value: Decimal | None = None
    currency: str | None = None
    last_valued_date: date_type | None = None
    last_service_date: date_type | None = None
    next_service_due_date: date_type | None = None
    notes: str | None = None


def _row(a: Asset) -> AssetRow:
    return AssetRow(
        id=a.id,
        name=a.name,
        kind=a.kind.value,
        location=a.location,
        acquired_date=a.acquired_date,
        current_value=Decimal(a.current_value),
        currency=a.currency.value,
        last_valued_date=a.last_valued_date,
        last_service_date=a.last_service_date,
        next_service_due_date=a.next_service_due_date,
        notes=a.notes,
    )


def list_assets(session: Session) -> list[AssetRow]:
    rows = session.scalars(select(Asset).order_by(Asset.id)).all()
    return [_row(a) for a in rows]


def create_asset(session: Session, payload: AssetCreate) -> AssetRow:
    a = Asset(
        name=payload.name[:120],
        kind=AssetKind(payload.kind),
        location=payload.location[:120] if payload.location else None,
        acquired_date=payload.acquired_date,
        current_value=payload.current_value,
        currency=Currency(payload.currency),
        last_valued_date=payload.last_valued_date,
        last_service_date=payload.last_service_date,
        next_service_due_date=payload.next_service_due_date,
        notes=payload.notes[:500] if payload.notes else None,
    )
    session.add(a)
    session.flush()
    session.commit()
    return _row(a)


def update_asset(session: Session, asset_id: int, patch: AssetPatch) -> AssetRow:
    a = session.get(Asset, asset_id)
    if a is None:
        raise LookupError(f"Asset {asset_id} not found")
    if patch.name is not None:
        a.name = patch.name[:120]
    if patch.kind is not None:
        a.kind = AssetKind(patch.kind)
    if patch.location is not None:
        a.location = patch.location[:120] or None
    if patch.acquired_date is not None:
        a.acquired_date = patch.acquired_date
    if patch.current_value is not None:
        a.current_value = patch.current_value
        # Touching the value sets last_valued_date unless caller overrides.
        if patch.last_valued_date is None:
            a.last_valued_date = datetime.now(timezone.utc).date()
    if patch.currency is not None:
        a.currency = Currency(patch.currency)
    if patch.last_valued_date is not None:
        a.last_valued_date = patch.last_valued_date
    if patch.last_service_date is not None:
        a.last_service_date = patch.last_service_date
    if patch.next_service_due_date is not None:
        a.next_service_due_date = patch.next_service_due_date
    if patch.notes is not None:
        a.notes = patch.notes[:500] or None
    session.flush()
    session.commit()
    return _row(a)


def delete_asset(session: Session, asset_id: int) -> None:
    a = session.get(Asset, asset_id)
    if a is None:
        raise LookupError(f"Asset {asset_id} not found")
    session.delete(a)
    session.commit()


def assets_total_usd(session: Session, effective: Decimal) -> Decimal:
    """Sum every asset's value in USD using the given effective BRL/USD rate.

    Used by the monthly/annual reports for the Total Worth KPI.
    """
    rows = session.scalars(select(Asset)).all()
    total = Decimal("0")
    for a in rows:
        v = Decimal(a.current_value)
        if a.currency == Currency.USD:
            total += v
        else:
            total += v / Decimal(effective) if effective else v
    return total
