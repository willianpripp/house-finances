"""Read side of the household configuration.

Everything the importer and the projections used to know by hardcoded name is
looked up here instead. Nothing in this module knows who lives in the house.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HouseholdMember, HouseholdRole, IncomeSource, Merchant, WithholdingMerchant


def member_by_match_key(session: Session, match_key: str | None) -> HouseholdMember | None:
    """Resolve the member a parser's `match_hint` refers to (case-insensitive)."""
    if not match_key:
        return None
    return session.scalar(
        select(HouseholdMember).where(HouseholdMember.match_key.ilike(match_key))
    )


def member_by_role(session: Session, role: HouseholdRole) -> HouseholdMember | None:
    return session.scalar(select(HouseholdMember).where(HouseholdMember.role == role))


def member_by_income_source(session: Session, source: IncomeSource) -> HouseholdMember | None:
    return session.scalar(
        select(HouseholdMember).where(HouseholdMember.salary_income_source == source)
    )


def all_members(session: Session) -> list[HouseholdMember]:
    return list(session.scalars(select(HouseholdMember).order_by(HouseholdMember.role)).all())


def gross_for_month(member: HouseholdMember, year: int, month: int) -> Decimal | None:
    """The member's gross for the income month `year`-`month`.

    Picks the latest level effective on or before that month, so a raise applies
    going forward and historical months keep reconciling against the gross that
    was in force then. Returns None when the member has no levels configured.
    """
    applicable = [
        level
        for level in member.salary_levels
        if (level.effective_year, level.effective_month) <= (year, month)
    ]
    if not applicable:
        return None
    latest = max(applicable, key=lambda level: (level.effective_year, level.effective_month))
    return Decimal(latest.gross)


_SALARY_LABEL_FALLBACKS: tuple[tuple[HouseholdRole, str, str], ...] = (
    (HouseholdRole.PRIMARY, "primary_salary", "Primary Salary"),
    (HouseholdRole.PARTNER, "partner_salary", "Partner Salary"),
)


def salary_labels(session: Session) -> dict[str, str]:
    """Display labels for the two salary income sources, keyed by enum value.

    The pages that list income sources used to spell the members out in their
    JavaScript. The names are household data, so they are resolved here and
    injected into the template context instead. A database with no members
    configured yet (fresh install, demo seed not run) falls back to the role
    names rather than failing the page.
    """
    labels: dict[str, str] = {}
    for role, key, fallback in _SALARY_LABEL_FALLBACKS:
        member = member_by_role(session, role)
        labels[key] = f"{member.match_key} Salary" if member and member.match_key else fallback
    return labels


def withholding_merchant_names(session: Session, member: HouseholdMember) -> tuple[str, ...]:
    rows = session.scalars(
        select(Merchant.name)
        .join(WithholdingMerchant, WithholdingMerchant.merchant_id == Merchant.id)
        .where(WithholdingMerchant.member_id == member.id)
        .order_by(Merchant.name)
    ).all()
    return tuple(rows)
