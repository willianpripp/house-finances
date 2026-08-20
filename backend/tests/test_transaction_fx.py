"""Per-transaction FX conversion of spending.

The rule under test: a BRL transaction's USD figure is its amount converted at
the rate in force on its OWN transaction_date. The old rule converted every row
in the month at one month-end rate, which re-priced the open month's spending
every time the daily rate refresh landed a row. Income got this treatment
first (`test_income_fx.py`); this is the spending half, and it goes further in
one respect: spending has FOUR surfaces (the monthly total, the category
breakdown, the per-transaction tables, the annual roll-up) and they all have to
be views of one conversion, so several tests here pin the wiring rather than
the arithmetic.

Every amount and rate is invented and round, chosen so the two rules give
visibly different answers rather than to resemble anything real. Categories,
merchants and accounts are named after what they test. `tests/factories.py`
explains why that matters (this file ships publicly) and
`scripts/export_public.py` gates it.
"""
from __future__ import annotations

import calendar
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, event

from app.models import (
    Category,
    CategoryType,
    Currency,
    ExchangeRate,
    Merchant,
    MonthlySnapshot,
    PaymentMethod,
    PaymentMethodType,
    Transaction,
)
from app.services.exchange_rates import (
    NO_RATE_EFFECTIVE,
    DatedRateCache,
    rate_for_date,
)
from app.services.reports import annual_report, monthly_report

# Far from the fixture household's periods and from every other test module's
# year, including test_income_fx's, so nothing here collides.
YEAR = 2037

CENTS = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENTS)


def _drop_all_rates(db) -> None:
    """Clear the fixture household's rates so a test controls the whole table.

    Safe: the `db` fixture rolls back, and both `income_entries` and
    `monthly_snapshots` reference exchange_rates ON DELETE SET NULL.
    """
    db.execute(delete(ExchangeRate))
    db.flush()


def _rate(db, on: date, effective: str) -> ExchangeRate:
    """One rate row. Only `effective` is read by any conversion, so commercial
    is set to the same value and the spread/IOF factors to zero rather than
    inviting arithmetic into an assertion."""
    row = ExchangeRate(
        rate_date=on,
        commercial=Decimal(effective),
        spread=Decimal("0"),
        iof=Decimal("0"),
        effective=Decimal(effective),
    )
    db.add(row)
    db.flush()
    return row


def _category(
    db, name: str, ctype: CategoryType = CategoryType.VARIABLE, *, excluded: bool = False
) -> Category:
    row = Category(name=name, type=ctype, exclude_from_spending=excluded)
    db.add(row)
    db.flush()
    return row


def _merchant(db, name: str) -> Merchant:
    row = Merchant(name=name)
    db.add(row)
    db.flush()
    return row


