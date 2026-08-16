from datetime import date

from sqlalchemy import Date, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class ExchangeRate(Base, TimestampMixin):
    __tablename__ = "exchange_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    commercial: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    spread: Mapped[float] = mapped_column(Numeric(6, 4), default=0.015, nullable=False)
    iof: Mapped[float] = mapped_column(Numeric(6, 4), default=0.011, nullable=False)
    effective: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)

    def __repr__(self) -> str:
        return f"<ExchangeRate {self.rate_date}: BRL {self.effective}>"
