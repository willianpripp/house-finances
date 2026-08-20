"""Exchange rate storage with effective auto-computation.

The effective rate is what we apply to BRL→USD conversions in reports;
it bakes in the bank spread and the IOF tax. Defaults are the project's
standard 1.5% spread + 1.1% IOF (matches v1), now env-overridable via
app.config.settings (Phase C) rather than hardcoded here.

create_rate() has no HTTP surface (2026-08-20: manual entry removed, see
STATUS.md). Its only callers are scripts/refresh_exchange_rate.py's daily
refresh and --backfill mode, both of which always pass source="ptax". It
stays a function here (rather than inlining into the script) because both
call sites need the same never-overwrite check via get_rate_by_date first.

`rate_for_date` is the canonical "which rate applied to money that moved on
day D" lookup, and it lives here rather than in a report module because it is
a question about the rate table, not about any one report. Reports own the
per-report question (which day to ask about); this module owns the answer.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExchangeRate


def default_spread() -> Decimal:
    return Decimal(settings.exchange_rate_spread)


def default_iof() -> Decimal:
    return Decimal(settings.exchange_rate_iof)


def compute_effective(commercial: Decimal, spread: Decimal, iof: Decimal) -> Decimal:
    raw = Decimal(commercial) * (Decimal("1") + Decimal(spread)) * (Decimal("1") + Decimal(iof))
    return raw.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass
class ExchangeRateRow:
    id: int
    rate_date: date
    commercial: Decimal
    spread: Decimal
    iof: Decimal
    effective: Decimal
    source: str


def _to_row(r: ExchangeRate) -> ExchangeRateRow:
    return ExchangeRateRow(
        id=r.id,
        rate_date=r.rate_date,
        commercial=Decimal(r.commercial),
        spread=Decimal(r.spread),
        iof=Decimal(r.iof),
        effective=Decimal(r.effective),
        source=r.source,
    )


# What a BRL amount is divided by when the rate table cannot answer at all
# (no row anywhere). It is 1 because that is what services/reports.py has
# always used in that case: a database with no rates renders native BRL
# figures unchanged rather than dividing by zero or refusing to render. The
# `approximate` flag is what makes the fallback visible, not the value.
NO_RATE_EFFECTIVE = Decimal("1")


@dataclass(frozen=True)
class DatedRate:
    """The effective rate that applies to money which moved on one given date.

    `approximate` is carried instead of raising or returning None because
    every caller is a report: a period whose rate history has a hole still has
    to render a figure and say the figure is not exact. A caller that wants to
    surface that honestly reads the flag; a caller that only wants the number
    reads `effective` and gets the closest thing the table has.
    """

    rate_id: int | None
    rate_date: date | None
    effective: Decimal
    approximate: bool

    @classmethod
    def from_row(cls, row: ExchangeRate, *, approximate: bool = False) -> DatedRate:
        return cls(
            rate_id=row.id,
            rate_date=row.rate_date,
            effective=Decimal(row.effective),
            approximate=approximate,
        )

    @classmethod
    def unavailable(cls) -> DatedRate:
        return cls(
            rate_id=None,
            rate_date=None,
            effective=NO_RATE_EFFECTIVE,
            approximate=True,
        )


def rate_for_date(session: Session, on_date: date) -> DatedRate:
    """The rate in force on `on_date`: the latest row with rate_date <= it.

    The `<=` is not a convenience, it is how the table is shaped. PTAX
    publishes on business days only, so the daily refresh and `--backfill`
    fill business days and nothing else; the rate in force on a Saturday IS
    Friday's close, and the same holds across a holiday stretch. This is the
    same rule `reports._resolve_rate` applies to a month end, asked of an
    arbitrary day instead.

    When the table has no row at or before `on_date` at all — a receipt older
    than the earliest rate on file — the earliest available row is used and
    the result is flagged `approximate`. Two reasons not to fail instead: a
    report must still render, and the earliest known rate is a far better
    estimate of an older one than treating BRL as USD would be. It is the
    wrong rate, though, so it is never returned quietly.

    Only when the table is entirely empty does the result carry
    `NO_RATE_EFFECTIVE` (also flagged approximate).
    """
    row = session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date <= on_date)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )
    if row is not None:
        return DatedRate.from_row(row)

    earliest = session.scalar(
        select(ExchangeRate).order_by(ExchangeRate.rate_date.asc()).limit(1)
    )
    if earliest is not None:
        return DatedRate.from_row(earliest, approximate=True)
    return DatedRate.unavailable()


class DatedRateCache:
    """Memoizes `rate_for_date` for the life of one report render.

    Per-receipt conversion asks the rate table once per receipt date, and the
    annual report renders twelve months of those in one request. Rates cannot
    change mid-render (nothing in a read path writes them), so caching by date
    is free correctness-wise and turns a few hundred point queries into a few
    dozen. Build one per render and throw it away; it is deliberately not a
    process-wide cache, because a rate inserted by the daily refresh must be
    visible to the next request.

    `warm` closes the remaining gap. Per-receipt income asks about a handful of
    dates a month, but per-transaction spending asks about every day money was
    spent, and a month of card rows can touch every date in it. One point query
    per distinct date is a query count that grows with the ledger, so a caller
    that knows the window up front loads it in a single query instead and every
    lookup inside the window is then answered in memory.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_date: dict[date, DatedRate] = {}
        # A warmed window: the dates it covers, plus the rows in force across
        # it, ascending by rate_date. `for_date` bisects this instead of
        # querying when the day asked about falls inside [start, end].
        self._window: tuple[date, date] | None = None
        self._window_dates: list[date] = []
        self._window_rates: list[DatedRate] = []
        # What a day inside the window resolves to when the window holds no row
        # at or before it. Always the `rate_for_date` answer for that situation
        # (earliest row on file, or the no-rate fallback), flagged approximate.
        self._window_floor: DatedRate | None = None

    def warm(self, start: date, end: date) -> None:
        """Load, in ONE query, everything needed to answer `for_date` for any
        day in [start, end].

        The rows fetched are not just those dated inside the window: the rate in
        force on `start` itself is by definition the latest row at or before it,
        which is usually older than the window (PTAX publishes business days,
        and a month can open on a weekend). That row is picked up by the same
        statement via a floor subquery, so the query count stays at one and the
        row count stays bounded by the window rather than by the whole table.

        Calling it twice replaces the window rather than merging: a render warms
        one period, and pretending otherwise would mean holding rows nobody is
        going to ask about.
        """
        if end < start:
            start, end = end, start
        floor = (
            select(func.max(ExchangeRate.rate_date))
            .where(ExchangeRate.rate_date <= start)
            .scalar_subquery()
        )
        rows = self._session.scalars(
            select(ExchangeRate)
            .where(
                ExchangeRate.rate_date <= end,
                # The floor is NULL when the table has nothing at or before
                # `start`. Comparing against NULL would drop every row and make
                # a populated table look empty, so that case takes the whole
                # window and lets the per-date resolution below decide.
                or_(floor.is_(None), ExchangeRate.rate_date >= floor),
            )
            .order_by(ExchangeRate.rate_date)
        ).all()
        self._window = (start, end)
        self._window_dates = [r.rate_date for r in rows]
        self._window_rates = [DatedRate.from_row(r) for r in rows]
        # A day inside the window with no row at or before it can only happen
        # when the floor subquery found nothing, i.e. the table starts after
        # `start`. Then the earliest row on file is the first one fetched, and
        # `rate_for_date`'s rule is to use it and flag the result. When even
        # that is missing (nothing at all on or before `end`) one extra query
        # settles it, rather than leaving every such day to query for itself.
        if rows and rows[0].rate_date <= start:
            self._window_floor = None  # the floor row is in hand; unreachable
        elif rows:
            self._window_floor = DatedRate.from_row(rows[0], approximate=True)
        else:
            self._window_floor = rate_for_date(self._session, start)

    def _from_window(self, on_date: date) -> DatedRate | None:
        """The warmed answer for `on_date`, or None when no window covers it.

        Inside the window the rule is `rate_for_date`'s, applied to rows in
        memory: the last row at or before the day, falling back to the window
        floor when there is none.
        """
        if self._window is None:
            return None
        start, end = self._window
        if not (start <= on_date <= end):
            return None
        idx = bisect.bisect_right(self._window_dates, on_date)
        if idx == 0:
            return self._window_floor
        return self._window_rates[idx - 1]

    def for_date(self, on_date: date) -> DatedRate:
        hit = self._by_date.get(on_date)
        if hit is None:
            hit = self._from_window(on_date)
            if hit is None:
                hit = rate_for_date(self._session, on_date)
            self._by_date[on_date] = hit
        return hit


