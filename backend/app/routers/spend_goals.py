"""Spend-goal endpoints: list with derived progress, create, edit."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.spend_goals import (
    DuplicateSpendGoalError,
    SpendGoalCreate,
    SpendGoalPatch,
    SpendGoalProgress,
    create_goal,
    get_goal,
    list_goals,
    update_goal,
)

router = APIRouter(prefix="/api/spend-goals", tags=["spend_goals"])


class SpendGoalOut(BaseModel):
    id: int
    payment_method_id: int
    payment_method_name: str
    target_amount: Decimal
    currency: str
    start_date: date_type
    deadline: date_type
    reward_note: str
    active: bool
    spent: Decimal
    remaining: Decimal
    pct: Decimal
    days_total: int
    days_elapsed: int
    days_left: int
    on_pace: bool
    completed: bool

    @classmethod
    def from_row(cls, row: SpendGoalProgress) -> "SpendGoalOut":
        return cls(**row.__dict__)


class SpendGoalIn(BaseModel):
    payment_method_id: int
    target_amount: Decimal
    start_date: date_type
    deadline: date_type
    reward_note: str
    active: bool = True


class SpendGoalPatchIn(BaseModel):
    target_amount: Decimal | None = None
    start_date: date_type | None = None
    deadline: date_type | None = None
    reward_note: str | None = None
    active: bool | None = None


@router.get("", response_model=list[SpendGoalOut])
def list_spend_goals_endpoint(
    active_only: bool = False,
    db: Session = Depends(get_db),
) -> list[SpendGoalOut]:
    return [SpendGoalOut.from_row(g) for g in list_goals(db, active_only=active_only)]


@router.post("", response_model=SpendGoalOut, status_code=status.HTTP_201_CREATED)
def create_spend_goal_endpoint(payload: SpendGoalIn, db: Session = Depends(get_db)) -> SpendGoalOut:
    try:
        row = create_goal(db, SpendGoalCreate(**payload.model_dump()))
    except DuplicateSpendGoalError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SpendGoalOut.from_row(row)


@router.patch("/{goal_id}", response_model=SpendGoalOut)
def patch_spend_goal_endpoint(
    goal_id: int,
    patch: SpendGoalPatchIn,
    db: Session = Depends(get_db),
) -> SpendGoalOut:
    try:
        row = update_goal(db, goal_id, SpendGoalPatch(**patch.model_dump(exclude_unset=True)))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SpendGoalOut.from_row(row)


@router.get("/{goal_id}", response_model=SpendGoalOut)
def get_spend_goal_endpoint(goal_id: int, db: Session = Depends(get_db)) -> SpendGoalOut:
    try:
        row = get_goal(db, goal_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return SpendGoalOut.from_row(row)
