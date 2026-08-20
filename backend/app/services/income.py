"""Income: a per-receipt ledger with derived monthly totals.

Two tables, one direction of flow:

    income_receipts  (one row per deposit / paycheck / Pix)
        |  recompute_month()
        v
    income_entries   (one row per year+month+source, DERIVED)

`income_entries` stays a materialized table rather than becoming a view. Three
reasons, in order of weight: `services/reports.py`, `services/warnings.py` and
`services/home.py` all read it directly and their queries stay untouched; it
carries `exchange_rate_id`, which is per-row state a view would have to
recompute in SQL; and the historical rows imported from v1 keep working with no
special case. So the read surface does not move at all — only the writer does.

**Nothing types an income amount any more.** There is no create, update or
delete of a monthly total: `record_receipt` + `recompute_month` are the only
writers, both called by the importers. Every one of the five sources has an
automatic writer in `services/checking_importer.py`
(`_record_primary_salary`, `_ensure_partner_salary`, `_record_rent_deposit`,
`_record_extra_income` for both EXTRA currencies), so there is nothing left for
a human to enter and the HTTP surface is read-only. Same call the owner made
for exchange rates on 2026-08-20: automation replaces manual entry, it does not
sit next to it.

What this buys, and why the schema change happened at all: the old importer
FROZE a month's extra-income total once it existed, because recomputing from
one provider's window would have erased another provider's deposits (two
different BRL checking accounts both feed EXTRA_BRL). With each deposit its own
row, recompute is simply a sum, and a deposit that posts after the month's
first sync lands on the next sync with no manual correction.

One period is exempt from that, and only one: a month whose total predates the
ledger. Those rows were backfilled as a single lump receipt whose constituents
were never recorded, so the lump holds the total until a human retires it. The
full reasoning is in `recompute_month`; it is the only place in this module
where a total is not a plain sum of what was observed.

**USD conversion is per receipt** (2026-08-20). A month's reported USD figure
for a BRL source is the sum of its receipts each converted at the rate in
force on its own `receipt_date`, not the whole month's native total converted
at one month-end rate. `convert_entry_to_usd` is that rule, and it lives here
rather than in `services/reports.py` because it needs two things this module
owns and reports does not: the receipt grain and the pre-ledger lump
exception. Reports keeps the other half of the split, deciding which period to
ask about, and calls this once from `_compute_income` so the monthly and the
annual report cannot diverge.

**What `income_entries.exchange_rate_id` means now.** It never was a
conversion input (reports have always re-resolved the rate at read time) and
after this change it is not one either. Its meaning is narrowed and written
down rather than left to be guessed: it is the month-end rate as it stood the
moment the monthly row was first created, kept as provenance of what the row
looked like then. Nothing reads it except `list_income`, which surfaces it for
a caller that wants that history. It is deliberately not removed in this
change (no schema churn for a column that still records something true), and
retiring it is a decision for a future migration, not a silent one.
"""
from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Currency, ExchangeRate, IncomeEntry, IncomeReceipt, IncomeSource
from app.services.exchange_rates import DatedRate, DatedRateCache

# Where a receipt came from. Plain strings, not a Postgres enum, for the reason
# `import_logs.source` is text: a new ingestion path should not need a
# migration.
PROVENANCE_PLAID = "plaid"
PROVENANCE_PLUGGY = "pluggy"
PROVENANCE_STATEMENT = "statement"  # parsed PDF or hand-pasted statement text
PROVENANCE_BACKFILL = "backfill"    # pre-ledger monthly lump, see the backfill migration
PROVENANCES = (
    PROVENANCE_PLAID,
    PROVENANCE_PLUGGY,
    PROVENANCE_STATEMENT,
    PROVENANCE_BACKFILL,
)

