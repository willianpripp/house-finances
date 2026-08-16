from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin
from app.models.enums import PlaidItemStatus
from app.models.user import User


class PlaidItem(Base, TimestampMixin):
    """One Plaid "Item" = one bank connection. A single Item can expose
    multiple accounts (one Item at an issuer can expose its credit card, its
    checking and its savings account, all linked via the same
    access_token). Each Plaid account
    maps to one `payment_methods` row via `payment_methods.plaid_account_id`."""
    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Plaid identifiers
    item_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    institution_id: Mapped[str] = mapped_column(String(50), nullable=False)
    institution_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Encrypted at rest via Fernet (key in FERNET_KEY env). Service layer
    # encrypts on write / decrypts on use.
    access_token: Mapped[str] = mapped_column(Text, nullable=False)

    # Cursor for /transactions/sync — opaque string returned by Plaid.
    last_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[PlaidItemStatus] = mapped_column(
        SAEnum(PlaidItemStatus, name="plaid_item_status"),
        nullable=False,
        default=PlaidItemStatus.ACTIVE,
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_skipped_unmapped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship("User", lazy="joined")

    def __repr__(self) -> str:
        return f"<PlaidItem {self.institution_name} ({self.status.value})>"
