"""Exchange rate endpoints. Read-only: rates land in the table only through
the PTAX auto-fetch (scripts/refresh_exchange_rate.py, daily run and
--backfill mode). There is no create or edit HTTP surface (2026-08-20:
automation replaces manual entry, it does not sit alongside it) — the
service-layer create_rate() still exists for those scripts to call, see
app/services/exchange_rates.py.

The dedicated /exchange-rates page is gone too (2026-08-20: it was a
read-only listing of data the monthly report already surfaces; one path,
no second page for the same numbers). The list GET below stays: it is a
real consumer of /assets, /savings and /income, all of which fetch it
directly for the latest/available rate(s). GET /defaults is gone with the
page: it had no other caller.

DELETE stays: it is not a way to type in a rate, it is the escape hatch for
a bad auto-fetched row, letting the next refresh/backfill run re-fill that
date under the same never-overwrite rule.
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.exchange_rates import ExchangeRateRow, delete_rate, list_rates

router = APIRouter(prefix="/api/exchange-rates", tags=["exchange_rates"])


class ExchangeRateOut(BaseModel):
    id: int
    rate_date: date_type
    commercial: Decimal
    spread: Decimal
    iof: Decimal
    effective: Decimal
    source: str

    @classmethod
    def from_row(cls, row: ExchangeRateRow) -> "ExchangeRateOut":
        return cls(**row.__dict__)


@router.get("", response_model=list[ExchangeRateOut])
def list_endpoint(db: Session = Depends(get_db)) -> list[ExchangeRateOut]:
    return [ExchangeRateOut.from_row(r) for r in list_rates(db)]


@router.delete("/{rate_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(rate_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_rate(db, rate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
