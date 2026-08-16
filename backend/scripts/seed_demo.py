"""Deterministic demo data for a fresh database.

Fills an empty database with a fictional household so every page of the app has
something plausible on it: KPIs, twelve months of transactions, salaries with
withholdings, an installment series, a contract that expires, receivables in
both directions and both currencies, savings and card-balance snapshots, FX
rates, assets and a car loan.

Deterministic on purpose: one fixed seed and a fixed window, the whole of
calendar 2026, so screenshots taken from it are reproducible run to run and the
annual report has all twelve months of the year on it.

Nothing here is real. The household is Alex Rivera (primary earner, foreign
salary paid gross) and Sam Rivera (partner, local salary net of withholdings).

**Demo login:** `alex@example.com` or `sam@example.com`, password `demo1234`.

Run it inside the app container, where `DATABASE_URL` is already set:

    docker compose run --rm -T app python scripts/seed_demo.py

It refuses to run against a database that already has users, so it can never
be pointed at a populated ledger.
"""
from __future__ import annotations

import random
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

# Runnable as `python scripts/seed_demo.py`: sys.path[0] is then scripts/, so
# the `app` package would not be importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    Asset,
    AssetKind,
    CarLoanPayment,
    Category,
    CategorizationRule,
    CategoryType,
    CreditCardBalance,
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
    RecurrenceKind,
    SalaryLevel,
    SavingsSnapshot,
    StatementMatchRule,
    Transaction,
    User,
    WithholdingMerchant,
)
from app.services.auth import hash_password
from app.services.exchange_rates import DEFAULT_IOF, DEFAULT_SPREAD, compute_effective

SEED = 42
DEMO_PASSWORD = "demo1234"

# The whole of calendar 2026: January through December, inclusive. A window
# aligned to the year is what makes /reports/annual show twelve bars instead of
# a partial year.
WINDOW_END = (2026, 12)
WINDOW_MONTHS = 12

# The month the demo is written "as of". Everything after it is a projection:
# the ledger legitimately holds future-dated rows (that is what rollover
# produces), reports recompute live, and the live card-balance derivation
# already ignores anything dated ahead of today. Two things are anchored here
# rather than to the end of the window: the FX pin, and the sparse card
# snapshots, because "latest recorded balance" is read as *current* debt and a
# row recorded four months from now would be a strange thing to show a viewer.
DEMO_PRESENT = (2026, 8)

PRIMARY_NAME = "Alex Rivera"
PARTNER_NAME = "Sam Rivera"
PRIMARY_EMAIL = "alex@example.com"
PARTNER_EMAIL = "sam@example.com"

CHECKING_USD = "Meridian Checking"
SAVINGS_USD = "Summit High-Yield Savings"
CARD_MAIN = "Aurora Bank Visa"
CARD_SECOND = "Beacon Rewards Card"
CARD_STORE = "Harbor Store Card"
CHECKING_BRL = "Vale Conta Corrente"

# The partner's pay levels: a baseline plus one raise inside the window, which
# is what exercises the salary_levels lookup instead of a single flat figure.
PARTNER_GROSS = Decimal("4150.00")
PARTNER_GROSS_AFTER_RAISE = Decimal("4400.00")
PRIMARY_GROSS_BRL = Decimal("19750.00")
PRIMARY_GROSS_BRL_AFTER_RAISE = Decimal("20900.00")
RAISE_FROM = (2026, 3)

# The car note, in one place because two passes have to agree on it: the
# monthly FIXED transaction and the car_loan_payments trajectory. A 48-month
# loan of ~$25.5k at 7.2% nominal, opened well before the window (the car is
# the 2019 sedan on /assets, bought 2024-03): 22 payments are already behind it
# when 2026 opens, so the twelve months in view run #23 to #34 and #35 is the
# next one due. Amortising CAR_LOAN_OPENING_BALANCE twelve times at
# CAR_LOAN_MONTHLY_RATE ends the year at $8,201.54, which is what the remaining
# 14 payments are worth discounted at the same rate ($8,200.36, the cent of
# drift being the per-row rounding of interest).
CAR_LOAN_TERM = 48
CAR_LOAN_PAYMENT = Decimal("612.44")
CAR_LOAN_MONTHLY_RATE = Decimal("0.0060")
CAR_LOAN_PAID_BEFORE_WINDOW = 22
CAR_LOAN_OPENING_BALANCE = Decimal("14703.92")

# BRL/USD commercial rates. Real market territory rather than a fictional
# band: the rate is public data and nothing about it identifies a household.
# Jitter around a centre instead of a cumulative walk — twelve unbounded steps
# wander half a real away from the anchor — which keeps every month inside
# 4.96-5.08. DEMO_PRESENT is pinned to FX_COMMERCIAL_PIN (effective 5.2027 once
# the default spread and IOF are applied); the months on either side of it,
# past and projected alike, keep the jitter.
FX_COMMERCIAL_CENTRE = Decimal("5.0200")
FX_COMMERCIAL_JITTER = 6  # cents either side of the centre
FX_COMMERCIAL_PIN = Decimal("5.0700")