# Sources whose receipts are period-scoped rather than per-transaction. A
# paycheck is one receipt per funded month: the partner's gross is household
# configuration (`salary_levels`) and the primary's is a single monthly
# deposit, so summing two deposits into one month would break the "salary gross
# is invariant per pay level" rule in CLAUDE.md. Keeping these period-scoped
# also preserves exactly what the importer did before the ledger existed (an
# existing salary row was left as-is).
PERIOD_SCOPED_SOURCES = (IncomeSource.PRIMARY_SALARY, IncomeSource.PARTNER_SALARY)

_CENTS = Decimal("0.01")

# Which rule produced a month's USD figure, so a report can say so rather than
# presenting three different conversions as one kind of number.
RATE_BASIS_USD = "usd"                  # already USD, nothing was converted
RATE_BASIS_PER_RECEIPT = "per_receipt"  # sum of receipts at their own dates' rates
RATE_BASIS_MONTH_END = "month_end"      # one month-end rate over the whole total


class MixedCurrencyReceiptsError(ValueError):
    """One (year, month, source) holds receipts in more than one currency.

    Not reachable through any current writer — source and currency are decided
    together (EXTRA_BRL is BRL by construction, EXTRA_USD is USD, the salaries
    and rents take the account's currency) — so this is a data-integrity
    assertion, raised loudly rather than silently summing incomparable amounts.
    """


# ---------- monthly totals (derived, read-only) ----------


@dataclass(frozen=True)
class IncomeRow:
    id: int
    year: int
    month: int
    source: str
    amount: Decimal
    currency: str
    exchange_rate_id: int | None
    exchange_rate_effective: Decimal | None
    exchange_rate_date: date_type | None


@dataclass(frozen=True)
class IncomeListResult:
    rows: list[IncomeRow]
    sum_by_currency: dict[str, Decimal]


def _row(entry: IncomeEntry) -> IncomeRow:
    rate = entry.exchange_rate
    return IncomeRow(
        id=entry.id,
        year=entry.year,
        month=entry.month,
        source=entry.source.value,
        amount=entry.amount,
        currency=entry.currency.value,
        exchange_rate_id=entry.exchange_rate_id,
        exchange_rate_effective=Decimal(rate.effective) if rate else None,
        exchange_rate_date=rate.rate_date if rate else None,
    )


def list_income(
    session: Session,
    *,
    year: int | None = None,
    month: int | None = None,
    source: IncomeSource | None = None,
) -> IncomeListResult:
    stmt = select(IncomeEntry).order_by(
        IncomeEntry.year.desc(),
        IncomeEntry.month.desc(),
        IncomeEntry.source,
    )
    if year is not None:
        stmt = stmt.where(IncomeEntry.year == year)
    if month is not None:
        stmt = stmt.where(IncomeEntry.month == month)
    if source is not None:
        stmt = stmt.where(IncomeEntry.source == source)
    entries = session.scalars(stmt).all()

    rows = [_row(e) for e in entries]
    sums: dict[str, Decimal] = {}
    for r in rows:
        sums[r.currency] = sums.get(r.currency, Decimal("0")) + r.amount
    return IncomeListResult(rows=rows, sum_by_currency=sums)


def _latest_rate_for_month(session: Session, year: int, month: int) -> ExchangeRate | None:
    last_day = date_type(year, month, calendar.monthrange(year, month)[1])
    return session.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date <= last_day)
        .order_by(ExchangeRate.rate_date.desc())
        .limit(1)
    )


# ---------- receipts (the only writable grain) ----------


@dataclass(frozen=True)
class ReceiptRow:
    id: int
    source: str
    year: int
    month: int
    receipt_date: date_type
    amount: Decimal
    currency: str
    provenance: str
    payment_method_id: int | None
    payment_method_name: str | None
    plaid_transaction_id: str | None
    pluggy_transaction_id: str | None
    description: str
    # False for an observed receipt in a period still held by a pre-ledger lump:
    # the row is recorded and visible, but it is not part of the total, because
    # it may already be inside the lump. See `recompute_month`.
    counts_toward_total: bool = True


