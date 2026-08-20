from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum as SAEnum, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency
from app.models.payment_method import PaymentMethod


class SpendGoal(Base, TimestampMixin):
    """A spend-to-earn tracker on one payment method: reach `target_amount`
    in purchases between `start_date` and `deadline` to unlock whatever
    `reward_note` describes (a card signup bonus, most often). Generic on
    purpose — signup bonuses recur across cards, this table is not
    Samsung-specific.

    Progress is computed on read by `services/spend_goals.py`, never stored:
    same "derive, don't cache" approach as the live credit-card balance.
    """
    __tablename__ = "spend_goals"
    __table_args__ = (
        UniqueConstraint("payment_method_id", "start_date", name="uq_spend_goals_pm_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE"), nullable=False
    )
    target_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False
    )
    start_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    deadline: Mapped[date_type] = mapped_column(Date, nullable=False)
    reward_note: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    payment_method: Mapped[PaymentMethod] = relationship("PaymentMethod", lazy="joined")

    def __repr__(self) -> str:
        return f"<SpendGoal pm={self.payment_method_id} target={self.target_amount} {self.currency.value}>"
