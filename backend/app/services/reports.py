"""Monthly and annual report aggregations.

Finalized months are read straight from monthly_snapshots — those values are
authoritative and frozen with the exchange rate active that month.

Live months (no snapshot yet, or is_finalized=False) are computed on the
fly from transactions/income/balances. The conversion rule:

  - amounts in transaction.currency stay in their native unit
  - money that MOVED on a day converts at the rate in force that day
  - a BALANCE (savings, card and loan debt, assets) is a value as of the end
    of the month being reported, so it converts at the month-end rate: the
    latest row whose rate_date <= the last day of the month
  - per-installment rows: we use COALESCE(installment_value, amount), so
    a 12x purchase contributes only its monthly slice

**Money that moved converts at ITS OWN date's rate (2026-08-20).** Income
landed first (per receipt, `services/income.convert_entry_to_usd`) and spending
followed the same day (per transaction, `_spend_rows` below). Both exist for
one reason: converting a whole period at "the latest rate on or before the
month end" re-priced the open month every time the daily PTAX run landed a row,
so a purchase made on the 5th was worth one thing in the morning and another
after the evening fetch, with nothing having happened in the household's
finances.

Neither grain is duplicated anywhere. Income has one call site
(`_compute_income`) and spending has one (`_month_spending`), both reached by
the monthly AND the annual report through `_month_totals`, so no two surfaces
can report different numbers for the same month. `tests/test_income_fx.py` and
`tests/test_transaction_fx.py` pin that month by month.

The month-end rate is still resolved here and still passed down: balances need
it, and two income cases fall back to it (a pre-ledger lump, and a monthly row
with no receipts). Spending has no such fallback, because every transaction
has a real date of its own.

The live computation is a best-effort approximation. The "Close Month" flow
shows these numbers, lets the user adjust transactions, then freezes them
into a snapshot — that snapshot is what future reports read back.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import and_, extract, func, select
from sqlalchemy.orm import Session

from app.models import (
    CarLoanPayment,
    Category,
    CategoryType,
    CreditCardBalance,
    Currency,
    ExchangeRate,
    IncomeEntry,
    IncomeSource,
    MonthlySnapshot,
    PaymentMethod,
    SavingsSnapshot,
    Transaction,
)
from app.services import income as income_service
from app.services.exchange_rates import DatedRate, DatedRateCache

ZERO = Decimal("0.00")
TAXES_CATEGORY_NAME = "Taxes"


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _month_bounds(year: int, month: int) -> tuple[date_type, date_type]:
    last_day = calendar.monthrange(year, month)[1]
    return date_type(year, month, 1), date_type(year, month, last_day)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _resolve_rate(session: Session, year: int, month: int) -> ExchangeRate | None:
    _, last_day = _month_bounds(year, month)
    return session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date <= last_day)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )


def _month_end_rate(session: Session, year: int, month: int) -> DatedRate:
    """`_resolve_rate` as a DatedRate, so the no-row case carries its own flag
    instead of every caller remembering to substitute 1."""
    row = _resolve_rate(session, year, month)
    return DatedRate.from_row(row) if row is not None else DatedRate.unavailable()


def _rate_inside_month(session: Session, year: int, month: int) -> ExchangeRate | None:
    """Latest exchange-rate row whose rate_date falls within [day 1, last day]
    of the target month. Used by Close Month — a rate from a prior month is
    not strict enough to freeze a snapshot."""
    first_day, last_day = _month_bounds(year, month)
    return session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date >= first_day, ExchangeRate.rate_date <= last_day)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )


def _finalize_status(
    session: Session, year: int, month: int
) -> tuple[bool, str | None]:
    """Returns (can_finalize, blocked_reason) for the given month."""
    snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == year, MonthlySnapshot.month == month
        )
    )
    if snap is not None and snap.is_finalized:
        return False, "Month is already finalized."
    if _rate_inside_month(session, year, month) is None:
        return False, (
            "No exchange-rate row inside this month. "
            "Add one on /exchange-rates before closing."
        )
    return True, None


def _to_usd(amount: Decimal, currency: str, effective: Decimal) -> Decimal:
    """Convert a BALANCE at one given rate.

    Money that moved on a day converts at that day's rate instead, via
    `_usd_on_date` (spending) or `income.convert_entry_to_usd` (income). This
    helper is for savings, card and loan debt and assets: those are values as
    of a point in time, so one rate for that point is the right answer, not a
    per-row one.
    """
    if currency == Currency.USD.value:
        return Decimal(amount)
    return Decimal(amount) / Decimal(effective)


# ---------- DTOs ----------

@dataclass
class IncomeBucket:
    source: str
    amount_native: Decimal
    currency: str
    amount_usd: Decimal
    # How amount_usd was arrived at, and whether any rate behind it was a
    # fallback rather than the rate of the day. See
    # services/income.RATE_BASIS_* and convert_entry_to_usd. Defaulted so a
    # caller building a bucket by hand still gets the honest "already USD"
    # answer rather than a claim it cannot back.
    rate_basis: str = income_service.RATE_BASIS_USD
    approximate: bool = False


@dataclass
class CategoryBucket:
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    transaction_count: int
    category_icon: str | None = None
    # True when at least one transaction in this category converted with a
    # fallback rate (no exchange_rates row at or before its transaction_date).
    # The same meaning IncomeBucket.approximate has, at the category grain, and
    # what MonthTotals.spending_rate_approximate is derived from.
    approximate: bool = False


@dataclass
class MonthTotals:
    year: int
    month: int
    is_finalized: bool

    rate_id: int | None
    rate_effective: Decimal | None
    rate_date: date_type | None

    primary_salary_usd: Decimal
    partner_salary_usd: Decimal
    rents_brazil_usd: Decimal
    extra_income_usd: Decimal
    gross_income_usd: Decimal
    taxes_usd: Decimal
    net_income_usd: Decimal

    fixed_spending_usd: Decimal
    variable_spending_usd: Decimal
    total_spending_usd: Decimal

    surplus_usd: Decimal

    total_savings_usd: Decimal
    total_debt_usd: Decimal
    net_worth_usd: Decimal

    assets_total_usd: Decimal = ZERO
    total_worth_usd: Decimal = ZERO

    can_finalize: bool = False
    finalize_blocked_reason: str | None = None

    # True when any income bucket in this month was converted with a fallback
    # rate (no exchange_rates row at or before a receipt's date). Derived from
    # the buckets by whichever path built these totals, so the annual view —
    # which carries totals and no buckets — can still say so.
    income_rate_approximate: bool = False

    # The same, for the spending side: set when any transaction counted toward
    # this month's spending had no exchange_rates row at or before its
    # transaction_date. Separate from the income flag because the two answer
    # different questions ("is the income figure exact" / "is the spending
    # figure exact") and a period can easily have one without the other.
    spending_rate_approximate: bool = False

    # Per-salary tax breakdown (USD taxes -> partner, BRL taxes -> primary).
    # Re-derived from transactions; not persisted in monthly_snapshots so
    # finalized months also recompute on read. Optional — defaults to ZERO
    # if the report is built without the per-salary split.
    taxes_partner_usd: Decimal = ZERO
    taxes_primary_usd: Decimal = ZERO


@dataclass
class TransactionDetail:
    id: int
    transaction_date: date_type
    merchant_name: str
    payment_method_name: str
    category_id: int
    category_name: str
    category_type: str
    category_color: str
    owner_name: str | None
    amount_native: Decimal
    currency: str
    amount_usd: Decimal
    installment_current: int
    installment_total: int
    description: str | None
    category_icon: str | None = None
    pending: bool = False


@dataclass
class MonthlyReport:
    totals: MonthTotals
    income: list[IncomeBucket]
    by_category: list[CategoryBucket]
    fixed_categories: list[CategoryBucket] = field(default_factory=list)
    variable_categories: list[CategoryBucket] = field(default_factory=list)
    fixed_transactions: list[TransactionDetail] = field(default_factory=list)
    variable_transactions: list[TransactionDetail] = field(default_factory=list)
    # Categories flagged exclude_from_spending (e.g. Car Extra = loan principal).
    # Shown as a separate section, NOT in the spending totals/surplus.
    excluded_categories: list[CategoryBucket] = field(default_factory=list)
    excluded_transactions: list[TransactionDetail] = field(default_factory=list)
    excluded_total_usd: Decimal = ZERO
    prior: MonthTotals | None = None


# ---------- live computation ----------

def _compute_income(
    session: Session,
    year: int,
    month: int,
    month_end: DatedRate,
    rates: DatedRateCache | None = None,
) -> tuple[list[IncomeBucket], dict[str, Decimal]]:
    """The month's income per source, in native units and in USD.

    The single place income becomes USD, for every report. `amount_native`
    stays the derived monthly total on `income_entries` (what warnings.py and
    home.py read); `amount_usd` is the per-receipt conversion of that same
    total, which is the sum of the receipts the total is made of. The two
    describe one number, not two.

    One `DatedRateCache` per month, shared across the five sources (and, when
    the caller passes its own, with that month's spending conversion too): the
    annual report renders twelve of these in one request and receipt dates
    repeat.
    """
    rows = session.scalars(
        select(IncomeEntry).where(
            and_(IncomeEntry.year == year, IncomeEntry.month == month)
        )
    ).all()
    rates = rates if rates is not None else DatedRateCache(session)
    buckets: list[IncomeBucket] = []
    by_source: dict[str, Decimal] = {s.value: ZERO for s in IncomeSource}
    for r in rows:
        converted = income_service.convert_entry_to_usd(
            session, r, month_end=month_end, rates=rates
        )
        buckets.append(IncomeBucket(
            source=r.source.value,
            amount_native=Decimal(r.amount),
            currency=r.currency.value,
            amount_usd=_q(converted.amount_usd),
            rate_basis=converted.rate_basis,
            approximate=converted.approximate,
        ))
        by_source[r.source.value] = (
            by_source.get(r.source.value, ZERO) + converted.amount_usd
        )
    return buckets, by_source


# ---------- spending conversion (per transaction date) ----------


@dataclass(frozen=True)
class SpendRow:
    """One transaction's contribution to a period's spending, already in USD.

    `amount_native` is the installment slice, not the purchase price: a 12x
    buy contributes one twelfth to each of twelve months. `amount_usd` is NOT
    quantized, for the same reason `income.EntryConversion.amount_usd` is not:
    rounding every row to the cent before summing a few hundred of them moves
    the total. Quantize at the display edge, once.
    """

    transaction_id: int
    transaction_date: date_type
    category_id: int
    category_name: str
    category_type: str
    category_color: str
    category_icon: str | None
    excluded_from_spending: bool
    currency: str
    amount_native: Decimal
    amount_usd: Decimal
    approximate: bool


def _usd_on_date(
    amount: Decimal, currency: str, on_date: date_type, rates: DatedRateCache
) -> tuple[Decimal, bool]:
    """Money in `currency` that moved on `on_date`, in USD, and whether the
    rate behind it was a fallback.

    The transaction-grain twin of `income.convert_entry_to_usd`'s inner loop.
    USD reads no rate at all: a row is converted by ITS OWN currency, never by
    its account's or its period's, which is what keeps the never-mix-currencies
    hard rule true. Weekends and holidays need no special case, because
    `rate_for_date` takes the latest row at or before the day and PTAX
    publishes business days, so a Saturday purchase converts at Friday's close.
    """
    if currency == Currency.USD.value:
        return Decimal(amount), False
    rate = rates.for_date(on_date)
    return Decimal(amount) / rate.effective, rate.approximate


def _spend_rows(
    session: Session, year: int, month: int, rates: DatedRateCache
) -> list[SpendRow]:
    """Every transaction dated in the month, converted at its own date's rate.

    THE single place a transaction becomes USD. One query for the whole month,
    columns only (no ORM entities, no relationship loads), including the
    `exclude_from_spending` categories so that the counted total, the excluded
    section and the per-salary tax split all derive from one conversion instead
    of three.

    Cost: one query here, plus the one `rates.warm` already spent on the
    month's rate window. Deliberately NOT one rate lookup per row, which is
    what a naive per-date conversion would cost and what
    `test_transaction_fx.py` pins against.
    """
    amount_expr = func.coalesce(Transaction.installment_value, Transaction.amount)
    stmt = (
        select(
            Transaction.id,
            Transaction.transaction_date,
            Transaction.currency,
            amount_expr.label("amount_native"),
            Category.id.label("category_id"),
            Category.name,
            Category.type,
            Category.color,
            Category.icon,
            Category.exclude_from_spending,
        )
        .join(Category, Category.id == Transaction.category_id)
        .where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
        )
        .order_by(Category.type, Transaction.transaction_date, Transaction.id)
    )
    out: list[SpendRow] = []
    for row in session.execute(stmt).all():
        native = Decimal(row.amount_native)
        usd, approximate = _usd_on_date(
            native, row.currency.value, row.transaction_date, rates
        )
        out.append(SpendRow(
            transaction_id=row.id,
            transaction_date=row.transaction_date,
            category_id=row.category_id,
            category_name=row.name,
            category_type=row.type.value,
            category_color=row.color,
            category_icon=row.icon,
            excluded_from_spending=row.exclude_from_spending,
            currency=row.currency.value,
            amount_native=native,
            amount_usd=usd,
            approximate=approximate,
        ))
    return out


# The order the old SQL `ORDER BY Category.type` produced: a Postgres enum
# sorts by declaration order, not alphabetically. Grouping moved into Python,
# so the order has to move with it or the category chart reshuffles for no
# reason.
_CATEGORY_TYPE_ORDER = {t.value: i for i, t in enumerate(CategoryType)}


def _category_buckets(rows: list[SpendRow]) -> list[CategoryBucket]:
    """Group already-converted rows by category, ordered as the SQL GROUP BY
    used to order them (category type, then name)."""
    buckets: dict[int, CategoryBucket] = {}
    for r in rows:
        bucket = buckets.get(r.category_id)
        if bucket is None:
            bucket = buckets[r.category_id] = CategoryBucket(
                category_id=r.category_id,
                category_name=r.category_name,
                category_type=r.category_type,
                color=r.category_color,
                amount_usd=ZERO,
                transaction_count=0,
                category_icon=r.category_icon,
            )
        bucket.amount_usd += r.amount_usd
        bucket.transaction_count += 1
        bucket.approximate = bucket.approximate or r.approximate
    ordered = sorted(
        buckets.values(),
        key=lambda b: (_CATEGORY_TYPE_ORDER.get(b.category_type, 0), b.category_name),
    )
    for b in ordered:
        b.amount_usd = _q(b.amount_usd)
    return ordered


def _taxes_by_salary(rows: list[SpendRow]) -> dict[str, Decimal]:
    """Split the Taxes category total by transaction currency, keyed by
    income-source name. Heuristic: USD-denominated taxes (Federal / State
    withholdings) attach to PARTNER_SALARY (the US paycheck), BRL taxes
    (SIMPLES NACIONAL / DARF UNIFICADO) attach to PRIMARY_SALARY (the BR
    paycheck). Returns USD-equivalent totals.

    Used by the monthly report to display a "taxes: $XXX" hint under each
    salary bucket so the user can sanity-check the withholding ratio. Derived
    from the same converted rows as the category buckets, so the hint and the
    Taxes bucket cannot drift apart.
    """
    out: dict[str, Decimal] = {
        IncomeSource.PARTNER_SALARY.value: ZERO,
        IncomeSource.PRIMARY_SALARY.value: ZERO,
    }
    for r in rows:
        if r.category_name != TAXES_CATEGORY_NAME:
            continue
        if r.currency == Currency.USD.value:
            out[IncomeSource.PARTNER_SALARY.value] += r.amount_usd
        else:  # BRL → primary earner
            out[IncomeSource.PRIMARY_SALARY.value] += r.amount_usd
    return {k: _q(v) for k, v in out.items()}


@dataclass
class MonthSpending:
    """One month's spending, converted once and shared by every surface.

    Built by `_month_spending` and carried through `_month_totals`, so the
    monthly KPI, the category chart, the transaction tables, the excluded
    section and the annual roll-up are all views of the same conversion. A
    surface that re-derived its own would be free to disagree, which is the
    class of bug this type exists to make impossible.
    """

    rows: list[SpendRow]
    by_category: list[CategoryBucket]
    excluded_categories: list[CategoryBucket]
    taxes_by_salary: dict[str, Decimal]
    approximate: bool

    def by_transaction(self) -> dict[int, SpendRow]:
        return {r.transaction_id: r for r in self.rows}


def _month_spending(
    session: Session, year: int, month: int, rates: DatedRateCache
) -> MonthSpending:
    rows = _spend_rows(session, year, month, rates)
    counted = [r for r in rows if not r.excluded_from_spending]
    excluded = [r for r in rows if r.excluded_from_spending]
    return MonthSpending(
        rows=rows,
        by_category=_category_buckets(counted),
        excluded_categories=_category_buckets(excluded),
        taxes_by_salary=_taxes_by_salary(rows),
        # Only the counted rows: the flag sits beside the spending total, and an
        # excluded category (loan principal, transfers) is not in that total.
        approximate=any(r.approximate for r in counted),
    )


def _fetch_transaction_details(
    session: Session,
    year: int,
    month: int,
    spending: MonthSpending,
    *,
    excluded: bool = False,
) -> list[TransactionDetail]:
    """The per-transaction table under the monthly report.

    Amount and USD figure are read from `spending`, never recomputed: a detail
    row that priced itself could differ from the category bucket it sits under.
    Only the presentational fields (merchant, account, owner) come from the ORM
    rows loaded here.
    """
    converted = spending.by_transaction()
    rows = session.scalars(
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
            Category.exclude_from_spending.is_(excluded),
        )
        .order_by(Category.type, Transaction.transaction_date, Transaction.id)
    ).all()
    out: list[TransactionDetail] = []
    for t in rows:
        # Both queries run in the same transaction over the same month, and the
        # converted set covers excluded and counted rows alike, so a missing key
        # is a broken invariant rather than a data case. Let it raise.
        row = converted[t.id]
        out.append(TransactionDetail(
            id=t.id,
            transaction_date=t.transaction_date,
            merchant_name=t.merchant.name,
            payment_method_name=t.payment_method.name,
            category_id=t.category_id,
            category_name=t.category.name,
            category_type=t.category.type.value,
            category_color=t.category.color,
            category_icon=t.category.icon,
            owner_name=t.created_by.name if t.created_by else None,
            amount_native=row.amount_native,
            currency=t.currency.value,
            amount_usd=_q(row.amount_usd),
            installment_current=t.installment_current,
            installment_total=t.installment_total,
            description=t.description,
            pending=t.pending,
        ))
    return out


def _compute_savings_at(session: Session, as_of: datetime, effective: Decimal) -> Decimal:
    sub = (
        select(
            SavingsSnapshot.account_name,
            func.max(SavingsSnapshot.recorded_at).label("max_at"),
        )
        .where(SavingsSnapshot.recorded_at <= as_of)
        .group_by(SavingsSnapshot.account_name)
        .subquery()
    )
    snaps = session.scalars(
        select(SavingsSnapshot).join(
            sub,
            (SavingsSnapshot.account_name == sub.c.account_name)
            & (SavingsSnapshot.recorded_at == sub.c.max_at),
        )
    ).all()
    total = ZERO
    for s in snaps:
        total += _to_usd(s.balance, s.currency.value, effective)
    return total


def _compute_debt_at(session: Session, as_of: datetime, effective: Decimal) -> Decimal:
    """Total debt as of `as_of`: cards (live-derived) plus the car loan.

    `credit_card_balances` is a sparse snapshot, not a running total, so the
    latest row at-or-before `as_of` is stale by whatever posted after it.
    /debts and /warnings have derived past it since 2026-06-04; this report
    did not, and the two disagreed by exactly the charges posted since each
    card's last recorded balance. Same derivation now, via the same helper, so
    there is one definition of card debt rather than two.

    The delta window is clamped to today: for the current month `as_of` is a
    future end-of-month, and future-dated FIXED projections must not inflate
    the figure (the clamp is what `post_balance_delta` already enforces for
    /debts). Past months are unaffected — their `as_of` is already behind us.
    """
    from app.services.debts import post_balance_delta

    sub = (
        select(
            CreditCardBalance.payment_method_id,
            func.max(CreditCardBalance.recorded_at).label("max_at"),
        )
        .where(CreditCardBalance.recorded_at <= as_of)
        .group_by(CreditCardBalance.payment_method_id)
        .subquery()
    )
    rows = session.execute(
        select(CreditCardBalance, PaymentMethod.currency)
        .join(
            sub,
            (CreditCardBalance.payment_method_id == sub.c.payment_method_id)
            & (CreditCardBalance.recorded_at == sub.c.max_at),
        )
        .join(PaymentMethod, PaymentMethod.id == CreditCardBalance.payment_method_id)
    ).all()
    cutoff = min(as_of.date(), date_type.today())
    total = ZERO
    for ccb, currency in rows:
        live = Decimal(ccb.balance) + post_balance_delta(
            session,
            payment_method_id=ccb.payment_method_id,
            after=ccb.recorded_at,
            today=cutoff,
        )
        total += _to_usd(live, currency.value, effective)

    car = session.scalar(
        select(CarLoanPayment)
        .where(CarLoanPayment.posting_date <= as_of.date())
        .order_by(CarLoanPayment.posting_date.desc(), CarLoanPayment.id.desc())
        .limit(1)
    )
    if car is not None:
        total += Decimal(car.new_balance)
    return total


def _live_totals(session: Session, year: int, month: int) -> tuple[MonthTotals, list[IncomeBucket], MonthSpending]:
    rate = _resolve_rate(session, year, month)
    month_end = _month_end_rate(session, year, month)
    effective = month_end.effective

    # One rate cache for the whole month, warmed in a single query before
    # anything asks it a question. Income (per receipt) and spending (per
    # transaction) then share it, so the render costs one rate query for the
    # month rather than one per date money moved.
    first_day, last_day = _month_bounds(year, month)
    rates = DatedRateCache(session)
    rates.warm(first_day, last_day)

    income_buckets, by_source = _compute_income(
        session, year, month, month_end, rates=rates
    )
    spending = _month_spending(session, year, month, rates)
    by_category = spending.by_category
    taxes_by_salary = spending.taxes_by_salary

    primary = by_source.get(IncomeSource.PRIMARY_SALARY.value, ZERO)
    partner = by_source.get(IncomeSource.PARTNER_SALARY.value, ZERO)
    rents = by_source.get(IncomeSource.RENTS_BRAZIL.value, ZERO)
    extra = (
        by_source.get(IncomeSource.EXTRA_USD.value, ZERO)
        + by_source.get(IncomeSource.EXTRA_BRL.value, ZERO)
    )
    gross = primary + partner + rents + extra

    taxes = ZERO
    fixed = ZERO
    variable = ZERO
    for c in by_category:
        if c.category_name == TAXES_CATEGORY_NAME:
            taxes += c.amount_usd
        elif c.category_type == CategoryType.FIXED.value:
            fixed += c.amount_usd
        else:
            variable += c.amount_usd

    net_income = gross - taxes
    total_spending = fixed + variable
    surplus = net_income - total_spending

    end_of_month = datetime.combine(last_day, time(23, 59, 59))
    savings_total = _compute_savings_at(session, end_of_month, effective)
    debt_total = _compute_debt_at(session, end_of_month, effective)
    net_worth = savings_total - debt_total

    # Assets are point-in-time values (manually updated), not historical —
    # use the current row for every month. Past months don't get re-rated
    # because the snapshot path already captured a frozen value.
    from app.services.assets import assets_total_usd
    assets_total = assets_total_usd(session, effective)
    total_worth = net_worth + assets_total

    can_finalize, blocked_reason = _finalize_status(session, year, month)

    totals = MonthTotals(
        year=year,
        month=month,
        is_finalized=False,
        rate_id=rate.id if rate else None,
        rate_effective=Decimal(rate.effective) if rate else None,
        rate_date=rate.rate_date if rate else None,
        primary_salary_usd=_q(primary),
        partner_salary_usd=_q(partner),
        rents_brazil_usd=_q(rents),
        extra_income_usd=_q(extra),
        gross_income_usd=_q(gross),
        taxes_usd=_q(taxes),
        net_income_usd=_q(net_income),
        taxes_partner_usd=taxes_by_salary.get(IncomeSource.PARTNER_SALARY.value, ZERO),
        taxes_primary_usd=taxes_by_salary.get(IncomeSource.PRIMARY_SALARY.value, ZERO),
        fixed_spending_usd=_q(fixed),
        variable_spending_usd=_q(variable),
        total_spending_usd=_q(total_spending),
        surplus_usd=_q(surplus),
        total_savings_usd=_q(savings_total),
        total_debt_usd=_q(debt_total),
        net_worth_usd=_q(net_worth),
        assets_total_usd=_q(assets_total),
        total_worth_usd=_q(total_worth),
        can_finalize=can_finalize,
        finalize_blocked_reason=blocked_reason,
        income_rate_approximate=any(b.approximate for b in income_buckets),
        spending_rate_approximate=spending.approximate,
    )
    return totals, income_buckets, spending


def _snapshot_to_totals(
    snap: MonthlySnapshot,
    assets_total: Decimal = ZERO,
    *,
    taxes_by_salary: dict[str, Decimal] | None = None,
) -> MonthTotals:
    rate = snap.exchange_rate
    gross = (
        Decimal(snap.primary_salary_usd)
        + Decimal(snap.partner_salary_usd)
        + Decimal(snap.rents_brazil_usd)
        + Decimal(snap.extra_income_usd)
    )
    taxes = Decimal(snap.taxes_usd)
    fixed = Decimal(snap.fixed_spending_usd)
    variable = Decimal(snap.variable_spending_usd)
    total_spending = fixed + variable
    # net and surplus are derived from components; stored values can drift
    # (older migration paths wrote net/surplus from v1 directly). Always
    # re-derive at read time so the displayed totals are self-consistent.
    net_income = gross - taxes
    surplus = net_income - total_spending
    return MonthTotals(
        year=snap.year,
        month=snap.month,
        is_finalized=snap.is_finalized,
        rate_id=snap.exchange_rate_id,
        rate_effective=Decimal(rate.effective) if rate else None,
        rate_date=rate.rate_date if rate else None,
        primary_salary_usd=Decimal(snap.primary_salary_usd),
        partner_salary_usd=Decimal(snap.partner_salary_usd),
        rents_brazil_usd=Decimal(snap.rents_brazil_usd),
        extra_income_usd=Decimal(snap.extra_income_usd),
        gross_income_usd=_q(gross),
        taxes_usd=_q(taxes),
        net_income_usd=_q(net_income),
        taxes_partner_usd=(taxes_by_salary or {}).get(IncomeSource.PARTNER_SALARY.value, ZERO),
        taxes_primary_usd=(taxes_by_salary or {}).get(IncomeSource.PRIMARY_SALARY.value, ZERO),
        fixed_spending_usd=_q(fixed),
        variable_spending_usd=_q(variable),
        total_spending_usd=_q(total_spending),
        surplus_usd=_q(surplus),
        total_savings_usd=Decimal(snap.total_savings_usd),
        total_debt_usd=Decimal(snap.total_debt_usd),
        net_worth_usd=Decimal(snap.net_worth_usd),
        assets_total_usd=_q(assets_total),
        total_worth_usd=_q(Decimal(snap.net_worth_usd) + assets_total),
        can_finalize=False,
        finalize_blocked_reason=(
            "Month is already finalized." if snap.is_finalized else None
        ),
    )


def _month_totals(session: Session, year: int, month: int) -> tuple[
    MonthTotals, list[IncomeBucket], MonthSpending
]:
    """Returns totals + supporting buckets. Income/category buckets are
    always live-computed (snapshots don't store them per-row), but totals
    come from the snapshot when finalized.

    A finalized month therefore shows frozen spending TOTALS beside a live
    per-transaction-date breakdown, and the two can differ by the rate grain:
    the totals were frozen when the month was closed at one month-end rate,
    while the breakdown re-derives at each row's own date. That is the same
    kind of gap a transaction edited after closing already opened, it is
    visible rather than silent, and `close_month` freezes the per-date figure
    from now on, so reopening and re-closing a month realigns the two.
    """
    snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == year, MonthlySnapshot.month == month
        )
    )
    if snap is not None and snap.is_finalized:
        rate = snap.exchange_rate
        # The rate frozen into the snapshot when the month was closed, not
        # today's. Income conversion gets the same one as its month-end
        # fallback, and the balances are valued with it, so a finalized month
        # keeps reporting what it was closed at.
        month_end = (
            DatedRate.from_row(rate)
            if rate is not None
            else _month_end_rate(session, year, month)
        )
        effective = month_end.effective
        first_day, last_day = _month_bounds(year, month)
        rates = DatedRateCache(session)
        rates.warm(first_day, last_day)
        from app.services.assets import assets_total_usd
        assets_total = assets_total_usd(session, effective)
        spending = _month_spending(session, year, month, rates)
        totals = _snapshot_to_totals(
            snap, assets_total=assets_total, taxes_by_salary=spending.taxes_by_salary
        )
        income_buckets, _ = _compute_income(
            session, year, month, month_end, rates=rates
        )
        totals.income_rate_approximate = any(b.approximate for b in income_buckets)
        totals.spending_rate_approximate = spending.approximate
        return totals, income_buckets, spending
    return _live_totals(session, year, month)


@dataclass
class AnnualCategoryBucket:
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    category_icon: str | None = None
    # Any month's contribution to this category converted with a fallback rate.
    approximate: bool = False


@dataclass
class AnnualReport:
    year: int
    months: list[MonthTotals]
    gross_income_usd: Decimal
    taxes_usd: Decimal
    net_income_usd: Decimal
    fixed_spending_usd: Decimal
    variable_spending_usd: Decimal
    total_spending_usd: Decimal
    surplus_usd: Decimal
    end_savings_usd: Decimal       # net worth at the close of the latest month with data
    end_debt_usd: Decimal
    end_net_worth_usd: Decimal
    end_assets_usd: Decimal
    end_total_worth_usd: Decimal
    top_categories: list[AnnualCategoryBucket]
    # Any month in the year whose spending conversion used a fallback rate. The
    # annual page shows totals and no per-transaction detail, so without this
    # the estimate would be invisible on the one surface that aggregates it.
    spending_rate_approximate: bool = False


def annual_report(session: Session, year: int) -> AnnualReport:
    months: list[MonthTotals] = []
    category_totals: dict[int, AnnualCategoryBucket] = {}
    for m in range(1, 13):
        totals, _, spending = _month_totals(session, year, m)
        months.append(totals)
        # Top-15 view: drop Taxes from the spending list (it's surfaced in its
        # own KPI / chart). Sum across the 12 months keyed on category_id. The
        # buckets are the monthly report's own, so a category's annual figure is
        # the sum of the twelve figures its monthly pages show.
        for c in spending.by_category:
            if c.category_name == TAXES_CATEGORY_NAME:
                continue
            existing = category_totals.get(c.category_id)
            if existing is None:
                category_totals[c.category_id] = AnnualCategoryBucket(
                    category_id=c.category_id,
                    category_name=c.category_name,
                    category_type=c.category_type,
                    color=c.color,
                    amount_usd=c.amount_usd,
                    category_icon=c.category_icon,
                    approximate=c.approximate,
                )
            else:
                existing.amount_usd += c.amount_usd
                existing.approximate = existing.approximate or c.approximate

    top_categories = sorted(
        category_totals.values(),
        key=lambda b: b.amount_usd,
        reverse=True,
    )[:15]
    # Quantize amounts for display.
    for b in top_categories:
        b.amount_usd = _q(b.amount_usd)

    # Months without activity look like zeros — filter them OUT of the aggregates
    # so a YTD view in March 2026 doesn't dilute averages with empty April-Dec.
    active = [
        t for t in months
        if (t.gross_income_usd or t.total_spending_usd or t.total_savings_usd or t.total_debt_usd)
    ]

    def _sum(attr: str) -> Decimal:
        return sum((getattr(t, attr) for t in active), start=Decimal("0"))

    end = active[-1] if active else months[0]
    return AnnualReport(
        year=year,
        months=months,
        gross_income_usd=_q(_sum("gross_income_usd")),
        taxes_usd=_q(_sum("taxes_usd")),
        net_income_usd=_q(_sum("net_income_usd")),
        fixed_spending_usd=_q(_sum("fixed_spending_usd")),
        variable_spending_usd=_q(_sum("variable_spending_usd")),
        total_spending_usd=_q(_sum("total_spending_usd")),
        surplus_usd=_q(_sum("surplus_usd")),
        end_savings_usd=end.total_savings_usd,
        end_debt_usd=end.total_debt_usd,
        end_net_worth_usd=end.net_worth_usd,
        end_assets_usd=end.assets_total_usd,
        end_total_worth_usd=end.total_worth_usd,
        top_categories=top_categories,
        spending_rate_approximate=any(t.spending_rate_approximate for t in months),
    )


def monthly_report(session: Session, year: int, month: int) -> MonthlyReport:
    totals, income, spending = _month_totals(session, year, month)
    by_category = spending.by_category

    fixed_cats = [c for c in by_category
                  if c.category_type == CategoryType.FIXED.value
                  and c.category_name != TAXES_CATEGORY_NAME]
    variable_cats = [c for c in by_category
                     if c.category_type == CategoryType.VARIABLE.value]

    all_tx = _fetch_transaction_details(session, year, month, spending)
    fixed_tx = [t for t in all_tx
                if t.category_type == CategoryType.FIXED.value
                and t.category_name != TAXES_CATEGORY_NAME]
    variable_tx = [t for t in all_tx
                   if t.category_type == CategoryType.VARIABLE.value]

    # Excluded-from-spending categories (loan principal, transfers): surfaced as
    # a separate section so the cash movement is visible without inflating spend.
    excluded_cats = spending.excluded_categories
    excluded_tx = _fetch_transaction_details(session, year, month, spending, excluded=True)
    excluded_total = sum((c.amount_usd for c in excluded_cats), ZERO)

    prior_year, prior_month = _prev_month(year, month)
    prior_snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == prior_year, MonthlySnapshot.month == prior_month
        )
    )
    prior: MonthTotals | None
    if prior_snap is not None and prior_snap.is_finalized:
        # The same path the reported month took, rather than a second
        # assembly of the same snapshot: the diff badges then compare figures
        # built by one conversion, and a change to that conversion cannot move
        # this month's number without moving last month's.
        prior = _month_totals(session, prior_year, prior_month)[0]
    else:
        # only compute prior live if there is any data at all to avoid empty noise
        has_data = session.scalar(
            select(func.count(Transaction.id)).where(
                extract("year", Transaction.transaction_date) == prior_year,
                extract("month", Transaction.transaction_date) == prior_month,
            )
        )
        prior = _live_totals(session, prior_year, prior_month)[0] if has_data else None

    return MonthlyReport(
        totals=totals,
        income=income,
        by_category=by_category,
        fixed_categories=fixed_cats,
        variable_categories=variable_cats,
        fixed_transactions=fixed_tx,
        variable_transactions=variable_tx,
        excluded_categories=excluded_cats,
        excluded_transactions=excluded_tx,
        excluded_total_usd=excluded_total,
        prior=prior,
    )


def default_month(session: Session) -> tuple[int, int]:
    """Latest month with transactions that is NOT finalized (open month).
    Falls back to the latest month with any transactions, then current calendar.

    Capped at today's calendar month so future-dated rows (rolled-over
    installments, manual-add Split into N months) don't drag the picker to a
    month the user hasn't reached yet.
    """
    today = date_type.today()
    # Pick the latest month with activity that is also <= today.
    latest = session.execute(
        select(
            extract("year", Transaction.transaction_date).label("y"),
            extract("month", Transaction.transaction_date).label("m"),
        )
        .where(Transaction.transaction_date <= today)
        .order_by(Transaction.transaction_date.desc())
        .limit(1)
    ).first()
    if latest is None:
        return today.year, today.month
    y, m = int(latest.y), int(latest.m)
    finalized = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == y,
            MonthlySnapshot.month == m,
            MonthlySnapshot.is_finalized.is_(True),
        )
    )
    if finalized is None:
        return y, m
    # Latest month-with-data is finalized — fall back to today's calendar month.
    return today.year, today.month


# ---------- Close Month ----------

class CloseMonthError(ValueError):
    """Raised when a Close Month attempt cannot proceed."""


def close_month(session: Session, year: int, month: int) -> MonthlyReport:
    """Freeze a live month into a finalized monthly_snapshots row.

    - Requires an exchange_rates row whose rate_date is inside [first_day, last_day].
    - If a non-finalized snapshot already exists, update it in place.
    - If the snapshot is already finalized, refuse (use a future un-finalize flow
      to amend; we don't want silent overwrites of historical numbers).
    """
    rate = _rate_inside_month(session, year, month)
    if rate is None:
        raise CloseMonthError(
            "No exchange-rate row inside this month. "
            "Add one on /exchange-rates before closing."
        )

    existing = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == year, MonthlySnapshot.month == month
        )
    )
    if existing is not None and existing.is_finalized:
        raise CloseMonthError("Month is already finalized.")

    live, _, _ = _live_totals(session, year, month)

    snap = existing if existing is not None else MonthlySnapshot(year=year, month=month)
    snap.primary_salary_usd = live.primary_salary_usd
    snap.partner_salary_usd = live.partner_salary_usd
    snap.rents_brazil_usd = live.rents_brazil_usd
    snap.extra_income_usd = live.extra_income_usd
    snap.fixed_spending_usd = live.fixed_spending_usd
    snap.variable_spending_usd = live.variable_spending_usd
    snap.taxes_usd = live.taxes_usd
    snap.net_income_usd = live.net_income_usd
    snap.surplus_usd = live.surplus_usd
    snap.total_savings_usd = live.total_savings_usd
    snap.total_debt_usd = live.total_debt_usd
    snap.net_worth_usd = live.net_worth_usd
    snap.exchange_rate_id = rate.id
    snap.is_finalized = True
    snap.finalized_at = datetime.now(timezone.utc)

    if existing is None:
        session.add(snap)
    session.commit()

    return monthly_report(session, year, month)


def reopen_month(session: Session, year: int, month: int) -> MonthlyReport:
    """Flip a finalized snapshot back to live so the user can amend the month
    and re-close it. The row stays in `monthly_snapshots` so a re-close updates
    in place rather than inserting a duplicate."""
    snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == year, MonthlySnapshot.month == month
        )
    )
    if snap is None or not snap.is_finalized:
        raise CloseMonthError("Month is not finalized.")
    snap.is_finalized = False
    snap.finalized_at = None
    session.commit()
    return monthly_report(session, year, month)
