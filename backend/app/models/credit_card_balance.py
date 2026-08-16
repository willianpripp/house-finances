from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.payment_method import PaymentMethod


class CreditCardBalance(Base, TimestampMixin):
    __tablename__ = "credit_card_balances"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="CASCADE"), nullable=False, index=True
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    statement: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    payment_method: Mapped[PaymentMethod] = relationship("PaymentMethod", lazy="joined")

    def __repr__(self) -> str:
        return f"<CCBalance {self.payment_method.name}: {self.balance}>"
