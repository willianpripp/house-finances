"""Per-receipt FX conversion of income.

The rule under test: a BRL income month's USD figure is the sum of its
receipts, each converted at the rate in force on its OWN receipt_date. The old
rule converted the whole native monthly total at one month-end rate, which
re-priced the open month every time the daily rate refresh landed a row.

Every amount and rate here is invented and round, chosen so the two rules give
visibly different answers rather than to resemble anything real. Accounts are
named after what they test. `tests/factories.py` explains why that matters
(this file ships publicly) and `scripts/export_public.py` gates it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.models import (
    Currency,
    ExchangeRate,
    IncomeEntry,
    IncomeReceipt,
    IncomeSource,
    PaymentMethod,
    PaymentMethodType,
)
from app.services import income as income_service
from app.services.exchange_rates import NO_RATE_EFFECTIVE, rate_for_date
from app.services.reports import annual_report, monthly_report

# Far from every seeded fixture period and from the other test modules' years,
# so nothing here collides with the factory household.
YEAR = 2035

CENTS = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENTS)


def _drop_all_rates(db) -> None:
    """Clear the fixture household's rates so a test controls the whole table.

    Safe: the `db` fixture rolls back, and `income_entries.exchange_rate_id` is
    ON DELETE SET NULL. Tests that need "no rate at or before this date" cannot
    rely on the fixture's earliest rate staying where it is.
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


def _checking(db, name: str, currency: Currency) -> PaymentMethod:
    pm = PaymentMethod(
        name=name,
        type=PaymentMethodType.CHECKING,
        currency=currency,
        active=True,
    )
    db.add(pm)
    db.flush()
    return pm


def _receipt(
    db,
    pm: PaymentMethod,
    *,
    on: date,
    amount: str,
    source: IncomeSource = IncomeSource.EXTRA_BRL,
    currency: Currency = Currency.BRL,
    tag: str = "",
) -> IncomeReceipt:
    """One observed receipt, then re-derive its month.

    Extras are booked in the calendar month they arrived, so the funded period
    is the receipt's own month here. `tag` only keeps the deterministic
    statement signature distinct between two same-day, same-amount receipts.
    """
    receipt, _ = income_service.record_receipt(
        db,
        income_service.ReceiptDraft(
            source=source,
            year=on.year,
            month=on.month,
            receipt_date=on,
            amount=Decimal(amount),
            currency=currency,
            provenance=income_service.PROVENANCE_STATEMENT,
            payment_method_id=pm.id,
            description=f"Transfer received {tag or on.isoformat()}",
        ),
    )
    income_service.recompute_month(
        db, receipt.year, receipt.month, receipt.source
    )
    db.flush()
    return receipt


def _lump(db, *, year: int, month: int, amount: str, source: IncomeSource) -> None:
    """A pre-ledger monthly total and its backfill lump, as migration
    c3a86f512e9d writes them: one synthetic receipt dated month end, no
    account, no provider id. Built directly rather than by running the
    migration, which `test_income_receipts.py` already pins to this shape.
    """
    last_day = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    db.add(
        IncomeEntry(
            year=year,
            month=month,
            source=source,
            amount=Decimal(amount),
            currency=Currency.BRL,
        )
    )
    db.add(
        IncomeReceipt(
            source=source,
            year=year,
            month=month,
            receipt_date=last_day,
            amount=Decimal(amount),
            currency=Currency.BRL,
            provenance=income_service.PROVENANCE_BACKFILL,
            description="pre-ledger monthly total",
            signature=f"legacy:{source.value}:{year}-{month:02d}",
        )
    )
    db.flush()


def _bucket(report, source: IncomeSource):
    return next(b for b in report.income if b.source == source.value)


# ---------- the rate-for-a-day lookup ----------


