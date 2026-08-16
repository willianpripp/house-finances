from sqlalchemy import Boolean, String
from sqlalchemy import true as sa_true
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class Person(Base, TimestampMixin):
    """A non-household person who owes money for charges put on our cards —
    family/friends (e.g. in-laws splitting a restaurant bill). Used by the
    'A Receber' (receivables) tab. NOT a system user."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    relation: Mapped[str | None] = mapped_column(String(40))  # sogro, sogra, pai...
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_true(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Person {self.name} ({self.relation})>"
