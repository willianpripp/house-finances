"""Match a transaction description against the categorization_rules table.

Rules are loaded once per Categorizer instance — keep one alive for the duration
of a single import to avoid hitting the DB per row.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, CategorizationRule, Merchant

DEFAULT_CATEGORY_NAME = "Variable"
MAX_MERCHANT_NAME_LEN = 100


@dataclass
class Match:
    merchant_name: str
    category_name: str
    category_id: int
    merchant_id: int | None  # None means "would create on commit"
    rule_id: int | None
    keyword: str | None


class Categorizer:
    def __init__(self, session: Session) -> None:
        self.session = session
        # Amount-scoped rules first (amount NOT NULL), so a "$70 google" rule
        # wins over an amount-agnostic "google" rule.
        self._rules: list[CategorizationRule] = list(session.scalars(
            select(CategorizationRule).order_by(
                CategorizationRule.amount.is_(None), CategorizationRule.priority
            )
        ).all())
        default = session.scalar(select(Category).filter_by(name=DEFAULT_CATEGORY_NAME))
        if default is None:
            raise RuntimeError(
                f"Default category '{DEFAULT_CATEGORY_NAME}' missing from categories table. "
                "Run scripts/seed_reference_data.py first."
            )
        self._default_category = default

    def classify(self, description: str, amount: Decimal | None = None) -> Match:
        desc_lower = description.lower()
        abs_amt = abs(amount) if amount is not None else None
        for rule in self._rules:
            if rule.keyword.lower() not in desc_lower:
                continue
            # Amount-scoped rule: only matches a transaction of that value.
            if rule.amount is not None and (abs_amt is None or abs(rule.amount) != abs_amt):
                continue
            return Match(
                    merchant_name=rule.merchant.name,
                    category_name=rule.category.name,
                    category_id=rule.category_id,
                    merchant_id=rule.merchant_id,
                    rule_id=rule.id,
                    keyword=rule.keyword,
                )

        fallback_name = (description.strip()[:MAX_MERCHANT_NAME_LEN]) or "Unknown"
        return Match(
            merchant_name=fallback_name,
            category_name=self._default_category.name,
            category_id=self._default_category.id,
            merchant_id=None,
            rule_id=None,
            keyword=None,
        )

    def get_or_create_merchant(self, name: str, default_category_id: int) -> Merchant:
        existing = self.session.scalar(select(Merchant).filter_by(name=name))
        if existing:
            return existing
        merchant = Merchant(name=name, default_category_id=default_category_id)
        self.session.add(merchant)
        self.session.flush()
        return merchant