def test_the_lookup_takes_the_latest_rate_at_or_before_the_day(db):
    _drop_all_rates(db)
    _rate(db, date(YEAR, 3, 5), "5.0000")
    _rate(db, date(YEAR, 3, 9), "4.0000")

    # On a day with its own row, that row.
    assert rate_for_date(db, date(YEAR, 3, 9)).effective == Decimal("4.0000")
    # On a day with no row, the last one published before it — never the next.
    assert rate_for_date(db, date(YEAR, 3, 8)).effective == Decimal("5.0000")
    # After the last row, that row keeps applying.
    assert rate_for_date(db, date(YEAR, 3, 30)).effective == Decimal("4.0000")
    assert rate_for_date(db, date(YEAR, 3, 9)).approximate is False


def test_a_weekend_receipt_converts_at_the_friday_rate(db):
    """The reason the lookup is `<=` and not `==`. The rate source publishes on
    business days only, so a Saturday or Sunday arrival has no row of its own
    and the rate in force is genuinely Friday's close."""
    friday = date(YEAR, 3, 2)
    assert friday.weekday() == 4
    monday = friday + timedelta(days=3)

    _drop_all_rates(db)
    _rate(db, friday, "5.0000")
    _rate(db, monday, "4.0000")

    for offset in (1, 2):  # Saturday, Sunday
        weekend_day = friday + timedelta(days=offset)
        assert weekend_day.weekday() in (5, 6)
        assert rate_for_date(db, weekend_day).effective == Decimal("5.0000")
        assert rate_for_date(db, weekend_day).approximate is False

    pm = _checking(db, "FX Weekend Checking", Currency.BRL)
    _receipt(db, pm, on=friday + timedelta(days=1), amount="1000.00")

    bucket = _bucket(monthly_report(db, YEAR, 3), IncomeSource.EXTRA_BRL)
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
    assert bucket.approximate is False


# ---------- the sum, per receipt ----------


def test_two_receipts_on_different_days_convert_at_their_own_rates(db):
    """The whole change, in one month. Two deposits, two rates, and the USD
    figure is the sum of the two conversions rather than one conversion of the
    sum."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 4, 3), "5.0000")
    _rate(db, date(YEAR, 4, 17), "4.0000")

    pm = _checking(db, "FX Two Rates Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 4, 3), amount="1000.00", tag="first")
    _receipt(db, pm, on=date(YEAR, 4, 17), amount="800.00", tag="second")

    report = monthly_report(db, YEAR, 4)
    bucket = _bucket(report, IncomeSource.EXTRA_BRL)

    # Native total is untouched: it is still the derived monthly figure that
    # warnings.py and home.py read.
    assert bucket.amount_native == Decimal("1800.00")
    assert bucket.currency == Currency.BRL.value

    per_receipt = Decimal("1000.00") / Decimal("5.0000") + Decimal("800.00") / Decimal("4.0000")
    assert bucket.amount_usd == _q(per_receipt)
    assert bucket.rate_basis == income_service.RATE_BASIS_PER_RECEIPT
    assert bucket.approximate is False

    # And it is NOT the old rule: the whole native total at the month-end rate.
    month_end_rule = Decimal("1800.00") / Decimal("4.0000")
    assert bucket.amount_usd != _q(month_end_rule)
    # The month-end rate is still resolved and still reported; it just no longer
    # decides the income figure.
    assert report.totals.rate_effective == Decimal("4.0000")
    assert report.totals.gross_income_usd == _q(per_receipt)


def test_a_usd_source_is_never_converted(db):
    """USD income reads no rate at all, which an empty rate table proves: if
    anything looked one up, this would divide by the no-rate fallback instead of
    returning the amount unchanged."""
    _drop_all_rates(db)
    pm = _checking(db, "FX Untouched Checking", Currency.USD)
    _receipt(
        db, pm,
        on=date(YEAR, 4, 6),
        amount="250.00",
        source=IncomeSource.EXTRA_USD,
        currency=Currency.USD,
    )

    bucket = _bucket(monthly_report(db, YEAR, 4), IncomeSource.EXTRA_USD)
    assert bucket.amount_usd == Decimal("250.00")
    assert bucket.rate_basis == income_service.RATE_BASIS_USD
    assert bucket.approximate is False


# ---------- the open month stops moving ----------


def test_a_later_rate_does_not_reprice_an_earlier_receipt(db):
    """The bug, pinned shut. The daily refresh lands a row this afternoon; a
    deposit from earlier in the month must be worth exactly what it was worth
    this morning, and a deposit dated after the new row must use it."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 6, 1), "5.0000")

    pm = _checking(db, "FX Open Month Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 6, 5), amount="1000.00", tag="early")

    before = _bucket(monthly_report(db, YEAR, 6), IncomeSource.EXTRA_BRL)
    assert before.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))

    # The daily refresh runs, mid-month, at a very different rate.
    _rate(db, date(YEAR, 6, 20), "4.0000")

    after_report = monthly_report(db, YEAR, 6)
    after = _bucket(after_report, IncomeSource.EXTRA_BRL)
    assert after.amount_usd == before.amount_usd
    # The report DID see the new row — this is invariance, not a stale read.
    assert after_report.totals.rate_effective == Decimal("4.0000")

    # A receipt dated on or after the new row is exactly what it may move.
    _receipt(db, pm, on=date(YEAR, 6, 22), amount="400.00", tag="late")
    final = _bucket(monthly_report(db, YEAR, 6), IncomeSource.EXTRA_BRL)
    assert final.amount_usd == _q(
        Decimal("1000.00") / Decimal("5.0000") + Decimal("400.00") / Decimal("4.0000")
    )


