from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlaidSeenTransaction(Base):
    """A Plaid transaction id that has been reviewed + committed through the
    /connections flow — including no-op rows (INTERNAL_TRANSFER, deduped CC
    payments, FIXED matches) that leave no trace in `transactions`. The review
    re-pulls the whole window each time, so without this the same handled rows
    reappear forever. The preview hides ids present here (or in `transactions`)
    so a re-pull shows only genuinely new activity."""

    __tablename__ = "plaid_seen_transactions"

    plaid_transaction_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
