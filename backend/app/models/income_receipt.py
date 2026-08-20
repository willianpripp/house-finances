from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, Enum as SAEnum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency, IncomeSource
from app.models.payment_method import PaymentMethod


class IncomeReceipt(Base, TimestampMixin):
    """One constituent receipt of income: a single deposit, paycheck or Pix.

    `income_entries` used to be the only income table, one row per
    (year, month, source) holding a total that an importer either created or
    refused to touch. That shape has no provenance, which is why a deposit
    landing after a month's first provider sync could not be added: two
    different BRL checking accounts both feed EXTRA_BRL, so recomputing the
    month from one provider's window would erase the other's contribution. The
    month total was therefore frozen once set and a human had to retype it
    (limitation recorded 2026-08-17). This table is that missing grain: every
    receipt is its own row, and `income_entries` is now derived from it by
    `services/income.recompute_month`.

    `year` / `month` are the income month the receipt FUNDS, not the month it
    arrived in. The two differ by design: salary and BR rents follow the
    lag-by-1-month rule (a deposit at the end of month X funds X+1), extras are
    booked in the calendar month they arrived. Which rule applies is decided by
    the writer, so storing the funded period explicitly keeps the recompute a
    plain GROUP BY instead of a rule engine.

    `signature` is the idempotency key, unique across the table, and it carries
    two deliberately different grains (built by
    `services/income.receipt_signature`):

    - Salaries are period-scoped (`salary:<source>:<YYYY-MM>`). A paycheck is
      one receipt per funded month, never a sum of deposits: the partner's
      gross is household configuration (`salary_levels`) and summing two
      deposits into one month would break the "salary gross is invariant per
      pay level" rule outright.
    - Everything else is per-transaction: the provider transaction id when the
      row came from Plaid or Pluggy, otherwise a deterministic signature over
      (payment method, date, amount, source, description). Two Pix in one month
      are two receipts and legitimately sum.
    """

    __tablename__ = "income_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[IncomeSource] = mapped_column(
        SAEnum(IncomeSource, name="income_source"), nullable=False
    )
    # The income month this receipt funds. See the class docstring: this is not
    # derivable from receipt_date alone, because the lag-1 rule applies to
    # salary and rents but not to extras.
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the money actually arrived, distinct from the funded (year, month)
    # above and what makes a late-posting deposit recognisable as such.
    receipt_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False
    )

    # Nullable because backfilled legacy rows have no account to point at: the
    # monthly totals they reproduce predate any per-receipt provenance.
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )

    # Same contract as on `transactions`: set only on rows that came from that
    # provider, unique where not null. Redundant with `signature` for provider
    # rows on purpose, so a provider transaction feeding two receipts fails at
    # the database rather than double-counting a month.
    plaid_transaction_id: Mapped[str | None] = mapped_column(String(100))
    pluggy_transaction_id: Mapped[str | None] = mapped_column(String(64))

    # Where this row came from: see PROVENANCES in services/income.py. Plain
    # text rather than a Postgres enum, for the reason import_logs.source is
    # text (app/models/enums.py, ImportSource): adding an ingestion path should
    # not need a migration.
    provenance: Mapped[str] = mapped_column(String(20), nullable=False)

    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    signature: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)

    payment_method: Mapped[PaymentMethod | None] = relationship(
        "PaymentMethod", lazy="joined"
    )

    def __repr__(self) -> str:
        return (
            f"<IncomeReceipt {self.receipt_date} {self.source.value} "
            f"{self.amount} {self.currency.value} -> {self.year}-{self.month:02d}>"
        )
