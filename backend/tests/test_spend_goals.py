"""Spend-goal progress, pace and deadline math.

`spend_goals` is generic (signup bonuses recur across cards), so these tests
build their own payment method and transactions rather than relying on the
shared fixture household's specific cards — same approach as
test_report_debt_live.py.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    Category,
    CategoryType,
    Currency,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    SpendGoal,
    Transaction,
)
from app.services.spend_goals import (
    DuplicateSpendGoalError,
    SpendGoalCreate,
    create_goal,
    get_goal,
    list_goals,
)


def _card(db) -> PaymentMethod:
    # `create_goal` commits (mirroring services/debts.py), so a test that
    # exercises it leaves its payment method permanently in the session-scoped
    # test schema — a fixed name here would collide with itself across tests.
    pm = PaymentMethod(
        name=f"Goal Test Card {uuid.uuid4().hex[:8]}",
        type=PaymentMethodType.CREDIT_CARD,
        currency=Currency.USD,
        active=True,
    )
    db.add(pm)
    db.flush()
    return pm


def _charge(db, pm: PaymentMethod, when: date, amount: str, *, category: Category | None = None) -> Transaction:
    merchant = db.scalar(select(Merchant).limit(1))
    cat = category or db.scalar(
        select(Category).where(Category.exclude_from_spending.is_(False)).limit(1)
    )
    t = Transaction(
        transaction_date=when,
        merchant_id=merchant.id,
        category_id=cat.id,
        payment_method_id=pm.id,
        amount=Decimal(amount),
        currency=Currency.USD,
    )
    db.add(t)
    db.flush()
    return t


def _goal(db, pm: PaymentMethod, *, start: date, deadline: date, target: str = "2000.00") -> SpendGoal:
    g = SpendGoal(
        payment_method_id=pm.id,
        target_amount=Decimal(target),
        currency=Currency.USD,
        start_date=start,
        deadline=deadline,
        reward_note="test bonus",
        active=True,
    )
    db.add(g)
    db.flush()
    return g


def test_progress_sums_only_positive_purchases_in_window(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    goal = _goal(db, pm, start=start, deadline=deadline)

    _charge(db, pm, start, "500.00")                        # counts
    _charge(db, pm, start + timedelta(days=1), "-50.00")    # refund/credit: excluded
    _charge(db, pm, start - timedelta(days=1), "999.00")    # before window: excluded
    _charge(db, pm, deadline + timedelta(days=1), "999.00")  # after window: excluded

    progress = get_goal(db, goal.id, today=start + timedelta(days=1))
    assert progress.spent == Decimal("500.00")
    assert progress.remaining == Decimal("1500.00")
    assert progress.pct == Decimal("25.0")


def test_progress_excludes_exclude_from_spending_categories(db):
    """A category flagged exclude_from_spending (equity transfer, not
    consumption) must not count toward the bonus even if it lands on the
    card with a positive amount."""
    pm = _card(db)
    transfer_cat = Category(
        name="Equity Transfer Test", type=CategoryType.VARIABLE, exclude_from_spending=True
    )
    db.add(transfer_cat)
    db.flush()
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    goal = _goal(db, pm, start=start, deadline=deadline)

    _charge(db, pm, start, "300.00")
    _charge(db, pm, start, "700.00", category=transfer_cat)

    progress = get_goal(db, goal.id, today=start)
    assert progress.spent == Decimal("300.00")


def test_on_pace_true_when_ahead_of_linear_schedule(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)  # days_total = 90
    goal = _goal(db, pm, start=start, deadline=deadline, target="2000.00")
    # Halfway through (45 days): linear expectation is 1000.00.
    _charge(db, pm, start, "1200.00")

    progress = get_goal(db, goal.id, today=start + timedelta(days=45))
    assert progress.days_elapsed == 45
    assert progress.on_pace is True


def test_on_pace_false_when_behind_linear_schedule(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    goal = _goal(db, pm, start=start, deadline=deadline, target="2000.00")
    _charge(db, pm, start, "100.00")

    progress = get_goal(db, goal.id, today=start + timedelta(days=45))
    assert progress.on_pace is False


def test_completed_goal_is_on_pace_and_stays_completed_past_deadline(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    goal = _goal(db, pm, start=start, deadline=deadline, target="2000.00")
    _charge(db, pm, start, "2000.00")

    progress = get_goal(db, goal.id, today=deadline + timedelta(days=10))
    assert progress.completed is True
    assert progress.on_pace is True
    assert progress.days_left == -10


def test_deadline_math_days_total_and_elapsed_clamp(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = date(2026, 11, 15)  # matches the real Samsung Galaxy Card goal
    goal = _goal(db, pm, start=start, deadline=deadline)
    assert (deadline - start).days == 90

    before_start = get_goal(db, goal.id, today=start - timedelta(days=5))
    assert before_start.days_elapsed == 0
    assert before_start.days_total == 90

    midpoint = get_goal(db, goal.id, today=start + timedelta(days=10))
    assert midpoint.days_elapsed == 10
    assert midpoint.days_left == 80

    after_deadline = get_goal(db, goal.id, today=deadline + timedelta(days=5))
    assert after_deadline.days_elapsed == after_deadline.days_total == 90
    assert after_deadline.days_left == -5


def test_list_goals_active_only_filter(db):
    # Other goals may already exist in this session-scoped test schema (the
    # seed migration's own Samsung Galaxy Card row, or ones committed by an
    # earlier test), so this asserts membership rather than the full set.
    #
    # The two goals here use non-overlapping windows on the same payment
    # method: create_goal now refuses an overlapping window regardless of
    # the active flag (see test_create_rejects_overlapping_window_same_card),
    # so this test's own two goals must not collide with each other either.
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    active = create_goal(db, SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("2000.00"),
        start_date=start, deadline=deadline, reward_note="active goal", active=True,
    ))
    start2 = deadline + timedelta(days=1)
    deadline2 = start2 + timedelta(days=90)
    inactive = create_goal(db, SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("500.00"),
        start_date=start2, deadline=deadline2, reward_note="inactive goal", active=False,
    ))

    all_ids = {g.id for g in list_goals(db, active_only=False)}
    assert active.id in all_ids and inactive.id in all_ids

    active_only_ids = {g.id for g in list_goals(db, active_only=True)}
    assert active.id in active_only_ids
    assert inactive.id not in active_only_ids


def test_create_rejects_deadline_not_after_start(db):
    pm = _card(db)
    start = date(2026, 8, 17)
    try:
        create_goal(db, SpendGoalCreate(
            payment_method_id=pm.id, target_amount=Decimal("2000.00"),
            start_date=start, deadline=start, reward_note="bad window", active=True,
        ))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_create_rejects_exact_duplicate(db):
    """Re-POSTing what already exists (the bug the audit found: re-running
    the seed migration's payload through the API) must be refused, not
    silently double the goal."""
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    payload = SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("2000.00"),
        start_date=start, deadline=deadline, reward_note="signup bonus", active=True,
    )
    create_goal(db, payload)

    with pytest.raises(DuplicateSpendGoalError):
        create_goal(db, payload)


def test_create_rejects_overlapping_window_same_card(db):
    """A second goal on the same card whose window overlaps an existing
    one, even with a different start_date/target/reward_note, is refused:
    two spend-to-earn windows on one card can't both be "in progress" at
    once without double-counting the same purchases."""
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    create_goal(db, SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("2000.00"),
        start_date=start, deadline=deadline, reward_note="first bonus", active=True,
    ))

    overlapping_start = start + timedelta(days=30)  # inside the first window
    overlapping_deadline = deadline + timedelta(days=30)  # extends past it
    with pytest.raises(DuplicateSpendGoalError):
        create_goal(db, SpendGoalCreate(
            payment_method_id=pm.id, target_amount=Decimal("500.00"),
            start_date=overlapping_start, deadline=overlapping_deadline,
            reward_note="second bonus", active=True,
        ))


def test_create_allows_non_overlapping_second_goal_same_card(db):
    """A second, later goal on the same card is fine once the first
    window has fully closed (deadline is exclusive-of-next-start in
    practice, i.e. back-to-back windows don't overlap)."""
    pm = _card(db)
    start = date(2026, 8, 17)
    deadline = start + timedelta(days=90)
    first = create_goal(db, SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("2000.00"),
        start_date=start, deadline=deadline, reward_note="first bonus", active=True,
    ))

    second_start = deadline + timedelta(days=1)
    second_deadline = second_start + timedelta(days=90)
    second = create_goal(db, SpendGoalCreate(
        payment_method_id=pm.id, target_amount=Decimal("2000.00"),
        start_date=second_start, deadline=second_deadline,
        reward_note="second bonus", active=True,
    ))

    assert first.id != second.id
    all_ids = {g.id for g in list_goals(db, active_only=False)}
    assert {first.id, second.id} <= all_ids


def test_create_endpoint_duplicate_returns_409(client, db):
    pm = _card(db)
    db.commit()
    payload = {
        "payment_method_id": pm.id,
        "target_amount": "2000.00",
        "start_date": "2026-08-17",
        "deadline": "2026-11-15",
        "reward_note": "signup bonus",
    }
    r1 = client.post("/api/spend-goals", json=payload)
    assert r1.status_code == 201, r1.text

    r2 = client.post("/api/spend-goals", json=payload)
    assert r2.status_code == 409
    assert "overlap" in r2.json()["detail"].lower() or "already has" in r2.json()["detail"].lower()


# ---------- endpoint smoke test ----------

def test_list_endpoint_smoke(client):
    r = client.get("/api/spend-goals")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_create_and_patch_endpoint_roundtrip(client, db):
    pm = _card(db)
    db.commit()

    r = client.post("/api/spend-goals", json={
        "payment_method_id": pm.id,
        "target_amount": "2000.00",
        "start_date": "2026-08-17",
        "deadline": "2026-11-15",
        "reward_note": "endpoint test bonus",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["payment_method_id"] == pm.id
    assert body["reward_note"] == "endpoint test bonus"
    assert body["currency"] == "USD"
    goal_id = body["id"]

    r2 = client.patch(f"/api/spend-goals/{goal_id}", json={"active": False})
    assert r2.status_code == 200
    assert r2.json()["active"] is False

    r3 = client.get(f"/api/spend-goals/{goal_id}")
    assert r3.status_code == 200
    assert r3.json()["active"] is False