def _receipt_row(r: IncomeReceipt, *, counts_toward_total: bool = True) -> ReceiptRow:
    return ReceiptRow(
        id=r.id,
        source=r.source.value,
        year=r.year,
        month=r.month,
        receipt_date=r.receipt_date,
        amount=r.amount,
        currency=r.currency.value,
        provenance=r.provenance,
        payment_method_id=r.payment_method_id,
        payment_method_name=r.payment_method.name if r.payment_method else None,
        plaid_transaction_id=r.plaid_transaction_id,
        pluggy_transaction_id=r.pluggy_transaction_id,
        description=r.description,
        counts_toward_total=counts_toward_total,
    )


@dataclass(frozen=True)
class ReceiptDraft:
    """What a writer knows about one receipt.

    `year` / `month` are the income month the receipt FUNDS, which the caller
    decides: lag-1 for salary and BR rents, calendar month for extras (see the
    "Salary lag-by-1-month" rule in CLAUDE.md). `receipt_date` is when the
    money actually arrived.
    """

    source: IncomeSource
    year: int
    month: int
    receipt_date: date_type
    amount: Decimal
    currency: Currency
    provenance: str
    payment_method_id: int | None = None
    plaid_transaction_id: str | None = None
    pluggy_transaction_id: str | None = None
    description: str = ""


def receipt_signature(draft: ReceiptDraft) -> str:
    """The idempotency key for a receipt. Unique across the whole table.

    Two grains, deliberately:

    - Salaries (`PERIOD_SCOPED_SOURCES`) key on the funded period alone, so a
      second deposit for a month a paycheck was already booked for is a no-op.
      That is what keeps a gross invariant per pay level.
    - Everything else keys per transaction: the provider transaction id when
      Plaid or Pluggy supplied one (which is what makes a re-pulled window
      idempotent at the receipt grain instead of freezing the month), otherwise
      a deterministic signature over the account, date, amount, source and
      description.

    The statement signature inherits the trade-off already documented for
    `transactions` in CLAUDE.md: two genuinely distinct receipts that share
    account, date, amount and description collapse into one. For a hand-pasted
    statement that is the price of making a re-paste idempotent.
    """
    if draft.source in PERIOD_SCOPED_SOURCES:
        return f"salary:{draft.source.value}:{draft.year}-{draft.month:02d}"
    if draft.plaid_transaction_id:
        return f"plaid:{draft.plaid_transaction_id}"
    if draft.pluggy_transaction_id:
        return f"pluggy:{draft.pluggy_transaction_id}"
    digest = hashlib.sha1(draft.description.strip().upper().encode()).hexdigest()[:12]
    amount = Decimal(draft.amount).quantize(_CENTS)
    return (
        f"stmt:{draft.source.value}:{draft.payment_method_id}:"
        f"{draft.receipt_date.isoformat()}:{amount}:{digest}"
    )


def record_receipt(session: Session, draft: ReceiptDraft) -> tuple[IncomeReceipt, bool]:
    """Insert the receipt unless its signature is already present.

    Returns `(receipt, created)`. Does not commit, and does not recompute the
    monthly total: callers pair this with `recompute_month` so a single import
    can write several receipts for one month and derive the total once.
    """
    if draft.provenance not in PROVENANCES:
        raise ValueError(
            f"unknown receipt provenance {draft.provenance!r} (expected one of {PROVENANCES})"
        )
    signature = receipt_signature(draft)
    # The session runs with autoflush=False, so a receipt added earlier in the
    # same import is invisible to this SELECT until flushed. Without this, two
    # same-signature rows in one paste would both insert and collide on commit.
    session.flush()
    existing = session.scalar(select(IncomeReceipt).filter_by(signature=signature))
    if existing is not None:
        return existing, False

    receipt = IncomeReceipt(
        source=draft.source,
        year=draft.year,
        month=draft.month,
        receipt_date=draft.receipt_date,
        amount=Decimal(draft.amount).quantize(_CENTS),
        currency=draft.currency,
        payment_method_id=draft.payment_method_id,
        plaid_transaction_id=draft.plaid_transaction_id,
        pluggy_transaction_id=draft.pluggy_transaction_id,
        provenance=draft.provenance,
        description=(draft.description or "")[:500],
        signature=signature,
    )
    session.add(receipt)
    session.flush()
    return receipt, True


