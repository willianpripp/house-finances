"""Monthly and annual report aggregations.

Finalized months are read straight from monthly_snapshots — those values are
authoritative and frozen with the exchange rate active that month.

Live months (no snapshot yet, or is_finalized=False) are computed on the
fly from transactions/income/balances. The conversion rule:

  - amounts in transaction.currency stay in their native unit
  - to compute USD-denominated totals (income, spending, surplus, debt,
    savings, net worth), BRL amounts are divided by exchange_rate.effective
    for the month being reported
  - if no exchange_rate row exists for the month, we use the latest one
    whose rate_date <= last day of the month
  - per-installment rows: we use COALESCE(installment_value, amount), so
    a 12x purchase contributes only its monthly slice

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

from sqlalchemy import and_, case, extract, func, select
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


@dataclass
class CategoryBucket:
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    transaction_count: int
    category_icon: str | None = None


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

def _compute_income(session: Session, year: int, month: int, effective: Decimal) -> tuple[
    list[IncomeBucket], dict[str, Decimal]
]:
    rows = session.scalars(
        select(IncomeEntry).where(
            and_(IncomeEntry.year == year, IncomeEntry.month == month)
        )
    ).all()
    buckets: list[IncomeBucket] = []
    by_source: dict[str, Decimal] = {s.value: ZERO for s in IncomeSource}
    for r in rows:
        usd = _to_usd(r.amount, r.currency.value, effective)
        buckets.append(IncomeBucket(
            source=r.source.value,
            amount_native=Decimal(r.amount),
            currency=r.currency.value,
            amount_usd=_q(usd),
        ))
        by_source[r.source.value] = by_source.get(r.source.value, ZERO) + usd
    return buckets, by_source


def _compute_taxes_by_salary(
    session: Session, year: int, month: int, effective: Decimal
) -> dict[str, Decimal]:
    """Split the Taxes category total by transaction currency, keyed by
    income-source name. Heuristic: USD-denominated taxes (Federal / State
    withholdings) attach to PARTNER_SALARY (the US paycheck), BRL taxes
    (SIMPLES NACIONAL / DARF UNIFICADO) attach to PRIMARY_SALARY (the BR
    paycheck). Returns USD-equivalent totals.

    Used by the monthly report to display a "taxes: $XXX" hint under each
    salary bucket so the user can sanity-check the withholding ratio.
    """
    amount_expr = func.coalesce(Transaction.installment_value, Transaction.amount)
    rows = session.execute(
        select(Transaction.currency, func.sum(amount_expr))
        .join(Category, Category.id == Transaction.category_id)
        .where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
            Category.name == TAXES_CATEGORY_NAME,
        )
        .group_by(Transaction.currency)
    ).all()
    out: dict[str, Decimal] = {
        IncomeSource.PARTNER_SALARY.value: ZERO,
        IncomeSource.PRIMARY_SALARY.value: ZERO,
    }
    for currency, total in rows:
        if total is None:
            continue
        usd = _to_usd(Decimal(total), currency.value, effective)
        if currency == Currency.USD:
            out[IncomeSource.PARTNER_SALARY.value] += usd
        else:  # BRL → primary earner
            out[IncomeSource.PRIMARY_SALARY.value] += usd
    return {k: _q(v) for k, v in out.items()}


def _compute_spending_by_category(
    session: Session, year: int, month: int, effective: Decimal, *, excluded: bool = False
) -> list[CategoryBucket]:
    amount_expr = func.coalesce(Transaction.installment_value, Transaction.amount)
    usd_expr = case(
        (Transaction.currency == Currency.BRL, amount_expr / Decimal(effective)),
        else_=amount_expr,
    )
    stmt = (
        select(
            Category.id,
            Category.name,
            Category.type,
            Category.color,
            Category.icon,
            func.sum(usd_expr).label("amount_usd"),
            func.count(Transaction.id).label("count"),
        )
        .join(Category, Category.id == Transaction.category_id)
        .where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
            Category.exclude_from_spending.is_(excluded),
        )
        .group_by(Category.id, Category.name, Category.type, Category.color, Category.icon)
        .order_by(Category.type, Category.name)
    )
    return [
        CategoryBucket(
            category_id=row.id,
            category_name=row.name,
            category_type=row.type.value,
            color=row.color,
            amount_usd=_q(Decimal(row.amount_usd)),
            transaction_count=row.count,
            category_icon=row.icon,
        )
        for row in session.execute(stmt).all()
    ]


def _fetch_transaction_details(
    session: Session, year: int, month: int, effective: Decimal, *, excluded: bool = False
) -> list[TransactionDetail]:
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
        amount = Decimal(t.installment_value) if t.installment_value is not None else Decimal(t.amount)
        usd = _to_usd(amount, t.currency.value, effective)
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
            amount_native=amount,
            currency=t.currency.value,
            amount_usd=_q(usd),
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
    total = ZERO
    for ccb, currency in rows:
        total += _to_usd(ccb.balance, currency.value, effective)

    car = session.scalar(
        select(CarLoanPayment)
        .where(CarLoanPayment.posting_date <= as_of.date())
        .order_by(CarLoanPayment.posting_date.desc(), CarLoanPayment.id.desc())
        .limit(1)
    )
    if car is not None:
        total += Decimal(car.new_balance)
    return total


def _live_totals(session: Session, year: int, month: int) -> tuple[MonthTotals, list[IncomeBucket], list[CategoryBucket]]:
    rate = _resolve_rate(session, year, month)
    effective = Decimal(rate.effective) if rate is not None else Decimal("1")

    income_buckets, by_source = _compute_income(session, year, month, effective)
    by_category = _compute_spending_by_category(session, year, month, effective)
    taxes_by_salary = _compute_taxes_by_salary(session, year, month, effective)

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

    _, last_day = _month_bounds(year, month)
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
    )
    return totals, income_buckets, by_category


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
    MonthTotals, list[IncomeBucket], list[CategoryBucket]
]:
    """Returns totals + supporting buckets. Income/category buckets are
    always live-computed (snapshots don't store them per-row), but totals
    come from the snapshot when finalized."""
    snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == year, MonthlySnapshot.month == month
        )
    )
    if snap is not None and snap.is_finalized:
        rate = snap.exchange_rate
        effective = Decimal(rate.effective) if rate else _resolve_rate_value(session, year, month)
        from app.services.assets import assets_total_usd
        assets_total = assets_total_usd(session, effective)
        taxes_by_salary = _compute_taxes_by_salary(session, year, month, effective)
        totals = _snapshot_to_totals(snap, assets_total=assets_total, taxes_by_salary=taxes_by_salary)
        income_buckets, _ = _compute_income(session, year, month, effective)
        by_category = _compute_spending_by_category(session, year, month, effective)
        return totals, income_buckets, by_category
    return _live_totals(session, year, month)


def _resolve_rate_value(session: Session, year: int, month: int) -> Decimal:
    rate = _resolve_rate(session, year, month)
    return Decimal(rate.effective) if rate else Decimal("1")


@dataclass
class AnnualCategoryBucket:
    category_id: int
    category_name: str
    category_type: str
    color: str
    amount_usd: Decimal
    category_icon: str | None = None


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


def annual_report(session: Session, year: int) -> AnnualReport:
    months: list[MonthTotals] = []
    category_totals: dict[int, AnnualCategoryBucket] = {}
    for m in range(1, 13):
        totals, _, by_category = _month_totals(session, year, m)
        months.append(totals)
        # Top-15 view: drop Taxes from the spending list (it's surfaced in its
        # own KPI / chart). Sum across the 12 months keyed on category_id.
        for c in by_category:
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
                )
            else:
                existing.amount_usd += c.amount_usd

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
    )


def monthly_report(session: Session, year: int, month: int) -> MonthlyReport:
    totals, income, by_category = _month_totals(session, year, month)

    fixed_cats = [c for c in by_category
                  if c.category_type == CategoryType.FIXED.value
                  and c.category_name != TAXES_CATEGORY_NAME]
    variable_cats = [c for c in by_category
                     if c.category_type == CategoryType.VARIABLE.value]

    effective = (
        Decimal(totals.rate_effective) if totals.rate_effective is not None else Decimal("1")
    )
    all_tx = _fetch_transaction_details(session, year, month, effective)
    fixed_tx = [t for t in all_tx
                if t.category_type == CategoryType.FIXED.value
                and t.category_name != TAXES_CATEGORY_NAME]
    variable_tx = [t for t in all_tx
                   if t.category_type == CategoryType.VARIABLE.value]

    # Excluded-from-spending categories (loan principal, transfers): surfaced as
    # a separate section so the cash movement is visible without inflating spend.
    excluded_cats = _compute_spending_by_category(session, year, month, effective, excluded=True)
    excluded_tx = _fetch_transaction_details(session, year, month, effective, excluded=True)
    excluded_total = sum((c.amount_usd for c in excluded_cats), ZERO)

    prior_year, prior_month = _prev_month(year, month)
    prior_snap = session.scalar(
        select(MonthlySnapshot).where(
            MonthlySnapshot.year == prior_year, MonthlySnapshot.month == prior_month
        )
    )
    prior: MonthTotals | None
    if prior_snap is not None and prior_snap.is_finalized:
        prior_rate = prior_snap.exchange_rate
        prior_effective = (
            Decimal(prior_rate.effective)
            if prior_rate else _resolve_rate_value(session, prior_year, prior_month)
        )
        from app.services.assets import assets_total_usd
        prior_assets = assets_total_usd(session, prior_effective)
        prior_taxes_by_salary = _compute_taxes_by_salary(session, prior_year, prior_month, prior_effective)
        prior = _snapshot_to_totals(
            prior_snap, assets_total=prior_assets, taxes_by_salary=prior_taxes_by_salary
        )
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
