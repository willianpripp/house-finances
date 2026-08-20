"""The income receipt ledger: idempotency, recompute, and the pre-ledger lump.

Replaces test_extra_income_idempotency.py, whose subject (a month-level freeze
guarded on the provider id) no longer exists. The freeze was the workaround for
`income_entries` having no provenance; these tests pin down the fix and the two
things the fix must not break, the salary gross invariant and the existing
production totals.

Households here are fictional. `tests/factories.py` explains why that matters
(the file ships publicly) and `scripts/export_public.py` gates it.
"""
from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.models import (
    Currency,
    IncomeEntry,
    IncomeReceipt,
    IncomeSource,
    PaymentMethod,
    PaymentMethodType,
)
from app.services import income as income_service
from app.services.checking_importer import (
    _record_extra_income,
    _record_rent_deposit,
)
from app.services.parsers.checking import CheckingActivity, CheckingClass

# Far from every seeded fixture period, so these tests never collide with the
# factory household or with each other.
YEAR = 2032


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


def _deposit(
    day: int,
    amount: str,
    *,
    month: int,
    description: str = "Transfer received, sender A",
    classification: CheckingClass = CheckingClass.EXTRA_INCOME,
    **ids,
) -> CheckingActivity:
    return CheckingActivity(
        activity_date=date(YEAR, month, day),
        description=description,
        amount=Decimal(amount),
        running_balance=None,
        classification=classification,
        **ids,
    )


def _entry(db, month: int, source: IncomeSource) -> IncomeEntry | None:
    return db.scalar(
        select(IncomeEntry).filter_by(year=YEAR, month=month, source=source)
    )


def _total(db, month: int, source: IncomeSource) -> Decimal:
    entry = _entry(db, month, source)
    return Decimal(entry.amount) if entry else Decimal("0")


def _receipt_count(db, month: int, source: IncomeSource) -> int:
    return len(
        db.scalars(
            select(IncomeReceipt).filter_by(year=YEAR, month=month, source=source)
        ).all()
    )


# ---------- receipt idempotency per provider ----------