# The financed handset: two years of installments, halfway through at the end
# of the window, so /rollover has a live series to project forward.
PHONE_INSTALLMENT = Decimal("54.13")
PHONE_INSTALLMENT_TOTAL = 24

# The lease. Its end date falls inside the window on purpose: /warnings then
# has a real contract expiry to raise and the rollover preview has a series
# that stops. The renewal that picks up in November keeps the fixed baseline
# continuous, instead of leaving the last two months of the year with no rent
# at all (which reads as missing data, not as a lease that ended).
RENT = Decimal("1850.00")
RENT_CONTRACT_END = date(2026, 10, 31)
RENT_RENEWED = Decimal("1920.00")
RENT_RENEWED_END = date(2027, 10, 31)

CATEGORIES: tuple[tuple[str, CategoryType, str], ...] = (
    ("Variable", CategoryType.VARIABLE, "#95a5a6"),
    ("Groceries", CategoryType.VARIABLE, "#2ecc71"),
    ("Dining", CategoryType.VARIABLE, "#f1c40f"),
    ("Transport", CategoryType.VARIABLE, "#e67e22"),
    ("Vacation", CategoryType.VARIABLE, "#3498db"),
    ("Shopping", CategoryType.VARIABLE, "#9b59b6"),
    ("Home", CategoryType.VARIABLE, "#1abc9c"),
    ("Services", CategoryType.VARIABLE, "#34495e"),
    ("Entertainment", CategoryType.VARIABLE, "#bdc3c7"),
    ("Health Insurance - Copay", CategoryType.VARIABLE, "#d35400"),
    ("Rent", CategoryType.FIXED, "#8e44ad"),
    ("Internet", CategoryType.FIXED, "#2980b9"),
    ("Phone", CategoryType.FIXED, "#27ae60"),
    ("Streaming", CategoryType.FIXED, "#d35400"),
    ("Gym", CategoryType.FIXED, "#c0392b"),
    ("Car", CategoryType.FIXED, "#7f8c8d"),
    ("Health Insurance", CategoryType.FIXED, "#bdc3c7"),
    ("Taxes", CategoryType.FIXED, "#c0392b"),
)

# (merchant, default category)
MERCHANTS: tuple[tuple[str, str], ...] = (
    ("Northgate Market", "Groceries"),
    ("Riverbend Grocers", "Groceries"),
    ("Cedar Deli", "Dining"),
    ("Ponte Coffee", "Dining"),
    ("Two Rivers Pizza", "Dining"),
    ("Metro Transit", "Transport"),
    ("Halcyon Fuel", "Transport"),
    ("Orbit Marketplace", "Shopping"),
    ("Fernwood Hardware", "Home"),
    ("Parcel Express", "Services"),
    ("Lumen Cinema", "Entertainment"),
    ("Skyline Air", "Vacation"),
    ("Riverside Apartments", "Rent"),
    ("Fiberline", "Internet"),
    ("Tallgrass Mobile", "Phone"),
    ("Reelhouse", "Streaming"),
    ("Tonewave", "Streaming"),
    ("Ironworks Gym", "Gym"),
    ("Pinnacle Auto Insurance", "Car"),
    ("Rivera Auto Finance", "Car"),
    ("Meridian Health", "Health Insurance"),
    ("Clinica Sao Jorge", "Health Insurance - Copay"),
    ("Federal Withholding", "Taxes"),
    ("State Withholding", "Taxes"),
    ("Bookkeeper BR", "Taxes"),
    ("Receita Federal", "Taxes"),
    ("Handset Financing", "Phone"),
)

# (keyword, merchant, category) — a dozen rules so /rules and the importer
# preview have something to show.
CATEGORIZATION_RULES: tuple[tuple[str, str, str], ...] = (
    ("northgate", "Northgate Market", "Groceries"),
    ("riverbend", "Riverbend Grocers", "Groceries"),
    ("cedar deli", "Cedar Deli", "Dining"),
    ("ponte coffee", "Ponte Coffee", "Dining"),
    ("two rivers", "Two Rivers Pizza", "Dining"),
    ("metro transit", "Metro Transit", "Transport"),
    ("halcyon", "Halcyon Fuel", "Transport"),
    ("orbit", "Orbit Marketplace", "Shopping"),
    ("fernwood", "Fernwood Hardware", "Home"),
    ("reelhouse", "Reelhouse", "Streaming"),
    ("tonewave", "Tonewave", "Streaming"),
    ("ironworks", "Ironworks Gym", "Gym"),
    ("fiberline", "Fiberline", "Internet"),
    ("riverside apartments", "Riverside Apartments", "Rent"),
)

