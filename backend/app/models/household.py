"""Household configuration — who lives here and how their pay behaves.

Before these tables existed, the importer matched `users.name` against
hardcoded member names, the partner's pay levels were a module constant, and
the projection rules named specific bank accounts. All of that is
per-household data, so it belongs in the database next to
`categorization_rules`, not in the source tree.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency, HouseholdRole, IncomeSource
from app.models.payment_method import PaymentMethod
from app.models.user import User


class HouseholdMember(TimestampMixin, Base):
    """One earner in the household, and everything the importer needs to route
    their paycheck without knowing their name."""

    __tablename__ = "household_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    role: Mapped[HouseholdRole] = mapped_column(
        SAEnum(HouseholdRole, name="household_role"), nullable=False, unique=True
    )
    # What the statement parsers emit as `match_hint` for this member's salary
    # deposit. Kept separate from `users.name` so renaming a user is safe.
    match_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    salary_income_source: Mapped[IncomeSource] = mapped_column(
        SAEnum(IncomeSource, name="income_source"), nullable=False
    )
    # True when the paycheck arrives net of withholdings tracked as FIXED Tax
    # rows, which the salary import reconciles against the real deposit. False
    # for a gross deposit whose taxes arrive later as their own payments.
    has_withholdings: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Where the paycheck lands, used by the overdraft projection.
    salary_checking_pm_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    # Day the deposit is expected; 99 means "end of month".
    salary_day_of_month: Mapped[int] = mapped_column(Integer, nullable=False, default=99)

    user: Mapped[User] = relationship(User, lazy="joined")
    salary_checking: Mapped[PaymentMethod | None] = relationship(PaymentMethod, lazy="joined")
    salary_levels: Mapped[list["SalaryLevel"]] = relationship(
        "SalaryLevel", back_populates="member", lazy="selectin"
    )

    @property
    def display_name(self) -> str:
        return self.user.name

    def __repr__(self) -> str:
        return f"<HouseholdMember {self.role.value} match_key={self.match_key!r}>"


class SalaryLevel(TimestampMixin, Base):
    """A member's gross pay from `effective_year`-`effective_month` onward.

    Replaces the old hardcoded schedule. A raise is a new row; **existing rows
    are never edited**, because a historical month has to reconcile against the
    gross that was actually in force then — editing one makes the importer read
    an old raise as a tax change.
    """

    __tablename__ = "salary_levels"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "effective_year", "effective_month", name="uq_salary_level_member_month"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("household_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    effective_year: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_month: Mapped[int] = mapped_column(Integer, nullable=False)
    gross: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False, default=Currency.USD
    )

    member: Mapped[HouseholdMember] = relationship(HouseholdMember, back_populates="salary_levels")

    @property
    def effective_from(self) -> date:
        return date(self.effective_year, self.effective_month, 1)

    def __repr__(self) -> str:
        return f"<SalaryLevel m{self.member_id} {self.effective_year}-{self.effective_month:02d} {self.gross}>"


class WithholdingMerchant(TimestampMixin, Base):
    """Merchants whose FIXED Tax rows are this member's withholdings.

    Used two ways: the salary import rebalances them against the net deposit,
    and the cashflow projection subtracts them from gross to forecast net pay.
    """

    __tablename__ = "withholding_merchants"
    __table_args__ = (
        UniqueConstraint("member_id", "merchant_id", name="uq_withholding_member_merchant"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("household_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )

    member: Mapped[HouseholdMember] = relationship(HouseholdMember)
