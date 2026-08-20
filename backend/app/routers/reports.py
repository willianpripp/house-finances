"""Report endpoints."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.reports import (
    AnnualCategoryBucket,
    AnnualReport,
    CategoryBucket,
    IncomeBucket,
    MonthTotals,
    MonthlyReport,
    TransactionDetail,
    annual_report,
    default_month,
    monthly_report,
)
from app.services.rollover import (
    CommitItem,
    RolloverError,
    RolloverItem,
    RolloverPreview,
    commit_rollover,
    preview_rollover,
)

router = APIRouter(prefix="/api/reports", tags=["reports"])


class IncomeBucketOut(BaseModel):
    source: str
    amount_native: Decimal
    currency: str
    amount_usd: Decimal
    # Which conversion rule produced amount_usd, and whether a fallback rate
    # was involved. Both UIs render the flag; see
    # services/income.convert_entry_to_usd.
    rate_basis: str = "usd"
    approximate: bool = False

    @classmethod
    def from_obj(cls, b: IncomeBucket) -> "IncomeBucketOut":
        return cls(**b.__dict__)


class CategoryBucketOut(BaseModel):
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    transaction_count: int
    category_icon: str | None = None
    # A transaction in this category had no exchange rate at or before its
    # date; see reports._usd_on_date.
    approximate: bool = False

    @classmethod
    def from_obj(cls, b: CategoryBucket) -> "CategoryBucketOut":
        return cls(**b.__dict__)


class MonthTotalsOut(BaseModel):
    year: int
    month: int
    is_finalized: bool

    rate_id: int | None
    rate_effective: Decimal | None
    rate_date: date_type | None

    primary_salary_usd: Decimal
    partner_salary_usd: Decimal
    rents_brazil_usd: Decimal
    extra_income_usd: Decimal
    gross_income_usd: Decimal
    taxes_usd: Decimal
    net_income_usd: Decimal
    taxes_partner_usd: Decimal = Decimal("0")
    taxes_primary_usd: Decimal = Decimal("0")
    income_rate_approximate: bool = False
    spending_rate_approximate: bool = False

    fixed_spending_usd: Decimal
    variable_spending_usd: Decimal
    total_spending_usd: Decimal

    surplus_usd: Decimal

    total_savings_usd: Decimal
    total_debt_usd: Decimal
    net_worth_usd: Decimal

    assets_total_usd: Decimal = Decimal("0")
    total_worth_usd: Decimal = Decimal("0")

    can_finalize: bool = False
    finalize_blocked_reason: str | None = None

    @classmethod
    def from_obj(cls, t: MonthTotals) -> "MonthTotalsOut":
        return cls(**t.__dict__)


class TransactionDetailOut(BaseModel):
    id: int
    transaction_date: date_type
    merchant_name: str
    payment_method_name: str
    category_id: int
    category_name: str
    category_type: str
    category_color: str
    category_icon: str | None = None
    owner_name: str | None
    amount_native: Decimal
    currency: str
    amount_usd: Decimal
    installment_current: int
    installment_total: int
    description: str | None
    pending: bool = False

    @classmethod
    def from_obj(cls, t: TransactionDetail) -> "TransactionDetailOut":
        return cls(**t.__dict__)


class MonthlyReportOut(BaseModel):
    totals: MonthTotalsOut
    income: list[IncomeBucketOut]
    by_category: list[CategoryBucketOut]
    fixed_categories: list[CategoryBucketOut]
    variable_categories: list[CategoryBucketOut]
    fixed_transactions: list[TransactionDetailOut]
    variable_transactions: list[TransactionDetailOut]
    excluded_categories: list[CategoryBucketOut]
    excluded_transactions: list[TransactionDetailOut]
    excluded_total_usd: Decimal
    prior: MonthTotalsOut | None

    @classmethod
    def from_obj(cls, r: MonthlyReport) -> "MonthlyReportOut":
        return cls(
            totals=MonthTotalsOut.from_obj(r.totals),
            income=[IncomeBucketOut.from_obj(b) for b in r.income],
            by_category=[CategoryBucketOut.from_obj(b) for b in r.by_category],
            fixed_categories=[CategoryBucketOut.from_obj(b) for b in r.fixed_categories],
            variable_categories=[CategoryBucketOut.from_obj(b) for b in r.variable_categories],
            fixed_transactions=[TransactionDetailOut.from_obj(t) for t in r.fixed_transactions],
            variable_transactions=[TransactionDetailOut.from_obj(t) for t in r.variable_transactions],
            excluded_categories=[CategoryBucketOut.from_obj(b) for b in r.excluded_categories],
            excluded_transactions=[TransactionDetailOut.from_obj(t) for t in r.excluded_transactions],
            excluded_total_usd=r.excluded_total_usd,
            prior=MonthTotalsOut.from_obj(r.prior) if r.prior else None,
        )


class DefaultMonthOut(BaseModel):
    year: int
    month: int


@router.get("/monthly/default", response_model=DefaultMonthOut)
def monthly_default_endpoint(db: Session = Depends(get_db)) -> DefaultMonthOut:
    y, m = default_month(db)
    return DefaultMonthOut(year=y, month=m)


@router.get("/monthly", response_model=MonthlyReportOut)
def monthly_endpoint(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
) -> MonthlyReportOut:
    return MonthlyReportOut.from_obj(monthly_report(db, year, month))


class AnnualCategoryBucketOut(BaseModel):
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    category_icon: str | None = None
    approximate: bool = False

    @classmethod
    def from_obj(cls, b: AnnualCategoryBucket) -> "AnnualCategoryBucketOut":
        return cls(**b.__dict__)


class AnnualReportOut(BaseModel):
    year: int
    months: list[MonthTotalsOut]
    gross_income_usd: Decimal
    taxes_usd: Decimal
    net_income_usd: Decimal
    fixed_spending_usd: Decimal
    variable_spending_usd: Decimal
    total_spending_usd: Decimal
    surplus_usd: Decimal
    end_savings_usd: Decimal
    end_debt_usd: Decimal
    end_net_worth_usd: Decimal
    end_assets_usd: Decimal = Decimal("0")
    end_total_worth_usd: Decimal = Decimal("0")
    top_categories: list[AnnualCategoryBucketOut]
    spending_rate_approximate: bool = False

    @classmethod
    def from_obj(cls, r: AnnualReport) -> "AnnualReportOut":
        return cls(
            year=r.year,
            months=[MonthTotalsOut.from_obj(m) for m in r.months],
            gross_income_usd=r.gross_income_usd,
            taxes_usd=r.taxes_usd,
            net_income_usd=r.net_income_usd,
            fixed_spending_usd=r.fixed_spending_usd,
            variable_spending_usd=r.variable_spending_usd,
            total_spending_usd=r.total_spending_usd,
            surplus_usd=r.surplus_usd,
            end_savings_usd=r.end_savings_usd,
            end_debt_usd=r.end_debt_usd,
            end_net_worth_usd=r.end_net_worth_usd,
            end_assets_usd=r.end_assets_usd,
            end_total_worth_usd=r.end_total_worth_usd,
            top_categories=[AnnualCategoryBucketOut.from_obj(c) for c in r.top_categories],
            spending_rate_approximate=r.spending_rate_approximate,
        )


@router.get("/annual", response_model=AnnualReportOut)
def annual_endpoint(
    year: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_db),
) -> AnnualReportOut:
    return AnnualReportOut.from_obj(annual_report(db, year))


# ---------- Rollover ----------

class RolloverItemOut(BaseModel):
    source_transaction_id: int
    merchant_id: int
    merchant_name: str
    payment_method_id: int
    payment_method_name: str
    category_id: int
    category_name: str
    category_color: str
    category_icon: str | None = None
    owner_id: int | None
    owner_name: str | None
    amount: Decimal
    currency: str
    description: str | None
    source_date: date_type
    suggested_target_date: date_type
    already_in_target: bool
    installment_current: int
    installment_total: int
    installment_value: Decimal | None
    installment_complete: bool
    recurrence_kind: str | None
    contract_end_date: date_type | None
    contract_complete: bool

    @classmethod
    def from_obj(cls, i: RolloverItem) -> "RolloverItemOut":
        return cls(**i.__dict__)


class RolloverPreviewOut(BaseModel):
    source_year: int
    source_month: int
    target_year: int
    target_month: int
    items: list[RolloverItemOut]

    @classmethod
    def from_obj(cls, p: RolloverPreview) -> "RolloverPreviewOut":
        return cls(
            source_year=p.source_year,
            source_month=p.source_month,
            target_year=p.target_year,
            target_month=p.target_month,
            items=[RolloverItemOut.from_obj(i) for i in p.items],
        )


class RolloverCommitItemIn(BaseModel):
    source_transaction_id: int
    target_date: date_type
    amount: Decimal


class RolloverCommitIn(BaseModel):
    items: list[RolloverCommitItemIn]


class RolloverCommitOut(BaseModel):
    target_year: int
    target_month: int
    inserted_ids: list[int]


@router.get("/monthly/rollover/preview", response_model=RolloverPreviewOut)
def rollover_preview_endpoint(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
) -> RolloverPreviewOut:
    return RolloverPreviewOut.from_obj(preview_rollover(db, year, month))


@router.post("/monthly/rollover/commit", response_model=RolloverCommitOut)
def rollover_commit_endpoint(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    payload: RolloverCommitIn = ...,
    db: Session = Depends(get_db),
) -> RolloverCommitOut:
    items = [
        CommitItem(
            source_transaction_id=i.source_transaction_id,
            target_date=i.target_date,
            amount=i.amount,
        )
        for i in payload.items
    ]
    try:
        ids = commit_rollover(db, year, month, items)
    except RolloverError as e:
        raise HTTPException(status_code=400, detail=str(e))
    target_year, target_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return RolloverCommitOut(
        target_year=target_year, target_month=target_month, inserted_ids=ids
    )
