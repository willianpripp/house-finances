"""Home dashboard stats aggregator.

Powers the launcher's per-button stat preview at `GET /api/home/stats`.
Each field is what the corresponding nav tile shows underneath its label
(e.g. Savings shows the USD-equivalent total + MoM delta).

Cheap to compute — reuses already-existing per-domain services. No
recomputation of finalized snapshots, no chart-data joins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    CategorizationRule,
    Currency,
    ExchangeRate,
    IncomeEntry,
    Transaction,
)
from app.services.assets import assets_total_usd
from app.services.debts import current_card_balances
from app.services.parsers.registry import card_specs, checking_specs
from app.services.reports import default_month, monthly_report
from app.services.savings import current_balances
from app.services.warnings import summarize as summarize_warnings


def _latest_effective_rate(session: Session) -> Decimal:
    rate = session.scalar(
        select(ExchangeRate).order_by(ExchangeRate.rate_date.desc()).limit(1)
    )
    return Decimal(rate.effective) if rate else Decimal("1")


@dataclass
class HomeStat:
    """Stat preview shown under a launcher button. `value` is the human
    string ("$7,805", "12 parsers"); `delta_pct` is the MoM badge if any."""
    value: str
    delta_pct: float | None = None
    severity: str = "neutral"  # neutral | good | warn | bad


@dataclass
class HomeStats:
    year: int
    month: int
    monthly: HomeStat
    annual: HomeStat
    savings: HomeStat
    debts: HomeStat
    assets: HomeStat
    warnings: HomeStat
    transactions: HomeStat
    income: HomeStat
    imports_: HomeStat = field(default_factory=lambda: HomeStat(value=""))
    rates: HomeStat = field(default_factory=lambda: HomeStat(value=""))
    rules: HomeStat = field(default_factory=lambda: HomeStat(value=""))


def _fmt_usd(amount: Decimal) -> str:
    n = float(amount)
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:,.0f}"


def _parsers_count() -> int:
    """How many statement layouts this tree can read. Counted from the parser
    registry rather than typed in, so a deployment shipping a subset of the
    parser modules reports its own number."""
    return len({spec.parse for spec in card_specs() + checking_specs()})


def _usd(amount: Decimal | None, currency: str, effective: Decimal | None) -> Decimal | None:
    """Convert a native-currency amount to USD using the same rule the
    savings/debts services use, so the two agree."""
    if amount is None:
        return None
    if currency == Currency.USD.value:
        return amount
    return amount / effective if effective else amount


def _mom_of_totals(rows, effective: Decimal | None) -> float | None:
    """Month-over-month change of the TOTAL, in percent.

    Deliberately not the average of each row's `mom_pct`, which is what this
    used to do. Averaging percentages lets a trivial account dominate: an
    account holding a few cents can swing by thousands of percent on a $10
    deposit and produce a headline like "savings ▲ 8000% vs last month" while
    the real total moved about 19%. A percentage is only meaningful weighted
    by the amount it applies to, which is what dividing the summed change by
    the summed base does.

    Only rows that HAVE a previous balance count, on both sides of the
    division. Including an account's current balance while it contributes
    nothing to the base would report growth that is really just a new account
    appearing.
    """
    now_total = Decimal("0")
    prev_total = Decimal("0")
    for r in rows:
        if r.prev_balance is None:
            continue
        now = _usd(r.balance, r.currency, effective)
        prev = _usd(r.prev_balance, r.currency, effective)
        if now is None or prev is None:
            continue
        now_total += now
        prev_total += prev
    if prev_total == 0:
        return None
    return round(float((now_total - prev_total) / abs(prev_total) * 100), 1)


def compute_home_stats(session: Session) -> HomeStats:
    today = date.today()
    year, month = default_month(session)

    # Monthly + annual reports — reuse the live computation. monthly_report()
    # already handles the finalized/snapshot fallback.
    report = monthly_report(session, year, month)
    net_income = report.totals.net_income_usd

    # YTD surplus (cheap: sum of surplus_usd across all months in current
    # year). Don't run annual_report() — too heavy for a homepage hit.
    ytd_surplus = Decimal("0")
    for m in range(1, today.month + 1):
        try:
            r = monthly_report(session, today.year, m)
            ytd_surplus += r.totals.surplus_usd
        except Exception:
            pass

    # Needed before the deltas below, which convert previous balances to USD.
    effective = _latest_effective_rate(session)

    savings_result = current_balances(session)
    savings_total_usd = sum(
        (Decimal(r.usd_equivalent) for r in savings_result.rows if r.usd_equivalent is not None),
        Decimal("0"),
    )
    mom_savings = _mom_of_totals(savings_result.rows, effective)

    debts_result = current_card_balances(session)
    debts_total_usd = sum(
        (Decimal(r.usd_equivalent) for r in debts_result.rows if r.usd_equivalent is not None),
        Decimal("0"),
    )
    mom_debts = _mom_of_totals(debts_result.rows, effective)
    assets_total = assets_total_usd(session, effective)
    assets_count = session.scalar(select(func.count(Asset.id))) or 0

    txn_count = session.scalar(
        select(func.count(Transaction.id)).where(
            extract("year", Transaction.transaction_date) == year,
            extract("month", Transaction.transaction_date) == month,
        )
    ) or 0

    income_sources = session.scalar(
        select(func.count(func.distinct(IncomeEntry.source))).where(
            IncomeEntry.year == year, IncomeEntry.month == month
        )
    ) or 0

    rates_count = session.scalar(select(func.count(ExchangeRate.id))) or 0
    rules_count = session.scalar(select(func.count(CategorizationRule.id))) or 0

    # Warnings: overdraft + expiring contract count.
    summary = summarize_warnings(session, today=today)
    warnings_count = summary.overdraft_count + summary.expiring_count
    if summary.overdraft_count > 0:
        warnings_severity = "bad"
    elif summary.expiring_count > 0:
        warnings_severity = "warn"
    else:
        warnings_severity = "good"

    return HomeStats(
        year=year,
        month=month,
        monthly=HomeStat(
            value=_fmt_usd(net_income),
            severity="good" if net_income > 0 else "neutral",
        ),
        annual=HomeStat(value=f"YTD {_fmt_usd(ytd_surplus)}"),
        savings=HomeStat(
            value=_fmt_usd(savings_total_usd),
            delta_pct=round(mom_savings, 1) if mom_savings is not None else None,
            severity="good" if (mom_savings or 0) >= 0 else "warn",
        ),
        debts=HomeStat(
            value=_fmt_usd(debts_total_usd),
            delta_pct=round(mom_debts, 1) if mom_debts is not None else None,
            # For debts the semantic inverts: rising debt is bad.
            severity="warn" if (mom_debts or 0) > 0 else "good",
        ),
        assets=HomeStat(
            value=f"{_fmt_usd(assets_total)} ({assets_count})",
            severity="neutral",
        ),
        warnings=HomeStat(
            value=str(warnings_count),
            severity=warnings_severity,
        ),
        transactions=HomeStat(value=f"{txn_count} this month"),
        income=HomeStat(value=f"{income_sources} sources"),
        imports_=HomeStat(value=f"{_parsers_count()} parsers"),
        rates=HomeStat(value=f"{rates_count} entries"),
        rules=HomeStat(value=f"{rules_count} rules"),
    )
