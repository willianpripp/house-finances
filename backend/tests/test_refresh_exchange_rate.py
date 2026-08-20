"""scripts/refresh_exchange_rate.py: the never-overwrite rule, exit codes,
landing a row under PTAX's own rate_date (which is not always today), and
the --backfill mode. PTAX itself is monkeypatched at the module's
fetch_latest_closing_rate/fetch_closing_rates_range names — payload
parsing/fallback has its own coverage in test_ptax_client.py."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import ExchangeRate
from app.services.exchange_rates import create_rate, get_rate_by_date
from app.services.ptax_client import PtaxError, PtaxRate
from scripts import refresh_exchange_rate


def test_get_rate_by_date_is_none_when_absent(db):
    assert get_rate_by_date(db, date(2026, 1, 1)) is None


def test_refresh_script_inserts_ptax_row_when_absent(db, monkeypatch):
    fake_rate_date = date(2026, 8, 19)
    assert get_rate_by_date(db, fake_rate_date) is None

    monkeypatch.setattr(
        refresh_exchange_rate,
        "fetch_latest_closing_rate",
        lambda: PtaxRate(rate_date=fake_rate_date, venda=Decimal("5.1714")),
    )

    exit_code = refresh_exchange_rate.main()
    assert exit_code == 0

    row = get_rate_by_date(db, fake_rate_date)
    assert row is not None
    assert row.source == "ptax"
    assert row.commercial == Decimal("5.1714")

    db.delete(db.get(ExchangeRate, row.id))
    db.commit()


def test_refresh_script_never_overwrites_an_existing_manual_row(db, monkeypatch):
    fake_rate_date = date(2026, 8, 19)
    manual = create_rate(
        db, rate_date=fake_rate_date, commercial=Decimal("5.00"), source="manual"
    )
    try:
        monkeypatch.setattr(
            refresh_exchange_rate,
            "fetch_latest_closing_rate",
            lambda: PtaxRate(rate_date=fake_rate_date, venda=Decimal("5.1714")),
        )

        exit_code = refresh_exchange_rate.main()
        assert exit_code == 0

        row = get_rate_by_date(db, fake_rate_date)
        assert row.id == manual.id
        assert row.source == "manual"
        assert row.commercial == Decimal("5.00")  # untouched
    finally:
        db.delete(db.get(ExchangeRate, manual.id))
        db.commit()


def test_refresh_script_exits_1_on_ptax_failure(monkeypatch):
    def boom():
        raise PtaxError("BCB is down")

    monkeypatch.setattr(refresh_exchange_rate, "fetch_latest_closing_rate", boom)

    assert refresh_exchange_rate.main() == 1


def test_refresh_script_lands_row_under_ptax_date_not_calendar_today(db, monkeypatch):
    """A Monday run whose PTAX answer is last Friday must insert under
    Friday's date, matching the <=-reference-day lookup reports.py relies on."""
    friday = date(2026, 8, 14)
    monkeypatch.setattr(
        refresh_exchange_rate,
        "fetch_latest_closing_rate",
        lambda: PtaxRate(rate_date=friday, venda=Decimal("5.2236")),
    )

    exit_code = refresh_exchange_rate.main()
    assert exit_code == 0

    row = get_rate_by_date(db, friday)
    assert row is not None
    assert row.source == "ptax"

    db.delete(db.get(ExchangeRate, row.id))
    db.commit()


# ---------- --backfill mode ----------

def test_backfill_requires_a_range_argument():
    assert refresh_exchange_rate.main(["--backfill"]) == 1


def test_backfill_rejects_a_malformed_range():
    assert refresh_exchange_rate.main(["--backfill", "not-a-range"]) == 1


def test_backfill_rejects_start_after_end():
    assert refresh_exchange_rate.main(["--backfill", "2026-08-19..2026-08-01"]) == 1


def test_backfill_exits_1_on_ptax_failure(monkeypatch):
    def boom(start, end):
        raise PtaxError("BCB is down")

    monkeypatch.setattr(refresh_exchange_rate, "fetch_closing_rates_range", boom)

    assert refresh_exchange_rate.main(["--backfill", "2026-08-01..2026-08-07"]) == 1


def test_backfill_inserts_every_missing_business_day(db, monkeypatch):
    """A week with no rows at all: PTAX returns Mon/Tue/Wed (no Sat/Sun rows
    to begin with, matching how BCB's own period endpoint behaves), all
    three land as source=ptax."""
    days = [date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)]
    fake_rates = [PtaxRate(rate_date=d, venda=Decimal("5.10") + i) for i, d in enumerate(days)]

    monkeypatch.setattr(
        refresh_exchange_rate,
        "fetch_closing_rates_range",
        lambda start, end: fake_rates,
    )

    exit_code = refresh_exchange_rate.main(["--backfill", "2026-08-17..2026-08-19"])
    assert exit_code == 0

    try:
        for d, expected in zip(days, fake_rates):
            row = get_rate_by_date(db, d)
            assert row is not None
            assert row.source == "ptax"
            assert row.commercial == expected.venda
    finally:
        for d in days:
            row = get_rate_by_date(db, d)
            if row is not None:
                db.delete(db.get(ExchangeRate, row.id))
        db.commit()


def test_backfill_never_overwrites_an_existing_row(db, monkeypatch):
    """One day in the range already has a row (a manual historical entry);
    backfill must leave it untouched and only fill the gaps around it."""
    existing_date = date(2026, 8, 18)
    manual = create_rate(
        db, rate_date=existing_date, commercial=Decimal("5.00"), source="manual"
    )
    other_date = date(2026, 8, 19)

    fake_rates = [
        PtaxRate(rate_date=existing_date, venda=Decimal("5.1714")),
        PtaxRate(rate_date=other_date, venda=Decimal("5.1800")),
    ]
    monkeypatch.setattr(
        refresh_exchange_rate,
        "fetch_closing_rates_range",
        lambda start, end: fake_rates,
    )

    try:
        exit_code = refresh_exchange_rate.main(["--backfill", "2026-08-17..2026-08-19"])
        assert exit_code == 0

        untouched = get_rate_by_date(db, existing_date)
        assert untouched.id == manual.id
        assert untouched.source == "manual"
        assert untouched.commercial == Decimal("5.00")  # not overwritten

        filled = get_rate_by_date(db, other_date)
        assert filled is not None
        assert filled.source == "ptax"
        assert filled.commercial == Decimal("5.1800")
    finally:
        db.delete(db.get(ExchangeRate, manual.id))
        other = get_rate_by_date(db, other_date)
        if other is not None:
            db.delete(db.get(ExchangeRate, other.id))
        db.commit()
