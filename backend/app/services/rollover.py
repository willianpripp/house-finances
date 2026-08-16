"""Fixed-expense rollover, narrowed to INSTALLMENT + Taxes only.

History: rollover was the v1-era mechanism for "copy every FIXED row from
month M to M+1". With the checking importer in place, most FIXED rows can
be auto-propagated by [services/recurrence.py](recurrence.py) when the
real debit lands — the importer copies `recurrence_kind`,
`contract_end_date`, and `installment_current+1` from the most recent
prior row.

Rollover now covers only the cases where importer-side propagation
isn't sufficient:

1. **INSTALLMENT series**: the user wants to see upcoming installments
   proactively (car loan parcela #19/72 → #20/72 → …), even before the
   debit lands. Preview also lets them shift dates or cancel a series
   that's been paid off early.
2. **US withholding placeholders**: the salary import needs same-month
   Federal/State Withholding rows to rebalance against the actual net deposit.
   Without pre-rolled placeholders, the salary reconciliation has nothing
   to adjust.
3. **INDEFINITE Taxes fees**: a fixed monthly fee filed under Taxes, e.g. an
   accountant's retainer charged on a card. Same amount, same day, every
   month, so it dedupes against the real charge. An earlier narrowing keyed
   the Taxes branch on the merchant *name* and silently dropped these rows
   (a month came up empty); the branch is keyed on
   `recurrence_kind == INDEFINITE` instead.

The variable tax *payments* (BR Simples Nacional, DARF) still do NOT roll:
they carry no recurrence_kind, they arrive via import, and their amounts
vary month to month, so a rolled placeholder that misses the real debit by a
few units of currency duplicates instead of dedups.

Everything else (CONTRACT Rent, INDEFINITE streaming/gym subscriptions,
EXTRA_PRINCIPAL, variable tax payments) is filtered out of the rollover
preview — those land via the importer's history lookup. This kills the class
of "stale placeholder" bugs where a rolled row lingers next to the real
charge it failed to match.

Design notes:
- Source = explicit (year, month). Target = always the next calendar month.
- Each item carries `already_in_target=True` when a transaction in the target
  month already matches the (merchant, payment_method, amount, owner) signature
  of the candidate. The UI defaults those rows to unchecked.
- Suggested target date = same day of month, clamped to target month's last
  day (Jan 31 → Feb 28).
- Installments increment `installment_current` and preserve total + value.
  Final-installment sources carry `installment_complete=True` and default
  unchecked — rolling them forward would create a 7/6, which is invalid.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import and_, extract, or_, select
from sqlalchemy.orm import Session

from app.models import Category, CategoryType, Merchant, Transaction
from app.models.enums import RecurrenceKind


class RolloverError(ValueError):
    """Raised when a commit_rollover input is invalid."""


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _month_bounds(year: int, month: int) -> tuple[date_type, date_type]:
    last = calendar.monthrange(year, month)[1]
    return date_type(year, month, 1), date_type(year, month, last)


def _shift_day(source: date_type, target_year: int, target_month: int) -> date_type:
    last = calendar.monthrange(target_year, target_month)[1]
    return date_type(target_year, target_month, min(source.day, last))


@dataclass
class RolloverItem:
    source_transaction_id: int
    merchant_id: int
    merchant_name: str
    payment_method_id: int
    payment_method_name: str
    category_id: int
    category_name: str
    category_color: str
    category_icon: str | None
    owner_id: int | None
    owner_name: str | None
    amount: Decimal
    currency: str
    description: str | None
    source_date: date_type
    suggested_target_date: date_type
    already_in_target: bool
    installment_current: int
    installment_total: int
    installment_value: Decimal | None
    installment_complete: bool
    recurrence_kind: str | None
    contract_end_date: date_type | None
    contract_complete: bool  # CONTRACT whose end date is before target month


@dataclass
class RolloverPreview:
    source_year: int
    source_month: int
    target_year: int
    target_month: int
    items: list[RolloverItem]


TAXES_CATEGORY_NAME = "Taxes"  # placeholders the salary import rebalances against
# Two kinds of Taxes row roll forward:
#   1. the US withholding rows (Federal/State), which the salary reconciliation adjusts
#      against the real net deposit;
#   2. INDEFINITE Taxes rows — a fixed monthly fee that happens to be filed under
#      Taxes (e.g. an accountant's retainer on a card). Same amount, same day,
#      every month, so it dedupes cleanly against the real charge when the
#      statement lands.
# The variable tax *payments* (BR Simples Nacional, DARF) stay out: they carry no
# recurrence_kind, they arrive via import, and their amounts move month to month,
# so a rolled placeholder that misses the real debit duplicates instead of
# dedupes.
WITHHOLDING_MERCHANT_FRAGMENTS = ("Withholding",)


def _is_withholding(merchant_name: str | None) -> bool:
    return bool(merchant_name) and any(
        frag in merchant_name for frag in WITHHOLDING_MERCHANT_FRAGMENTS
    )


def preview_rollover(session: Session, year: int, month: int) -> RolloverPreview:
    target_year, target_month = _next_month(year, month)
    target_first, target_last = _month_bounds(target_year, target_month)

    # Rollover scope is narrowed to INSTALLMENT series + Taxes
    # placeholders. CONTRACT / INDEFINITE / EXTRA_PRINCIPAL are propagated
    # by the importer via services/recurrence.py when the real debit lands.
    sources = session.scalars(
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .join(Merchant, Merchant.id == Transaction.merchant_id)
        .where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
            Category.type == CategoryType.FIXED,
            or_(
                Transaction.installment_total > 1,
                and_(
                    Category.name == TAXES_CATEGORY_NAME,
                    or_(
                        Transaction.recurrence_kind == RecurrenceKind.INDEFINITE,
                        *[
                            Merchant.name.ilike(f"%{frag}%")
                            for frag in WITHHOLDING_MERCHANT_FRAGMENTS
                        ],
                    ),
                ),
            ),
        )
        .order_by(Transaction.transaction_date, Transaction.id)
    ).all()

    existing = session.scalars(
        select(Transaction).where(
            Transaction.transaction_date >= target_first,
            Transaction.transaction_date <= target_last,
        )
    ).all()
    existing_keys = {
        (t.merchant_id, t.payment_method_id, Decimal(t.amount), t.created_by_user_id)
        for t in existing
    }

    items: list[RolloverItem] = []
    for t in sources:
        sig = (t.merchant_id, t.payment_method_id, Decimal(t.amount), t.created_by_user_id)
        installment_complete = t.installment_total > 1 and t.installment_current >= t.installment_total
        rk_value = t.recurrence_kind.value if t.recurrence_kind is not None else None
        contract_complete = (
            rk_value == RecurrenceKind.CONTRACT.value
            and t.contract_end_date is not None
            and t.contract_end_date < target_first
        )
        items.append(
            RolloverItem(
                source_transaction_id=t.id,
                merchant_id=t.merchant_id,
                merchant_name=t.merchant.name,
                payment_method_id=t.payment_method_id,
                payment_method_name=t.payment_method.name,
                category_id=t.category_id,
                category_name=t.category.name,
                category_color=t.category.color,
                category_icon=t.category.icon,
                owner_id=t.created_by_user_id,
                owner_name=t.created_by.name if t.created_by else None,
                amount=Decimal(t.amount),
                currency=t.currency.value,
                description=t.description,
                source_date=t.transaction_date,
                suggested_target_date=_shift_day(t.transaction_date, target_year, target_month),
                already_in_target=sig in existing_keys,
                installment_current=t.installment_current,
                installment_total=t.installment_total,
                installment_value=Decimal(t.installment_value) if t.installment_value is not None else None,
                installment_complete=installment_complete,
                recurrence_kind=rk_value,
                contract_end_date=t.contract_end_date,
                contract_complete=contract_complete,
            )
        )
    return RolloverPreview(
        source_year=year,
        source_month=month,
        target_year=target_year,
        target_month=target_month,
        items=items,
    )


@dataclass
class CommitItem:
    source_transaction_id: int
    target_date: date_type
    amount: Decimal


def commit_rollover(
    session: Session, year: int, month: int, items: list[CommitItem]
) -> list[int]:
    """Persist the user-selected items as new transactions in the target month.
    Returns the list of new transaction IDs."""
    target_year, target_month = _next_month(year, month)
    target_first, target_last = _month_bounds(target_year, target_month)

    if not items:
        return []

    src_ids = [c.source_transaction_id for c in items]
    sources = {
        t.id: t
        for t in session.scalars(select(Transaction).where(Transaction.id.in_(src_ids))).all()
    }

    new_ids: list[int] = []
    for c in items:
        src = sources.get(c.source_transaction_id)
        if src is None:
            raise RolloverError(f"Source transaction {c.source_transaction_id} not found.")
        # Server-side guard: rollover scope = INSTALLMENT or Taxes.
        # CONTRACT / INDEFINITE / EXTRA_PRINCIPAL are propagated via the
        # importer; rolling them here would re-introduce the duplicate-write
        # path (importer creates real row, rollover creates placeholder).
        in_scope = src.installment_total > 1 or (
            src.category.name == TAXES_CATEGORY_NAME
            and (
                _is_withholding(src.merchant.name)
                or src.recurrence_kind == RecurrenceKind.INDEFINITE
            )
        )
        if not in_scope:
            raise RolloverError(
                f"Source transaction {src.id} is out of rollover scope (only "
                f"INSTALLMENT series, US withholding placeholders and INDEFINITE "
                f"Taxes fees roll forward; variable BR tax payments and "
                f"CONTRACT/INDEFINITE bills come from importer history)."
            )
        if not (target_first <= c.target_date <= target_last):
            raise RolloverError(
                f"Target date {c.target_date} is outside {target_year}-{target_month:02d}."
            )
        if c.amount <= 0:
            raise RolloverError(f"Amount must be positive (got {c.amount}).")
        if src.installment_total > 1 and src.installment_current >= src.installment_total:
            raise RolloverError(
                f"Source transaction {src.id} is the final installment "
                f"({src.installment_current}/{src.installment_total}); the series is over."
            )
        if (
            src.recurrence_kind is not None
            and src.recurrence_kind.value == RecurrenceKind.CONTRACT.value
            and src.contract_end_date is not None
            and c.target_date > src.contract_end_date
        ):
            raise RolloverError(
                f"Source transaction {src.id} is a CONTRACT ending "
                f"{src.contract_end_date}; target {c.target_date} is past the contract end."
            )
        if src.installment_total > 1:
            new_current = src.installment_current + 1
            new_total = src.installment_total
            new_value = src.installment_value
        else:
            new_current = 1
            new_total = 1
            new_value = None
        new_tx = Transaction(
            transaction_date=c.target_date,
            merchant_id=src.merchant_id,
            category_id=src.category_id,
            payment_method_id=src.payment_method_id,
            amount=c.amount,
            currency=src.currency,
            description=src.description,
            installment_current=new_current,
            installment_total=new_total,
            installment_value=new_value,
            recurrence_kind=src.recurrence_kind,
            contract_end_date=src.contract_end_date,
            created_by_user_id=src.created_by_user_id,
        )
        session.add(new_tx)
        session.flush()
        new_ids.append(new_tx.id)
    session.commit()
    return new_ids
