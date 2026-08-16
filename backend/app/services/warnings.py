"""Warnings: overdraft forecasting + contract/installment expiry.

Two readouts feed `/warnings` and the Home dashboard:

1. `overdraft_forecast(horizon_days)` — for each CHECKING payment_method,
   project the running balance over the next N days using
     • upcoming FIXED transactions paid directly from this checking
       (rent, gym, insurance — recurrence_kind is INDEFINITE/CONTRACT/
       INSTALLMENT). Next-occurrence date = same day of month as the
       latest historical row for that recurring bill, clamped to the
       target month's last day.
     • upcoming CC autopays — for each credit card whose
       `paid_from_payment_method_id` points at this checking AND whose
       current balance > 0, draft the payment on its `due_day` (next
       monthly occurrence inside the horizon).
   Returns the projected end balance plus the largest deficit observed
   along the way (negative balance is the alarm).

2. `expiring_contracts(horizon_days)` — INSTALLMENT transactions whose
   final installment falls inside the horizon, and CONTRACT transactions
   whose `contract_end_date` falls inside the horizon. Each row links
   back to the transaction.

The service is pure-query: no writes, no caching. Cheap enough to
recompute on every Home / Warnings page load.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.services import household
from app.models import (
    Category,
    CategoryType,
    Currency,
    ImportLog,
    IncomeEntry,
    PaymentMethod,
    PaymentMethodType,
    Transaction,
)
from app.models.enums import IncomeSource, RecurrenceKind
from app.services.debts import latest_card_balance_live
from app.services.savings import _latest_effective_rate, current_balances


# Map recurring IncomeSource -> projection metadata.
#   checking_pm_name: target account where the deposit lands.
#   day_of_month: when the deposit posts (99 = end-of-month, clamped).
#   withholding_merchants: merchants whose latest same-currency FIXED Tax
#     rows are subtracted from the gross IncomeEntry to project NET cash.
#     A member's gross is invariant per pay level; their withholdings vary by
#     month and arrive subtracted from the deposit. Both members share the
#     shape — which merchants count is `withholding_merchants` config.
# Salary deposits land at the very end of the month BEFORE the one they fund
# (lag-1: IncomeEntry(2026-06) is funded by the deposit at the end of
# 2026-05). Rents arrive around day 5 in BRL.
# Salary rules now come from `household_members` (which checking account, which
# day, which withholding merchants) — see app/services/household.py. Only the
# non-salary sources stay here, because they are not tied to a member.
RENTS_DAY_OF_MONTH = 10


def _rents_rule(session: Session) -> tuple[IncomeSource, dict] | None:
    """Rent deposits are not tied to a household member, so they are keyed on
    the account that receives them: the foreign-currency checking account the
    rent importer writes to. Returns None when there is no such account."""
    pm_name = session.scalar(
        select(PaymentMethod.name)
        .where(
            PaymentMethod.type == PaymentMethodType.CHECKING,
            PaymentMethod.currency == Currency.BRL,
            PaymentMethod.active.is_(True),
        )
        .order_by(PaymentMethod.id)
    )
    if pm_name is None:
        return None
    return (
        IncomeSource.RENTS_BRAZIL,
        {
            "checking_pm_name": pm_name,
            "day_of_month": RENTS_DAY_OF_MONTH,
            "withholding_merchants": (),
        },
    )


def _projection_rules(session: Session) -> list[tuple[IncomeSource, dict]]:
    """Salary rules from household config, plus the standalone rent rule."""
    rules: list[tuple[IncomeSource, dict]] = []
    for member in household.all_members(session):
        if member.salary_checking is None:
            continue
        rules.append(
            (
                member.salary_income_source,
                {
                    "checking_pm_name": member.salary_checking.name,
                    "day_of_month": member.salary_day_of_month,
                    "withholding_merchants": household.withholding_merchant_names(session, member),
                },
            )
        )
    rents = _rents_rule(session)
    if rents is not None:
        rules.append(rents)
    return rules


# ---------- DTOs ----------

@dataclass
class OverdraftEvent:
    """One projected cashflow hitting a checking account on `date`.
    Positive `amount` is always the magnitude — the running-balance walk
    subtracts when `source_type` is debit-like, adds when it's an income.
    `amount_usd` is populated only when the account currency is BRL, so
    the UI can show a USD-equivalent under the native value."""
    event_date: date
    amount: Decimal
    source: str            # "FIXED: Rent" / "CC autopay: <card>" / "Income: <member> salary"
    source_type: str       # "fixed" | "cc_autopay" | "income"
    amount_usd: Decimal | None = None


@dataclass
class OverdraftForecast:
    checking_id: int
    checking_name: str
    currency: str
    current_balance: Decimal
    projected_balance: Decimal     # balance at the end of the horizon (after debits + incomes)
    projected_debits: Decimal      # sum of upcoming outflows in horizon
    projected_incomes: Decimal     # sum of upcoming inflows in horizon
    min_balance: Decimal           # lowest point reached
    min_balance_date: date | None
    deficit: Decimal               # max(0, -min_balance) — 0 means no overdraft
    events: list[OverdraftEvent] = field(default_factory=list)
    has_cc_links: bool = True      # False = CCs aren't wired → forecast is incomplete
    # USD-equivalents for BRL accounts (None when account is already USD).
    current_balance_usd: Decimal | None = None
    projected_balance_usd: Decimal | None = None
    projected_debits_usd: Decimal | None = None
    projected_incomes_usd: Decimal | None = None


@dataclass
class ExpiringItem:
    transaction_id: int
    transaction_date: date
    merchant_name: str
    category_name: str
    payment_method_name: str
    amount: Decimal
    currency: str
    recurrence_kind: str           # INSTALLMENT | CONTRACT
    severity: str                  # "high" (this month) | "medium" (within horizon)
    detail: str                    # "Last installment 6/6 due 2026-06-12" / "Contract ends 2026-07-01"


# ---------- helpers ----------

def _shift_day(source: date, target_year: int, target_month: int) -> date:
    last = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(source.day, last))


def _months_in_horizon(start: date, end: date) -> list[tuple[int, int]]:
    """Every (year, month) pair touched by the [start, end] interval."""
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ---------- income projection ----------

def _latest_withholding_total(
    session: Session,
    *,
    merchant_names: tuple[str, ...],
    today: date,
) -> Decimal:
    """Sum the magnitude of the most recent FIXED row per withholding-merchant
    (looking back up to 90 days). Each merchant contributes its latest amount;
    merchants with no recent row contribute 0."""
    if not merchant_names:
        return Decimal("0")
    cutoff = today - timedelta(days=90)
    total = Decimal("0")
    for name in merchant_names:
        latest = session.scalar(
            select(Transaction)
            .join(Transaction.merchant)
            .where(
                Transaction.transaction_date >= cutoff,
                Transaction.transaction_date <= today,
            )
            .where(Transaction.merchant.has(name=name))
            .order_by(desc(Transaction.transaction_date))
            .limit(1)
        )
        if latest is not None:
            total += abs(Decimal(latest.amount))
    return total


def _projected_incomes_for_checking(
    session: Session,
    *,
    checking_name: str,
    today: date,
    horizon_end: date,
    months: list[tuple[int, int]],
) -> list[OverdraftEvent]:
    """Project recurring inflows (salaries, BR rents) that historically land in
    `checking_name`. Amount projected is NET — gross IncomeEntry minus the
    most-recent same-currency withholding rows that arrive subtracted from
    the deposit (each member's configured withholding merchants; nothing for
    BR rents).

    Lag-1 model: a deposit at the end of month M funds the
    IncomeEntry for month M+1. We project a deposit when its funded month
    (M+1) has no IncomeEntry yet AND the source has been active recently
    (any entry in the last 3 funded months)."""
    out: list[OverdraftEvent] = []
    eligible_sources = [
        (src, rule) for src, rule in _projection_rules(session)
        if rule["checking_pm_name"] == checking_name
    ]
    if not eligible_sources:
        return out

    def _next_month(y: int, m: int) -> tuple[int, int]:
        return (y + 1, 1) if m == 12 else (y, m + 1)

    cutoff_year, cutoff_month = today.year, today.month - 3
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1

    for src, rule in eligible_sources:
        day_of_month = rule["day_of_month"]
        existing_months: set[tuple[int, int]] = set(
            session.execute(
                select(IncomeEntry.year, IncomeEntry.month).where(IncomeEntry.source == src)
            ).all()
        )
        if not existing_months:
            continue
        latest = max(existing_months)
        if latest < (cutoff_year, cutoff_month):
            continue
        latest_entry = session.scalar(
            select(IncomeEntry)
            .where(IncomeEntry.source == src, IncomeEntry.year == latest[0], IncomeEntry.month == latest[1])
        )
        gross = abs(Decimal(latest_entry.amount))
        withholding_total = _latest_withholding_total(
            session, merchant_names=rule["withholding_merchants"], today=today
        )
        net = gross - withholding_total
        if net <= 0:
            continue
        member = household.member_by_income_source(session, src)
        if member is not None:
            label = f"{member.display_name} salary (net)"
        elif src == IncomeSource.RENTS_BRAZIL:
            label = "BR rents"
        else:
            label = src.value.replace("_", " ")
        for y, m in months:
            last_day = calendar.monthrange(y, m)[1]
            projected = date(y, m, min(day_of_month, last_day))
            if projected < today or projected > horizon_end:
                continue
            funded = _next_month(y, m)
            if funded in existing_months:
                continue
            out.append(OverdraftEvent(
                event_date=projected,
                amount=net,
                source=f"Income: {label}",
                source_type="income",
            ))
    return out


# ---------- overdraft ----------

def overdraft_forecast(
    session: Session,
    *,
    horizon_days: int = 14,
    today: date | None = None,
) -> list[OverdraftForecast]:
    """For each CHECKING payment method, return an overdraft projection
    covering the next `horizon_days`. The list is sorted by deficit DESC
    so the worst case shows first."""
    today = today or date.today()
    horizon_end = today + timedelta(days=horizon_days)

    # Current per-account balance (USD-eq is irrelevant here — we project
    # in the account's native currency).
    savings = current_balances(session)
    balance_by_account: dict[str, Decimal] = {
        r.account_name: r.balance for r in savings.rows
    }

    checkings = session.scalars(
        select(PaymentMethod)
        .where(
            PaymentMethod.type == PaymentMethodType.CHECKING,
            PaymentMethod.active.is_(True),
        )
        .order_by(PaymentMethod.name)
    ).all()

    forecasts: list[OverdraftForecast] = []
    months = _months_in_horizon(today, horizon_end)
    effective_rate = _latest_effective_rate(session)

    def _to_usd(value: Decimal) -> Decimal:
        return (value / effective_rate).quantize(Decimal("0.01")) if effective_rate else value

    for pm in checkings:
        events: list[OverdraftEvent] = []

        # 1) FIXED transactions paid directly from this checking.
        #    Look at the latest occurrence of each recurring bill on this
        #    payment_method and project forward into the horizon months.
        #    NULL recurrence_kind means "one-off" after the backfill,
        #    so we exclude it — only INDEFINITE / CONTRACT / INSTALLMENT
        #    rows are real recurring bills. (Pre-backfill rows are skipped.)
        # B-OV1: Taxes-category FIXED rows (Federal/State Withholding, the
        # accountant fee, SIMPLES/DARF) exist for gross→net accounting on the
        # Income card in /reports/monthly — they are never charged against a
        # checking account. Including them here would double-count the
        # withholdings that already arrive subtracted from the salary deposit.
        fixed_latest = session.scalars(
            select(Transaction)
            .join(Transaction.category)
            .where(
                Transaction.payment_method_id == pm.id,
                Category.type == CategoryType.FIXED,
                Category.name != "Taxes",
                Transaction.recurrence_kind.in_([
                    RecurrenceKind.INDEFINITE,
                    RecurrenceKind.CONTRACT,
                    RecurrenceKind.INSTALLMENT,
                ]),
            )
            .order_by(Transaction.merchant_id, desc(Transaction.transaction_date))
        ).all()
        # Take the most recent row per merchant_id (first one seen since we
        # ordered by date DESC within merchant).
        seen: set[int] = set()
        for t in fixed_latest:
            if t.merchant_id in seen:
                continue
            seen.add(t.merchant_id)
            # Skip CONTRACT ones whose end date already passed.
            if (
                t.recurrence_kind == RecurrenceKind.CONTRACT
                and t.contract_end_date is not None
                and t.contract_end_date < today
            ):
                continue
            # Project each subsequent month in horizon.
            for y, m in months:
                projected = _shift_day(t.transaction_date, y, m)
                if projected <= t.transaction_date:
                    # Same month or earlier than the source row — already paid.
                    continue
                if projected < today or projected > horizon_end:
                    continue
                # If we know the contract ends before this projection, skip.
                if (
                    t.recurrence_kind == RecurrenceKind.CONTRACT
                    and t.contract_end_date is not None
                    and projected > t.contract_end_date
                ):
                    continue
                # INSTALLMENT: skip if already at final installment.
                if (
                    t.recurrence_kind == RecurrenceKind.INSTALLMENT
                    and t.installment_total > 1
                    and t.installment_current >= t.installment_total
                ):
                    continue
                amount = abs(Decimal(t.installment_value) if t.installment_value is not None else Decimal(t.amount))
                events.append(OverdraftEvent(
                    event_date=projected,
                    amount=amount,
                    source=f"FIXED: {t.merchant.name}",
                    source_type="fixed",
                ))

        # 2) CC autopays — every card with paid_from_payment_method_id == pm.id.
        cards = session.scalars(
            select(PaymentMethod).where(
                PaymentMethod.paid_from_payment_method_id == pm.id,
                PaymentMethod.type == PaymentMethodType.CREDIT_CARD,
            )
        ).all()
        has_links = len(cards) > 0
        for card in cards:
            # Live balance: latest row + post-balance tx (so new charges
            # since the last recorded balance row contribute to the autopay
            # projection, matching what /debts shows).
            live, cb = latest_card_balance_live(session, card.id, today=today)
            if cb is None or live <= 0:
                continue
            # due_day lives on payment_methods now.
            due_day = card.due_day
            if due_day is None:
                continue
            # Project the next due date(s) in horizon.
            for y, m in months:
                last = calendar.monthrange(y, m)[1]
                projected = date(y, m, min(due_day, last))
                if projected < today or projected > horizon_end:
                    continue
                events.append(OverdraftEvent(
                    event_date=projected,
                    amount=live,
                    source=f"CC autopay: {card.name}",
                    source_type="cc_autopay",
                ))

        # 3) Recurring incomes landing in this checking (salary + BR rents).
        events.extend(_projected_incomes_for_checking(
            session,
            checking_name=pm.name,
            today=today,
            horizon_end=horizon_end,
            months=months,
        ))

        # Sort by date for the running-balance walk.
        events.sort(key=lambda e: e.event_date)

        # Walk the projection. Incomes add, everything else subtracts.
        running = balance_by_account.get(pm.name, Decimal("0"))
        min_b = running
        min_date: date | None = None
        debits_total = Decimal("0")
        incomes_total = Decimal("0")
        for e in events:
            if e.source_type == "income":
                running += e.amount
                incomes_total += e.amount
            else:
                running -= e.amount
                debits_total += e.amount
            if running < min_b:
                min_b = running
                min_date = e.event_date
        is_brl = pm.currency.value == "BRL"
        current = balance_by_account.get(pm.name, Decimal("0"))
        if is_brl:
            for e in events:
                e.amount_usd = _to_usd(e.amount)
        forecasts.append(OverdraftForecast(
            checking_id=pm.id,
            checking_name=pm.name,
            currency=pm.currency.value,
            current_balance=current,
            projected_balance=running,
            projected_debits=debits_total,
            projected_incomes=incomes_total,
            min_balance=min_b,
            min_balance_date=min_date,
            deficit=max(Decimal("0"), -min_b),
            events=events,
            has_cc_links=has_links,
            current_balance_usd=_to_usd(current) if is_brl else None,
            projected_balance_usd=_to_usd(running) if is_brl else None,
            projected_debits_usd=_to_usd(debits_total) if is_brl else None,
            projected_incomes_usd=_to_usd(incomes_total) if is_brl else None,
        ))

    forecasts.sort(key=lambda f: f.deficit, reverse=True)
    return forecasts


# ---------- expiring contracts / installments ----------

def expiring_contracts(
    session: Session,
    *,
    horizon_days: int = 90,
    today: date | None = None,
) -> list[ExpiringItem]:
    """Find INSTALLMENT series ending and CONTRACT transactions whose
    contract_end_date falls inside [today, today + horizon_days].

    Severity tiers: high ≤30d ('sign now'), medium 30-60d ('prepare'),
    low 60-90d ('heads up'). The 90d default gives ~3 months of runway,
    which matters for rent / insurance / phone contracts that need
    paperwork before signing the renewal."""
    today = today or date.today()
    horizon_end = today + timedelta(days=horizon_days)

    out: list[ExpiringItem] = []

    # CONTRACT rows ending in the horizon.
    contracts = session.scalars(
        select(Transaction)
        .where(
            Transaction.recurrence_kind == RecurrenceKind.CONTRACT,
            Transaction.contract_end_date.is_not(None),
        )
        .order_by(Transaction.contract_end_date)
    ).all()
    seen_contract_keys: set[tuple[int, int]] = set()
    for t in contracts:
        if t.contract_end_date is None:
            continue
        if not (today <= t.contract_end_date <= horizon_end):
            continue
        key = (t.merchant_id, t.payment_method_id)
        if key in seen_contract_keys:
            continue
        seen_contract_keys.add(key)
        days_left = (t.contract_end_date - today).days
        if days_left <= 30:
            severity = "high"
        elif days_left <= 60:
            severity = "medium"
        else:
            severity = "low"
        out.append(ExpiringItem(
            transaction_id=t.id,
            transaction_date=t.transaction_date,
            merchant_name=t.merchant.name,
            category_name=t.category.name,
            payment_method_name=t.payment_method.name,
            amount=abs(Decimal(t.installment_value) if t.installment_value is not None else Decimal(t.amount)),
            currency=t.currency.value,
            recurrence_kind=t.recurrence_kind.value,
            severity=severity,
            detail=f"Contract ends {t.contract_end_date.isoformat()} ({days_left}d)",
        ))

    # INSTALLMENT series on their last 1 installment in the horizon. We look
    # for the latest row per series where current >= total - 1 (penultimate
    # or final). For each merchant we want the latest pre-final row that
    # implies a "final installment" coming up.
    installments = session.scalars(
        select(Transaction)
        .where(
            Transaction.recurrence_kind == RecurrenceKind.INSTALLMENT,
            Transaction.installment_total > 1,
        )
        .order_by(Transaction.merchant_id, desc(Transaction.transaction_date))
    ).all()
    seen_installment_keys: set[tuple[int, int]] = set()
    for t in installments:
        key = (t.merchant_id, t.payment_method_id)
        if key in seen_installment_keys:
            continue
        seen_installment_keys.add(key)
        # The "final installment date" projection: increment the current
        # by (total - current) months from this row's date.
        steps_left = t.installment_total - t.installment_current
        if steps_left <= 0:
            continue
        # The next installment is one month after this row.
        next_year, next_month = t.transaction_date.year, t.transaction_date.month + steps_left
        while next_month > 12:
            next_month -= 12
            next_year += 1
        final_date = _shift_day(t.transaction_date, next_year, next_month)
        if not (today <= final_date <= horizon_end):
            continue
        days_left = (final_date - today).days
        if days_left <= 30:
            severity = "high"
        elif days_left <= 60:
            severity = "medium"
        else:
            severity = "low"
        out.append(ExpiringItem(
            transaction_id=t.id,
            transaction_date=t.transaction_date,
            merchant_name=t.merchant.name,
            category_name=t.category.name,
            payment_method_name=t.payment_method.name,
            amount=abs(Decimal(t.installment_value) if t.installment_value is not None else Decimal(t.amount)),
            currency=t.currency.value,
            recurrence_kind=t.recurrence_kind.value,
            severity=severity,
            detail=f"Last installment ({t.installment_total}/{t.installment_total}) ~{final_date.isoformat()}",
        ))

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda r: (severity_rank.get(r.severity, 3), r.detail))
    return out


# ---------- statement alerts (close + due) ----------

def _imported_since(session: Session, *, payment_method_id: int, after: date) -> bool:
    """True iff there is an `import_logs` row for this PM with
    `imported_at >= after`. Uses `ImportLog.payment_method_id` so a
    fully-idempotent re-import (transaction_count=0, no Transactions
    pointing back) still counts as "I already imported this cycle".
    Independent of `ImportSource` — covers CC files, CC paste (MANUAL),
    checking PDFs and checking paste alike."""
    after_dt = datetime.combine(after, time.min)
    hit = session.scalar(
        select(ImportLog.id)
        .where(
            ImportLog.payment_method_id == payment_method_id,
            ImportLog.imported_at >= after_dt,
        )
        .limit(1)
    )
    return hit is not None


@dataclass
class StatementAlert:
    payment_method_id: int
    payment_method_name: str
    currency: str
    kind: str              # "statement_closed" | "closing_soon" | "due_soon"
    severity: str          # "high" | "medium" | "low"
    target_date: date      # the close or due date the alert is anchored on
    days_offset: int       # negative = days passed since target; positive = days until target
    message: str
    balance: Decimal | None = None  # populated for due_soon (latest CC balance)


def _next_dom(today: date, day: int) -> date:
    """Next occurrence of `day` of month on or after `today`."""
    last = calendar.monthrange(today.year, today.month)[1]
    candidate = date(today.year, today.month, min(day, last))
    if candidate >= today:
        return candidate
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day, last))


def _prev_dom(today: date, day: int) -> date:
    """Most recent occurrence of `day` of month on or before `today`."""
    last = calendar.monthrange(today.year, today.month)[1]
    candidate = date(today.year, today.month, min(day, last))
    if candidate <= today:
        return candidate
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day, last))


def statement_alerts(
    session: Session,
    *,
    today: date | None = None,
    closing_horizon_days: int = 5,
    closed_lookback_days: int = 10,
    due_horizon_days: int = 7,
) -> list[StatementAlert]:
    """Per active credit card AND checking account, surface timing alerts:

    - **statement_closed**: PM has a `statement_close_day` AND its
      previous close fell within `closed_lookback_days` AND no
      transaction has been imported for this PM since that close.
      Tells the user "the fatura/statement just came out — import it now".

    - **closing_soon**: PM has a `statement_close_day` AND the next
      close is within `closing_horizon_days`. For CCs: "ensure balance".
      For checkings: "ready the import".

    - **due_soon**: card has a `due_day` AND the next due is within
      `due_horizon_days` AND there's a non-zero latest balance on the
      card. Tells the user "the payment is about to be charged".
      Checkings never have due_day so this branch is CC-only.

    PMs without `statement_close_day` get only `due_soon` (CCs); PMs
    without `due_day` get only the close-related ones; PMs with
    neither contribute nothing.
    """
    today = today or date.today()
    out: list[StatementAlert] = []

    pms = session.scalars(
        select(PaymentMethod).where(
            PaymentMethod.type.in_(
                (PaymentMethodType.CREDIT_CARD, PaymentMethodType.CHECKING)
            ),
            PaymentMethod.active.is_(True),
        )
    ).all()

    for c in pms:
        close_day = c.statement_close_day
        due_day = c.due_day
        currency = c.currency.value
        is_checking = c.type == PaymentMethodType.CHECKING
        artifact = "statement"  # English-only UI (was "fatura" for cards)

        if close_day is not None:
            # closing_soon
            next_close = _next_dom(today, close_day)
            days_to_close = (next_close - today).days
            if 0 <= days_to_close <= closing_horizon_days:
                sev = "medium" if days_to_close <= 2 else "low"
                tail = "ensure balance" if not is_checking else "ready the import"
                out.append(StatementAlert(
                    payment_method_id=c.id,
                    payment_method_name=c.name,
                    currency=currency,
                    kind="closing_soon",
                    severity=sev,
                    target_date=next_close,
                    days_offset=days_to_close,
                    message=(
                        f"Closes in {days_to_close}d ({next_close.isoformat()}) — {tail}"
                        if days_to_close > 0
                        else f"Closes today ({next_close.isoformat()})"
                    ),
                ))

            # statement_closed (prev close in lookback window, no import since)
            prev_close = _prev_dom(today, close_day)
            days_since_close = (today - prev_close).days
            if 1 <= days_since_close <= closed_lookback_days:
                if not _imported_since(session, payment_method_id=c.id, after=prev_close):
                    sev = "high" if days_since_close >= 5 else "medium"
                    out.append(StatementAlert(
                        payment_method_id=c.id,
                        payment_method_name=c.name,
                        currency=currency,
                        kind="statement_closed",
                        severity=sev,
                        target_date=prev_close,
                        days_offset=-days_since_close,
                        message=(
                            f"Closed {days_since_close}d ago ({prev_close.isoformat()}) — "
                            f"import the new {artifact}"
                        ),
                    ))

        if due_day is not None:
            next_due = _next_dom(today, due_day)
            days_to_due = (next_due - today).days
            if 0 <= days_to_due <= due_horizon_days:
                live, cb = latest_card_balance_live(session, c.id, today=today)
                bal = abs(live) if cb is not None else None
                if bal and bal > 0:
                    sev = "high" if days_to_due <= 2 else "medium" if days_to_due <= 5 else "low"
                    out.append(StatementAlert(
                        payment_method_id=c.id,
                        payment_method_name=c.name,
                        currency=currency,
                        kind="due_soon",
                        severity=sev,
                        target_date=next_due,
                        days_offset=days_to_due,
                        message=(
                            f"Due in {days_to_due}d ({next_due.isoformat()}) — "
                            f"{bal:.2f} {currency}"
                            if days_to_due > 0
                            else f"Due today ({next_due.isoformat()}) — {bal:.2f} {currency}"
                        ),
                        balance=bal,
                    ))

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    kind_rank = {"statement_closed": 0, "due_soon": 1, "closing_soon": 2}
    out.sort(key=lambda a: (severity_rank.get(a.severity, 3), kind_rank.get(a.kind, 9), a.target_date))
    return out


# ---------- summary (for Home) ----------

@dataclass
class WarningsSummary:
    overdraft_count: int       # how many checking accounts go negative
    expiring_count: int        # rows in expiring_contracts
    items: list[dict] = field(default_factory=list)  # top 5 mixed


def summarize(session: Session, *, today: date | None = None) -> WarningsSummary:
    today = today or date.today()
    fs = overdraft_forecast(session, today=today)
    es = expiring_contracts(session, today=today)

    items: list[dict] = []
    for f in fs:
        if f.deficit > 0:
            items.append({
                "severity": "high",
                "kind": "overdraft",
                "title": f"{f.checking_name} projected -{f.deficit:.2f} {f.currency}",
                "detail": f"on {f.min_balance_date}" if f.min_balance_date else "",
            })
    for e in es[:10]:
        items.append({
            "severity": e.severity,
            "kind": "expiring",
            "title": f"{e.merchant_name} — {e.recurrence_kind.lower()}",
            "detail": e.detail,
        })
    summary_rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: summary_rank.get(i["severity"], 3))
    return WarningsSummary(
        overdraft_count=sum(1 for f in fs if f.deficit > 0),
        expiring_count=len(es),
        items=items[:5],
    )
