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


class ImportSource(str, Enum):
    """The ingestion paths that no statement parser owns.

    This is deliberately NOT the full set of values `import_logs.source` can
    hold. Per-parser identity lives on the parser: each module's `ParserSpec`
    carries its `source` as a plain string (`services/parsers/registry.py`),
    and `import_logs.source` is a TEXT column, so adding a parser is still
    just dropping a module in a directory — no enum to extend, no migration,
    and no type in the database enumerating every institution the tree can
    read. `services/import_sources.py` is the union of the two halves and the
    check every `import_logs` write goes through.

    Values equal names because they are written to the column verbatim, and
    they are the labels the retired Postgres `import_source` type used, so
    rows written before the enum→text migration read back unchanged.
    """

    MANUAL = "MANUAL"   # typed or pasted by hand, no parser claimed the file
    PLAID = "PLAID"     # US auto-pull via Plaid (one import_log per sync run)
    PLUGGY = "PLUGGY"   # BR auto-pull via Pluggy (one import_log per review commit)
    CAR_LOAN = "CAR_LOAN"  # loan-schedule import, not a bank statement


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