def _period_filter(year: int, month: int, source: IncomeSource) -> tuple:
    """The one definition of "this period's receipts".

    Shared by the recompute (which sums them) and by the conversion (which
    converts them one at a time), so the two can never come to disagree about
    which rows a monthly figure is made of.
    """
    return (
        IncomeReceipt.year == year,
        IncomeReceipt.month == month,
        IncomeReceipt.source == source,
    )


def has_backfill_lump(
    session: Session, year: int, month: int, source: IncomeSource
) -> bool:
    """True while a period's total is still the pre-ledger lump.

    See `recompute_month` for what that means for the total, and
    `delete_receipt` for how a period is moved off it.
    """
    return session.scalar(
        select(func.count(IncomeReceipt.id)).where(
            *_period_filter(year, month, source),
            IncomeReceipt.provenance == PROVENANCE_BACKFILL,
        )
    ) > 0


def recompute_month(
    session: Session, year: int, month: int, source: IncomeSource
) -> IncomeRow | None:
    """Re-derive `income_entries` for one (year, month, source) from receipts.

    Exact, in both directions: the entry's amount becomes the sum of that
    period's receipts, and the entry is DELETED when no receipts remain. A
    derived row that outlived its evidence would be a number nobody can explain,
    which is the situation this whole table exists to end.

    **The pre-ledger exception.** A period that still holds a `backfill` receipt
    (one lump per monthly row that existed before receipts did, written by
    migration c3a86f512e9d) sums ONLY its backfill receipts, and any observed
    receipt recorded for that period is stored and shown but not counted. Both
    halves matter:

    - Plaid and Pluggy re-pull their whole window from the clean-start anchor on
      every commit, so the first sync after this ships re-presents deposits that
      are already inside a lump. Summing lump + observed would double-count them
      — silently, on live months.
    - Netting them off instead ("subtract observed from the lump") would swallow
      a genuinely new deposit in the same window, which is the exact bug being
      fixed here.

    Neither is honest, because which observed receipts are inside a lump is
    information that was never recorded. So the lump holds the total, exactly as
    it reads today, until it is explicitly retired: delete the lump receipt
    (`delete_receipt`) once its window has been re-synced, and the period becomes
    fully derived from that point on. Months created after this ships have no
    lump and are derived from the first receipt onward, which is the whole point.

    On first creation the month's exchange rate is attached the same way manual
    entry used to attach it (`_latest_rate_for_month`). On an existing entry the
    rate is left alone: it is the rate captured when the month was first
    recorded, and no report reads it (reports convert per receipt, see
    `convert_entry_to_usd`). It is provenance of what the row looked like when
    it was born, so re-resolving it later would only make it drift. The module
    docstring records that narrowed meaning and why the column stays.

    Does not commit; the caller owns the transaction boundary.
    """
    session.flush()
    stmt = (
        select(IncomeReceipt.currency, func.sum(IncomeReceipt.amount))
        .where(*_period_filter(year, month, source))
        .group_by(IncomeReceipt.currency)
    )
    if has_backfill_lump(session, year, month, source):
        stmt = stmt.where(IncomeReceipt.provenance == PROVENANCE_BACKFILL)
    grouped = session.execute(stmt).all()

    entry = session.scalar(
        select(IncomeEntry).filter_by(year=year, month=month, source=source)
    )

    if not grouped:
        if entry is not None:
            session.delete(entry)
            session.flush()
        return None

    if len(grouped) > 1:
        currencies = sorted(c.value for c, _ in grouped)
        raise MixedCurrencyReceiptsError(
            f"{year}-{month:02d} {source.value} has receipts in {currencies}; "
            f"a monthly total cannot sum across currencies"
        )

    currency, total = grouped[0]
    total = Decimal(total).quantize(_CENTS)

    if entry is None:
        rate = _latest_rate_for_month(session, year, month)
        entry = IncomeEntry(
            year=year,
            month=month,
            source=source,
            amount=total,
            currency=currency,
            exchange_rate_id=rate.id if rate else None,
        )
        session.add(entry)
    else:
        entry.amount = total
        entry.currency = currency

    session.flush()
    return _row(entry)


