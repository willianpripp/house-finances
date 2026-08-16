from decimal import Decimal

from sqlalchemy import Enum as SAEnum, ForeignKey, Integer, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency, IncomeSource
from app.models.exchange_rate import ExchangeRate


class IncomeEntry(Base, TimestampMixin):
    __tablename__ = "income_entries"
    __table_args__ = (
        UniqueConstraint("year", "month", "source", name="uq_income_period_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[IncomeSource] = mapped_column(
        SAEnum(IncomeSource, name="income_source"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False
    )
    exchange_rate_id: Mapped[int | None] = mapped_column(
        ForeignKey("exchange_rates.id", ondelete="SET NULL")
    )

    exchange_rate: Mapped[ExchangeRate | None] = relationship("ExchangeRate", lazy="joined")

    def __repr__(self) -> str:
        return f"<Income {self.year}-{self.month:02d} {self.source.value} {self.amount} {self.currency.value}>"
