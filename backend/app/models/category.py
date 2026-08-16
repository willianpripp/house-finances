from sqlalchemy import Boolean
from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import CategoryType


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    type: Mapped[CategoryType] = mapped_column(
        SAEnum(CategoryType, name="category_type"), nullable=False
    )
    color: Mapped[str] = mapped_column(String(7), default="#94a3b8", nullable=False)
    icon: Mapped[str | None] = mapped_column(String(50))
    # When true, transactions in this category are excluded from spending /
    # surplus totals — they're transfers to equity, not consumption (e.g.
    # "Car Extra" = loan principal paydown, captured in car_loan_payments).
    exclude_from_spending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Category {self.name} ({self.type.value})>"
