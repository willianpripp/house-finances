"""Push finance deadlines into the family-calendar app as reminders.

Why this exists: `/warnings` is a PULL page. In practice only the
expiring-contracts feed ever got acted on, because the other two are seen
solely by whoever chose to open the page. The calendar is already the
household's push channel (it owns the Telegram bot), so finances hands it dated
reminders and never talks to Telegram itself.

Direction is one-way: finances writes, the calendar notifies. Nothing here
reads back, and nothing here is a source of truth — the reminder is a copy of
a date this database already knows.

The calendar's endpoint
-----------------------
    POST {CAL_API_URL}/api/reminders
    Authorization: Bearer {CAL_API_KEY}
    {"title", "due_date" (YYYY-MM-DD), "owner", "category",
     "lead_days", "notes", "external_id"}

    200 {"id": int, "created": bool}   created=false means "already there"
    401 bad key · 422 validation · 503 reminders disabled on the calendar side

`external_id` is the ENTIRE safety story
----------------------------------------
This runs daily and recomputes the whole push set every time, so the same
deadline is POSTed dozens of times before it passes. The calendar dedupes on
`external_id`, so every reminder we build has to derive its id from stable
facts only — never from `today`, a counter, or anything that reflects current
progress. The scheme:

    fin-contract-{transaction_id}-{end_date}   contract end / final installment
    fin-goal-{spend_goal_id}                   one spend-goal deadline

`fin-contract-*` deliberately carries the end date: the final-installment date
is a PROJECTION (see `warnings.expiring_contracts`), and if it moves, the old
reminder is wrong rather than stale. A new id then creates a second reminder
for the corrected date instead of silently leaving the wrong one to fire.

`fin-goal-*` needs nothing but the row id, and that constrains the title:
a spend-goal reminder is written ONCE, months before its deadline, while the
remaining amount changes every day. So the title states the target, never the
remainder ("<card>: <target> bonus window closes"), because a "<remaining> to
go" title would be written once and then be a lie for the rest of the window.

Best-effort by construction
---------------------------
The calendar being down, misconfigured or absent must never break a finances
page or abort a script. Unset `CAL_API_URL`/`CAL_API_KEY` is a normal state
(a fresh clone, or the calendar not yet deployed): every push path no-ops and
logs once. A failed POST is logged and counted; the only place a failure is
visible is the exit code of `scripts/push_calendar_reminders.py`, which is what
the systemd timer's LAST_RESULT watches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.spend_goals import list_goals
from app.services.warnings import expiring_contracts

logger = logging.getLogger("uvicorn.error")

_TIMEOUT = 10.0  # one POST per reminder, a handful per run; no retries

CATEGORY_BILLS = "Bills"
OWNER_BOTH = "Both"

# How far ahead the calendar should start nagging. The calendar owns the
# notification, we only say how much runway each kind of deadline needs.
CONTRACT_LEAD_DAYS = 14   # renewals need paperwork before the end date
GOAL_LEAD_DAYS = 7        # last chance to put spend on the card

# Horizons for the push set. Contracts match the /warnings page (90d runway).
# Statement dues use a wider window than the page's 7d default so the reminder
# lands in the calendar BEFORE it is already urgent.
CONTRACT_HORIZON_DAYS = 90

_CURRENCY_SYMBOLS = {"USD": "$", "BRL": "R$"}

_warned_unconfigured = False


def is_configured() -> bool:
    """Both halves or nothing: a URL without a key gets 401 on every POST, and
    a key without a URL has nowhere to go."""
    return bool(settings.cal_api_url and settings.cal_api_key)


def _warn_unconfigured_once() -> None:
    global _warned_unconfigured
    if _warned_unconfigured:
        return
    _warned_unconfigured = True
    logger.info(
        "calendar push disabled: set CAL_API_URL and CAL_API_KEY to enable "
        "(reminders are skipped, nothing else is affected)"
    )


def _endpoint() -> str:
    return f"{settings.cal_api_url.rstrip('/')}/api/reminders"


def _fmt_amount(amount: Decimal, currency: str) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency)
    whole = amount.quantize(Decimal("1"))
    if symbol:
        return f"{symbol}{whole:,}"
    return f"{whole:,} {currency}"


def _month_key(when: date_type) -> str:
    return f"{when.year:04d}-{when.month:02d}"


# ---------- what we push ----------

@dataclass(frozen=True)
class Reminder:
    """One calendar reminder, fully determined by database facts."""
    external_id: str
    title: str
    due_date: date_type
    lead_days: int
    notes: str | None = None
    category: str = CATEGORY_BILLS
    owner: str = OWNER_BOTH

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": self.title,
            "due_date": self.due_date.isoformat(),
            "owner": self.owner,
            "category": self.category,
            "lead_days": self.lead_days,
            "external_id": self.external_id,
        }
        if self.notes:
            body["notes"] = self.notes
        return body


def _contract_reminders(session: Session, *, today: date_type) -> list[Reminder]:
    """Contract ends and final installments — the one feed that already got
    acted on, which is why it is the first thing to become push."""
    out: list[Reminder] = []
    for item in expiring_contracts(session, horizon_days=CONTRACT_HORIZON_DAYS, today=today):
        is_installment = item.recurrence_kind == "INSTALLMENT"
        title = (
            f"{item.merchant_name} final installment"
            if is_installment
            else f"{item.merchant_name} contract ends"
        )
        out.append(Reminder(
            external_id=f"fin-contract-{item.transaction_id}-{item.end_date.isoformat()}",
            title=title,
            due_date=item.end_date,
            lead_days=CONTRACT_LEAD_DAYS,
            notes=(
                f"{item.amount:.2f} {item.currency} on {item.payment_method_name} "
                f"({item.category_name})"
            ),
        ))
    return out


def _goal_reminders(session: Session, *, today: date_type) -> list[Reminder]:
    """One reminder per active spend goal, on its deadline.

    A goal already met still gets its reminder: whether it is met is a fact
    about today, and the reminder is written once, months earlier. Filtering on
    it would make the push set depend on progress and stop being reproducible.
    Deadlines already in the past are dropped — the calendar has nothing to
    remind anyone about."""
    out: list[Reminder] = []
    for goal in list_goals(session, active_only=True, today=today):
        if goal.deadline < today:
            continue
        out.append(Reminder(
            external_id=f"fin-goal-{goal.id}",
            title=(
                f"{goal.payment_method_name}: "
                f"{_fmt_amount(goal.target_amount, goal.currency)} bonus window closes"
            ),
            due_date=goal.deadline,
            lead_days=GOAL_LEAD_DAYS,
            notes=goal.reward_note or None,
        ))
    return out


def build_push_set(session: Session, *, today: date_type | None = None) -> list[Reminder]:
    """The complete set of reminders the calendar should hold right now.

    Pure read: no writes, no HTTP. Recomputed from scratch on every run, which
    is only safe because every `external_id` is deterministic."""
    today = today or date_type.today()
    return [
        *_contract_reminders(session, today=today),
        *_goal_reminders(session, today=today),
    ]


# ---------- pushing ----------

@dataclass
class PushOutcome:
    external_id: str
    status: str          # "created" | "skipped" | "failed"
    detail: str = ""


@dataclass
class PushSummary:
    configured: bool = True
    reminders: int = 0
    created: int = 0
    skipped: int = 0     # calendar answered created=false — already there
    failed: int = 0
    outcomes: list[PushOutcome] = field(default_factory=list)


def push_reminder(reminder: Reminder) -> PushOutcome:
    """POST one reminder. Never raises: every failure comes back as an outcome
    so one dead reminder cannot take the rest of the run with it."""
    if not is_configured():
        _warn_unconfigured_once()
        return PushOutcome(reminder.external_id, "failed", "calendar not configured")

    try:
        resp = httpx.post(
            _endpoint(),
            json=reminder.payload(),
            headers={"Authorization": f"Bearer {settings.cal_api_key}"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # httpx transport errors, DNS, TLS, timeouts
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("calendar push %s failed: %s", reminder.external_id, detail)
        return PushOutcome(reminder.external_id, "failed", detail)

    if resp.status_code == 200:
        created = bool(resp.json().get("created", True))
        if created:
            logger.info("calendar push %s created", reminder.external_id)
            return PushOutcome(reminder.external_id, "created")
        logger.info("calendar push %s already present", reminder.external_id)
        return PushOutcome(reminder.external_id, "skipped", "already present")

    detail = {
        401: "calendar rejected CAL_API_KEY (401)",
        422: f"calendar rejected the payload (422): {resp.text[:200]}",
        503: "calendar reminders are disabled on the calendar side (503)",
    }.get(resp.status_code, f"HTTP {resp.status_code} {resp.text[:200]}")
    logger.warning("calendar push %s failed: %s", reminder.external_id, detail)
    return PushOutcome(reminder.external_id, "failed", detail)


def push_all(session: Session, *, today: date_type | None = None) -> PushSummary:
    """Compute the push set and POST every reminder in it.

    Best-effort: a failure is counted, never raised. An unconfigured calendar
    returns `configured=False` with an empty set and touches no network."""
    if not is_configured():
        _warn_unconfigured_once()
        return PushSummary(configured=False)

    reminders = build_push_set(session, today=today)
    summary = PushSummary(reminders=len(reminders))
    for reminder in reminders:
        outcome = push_reminder(reminder)
        summary.outcomes.append(outcome)
        if outcome.status == "created":
            summary.created += 1
        elif outcome.status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1
    return summary
