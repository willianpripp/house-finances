"""Payment methods endpoints: list + patch. Patch carries the
`paid_from_payment_method_id` link so /warnings can forecast overdrafts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import PaymentMethod, PaymentMethodType

router = APIRouter(prefix="/api/payment-methods", tags=["payment_methods"])


class PaymentMethodOut(BaseModel):
    id: int
    name: str
    type: str
    currency: str
    active: bool
    paid_from_payment_method_id: int | None = None
    statement_close_day: int | None = None
    due_day: int | None = None
    plaid_item_id: int | None = None
    plaid_account_id: str | None = None


class PaymentMethodPatchIn(BaseModel):
    paid_from_payment_method_id: int | None = None
    clear_paid_from: bool = False  # explicit nullification flag
    statement_close_day: int | None = None
    due_day: int | None = None


def _serialize(m: PaymentMethod) -> PaymentMethodOut:
    return PaymentMethodOut(
        id=m.id,
        name=m.name,
        type=m.type.value,
        currency=m.currency.value,
        active=m.active,
        paid_from_payment_method_id=m.paid_from_payment_method_id,
        statement_close_day=m.statement_close_day,
        due_day=m.due_day,
        plaid_item_id=m.plaid_item_id,
        plaid_account_id=m.plaid_account_id,
    )


@router.get("", response_model=list[PaymentMethodOut])
def list_payment_methods(
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> list[PaymentMethodOut]:
    stmt = select(PaymentMethod).order_by(PaymentMethod.name)
    if active_only:
        stmt = stmt.where(PaymentMethod.active.is_(True))
    methods = db.scalars(stmt).all()
    return [_serialize(m) for m in methods]


@router.patch("/{payment_method_id}", response_model=PaymentMethodOut)
def patch_payment_method(
    payment_method_id: int,
    patch: PaymentMethodPatchIn,
    db: Session = Depends(get_db),
) -> PaymentMethodOut:
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise HTTPException(404, f"Payment method {payment_method_id} not found")

    if patch.clear_paid_from:
        pm.paid_from_payment_method_id = None
    elif patch.paid_from_payment_method_id is not None:
        if pm.type != PaymentMethodType.CREDIT_CARD:
            raise HTTPException(
                400, "paid_from_payment_method_id only applies to credit cards"
            )
        if patch.paid_from_payment_method_id == pm.id:
            raise HTTPException(400, "A card cannot pay itself")
        target = db.get(PaymentMethod, patch.paid_from_payment_method_id)
        if target is None:
            raise HTTPException(
                404, f"Linked payment method {patch.paid_from_payment_method_id} not found"
            )
        if target.type != PaymentMethodType.CHECKING:
            raise HTTPException(
                400, "Linked payment method must be a checking account"
            )
        pm.paid_from_payment_method_id = target.id

    if patch.statement_close_day is not None:
        if not 1 <= patch.statement_close_day <= 31:
            raise HTTPException(400, "statement_close_day must be between 1 and 31")
        pm.statement_close_day = patch.statement_close_day
    if patch.due_day is not None:
        if not 1 <= patch.due_day <= 31:
            raise HTTPException(400, "due_day must be between 1 and 31")
        pm.due_day = patch.due_day

    db.flush()
    db.commit()
    db.refresh(pm)
    return _serialize(pm)
