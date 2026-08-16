"""Deterministic fixture household.

Two jobs:

1. Today, it is the fixture the characterization tests run against.
2. Later it becomes the base of `scripts/seed_demo.py` for the public repo
   (the public-export plan, Phase 3), which is why nothing here is random and
   nothing here is real.

`PRIMARY_NAME` / `PARTNER_NAME` are fictional, and that is the point: since
Phase 1 the importer resolves the salary owner through `household_members`
config rather than by matching hardcoded names, so the suite passes with any
names at all.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    StatementMatchRule,
    Category,
    CategoryType,
    Currency,
    ExchangeRate,
    HouseholdMember,
    HouseholdRole,
    IncomeEntry,
    IncomeSource,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    Person,
    Receivable,
    ReceivableDirection,
    SalaryLevel,
    RecurrenceKind,
    Transaction,
    User,
    WithholdingMerchant,
)

# Fictional — nothing in app/ knows or cares what these are.
PRIMARY_NAME = "Alex Costa"   # foreign-currency salary, gross deposit
PARTNER_NAME = "Sam Costa"    # local salary, withholdings reconciled by the importer

# Aliases so a rename of the enum members touches one line each, not every test.
PRIMARY_SALARY = IncomeSource.PRIMARY_SALARY
PARTNER_SALARY = IncomeSource.PARTNER_SALARY

PARTNER_GROSS = Decimal("3000.00")
PARTNER_GROSS_AFTER_RAISE = Decimal("3300.00")  # exercises the salary_levels lookup
PRIMARY_GROSS_FOREIGN = Decimal("9000.00")
FED_WITHHOLDING = Decimal("300.00")
STATE_WITHHOLDING = Decimal("90.00")
ACCOUNTANT_FEE = Decimal("150.00")      # INDEFINITE Taxes fee — must roll
FOREIGN_TAX_PAYMENT = Decimal("800.00")  # variable tax payment — must NOT roll
CAR_INSTALLMENT = Decimal("425.00")
STREAMING = Decimal("12.50")


class Ids:
    """Filled in by seed_household so tests can address rows without querying."""

    primary_user: int
    partner_user: int
    checking_usd: int
    card_usd: int
    checking_foreign: int
    card_foreign: int
    taxes_category: int
    fed_row_jul: int
    state_row_jul: int
    accountant_row_jul: int
    foreign_tax_row_jul: int
    car_row_jul: int
    streaming_row_jul: int
    person_a: int
    person_b: int
    primary_member: int
    partner_member: int


def _month_rows(
    session: Session,
    ids: Ids,
    year: int,
    month: int,
    merchants: dict[str, Merchant],
    categories: dict[str, Category],
) -> dict[str, Transaction]:
    """One month of FIXED rows, mirroring the real shapes the services care about."""
    rows: dict[str, Transaction] = {}

    rows["fed"] = Transaction(
        transaction_date=date(year, month, 1),
        merchant_id=merchants["Federal Withholding"].id,
        category_id=categories["Taxes"].id,
        payment_method_id=ids.checking_usd,
        amount=FED_WITHHOLDING,
        currency=Currency.USD,
        recurrence_kind=RecurrenceKind.INDEFINITE,
        created_by_user_id=ids.primary_user,
    )
    rows["state"] = Transaction(
        transaction_date=date(year, month, 1),
        merchant_id=merchants["State Withholding"].id,
        category_id=categories["Taxes"].id,
        payment_method_id=ids.checking_usd,
        amount=STATE_WITHHOLDING,
        currency=Currency.USD,
        recurrence_kind=RecurrenceKind.INDEFINITE,
        created_by_user_id=ids.primary_user,
    )
    # Fixed monthly fee that happens to sit in Taxes: rolls forward.
    rows["accountant"] = Transaction(
        transaction_date=date(year, month, 14),
        merchant_id=merchants["Accountant"].id,
        category_id=categories["Taxes"].id,
        payment_method_id=ids.card_foreign,
        amount=ACCOUNTANT_FEE,
        currency=Currency.BRL,
        recurrence_kind=RecurrenceKind.INDEFINITE,
        created_by_user_id=ids.primary_user,
    )
    # Variable tax payment: arrives via import, must never roll.
    rows["foreign_tax"] = Transaction(
        transaction_date=date(year, month, 20),
        merchant_id=merchants["Tax Authority"].id,
        category_id=categories["Taxes"].id,
        payment_method_id=ids.checking_foreign,
        amount=FOREIGN_TAX_PAYMENT,
        currency=Currency.BRL,
        recurrence_kind=None,
        created_by_user_id=ids.primary_user,
    )
    rows["car"] = Transaction(
        transaction_date=date(year, month, 4),
        merchant_id=merchants["Car Loan"].id,
        category_id=categories["Car"].id,
        payment_method_id=ids.checking_usd,
        amount=CAR_INSTALLMENT,
        currency=Currency.USD,
        recurrence_kind=RecurrenceKind.INSTALLMENT,
        installment_current=18 + month,
        installment_total=72,
        installment_value=CAR_INSTALLMENT,
        created_by_user_id=ids.primary_user,
    )
    # INDEFINITE but not Taxes: the importer propagates it, rollover must skip.
    rows["streaming"] = Transaction(
        transaction_date=date(year, month, 17),
        merchant_id=merchants["Streaming Co"].id,
        category_id=categories["Streaming"].id,
        payment_method_id=ids.card_usd,
        amount=STREAMING,
        currency=Currency.USD,
        recurrence_kind=RecurrenceKind.INDEFINITE,
        created_by_user_id=ids.primary_user,
    )
    rows["groceries"] = Transaction(
        transaction_date=date(year, month, 9),
        merchant_id=merchants["Market"].id,
        category_id=categories["Groceries"].id,
        payment_method_id=ids.card_usd,
        amount=Decimal("120.00"),
        currency=Currency.USD,
        created_by_user_id=ids.primary_user,
    )

    for row in rows.values():
        session.add(row)
    session.flush()
    return rows


def seed_household(session: Session) -> Ids:
    ids = Ids()

    primary = User(email="primary@example.test", name=PRIMARY_NAME)
    partner = User(email="partner@example.test", name=PARTNER_NAME)
    session.add_all([primary, partner])
    session.flush()
    ids.primary_user, ids.partner_user = primary.id, partner.id

    categories = {
        name: Category(name=name, type=ctype)
        for name, ctype in (
            ("Variable", CategoryType.VARIABLE),  # categorizer's required default
            ("Groceries", CategoryType.VARIABLE),
            ("Rent", CategoryType.FIXED),
            ("Taxes", CategoryType.FIXED),
            ("Streaming", CategoryType.FIXED),
            ("Car", CategoryType.FIXED),
        )
    }
    session.add_all(categories.values())
    session.flush()
    ids.taxes_category = categories["Taxes"].id

    merchants = {
        name: Merchant(name=name)
        for name in (
            "Market",
            "Landlord",
            "Federal Withholding",
            "State Withholding",
            "Accountant",
            "Tax Authority",
            "Streaming Co",
            "Car Loan",
        )
    }
    session.add_all(merchants.values())
    session.flush()

    checking_usd = PaymentMethod(
        name="Main Checking", type=PaymentMethodType.CHECKING, currency=Currency.USD
    )
    checking_foreign = PaymentMethod(
        name="Foreign Checking", type=PaymentMethodType.CHECKING, currency=Currency.BRL
    )
    session.add_all([checking_usd, checking_foreign])
    session.flush()

    card_usd = PaymentMethod(
        name="Rewards Card",
        type=PaymentMethodType.CREDIT_CARD,
        currency=Currency.USD,
        paid_from_payment_method_id=checking_usd.id,
        statement_close_day=20,
        due_day=10,
    )
    card_foreign = PaymentMethod(
        name="Foreign Card",
        type=PaymentMethodType.CREDIT_CARD,
        currency=Currency.BRL,
        paid_from_payment_method_id=checking_foreign.id,
        due_day=14,
    )
    session.add_all([card_usd, card_foreign])
    session.flush()

    ids.checking_usd = checking_usd.id
    ids.checking_foreign = checking_foreign.id
    ids.card_usd = card_usd.id
    ids.card_foreign = card_foreign.id

    # --- household configuration (Phase 1): who earns what, and how ---
    partner_member = HouseholdMember(
        user_id=partner.id,
        role=HouseholdRole.PARTNER,
        match_key=PARTNER_NAME.split()[0],
        salary_income_source=PARTNER_SALARY,
        has_withholdings=True,
        salary_checking_pm_id=checking_usd.id,
        salary_day_of_month=99,
    )
    primary_member = HouseholdMember(
        user_id=primary.id,
        role=HouseholdRole.PRIMARY,
        match_key=PRIMARY_NAME.split()[0],
        salary_income_source=PRIMARY_SALARY,
        has_withholdings=False,
        salary_checking_pm_id=checking_foreign.id,
        salary_day_of_month=99,
    )
    session.add_all([partner_member, primary_member])
    session.flush()
    ids.partner_member = partner_member.id
    ids.primary_member = primary_member.id

    session.add_all(
        [
            SalaryLevel(
                member_id=partner_member.id,
                effective_year=1900,
                effective_month=1,
                gross=PARTNER_GROSS,
                currency=Currency.USD,
            ),
            SalaryLevel(
                member_id=partner_member.id,
                effective_year=2026,
                effective_month=8,
                gross=PARTNER_GROSS_AFTER_RAISE,
                currency=Currency.USD,
            ),
        ]
    )
    session.add_all(
        [
            WithholdingMerchant(member_id=partner_member.id, merchant_id=merchants[name].id)
            for name in ("Federal Withholding", "State Withholding")
        ]
    )
    session.flush()

    for month in (6, 7, 8):
        session.add(
            ExchangeRate(
                rate_date=date(2026, month, 1),
                commercial=Decimal("5.0000"),
                spread=Decimal("1.0150"),
                iof=Decimal("1.0110"),
                effective=Decimal("5.1281"),
            )
        )
    session.flush()

    for month in (6, 7):
        session.add_all(
            [
                IncomeEntry(
                    year=2026,
                    month=month,
                    source=PARTNER_SALARY,
                    amount=PARTNER_GROSS,
                    currency=Currency.USD,
                ),
                IncomeEntry(
                    year=2026,
                    month=month,
                    source=PRIMARY_SALARY,
                    amount=PRIMARY_GROSS_FOREIGN,
                    currency=Currency.BRL,
                ),
            ]
        )
    session.flush()

    _month_rows(session, ids, 2026, 6, merchants, categories)
    july = _month_rows(session, ids, 2026, 7, merchants, categories)
    ids.fed_row_jul = july["fed"].id
    ids.state_row_jul = july["state"].id
    ids.accountant_row_jul = july["accountant"].id
    ids.foreign_tax_row_jul = july["foreign_tax"].id
    ids.car_row_jul = july["car"].id
    ids.streaming_row_jul = july["streaming"].id

    person_a = Person(name="Person A")
    person_b = Person(name="Person B")
    session.add_all([person_a, person_b])
    session.flush()
    ids.person_a, ids.person_b = person_a.id, person_b.id

    session.add_all(
        [
            Receivable(
                person_id=person_a.id,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("80.00"),
                currency=Currency.USD,
                description="Dinner",
                charge_date=date(2026, 7, 5),
            ),
            Receivable(
                person_id=person_a.id,
                direction=ReceivableDirection.I_OWE,
                amount=Decimal("30.00"),
                currency=Currency.USD,
                description="Concert ticket",
                charge_date=date(2026, 7, 12),
            ),
            Receivable(
                person_id=person_b.id,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("45.00"),
                currency=Currency.USD,
                description="Groceries",
                charge_date=date(2026, 7, 18),
            ),
        ]
    )
    session.flush()

    # Statement match rules — the fictional household's classifier config.
    # The migration seeds the real household's rules unconditionally, so wipe
    # those first: the fixture world is fully fictional, rules included.
    session.query(StatementMatchRule).delete()
    session.add_all(
        [
            StatementMatchRule(classification="CC_PAYMENT", keyword="BANKCO AUTOPAY", match_hint="Blue Card", sort_order=10),
            StatementMatchRule(classification="SALARY", keyword="EMPLOYER X EDI", match_hint=PARTNER_NAME.split()[0], sort_order=10),
            StatementMatchRule(classification="SALARY", keyword="CONSULTCO LTDA", match_hint=PRIMARY_NAME.split()[0], sort_order=20),
            StatementMatchRule(classification="RENT_DEPOSIT", keyword="SAM COSTA PIX", match_hint=PRIMARY_NAME.split()[0], sort_order=10),
            StatementMatchRule(classification="TAX_PAYMENT", keyword="IRS USATAXPYMT", sort_order=10),
            StatementMatchRule(classification="INTEREST", keyword="INTEREST DEPOSIT", sort_order=10),
            StatementMatchRule(classification="INTERNAL_TRANSFER", keyword="ZELLE TO", sort_order=10),
            StatementMatchRule(classification="EXTRA_INCOME", keyword="TRANSFERENCIA RECEBIDA", sort_order=10),
            StatementMatchRule(classification="NOISE", keyword="ALEX COSTA", sort_order=10),
            StatementMatchRule(classification="HOLDER_NAME", keyword="ALEX COSTA", sort_order=10),
            StatementMatchRule(classification="HOLDER_NAME", keyword="SAM COSTA", sort_order=20),
            StatementMatchRule(classification="INTERNAL_TRANSFER", keyword="DEAD RULE", sort_order=5, active=False),
        ]
    )
    session.flush()

    return ids
