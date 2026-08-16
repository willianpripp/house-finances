"""List categories (for UI dropdowns)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Category

router = APIRouter(prefix="/api/categories", tags=["categories"])


class CategoryOut(BaseModel):
    id: int
    name: str
    type: str
    color: str
    icon: str | None = None


@router.get("", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_db)) -> list[CategoryOut]:
    rows = db.scalars(select(Category).order_by(Category.type, Category.name)).all()
    return [
        CategoryOut(id=c.id, name=c.name, type=c.type.value, color=c.color, icon=c.icon)
        for c in rows
    ]
