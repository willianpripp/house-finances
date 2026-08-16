from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, Enum as SAEnum, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import AssetKind, Currency


class Asset(Base, TimestampMixin):
    """A material asset whose value is tracked separately from cash savings.

    Updated manually (typically once a year). Surfaces in monthly/annual
    reports as Total Worth = (savings - debt) + sum(assets in USD).
    """
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[AssetKind] = mapped_column(
        SAEnum(AssetKind, name="asset_kind"), nullable=False
    )
    location: Mapped[str | None] = mapped_column(String(120))
    acquired_date: Mapped[date_type | None] = mapped_column(Date)
    current_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False
    )
    last_valued_date: Mapped[date_type | None] = mapped_column(Date)
    last_service_date: Mapped[date_type | None] = mapped_column(Date)
    next_service_due_date: Mapped[date_type | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(500))

    def __repr__(self) -> str:
        return f"<Asset {self.name}: {self.current_value} {self.currency.value}>"
