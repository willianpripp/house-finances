from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.exchange_rate import ExchangeRate


class MonthlySnapshot(Base, TimestampMixin):
    """Frozen v1-import fossil, not a live table.

    Only 4 rows exist (Jan-Apr 2026, frozen 2026-05-02/05) and there is no
    write path in v2.5 code that inserts or updates this table anymore
    (`/reports/monthly` recomputes everything live from `transactions` +
    `exchange_rates` + `savings_snapshots` + `credit_card_balances`, and only
    labels these 4 pre-existing rows "Snapshot" in the UI). The rows stay
    because the migrated v1 data for that period is unstable and this is the
    only record of it; do not add a new writer, and do not drop the table or
    its data as part of unrelated cleanup. See CLAUDE.md's Architectural
    Decisions section for the full rationale.
    """

    __tablename__ = "monthly_snapshots"
    __table_args__ = (UniqueConstraint("year", "month", name="uq_snapshot_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False)

    primary_salary_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    partner_salary_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    rents_brazil_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    extra_income_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    fixed_spending_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    variable_spending_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    taxes_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    net_income_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    surplus_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    total_savings_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    total_debt_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    net_worth_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    exchange_rate_id: Mapped[int | None] = mapped_column(
        ForeignKey("exchange_rates.id", ondelete="SET NULL")
    )
    is_finalized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    exchange_rate: Mapped[ExchangeRate | None] = relationship("ExchangeRate", lazy="joined")

    def __repr__(self) -> str:
        status = "finalized" if self.is_finalized else "live"
        return f"<MonthlySnapshot {self.year}-{self.month:02d} ({status})>"
