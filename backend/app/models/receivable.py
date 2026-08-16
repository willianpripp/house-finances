from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency, ReceivableDirection
from app.models.payment_method import PaymentMethod
from app.models.person import Person


class Receivable(Base, TimestampMixin):
    """One person's share of a bill, in either direction (see `direction`).

    OWED_TO_ME: a charge made on one of our cards, to be paid back later. A
    split across N people produces N rows sharing a `group_id`. The charge
    stays in the ledger as normal spending; this table only tracks who owes
    what until they settle (then it's extra income — handled at settle time,
    not here).

    I_OWE: someone else paid and we owe them our share. Nothing is in the
    ledger, because the money never left our accounts; the real expense is
    logged manually when we actually pay them back. Settling only marks the
    debt closed — same as the other direction."""

    __tablename__ = "receivables"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Shared across the rows of one split purchase (NULL for a single-person
    # charge). Lets the UI show "Dinner $90: Jordan Blake $30, Priya Nair $30".
    group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    direction: Mapped[ReceivableDirection] = mapped_column(
        SAEnum(ReceivableDirection, name="receivable_direction"),
        nullable=False,
        default=ReceivableDirection.OWED_TO_ME,
        server_default=ReceivableDirection.OWED_TO_ME.value,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False, default=Currency.USD
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    store: Mapped[str | None] = mapped_column(String(120))
    # Which of our cards took the charge. Always null for I_OWE rows: the
    # charge landed on someone else's card, not ours.
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    charge_date: Mapped[date] = mapped_column(Date, nullable=False)
    settled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false(), nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship(Person, lazy="joined")
    payment_method: Mapped[PaymentMethod | None] = relationship(PaymentMethod, lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<Receivable {self.direction.value} {self.person_id} ${self.amount} "
            f"{'paid' if self.settled else 'open'}>"
        )
