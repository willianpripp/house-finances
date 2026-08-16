from sqlalchemy import Boolean, Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Currency, PaymentMethodType


class PaymentMethod(Base, TimestampMixin):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    type: Mapped[PaymentMethodType] = mapped_column(
        SAEnum(PaymentMethodType, name="payment_method_type"), nullable=False
    )
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), default=Currency.USD, nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Overdraft forecast wiring. For CREDIT_CARD rows, points to
    # the CHECKING row that autopays this card. None means "not configured"
    # → the card is excluded from overdraft projections. For non-CC rows,
    # always None.
    paid_from_payment_method_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True
    )

    # Per-card statement-close-day + due-day. Both
    # nullable: only meaningful for CREDIT_CARD rows and CHECKING rows that
    # have a known cycle. Filled per-parser as faturas land (item 12a).
    statement_close_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Plaid auto-pull linkage. A PM with a non-null `plaid_account_id` is
    # fed by Plaid: its transactions come from /transactions/sync and its
    # balance from /accounts/balance/get. Manual import is blocked for it.
    plaid_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("plaid_items.id", ondelete="SET NULL"), nullable=True
    )
    plaid_account_id: Mapped[str | None] = mapped_column(
        String(100), unique=True, nullable=True
    )

    # Pluggy auto-pull linkage (BR accounts), same semantics as the Plaid
    # pair above: non-null `pluggy_account_id` means "fed by Pluggy" and
    # manual import is blocked for it.
    pluggy_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pluggy_items.id", ondelete="SET NULL"), nullable=True
    )
    pluggy_account_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )

    def __repr__(self) -> str:
        return f"<PaymentMethod {self.name} ({self.currency.value})>"
