from enum import Enum


class CategoryType(str, Enum):
    VARIABLE = "variable"
    FIXED = "fixed"


class Currency(str, Enum):
    USD = "USD"
    BRL = "BRL"


class PaymentMethodType(str, Enum):
    CREDIT_CARD = "credit_card"
    CHECKING = "checking"
    SAVINGS = "savings"
    INVESTMENT = "investment"
    CASH = "cash"
    OTHER = "other"


class IncomeSource(str, Enum):
    PRIMARY_SALARY = "primary_salary"
    PARTNER_SALARY = "partner_salary"
    RENTS_BRAZIL = "rents_brazil"
    EXTRA_USD = "extra_usd"
    EXTRA_BRL = "extra_brl"


class AssetKind(str, Enum):
    """Material-asset taxonomy. Manually tagged, used for grouping on /assets."""
    VEHICLE = "VEHICLE"
    PROPERTY = "PROPERTY"
    ELECTRONICS = "ELECTRONICS"
    JEWELRY = "JEWELRY"
    OTHER = "OTHER"


class RecurrenceKind(str, Enum):
    """Classifies how a FIXED transaction repeats.

    - INDEFINITE: rolls forward forever (Mint, Google Fiber, Claro, gym, etc.).
    - CONTRACT: rolls until `contract_end_date`, then stops and signals renewal.
    - INSTALLMENT: a real purchase split into N payments with interest
      (car loan, a financed phone via AFFIRM). Driven by
      `installment_current`/`installment_total`.
    - EXTRA_PRINCIPAL: manual extra payment with no interest that debits the
      car loan principal directly. Creating one inserts a matching
      `car_loan_payments` row (principal_paid = amount, interest_paid = 0).
    """
    INDEFINITE = "INDEFINITE"
    CONTRACT = "CONTRACT"
    INSTALLMENT = "INSTALLMENT"
    EXTRA_PRINCIPAL = "EXTRA_PRINCIPAL"


# `ImportSource` is assembled rather than declared in one block, because a
# label only means something in a tree that can emit it. The members below are
# either generic (MANUAL, PLAID, PLUGGY, CAR_LOAN) or belong to a parser that
# every tree ships. The institution labels of the parsers that are NOT public
# live next to nothing else, in `import_sources_extra.py`, which the export
# omits along with those parser modules (the public export ships three reference parsers:
# set of parsers a repo ships is the set of accounts its author holds, and an
# enum enumerating eleven institutions says the same thing louder).
#
# Same mechanism as the parser registry: a module that is absent registers
# nothing and nothing imports it by name. No file is rewritten on the way out.
_IMPORT_SOURCES: dict[str, str] = {
    "CITI": "citi",
    "AMAZON": "amazon",
    "NUBANK_CREDITO": "nubank_credito",
    "CHECKING_NUBANK": "checking_nubank",
    "CAR_LOAN": "car_loan",
    "MANUAL": "manual",
    "PLAID": "plaid",  # US auto-pull via Plaid (one import_log per sync run)
    "PLUGGY": "pluggy",  # BR auto-pull via Pluggy (one import_log per review commit)
}

try:  # pragma: no cover - the except branch is the public tree's only path
    from app.models.import_sources_extra import EXTRA_IMPORT_SOURCES
except ImportError:
    EXTRA_IMPORT_SOURCES: dict[str, str] = {}

ImportSource = Enum(  # type: ignore[misc]
    "ImportSource",
    {**_IMPORT_SOURCES, **EXTRA_IMPORT_SOURCES},
    type=str,
    module=__name__,
    qualname="ImportSource",
)
ImportSource.__doc__ = (
    "Which ingestion path produced an `import_logs` row. Member NAMES are the "
    "labels of the Postgres `import_source` type; every member must exist "
    "there (tests/test_import_source_enum.py pins both directions)."
)


class PlaidItemStatus(str, Enum):
    ACTIVE = "ACTIVE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # bank wants user to re-auth via Plaid Link
    REVOKED = "REVOKED"                # user disconnected


class ReceivableDirection(str, Enum):
    """Which way a receivable row points.

    - OWED_TO_ME: the original case — the charge is on one of our cards and
      the person pays us back.
    - I_OWE: someone else paid and we owe them our share. The charge never
      touched our accounts, so nothing is in the ledger until we actually pay.
    """
    OWED_TO_ME = "OWED_TO_ME"
    I_OWE = "I_OWE"


class HouseholdRole(str, Enum):
    """Which side of the household an earner is. The app is deliberately scoped
    to two people; N members was deliberately not pursued."""
    PRIMARY = "PRIMARY"
    PARTNER = "PARTNER"
