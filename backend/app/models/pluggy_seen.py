from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PluggySeenTransaction(Base):
    """A Pluggy transaction id already reviewed + committed through the
    /connections flow — including no-op rows that leave no trace in
    `transactions`. Same role as PlaidSeenTransaction: the review re-pulls
    the whole window, so without this the same handled rows reappear
    forever. Dedupes re-pulls of the Pluggy source only; cross-source
    duplicates (manual import vs Pluggy of the same real transaction) are
    caught by the signature dedupe, never by provider ids."""

    __tablename__ = "pluggy_seen_transactions"

    pluggy_transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL")
    )
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
