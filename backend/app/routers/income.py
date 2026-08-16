"""Income entry endpoints."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Currency, IncomeSource
from app.services.income import (
    IncomeCreate,
    IncomePatch,
    IncomeRow,
    create_income,
    delete_income,
    list_income,
    update_income,
)

router = APIRouter(prefix="/api/income", tags=["income"])


class IncomeOut(BaseModel):
    id: int
    year: int
    month: int
    source: str
    amount: Decimal
    currency: str
    exchange_rate_id: int | None
    exchange_rate_effective: Decimal | None
    exchange_rate_date: date_type | None

    @classmethod
    def from_row(cls, row: IncomeRow) -> "IncomeOut":
        return cls(**row.__dict__)


class IncomeListOut(BaseModel):
    sum_by_currency: dict[str, Decimal]
    entries: list[IncomeOut]


class IncomeIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    source: IncomeSource
    amount: Decimal
    currency: Currency
    exchange_rate_id: int | None = None


class IncomePatchIn(BaseModel):
    year: int | None = Field(default=None, ge=2000, le=2100)
    month: int | None = Field(default=None, ge=1, le=12)
    source: IncomeSource | None = None
    amount: Decimal | None = None
    currency: Currency | None = None
    exchange_rate_id: int | None = None


@router.get("", response_model=IncomeListOut)
def list_endpoint(
    year: int | None = None,
    month: int | None = Query(default=None, ge=1, le=12),
    source: IncomeSource | None = None,
    db: Session = Depends(get_db),
) -> IncomeListOut:
    result = list_income(db, year=year, month=month, source=source)
    return IncomeListOut(
        sum_by_currency=result.sum_by_currency,
        entries=[IncomeOut.from_row(r) for r in result.rows],
    )


@router.post("", response_model=IncomeOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(payload: IncomeIn, db: Session = Depends(get_db)) -> IncomeOut:
    try:
        row = create_income(
            db,
            IncomeCreate(
                year=payload.year,
                month=payload.month,
                source=payload.source,
                amount=payload.amount,
                currency=payload.currency,
                exchange_rate_id=payload.exchange_rate_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return IncomeOut.from_row(row)


@router.patch("/{income_id}", response_model=IncomeOut)
def patch_endpoint(
    income_id: int,
    patch: IncomePatchIn,
    db: Session = Depends(get_db),
) -> IncomeOut:
    try:
        row = update_income(
            db,
            income_id,
            IncomePatch(**patch.model_dump(exclude_unset=True)),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return IncomeOut.from_row(row)


@router.delete("/{income_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(income_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_income(db, income_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
