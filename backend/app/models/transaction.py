from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum as SAEnum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.category import Category
from app.models.enums import Currency, RecurrenceKind
from app.models.merchant import Merchant
from app.models.payment_method import PaymentMethod
from app.models.user import User


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "transaction_date",
            "merchant_id",
            "amount",
            "payment_method_id",
            "created_by_user_id",
            name="uq_transaction_signature",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    transaction_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    merchant_id: Mapped[int] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    payment_method_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="RESTRICT"), nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        SAEnum(Currency, name="currency"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(500))

    # Plaid's stable transaction id, set only on Plaid-origin rows. It is the
    # dedup key for auto-pull (unique where not null). Manual/parsed rows keep
    # it null and dedupe via uq_transaction_signature instead.
    plaid_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Pluggy's stable transaction id (UUID), set only on Pluggy-origin rows.
    # Same contract as plaid_transaction_id: unique where not null; dedupes
    # re-pulls of the Pluggy source only — cross-source duplicates are the
    # signature dedupe's job (provider ids differ per source for the same
    # real transaction).
    pluggy_transaction_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Plaid pending (provisional) — set at commit; cleared when the posted
    # version reconciles. Surfaced in the monthly report.
    pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    installment_current: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    installment_total: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    installment_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    recurrence_kind: Mapped[RecurrenceKind | None] = mapped_column(
        SAEnum(RecurrenceKind, name="recurrence_kind"),
        nullable=True,
    )
    contract_end_date: Mapped[date_type | None] = mapped_column(Date)

    import_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_logs.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    merchant: Mapped[Merchant] = relationship("Merchant", lazy="joined")
    category: Mapped[Category] = relationship("Category", lazy="joined")
    payment_method: Mapped[PaymentMethod] = relationship("PaymentMethod", lazy="joined")
    created_by: Mapped[User | None] = relationship("User", lazy="joined")

    def __repr__(self) -> str:
        return f"<Transaction {self.transaction_date} {self.amount} {self.currency.value}>"
