from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.category import Category
from app.models.merchant import Merchant


class CategorizationRule(Base, TimestampMixin):
    __tablename__ = "categorization_rules"
    # Uniqueness on (keyword, amount): a keyword can have an amount-scoped
    # variant per value (e.g. "google" $70 → Fiber, "google" $2.50 → Services)
    # plus one amount-agnostic rule (amount NULL).
    __table_args__ = (UniqueConstraint("keyword", "amount", name="uq_rule_keyword_amount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(100), nullable=False)
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    # Optional amount scope: when set, only matches a transaction of this
    # (absolute) amount; NULL = matches any amount.
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    merchant: Mapped[Merchant] = relationship("Merchant", lazy="joined")
    category: Mapped[Category] = relationship("Category", lazy="joined")

    def __repr__(self) -> str:
        return f"<Rule '{self.keyword}' -> {self.merchant.name}>"
