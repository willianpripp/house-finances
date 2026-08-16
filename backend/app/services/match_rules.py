"""Load `statement_match_rules` into the `MatchRules` shape parsers consume.

Loaded once per import (or Plaid review) and passed down; parsers never see
the database. Rules apply within a class in `sort_order`, then id, so the
"earlier keyword wins" contract from the old in-code tuples is preserved.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StatementMatchRule
from app.services.parsers.checking import MatchRules


def load_match_rules(session: Session) -> MatchRules:
    rows = session.scalars(
        select(StatementMatchRule)
        .where(StatementMatchRule.active.is_(True))
        .order_by(StatementMatchRule.sort_order, StatementMatchRule.id)
    ).all()

    def pairs(cls: str) -> tuple[tuple[str, str], ...]:
        return tuple((r.keyword, r.match_hint) for r in rows if r.classification == cls)

    def keywords(cls: str) -> tuple[str, ...]:
        return tuple(r.keyword for r in rows if r.classification == cls)

    return MatchRules(
        cc_payment=pairs("CC_PAYMENT"),
        salary=pairs("SALARY"),
        rent_deposit=pairs("RENT_DEPOSIT"),
        tax_payment=keywords("TAX_PAYMENT"),
        interest=keywords("INTEREST"),
        internal_transfer=keywords("INTERNAL_TRANSFER"),
        extra_income=keywords("EXTRA_INCOME"),
        noise_prefixes=keywords("NOISE"),
        holder_names=keywords("HOLDER_NAME"),
    )