def test_pluggy_resync_records_the_receipt_once(db):
    """A re-pulled window presents the same deposit again on every commit."""
    month = 2
    pm = _checking(db, "Receipt Pluggy Checking", Currency.BRL)

    _record_extra_income(db, _deposit(2, "900.00", month=month, pluggy_transaction_id="pg-a1"), pm)
    _record_extra_income(db, _deposit(2, "900.00", month=month, pluggy_transaction_id="pg-a1"), pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 1
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("900.00")


def test_plaid_resync_records_the_receipt_once(db):
    month = 3
    pm = _checking(db, "Receipt Plaid Checking", Currency.USD)

    _record_extra_income(db, _deposit(9, "250.00", month=month, plaid_transaction_id="pl-a1"), pm)
    _record_extra_income(db, _deposit(9, "250.00", month=month, plaid_transaction_id="pl-a1"), pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_USD) == 1
    assert _total(db, month, IncomeSource.EXTRA_USD) == Decimal("250.00")


def test_a_provider_id_is_never_reused_across_receipts(db):
    """The provider id columns are unique where set, so one bank transaction
    cannot become two receipts even if a caller passes it twice with a
    different amount."""
    month = 4
    pm = _checking(db, "Receipt Provider Unique Checking", Currency.BRL)

    _record_extra_income(db, _deposit(5, "100.00", month=month, pluggy_transaction_id="pg-b1"), pm)
    # Same provider id, different amount: the signature is the provider id, so
    # this is recognised as the same receipt and the amount is NOT overwritten.
    _record_extra_income(db, _deposit(5, "175.00", month=month, pluggy_transaction_id="pg-b1"), pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 1
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("100.00")


def test_statement_receipts_dedupe_on_a_deterministic_signature(db):
    """No provider id (hand-pasted or parsed statement): re-importing the same
    line must not add a second receipt."""
    month = 5
    pm = _checking(db, "Receipt Statement Checking", Currency.BRL)
    activity = _deposit(11, "430.00", month=month, description="Pix received, sender B")

    _record_extra_income(db, activity, pm)
    _record_extra_income(db, activity, pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 1
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("430.00")


def test_two_distinct_statement_deposits_in_one_month_sum(db):
    """The manual-paste ACCUMULATE behaviour, now a consequence of the grain
    rather than a special branch."""
    month = 6
    pm = _checking(db, "Receipt Accumulate Checking", Currency.BRL)

    _record_extra_income(db, _deposit(3, "100.00", month=month, description="Pix, sender C"), pm)
    _record_extra_income(db, _deposit(19, "65.00", month=month, description="Pix, sender D"), pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 2
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("165.00")


def test_a_debit_is_still_not_income(db):
    month = 7
    pm = _checking(db, "Receipt Debit Checking", Currency.BRL)

    note = _record_extra_income(db, _deposit(4, "-80.00", month=month), pm)
    db.flush()

    assert "skipped" in note
    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 0
    assert _entry(db, month, IncomeSource.EXTRA_BRL) is None


# ---------- two providers feeding one source in one month ----------


def test_two_accounts_feed_one_source_and_neither_erases_the_other(db):
    """The reason the schema changed. Two BRL checking accounts both classify
    into EXTRA_BRL; re-syncing one must not drop the other's contribution.

    This is the real two-account case recorded in STATUS.md on 2026-08-17, with
    the accounts anonymised.
    """
    month = 8
    bank_one = _checking(db, "Receipt Bank One Checking", Currency.BRL)
    bank_two = _checking(db, "Receipt Bank Two Checking", Currency.BRL)

    _record_extra_income(db, _deposit(2, "1500.00", month=month, pluggy_transaction_id="pg-c1"), bank_one)
    _record_extra_income(db, _deposit(6, "570.00", month=month, pluggy_transaction_id="pg-c2"), bank_two)
    db.flush()
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("2070.00")

    # Bank one re-syncs its whole window. Bank two is not part of that sync at
    # all, and its 570 must survive.
    _record_extra_income(db, _deposit(2, "1500.00", month=month, pluggy_transaction_id="pg-c1"), bank_one)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 2
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("2070.00")


def test_a_deposit_posting_after_the_first_sync_lands_with_no_manual_fix(db):
    """The exact scenario the freeze could not handle.

    Before: the month's total was frozen at 1,500 by the first sync, and the
    later deposit needed a hand-typed /income correction. Now the second sync
    adds a receipt and the total follows.
    """
    month = 9
    pm = _checking(db, "Receipt Late Deposit Checking", Currency.BRL)

    _record_extra_income(db, _deposit(2, "1500.00", month=month, pluggy_transaction_id="pg-d1"), pm)
    db.flush()
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("1500.00")

    # Next sync: the same window, plus a deposit that posted after the first one.
    _record_extra_income(db, _deposit(2, "1500.00", month=month, pluggy_transaction_id="pg-d1"), pm)
    _record_extra_income(db, _deposit(21, "320.00", month=month, pluggy_transaction_id="pg-d2"), pm)
    db.flush()

    assert _receipt_count(db, month, IncomeSource.EXTRA_BRL) == 2
    assert _total(db, month, IncomeSource.EXTRA_BRL) == Decimal("1820.00")


# ---------- rents: lag-1, and per-deposit ----------


def test_rent_deposits_are_booked_to_the_funded_month_and_sum(db):
    """RENTS_BRAZIL follows lag-1 (a deposit at the end of X funds X+1), and
    two deposits in one funded month now sum. The pre-ledger version kept
    whatever total the month already had, silently dropping the second.
    """
    pm = _checking(db, "Receipt Rents Checking", Currency.BRL)
    activity_month, funded_month = 10, 11

    _record_rent_deposit(
        db,
        _deposit(
            28, "1900.00", month=activity_month,
            description="Rent received, unit A",
            classification=CheckingClass.RENT_DEPOSIT,
            pluggy_transaction_id="pg-r1",
        ),
        pm,
    )
    _record_rent_deposit(
        db,
        _deposit(
            30, "1250.00", month=activity_month,
            description="Rent received, unit B",
            classification=CheckingClass.RENT_DEPOSIT,
            pluggy_transaction_id="pg-r2",
        ),
        pm,
    )
    db.flush()

    assert _entry(db, activity_month, IncomeSource.RENTS_BRAZIL) is None
    assert _total(db, funded_month, IncomeSource.RENTS_BRAZIL) == Decimal("3150.00")
    assert _receipt_count(db, funded_month, IncomeSource.RENTS_BRAZIL) == 2


# ---------- salary invariants ----------


def test_a_salary_receipt_is_one_per_funded_month(db):
    """Salary sources are period-scoped, so a second deposit for a month the
    paycheck is already booked for changes nothing. Without this, two deposits
    in one window would sum and break "salary gross is invariant per pay
    level" (CLAUDE.md).
    """
    draft = income_service.ReceiptDraft(
        source=IncomeSource.PARTNER_SALARY,
        year=YEAR,
        month=12,
        receipt_date=date(YEAR, 11, 28),
        amount=Decimal("3000.00"),
        currency=Currency.USD,
        provenance=income_service.PROVENANCE_PLAID,
        plaid_transaction_id="pl-s1",
        description="Payroll deposit",
    )
    income_service.record_receipt(db, draft)

    # A different transaction, a different amount, same funded month.
    second = income_service.ReceiptDraft(
        **{
            **draft.__dict__,
            "amount": Decimal("2900.00"),
            "plaid_transaction_id": "pl-s2",
            "receipt_date": date(YEAR, 11, 29),
        }
    )
    _, created = income_service.record_receipt(db, second)
    income_service.recompute_month(db, YEAR, 12, IncomeSource.PARTNER_SALARY)
    db.flush()

    assert created is False
    assert _receipt_count(db, 12, IncomeSource.PARTNER_SALARY) == 1
    assert _total(db, 12, IncomeSource.PARTNER_SALARY) == Decimal("3000.00")


def test_salary_signature_is_the_period_not_the_transaction(db):
    for source in income_service.PERIOD_SCOPED_SOURCES:
        draft = income_service.ReceiptDraft(
            source=source,
            year=2030,
            month=4,
            receipt_date=date(2030, 3, 31),
            amount=Decimal("1.00"),
            currency=Currency.USD,
            provenance=income_service.PROVENANCE_PLAID,
            plaid_transaction_id="whatever",
        )
        assert income_service.receipt_signature(draft) == f"salary:{source.value}:2030-04"


def test_non_salary_signature_prefers_the_provider_id(db):
    base = dict(
        source=IncomeSource.EXTRA_BRL,
        year=2030,
        month=4,
        receipt_date=date(2030, 4, 2),
        amount=Decimal("10.00"),
        currency=Currency.BRL,
        provenance=income_service.PROVENANCE_PLUGGY,
        payment_method_id=7,
        description="Pix",
    )
    with_provider = income_service.ReceiptDraft(**base, pluggy_transaction_id="pg-x")
    without = income_service.ReceiptDraft(**base)

    assert income_service.receipt_signature(with_provider) == "pluggy:pg-x"
    assert income_service.receipt_signature(without).startswith("stmt:extra_brl:7:2030-04-02:10.00:")


# ---------- recompute semantics ----------


def test_recompute_deletes_the_entry_when_its_last_receipt_goes(db):
    month = 1
    pm = _checking(db, "Receipt Recompute Delete Checking", Currency.USD)
    _record_extra_income(db, _deposit(8, "42.00", month=month, plaid_transaction_id="pl-e1"), pm)
    db.flush()
    assert _entry(db, month, IncomeSource.EXTRA_USD) is not None

    receipt = db.scalars(
        select(IncomeReceipt).filter_by(year=YEAR, month=month, source=IncomeSource.EXTRA_USD)
    ).one()
    db.delete(receipt)
    income_service.recompute_month(db, YEAR, month, IncomeSource.EXTRA_USD)
    db.flush()

    assert _entry(db, month, IncomeSource.EXTRA_USD) is None


def test_recompute_attaches_the_month_rate_when_it_creates_the_entry(db):
    """The rate attachment used to live in manual create_income. It now lives
    in the only place a monthly row is born."""
    pm = _checking(db, "Receipt Rate Attach Checking", Currency.BRL)
    # The fixture household seeds exchange rates in 2026, so a 2026 month has a
    # rate at or before its month end.
    income_service.record_receipt(
        db,
        income_service.ReceiptDraft(
            source=IncomeSource.EXTRA_BRL,
            year=2026,
            month=9,
            receipt_date=date(2026, 9, 4),
            amount=Decimal("55.00"),
            currency=Currency.BRL,
            provenance=income_service.PROVENANCE_STATEMENT,
            payment_method_id=pm.id,
            description="Rate attach probe",
        ),
    )
    row = income_service.recompute_month(db, 2026, 9, IncomeSource.EXTRA_BRL)
    db.flush()

    assert row is not None
    assert row.exchange_rate_id is not None
    assert row.exchange_rate_effective is not None


def test_mixed_currency_receipts_in_one_period_are_refused(db):
    """Not reachable through any writer (source and currency are decided
    together), so it is an integrity assertion rather than a code path."""
    common = dict(
        source=IncomeSource.EXTRA_BRL,
        year=YEAR,
        month=12,
        receipt_date=date(YEAR, 12, 3),
        provenance=income_service.PROVENANCE_STATEMENT,
        payment_method_id=None,
    )
    income_service.record_receipt(
        db,
        income_service.ReceiptDraft(
            **common, amount=Decimal("10.00"), currency=Currency.BRL, description="brl one"
        ),
    )
    income_service.record_receipt(
        db,
        income_service.ReceiptDraft(
            **common, amount=Decimal("20.00"), currency=Currency.USD, description="usd one"
        ),
    )
    db.flush()

    with pytest.raises(income_service.MixedCurrencyReceiptsError):
        income_service.recompute_month(db, YEAR, 12, IncomeSource.EXTRA_BRL)


def test_an_unknown_provenance_is_refused(db):
    with pytest.raises(ValueError):
        income_service.record_receipt(
            db,
            income_service.ReceiptDraft(
                source=IncomeSource.EXTRA_USD,
                year=YEAR,
                month=12,
                receipt_date=date(YEAR, 12, 1),
                amount=Decimal("1.00"),
                currency=Currency.USD,
                provenance="typo",
            ),
        )


# ---------- the pre-ledger backfill ----------


_BACKFILL_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "2026_08_20_1100-backfill_income_receipts.py"
)

# The public export ships a squashed migration baseline, so this private-only
# migration file does not exist there and the tests that replay its SQL skip.
requires_backfill_migration = pytest.mark.skipif(
    not _BACKFILL_MIGRATION_PATH.exists(),
    reason="backfill migration is private history, not part of the public export",
)


def _backfill_migration():
    """Load the backfill migration module so the test runs its real SQL.

    Migration modules import nothing from `app`, so importing one by path is
    safe and keeps this test from drifting away from what actually ran against
    production.
    """
    path = _BACKFILL_MIGRATION_PATH
    spec = importlib.util.spec_from_file_location("backfill_income_receipts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_backfill(db) -> None:
    module = _backfill_migration()
    db.execute(text(module.BACKFILL_SQL), {"description": module.BACKFILL_DESCRIPTION})
    db.flush()


@requires_backfill_migration
def test_backfill_reproduces_an_existing_total_exactly(db):
    """A row that predates receipts. The backfill must leave its number
    untouched to the cent, and recompute must agree."""
    month = 2
    db.add(
        IncomeEntry(
            year=2029,
            month=month,
            source=IncomeSource.EXTRA_BRL,
            amount=Decimal("2070.00"),
            currency=Currency.BRL,
        )
    )
    db.flush()

    _run_backfill(db)

    lump = db.scalars(
        select(IncomeReceipt).filter_by(
            year=2029, month=month, source=IncomeSource.EXTRA_BRL
        )
    ).one()
    assert lump.provenance == income_service.PROVENANCE_BACKFILL
    assert Decimal(lump.amount) == Decimal("2070.00")
    assert lump.receipt_date == date(2029, 2, 28)  # month end, not the 1st
    assert lump.payment_method_id is None
    assert lump.signature == "legacy:extra_brl:2029-02"

    row = income_service.recompute_month(db, 2029, month, IncomeSource.EXTRA_BRL)
    assert row is not None
    assert Decimal(row.amount) == Decimal("2070.00")


@requires_backfill_migration
def test_backfill_is_repeatable(db):
    db.add(
        IncomeEntry(
            year=2029,
            month=3,
            source=IncomeSource.EXTRA_USD,
            amount=Decimal("310.00"),
            currency=Currency.USD,
        )
    )
    db.flush()

    _run_backfill(db)
    _run_backfill(db)

    assert (
        len(
            db.scalars(
                select(IncomeReceipt).filter_by(
                    year=2029, month=3, source=IncomeSource.EXTRA_USD
                )
            ).all()
        )
        == 1
    )


@requires_backfill_migration
def test_a_lumped_month_holds_its_total_when_a_resync_re_presents_deposits(db):
    """The migration hazard, pinned down.

    Plaid and Pluggy re-pull their whole window on every commit, so the first
    sync after the ledger ships re-presents deposits that are already inside a
    lump. Summing both would double-count them. The lump holds instead, and the
    observed receipts are recorded and flagged as not counted.
    """
    pm = _checking(db, "Receipt Lump Checking", Currency.BRL)
    db.add(
        IncomeEntry(
            year=2029,
            month=4,
            source=IncomeSource.EXTRA_BRL,
            amount=Decimal("2070.00"),
            currency=Currency.BRL,
        )
    )
    db.flush()
    _run_backfill(db)

    receipt = CheckingActivity(
        activity_date=date(2029, 4, 2),
        description="Pix received, already inside the lump",
        amount=Decimal("1500.00"),
        running_balance=None,
        classification=CheckingClass.EXTRA_INCOME,
        pluggy_transaction_id="pg-lump-1",
    )
    note = _record_extra_income(db, receipt, pm)
    db.flush()

    entry = db.scalar(
        select(IncomeEntry).filter_by(year=2029, month=4, source=IncomeSource.EXTRA_BRL)
    )
    assert Decimal(entry.amount) == Decimal("2070.00")
    assert "pre-ledger total held" in note

    rows = income_service.list_receipts(
        db, year=2029, month=4, source=IncomeSource.EXTRA_BRL
    )
    counted = {r.provenance: r.counts_toward_total for r in rows}
    assert counted == {
        income_service.PROVENANCE_BACKFILL: True,
        income_service.PROVENANCE_PLUGGY: False,
    }


@requires_backfill_migration
def test_retiring_the_lump_makes_the_month_fully_derived(db):
    """Deleting the backfill receipt is the migration path off a frozen month."""
    pm = _checking(db, "Receipt Retire Lump Checking", Currency.BRL)
    db.add(
        IncomeEntry(
            year=2029,
            month=5,
            source=IncomeSource.EXTRA_BRL,
            amount=Decimal("2070.00"),
            currency=Currency.BRL,
        )
    )
    db.flush()
    _run_backfill(db)

    for idx, amount in (("1", "1500.00"), ("2", "570.00")):
        _record_extra_income(
            db,
            CheckingActivity(
                activity_date=date(2029, 5, 2),
                description=f"Pix received, part {idx}",
                amount=Decimal(amount),
                running_balance=None,
                classification=CheckingClass.EXTRA_INCOME,
                pluggy_transaction_id=f"pg-retire-{idx}",
            ),
            pm,
        )
    db.flush()

    lump = db.scalars(
        select(IncomeReceipt).filter_by(
            year=2029, month=5, provenance=income_service.PROVENANCE_BACKFILL
        )
    ).one()
    # What `delete_receipt` does, minus its commit. The commit matters here:
    # `_run_backfill` lumps EVERY income_entries row, including the fixture
    # household's, so committing would leak those lumps into the session-scoped
    # test schema and change what later tests see. The HTTP path through
    # `delete_receipt` is covered in test_income_api.py, where nothing but its
    # own rows exist.
    db.delete(lump)
    row = income_service.recompute_month(db, 2029, 5, IncomeSource.EXTRA_BRL)
    db.flush()

    assert row is not None
    assert Decimal(row.amount) == Decimal("2070.00")  # the observed receipts, now counted
    assert not income_service.has_backfill_lump(
        db, 2029, 5, IncomeSource.EXTRA_BRL
    )

    # From here on the month behaves like any post-ledger month.
    _record_extra_income(
        db,
        CheckingActivity(
            activity_date=date(2029, 5, 22),
            description="Pix received, genuinely new",
            amount=Decimal("300.00"),
            running_balance=None,
            classification=CheckingClass.EXTRA_INCOME,
            pluggy_transaction_id="pg-retire-3",
        ),
        pm,
    )
    db.flush()
    entry = db.scalar(
        select(IncomeEntry).filter_by(year=2029, month=5, source=IncomeSource.EXTRA_BRL)
    )
    assert Decimal(entry.amount) == Decimal("2370.00")
