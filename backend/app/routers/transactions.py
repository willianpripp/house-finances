"""Transaction list / update / delete endpoints."""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Transaction
from app.services.provider_guard import (
    guard_transaction_create,
    guard_transaction_patch,
    guard_transaction_split,
)
from app.services.transactions import (
    MAX_INSTALLMENTS,
    TransactionCreate,
    TransactionFilters,
    TransactionPatch,
    TransactionRow,
    create_transaction,
    delete_transaction,
    list_transactions,
    split_transaction,
    update_transaction,
)

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


class TransactionOut(BaseModel):
    id: int
    transaction_date: date_type
    amount: Decimal
    currency: str
    description: str | None
    installment_current: int
    installment_total: int
    installment_value: Decimal | None
    merchant_id: int
    merchant_name: str
    category_id: int
    category_name: str
    category_color: str
    category_icon: str | None = None
    payment_method_id: int
    payment_method_name: str
    recurrence_kind: str | None = None
    contract_end_date: date_type | None = None
    provider: str | None = None

    @classmethod
    def from_row(cls, row: TransactionRow) -> "TransactionOut":
        return cls(**row.__dict__)


class TransactionListOut(BaseModel):
    total_count: int
    sum_by_currency: dict[str, Decimal]
    transactions: list[TransactionOut]


class TransactionPatchIn(BaseModel):
    transaction_date: date_type | None = None
    merchant_id: int | None = None
    merchant_name: str | None = None  # alternative to merchant_id: create-or-get by name
    category_id: int | None = None
    payment_method_id: int | None = None
    amount: Decimal | None = None
    description: str | None = None
    installment_current: int | None = Field(default=None, ge=1)
    installment_total: int | None = Field(default=None, ge=1)
    installment_value: Decimal | None = None
    recurrence_kind: str | None = None
    contract_end_date: date_type | None = None
    clear_contract_end_date: bool = False


@router.get("", response_model=TransactionListOut)
def list_endpoint(
    year: int | None = None,
    month: int | None = Query(default=None, ge=1, le=12),
    category_id: int | None = None,
    payment_method_id: int | None = None,
    merchant_id: int | None = None,
    currency: str | None = None,
    search: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TransactionListOut:
    filters = TransactionFilters(
        year=year,
        month=month,
        category_id=category_id,
        payment_method_id=payment_method_id,
        merchant_id=merchant_id,
        currency=currency,
        search=search,
        limit=limit,
        offset=offset,
    )
    result = list_transactions(db, filters)
    return TransactionListOut(
        total_count=result.total_count,
        sum_by_currency=result.sum_by_currency,
        transactions=[TransactionOut.from_row(r) for r in result.rows],
    )


@router.patch("/{transaction_id}", response_model=TransactionOut)
def patch_endpoint(
    transaction_id: int,
    patch: TransactionPatchIn,
    db: Session = Depends(get_db),
) -> TransactionOut:
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(
            status_code=404, detail=f"Transaction {transaction_id} not found"
        )
    changes = patch.model_dump(exclude_unset=True)
    guard_transaction_patch(db, txn, changes)
    try:
        row = update_transaction(
            db,
            transaction_id,
            TransactionPatch(**changes),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TransactionOut.from_row(row)


class TransactionCreateIn(BaseModel):
    transaction_date: date_type
    merchant_id: int | None = None
    merchant_name: str | None = None  # alternative to merchant_id: create-or-get by name
    category_id: int
    payment_method_id: int
    amount: Decimal
    description: str | None = None
    owner_user_id: int | None = None
    installment_current: int = Field(default=1, ge=1)
    installment_total: int = Field(default=1, ge=1)
    installment_value: Decimal | None = None
    recurrence_kind: str | None = None
    contract_end_date: date_type | None = None


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(
    body: TransactionCreateIn,
    db: Session = Depends(get_db),
) -> TransactionOut:
    guard_transaction_create(db, body.payment_method_id)
    try:
        row = create_transaction(db, TransactionCreate(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return TransactionOut.from_row(row)


class SplitIn(BaseModel):
    installments: int = Field(ge=2, le=MAX_INSTALLMENTS)


class SplitOut(BaseModel):
    transactions: list[TransactionOut]


@router.post("/{transaction_id}/split", response_model=SplitOut)
def split_endpoint(
    transaction_id: int,
    body: SplitIn,
    db: Session = Depends(get_db),
) -> SplitOut:
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(
            status_code=404, detail=f"Transaction {transaction_id} not found"
        )
    guard_transaction_split(txn)
    try:
        rows = split_transaction(db, transaction_id, body.installments)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SplitOut(transactions=[TransactionOut.from_row(r) for r in rows])


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_endpoint(
    transaction_id: int,
    db: Session = Depends(get_db),
) -> Response:
    try:
        delete_transaction(db, transaction_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
