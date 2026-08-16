from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Date, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class CarLoanPayment(Base, TimestampMixin):
    __tablename__ = "car_loan_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    payment_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    principal_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    interest_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    new_balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    def __repr__(self) -> str:
        return f"<CarPayment {self.posting_date} -{self.payment_amount} bal={self.new_balance}>"