# ---------- USD conversion (per receipt) ----------


@dataclass(frozen=True)
class EntryConversion:
    """One monthly row's USD figure, plus how it was arrived at.

    `amount_usd` is NOT quantized: the caller both displays it and adds it into
    gross income, and rounding each source to the cent before summing five of
    them moves the total. Quantize at the display edge, once.
    """

    amount_usd: Decimal
    rate_basis: str
    approximate: bool


def _counted_receipts(
    session: Session, year: int, month: int, source: IncomeSource
) -> tuple[list[IncomeReceipt], bool]:
    """The receipts a period's total is actually made of, and whether a
    pre-ledger lump is what holds it.

    Applies the same lump rule as `recompute_month`: while a `backfill` receipt
    is present it is the only thing counted, and the observed receipts recorded
    for that period are excluded. Converting the excluded ones would put money
    into the USD figure that is not in the native total.
    """
    lumped = has_backfill_lump(session, year, month, source)
    stmt = (
        select(IncomeReceipt)
        .where(*_period_filter(year, month, source))
        .order_by(IncomeReceipt.receipt_date, IncomeReceipt.id)
    )
    if lumped:
        stmt = stmt.where(IncomeReceipt.provenance == PROVENANCE_BACKFILL)
    return list(session.scalars(stmt).all()), lumped


def convert_entry_to_usd(
    session: Session,
    entry: IncomeEntry,
    *,
    month_end: DatedRate,
    rates: DatedRateCache | None = None,
) -> EntryConversion:
    """One monthly row's USD figure: each receipt at its own date's rate.

    Why per receipt (owner's call, 2026-08-20). Converting the month's whole
    native total at "the latest rate on or before the reference day" means the
    open month is re-priced every time the daily PTAX run lands a row, so last
    week's salary quietly changes value this afternoon. A receipt is money that
    arrived on a day; the rate that day is a fact and does not become a
    different fact later. So a deposit dated the 5th converts at the 5th's rate
    forever, and today's PTAX row can only affect receipts dated today or
    later. Reported USD for the month is the sum of those conversions.

    Weekend and holiday arrivals need no special case: `rate_for_date` reads
    the latest row at or before the date, and PTAX fills business days, so a
    Saturday deposit converts at Friday's close. A date with no rate at or
    before it falls back to the earliest rate on file and comes back
    `approximate`, which is the report's cue to say the figure is an estimate.

    Three deliberate exceptions:

    - **USD sources are untouched.** No rate is read at all, no conversion
      happens, and `rate_basis` says so. Mixing currencies is a hard rule in
      CLAUDE.md; a receipt is converted by ITS OWN currency, never the entry's,
      which is what keeps that true even if a period ever held both.
    - **Pre-ledger lumps keep the month-end convention.** A `backfill` receipt
      has a synthetic month-end `receipt_date` standing in for constituents
      nobody recorded, so its own date carries no information and converting
      "at its date" would only dress a month-end conversion up as a per-receipt
      one. Using `month_end` explicitly instead means every month whose total
      predates the ledger reports exactly the number it reports today, which is
      also what keeps the migration a no-op for reports.
    - **A monthly row with no receipts at all** (a legacy row that never got a
      lump, or a hand-built fixture) converts at `month_end` too. Falling back
      to the old rule beats reporting zero USD against a non-zero native total.

    `month_end` is the caller's: the live report resolves it from the rate
    table, the finalized-snapshot path passes the rate frozen into the
    snapshot. Neither is re-derived here, because which month-end rate applies
    is a report's question, not this module's.
    """
    if entry.currency == Currency.USD:
        return EntryConversion(Decimal(entry.amount), RATE_BASIS_USD, False)

    receipts, lumped = _counted_receipts(session, entry.year, entry.month, entry.source)
    if not receipts:
        return EntryConversion(
            Decimal(entry.amount) / month_end.effective,
            RATE_BASIS_MONTH_END,
            month_end.approximate,
        )

    rates = rates if rates is not None else DatedRateCache(session)
    total = Decimal("0")
    approximate = False
    for receipt in receipts:
        if receipt.currency == Currency.USD:
            total += Decimal(receipt.amount)
            continue
        rate = (
            month_end
            if receipt.provenance == PROVENANCE_BACKFILL
            else rates.for_date(receipt.receipt_date)
        )
        total += Decimal(receipt.amount) / rate.effective
        approximate = approximate or rate.approximate

    return EntryConversion(
        total,
        RATE_BASIS_MONTH_END if lumped else RATE_BASIS_PER_RECEIPT,
        approximate,
    )


