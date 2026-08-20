"""Debt endpoints — credit card balances and car loan payments."""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CreditCardBalance
from app.services.provider_guard import guard_card_balance_write
from app.services.debts import (
    CarPaymentCreate,
    CarPaymentPatch,
    CarPaymentRow,
    CardBalanceCreate,
    CardBalancePatch,
    CardBalanceRow,
    car_loan_summary,
    create_car_payment,
    create_card_balance,
    current_card_balances,
    delete_car_payment,
    delete_card_balance,
    list_car_payments,
    list_card_balances,
    update_car_payment,
    update_card_balance,
)

router = APIRouter(prefix="/api/debts", tags=["debts"])


# ---------- credit card schemas ----------

class CardBalanceOut(BaseModel):
    id: int
    payment_method_id: int
    payment_method_name: str
    currency: str
    balance: Decimal
    statement: Decimal | None
    due_day: int | None
    recorded_at: datetime
    # Populated only on /cards/current.
    usd_equivalent: Decimal | None = None
    prev_balance: Decimal | None = None
    mom_pct: Decimal | None = None
    # When populated, `balance` is recorded + post-balance delta
    # and these fields expose the breakdown for /debts and /warnings tooltips.
    recorded_balance: Decimal | None = None
    post_balance_delta: Decimal | None = None

    @classmethod
    def from_row(cls, row: CardBalanceRow) -> "CardBalanceOut":
        return cls(**row.__dict__)


class CardHeatmapBoundsOut(BaseModel):
    min_usd: Decimal
    max_usd: Decimal


class CardBalanceListOut(BaseModel):
    sum_by_currency: dict[str, Decimal]
    balances: list[CardBalanceOut]
    heatmap_bounds: CardHeatmapBoundsOut | None = None


class CardBalanceIn(BaseModel):
    payment_method_id: int
    balance: Decimal
    statement: Decimal | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    recorded_at: datetime | None = None


class CardBalancePatchIn(BaseModel):
    payment_method_id: int | None = None
    balance: Decimal | None = None
    statement: Decimal | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    recorded_at: datetime | None = None


# ---------- credit card endpoints ----------

@router.get("/cards/current", response_model=CardBalanceListOut)
def cards_current_endpoint(db: Session = Depends(get_db)) -> CardBalanceListOut:
    result = current_card_balances(db)
    bounds = None
    if result.heatmap_bounds is not None:
        bounds = CardHeatmapBoundsOut(
            min_usd=result.heatmap_bounds.min_usd,
            max_usd=result.heatmap_bounds.max_usd,
        )
    return CardBalanceListOut(
        sum_by_currency=result.sum_by_currency,
        balances=[CardBalanceOut.from_row(r) for r in result.rows],
        heatmap_bounds=bounds,
    )


@router.get("/cards/balances", response_model=CardBalanceListOut)
def cards_list_endpoint(
    payment_method_id: int | None = None,
    from_date: datetime | None = Query(default=None, alias="from"),
    to_date: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> CardBalanceListOut:
    result = list_card_balances(
        db,
        payment_method_id=payment_method_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )
    return CardBalanceListOut(
        sum_by_currency=result.sum_by_currency,
        balances=[CardBalanceOut.from_row(r) for r in result.rows],
    )


@router.post("/cards/balances", response_model=CardBalanceOut, status_code=status.HTTP_201_CREATED)
def cards_create_endpoint(payload: CardBalanceIn, db: Session = Depends(get_db)) -> CardBalanceOut:
    guard_card_balance_write(db, payload.payment_method_id)
    try:
        row = create_card_balance(
            db,
            CardBalanceCreate(
                payment_method_id=payload.payment_method_id,
                balance=payload.balance,
                statement=payload.statement,
                due_day=payload.due_day,
                recorded_at=payload.recorded_at,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return CardBalanceOut.from_row(row)


@router.patch("/cards/balances/{balance_id}", response_model=CardBalanceOut)
def cards_patch_endpoint(
    balance_id: int,
    patch: CardBalancePatchIn,
    db: Session = Depends(get_db),
) -> CardBalanceOut:
    row_before = db.get(CreditCardBalance, balance_id)
    if row_before is None:
        raise HTTPException(status_code=404, detail=f"Card balance {balance_id} not found")
    # Both ends of the edit, as on savings: the card this row records and the
    # card it would be reassigned to.
    guard_card_balance_write(db, row_before.payment_method_id)
    if patch.payment_method_id is not None:
        guard_card_balance_write(db, patch.payment_method_id)
    try:
        row = update_card_balance(
            db, balance_id, CardBalancePatch(**patch.model_dump(exclude_unset=True))
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return CardBalanceOut.from_row(row)


@router.delete(
    "/cards/balances/{balance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def cards_delete_endpoint(balance_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_card_balance(db, balance_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------- car loan schemas ----------

class CarPaymentOut(BaseModel):
    id: int
    posting_date: date_type
    payment_amount: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    new_balance: Decimal

    @classmethod
    def from_row(cls, row: CarPaymentRow) -> "CarPaymentOut":
        return cls(**row.__dict__)


class CarLoanSummaryOut(BaseModel):
    latest_balance: Decimal | None
    latest_payment_date: date_type | None
    total_payments: int
    total_principal_paid: Decimal
    total_interest_paid: Decimal


class CarLoanListOut(BaseModel):
    summary: CarLoanSummaryOut
    payments: list[CarPaymentOut]


class CarPaymentIn(BaseModel):
    posting_date: date_type
    payment_amount: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    new_balance: Decimal


class CarPaymentPatchIn(BaseModel):
    posting_date: date_type | None = None
    payment_amount: Decimal | None = None
    principal_paid: Decimal | None = None
    interest_paid: Decimal | None = None
    new_balance: Decimal | None = None


# ---------- car loan endpoints ----------

@router.get("/car-loan", response_model=CarLoanListOut)
def car_loan_endpoint(
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> CarLoanListOut:
    summary = car_loan_summary(db)
    payments = list_car_payments(db, limit=limit)
    return CarLoanListOut(
        summary=CarLoanSummaryOut(**summary.__dict__),
        payments=[CarPaymentOut.from_row(p) for p in payments],
    )


@router.post("/car-loan/payments", response_model=CarPaymentOut, status_code=status.HTTP_201_CREATED)
def car_loan_create_endpoint(payload: CarPaymentIn, db: Session = Depends(get_db)) -> CarPaymentOut:
    row = create_car_payment(db, CarPaymentCreate(**payload.model_dump()))
    return CarPaymentOut.from_row(row)


@router.patch("/car-loan/payments/{payment_id}", response_model=CarPaymentOut)
def car_loan_patch_endpoint(
    payment_id: int,
    patch: CarPaymentPatchIn,
    db: Session = Depends(get_db),
) -> CarPaymentOut:
    try:
        row = update_car_payment(
            db, payment_id, CarPaymentPatch(**patch.model_dump(exclude_unset=True))
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return CarPaymentOut.from_row(row)


@router.delete(
    "/car-loan/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def car_loan_delete_endpoint(payment_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_car_payment(db, payment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
