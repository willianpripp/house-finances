"""Assets (Total Worth) JSON API."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.assets import (
    AssetCreate,
    AssetPatch,
    AssetRow,
    create_asset,
    delete_asset,
    list_assets,
    update_asset,
)

router = APIRouter(prefix="/api/assets", tags=["assets"])


class AssetOut(BaseModel):
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

    @classmethod
    def from_row(cls, row: AssetRow) -> "AssetOut":
        return cls(**row.__dict__)


class AssetCreateIn(BaseModel):
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


class AssetPatchIn(BaseModel):
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


@router.get("", response_model=list[AssetOut])
def list_endpoint(db: Session = Depends(get_db)) -> list[AssetOut]:
    return [AssetOut.from_row(r) for r in list_assets(db)]


@router.post("", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(body: AssetCreateIn, db: Session = Depends(get_db)) -> AssetOut:
    try:
        return AssetOut.from_row(create_asset(db, AssetCreate(**body.model_dump())))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{asset_id}", response_model=AssetOut)
def patch_endpoint(
    asset_id: int, body: AssetPatchIn, db: Session = Depends(get_db)
) -> AssetOut:
    try:
        return AssetOut.from_row(
            update_asset(db, asset_id, AssetPatch(**body.model_dump(exclude_unset=True)))
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(asset_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        delete_asset(db, asset_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
