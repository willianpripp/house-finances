from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

# Clean-start anchor. Every parser drops rows dated before this year and counts
# them in `ParseResult.skipped`, so importing a statement that spans the switch
# to this app does not back-fill months that were already closed elsewhere.
# Change it to the first year this app should own — a statement entirely before
# it imports as zero transactions, which is the usual cause of "my import found
# nothing".
EARLIEST_IMPORT_YEAR = 2026


class ParsedTransaction(BaseModel):
    """One row from a statement. Pure data — no DB ids yet.

    `amount` is signed: positive for charges, negative for refunds.
    `is_payment` rows pay down the card balance and are NOT stored as transactions;
    the importer handles them separately by updating credit_card_balances.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    transaction_date: date
    description: str
    amount: Decimal
    is_payment: bool = False
    raw: dict[str, Any] | None = None


class ParseResult(BaseModel):
    """Outcome of parsing a single file."""

    transactions: list[ParsedTransaction] = []
    payments: list[ParsedTransaction] = []
    skipped: int = 0
    parser: str = "unknown"
    # Statement-header dates. When the parser can recover them from the file
    # header, the importer updates `payment_methods.statement_close_day` /
    # `due_day` on commit. Both optional: parsers that don't extract them
    # leave these as None and the per-card record stays untouched.
    statement_close_date: date | None = None
    due_date: date | None = None
