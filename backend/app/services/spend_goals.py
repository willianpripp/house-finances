"""Spend goals: generic spend-to-earn progress tracker.

A `SpendGoal` row says "reach `target_amount` in purchases on this payment
method between `start_date` and `deadline` to unlock `reward_note`" — a card
signup bonus, most commonly, but nothing here is bonus-specific.

Progress is derived on read, never stored, same "derive don't cache"
approach as the live credit-card balance (`services/debts.py`):

- **What counts as "spent"**: `transactions.amount` carries the natural sign
  (purchases positive, refunds/credits negative — see the docstring on
  `services/debts.py:post_balance_delta`). Credit-card autopays are never
  persisted as `Transaction` rows (they only reduce `credit_card_balances`),
  so summing positive-amount rows on the goal's payment method already
  excludes payments; it also naturally excludes refunds/credits, which are
  negative and would only reduce the total if included.
- Rows in a category flagged `exclude_from_spending` (transfers to equity,
  not consumption — e.g. extra principal on a loan) are excluded too: they
  are not "purchases" in the sense a card issuer counts toward a bonus.
- The window is `[start_date, deadline]` inclusive: a purchase before the
  account existed or after the bonus window closed does not count, even if
  it lands on the same payment method.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Category, PaymentMethod, SpendGoal, Transaction


class DuplicateSpendGoalError(ValueError):
    """A goal on this payment method already covers an overlapping window.

    Raised by `create_goal`'s proactive overlap check (the honest error,
    with the clashing goal named); the UNIQUE(payment_method_id, start_date)
    constraint added in the `add_spend_goals` migration is the backstop for
    the exact-same-key race, converted here to the same error.
    """


@dataclass(frozen=True)
class SpendGoalProgress:
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
    remaining: Decimal          # max(0, target - spent)
    pct: Decimal                # spent / target * 100, uncapped (can exceed 100)
    days_total: int             # deadline - start_date, in days
    days_elapsed: int           # today - start_date, clamped to [0, days_total]
    days_left: int              # deadline - today, NOT clamped (negative = past deadline)
    on_pace: bool                # spent >= a linear-pace expectation for today
    completed: bool              # spent >= target_amount


def _spent_since(
    session: Session,
    *,
    payment_method_id: int,
    start_date: date_type,
    deadline: date_type,
) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(Transaction.amount), Decimal("0")))
        .join(Transaction.category)
        .where(
            Transaction.payment_method_id == payment_method_id,
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= deadline,
            Transaction.amount > 0,
            Category.exclude_from_spending.is_(False),
        )
    )
    return Decimal(total) if total is not None else Decimal("0")


def _progress(session: Session, goal: SpendGoal, *, today: date_type) -> SpendGoalProgress:
    spent = _spent_since(
        session,
        payment_method_id=goal.payment_method_id,
        start_date=goal.start_date,
        deadline=goal.deadline,
    )
    target = Decimal(goal.target_amount)
    remaining = max(Decimal("0"), target - spent)
    pct = (spent / target * Decimal("100")) if target else Decimal("0")
    pct = pct.quantize(Decimal("0.1"))

    days_total = max(0, (goal.deadline - goal.start_date).days)
    days_elapsed_raw = (today - goal.start_date).days
    days_elapsed = min(max(days_elapsed_raw, 0), days_total)
    days_left = (goal.deadline - today).days

    completed = spent >= target
    if days_total > 0:
        expected = target * Decimal(days_elapsed) / Decimal(days_total)
    else:
        expected = target
    on_pace = completed or spent >= expected

    return SpendGoalProgress(
        id=goal.id,
        payment_method_id=goal.payment_method_id,
        payment_method_name=goal.payment_method.name,
        target_amount=target,
        currency=goal.currency.value,
        start_date=goal.start_date,
        deadline=goal.deadline,
        reward_note=goal.reward_note,
        active=goal.active,
        spent=spent,
        remaining=remaining,
        pct=pct,
        days_total=days_total,
        days_elapsed=days_elapsed,
        days_left=days_left,
        on_pace=on_pace,
        completed=completed,
    )


def list_goals(
    session: Session,
    *,
    active_only: bool = False,
    today: date_type | None = None,
) -> list[SpendGoalProgress]:
    today = today or date_type.today()
    stmt = select(SpendGoal).order_by(SpendGoal.deadline)
    if active_only:
        stmt = stmt.where(SpendGoal.active.is_(True))
    goals = session.scalars(stmt).all()
    return [_progress(session, g, today=today) for g in goals]


def get_goal(session: Session, goal_id: int, *, today: date_type | None = None) -> SpendGoalProgress:
    goal = session.get(SpendGoal, goal_id)
    if goal is None:
        raise LookupError(f"Spend goal {goal_id} not found")
    return _progress(session, goal, today=today or date_type.today())


@dataclass
class SpendGoalCreate:
    payment_method_id: int
    target_amount: Decimal
    start_date: date_type
    deadline: date_type
    reward_note: str
    active: bool = True


def create_goal(session: Session, payload: SpendGoalCreate) -> SpendGoalProgress:
    pm = session.get(PaymentMethod, payload.payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payload.payment_method_id} not found")
    if payload.deadline <= payload.start_date:
        raise ValueError("deadline must be after start_date")

    # Same guard as the seed migration's NOT EXISTS, but window-aware rather
    # than exact-start_date-only: a card only opens once, so two goals on the
    # same payment method with overlapping [start_date, deadline] windows are
    # never legitimate — most commonly a re-POST of a goal that already
    # exists, which would otherwise silently double its progress card on
    # /warnings and /home.
    clash = session.scalar(
        select(SpendGoal).where(
            SpendGoal.payment_method_id == payload.payment_method_id,
            SpendGoal.start_date <= payload.deadline,
            SpendGoal.deadline >= payload.start_date,
        )
    )
    if clash is not None:
        raise DuplicateSpendGoalError(
            f"Payment method {payload.payment_method_id} already has spend goal "
            f"{clash.id} covering {clash.start_date}..{clash.deadline}, which "
            f"overlaps the requested {payload.start_date}..{payload.deadline}"
        )

    goal = SpendGoal(
        payment_method_id=payload.payment_method_id,
        target_amount=payload.target_amount,
        currency=pm.currency,
        start_date=payload.start_date,
        deadline=payload.deadline,
        reward_note=payload.reward_note,
        active=payload.active,
    )
    session.add(goal)
    try:
        session.flush()
        session.commit()
    except IntegrityError:
        # Backstop for the race the check above can't close (two concurrent
        # creates both reading past the SELECT before either commits).
        session.rollback()
        raise DuplicateSpendGoalError(
            f"Payment method {payload.payment_method_id} already has a spend "
            f"goal starting {payload.start_date}"
        )
    session.refresh(goal)
    return _progress(session, goal, today=date_type.today())


@dataclass
class SpendGoalPatch:
    target_amount: Decimal | None = None
    start_date: date_type | None = None
    deadline: date_type | None = None
    reward_note: str | None = None
    active: bool | None = None


def update_goal(session: Session, goal_id: int, patch: SpendGoalPatch) -> SpendGoalProgress:
    goal = session.get(SpendGoal, goal_id)
    if goal is None:
        raise LookupError(f"Spend goal {goal_id} not found")
    if patch.target_amount is not None:
        goal.target_amount = patch.target_amount
    if patch.start_date is not None:
        goal.start_date = patch.start_date
    if patch.deadline is not None:
        goal.deadline = patch.deadline
    if patch.reward_note is not None:
        goal.reward_note = patch.reward_note
    if patch.active is not None:
        goal.active = patch.active
    if goal.deadline <= goal.start_date:
        raise ValueError("deadline must be after start_date")
    session.flush()
    session.commit()
    session.refresh(goal)
    return _progress(session, goal, today=date_type.today())
