"""Income endpoints. Down to a single escape hatch.

`income_entries` is derived from `income_receipts` (see
`app/services/income.py`), and every income source has an automatic writer in
`services/checking_importer.py`, so there is nothing left for a human to type.
POST, PATCH and DELETE on a monthly total are therefore GONE, not guarded
(2026-08-20 — the same call made for exchange rates the same day: automation
replaces manual entry, it does not sit alongside it, or the derived total stops
meaning what it says).

The `/income` page (both UIs) was removed the same day it was added
(2026-08-20, the owner's call): with the total fully derived from receipts,
a read-only ledger view had nothing left to do that the monthly report
doesn't already show, and its only other job — surfacing a wrong receipt for
deletion — is an escape hatch a page is not needed for. `GET /api/income` and
`GET /api/income/receipts` existed only to feed that page; grepped for other
consumers (templates, scripts, services, tests) and found none, so they went
with it.

What remains:
  DELETE /api/income/receipts/{id}    remove one receipt, re-derive its month

Same kind of escape hatch DELETE on /api/exchange-rates is: not a way to enter
a number, the way to remove a wrong one. Note that a deleted Plaid or Pluggy
receipt comes back on the next sync (both re-pull their whole window), so it
is durable only for `statement` and `backfill` receipts. It has no button in
either UI; use the API directly (curl, httpie) to invoke it. A struck-through
receipt's `counts_toward_total=false` flag no longer has a page to render it —
that state now surfaces only in `income_receipts` itself and in the
explanatory line `checking_importer._income_total_note` writes to
`import_logs.notes` for any period still held by a pre-ledger lump.
"""
from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.income import IncomeRow, delete_receipt

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


class RecomputedMonthOut(BaseModel):
    """What the month's total became after a receipt was removed. `entry` is
    null when that receipt was the month's last one and the derived row went
    away with it."""

    entry: IncomeOut | None


@router.delete("/receipts/{receipt_id}", response_model=RecomputedMonthOut)
def delete_receipt_endpoint(
    receipt_id: int, db: Session = Depends(get_db)
) -> RecomputedMonthOut:
    try:
        row = delete_receipt(db, receipt_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RecomputedMonthOut(entry=IncomeOut.from_row(row) if row else None)
