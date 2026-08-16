"""Transaction list / update / delete.

The currency on a transaction always mirrors its payment method's currency:
`transactions.currency` must equal `payment_methods.currency`, never be
sniffed from the merchant string. When the payment method changes, currency
follows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, extract, func, or_, select
from sqlalchemy.orm import Session

from app.models import CarLoanPayment, Category, Merchant, PaymentMethod, Transaction
from app.models.enums import RecurrenceKind
from app.services.rollover import _next_month, _shift_day


@dataclass(frozen=True)
class TransactionFilters:
    year: int | None = None
    month: int | None = None
    category_id: int | None = None
    payment_method_id: int | None = None
    merchant_id: int | None = None
    currency: str | None = None
    search: str | None = None
    limit: int = 500
    offset: int = 0


@dataclass(frozen=True)
class TransactionRow:
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
    category_icon: str | None
    payment_method_id: int
    payment_method_name: str
    recurrence_kind: str | None = None
    contract_end_date: date_type | None = None


@dataclass(frozen=True)
class TransactionListResult:
    rows: list[TransactionRow]
    total_count: int
    sum_by_currency: dict[str, Decimal]


def _apply_filters(stmt, filters: TransactionFilters):
    conds = []
    if filters.year is not None:
        conds.append(extract("year", Transaction.transaction_date) == filters.year)
    if filters.month is not None:
        conds.append(extract("month", Transaction.transaction_date) == filters.month)
    if filters.category_id is not None:
        conds.append(Transaction.category_id == filters.category_id)
    if filters.payment_method_id is not None:
        conds.append(Transaction.payment_method_id == filters.payment_method_id)
    if filters.merchant_id is not None:
        conds.append(Transaction.merchant_id == filters.merchant_id)
    if filters.currency is not None:
        conds.append(Transaction.currency == filters.currency)
    if filters.search:
        like = f"%{filters.search}%"
        conds.append(or_(
            Transaction.description.ilike(like),
            Merchant.name.ilike(like),
        ))
    if conds:
        stmt = stmt.where(and_(*conds))
    return stmt


def list_transactions(session: Session, filters: TransactionFilters) -> TransactionListResult:
    base = (
        select(Transaction)
        .join(Merchant, Transaction.merchant_id == Merchant.id)
        .join(Category, Transaction.category_id == Category.id)
        .join(PaymentMethod, Transaction.payment_method_id == PaymentMethod.id)
    )
    base = _apply_filters(base, filters)

    listing = (
        base
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(filters.limit)
        .offset(filters.offset)
    )
    txns = session.scalars(listing).unique().all()

    count_stmt = (
        select(func.count(Transaction.id))
        .join(Merchant, Transaction.merchant_id == Merchant.id)
    )
    count_stmt = _apply_filters(count_stmt, filters)
    total = session.scalar(count_stmt) or 0

    sum_stmt = (
        select(Transaction.currency, func.coalesce(func.sum(Transaction.amount), 0))
        .join(Merchant, Transaction.merchant_id == Merchant.id)
        .group_by(Transaction.currency)
    )
    sum_stmt = _apply_filters(sum_stmt, filters)
    sums = {c.value: Decimal(s) for c, s in session.execute(sum_stmt).all()}

    rows = [_row_from_txn(t) for t in txns]

    return TransactionListResult(rows=rows, total_count=total, sum_by_currency=sums)


@dataclass
class TransactionPatch:
    transaction_date: date_type | None = None
    merchant_id: int | None = None
    category_id: int | None = None
    payment_method_id: int | None = None
    amount: Decimal | None = None
    description: str | None = None
    installment_current: int | None = None
    installment_total: int | None = None
    installment_value: Decimal | None = None
    recurrence_kind: str | None = None         # "" sentinel clears the field
    contract_end_date: date_type | None = None  # only meaningful when CONTRACT
    clear_contract_end_date: bool = False


def update_transaction(session: Session, transaction_id: int, patch: TransactionPatch) -> TransactionRow:
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise LookupError(f"Transaction {transaction_id} not found")

    # Capture pre-edit state for installment-series propagation below.
    old_merchant_id = txn.merchant_id
    old_payment_method_id = txn.payment_method_id
    old_installment_total = txn.installment_total
    old_installment_value = txn.installment_value
    old_amount = txn.amount
    old_owner = txn.created_by_user_id

    if patch.merchant_id is not None:
        if session.get(Merchant, patch.merchant_id) is None:
            raise ValueError(f"Merchant {patch.merchant_id} not found")
        txn.merchant_id = patch.merchant_id
    if patch.category_id is not None:
        if session.get(Category, patch.category_id) is None:
            raise ValueError(f"Category {patch.category_id} not found")
        txn.category_id = patch.category_id
    if patch.payment_method_id is not None:
        pm = session.get(PaymentMethod, patch.payment_method_id)
        if pm is None:
            raise ValueError(f"Payment method {patch.payment_method_id} not found")
        txn.payment_method_id = patch.payment_method_id
        txn.currency = pm.currency
    if patch.transaction_date is not None:
        txn.transaction_date = patch.transaction_date
    if patch.amount is not None:
        txn.amount = patch.amount
    if patch.description is not None:
        txn.description = patch.description[:500] if patch.description else None
    if patch.installment_current is not None:
        txn.installment_current = patch.installment_current
    if patch.installment_total is not None:
        txn.installment_total = patch.installment_total
    if patch.installment_value is not None:
        txn.installment_value = patch.installment_value
    if patch.recurrence_kind is not None:
        txn.recurrence_kind = (
            RecurrenceKind(patch.recurrence_kind) if patch.recurrence_kind else None
        )
    if patch.contract_end_date is not None:
        txn.contract_end_date = patch.contract_end_date
    elif patch.clear_contract_end_date:
        txn.contract_end_date = None

    # When the user edits the amount on a row that's part of an installment
    # series, treat the new amount as the per-installment value and propagate
    # it (plus installment_value) to every sibling in the series. Siblings are
    # identified by the PRE-edit (merchant, payment_method, installment_total,
    # installment_value, owner) signature, which is stable across our flows:
    # Split, manual auto-split, and rollover-driven series all stamp those
    # fields consistently. Edits to anything other than amount don't propagate.
    if (
        patch.amount is not None
        and patch.amount != old_amount
        and old_installment_total > 1
        and old_installment_value is not None
    ):
        new_amount = patch.amount
        siblings = session.scalars(
            select(Transaction).where(
                Transaction.id != txn.id,
                Transaction.merchant_id == old_merchant_id,
                Transaction.payment_method_id == old_payment_method_id,
                Transaction.installment_total == old_installment_total,
                Transaction.installment_value == old_installment_value,
                (Transaction.created_by_user_id == old_owner)
                if old_owner is not None
                else Transaction.created_by_user_id.is_(None),
            )
        ).all()
        for sib in siblings:
            sib.amount = new_amount
            sib.installment_value = new_amount
        # Update the edited row's installment_value to match too.
        txn.installment_value = new_amount

    session.flush()
    session.commit()

    return _row_from_txn(txn)


@dataclass
class TransactionCreate:
    transaction_date: date_type
    merchant_id: int
    category_id: int
    payment_method_id: int
    amount: Decimal
    description: str | None = None
    owner_user_id: int | None = None
    installment_current: int = 1
    installment_total: int = 1
    installment_value: Decimal | None = None
    recurrence_kind: str | None = None
    contract_end_date: date_type | None = None


def create_transaction(session: Session, payload: TransactionCreate) -> TransactionRow:
    pm = session.get(PaymentMethod, payload.payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payload.payment_method_id} not found")
    if session.get(Merchant, payload.merchant_id) is None:
        raise ValueError(f"Merchant {payload.merchant_id} not found")
    if session.get(Category, payload.category_id) is None:
        raise ValueError(f"Category {payload.category_id} not found")

    # When the user enters a brand-new installment series (current=1, total>1)
    # treat `amount` as the TOTAL purchase, divide it across the N installments,
    # and seed the future-month follow-ups — same semantics as the Split action.
    # Existing-series rows (current>1) are inserted as-is.
    auto_split = (
        payload.installment_current == 1
        and payload.installment_total > 1
        and payload.installment_total <= MAX_INSTALLMENTS
    )
    rk = RecurrenceKind(payload.recurrence_kind) if payload.recurrence_kind else None

    if auto_split:
        n = payload.installment_total
        per_share = (payload.amount / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        last_share = payload.amount - per_share * (n - 1)
        first = Transaction(
            transaction_date=payload.transaction_date,
            merchant_id=payload.merchant_id,
            category_id=payload.category_id,
            payment_method_id=payload.payment_method_id,
            amount=per_share,
            currency=pm.currency,
            description=(payload.description or "")[:500] or None,
            installment_current=1,
            installment_total=n,
            installment_value=per_share,
            recurrence_kind=rk,
            contract_end_date=payload.contract_end_date,
            created_by_user_id=payload.owner_user_id,
        )
        session.add(first)
        target_year, target_month = payload.transaction_date.year, payload.transaction_date.month
        for i in range(2, n + 1):
            target_year, target_month = _next_month(target_year, target_month)
            target_date = _shift_day(payload.transaction_date, target_year, target_month)
            share = last_share if i == n else per_share
            session.add(Transaction(
                transaction_date=target_date,
                merchant_id=payload.merchant_id,
                category_id=payload.category_id,
                payment_method_id=payload.payment_method_id,
                amount=share,
                currency=pm.currency,
                description=(payload.description or "")[:500] or None,
                installment_current=i,
                installment_total=n,
                installment_value=per_share,
                recurrence_kind=rk,
                contract_end_date=payload.contract_end_date,
                created_by_user_id=payload.owner_user_id,
            ))
        session.flush()
        session.commit()
        return _row_from_txn(first)

    txn = Transaction(
        transaction_date=payload.transaction_date,
        merchant_id=payload.merchant_id,
        category_id=payload.category_id,
        payment_method_id=payload.payment_method_id,
        amount=payload.amount,
        currency=pm.currency,
        description=(payload.description or "")[:500] or None,
        installment_current=payload.installment_current,
        installment_total=payload.installment_total,
        installment_value=payload.installment_value,
        recurrence_kind=rk,
        contract_end_date=payload.contract_end_date,
        created_by_user_id=payload.owner_user_id,
    )
    session.add(txn)
    session.flush()

    # EXTRA_PRINCIPAL side effect: mirror the principal into car_loan_payments
    # so the car-loan debt walks down without manual entry on /debts.
    if rk == RecurrenceKind.EXTRA_PRINCIPAL:
        _apply_extra_principal(session, txn)

    session.commit()
    return _row_from_txn(txn)


def _apply_extra_principal(session: Session, txn: Transaction) -> None:
    """Add a CarLoanPayment row for an EXTRA_PRINCIPAL transaction.

    The new row's `new_balance` = (latest balance on or before this date)
    minus `txn.amount`. interest_paid is always zero (extra principal).
    """
    latest = session.scalar(
        select(CarLoanPayment)
        .where(CarLoanPayment.posting_date <= txn.transaction_date)
        .order_by(CarLoanPayment.posting_date.desc(), CarLoanPayment.id.desc())
        .limit(1)
    )
    prev_balance = Decimal(latest.new_balance) if latest else Decimal("0")
    new_balance = prev_balance - Decimal(txn.amount)
    session.add(CarLoanPayment(
        posting_date=txn.transaction_date,
        payment_amount=Decimal(txn.amount),
        principal_paid=Decimal(txn.amount),
        interest_paid=Decimal("0"),
        new_balance=new_balance,
    ))
    session.flush()


def delete_transaction(session: Session, transaction_id: int) -> None:
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise LookupError(f"Transaction {transaction_id} not found")
    # Mirror the EXTRA_PRINCIPAL side effect on delete: remove the
    # car_loan_payments shadow row keyed by (posting_date, payment_amount,
    # principal-only). Matches the row created in _apply_extra_principal.
    if (
        txn.recurrence_kind is not None
        and txn.recurrence_kind.value == RecurrenceKind.EXTRA_PRINCIPAL.value
    ):
        shadow = session.scalar(
            select(CarLoanPayment)
            .where(
                CarLoanPayment.posting_date == txn.transaction_date,
                CarLoanPayment.payment_amount == Decimal(txn.amount),
                CarLoanPayment.principal_paid == Decimal(txn.amount),
                CarLoanPayment.interest_paid == Decimal("0"),
            )
            .order_by(CarLoanPayment.id.desc())
            .limit(1)
        )
        if shadow is not None:
            session.delete(shadow)
    session.delete(txn)
    session.commit()


MAX_INSTALLMENTS = 36


def _row_from_txn(txn: Transaction) -> TransactionRow:
    return TransactionRow(
        id=txn.id,
        transaction_date=txn.transaction_date,
        amount=txn.amount,
        currency=txn.currency.value,
        description=txn.description,
        installment_current=txn.installment_current,
        installment_total=txn.installment_total,
        installment_value=txn.installment_value,
        merchant_id=txn.merchant_id,
        merchant_name=txn.merchant.name,
        category_id=txn.category_id,
        category_name=txn.category.name,
        category_color=txn.category.color,
        category_icon=txn.category.icon,
        payment_method_id=txn.payment_method_id,
        payment_method_name=txn.payment_method.name,
        recurrence_kind=txn.recurrence_kind.value if txn.recurrence_kind is not None else None,
        contract_end_date=txn.contract_end_date,
    )


def split_transaction(
    session: Session, transaction_id: int, installments: int
) -> list[TransactionRow]:
    """Split a single transaction into N future-month installments.

    The original keeps its date and becomes 1/N with amount = total / N. The
    remaining N-1 transactions land on the same day of subsequent months
    (clamped to month-end where needed). Rounding remainder lands on the
    last installment so the sum exactly equals the original total.
    """
    if installments < 2:
        raise ValueError("installments must be at least 2")
    if installments > MAX_INSTALLMENTS:
        raise ValueError(f"installments must be at most {MAX_INSTALLMENTS}")

    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise LookupError(f"Transaction {transaction_id} not found")
    if txn.installment_total != 1:
        raise ValueError(
            f"Transaction {transaction_id} is already part of an installment series "
            f"({txn.installment_current}/{txn.installment_total})"
        )

    total = txn.amount
    per_share = (total / installments).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    last_share = total - per_share * (installments - 1)

    txn.installment_current = 1
    txn.installment_total = installments
    txn.installment_value = per_share
    txn.amount = per_share

    new_txns: list[Transaction] = []
    target_year, target_month = txn.transaction_date.year, txn.transaction_date.month

    for i in range(2, installments + 1):
        target_year, target_month = _next_month(target_year, target_month)
        target_date = _shift_day(txn.transaction_date, target_year, target_month)
        share = last_share if i == installments else per_share

        new_txn = Transaction(
            transaction_date=target_date,
            merchant_id=txn.merchant_id,
            category_id=txn.category_id,
            payment_method_id=txn.payment_method_id,
            amount=share,
            currency=txn.currency,
            description=txn.description,
            installment_current=i,
            installment_total=installments,
            installment_value=per_share,
            created_by_user_id=txn.created_by_user_id,
        )
        session.add(new_txn)
        new_txns.append(new_txn)

    session.flush()
    session.commit()

    return [_row_from_txn(t) for t in [txn, *new_txns]]
