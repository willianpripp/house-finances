"""Savings snapshot endpoints."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Currency, SavingsSnapshot
from app.services.provider_guard import guard_savings_snapshot_write
from app.services.savings import (
    SavingsCreate,
    SavingsPatch,
    SavingsRow,
    create_snapshot,
    current_balances,
    delete_snapshot,
    list_account_names,
    list_snapshots,
    update_snapshot,
)

router = APIRouter(prefix="/api/savings", tags=["savings"])


class SavingsOut(BaseModel):
    id: int
    account_name: str
    currency: str
    balance: Decimal
    recorded_at: datetime
    # Populated only on /current (None on /snapshots history list).
    usd_equivalent: Decimal | None = None
    prev_balance: Decimal | None = None
    mom_pct: Decimal | None = None

    @classmethod
    def from_row(cls, row: SavingsRow) -> "SavingsOut":
        return cls(**row.__dict__)


class HeatmapBoundsOut(BaseModel):
    min_usd: Decimal
    max_usd: Decimal


class SavingsListOut(BaseModel):
    sum_by_currency: dict[str, Decimal]
    snapshots: list[SavingsOut]
    heatmap_bounds: HeatmapBoundsOut | None = None


class SavingsIn(BaseModel):
    account_name: str
    currency: Currency
    balance: Decimal
    recorded_at: datetime | None = None


class SavingsPatchIn(BaseModel):
    account_name: str | None = None
    currency: Currency | None = None
    balance: Decimal | None = None
    recorded_at: datetime | None = None


@router.get("/snapshots", response_model=SavingsListOut)
def list_endpoint(
    account_name: str | None = None,
    from_date: datetime | None = Query(default=None, alias="from"),
    to_date: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> SavingsListOut:
    result = list_snapshots(
        db, account_name=account_name, from_date=from_date, to_date=to_date, limit=limit
    )
    return SavingsListOut(
        sum_by_currency=result.sum_by_currency,
        snapshots=[SavingsOut.from_row(r) for r in result.rows],
    )


@router.get("/current", response_model=SavingsListOut)
def current_endpoint(db: Session = Depends(get_db)) -> SavingsListOut:
    result = current_balances(db)
    bounds = None
    if result.heatmap_bounds is not None:
        bounds = HeatmapBoundsOut(
            min_usd=result.heatmap_bounds.min_usd,
            max_usd=result.heatmap_bounds.max_usd,
        )
    return SavingsListOut(
        sum_by_currency=result.sum_by_currency,
        snapshots=[SavingsOut.from_row(r) for r in result.rows],
        heatmap_bounds=bounds,
    )


@router.get("/accounts", response_model=list[str])
def accounts_endpoint(db: Session = Depends(get_db)) -> list[str]:
    return list_account_names(db)


@router.post("/snapshots", response_model=SavingsOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(payload: SavingsIn, db: Session = Depends(get_db)) -> SavingsOut:
    if not payload.account_name.strip():
        raise HTTPException(status_code=400, detail="account_name is required")
    guard_savings_snapshot_write(db, payload.account_name)
    row = create_snapshot(
        db,
        SavingsCreate(
            account_name=payload.account_name,
            currency=payload.currency,
            balance=payload.balance,
            recorded_at=payload.recorded_at,
        ),
    )
    return SavingsOut.from_row(row)


@router.patch("/snapshots/{snapshot_id}", response_model=SavingsOut)
def patch_endpoint(
    snapshot_id: int,
    patch: SavingsPatchIn,
    db: Session = Depends(get_db),
) -> SavingsOut:
    snap = db.get(SavingsSnapshot, snapshot_id)
    if snap is None:
        raise HTTPException(
            status_code=404, detail=f"Savings snapshot {snapshot_id} not found"
        )
    # Both ends of the edit: the account this row already belongs to, and the
    # account a rename would move it into. Either being provider-fed makes this
    # a second writer on that account's balance.
    guard_savings_snapshot_write(db, snap.account_name)
    if patch.account_name is not None:
        guard_savings_snapshot_write(db, patch.account_name)
    try:
        row = update_snapshot(
            db, snapshot_id, SavingsPatch(**patch.model_dump(exclude_unset=True))
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return SavingsOut.from_row(row)


@router.delete("/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(snapshot_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_snapshot(db, snapshot_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