# Same shape as a real household's classifier config: the card-payment
# keywords a checking statement prints, the two salary sources, the rent
# deposit, taxes, interest, internal transfers, and the boilerplate the
# line-oriented parsers must ignore.
STATEMENT_MATCH_RULES: tuple[tuple[str, str, str], ...] = (
    ("CC_PAYMENT", "AURORA BANK EPAYMENT", CARD_MAIN),
    ("CC_PAYMENT", "BEACON CARD AUTOPAY", CARD_SECOND),
    ("CC_PAYMENT", "HARBOR STORECRD PMT", CARD_STORE),
    ("CC_PAYMENT", "PAGAMENTO DE FATURA", CARD_MAIN),
    ("SALARY", "NORTHSTAR UNIV EDI", "Sam"),
    ("SALARY", "RIVERA,SAM", "Sam"),
    ("SALARY", "ACME CONSULTORIA", "Alex"),
    ("RENT_DEPOSIT", "SAM RIVERA PIX", "Alex"),
    ("TAX_PAYMENT", "STATE ITS TAX", ""),
    ("TAX_PAYMENT", "ST TX PYMT", ""),
    ("TAX_PAYMENT", "IRS USATAXPYMT", ""),
    ("TAX_PAYMENT", "FED TAX", ""),
    ("TAX_PAYMENT", "SIMPLES NACIONAL", ""),
    ("TAX_PAYMENT", "DARF UNIFICADO", ""),
    ("INTEREST", "INTEREST DEPOSIT", ""),
    ("INTEREST", "INTEREST PAID", ""),
    ("INTEREST", "RENDIMENTOS", ""),
    ("INTERNAL_TRANSFER", "SUMMIT SAVINGS TRANSFER", ""),
    ("INTERNAL_TRANSFER", "MERIDIAN BANK", ""),
    ("INTERNAL_TRANSFER", "ATM CASH DEPOSIT", ""),
    ("INTERNAL_TRANSFER", "ZELLE FROM", ""),
    ("INTERNAL_TRANSFER", "ZELLE TO", ""),
    ("INTERNAL_TRANSFER", "ALEX RIVERA", ""),
    ("INTERNAL_TRANSFER", "APLICAÇÃO RDB", ""),
    ("INTERNAL_TRANSFER", "RESGATE RDB", ""),
    ("EXTRA_INCOME", "TRANSFERÊNCIA RECEBIDA", ""),
    ("EXTRA_INCOME", "TRANSFERENCIA RECEBIDA", ""),
    ("EXTRA_INCOME", "TRANSFERÊNCIA RECEBIDA PELO PIX", ""),
    ("EXTRA_INCOME", "TRANSFERENCIA RECEBIDA PELO PIX", ""),
    ("NOISE", "ALEX RIVERA", ""),
    ("NOISE", "STATEMENT PERIOD", ""),
    ("HOLDER_NAME", "ALEX RIVERA", ""),
    ("HOLDER_NAME", "SAM RIVERA", ""),
)