def _card(db, name: str, currency: Currency) -> PaymentMethod:
    row = PaymentMethod(
        name=name,
        type=PaymentMethodType.CREDIT_CARD,
        currency=currency,
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def _spend(
    db,
    *,
    on: date,
    amount: str,
    category: Category,
    merchant: Merchant,
    card: PaymentMethod,
    currency: Currency = Currency.BRL,
    installment_value: str | None = None,
    installment_current: int = 1,
    installment_total: int = 1,
) -> Transaction:
    """One purchase. `installment_value` exercises the monthly-slice rule: a
    12x buy contributes its slice, not its price."""
    row = Transaction(
        transaction_date=on,
        merchant_id=merchant.id,
        category_id=category.id,
        payment_method_id=card.id,
        amount=Decimal(amount),
        currency=currency,
        installment_value=(
            Decimal(installment_value) if installment_value is not None else None
        ),
        installment_current=installment_current,
        installment_total=installment_total,
    )
    db.add(row)
    db.flush()
    return row


def _bucket(report, name: str):
    return next(b for b in report.by_category if b.category_name == name)


@contextmanager
def _rate_queries(db):
    """Count statements that hit the exchange_rates table.

    A per-date conversion is only viable if the rate table is read a bounded
    number of times per render, so the query count is behaviour worth pinning,
    not an implementation detail.
    """

    class Counter:
        n = 0

    counter = Counter()
    bind = db.get_bind()

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        if "exchange_rates" in statement:
            counter.n += 1

    event.listen(bind, "before_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(bind, "before_cursor_execute", _on_execute)


# ---------- the conversion itself ----------


def test_two_purchases_on_different_days_convert_at_their_own_rates(db):
    """The whole change, in one month. Two BRL charges, two rates, and the
    month's spending is the sum of the two conversions rather than one
    conversion of the sum."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 4, 3), "5.0000")
    _rate(db, date(YEAR, 4, 17), "4.0000")

    category = _category(db, "FX Two Rates Spend")
    merchant = _merchant(db, "Corner Shop")
    card = _card(db, "FX Two Rates Card", Currency.BRL)
    _spend(db, on=date(YEAR, 4, 3), amount="1000.00", category=category, merchant=merchant, card=card)
    _spend(db, on=date(YEAR, 4, 17), amount="800.00", category=category, merchant=merchant, card=card)

    report = monthly_report(db, YEAR, 4)
    per_date = Decimal("1000.00") / Decimal("5.0000") + Decimal("800.00") / Decimal("4.0000")

    assert _bucket(report, "FX Two Rates Spend").amount_usd == _q(per_date)
    assert report.totals.total_spending_usd == _q(per_date)
    assert report.totals.spending_rate_approximate is False

    # And it is NOT the old rule: the whole native total at the month-end rate.
    month_end_rule = Decimal("1800.00") / Decimal("4.0000")
    assert report.totals.total_spending_usd != _q(month_end_rule)
    # The month-end rate is still resolved and still reported (balances use it);
    # it just no longer decides what a purchase was worth.
    assert report.totals.rate_effective == Decimal("4.0000")


def test_a_weekend_purchase_converts_at_the_friday_rate(db):
    """The reason the lookup is `<=` and not `==`. The rate source publishes on
    business days only, so a Saturday charge has no row of its own and the rate
    in force is genuinely Friday's close."""
    friday = date(YEAR, 5, 1)
    assert friday.weekday() == 4
    monday = friday + timedelta(days=3)

    _drop_all_rates(db)
    _rate(db, friday, "5.0000")
    _rate(db, monday, "4.0000")

    category = _category(db, "FX Weekend Spend")
    merchant = _merchant(db, "Weekend Diner")
    card = _card(db, "FX Weekend Card", Currency.BRL)
    _spend(db, on=friday + timedelta(days=1), amount="1000.00", category=category, merchant=merchant, card=card)

    report = monthly_report(db, YEAR, 5)
    bucket = _bucket(report, "FX Weekend Spend")
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
    assert bucket.approximate is False


def test_usd_purchases_are_never_converted(db):
    """A USD row reads no rate at all, which an empty rate table proves: if
    anything looked one up, this would divide by the no-rate fallback instead of
    passing the amount through."""
    _drop_all_rates(db)

    category = _category(db, "FX Untouched Spend")
    merchant = _merchant(db, "Domestic Store")
    card = _card(db, "FX Untouched Card", Currency.USD)
    _spend(
        db, on=date(YEAR, 6, 6), amount="250.00",
        category=category, merchant=merchant, card=card, currency=Currency.USD,
    )

    report = monthly_report(db, YEAR, 6)
    bucket = _bucket(report, "FX Untouched Spend")
    assert bucket.amount_usd == Decimal("250.00")
    assert bucket.approximate is False
    assert report.totals.spending_rate_approximate is False
    assert report.variable_transactions
    assert all(t.amount_usd == t.amount_native for t in report.variable_transactions)


def test_an_installment_slice_converts_at_the_rows_own_date(db):
    """COALESCE(installment_value, amount) survives the move to per-date
    conversion: a 12x purchase contributes its monthly slice, converted at the
    slice row's date, not the full price."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 7, 2), "5.0000")

    category = _category(db, "FX Installment Spend", CategoryType.FIXED)
    merchant = _merchant(db, "Appliance Store")
    card = _card(db, "FX Installment Card", Currency.BRL)
    _spend(
        db, on=date(YEAR, 7, 9), amount="1200.00", installment_value="100.00",
        installment_current=3, installment_total=12,
        category=category, merchant=merchant, card=card,
    )

    report = monthly_report(db, YEAR, 7)
    assert _bucket(report, "FX Installment Spend").amount_usd == _q(
        Decimal("100.00") / Decimal("5.0000")
    )
    detail = next(t for t in report.fixed_transactions if t.category_name == "FX Installment Spend")
    assert detail.amount_native == Decimal("100.00")
    assert detail.amount_usd == _q(Decimal("100.00") / Decimal("5.0000"))


# ---------- the open month stops moving ----------


def test_a_later_rate_does_not_reprice_an_earlier_purchase(db):
    """The bug, pinned shut. The daily refresh lands a row this afternoon; a
    charge from earlier in the month must be worth exactly what it was worth
    this morning, and a charge dated after the new row must use it."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 8, 1), "5.0000")

    category = _category(db, "FX Open Month Spend")
    merchant = _merchant(db, "Open Month Store")
    card = _card(db, "FX Open Month Card", Currency.BRL)
    _spend(db, on=date(YEAR, 8, 5), amount="1000.00", category=category, merchant=merchant, card=card)

    before = monthly_report(db, YEAR, 8).totals.total_spending_usd
    assert before == _q(Decimal("1000.00") / Decimal("5.0000"))

    # The daily refresh runs, mid-month, at a very different rate.
    _rate(db, date(YEAR, 8, 20), "4.0000")

    after = monthly_report(db, YEAR, 8).totals
    assert after.total_spending_usd == before
    # The report DID see the new row — this is invariance, not a stale read.
    assert after.rate_effective == Decimal("4.0000")

    # A charge dated on or after the new row is exactly what it may move.
    _spend(db, on=date(YEAR, 8, 22), amount="400.00", category=category, merchant=merchant, card=card)
    final = monthly_report(db, YEAR, 8).totals
    assert final.total_spending_usd == _q(
        Decimal("1000.00") / Decimal("5.0000") + Decimal("400.00") / Decimal("4.0000")
    )


# ---------- when the rate table cannot answer ----------


def test_a_purchase_older_than_every_rate_uses_the_earliest_and_is_flagged(db):
    _drop_all_rates(db)
    _rate(db, date(YEAR, 10, 10), "5.0000")  # the only row on file

    category = _category(db, "FX No Earlier Rate Spend")
    merchant = _merchant(db, "Old Receipt Store")
    card = _card(db, "FX No Earlier Rate Card", Currency.BRL)
    _spend(db, on=date(YEAR, 9, 4), amount="1000.00", category=category, merchant=merchant, card=card)

    resolved = rate_for_date(db, date(YEAR, 9, 4))
    assert resolved.effective == Decimal("5.0000")
    assert resolved.approximate is True

    report = monthly_report(db, YEAR, 9)
    bucket = _bucket(report, "FX No Earlier Rate Spend")
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
    assert bucket.approximate is True
    assert report.totals.spending_rate_approximate is True
    # The income side has its own flag and is not dragged along by this one.
    assert report.totals.income_rate_approximate is False

    annual = annual_report(db, YEAR)
    assert annual.spending_rate_approximate is True
    assert next(
        c for c in annual.top_categories if c.category_name == "FX No Earlier Rate Spend"
    ).approximate is True


def test_an_empty_rate_table_leaves_the_amount_alone_and_is_flagged(db):
    """No rate anywhere. The figure has to render, so BRL passes through
    unconverted (what reports.py has always done) and the flag is the only
    thing that keeps that honest."""
    _drop_all_rates(db)

    assert rate_for_date(db, date(YEAR, 11, 6)).effective == NO_RATE_EFFECTIVE

    category = _category(db, "FX No Rates Spend")
    merchant = _merchant(db, "Unpriced Store")
    card = _card(db, "FX No Rates Card", Currency.BRL)
    _spend(db, on=date(YEAR, 11, 6), amount="1000.00", category=category, merchant=merchant, card=card)

    report = monthly_report(db, YEAR, 11)
    bucket = _bucket(report, "FX No Rates Spend")
    assert bucket.amount_usd == Decimal("1000.00")
    assert bucket.approximate is True
    assert report.totals.spending_rate_approximate is True


# ---------- one conversion, every surface ----------


def test_every_spending_surface_reports_the_same_conversion(db):
    """The wiring, pinned. Four surfaces show converted spending: the monthly
    total, the category buckets, the per-transaction tables and the annual
    roll-up. Each one that priced its own rows would be free to disagree with
    the others, and nobody would see it on any single page.
    """
    _drop_all_rates(db)
    _rate(db, date(YEAR, 2, 2), "5.0000")
    _rate(db, date(YEAR, 2, 20), "4.0000")

    fixed = _category(db, "FX Shared Fixed", CategoryType.FIXED)
    variable = _category(db, "FX Shared Variable")
    excluded = _category(db, "FX Shared Excluded", excluded=True)
    merchant = _merchant(db, "Shared Path Store")
    card = _card(db, "FX Shared Card", Currency.BRL)

    _spend(db, on=date(YEAR, 2, 5), amount="1000.00", category=fixed, merchant=merchant, card=card)
    _spend(db, on=date(YEAR, 2, 25), amount="800.00", category=fixed, merchant=merchant, card=card)
    _spend(db, on=date(YEAR, 2, 6), amount="500.00", category=variable, merchant=merchant, card=card)
    _spend(db, on=date(YEAR, 2, 26), amount="400.00", category=variable, merchant=merchant, card=card)
    _spend(db, on=date(YEAR, 2, 7), amount="300.00", category=excluded, merchant=merchant, card=card)

    report = monthly_report(db, YEAR, 2)

    # 1. The month's total is the sum of its category buckets, not a separate
    #    aggregation of the same rows.
    assert report.totals.total_spending_usd == _q(
        sum((c.amount_usd for c in report.by_category), start=Decimal("0"))
    )
    assert report.totals.fixed_spending_usd == _bucket(report, "FX Shared Fixed").amount_usd
    assert report.totals.variable_spending_usd == _bucket(report, "FX Shared Variable").amount_usd

    # 2. Each category bucket is the sum of the detail rows shown under it.
    for bucket, rows in (
        (_bucket(report, "FX Shared Fixed"), report.fixed_transactions),
        (_bucket(report, "FX Shared Variable"), report.variable_transactions),
    ):
        under_it = [t for t in rows if t.category_id == bucket.category_id]
        assert under_it
        assert bucket.amount_usd == _q(
            sum((t.amount_usd for t in under_it), start=Decimal("0"))
        )

    # 3. The excluded section converts by the same rule and stays out of the
    #    spending total.
    assert report.excluded_total_usd == _q(Decimal("300.00") / Decimal("5.0000"))
    assert report.excluded_total_usd not in (
        report.totals.total_spending_usd,
        report.totals.fixed_spending_usd,
    )
    assert [c.category_name for c in report.by_category] == [
        "FX Shared Variable",
        "FX Shared Fixed",
    ]

    # 4. The annual roll-up carries the monthly figures, month by month.
    annual = annual_report(db, YEAR)
    assert annual.months[1].total_spending_usd == report.totals.total_spending_usd
    assert annual.total_spending_usd == report.totals.total_spending_usd
    by_name = {c.category_name: c for c in annual.top_categories}
    for name in ("FX Shared Fixed", "FX Shared Variable"):
        assert by_name[name].amount_usd == _bucket(report, name).amount_usd


def test_the_per_salary_tax_hint_matches_the_taxes_bucket(db):
    """The taxes-by-salary hint under each income source used to be its own
    aggregation with its own conversion. It now derives from the same converted
    rows as the Taxes category bucket, so the hint and the bucket cannot drift."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 3, 2), "5.0000")
    _rate(db, date(YEAR, 3, 20), "4.0000")

    taxes = db.query(Category).filter_by(name="Taxes").one()
    merchant = _merchant(db, "Tax Office")
    brl_card = _card(db, "FX Taxes BRL Card", Currency.BRL)
    usd_card = _card(db, "FX Taxes USD Card", Currency.USD)

    _spend(db, on=date(YEAR, 3, 5), amount="1000.00", category=taxes, merchant=merchant, card=brl_card)
    _spend(db, on=date(YEAR, 3, 25), amount="800.00", category=taxes, merchant=merchant, card=brl_card)
    _spend(
        db, on=date(YEAR, 3, 10), amount="150.00", category=taxes, merchant=merchant,
        card=usd_card, currency=Currency.USD,
    )

    totals = monthly_report(db, YEAR, 3).totals
    brl_per_date = Decimal("1000.00") / Decimal("5.0000") + Decimal("800.00") / Decimal("4.0000")

    assert totals.taxes_primary_usd == _q(brl_per_date)
    assert totals.taxes_partner_usd == Decimal("150.00")
    assert totals.taxes_usd == _q(brl_per_date + Decimal("150.00"))
    assert totals.taxes_usd == _q(totals.taxes_primary_usd + totals.taxes_partner_usd)


# ---------- the query count does not follow the ledger ----------


def test_the_rate_table_is_read_a_fixed_number_of_times_per_render(db):
    """Per-date conversion must not mean one rate lookup per row. A month with
    a handful of purchases and the same month with two hundred of them, on
    every date in it, read the rate table exactly as many times."""
    _drop_all_rates(db)
    for day in (1, 8, 15, 22):
        _rate(db, date(YEAR, 6, day), "5.0000")

    category = _category(db, "FX Query Count Spend")
    merchant = _merchant(db, "Busy Store")
    card = _card(db, "FX Query Count Card", Currency.BRL)
    for day in (2, 9, 16, 23, 27):
        _spend(db, on=date(YEAR, 6, day), amount=f"{day}.00", category=category, merchant=merchant, card=card)

    with _rate_queries(db) as small:
        monthly_report(db, YEAR, 6)

    # Two hundred more rows, spread over every day of the month, with distinct
    # amounts so each one satisfies the transaction signature constraint.
    last_day = calendar.monthrange(YEAR, 6)[1]
    for n in range(200):
        _spend(
            db,
            on=date(YEAR, 6, (n % last_day) + 1),
            amount=f"{1000 + n}.00",
            category=category,
            merchant=merchant,
            card=card,
        )

    with _rate_queries(db) as big:
        report = monthly_report(db, YEAR, 6)

    assert _bucket(report, "FX Query Count Spend").transaction_count == 205
    assert big.n == small.n, "the rate table is being read per row or per date"
    # An absolute ceiling too, so the count cannot quietly grow for some other
    # reason: the month's rate window, the month-end rate, the rate the finalize
    # check looks for, and the same three again for the prior month's card.
    assert small.n <= 8


def test_the_warmed_cache_answers_a_whole_month_from_one_query(db):
    """`DatedRateCache.warm` is what keeps the count above flat. Its answers
    have to be `rate_for_date`'s answers, for every day in the window."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 3, 27), "5.0000")   # in force when the window opens
    _rate(db, date(YEAR, 4, 10), "4.0000")
    _rate(db, date(YEAR, 4, 24), "2.0000")

    first, last = date(YEAR, 4, 1), date(YEAR, 4, 30)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    expected = {day: rate_for_date(db, day) for day in days}

    cache = DatedRateCache(db)
    with _rate_queries(db) as counted:
        cache.warm(first, last)
        got = {day: cache.for_date(day) for day in days}
    assert counted.n == 1

    for day in days:
        assert got[day].effective == expected[day].effective
        assert got[day].approximate == expected[day].approximate
    assert got[first].effective == Decimal("5.0000")
    assert got[date(YEAR, 4, 23)].effective == Decimal("4.0000")
    assert got[last].effective == Decimal("2.0000")


def test_the_warmed_cache_handles_a_window_that_starts_before_every_rate(db):
    """No row at or before the window's first day. Each day still resolves to
    `rate_for_date`'s answer (the earliest row on file, flagged), and still
    without a query per day."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 4, 20), "4.0000")

    first, last = date(YEAR, 4, 1), date(YEAR, 4, 30)
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    expected = {day: rate_for_date(db, day) for day in days}

    cache = DatedRateCache(db)
    with _rate_queries(db) as counted:
        cache.warm(first, last)
        got = {day: cache.for_date(day) for day in days}
    assert counted.n == 1

    for day in days:
        assert got[day].effective == expected[day].effective
        assert got[day].approximate == expected[day].approximate
    assert got[first].approximate is True      # before every row on file
    assert got[last].approximate is False      # priced by the row of the 20th


# ---------- what this does to months that are already closed ----------


def test_a_closed_month_keeps_its_frozen_total_and_reprices_its_breakdown(db):
    """Requirement-5 evidence: the size of the shift, on one representative
    closed month.

    A finalized month's spending TOTALS are read from its snapshot, so they do
    not move at all. Its category breakdown is live-derived, and that is what
    re-prices: it used to be converted at the one rate frozen into the
    snapshot, and now each row converts at its own date. With the fixture below
    the two differ by 50.00 on 600.00, which is 8.3 percent of the month's
    spending, and the direction depends only on which way the rate moved during
    the month.

    Closing a month from now on freezes the per-date figure (`close_month`
    reads the same live totals), so reopening and re-closing a month realigns
    the frozen total with the breakdown.
    """
    _drop_all_rates(db)
    early = _rate(db, date(YEAR, 1, 2), "5.0000")
    month_end = _rate(db, date(YEAR, 1, 20), "4.0000")
    assert early.effective != month_end.effective

    category = _category(db, "FX Closed Month Spend")
    merchant = _merchant(db, "Closed Month Store")
    brl_card = _card(db, "FX Closed Month BRL Card", Currency.BRL)
    usd_card = _card(db, "FX Closed Month USD Card", Currency.USD)

    _spend(db, on=date(YEAR, 1, 5), amount="1000.00", category=category, merchant=merchant, card=brl_card)
    _spend(db, on=date(YEAR, 1, 25), amount="1000.00", category=category, merchant=merchant, card=brl_card)
    _spend(
        db, on=date(YEAR, 1, 10), amount="100.00", category=category, merchant=merchant,
        card=usd_card, currency=Currency.USD,
    )

    # The old rule, and what a close under it froze: the whole native BRL total
    # at the month-end rate, plus the USD row untouched.
    old_rule = Decimal("2000.00") / month_end.effective + Decimal("100.00")
    assert _q(old_rule) == Decimal("600.00")

    db.add(
        MonthlySnapshot(
            year=YEAR,
            month=1,
            variable_spending_usd=_q(old_rule),
            exchange_rate_id=month_end.id,
            is_finalized=True,
        )
    )
    db.flush()

    report = monthly_report(db, YEAR, 1)
    assert report.totals.is_finalized is True

    # Frozen totals: untouched by this change.
    assert report.totals.total_spending_usd == Decimal("600.00")

    # Live breakdown: per date, and lower here because the second purchase
    # happened while a BRL bought more dollars than at the month's end.
    new_rule = (
        Decimal("1000.00") / Decimal("5.0000")
        + Decimal("1000.00") / Decimal("4.0000")
        + Decimal("100.00")
    )
    assert _bucket(report, "FX Closed Month Spend").amount_usd == _q(new_rule)
    assert _q(new_rule) == Decimal("550.00")
    assert Decimal("600.00") - Decimal("550.00") == Decimal("50.00")

    # The annual page aggregates the breakdown for closed months too, so the
    # same shift shows there.
    annual = annual_report(db, YEAR)
    assert next(
        c for c in annual.top_categories if c.category_name == "FX Closed Month Spend"
    ).amount_usd == Decimal("550.00")
