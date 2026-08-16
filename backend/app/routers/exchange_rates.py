"""Exchange rate endpoints."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.exchange_rates import (
    DEFAULT_IOF,
    DEFAULT_SPREAD,
    ExchangeRateRow,
    create_rate,
    delete_rate,
    list_rates,
    update_rate,
)

router = APIRouter(prefix="/api/exchange-rates", tags=["exchange_rates"])


class ExchangeRateOut(BaseModel):
    id: int
    rate_date: date_type
    commercial: Decimal
    spread: Decimal
    iof: Decimal
    effective: Decimal

    @classmethod
    def from_row(cls, row: ExchangeRateRow) -> "ExchangeRateOut":
        return cls(**row.__dict__)


class ExchangeRateIn(BaseModel):
    rate_date: date_type
    commercial: Decimal = Field(gt=0)
    spread: Decimal | None = None
    iof: Decimal | None = None


class ExchangeRatePatchIn(BaseModel):
    rate_date: date_type | None = None
    commercial: Decimal | None = Field(default=None, gt=0)
    spread: Decimal | None = None
    iof: Decimal | None = None


class ExchangeRateDefaults(BaseModel):
    spread: Decimal
    iof: Decimal


@router.get("/defaults", response_model=ExchangeRateDefaults)
def defaults_endpoint() -> ExchangeRateDefaults:
    return ExchangeRateDefaults(spread=DEFAULT_SPREAD, iof=DEFAULT_IOF)


@router.get("", response_model=list[ExchangeRateOut])
def list_endpoint(db: Session = Depends(get_db)) -> list[ExchangeRateOut]:
    return [ExchangeRateOut.from_row(r) for r in list_rates(db)]


@router.post("", response_model=ExchangeRateOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(payload: ExchangeRateIn, db: Session = Depends(get_db)) -> ExchangeRateOut:
    try:
        row = create_rate(
            db,
            rate_date=payload.rate_date,
            commercial=payload.commercial,
            spread=payload.spread,
            iof=payload.iof,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ExchangeRateOut.from_row(row)


@router.patch("/{rate_id}", response_model=ExchangeRateOut)
def patch_endpoint(
    rate_id: int,
    patch: ExchangeRatePatchIn,
    db: Session = Depends(get_db),
) -> ExchangeRateOut:
    try:
        row = update_rate(
            db,
            rate_id,
            **patch.model_dump(exclude_unset=True),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ExchangeRateOut.from_row(row)


@router.delete("/{rate_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(rate_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_rate(db, rate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
