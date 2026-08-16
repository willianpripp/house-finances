from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base
from app.models.base import TimestampMixin
from app.models.user import User


class ImportLog(Base, TimestampMixin):
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Text, not an enum type: the valid values come from the parser registry
    # plus `ImportSource`, checked below. See services/import_sources.py for
    # why the database is no longer the authority here.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_methods.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    user: Mapped[User | None] = relationship("User", lazy="joined")

    @validates("source")
    def _validate_source(self, _key: str, value: object) -> str:
        """Every write path converges here, so this is where an unrecognised
        source is rejected — the check the Postgres enum used to provide.
        Fires on assignment only, never on load: rows written by a parser this
        tree no longer ships must still read back."""
        from app.services.import_sources import normalize_import_source

        return normalize_import_source(value)

    def __repr__(self) -> str:
        return f"<ImportLog {self.filename} ({self.source}) {self.transaction_count} txn>"
