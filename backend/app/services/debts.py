"""Debts: credit card balances + car loan payments.

Card balances mirror the savings-snapshot pattern: each balance update is a
new row, latest-per-card is computed on read. Imports also append rows here
when a statement contains a PAYMENT line — see services/importer.py.

Car loan rows are payment events with running new_balance, mirroring the v1
Excel sheet structure.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models import (
    CarLoanPayment,
    CreditCardBalance,
    Currency,
    ExchangeRate,
    PaymentMethod,
    Transaction,
)


# ---------- credit card balances ----------

@dataclass(frozen=True)
class CardBalanceRow:
    id: int
    payment_method_id: int
    payment_method_name: str
    currency: str
    balance: Decimal
    statement: Decimal | None
    due_day: int | None
    recorded_at: datetime
    # Heatmap + MoM (only populated by current_card_balances()).
    usd_equivalent: Decimal | None = None
    prev_balance: Decimal | None = None
    mom_pct: Decimal | None = None
    # When `balance` was derived (recorded balance + post-balance
    # transactions in the same card), these expose the raw row and the
    # transaction delta that went on top. Both are None for rows from the
    # full balance-history list (list_card_balances), which keeps raw values.
    recorded_balance: Decimal | None = None
    post_balance_delta: Decimal | None = None


@dataclass(frozen=True)
class CardHeatmapBounds:
    min_usd: Decimal
    max_usd: Decimal


@dataclass(frozen=True)
class CardBalanceListResult:
    rows: list[CardBalanceRow]
    sum_by_currency: dict[str, Decimal] = field(default_factory=dict)
    heatmap_bounds: CardHeatmapBounds | None = None


def _card_row(balance: CreditCardBalance) -> CardBalanceRow:
    return CardBalanceRow(
        id=balance.id,
        payment_method_id=balance.payment_method_id,
        payment_method_name=balance.payment_method.name,
        currency=balance.payment_method.currency.value,
        balance=balance.balance,
        statement=balance.statement,
        # due_day now lives on payment_methods.
        # CardBalanceRow exposes it for backward-compat with the UI.
        due_day=balance.payment_method.due_day,
        recorded_at=balance.recorded_at,
    )


def list_card_balances(
    session: Session,
    *,
    payment_method_id: int | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 1000,
) -> CardBalanceListResult:
    stmt = (
        select(CreditCardBalance)
        .order_by(desc(CreditCardBalance.recorded_at), CreditCardBalance.id)
        .limit(limit)
    )
    if payment_method_id is not None:
        stmt = stmt.where(CreditCardBalance.payment_method_id == payment_method_id)
    if from_date is not None:
        stmt = stmt.where(CreditCardBalance.recorded_at >= from_date)
    if to_date is not None:
        stmt = stmt.where(CreditCardBalance.recorded_at <= to_date)
    items = session.scalars(stmt).all()
    rows = [_card_row(b) for b in items]
    sums: dict[str, Decimal] = {}
    for r in rows:
        sums[r.currency] = sums.get(r.currency, Decimal("0")) + r.balance
    return CardBalanceListResult(rows=rows, sum_by_currency=sums)


def _latest_effective_rate(session: Session) -> Decimal:
    rate = session.scalar(
        select(ExchangeRate).order_by(desc(ExchangeRate.rate_date)).limit(1)
    )
    return Decimal(rate.effective) if rate else Decimal("1")


def _prev_month_window(today: date_type) -> tuple[datetime, datetime]:
    if today.month == 1:
        prev_year, prev_month = today.year - 1, 12
    else:
        prev_year, prev_month = today.year, today.month - 1
    last_day = calendar.monthrange(prev_year, prev_month)[1]
    start = datetime.combine(date_type(prev_year, prev_month, 1), datetime.min.time())
    end = datetime.combine(date_type(prev_year, prev_month, last_day), datetime.max.time())
    return start, end


def _latest_prev_month_card_balance(
    session: Session, payment_method_id: int, today: date_type | None = None
) -> Decimal | None:
    if today is None:
        today = date_type.today()
    start, end = _prev_month_window(today)
    row = session.scalar(
        select(CreditCardBalance)
        .where(
            CreditCardBalance.payment_method_id == payment_method_id,
            CreditCardBalance.recorded_at >= start,
            CreditCardBalance.recorded_at <= end,
        )
        .order_by(desc(CreditCardBalance.recorded_at))
        .limit(1)
    )
    return Decimal(row.balance) if row else None


def latest_card_balance_live(
    session: Session, payment_method_id: int, *, today: date_type | None = None
) -> tuple[Decimal, "CreditCardBalance | None"]:
    """Return the live balance for one card: latest recorded row plus
    post-balance transaction delta. The second element is the underlying
    row (or None when the card has no balance history). Other modules use
    this so /warnings and overdraft stay in sync with /debts."""
    today = today or date_type.today()
    row = session.scalar(
        select(CreditCardBalance)
        .where(CreditCardBalance.payment_method_id == payment_method_id)
        .order_by(desc(CreditCardBalance.recorded_at))
        .limit(1)
    )
    if row is None:
        return Decimal("0"), None
    delta = _post_balance_delta(
        session, payment_method_id=payment_method_id, after=row.recorded_at, today=today
    )
    return Decimal(row.balance) + delta, row


def _post_balance_delta(
    session: Session, *, payment_method_id: int, after: datetime, today: date_type
) -> Decimal:
    """Sum of card transactions strictly after the latest balance row's date
    and not in the future. Lets `current_card_balances` derive a live number
    instead of returning the stale snapshot.

    - Future-dated FIXED projections (a monthly bill lined up for later in
      the month, etc.) are excluded so they don't inflate the current balance.
    - `transactions.amount` carries the natural sign: purchases positive,
      refunds/credits negative. CC autopays are NOT stored as transactions
      (they only reduce the balance row), so they don't double-count here.
    """
    delta = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), Decimal("0")))
        .where(
            Transaction.payment_method_id == payment_method_id,
            Transaction.transaction_date > after.date(),
            Transaction.transaction_date <= today,
        )
    )
    return Decimal(delta) if delta is not None else Decimal("0")


def current_card_balances(session: Session) -> CardBalanceListResult:
    """Latest balance row per payment_method_id, restricted to credit cards,
    with `balance` derived live: recorded balance + post-balance transactions.

    The `credit_card_balances` table only records *payment* events (via the
    checking importer) and manual snapshots; new charges land in
    `transactions` and never push the row up. To avoid the stale-debt
    confusion that produces (a card showing the balance as of its last
    payment while hundreds of dollars of newer charges sit in the ledger),
    we derive the live balance on read.

    `recorded_balance` exposes the underlying row value; `post_balance_delta`
    shows the transactions sum that was layered on top. MoM and heatmap
    still use the (now live) `balance`.
    """
    sub = (
        select(
            CreditCardBalance.payment_method_id,
            func.max(CreditCardBalance.recorded_at).label("max_at"),
        )
        .group_by(CreditCardBalance.payment_method_id)
        .subquery()
    )
    # Tie-break on id: identical recorded_at values (e.g. a double-committed
    # import) would otherwise return the same card twice.
    latest_ids = (
        select(func.max(CreditCardBalance.id).label("max_id"))
        .join(
            sub,
            (CreditCardBalance.payment_method_id == sub.c.payment_method_id)
            & (CreditCardBalance.recorded_at == sub.c.max_at),
        )
        .group_by(CreditCardBalance.payment_method_id)
        .subquery()
    )
    stmt = (
        select(CreditCardBalance)
        .join(latest_ids, CreditCardBalance.id == latest_ids.c.max_id)
        .join(PaymentMethod, PaymentMethod.id == CreditCardBalance.payment_method_id)
        .order_by(PaymentMethod.name)
    )
    items = session.scalars(stmt).all()
    effective = _latest_effective_rate(session)
    today = date_type.today()

    enriched: list[CardBalanceRow] = []
    sums: dict[str, Decimal] = {}
    for b in items:
        currency_value = b.payment_method.currency.value
        recorded = Decimal(b.balance)
        delta = _post_balance_delta(
            session,
            payment_method_id=b.payment_method_id,
            after=b.recorded_at,
            today=today,
        )
        balance = recorded + delta
        if currency_value == Currency.USD.value:
            usd_eq = balance
        else:
            usd_eq = balance / effective if effective else balance
        prev = _latest_prev_month_card_balance(session, b.payment_method_id, today=today)
        if prev is not None and prev != 0:
            mom_pct = ((balance - prev) / abs(prev)) * Decimal("100")
            mom_pct = mom_pct.quantize(Decimal("0.1"))
        else:
            mom_pct = None
        enriched.append(CardBalanceRow(
            id=b.id,
            payment_method_id=b.payment_method_id,
            payment_method_name=b.payment_method.name,
            currency=currency_value,
            balance=balance,
            statement=b.statement,
            due_day=b.payment_method.due_day,
            recorded_at=b.recorded_at,
            usd_equivalent=usd_eq.quantize(Decimal("0.01")),
            prev_balance=prev,
            mom_pct=mom_pct,
            recorded_balance=recorded,
            post_balance_delta=delta,
        ))
        sums[currency_value] = sums.get(currency_value, Decimal("0")) + balance

    bounds: CardHeatmapBounds | None = None
    usd_values = [r.usd_equivalent for r in enriched if r.usd_equivalent is not None]
    if usd_values:
        bounds = CardHeatmapBounds(min_usd=min(usd_values), max_usd=max(usd_values))

    return CardBalanceListResult(rows=enriched, sum_by_currency=sums, heatmap_bounds=bounds)


@dataclass
class CardBalanceCreate:
    payment_method_id: int
    balance: Decimal
    statement: Decimal | None = None
    due_day: int | None = None
    recorded_at: datetime | None = None


def create_card_balance(session: Session, payload: CardBalanceCreate) -> CardBalanceRow:
    pm = session.get(PaymentMethod, payload.payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payload.payment_method_id} not found")
    # due_day moved to payment_methods. When the
    # caller passes it on a balance create, treat it as a per-card update.
    if payload.due_day is not None and pm.due_day != payload.due_day:
        pm.due_day = payload.due_day
    row = CreditCardBalance(
        payment_method_id=payload.payment_method_id,
        balance=payload.balance,
        statement=payload.statement,
    )
    if payload.recorded_at is not None:
        row.recorded_at = payload.recorded_at
    session.add(row)
    session.flush()
    session.commit()
    session.refresh(row)
    return _card_row(row)


@dataclass
class CardBalancePatch:
    balance: Decimal | None = None
    statement: Decimal | None = None
    due_day: int | None = None
    recorded_at: datetime | None = None
    payment_method_id: int | None = None


def update_card_balance(session: Session, balance_id: int, patch: CardBalancePatch) -> CardBalanceRow:
    row = session.get(CreditCardBalance, balance_id)
    if row is None:
        raise LookupError(f"Card balance {balance_id} not found")
    if patch.payment_method_id is not None:
        if session.get(PaymentMethod, patch.payment_method_id) is None:
            raise ValueError(f"Payment method {patch.payment_method_id} not found")
        row.payment_method_id = patch.payment_method_id
    if patch.balance is not None:
        row.balance = patch.balance
    if patch.statement is not None:
        row.statement = patch.statement
    if patch.due_day is not None:
        # due_day lives on payment_methods now.
        row.payment_method.due_day = patch.due_day
    if patch.recorded_at is not None:
        row.recorded_at = patch.recorded_at
    session.flush()
    session.commit()
    session.refresh(row)
    return _card_row(row)


def delete_card_balance(session: Session, balance_id: int) -> None:
    row = session.get(CreditCardBalance, balance_id)
    if row is None:
        raise LookupError(f"Card balance {balance_id} not found")
    session.delete(row)
    session.commit()


# ---------- car loan ----------

@dataclass(frozen=True)
class CarPaymentRow:
    id: int
    posting_date: date_type
    payment_amount: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    new_balance: Decimal


@dataclass(frozen=True)
class CarLoanSummary:
    latest_balance: Decimal | None
    latest_payment_date: date_type | None
    total_payments: int
    total_principal_paid: Decimal
    total_interest_paid: Decimal


def _car_row(p: CarLoanPayment) -> CarPaymentRow:
    return CarPaymentRow(
        id=p.id,
        posting_date=p.posting_date,
        payment_amount=p.payment_amount,
        principal_paid=p.principal_paid,
        interest_paid=p.interest_paid,
        new_balance=p.new_balance,
    )


def list_car_payments(session: Session, *, limit: int = 1000) -> list[CarPaymentRow]:
    items = session.scalars(
        select(CarLoanPayment)
        .order_by(desc(CarLoanPayment.posting_date), desc(CarLoanPayment.id))
        .limit(limit)
    ).all()
    return [_car_row(p) for p in items]


def car_loan_summary(session: Session) -> CarLoanSummary:
    latest = session.scalar(
        select(CarLoanPayment).order_by(
            desc(CarLoanPayment.posting_date), desc(CarLoanPayment.id)
        ).limit(1)
    )
    totals = session.execute(
        select(
            func.count(CarLoanPayment.id),
            func.coalesce(func.sum(CarLoanPayment.principal_paid), 0),
            func.coalesce(func.sum(CarLoanPayment.interest_paid), 0),
        )
    ).one()
    count, principal_total, interest_total = totals
    return CarLoanSummary(
        latest_balance=latest.new_balance if latest else None,
        latest_payment_date=latest.posting_date if latest else None,
        total_payments=count or 0,
        total_principal_paid=Decimal(principal_total),
        total_interest_paid=Decimal(interest_total),
    )


@dataclass
class CarPaymentCreate:
    posting_date: date_type
    payment_amount: Decimal
    principal_paid: Decimal
    interest_paid: Decimal
    new_balance: Decimal


def create_car_payment(session: Session, payload: CarPaymentCreate) -> CarPaymentRow:
    row = CarLoanPayment(
        posting_date=payload.posting_date,
        payment_amount=payload.payment_amount,
        principal_paid=payload.principal_paid,
        interest_paid=payload.interest_paid,
        new_balance=payload.new_balance,
    )
    session.add(row)
    session.flush()
    session.commit()
    session.refresh(row)
    return _car_row(row)


@dataclass
class CarPaymentPatch:
    posting_date: date_type | None = None
    payment_amount: Decimal | None = None
    principal_paid: Decimal | None = None
    interest_paid: Decimal | None = None
    new_balance: Decimal | None = None


def update_car_payment(session: Session, payment_id: int, patch: CarPaymentPatch) -> CarPaymentRow:
    row = session.get(CarLoanPayment, payment_id)
    if row is None:
        raise LookupError(f"Car loan payment {payment_id} not found")
    if patch.posting_date is not None:
        row.posting_date = patch.posting_date
    if patch.payment_amount is not None:
        row.payment_amount = patch.payment_amount
    if patch.principal_paid is not None:
        row.principal_paid = patch.principal_paid
    if patch.interest_paid is not None:
        row.interest_paid = patch.interest_paid
    if patch.new_balance is not None:
        row.new_balance = patch.new_balance
    session.flush()
    session.commit()
    session.refresh(row)
    return _car_row(row)


def delete_car_payment(session: Session, payment_id: int) -> None:
    row = session.get(CarLoanPayment, payment_id)
    if row is None:
        raise LookupError(f"Car loan payment {payment_id} not found")
    session.delete(row)
    session.commit()
