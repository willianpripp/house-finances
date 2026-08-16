from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.user import User


class PluggyItem(Base, TimestampMixin):
    """One Pluggy "item" = one bank connection (Open Finance consent). A
    single item can expose multiple accounts; each maps to one
    `payment_methods` row via `payment_methods.pluggy_account_id`.

    Unlike PlaidItem there is no per-connection secret: Pluggy auth is
    app-level (CLIENT_ID/SECRET -> short-lived apiKey), items are referenced
    by their UUID only. The API deliberately has no list-items endpoint, so
    this table is the only inventory of connections we hold — ids are
    captured at creation (widget callback) or pasted manually for
    connections authorized on meu.pluggy.ai."""

    __tablename__ = "pluggy_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Pluggy identifiers. item_id is a UUID string.
    item_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    connector_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connector_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    # Provider vocabulary stored verbatim (UPDATED, OUTDATED, LOGIN_ERROR,
    # WAITING_USER_INPUT, UPDATING, ...) — it is theirs to evolve, so no enum.
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Investments are a separate Pluggy product (GET /investments), not
    # accounts: positions churn (each Nubank money-box deposit is a new CDB
    # id, closed ones linger as zeros), so tracking is per ITEM, aggregated:
    # the balance refresh sums the item's positions into one daily savings
    # snapshot under this payment method. Null = not tracked. Balance only,
    # never transactions (box moves are INTERNAL_TRANSFER on the checking
    # side already).
    investments_payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped[User] = relationship("User", lazy="joined")

    def __repr__(self) -> str:
        return f"<PluggyItem {self.connector_name} ({self.status})>"
