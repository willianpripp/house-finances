"""Calendar reminder push: the id scheme, the push set, and failure isolation.

The calendar's HTTP API is monkeypatched throughout — these tests pin OUR
behavior (which deadlines become reminders, that ids are stable, that one dead
POST cannot take the run with it), never the calendar's. No test here touches
the network.

Every figure and card name below is invented. The one exception is what the
migrations already seeded into the test schema, which is why the push-set
assertions check membership rather than an exact set: a reminder for a seeded
goal is expected and is not this file's business.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    Category,
    CreditCardBalance,
    Currency,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    RecurrenceKind,
    SpendGoal,
    Transaction,
)
from app.services import calendar_push

# Fixed "today" so the horizons and the projected dates are not a function of
# when the suite runs.
TODAY = date(2026, 9, 1)


# ---------- HTTP doubles ----------

class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self) -> dict:
        return self._body


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(calendar_push.settings, "cal_api_url", "https://calendar.test/")
    monkeypatch.setattr(calendar_push.settings, "cal_api_key", "test-key")
    monkeypatch.setattr(calendar_push, "_warned_unconfigured", False)


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(calendar_push.settings, "cal_api_url", "")
    monkeypatch.setattr(calendar_push.settings, "cal_api_key", "")
    monkeypatch.setattr(calendar_push, "_warned_unconfigured", False)


def _fake_post(monkeypatch, responder):
    """Replace httpx.post with `responder(payload) -> FakeResponse` and return
    the list of payloads it saw, in order."""
    seen: list[dict] = []

    def post(url, *, json, headers, timeout):
        assert url == "https://calendar.test/api/reminders", url
        assert headers["Authorization"] == "Bearer test-key"
        assert timeout > 0
        seen.append(json)
        return responder(json)

    monkeypatch.setattr(calendar_push.httpx, "post", post)
    return seen


def _always_created(_payload) -> FakeResponse:
    return FakeResponse(200, {"id": 1, "created": True})


# ---------- fixture data ----------

def _card(db, *, due_day: int | None = None) -> PaymentMethod:
    pm = PaymentMethod(
        name=f"Push Test Card {uuid.uuid4().hex[:8]}",
        type=PaymentMethodType.CREDIT_CARD,
        currency=Currency.USD,
        active=True,
        due_day=due_day,
    )
    db.add(pm)
    db.flush()
    return pm


def _contract(db, *, ends: date) -> Transaction:
    merchant = Merchant(name=f"Push Test Vendor {uuid.uuid4().hex[:8]}")
    db.add(merchant)
    db.flush()
    category = db.scalar(select(Category).limit(1))
    pm = _card(db)
    t = Transaction(
        transaction_date=TODAY - timedelta(days=60),
        merchant_id=merchant.id,
        category_id=category.id,
        payment_method_id=pm.id,
        amount=Decimal("31.00"),
        currency=Currency.USD,
        recurrence_kind=RecurrenceKind.CONTRACT,
        contract_end_date=ends,
    )
    db.add(t)
    db.flush()
    return t


def _card_with_balance(db, *, due_day: int) -> PaymentMethod:
    pm = _card(db, due_day=due_day)
    db.add(CreditCardBalance(
        payment_method_id=pm.id,
        balance=Decimal("410.00"),
        recorded_at=datetime.combine(
            TODAY - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc
        ),
    ))
    db.flush()
    return pm


def _goal(db, *, deadline: date, target: str = "2000.00") -> SpendGoal:
    pm = _card(db)
    goal = SpendGoal(
        payment_method_id=pm.id,
        target_amount=Decimal(target),
        currency=Currency.USD,
        start_date=TODAY - timedelta(days=15),
        deadline=deadline,
        reward_note="Invented bonus for the test suite",
        active=True,
    )
    db.add(goal)
    db.flush()
    return goal


@pytest.fixture
def push_set(db):
    """One of each source, all inside their push horizons."""
    contract = _contract(db, ends=TODAY + timedelta(days=30))
    card = _card_with_balance(db, due_day=5)  # present to prove cards push NOTHING
    goal = _goal(db, deadline=TODAY + timedelta(days=45))
    return {"contract": contract, "card": card, "goal": goal}


def _by_id(reminders) -> dict:
    return {r.external_id: r for r in reminders}


# ---------- the external_id scheme ----------

def test_external_ids_follow_the_documented_scheme(db, push_set):
    reminders = _by_id(calendar_push.build_push_set(db, today=TODAY))
    contract, goal = push_set["contract"], push_set["goal"]

    contract_id = f"fin-contract-{contract.id}-{contract.contract_end_date.isoformat()}"
    goal_id = f"fin-goal-{goal.id}"

    assert contract_id in reminders
    assert goal_id in reminders
    # Statement due dates are deliberately NOT pushed (the owner, 2026-08-20:
    # the statement-alerts feed is the one he does not use, and a calendar
    # reminder for it would resurrect it through the back door).
    assert not any(i.startswith("fin-stmt-") for i in reminders)


def test_external_ids_do_not_move_when_today_moves(db, push_set):
    """The whole idempotency story: a second run one day later must produce the
    same ids for the same deadlines, or the calendar double-books everything."""
    first = _by_id(calendar_push.build_push_set(db, today=TODAY))
    later = _by_id(calendar_push.build_push_set(db, today=TODAY + timedelta(days=1)))

    contract_id = (
        f"fin-contract-{push_set['contract'].id}-"
        f"{push_set['contract'].contract_end_date.isoformat()}"
    )
    for external_id in (contract_id, f"fin-goal-{push_set['goal'].id}"):
        assert external_id in first and external_id in later
        assert first[external_id].due_date == later[external_id].due_date
        assert first[external_id].title == later[external_id].title


def test_cards_push_no_statement_reminders(db):
    """Statement due dates never reach the calendar: the statement-alerts feed
    is unused by the household, and pushing it would resurrect it."""
    _card_with_balance(db, due_day=5)
    reminders = _by_id(calendar_push.build_push_set(db, today=TODAY))
    assert not any(i.startswith("fin-stmt-") for i in reminders)


# ---------- what the push set contains ----------

def test_contract_reminder_carries_the_end_date_and_a_lead(db):
    ends = TODAY + timedelta(days=30)
    contract = _contract(db, ends=ends)
    reminders = _by_id(calendar_push.build_push_set(db, today=TODAY))
    reminder = reminders[f"fin-contract-{contract.id}-{ends.isoformat()}"]

    assert reminder.due_date == ends
    assert reminder.lead_days == calendar_push.CONTRACT_LEAD_DAYS
    assert reminder.category == "Bills"
    assert "contract ends" in reminder.title


def test_goal_title_states_the_target_never_the_remainder(db):
    """The reminder is written once, months before the deadline, while the
    remaining amount changes daily — so the title may only carry static facts.
    The invented target is deliberately under a thousand: a formatted amount
    with a thousands separator in a test file is what the export's money audit
    is looking for, and this one is not a real figure."""
    goal = _goal(db, deadline=TODAY + timedelta(days=45), target="750.00")
    reminder = _by_id(calendar_push.build_push_set(db, today=TODAY))[f"fin-goal-{goal.id}"]

    assert reminder.title == f"{goal.payment_method.name}: $750 bonus window closes"
    assert reminder.due_date == goal.deadline
    assert reminder.lead_days == calendar_push.GOAL_LEAD_DAYS


def test_past_deadlines_and_far_futures_are_not_pushed(db):
    stale = _goal(db, deadline=TODAY - timedelta(days=1))
    far_contract = _contract(db, ends=TODAY + timedelta(days=200))

    reminders = _by_id(calendar_push.build_push_set(db, today=TODAY))
    assert f"fin-goal-{stale.id}" not in reminders
    assert not [k for k in reminders if k.startswith(f"fin-contract-{far_contract.id}-")]


def test_no_card_alert_of_any_kind_reaches_the_calendar(db):
    """Neither the due date nor the "statement closed, import it" chore is
    pushed: the whole statement-alerts feed stays out of the calendar."""
    _card_with_balance(db, due_day=5).statement_close_day = 28  # closed alert
    db.flush()

    reminders = calendar_push.build_push_set(db, today=TODAY)
    assert not any(r.external_id.startswith("fin-stmt-") for r in reminders)
    assert not [r for r in reminders if "import" in r.title.lower()]


def test_every_reminder_is_a_bill_owned_by_both(db, push_set):
    for reminder in calendar_push.build_push_set(db, today=TODAY):
        assert reminder.category == "Bills", reminder.external_id
        assert reminder.owner == "Both", reminder.external_id
        payload = reminder.payload()
        assert payload["due_date"] == reminder.due_date.isoformat()
        assert payload["external_id"] == reminder.external_id


# ---------- pushing ----------

def test_unconfigured_calendar_is_a_silent_no_op(db, push_set, unconfigured, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("an unconfigured calendar must not touch the network")

    monkeypatch.setattr(calendar_push.httpx, "post", explode)

    summary = calendar_push.push_all(db, today=TODAY)
    assert summary.configured is False
    assert (summary.reminders, summary.created, summary.skipped, summary.failed) == (0, 0, 0, 0)


def test_half_configured_counts_as_unconfigured(monkeypatch):
    """A URL without a key 401s on every POST; a key without a URL has nowhere
    to go. Neither is a usable state, so neither enables the push."""
    monkeypatch.setattr(calendar_push.settings, "cal_api_url", "https://calendar.test")
    monkeypatch.setattr(calendar_push.settings, "cal_api_key", "")
    assert calendar_push.is_configured() is False

    monkeypatch.setattr(calendar_push.settings, "cal_api_url", "")
    monkeypatch.setattr(calendar_push.settings, "cal_api_key", "key")
    assert calendar_push.is_configured() is False


def test_created_false_is_a_success_not_a_failure(db, push_set, configured, monkeypatch):
    """The steady state: this runs daily and every reminder already exists."""
    seen = _fake_post(monkeypatch, lambda _p: FakeResponse(200, {"id": 7, "created": False}))

    summary = calendar_push.push_all(db, today=TODAY)
    assert summary.failed == 0
    assert summary.created == 0
    assert summary.skipped == summary.reminders == len(seen)
    assert all(o.status == "skipped" for o in summary.outcomes)


def test_one_failing_post_does_not_abort_the_rest(db, push_set, configured, monkeypatch):
    calls: list[dict] = []

    def responder(payload):
        calls.append(payload)
        if len(calls) == 2:
            raise RuntimeError("connection reset")
        return FakeResponse(200, {"id": len(calls), "created": True})

    seen = _fake_post(monkeypatch, responder)

    summary = calendar_push.push_all(db, today=TODAY)
    assert summary.reminders >= 3
    assert len(seen) == summary.reminders, "every reminder must still be attempted"
    assert summary.failed == 1
    assert summary.created == summary.reminders - 1
    failed = [o for o in summary.outcomes if o.status == "failed"]
    assert "connection reset" in failed[0].detail


@pytest.mark.parametrize(
    "status,fragment",
    [(401, "CAL_API_KEY"), (422, "422"), (503, "disabled"), (500, "HTTP 500")],
)
def test_error_statuses_are_reported_not_raised(configured, monkeypatch, status, fragment):
    _fake_post(monkeypatch, lambda _p: FakeResponse(status, {}, text="nope"))

    outcome = calendar_push.push_reminder(calendar_push.Reminder(
        external_id="fin-goal-999",
        title="Invented goal",
        due_date=TODAY + timedelta(days=10),
        lead_days=7,
    ))
    assert outcome.status == "failed"
    assert fragment in outcome.detail


# ---------- the script ----------

def _script():
    import scripts.push_calendar_reminders as script

    return script


def test_script_exits_zero_when_everything_landed(monkeypatch, configured):
    script = _script()
    monkeypatch.setattr(script, "is_configured", lambda: True)
    monkeypatch.setattr(
        script,
        "push_all",
        lambda session, **kw: calendar_push.PushSummary(
            reminders=2,
            created=1,
            skipped=1,
            outcomes=[
                calendar_push.PushOutcome("fin-goal-1", "created"),
                calendar_push.PushOutcome("fin-goal-2", "skipped", "already present"),
            ],
        ),
    )
    assert script.main() == 0


def test_script_exits_zero_on_an_empty_push_set(monkeypatch, configured):
    """No upcoming deadlines is a normal day, not a failure."""
    script = _script()
    monkeypatch.setattr(script, "is_configured", lambda: True)
    monkeypatch.setattr(script, "push_all", lambda session, **kw: calendar_push.PushSummary())
    assert script.main() == 0


def test_script_exits_one_on_any_failed_reminder(monkeypatch, configured):
    script = _script()
    monkeypatch.setattr(script, "is_configured", lambda: True)
    monkeypatch.setattr(
        script,
        "push_all",
        lambda session, **kw: calendar_push.PushSummary(
            reminders=2,
            created=1,
            failed=1,
            outcomes=[
                calendar_push.PushOutcome("fin-goal-1", "created"),
                calendar_push.PushOutcome("fin-goal-2", "failed", "HTTP 500 boom"),
            ],
        ),
    )
    assert script.main() == 1


def test_script_exits_one_when_the_calendar_is_not_configured(monkeypatch):
    script = _script()
    monkeypatch.setattr(script, "is_configured", lambda: False)
    monkeypatch.setattr(
        script, "push_all", lambda *a, **k: pytest.fail("must not push when unconfigured")
    )
    assert script.main() == 1
