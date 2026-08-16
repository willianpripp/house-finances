"""List merchants (for UI dropdowns / autocomplete)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Merchant

router = APIRouter(prefix="/api/merchants", tags=["merchants"])


class MerchantOut(BaseModel):
    id: int
    name: str
    default_category_id: int | None = None


@router.get("", response_model=list[MerchantOut])
def list_merchants(
    search: str | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
) -> list[MerchantOut]:
    stmt = select(Merchant).order_by(Merchant.name).limit(limit)
    if search:
        stmt = select(Merchant).where(Merchant.name.ilike(f"%{search}%")).order_by(Merchant.name).limit(limit)
    rows = db.scalars(stmt).all()
    return [
        MerchantOut(id=m.id, name=m.name, default_category_id=m.default_category_id)
        for m in rows
    ]
