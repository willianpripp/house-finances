"""'A Receber' — informal debts between us and family/friends, both ways.

OWED_TO_ME: money owed for charges put on our cards. The charge itself stays
in the ledger as normal spending; these rows track who owes what until they
pay it back. Settling posts the payback to the ledger as a NEGATIVE
transaction (a refund) that nets that spending back down to the household's
own share. Deliberately not an income entry: that would inflate income and
leave spending overstated by the same amount in every report of that month.
The full argument, and the account/currency rules, are in
`app/services/receivables.py`.

I_OWE: someone else paid and we owe them our share. Nothing is in the ledger
while the debt is open (the money never left our accounts), so settling posts
the real expense, dated the day we paid the person back.

Either direction, settling first looks for an already-imported transaction
that plausibly IS the payback and links to it rather than writing a duplicate.
The settle response says which happened (`created` / `linked` / `none`), and
un-settling deletes a row we created while only unlinking an imported one.

A split across N people is N rows sharing a group_id (the split math is done
client-side and posted as a list of shares). Each share settles on its own and
gets its own ledger entry.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Currency, Person, Receivable, ReceivableDirection
from app.services import receivables as receivables_service
from app.services.auth import current_user_id

router = APIRouter(prefix="/api/receivables", tags=["receivables"])


# ---- Schemas ----
class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    relation: str | None = Field(default=None, max_length=40)


class PersonOut(BaseModel):
    id: int
    name: str
    relation: str | None = None
    active: bool


class SplitShare(BaseModel):
    person_id: int
    amount: Decimal = Field(gt=0)


class ReceivableCreateIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    store: str | None = Field(default=None, max_length=120)
    payment_method_id: int | None = None
    charge_date: date_type
    currency: Currency = Currency.USD
    direction: ReceivableDirection = ReceivableDirection.OWED_TO_ME
    shares: list[SplitShare] = Field(min_length=1)


class ReceivableOut(BaseModel):
    id: int
    person_id: int
    person_name: str
    group_id: str | None
    direction: ReceivableDirection
    amount: Decimal
    currency: str
    description: str
    store: str | None
    payment_method_id: int | None
    payment_method_name: str | None
    charge_date: date_type
    settled: bool
    settled_at: datetime | None
    settled_transaction_id: int | None = None
    settled_transaction_autocreated: bool = False


class SettleIn(BaseModel):
    settled: bool = True
    # The day the money actually moved. Defaults to today, which is the common
    # case (marking it paid as it happens), but a payback that arrived last
    # month has to land in last month's report, so it is settable.
    settled_on: date_type | None = None


class LedgerActionOut(BaseModel):
    """What the settle/unsettle call did to the ledger.

    `action` is one of created / linked / deleted / unlinked / none. The UI
    shows it verbatim so the user can tell a fresh entry from a link to a bank
    line they already imported."""

    action: str
    transaction_id: int | None = None
    transaction_date: date_type | None = None
    amount: Decimal | None = None
    currency: str | None = None
    account_name: str | None = None
    category_name: str | None = None
    reason: str | None = None


class SettleOut(BaseModel):
    receivable: ReceivableOut
    ledger: LedgerActionOut


class PersonSummaryOut(BaseModel):
    person_id: int
    person_name: str
    relation: str | None
    currency: str
    owed_to_me: Decimal
    owed_to_me_count: int
    i_owe: Decimal
    i_owe_count: int
    # Positive = they owe us on balance, negative = we owe them.
    net_amount: Decimal
    open_count: int


# ---- People ----
@router.get("/people", response_model=list[PersonOut])
def list_people(db: Session = Depends(get_db)) -> list[PersonOut]:
    rows = db.scalars(
        select(Person).where(Person.active.is_(True)).order_by(Person.name)
    ).all()
    return [PersonOut(id=p.id, name=p.name, relation=p.relation, active=p.active) for p in rows]


@router.post("/people", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
def create_person(payload: PersonIn, db: Session = Depends(get_db)) -> PersonOut:
    exists = db.scalar(select(Person).where(func.lower(Person.name) == payload.name.lower()))
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"Person '{payload.name}' already exists")
    p = Person(name=payload.name.strip(), relation=payload.relation)
    db.add(p)
    db.commit()
    db.refresh(p)
    return PersonOut(id=p.id, name=p.name, relation=p.relation, active=p.active)


# ---- Receivables ----
def _to_out(r: Receivable) -> ReceivableOut:
    return ReceivableOut(
        id=r.id,
        person_id=r.person_id,
        person_name=r.person.name,
        group_id=r.group_id,
        direction=r.direction,
        amount=r.amount,
        currency=r.currency.value,
        description=r.description,
        store=r.store,
        payment_method_id=r.payment_method_id,
        payment_method_name=r.payment_method.name if r.payment_method else None,
        charge_date=r.charge_date,
        settled=r.settled,
        settled_at=r.settled_at,
        settled_transaction_id=r.settled_transaction_id,
        settled_transaction_autocreated=r.settled_transaction_autocreated,
    )


@router.get("", response_model=list[ReceivableOut])
def list_receivables(
    settled: bool | None = None,
    direction: ReceivableDirection | None = None,
    db: Session = Depends(get_db),
) -> list[ReceivableOut]:
    stmt = select(Receivable)
    if settled is not None:
        stmt = stmt.where(Receivable.settled.is_(settled))
    if direction is not None:
        stmt = stmt.where(Receivable.direction == direction)
    stmt = stmt.order_by(Receivable.settled, Receivable.charge_date.desc(), Receivable.id.desc())
    return [_to_out(r) for r in db.scalars(stmt).all()]


@router.post("", response_model=list[ReceivableOut], status_code=status.HTTP_201_CREATED)
def create_receivable(payload: ReceivableCreateIn, db: Session = Depends(get_db)) -> list[ReceivableOut]:
    """One charge → one row per person on the other side of it. Multiple
    shares share a group_id so the UI can show them as a single split.

    For I_OWE each share is what *we* owe that person, and `payment_method_id`
    is dropped: the charge was on their card, not ours."""
    person_ids = {s.person_id for s in payload.shares}
    found = set(db.scalars(select(Person.id).where(Person.id.in_(person_ids))).all())
    missing = person_ids - found
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown person id(s): {sorted(missing)}")

    group_id = uuid.uuid4().hex if len(payload.shares) > 1 else None
    is_owed_to_me = payload.direction is ReceivableDirection.OWED_TO_ME
    created: list[Receivable] = []
    for s in payload.shares:
        r = Receivable(
            person_id=s.person_id,
            group_id=group_id,
            direction=payload.direction,
            amount=s.amount,
            currency=payload.currency,
            description=payload.description.strip(),
            store=payload.store,
            payment_method_id=payload.payment_method_id if is_owed_to_me else None,
            charge_date=payload.charge_date,
        )
        db.add(r)
        created.append(r)
    db.commit()
    for r in created:
        db.refresh(r)
    return [_to_out(r) for r in created]


@router.patch("/{receivable_id}/settle", response_model=SettleOut)
def settle_receivable(
    receivable_id: int,
    payload: SettleIn,
    db: Session = Depends(get_db),
    user_id: int = Depends(current_user_id),
) -> SettleOut:
    """Mark paid (or reopen) AND move the ledger with it.

    Settling posts the money that changed hands; reopening reverses that post.
    The `ledger` half of the response is what the UI reports back, so the user
    never has to go to /transactions to find out whether a row was created."""
    r = db.get(Receivable, receivable_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Receivable not found")

    if payload.settled:
        settled_on = payload.settled_on or date_type.today()
        action = receivables_service.post_settlement(db, r, settled_on, user_id)
        r.settled = True
        r.settled_at = datetime.now()
    else:
        action = receivables_service.reverse_settlement(db, r)
        r.settled = False
        r.settled_at = None

    db.commit()
    db.refresh(r)
    return SettleOut(
        receivable=_to_out(r), ledger=LedgerActionOut(**asdict(action))
    )


@router.delete("/{receivable_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_receivable(receivable_id: int, db: Session = Depends(get_db)) -> Response:
    """Deleting a settled receivable also reverses its ledger post, on the same
    terms as un-settling: a row we created goes, an imported one stays. Leaving
    an auto-created refund behind with nothing pointing at it would silently
    skew the month it sits in."""
    r = db.get(Receivable, receivable_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Receivable not found")
    receivables_service.reverse_settlement(db, r)
    db.delete(r)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/summary", response_model=list[PersonSummaryOut])
def summary_by_person(db: Session = Depends(get_db)) -> list[PersonSummaryOut]:
    """Open balance per person AND currency, both directions (unsettled only).

    `net_amount` nets the two sides so a person we both owe and are owed by
    shows a single figure: positive = they owe us. USD and BRL never mix in
    one figure — a person with debts in both currencies gets two rows."""
    owed = case((Receivable.direction == ReceivableDirection.OWED_TO_ME, Receivable.amount), else_=0)
    mine = case((Receivable.direction == ReceivableDirection.I_OWE, Receivable.amount), else_=0)
    owed_count = case((Receivable.direction == ReceivableDirection.OWED_TO_ME, 1), else_=0)
    mine_count = case((Receivable.direction == ReceivableDirection.I_OWE, 1), else_=0)

    stmt = (
        select(
            Person.id,
            Person.name,
            Person.relation,
            Receivable.currency,
            func.coalesce(func.sum(owed), 0).label("owed_to_me"),
            func.coalesce(func.sum(owed_count), 0).label("owed_to_me_count"),
            func.coalesce(func.sum(mine), 0).label("i_owe"),
            func.coalesce(func.sum(mine_count), 0).label("i_owe_count"),
            func.count(Receivable.id).label("open_count"),
        )
        .join(Receivable, (Receivable.person_id == Person.id) & (Receivable.settled.is_(False)))
        .group_by(Person.id, Person.name, Person.relation, Receivable.currency)
        .having(func.count(Receivable.id) > 0)
        .order_by(func.sum(owed - mine).desc(), Receivable.currency)
    )
    return [
        PersonSummaryOut(
            person_id=row.id,
            person_name=row.name,
            relation=row.relation,
            currency=row.currency.value if isinstance(row.currency, Currency) else row.currency,
            owed_to_me=Decimal(row.owed_to_me),
            owed_to_me_count=row.owed_to_me_count,
            i_owe=Decimal(row.i_owe),
            i_owe_count=row.i_owe_count,
            net_amount=Decimal(row.owed_to_me) - Decimal(row.i_owe),
            open_count=row.open_count,
        )
        for row in db.execute(stmt).all()
    ]
