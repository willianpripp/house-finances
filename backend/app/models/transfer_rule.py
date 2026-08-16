from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.category import Category
from app.models.merchant import Merchant


class TransferRule(Base, TimestampMixin):
    """Maps a recurring checking transfer to a category/merchant by
    (payment_method, exact amount). Checking transfers all share a generic
    description ("Online Transfer / Payment: Debit"), so the amount is the only
    stable key. The review pre-fills the category override from a match; the
    user just confirms. Used for fixed monthly bills paid by transfer (rent,
    gym, phone, car)."""

    __tablename__ = "transfer_rules"
    __table_args__ = (
        UniqueConstraint("payment_method_id", "amount", name="uq_transfer_rule_pm_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="SET NULL")
    )

    category: Mapped[Category] = relationship(Category, lazy="joined")
    merchant: Mapped[Merchant | None] = relationship(Merchant, lazy="joined")