class DemoSeeder:
    """Holds the resolved rows so the passes can reference each other by name.

    Written as a class rather than a chain of functions only because every pass
    needs the same handful of lookup dicts.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.rng = random.Random(SEED)
        self.counts: dict[str, int] = {}
        self.categories: dict[str, Category] = {}
        self.merchants: dict[str, Merchant] = {}
        self.pms: dict[str, PaymentMethod] = {}
        self.users: dict[str, User] = {}
        self.members: dict[HouseholdRole, HouseholdMember] = {}
        self.months: list[tuple[int, int]] = _window(WINDOW_END, WINDOW_MONTHS)
        # (date, merchant_id, amount, pm_id, user_id) — mirrors
        # uq_transaction_signature so a random collision cannot abort the run.
        self._signatures: set[tuple] = set()

    # ------------------------------------------------------------------ utils
    def _count(self, table: str, n: int = 1) -> None:
        self.counts[table] = self.counts.get(table, 0) + n

    def _money(self, low: str, high: str) -> Decimal:
        cents = self.rng.randint(int(Decimal(low) * 100), int(Decimal(high) * 100))
        return (Decimal(cents) / 100).quantize(Decimal("0.01"))

    def _day(self, year: int, month: int, day: int) -> date:
        return date(year, month, min(day, monthrange(year, month)[1]))

    def _txn(
        self,
        *,
        when: date,
        merchant: str,
        category: str,
        pm: str,
        amount: Decimal,
        owner: str = PRIMARY_NAME,
        recurrence: RecurrenceKind | None = None,
        contract_end: date | None = None,
        installment_current: int = 1,
        installment_total: int = 1,
        installment_value: Decimal | None = None,
        description: str | None = None,
    ) -> None:
        payment_method = self.pms[pm]
        signature = (
            when,
            self.merchants[merchant].id,
            amount,
            payment_method.id,
            self.users[owner].id,
        )
        if signature in self._signatures:
            return
        self._signatures.add(signature)
        self.session.add(
            Transaction(
                transaction_date=when,
                merchant_id=self.merchants[merchant].id,
                category_id=self.categories[category].id,
                payment_method_id=payment_method.id,
                amount=amount,
                currency=payment_method.currency,
                description=description,
                recurrence_kind=recurrence,
                contract_end_date=contract_end,
                installment_current=installment_current,
                installment_total=installment_total,
                installment_value=installment_value,
                created_by_user_id=self.users[owner].id,
            )
        )
        self._count("transactions")

    # ------------------------------------------------------------ seed passes
    def run(self) -> dict[str, int]:
        self.clear_reference_data()
        self.seed_taxonomy()
        self.seed_people_and_accounts()
        self.seed_household()
        self.seed_match_rules()
        self.seed_exchange_rates()
        self.seed_transactions()
        self.seed_income()
        self.seed_balances()
        self.seed_receivables()
        self.seed_assets_and_loan()
        self.session.commit()
        return self.counts

    def clear_reference_data(self) -> None:
        """Drop whatever the migration chain seeded for its own household.

        The private repo's migrations carry real reference rows (people, a
        payment method, an asset, the classifier keywords). The public
        baseline `0001_initial` carries none, but the seeder has to be correct
        against both, and a demo database must never inherit someone's real
        data. Safe because `main()` has already proved the ledger is empty.
        Ordered child-first so the foreign keys allow it.
        """
        for model in (
            CategorizationRule,
            WithholdingMerchant,
            SalaryLevel,
            HouseholdMember,
            StatementMatchRule,
            Asset,
            Person,
            CreditCardBalance,
            SavingsSnapshot,
            CarLoanPayment,
            ExchangeRate,
            PaymentMethod,
            Merchant,
            Category,
        ):
            self.session.query(model).delete()
        self.session.flush()

    def seed_taxonomy(self) -> None:
        for name, ctype, color in CATEGORIES:
            self.categories[name] = Category(name=name, type=ctype, color=color)
        self.session.add_all(self.categories.values())
        self.session.flush()
        self._count("categories", len(self.categories))

        for name, category in MERCHANTS:
            self.merchants[name] = Merchant(
                name=name, default_category_id=self.categories[category].id
            )
        self.session.add_all(self.merchants.values())
        self.session.flush()
        self._count("merchants", len(self.merchants))

        for priority, (keyword, merchant, category) in enumerate(CATEGORIZATION_RULES, start=1):
            self.session.add(
                CategorizationRule(
                    keyword=keyword,
                    merchant_id=self.merchants[merchant].id,
                    category_id=self.categories[category].id,
                    priority=priority * 10,
                )
            )
        self.session.flush()
        self._count("categorization_rules", len(CATEGORIZATION_RULES))

    def seed_people_and_accounts(self) -> None:
        password_hash = hash_password(DEMO_PASSWORD)
        for name, email in ((PRIMARY_NAME, PRIMARY_EMAIL), (PARTNER_NAME, PARTNER_EMAIL)):
            self.users[name] = User(email=email, name=name, password_hash=password_hash)
        self.session.add_all(self.users.values())
        self.session.flush()
        self._count("users", len(self.users))

        checking = PaymentMethod(
            name=CHECKING_USD, type=PaymentMethodType.CHECKING, currency=Currency.USD
        )
        savings = PaymentMethod(
            name=SAVINGS_USD, type=PaymentMethodType.SAVINGS, currency=Currency.USD
        )
        checking_brl = PaymentMethod(
            name=CHECKING_BRL, type=PaymentMethodType.CHECKING, currency=Currency.BRL
        )
        self.session.add_all([checking, savings, checking_brl])
        self.session.flush()

        cards = [
            PaymentMethod(
                name=CARD_MAIN,
                type=PaymentMethodType.CREDIT_CARD,
                currency=Currency.USD,
                paid_from_payment_method_id=checking.id,
                statement_close_day=18,
                due_day=12,
            ),
            PaymentMethod(
                name=CARD_SECOND,
                type=PaymentMethodType.CREDIT_CARD,
                currency=Currency.USD,
                paid_from_payment_method_id=checking.id,
                due_day=5,
            ),
            # Deliberately without a paid_from link: /warnings surfaces cards
            # that cannot be projected, and the demo should show that panel
            # doing its job rather than sitting empty.
            PaymentMethod(
                name=CARD_STORE,
                type=PaymentMethodType.CREDIT_CARD,
                currency=Currency.USD,
                due_day=22,
            ),
        ]
        self.session.add_all(cards)
        self.session.flush()

        for pm in [checking, savings, checking_brl, *cards]:
            self.pms[pm.name] = pm
        self._count("payment_methods", len(self.pms))

        for name, relation in (
            ("Jordan Blake", "friend"),
            ("Priya Nair", "colleague"),
            ("Marco Ferreira", "family"),
        ):
            self.session.add(Person(name=name, relation=relation))
        self.session.flush()
        self._count("people", 3)

    def seed_household(self) -> None:
        primary = HouseholdMember(
            user_id=self.users[PRIMARY_NAME].id,
            role=HouseholdRole.PRIMARY,
            match_key="Alex",
            salary_income_source=IncomeSource.PRIMARY_SALARY,
            has_withholdings=False,
            salary_checking_pm_id=self.pms[CHECKING_BRL].id,
            salary_day_of_month=99,
        )
        partner = HouseholdMember(
            user_id=self.users[PARTNER_NAME].id,
            role=HouseholdRole.PARTNER,
            match_key="Sam",
            salary_income_source=IncomeSource.PARTNER_SALARY,
            has_withholdings=True,
            salary_checking_pm_id=self.pms[CHECKING_USD].id,
            salary_day_of_month=99,
        )
        self.session.add_all([primary, partner])
        self.session.flush()
        self.members = {HouseholdRole.PRIMARY: primary, HouseholdRole.PARTNER: partner}
        self._count("household_members", 2)

        levels = [
            SalaryLevel(
                member_id=partner.id,
                effective_year=1900,
                effective_month=1,
                gross=PARTNER_GROSS,
                currency=Currency.USD,
            ),
            SalaryLevel(
                member_id=partner.id,
                effective_year=RAISE_FROM[0],
                effective_month=RAISE_FROM[1],
                gross=PARTNER_GROSS_AFTER_RAISE,
                currency=Currency.USD,
            ),
            SalaryLevel(
                member_id=primary.id,
                effective_year=1900,
                effective_month=1,
                gross=PRIMARY_GROSS_BRL,
                currency=Currency.BRL,
            ),
            SalaryLevel(
                member_id=primary.id,
                effective_year=RAISE_FROM[0],
                effective_month=RAISE_FROM[1],
                gross=PRIMARY_GROSS_BRL_AFTER_RAISE,
                currency=Currency.BRL,
            ),
        ]
        self.session.add_all(levels)
        self._count("salary_levels", len(levels))

        for merchant in ("Federal Withholding", "State Withholding"):
            self.session.add(
                WithholdingMerchant(
                    member_id=partner.id, merchant_id=self.merchants[merchant].id
                )
            )
        self.session.flush()
        self._count("withholding_merchants", 2)

    def seed_match_rules(self) -> None:
        order: dict[str, int] = {}
        for classification, keyword, hint in STATEMENT_MATCH_RULES:
            order[classification] = order.get(classification, 0) + 10
            self.session.add(
                StatementMatchRule(
                    classification=classification,
                    keyword=keyword,
                    match_hint=hint,
                    sort_order=order[classification],
                    active=True,
                )
            )
        self.session.flush()
        self._count("statement_match_rules", len(STATEMENT_MATCH_RULES))

    def seed_exchange_rates(self) -> None:
        # The one series here that is NOT invented: BRL/USD is public market
        # data, so the demo carries realistic rates instead of fictional ones.
        for year, month in self.months:
            if (year, month) == DEMO_PRESENT:
                commercial = FX_COMMERCIAL_PIN
            else:
                jitter = self.rng.randint(-FX_COMMERCIAL_JITTER, FX_COMMERCIAL_JITTER)
                commercial = FX_COMMERCIAL_CENTRE + Decimal(jitter) / Decimal(100)
            commercial = commercial.quantize(Decimal("0.0001"))
            self.session.add(
                ExchangeRate(
                    rate_date=date(year, month, 1),
                    commercial=commercial,
                    spread=DEFAULT_SPREAD,
                    iof=DEFAULT_IOF,
                    effective=compute_effective(commercial, DEFAULT_SPREAD, DEFAULT_IOF),
                )
            )
        self.session.flush()
        self._count("exchange_rates", len(self.months))

    def seed_transactions(self) -> None:
        for index, (year, month) in enumerate(self.months, start=1):
            self._seed_variable_month(year, month)
            self._seed_fixed_month(year, month)
            self._seed_withholdings(year, month)
            self._seed_brl_month(year, month)

            self._txn(
                when=self._day(year, month, 6),
                merchant="Handset Financing",
                category="Phone",
                pm=CARD_MAIN,
                amount=PHONE_INSTALLMENT,
                recurrence=RecurrenceKind.INSTALLMENT,
                installment_current=index,
                installment_total=PHONE_INSTALLMENT_TOTAL,
                installment_value=PHONE_INSTALLMENT,
                description=f"Handset financing {index}/{PHONE_INSTALLMENT_TOTAL}",
            )

    def _seed_variable_month(self, year: int, month: int) -> None:
        # Groceries land weekly; dining and transport are noisier. December and
        # July carry the shopping/travel bumps that make the annual chart read
        # like a real year.
        for week in range(4):
            self._txn(
                when=self._day(year, month, 3 + week * 7),
                merchant=self.rng.choice(["Northgate Market", "Riverbend Grocers"]),
                category="Groceries",
                pm=CARD_MAIN,
                amount=self._money("62.00", "168.00"),
                owner=self.rng.choice([PRIMARY_NAME, PARTNER_NAME]),
            )

        for _ in range(self.rng.randint(3, 6)):
            self._txn(
                when=self._day(year, month, self.rng.randint(1, 28)),
                merchant=self.rng.choice(["Cedar Deli", "Ponte Coffee", "Two Rivers Pizza"]),
                category="Dining",
                pm=CARD_SECOND,
                amount=self._money("11.00", "74.00"),
                owner=self.rng.choice([PRIMARY_NAME, PARTNER_NAME]),
            )

        for _ in range(self.rng.randint(2, 4)):
            self._txn(
                when=self._day(year, month, self.rng.randint(1, 28)),
                merchant=self.rng.choice(["Metro Transit", "Halcyon Fuel"]),
                category="Transport",
                pm=CARD_MAIN,
                amount=self._money("8.00", "58.00"),
            )

        high_season = month in (11, 12, 7)
        for _ in range(self.rng.randint(2, 4) if high_season else self.rng.randint(1, 2)):
            self._txn(
                when=self._day(year, month, self.rng.randint(1, 28)),
                merchant="Orbit Marketplace",
                category="Shopping",
                pm=CARD_STORE,
                amount=self._money("22.00", "260.00" if high_season else "120.00"),
                owner=self.rng.choice([PRIMARY_NAME, PARTNER_NAME]),
            )

        if self.rng.random() < 0.5:
            self._txn(
                when=self._day(year, month, self.rng.randint(5, 25)),
                merchant="Fernwood Hardware",
                category="Home",
                pm=CARD_MAIN,
                amount=self._money("18.00", "140.00"),
            )
        if self.rng.random() < 0.4:
            self._txn(
                when=self._day(year, month, self.rng.randint(5, 25)),
                merchant="Lumen Cinema",
                category="Entertainment",
                pm=CARD_SECOND,
                amount=self._money("14.00", "48.00"),
            )
        if self.rng.random() < 0.3:
            self._txn(
                when=self._day(year, month, self.rng.randint(5, 25)),
                merchant="Parcel Express",
                category="Services",
                pm=CARD_MAIN,
                amount=self._money("9.00", "36.00"),
            )
        if month in (12, 7):
            self._txn(
                when=self._day(year, month, 14),
                merchant="Skyline Air",
                category="Vacation",
                pm=CARD_SECOND,
                amount=self._money("380.00", "920.00"),
            )

    def _seed_fixed_month(self, year: int, month: int) -> None:
        rent_due = self._day(year, month, 1)
        renewed = rent_due > RENT_CONTRACT_END
        self._txn(
            when=rent_due,
            merchant="Riverside Apartments",
            category="Rent",
            pm=CHECKING_USD,
            amount=RENT_RENEWED if renewed else RENT,
            recurrence=RecurrenceKind.CONTRACT,
            contract_end=RENT_RENEWED_END if renewed else RENT_CONTRACT_END,
        )
        self._txn(
            when=self._day(year, month, 8),
            merchant="Fiberline",
            category="Internet",
            pm=CHECKING_USD,
            amount=Decimal("82.00"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        self._txn(
            when=self._day(year, month, 11),
            merchant="Tallgrass Mobile",
            category="Phone",
            pm=CARD_SECOND,
            amount=Decimal("36.00"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        self._txn(
            when=self._day(year, month, 17),
            merchant="Reelhouse",
            category="Streaming",
            pm=CARD_SECOND,
            amount=Decimal("15.49"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        self._txn(
            when=self._day(year, month, 21),
            merchant="Tonewave",
            category="Streaming",
            pm=CARD_SECOND,
            amount=Decimal("11.99"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        self._txn(
            when=self._day(year, month, 5),
            merchant="Ironworks Gym",
            category="Gym",
            pm=CARD_MAIN,
            amount=Decimal("39.99"),
            recurrence=RecurrenceKind.INDEFINITE,
            owner=PARTNER_NAME,
        )
        self._txn(
            when=self._day(year, month, 9),
            merchant="Pinnacle Auto Insurance",
            category="Car",
            pm=CHECKING_USD,
            amount=Decimal("143.75"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        self._txn(
            when=self._day(year, month, 4),
            merchant="Rivera Auto Finance",
            category="Car",
            pm=CHECKING_USD,
            amount=CAR_LOAN_PAYMENT,
            recurrence=RecurrenceKind.INSTALLMENT,
            installment_current=(
                CAR_LOAN_PAID_BEFORE_WINDOW + self.months.index((year, month)) + 1
            ),
            installment_total=CAR_LOAN_TERM,
            installment_value=CAR_LOAN_PAYMENT,
        )
        self._txn(
            when=self._day(year, month, 13),
            merchant="Meridian Health",
            category="Health Insurance",
            pm=CHECKING_USD,
            amount=Decimal("268.00"),
            recurrence=RecurrenceKind.INDEFINITE,
            owner=PARTNER_NAME,
        )

    def _seed_withholdings(self, year: int, month: int) -> None:
        # The partner's paycheck arrives net; these FIXED rows are what the
        # salary import reconciles against the real deposit.
        federal = self._money("480.00", "535.00")
        state = self._money("140.00", "168.00")
        for merchant, amount in (("Federal Withholding", federal), ("State Withholding", state)):
            self._txn(
                when=self._day(year, month, 1),
                merchant=merchant,
                category="Taxes",
                pm=CHECKING_USD,
                amount=amount,
                owner=PARTNER_NAME,
            )

    def _seed_brl_month(self, year: int, month: int) -> None:
        self._txn(
            when=self._day(year, month, 14),
            merchant="Bookkeeper BR",
            category="Taxes",
            pm=CHECKING_BRL,
            amount=Decimal("260.00"),
            recurrence=RecurrenceKind.INDEFINITE,
        )
        # Variable BR tax payment: arrives via import, never rolls.
        self._txn(
            when=self._day(year, month, 20),
            merchant="Receita Federal",
            category="Taxes",
            pm=CHECKING_BRL,
            amount=self._money("980.00", "1180.00"),
        )
        if self.rng.random() < 0.6:
            self._txn(
                when=self._day(year, month, self.rng.randint(5, 26)),
                merchant="Clinica Sao Jorge",
                category="Health Insurance - Copay",
                pm=CHECKING_BRL,
                amount=self._money("90.00", "320.00"),
            )

    def seed_income(self) -> None:
        for year, month in self.months:
            partner_gross = (
                PARTNER_GROSS_AFTER_RAISE if (year, month) >= RAISE_FROM else PARTNER_GROSS
            )
            primary_gross = (
                PRIMARY_GROSS_BRL_AFTER_RAISE if (year, month) >= RAISE_FROM else PRIMARY_GROSS_BRL
            )
            self.session.add_all(
                [
                    IncomeEntry(
                        year=year,
                        month=month,
                        source=IncomeSource.PARTNER_SALARY,
                        amount=partner_gross,
                        currency=Currency.USD,
                    ),
                    IncomeEntry(
                        year=year,
                        month=month,
                        source=IncomeSource.PRIMARY_SALARY,
                        amount=primary_gross,
                        currency=Currency.BRL,
                    ),
                ]
            )
        extras = [
            IncomeEntry(
                year=2026,
                month=6,
                source=IncomeSource.RENTS_BRAZIL,
                amount=Decimal("3150.00"),
                currency=Currency.BRL,
            ),
            IncomeEntry(
                year=2026,
                month=4,
                source=IncomeSource.EXTRA_USD,
                amount=Decimal("465.00"),
                currency=Currency.USD,
            ),
        ]
        self.session.add_all(extras)
        self.session.flush()
        self._count("income_entries", len(self.months) * 2 + len(extras))

    def seed_balances(self) -> None:
        savings_balance = Decimal("26750.00")
        checking_balance = Decimal("6480.00")
        # The BRL account must snapshot too, or /savings shows only the USD
        # accounts and the overdraft forecast reads it as permanently zero.
        checking_brl_balance = Decimal("4950.00")
        for year, month in self.months:
            last_day = self._day(year, month, 31)
            recorded = datetime(year, month, last_day.day, 23, 0, tzinfo=timezone.utc)
            savings_balance += self._money("180.00", "940.00")
            checking_balance += self._money("0.00", "700.00") - Decimal("350.00")
            checking_brl_balance += self._money("0.00", "900.00") - Decimal("400.00")
            self.session.add_all(
                [
                    SavingsSnapshot(
                        account_name=SAVINGS_USD,
                        currency=Currency.USD,
                        balance=savings_balance,
                        recorded_at=recorded,
                    ),
                    SavingsSnapshot(
                        account_name=CHECKING_USD,
                        currency=Currency.USD,
                        balance=checking_balance,
                        recorded_at=recorded,
                    ),
                    SavingsSnapshot(
                        account_name=CHECKING_BRL,
                        currency=Currency.BRL,
                        balance=checking_brl_balance,
                        recorded_at=recorded,
                    ),
                ]
            )
        self.session.flush()
        self._count("savings_snapshots", len(self.months) * 3)

        # Sparse card snapshots: the live balance is derived by adding the
        # transactions posted after the snapshot date, so a handful is enough.
        # They stop at DEMO_PRESENT rather than at the end of the window: the
        # latest row is what /debts and /warnings read as the current balance,
        # and the month-over-month badge compares it against last month's row.
        present = self.months.index(DEMO_PRESENT)
        card_months = self.months[max(0, present - 2):present + 1]
        card_rows = 0
        for card, base in ((CARD_MAIN, "1740.00"), (CARD_SECOND, "935.00"), (CARD_STORE, "555.00")):
            balance = Decimal(base)
            for year, month in card_months:
                balance += self._money("0.00", "160.00") - Decimal("60.00")
                self.session.add(
                    CreditCardBalance(
                        payment_method_id=self.pms[card].id,
                        balance=balance,
                        statement=balance,
                        recorded_at=datetime(year, month, 20, 12, 0, tzinfo=timezone.utc),
                    )
                )
                card_rows += 1
        self.session.flush()
        self._count("credit_card_balances", card_rows)

    def seed_receivables(self) -> None:
        people = {p.name: p for p in self.session.scalars(select(Person)).all()}
        # A split dinner: two rows of one charge share a group_id.
        group = str(UUID(int=self.rng.getrandbits(128), version=4))
        rows = [
            Receivable(
                person_id=people["Jordan Blake"].id,
                group_id=group,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("58.75"),
                currency=Currency.USD,
                description="Dinner at Two Rivers",
                store="Two Rivers Pizza",
                payment_method_id=self.pms[CARD_SECOND].id,
                charge_date=date(2026, 7, 11),
            ),
            Receivable(
                person_id=people["Priya Nair"].id,
                group_id=group,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("58.75"),
                currency=Currency.USD,
                description="Dinner at Two Rivers",
                store="Two Rivers Pizza",
                payment_method_id=self.pms[CARD_SECOND].id,
                charge_date=date(2026, 7, 11),
            ),
            Receivable(
                person_id=people["Jordan Blake"].id,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("164.00"),
                currency=Currency.USD,
                description="Concert tickets",
                payment_method_id=self.pms[CARD_MAIN].id,
                charge_date=date(2026, 5, 2),
                settled=True,
                settled_at=datetime(2026, 5, 20, 18, 30, tzinfo=timezone.utc),
            ),
            Receivable(
                person_id=people["Priya Nair"].id,
                direction=ReceivableDirection.I_OWE,
                amount=Decimal("41.80"),
                currency=Currency.USD,
                description="Shared taxi",
                charge_date=date(2026, 6, 18),
            ),
            Receivable(
                person_id=people["Marco Ferreira"].id,
                direction=ReceivableDirection.OWED_TO_ME,
                amount=Decimal("245.00"),
                currency=Currency.BRL,
                description="Farmácia",
                payment_method_id=self.pms[CHECKING_BRL].id,
                charge_date=date(2026, 6, 29),
            ),
            Receivable(
                person_id=people["Marco Ferreira"].id,
                direction=ReceivableDirection.I_OWE,
                amount=Decimal("135.00"),
                currency=Currency.BRL,
                description="Presente de aniversário",
                charge_date=date(2026, 4, 8),
                settled=True,
                settled_at=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            ),
        ]
        self.session.add_all(rows)
        self.session.flush()
        self._count("receivables", len(rows))

    def seed_assets_and_loan(self) -> None:
        assets = [
            Asset(
                name="2019 Sedan",
                kind=AssetKind.VEHICLE,
                location="Home",
                acquired_date=date(2024, 3, 15),
                current_value=Decimal("15900.00"),
                currency=Currency.USD,
                last_valued_date=date(2026, 1, 10),
                last_service_date=date(2026, 2, 4),
                next_service_due_date=date(2026, 11, 4),
                notes="Valued once a year against the trade-in guide.",
            ),
            Asset(
                name="Workstation laptop",
                kind=AssetKind.ELECTRONICS,
                acquired_date=date(2025, 8, 1),
                current_value=Decimal("1450.00"),
                currency=Currency.USD,
                last_valued_date=date(2026, 1, 10),
            ),
        ]
        self.session.add_all(assets)
        self._count("assets", len(assets))

        balance = CAR_LOAN_OPENING_BALANCE
        loan_rows = 0
        for year, month in self.months:
            interest = (balance * CAR_LOAN_MONTHLY_RATE).quantize(Decimal("0.01"))
            payment = CAR_LOAN_PAYMENT
            principal = payment - interest
            balance = (balance - principal).quantize(Decimal("0.01"))
            self.session.add(
                CarLoanPayment(
                    posting_date=self._day(year, month, 4),
                    payment_amount=payment,
                    principal_paid=principal,
                    interest_paid=interest,
                    new_balance=balance,
                )
            )
            loan_rows += 1
        self.session.flush()
        self._count("car_loan_payments", loan_rows)


def _window(end: tuple[int, int], length: int) -> list[tuple[int, int]]:
    """The `length` months ending at `end`, oldest first."""
    year, month = end
    months: list[tuple[int, int]] = []
    for _ in range(length):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))


def main() -> int:
    with SessionLocal() as session:
        for model in (User, Transaction, IncomeEntry, Receivable):
            rows = session.scalar(select(func.count()).select_from(model)) or 0
            if rows:
                print(
                    f"refusing to seed: {model.__tablename__} already holds {rows} row(s). "
                    "seed_demo.py only ever runs against an empty database.",
                    file=sys.stderr,
                )
                return 1

        counts = DemoSeeder(session).run()

    width = max(len(name) for name in counts)
    print("demo data seeded:")
    for name in sorted(counts):
        print(f"  {name.ljust(width)}  {counts[name]:>5}")
    print(f"\nlogin: {PRIMARY_EMAIL} or {PARTNER_EMAIL} / {DEMO_PASSWORD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