# ---------- pre-ledger months behave exactly as before ----------


def test_a_lumped_month_keeps_the_month_end_convention(db):
    """A month whose total predates the ledger has one synthetic receipt dated
    month end, standing in for constituents nobody recorded. Its own date
    carries no information, so it converts at the month-end rate and says so
    rather than claiming per-receipt precision."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 5, 2), "5.0000")
    _rate(db, date(YEAR, 5, 29), "4.0000")

    _lump(db, year=YEAR, month=5, amount="1000.00", source=IncomeSource.EXTRA_BRL)

    bucket = _bucket(monthly_report(db, YEAR, 5), IncomeSource.EXTRA_BRL)
    assert bucket.amount_native == Decimal("1000.00")
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("4.0000"))
    assert bucket.rate_basis == income_service.RATE_BASIS_MONTH_END


def test_a_lumped_month_does_not_convert_the_receipts_it_does_not_count(db):
    """While a lump holds a period's total, an observed receipt for that period
    is recorded but excluded from the total (see `recompute_month`). Converting
    it anyway would put money into the USD figure that is not in the native
    one, which is the double-count the lump exists to prevent."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 7, 1), "5.0000")

    _lump(db, year=YEAR, month=7, amount="1000.00", source=IncomeSource.EXTRA_BRL)
    pm = _checking(db, "FX Lump Resync Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 7, 4), amount="600.00", tag="inside the lump")

    bucket = _bucket(monthly_report(db, YEAR, 7), IncomeSource.EXTRA_BRL)
    assert bucket.amount_native == Decimal("1000.00")
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
    assert bucket.rate_basis == income_service.RATE_BASIS_MONTH_END


def test_a_monthly_row_with_no_receipts_falls_back_to_the_month_end_rate(db):
    """A legacy row that never got a lump. Reporting zero USD against a
    non-zero native total would be worse than the old rule, so the old rule is
    what it keeps."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 8, 3), "5.0000")
    db.add(
        IncomeEntry(
            year=YEAR,
            month=8,
            source=IncomeSource.RENTS_BRAZIL,
            amount=Decimal("1500.00"),
            currency=Currency.BRL,
        )
    )
    db.flush()

    bucket = _bucket(monthly_report(db, YEAR, 8), IncomeSource.RENTS_BRAZIL)
    assert bucket.amount_usd == _q(Decimal("1500.00") / Decimal("5.0000"))
    assert bucket.rate_basis == income_service.RATE_BASIS_MONTH_END


# ---------- when the rate table cannot answer ----------


def test_a_receipt_older_than_every_rate_uses_the_earliest_and_is_flagged(db):
    _drop_all_rates(db)
    _rate(db, date(YEAR, 9, 10), "5.0000")  # the only row on file

    pm = _checking(db, "FX No Earlier Rate Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 8, 4), amount="1000.00", tag="before any rate")

    resolved = rate_for_date(db, date(YEAR, 8, 4))
    assert resolved.effective == Decimal("5.0000")
    assert resolved.approximate is True

    report = monthly_report(db, YEAR, 8)
    bucket = _bucket(report, IncomeSource.EXTRA_BRL)
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
    assert bucket.approximate is True
    assert report.totals.income_rate_approximate is True


def test_an_empty_rate_table_leaves_the_amount_alone_and_is_flagged(db):
    """No rate anywhere. The figure has to render, so BRL passes through
    unconverted (what reports.py has always done) and the flag is the only
    thing that keeps that honest."""
    _drop_all_rates(db)

    resolved = rate_for_date(db, date(YEAR, 10, 6))
    assert resolved.effective == NO_RATE_EFFECTIVE
    assert resolved.rate_id is None
    assert resolved.approximate is True

    pm = _checking(db, "FX No Rates At All Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 10, 6), amount="1000.00")

    report = monthly_report(db, YEAR, 10)
    bucket = _bucket(report, IncomeSource.EXTRA_BRL)
    assert bucket.amount_usd == Decimal("1000.00")
    assert bucket.approximate is True
    assert report.totals.income_rate_approximate is True


# ---------- the annual report cannot disagree with the monthly one ----------


def test_the_annual_report_reports_the_same_conversions_as_the_monthly_one(db):
    """Both reports reach `_compute_income` through `_month_totals`, so this
    guards the wiring: an annual roll-up that re-derived income its own way
    could hold a different figure for the same month and nobody would see it on
    either page alone."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 11, 4), "5.0000")
    _rate(db, date(YEAR, 11, 21), "4.0000")
    _rate(db, date(YEAR, 12, 9), "2.0000")

    pm = _checking(db, "FX Annual Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 11, 6), amount="1000.00", tag="nov early")
    _receipt(db, pm, on=date(YEAR, 11, 25), amount="800.00", tag="nov late")
    _receipt(db, pm, on=date(YEAR, 12, 15), amount="600.00", tag="dec")

    monthlies = {m: monthly_report(db, YEAR, m).totals for m in (11, 12)}
    annual = annual_report(db, YEAR)

    # Same month, same figure, on both surfaces.
    for month, totals in monthlies.items():
        assert annual.months[month - 1].gross_income_usd == totals.gross_income_usd

    expected = _q(
        Decimal("1000.00") / Decimal("5.0000")
        + Decimal("800.00") / Decimal("4.0000")
        + Decimal("600.00") / Decimal("2.0000")
    )
    assert annual.gross_income_usd == expected
    assert annual.gross_income_usd == sum(
        (t.gross_income_usd for t in monthlies.values()), start=Decimal("0")
    )

    # December's own rate, not the year's last one, priced the December receipt.
    assert monthlies[12].gross_income_usd == _q(Decimal("600.00") / Decimal("2.0000"))


def test_the_receipt_sum_is_the_native_total_it_reports(db):
    """The native figure and the USD figure describe one number. Their
    relationship is only meaningful if the receipts converted are exactly the
    receipts the derived total is made of."""
    _drop_all_rates(db)
    _rate(db, date(YEAR, 2, 2), "5.0000")

    pm = _checking(db, "FX Coherence Checking", Currency.BRL)
    _receipt(db, pm, on=date(YEAR, 2, 5), amount="700.00", tag="one")
    _receipt(db, pm, on=date(YEAR, 2, 6), amount="300.00", tag="two")

    entry = db.scalar(
        select(IncomeEntry).filter_by(
            year=YEAR, month=2, source=IncomeSource.EXTRA_BRL
        )
    )
    receipts, lumped = income_service._counted_receipts(
        db, YEAR, 2, IncomeSource.EXTRA_BRL
    )
    assert lumped is False
    assert sum((Decimal(r.amount) for r in receipts), start=Decimal("0")) == Decimal(
        entry.amount
    )

    bucket = _bucket(monthly_report(db, YEAR, 2), IncomeSource.EXTRA_BRL)
    assert bucket.amount_native == Decimal("1000.00")
    assert bucket.amount_usd == _q(Decimal("1000.00") / Decimal("5.0000"))
