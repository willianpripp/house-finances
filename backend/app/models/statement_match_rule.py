"""Description-matching rules for the checking-statement classifier.

These rows replace the keyword tuples that used to live in
`parsers/checking.py`, the same move `categorization_rules` made for
merchant categorization. The classifier's
class priority stays in code (`classify_description`); what the household
actually matches on is data.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class StatementMatchRule(Base, TimestampMixin):
    __tablename__ = "statement_match_rules"
    __table_args__ = (UniqueConstraint("classification", "keyword"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # A CheckingClass name (CC_PAYMENT, SALARY, ...) or NOISE for statement
    # boilerplate prefixes. String, not the enum type: NOISE is not an
    # activity class, and rules are matching config, not ledger state.
    classification: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Uppercase substring matched against the uppercased description
    # (prefix-matched against raw lines for NOISE).
    keyword: Mapped[str] = mapped_column(String(120), nullable=False)
    # Card name for CC_PAYMENT, household match_key for SALARY/RENT_DEPOSIT,
    # empty for keyword-only classes.
    match_hint: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    # Within a class, lower sort_order matches first.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<StatementMatchRule {self.classification}:{self.keyword!r}>"
