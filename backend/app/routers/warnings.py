"""Warnings endpoints: overdraft forecast + expiring contracts."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.warnings import (
    expiring_contracts,
    overdraft_forecast,
    statement_alerts,
    summarize,
)

router = APIRouter(prefix="/api/warnings", tags=["warnings"])


class OverdraftEventOut(BaseModel):
    event_date: date
    amount: Decimal
    source: str
    source_type: str
    amount_usd: Decimal | None = None


class OverdraftForecastOut(BaseModel):
    checking_id: int
    checking_name: str
    currency: str
    current_balance: Decimal
    projected_balance: Decimal
    projected_debits: Decimal
    projected_incomes: Decimal
    min_balance: Decimal
    min_balance_date: date | None
    deficit: Decimal
    has_cc_links: bool
    events: list[OverdraftEventOut]
    current_balance_usd: Decimal | None = None
    projected_balance_usd: Decimal | None = None
    projected_debits_usd: Decimal | None = None
    projected_incomes_usd: Decimal | None = None


class ExpiringItemOut(BaseModel):
    transaction_id: int
    transaction_date: date
    merchant_name: str
    category_name: str
    payment_method_name: str
    amount: Decimal
    currency: str
    recurrence_kind: str
    severity: str
    detail: str
    end_date: date


class SummaryItemOut(BaseModel):
    severity: str
    kind: str
    title: str
    detail: str


class SummaryOut(BaseModel):
    overdraft_count: int
    expiring_count: int
    items: list[SummaryItemOut]


class StatementAlertOut(BaseModel):
    payment_method_id: int
    payment_method_name: str
    currency: str
    kind: str
    severity: str
    target_date: date
    days_offset: int
    message: str
    balance: Decimal | None = None


@router.get("/statements", response_model=list[StatementAlertOut])
def statements_endpoint(db: Session = Depends(get_db)) -> list[StatementAlertOut]:
    return [StatementAlertOut(**a.__dict__) for a in statement_alerts(db)]


@router.get("/overdraft", response_model=list[OverdraftForecastOut])
def overdraft_endpoint(
    horizon_days: int = Query(default=14, ge=1, le=180),
    db: Session = Depends(get_db),
) -> list[OverdraftForecastOut]:
    forecasts = overdraft_forecast(db, horizon_days=horizon_days)
    return [
        OverdraftForecastOut(
            checking_id=f.checking_id,
            checking_name=f.checking_name,
            currency=f.currency,
            current_balance=f.current_balance,
            projected_balance=f.projected_balance,
            projected_debits=f.projected_debits,
            projected_incomes=f.projected_incomes,
            min_balance=f.min_balance,
            min_balance_date=f.min_balance_date,
            deficit=f.deficit,
            has_cc_links=f.has_cc_links,
            events=[OverdraftEventOut(**e.__dict__) for e in f.events],
            current_balance_usd=f.current_balance_usd,
            projected_balance_usd=f.projected_balance_usd,
            projected_debits_usd=f.projected_debits_usd,
            projected_incomes_usd=f.projected_incomes_usd,
        )
        for f in forecasts
    ]


@router.get("/expiring", response_model=list[ExpiringItemOut])
def expiring_endpoint(
    horizon_days: int = Query(default=60, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[ExpiringItemOut]:
    return [
        ExpiringItemOut(**e.__dict__)
        for e in expiring_contracts(db, horizon_days=horizon_days)
    ]


@router.get("/summary", response_model=SummaryOut)
def summary_endpoint(db: Session = Depends(get_db)) -> SummaryOut:
    s = summarize(db)
    return SummaryOut(
        overdraft_count=s.overdraft_count,
        expiring_count=s.expiring_count,
        items=[SummaryItemOut(**i) for i in s.items],
    )