def list_receipts(
    session: Session,
    *,
    year: int | None = None,
    month: int | None = None,
    source: IncomeSource | None = None,
) -> list[ReceiptRow]:
    """The receipts behind the monthly totals, newest period first.

    Filters match `list_income` so a caller can query the same window on both
    and line the two up itself, without a join in the read path. Has no HTTP
    route of its own since the `/income` page was removed (2026-08-20); kept
    as a service function for `test_income_receipts.py` and any future
    consumer, the way the totals-only `list_income` already was.

    `counts_toward_total` is resolved here rather than per row in SQL: a period
    still held by a pre-ledger lump counts only the lump, so every observed
    receipt in that period is flagged as not counted (see `recompute_month`).
    """
    stmt = select(IncomeReceipt).order_by(
        IncomeReceipt.year.desc(),
        IncomeReceipt.month.desc(),
        IncomeReceipt.source,
        IncomeReceipt.receipt_date,
        IncomeReceipt.id,
    )
    if year is not None:
        stmt = stmt.where(IncomeReceipt.year == year)
    if month is not None:
        stmt = stmt.where(IncomeReceipt.month == month)
    if source is not None:
        stmt = stmt.where(IncomeReceipt.source == source)
    receipts = session.scalars(stmt).all()

    lumped = {
        (r.year, r.month, r.source)
        for r in receipts
        if r.provenance == PROVENANCE_BACKFILL
    }
    return [
        _receipt_row(
            r,
            counts_toward_total=(
                r.provenance == PROVENANCE_BACKFILL
                or (r.year, r.month, r.source) not in lumped
            ),
        )
        for r in receipts
    ]


def delete_receipt(session: Session, receipt_id: int) -> IncomeRow | None:
    """Drop one receipt and re-derive its month. Commits.

    The escape hatch, not a way to enter income: same role DELETE keeps on
    /api/exchange-rates. Two uses:

    1. Removing a wrong receipt (a misclassified deposit).
    2. **Retiring a pre-ledger lump.** Deleting a `backfill` receipt is how a
       legacy month stops being frozen at its pre-ledger total and becomes fully
       derived from the receipts actually observed for it. Do it once the
       month's window has been re-synced, so the observed receipts are complete;
       until then the lump is the safer number (see `recompute_month`).

    Caveat before using it on a provider row: Plaid and Pluggy re-pull their
    whole window from the clean-start anchor on every commit, so a deleted
    provider receipt is recreated by the next sync. Deleting is durable only for
    `statement` and `backfill` receipts.

    Returns the month's recomputed total, or None when that was its last receipt.
    """
    receipt = session.get(IncomeReceipt, receipt_id)
    if receipt is None:
        raise LookupError(f"Income receipt {receipt_id} not found")
    year, month, source = receipt.year, receipt.month, receipt.source
    session.delete(receipt)
    row = recompute_month(session, year, month, source)
    session.commit()
    return row
