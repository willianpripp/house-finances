from datetime import date

from sqlalchemy import Date, Numeric, String
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
    # 'ptax' (unattended fetch, scripts/refresh_exchange_rate.py, daily run
    # or --backfill) or 'manual' (historical: rows that predate the auto
    # path, migration 7d1f4a92c6b3, or entered by an import script under
    # scripts/). There is no HTTP path to 'manual' rows anymore (2026-08-20):
    # the column default stays for those historical/import cases, but
    # nothing in the running app writes a new 'manual' row.
    source: Mapped[str] = mapped_column(String(16), default="manual", nullable=False)

    def __repr__(self) -> str:
        return f"<ExchangeRate {self.rate_date}: BRL {self.effective}>"