def list_rates(session: Session) -> list[ExchangeRateRow]:
    rows = session.scalars(
        select(ExchangeRate).order_by(ExchangeRate.rate_date.desc())
    ).all()
    return [_to_row(r) for r in rows]


def get_rate(session: Session, rate_id: int) -> ExchangeRateRow:
    r = session.get(ExchangeRate, rate_id)
    if r is None:
        raise LookupError(f"Exchange rate {rate_id} not found")
    return _to_row(r)


def get_rate_by_date(session: Session, rate_date: date) -> ExchangeRateRow | None:
    """None when no row exists yet for that date — the never-overwrite check
    the refresh script (and anything else landing an auto rate) needs before
    calling create_rate."""
    r = session.scalar(select(ExchangeRate).filter_by(rate_date=rate_date))
    return _to_row(r) if r is not None else None


def create_rate(
    session: Session,
    *,
    rate_date: date,
    commercial: Decimal,
    spread: Decimal | None = None,
    iof: Decimal | None = None,
    source: str = "manual",
) -> ExchangeRateRow:
    spread = default_spread() if spread is None else Decimal(spread)
    iof = default_iof() if iof is None else Decimal(iof)
    existing = session.scalar(select(ExchangeRate).filter_by(rate_date=rate_date))
    if existing is not None:
        raise ValueError(f"Exchange rate for {rate_date} already exists (id={existing.id})")
    effective = compute_effective(commercial, spread, iof)
    r = ExchangeRate(
        rate_date=rate_date,
        commercial=commercial,
        spread=spread,
        iof=iof,
        effective=effective,
        source=source,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return _to_row(r)


def delete_rate(session: Session, rate_id: int) -> None:
    r = session.get(ExchangeRate, rate_id)
    if r is None:
        raise LookupError(f"Exchange rate {rate_id} not found")
    session.delete(r)
    session.commit()
