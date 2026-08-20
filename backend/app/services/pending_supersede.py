"""Plaid pending -> posted supersede: one purchase, two provider rows.

Plaid delivers a card purchase twice. First as a PENDING (provisional)
transaction; then, once the merchant captures it, as a POSTED transaction with
a NEW `transaction_id`, frequently a different descriptor, and sometimes a
different amount (restaurant tips, fuel holds). The posted object carries
`pending_transaction_id` = the id of the pending transaction it replaces, and
that field is the only reliable link between the two: descriptor, date and
amount can all move, so no signature dedupe can recognise them as the same
purchase.

Two ways both versions reach a commit. Both have to be handled, or the ledger
counts the purchase twice:

1. The pending row was committed in an earlier review and the posted version
   arrives in a later one. The posted row must UPDATE the ledger row it
   supersedes.
2. Both versions arrive in the SAME review window. The review window spans
   everything since the clean-start anchor, and an issuer can keep the pending
   transaction listed for days after the posted one appears, so one pull can
   carry both. Here the pending row must be DROPPED and only the posted one
   inserted. A per-row lookup cannot see this: the app's sessions do not
   autoflush (`app/db.py`), so a row added earlier in the same commit loop is
   invisible to a query, and the provider lists the two in whatever order it
   likes. `superseded_pending_ids` is therefore a pre-pass over the whole
   batch, which makes the outcome order-independent.

WHY UPDATE IN PLACE, never delete-and-insert. A ledger row's `id` is
referenced from the moment it exists: `receivables.settled_transaction_id`
points at it, and a human may already have fixed its category or merchant in
/transactions (which the provider guard explicitly allows, because
classification is human judgement about a fact rather than the fact).
Recreating the row would break the receivable link and discard that human
work. So a supersede keeps the row and everything human on it (id, category,
merchant, recurrence, installments, owner, import_log) and overwrites only
what the provider owns: transaction id, description, date, amount, and the
pending flag.

Pluggy has no equivalent: its transaction payload carries a `status` of
PENDING or POSTED and no link between the two, which is why
`services/pluggy_import.py` drops PENDING rows instead of reconciling them.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction


def superseded_pending_ids(pending_transaction_ids: Iterable[str | None]) -> set[str]:
    """The pending transaction ids that rows in THIS batch declare they replace.

    Any batch row whose own `plaid_transaction_id` is in this set is the
    pending version of a purchase whose posted version is in the same batch:
    committing it would be the duplicate. Feed this only the rows the commit
    will actually process, so a posted row the user unticked does not suppress
    the pending row the user kept.
    """
    return {pid for pid in pending_transaction_ids if pid}


@dataclass(frozen=True)
class PendingSupersede:
    """A posted transaction matched to the ledger row it replaces."""

    prior: Transaction
    amount: Decimal  # ledger-convention amount of the posted version

    @property
    def prior_id(self) -> int:
        return self.prior.id

    @property
    def prior_amount(self) -> Decimal:
        return Decimal(self.prior.amount)

    @property
    def amount_changed(self) -> bool:
        """Pending and posted amounts legitimately differ (tip, fuel hold).
        Same update-in-place either way; the preview flags it so the user sees
        the ledger figure is about to move."""
        return self.prior_amount != self.amount

    def apply(
        self,
        *,
        plaid_transaction_id: str | None,
        description: str | None,
        transaction_date: date,
        pending: bool = False,
    ) -> None:
        self.prior.plaid_transaction_id = plaid_transaction_id
        self.prior.description = (description or "")[:500]
        self.prior.transaction_date = transaction_date
        self.prior.amount = self.amount
        self.prior.pending = pending


def plan_supersede(
    session: Session,
    *,
    pending_transaction_id: str | None,
    amount: Decimal,
) -> PendingSupersede | None:
    """The ledger row this posted transaction supersedes, or None.

    `amount` is the posted amount in LEDGER convention (positive = charge), so
    the caller flips the sign on the checking path before calling.
    """
    if not pending_transaction_id:
        return None
    prior = session.scalar(
        select(Transaction).where(
            Transaction.plaid_transaction_id == pending_transaction_id
        )
    )
    if prior is None:
        return None
    return PendingSupersede(prior=prior, amount=amount)
