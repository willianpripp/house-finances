"""Settling a receivable posts the money movement to the ledger.

Until now settling was bookkeeping only: the row flipped to paid and the cash
that actually moved never showed up anywhere. This module is the missing half.

WHY OWED_TO_ME POSTS A NEGATIVE TRANSACTION AND NOT INCOME
----------------------------------------------------------
The original charge is already in the ledger as an expense (we put the whole
restaurant bill on our card; that row is real spending on our card). When the
other person hands their share back, there are two ways to record it and only
one of them reads correctly in `services/reports.py`:

- **An income entry** (`income_entries.EXTRA_USD` / `EXTRA_BRL`) inflates BOTH
  halves of every report that month. Gross income goes up by the share, and
  spending stays at the full bill even though we only ever bore our part. The
  surplus lands right, so the mistake hides, but "we earned $X and spent $Y"
  becomes wrong in both figures at once, and the annual report's top-category
  list keeps attributing other people's dinners to us.
- **A negative transaction** nets the expense back down to what the household
  actually paid, and touches income not at all. Category totals, total
  spending, surplus and the annual roll-up all come out right, because
  `reports._spend_rows` (the one place a transaction becomes USD) simply SUMS
  amounts, and every spending surface derives from it.

Negative is not an invention for this feature: `transactions.amount` already
means "positive = charge, negative = refund" (see
`checking_importer._insert_transaction`, which flips a statement credit that
falls through to SPENDING into a negative row for exactly this reason). A
payback IS a refund of our own spending, so it takes the refund shape. Income
entries stay reserved for money that is genuinely new to the household.

I_OWE is the mirror image and needs no such argument: nothing was in the
ledger while the debt was open, so settling posts a plain positive expense,
dated the day we actually paid the person back.

WHERE THE ROW LANDS
-------------------
On a CHECKING (or CASH) account in the receivable's own currency, never on the
credit card the original charge sat on. Two reasons: the money really does
arrive in, or leave from, a bank account, and a negative row on a card would
walk down the derived card debt in `services/debts.post_balance_delta` even
though the statement still owes the full amount. Card balances are the
checking importer's business, and that rule is not up for renegotiation here.

BRL and USD never mix: the posted row inherits the receivable's currency and
can only land on an account of that currency. If no such account exists the
receivable still settles, with no ledger entry and a reason the UI shows.

MATCHING BEFORE CREATING
------------------------
A payback often arrives through a bank account we import, so by the time the
user presses "mark paid" the ledger may already hold the row. Creating a
second one would double the netting. So the settle flow first looks for a
transaction that plausibly IS the payback (right currency, right sign,
amount within tolerance, near the settle date, on a plausible account, not
already claimed by another receivable) and LINKS to it. Only when nothing
matches does it create a row. `settled_transaction_autocreated` records which
happened, because unsettling may delete a row we created but must never
delete a row the bank gave us.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Currency,
    HouseholdMember,
    PaymentMethod,
    PaymentMethodType,
    Receivable,
    ReceivableDirection,
    Transaction,
)
from app.services.categorizer import Categorizer

# What happened to the ledger, reported back to the caller and shown in the UI.
CREATED = "created"
LINKED = "linked"
DELETED = "deleted"
UNLINKED = "unlinked"
NOTHING = "none"

# A payback rarely lands on the exact day the user marks the row paid, and
# rarely to the cent when someone rounds a split. Same ±$1 the checking
# importer uses for its FIXED / CC-payment matching, so there is one tolerance
# in this codebase and not three.
MATCH_WINDOW_DAYS = 5
MATCH_AMOUNT_TOLERANCE = Decimal("1.00")

# How far from `charge_date` to look for the original charge whose category the
# refund should net against. Tighter than the payback window: the charge date
# is typed from a receipt, not guessed.
ORIGINAL_CHARGE_WINDOW_DAYS = 3

# Money reaches or leaves us through a bank account. Deliberately excludes
# CREDIT_CARD (see the module docstring) and the investment/savings buckets,
# which are snapshot-driven and have no transaction ledger of their own.
SETTLEMENT_ACCOUNT_TYPES = (PaymentMethodType.CHECKING, PaymentMethodType.CASH)


@dataclass(frozen=True)
class LedgerAction:
    """What settling (or unsettling) did to the ledger, for API + UI feedback."""

    action: str
    transaction_id: int | None = None
    transaction_date: date_type | None = None
    amount: Decimal | None = None
    currency: str | None = None
    account_name: str | None = None
    category_name: str | None = None
    reason: str | None = None


def _describe(action: str, txn: Transaction, reason: str | None = None) -> LedgerAction:
    return LedgerAction(
        action=action,
        transaction_id=txn.id,
        transaction_date=txn.transaction_date,
        amount=Decimal(txn.amount),
        currency=txn.currency.value,
        account_name=txn.payment_method.name if txn.payment_method else None,
        category_name=txn.category.name if txn.category else None,
        reason=reason,
    )


def describe_link(session: Session, r: Receivable) -> LedgerAction:
    """The already-recorded ledger state of a settled receivable. Used when a
    settle call arrives for a row that is already linked, so repeating the
    action is a no-op that still tells the UI what is there."""
    if r.settled_transaction_id is None:
        return LedgerAction(
            action=NOTHING, reason="No ledger entry is linked to this receivable."
        )
    txn = session.get(Transaction, r.settled_transaction_id)
    if txn is None:
        return LedgerAction(action=NOTHING, reason="The linked ledger entry is gone.")
    return _describe(CREATED if r.settled_transaction_autocreated else LINKED, txn)


def _settlement_account_ids(session: Session, currency: Currency) -> list[int]:
    return list(
        session.scalars(
            select(PaymentMethod.id).where(
                PaymentMethod.active.is_(True),
                PaymentMethod.currency == currency,
                PaymentMethod.type.in_(SETTLEMENT_ACCOUNT_TYPES),
            )
        ).all()
    )


def _settlement_account(session: Session, r: Receivable) -> PaymentMethod | None:
    """Which account the payback moves through.

    Preference, most-informed first: the account that autopays the card the
    charge sat on (that is where we would send or receive the money), the
    receivable's own payment method when it is already a bank account, a
    household member's salary checking for this currency (configuration, so a
    household with several accounts gets its main one rather than whichever
    row happens to sort first), and only then any active account in the right
    currency, checking before cash."""
    candidates = list(
        session.scalars(
            select(PaymentMethod).where(
                PaymentMethod.active.is_(True),
                PaymentMethod.currency == r.currency,
                PaymentMethod.type.in_(SETTLEMENT_ACCOUNT_TYPES),
            )
        ).all()
    )
    if not candidates:
        return None
    candidates.sort(
        key=lambda pm: (0 if pm.type is PaymentMethodType.CHECKING else 1, pm.id)
    )
    by_id = {pm.id: pm for pm in candidates}

    charged_on = r.payment_method
    if charged_on is not None:
        paid_from = by_id.get(charged_on.paid_from_payment_method_id)
        if paid_from is not None:
            return paid_from
        if charged_on.id in by_id:
            return by_id[charged_on.id]

    salary_accounts = set(
        session.scalars(
            select(HouseholdMember.salary_checking_pm_id).where(
                HouseholdMember.salary_checking_pm_id.is_not(None)
            )
        ).all()
    )
    for pm in candidates:
        if pm.id in salary_accounts:
            return pm
    return candidates[0]


def _claimed_transaction_ids(session: Session):
    return select(Receivable.settled_transaction_id).where(
        Receivable.settled_transaction_id.is_not(None)
    )


def _is_claimed(session: Session, transaction_id: int) -> bool:
    return (
        session.scalar(
            select(func.count(Receivable.id)).where(
                Receivable.settled_transaction_id == transaction_id
            )
        )
        or 0
    ) > 0


def find_matching_transaction(
    session: Session, r: Receivable, settled_on: date_type
) -> Transaction | None:
    """An already-imported transaction that plausibly IS this payback.

    Sign carries the direction: money coming back to us is a negative (refund)
    row, money we paid out is a positive one. Ties are broken by amount
    exactness first, then by distance from the settle date, then by id so the
    choice is reproducible."""
    account_ids = _settlement_account_ids(session, r.currency)
    if not account_ids:
        return None

    target = Decimal(r.amount)
    low = target - MATCH_AMOUNT_TOLERANCE
    high = target + MATCH_AMOUNT_TOLERANCE
    money_in = r.direction is ReceivableDirection.OWED_TO_ME

    stmt = select(Transaction).where(
        Transaction.currency == r.currency,
        Transaction.payment_method_id.in_(account_ids),
        Transaction.transaction_date >= settled_on - timedelta(days=MATCH_WINDOW_DAYS),
        Transaction.transaction_date <= settled_on + timedelta(days=MATCH_WINDOW_DAYS),
        Transaction.id.not_in(_claimed_transaction_ids(session)),
        func.abs(Transaction.amount) >= low,
        func.abs(Transaction.amount) <= high,
        Transaction.amount < 0 if money_in else Transaction.amount > 0,
    )
    candidates = list(session.scalars(stmt).unique().all())
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: (
            abs(abs(Decimal(t.amount)) - target),
            abs((t.transaction_date - settled_on).days),
            t.id,
        )
    )
    return candidates[0]


def _original_charge(session: Session, r: Receivable) -> Transaction | None:
    """The ledger row this receivable was carved out of, when we can point at
    it. Reusing its category and merchant is what makes the refund net against
    the very line it belongs to rather than against a generic bucket."""
    if r.payment_method_id is None:
        return None
    window = timedelta(days=ORIGINAL_CHARGE_WINDOW_DAYS)
    rows = list(
        session.scalars(
            select(Transaction).where(
                Transaction.payment_method_id == r.payment_method_id,
                Transaction.currency == r.currency,
                Transaction.amount >= Decimal(r.amount),
                Transaction.transaction_date >= r.charge_date - window,
                Transaction.transaction_date <= r.charge_date + window,
            )
        ).unique().all()
    )
    if not rows:
        return None
    rows.sort(key=lambda t: (abs((t.transaction_date - r.charge_date).days), t.id))
    return rows[0]


def _classification_text(r: Receivable) -> str:
    return " ".join(part for part in (r.description, r.store) if part)


def _ledger_description(r: Receivable) -> str:
    if r.direction is ReceivableDirection.OWED_TO_ME:
        return f"Receivable #{r.id}: {r.person.name} paid back {r.description}"
    return f"Receivable #{r.id}: paid {r.person.name} back for {r.description}"


def _build_transaction(
    session: Session,
    r: Receivable,
    pm: PaymentMethod,
    settled_on: date_type,
    settled_by_user_id: int,
) -> Transaction:
    """Compose the ledger row, category first.

    The CATEGORY is what reports aggregate on, so a refund inherits the
    category of the charge it is refunding whenever we can find that charge;
    otherwise the categorization rules get a go at the description, same as
    every other write path in this app.

    The MERCHANT is the person. That is both the honest label ("Person A,
    -40.00, Groceries" reads exactly like what happened) and what keeps the
    two halves of a split from colliding: `uq_transaction_signature` includes
    the merchant, so two equal shares settled on the same day into the same
    account stay two distinct rows instead of one.

    The OWNER is whoever pressed "mark paid" (`settled_by_user_id`, threaded
    in from the router's session), same as a manual /transactions entry. Not
    a fixed "primary user" convention like the bank importers use: those rows
    have no acting user at all (a statement import or a Plaid pull), while a
    settlement is unambiguously performed by the logged-in user, and crediting
    it to anyone else is simply wrong.
    """
    categorizer = Categorizer(session)
    origin = (
        _original_charge(session, r)
        if r.direction is ReceivableDirection.OWED_TO_ME
        else None
    )
    category_id = (
        origin.category_id
        if origin is not None
        else categorizer.classify(_classification_text(r)).category_id
    )
    merchant_id = categorizer.get_or_create_merchant(r.person.name, category_id).id

    amount = Decimal(r.amount)
    if r.direction is ReceivableDirection.OWED_TO_ME:
        amount = -amount

    return Transaction(
        transaction_date=settled_on,
        merchant_id=merchant_id,
        category_id=category_id,
        payment_method_id=pm.id,
        amount=amount,
        # The currency rule: it comes from the account, and the account was
        # picked to match the receivable's currency. Never converted here.
        currency=pm.currency,
        description=_ledger_description(r)[:500],
        created_by_user_id=settled_by_user_id,
    )


def _existing_signature(session: Session, txn: Transaction) -> Transaction | None:
    """A row already carrying the exact signature we are about to insert.

    Without this the INSERT would trip `uq_transaction_signature` and 500 the
    settle call. Finding one means the entry is already there, so the honest
    outcome is a link, not an error."""
    return session.scalar(
        select(Transaction).filter_by(
            transaction_date=txn.transaction_date,
            merchant_id=txn.merchant_id,
            amount=txn.amount,
            payment_method_id=txn.payment_method_id,
            created_by_user_id=txn.created_by_user_id,
        )
    )


def post_settlement(
    session: Session, r: Receivable, settled_on: date_type, settled_by_user_id: int
) -> LedgerAction:
    """Link the receivable to the ledger entry that settles it, creating that
    entry only when the ledger does not already hold it. Caller commits.

    `settled_by_user_id` is the authenticated user pressing the button (see
    `routers/receivables.py`), and becomes the created row's
    `created_by_user_id` when a row is actually written. Required, never
    defaulted: a settle call always has a logged-in actor, and standing in
    a fixed user id here is exactly the bug that used to 500 this endpoint
    whenever that id did not exist in the database."""
    if r.settled_transaction_id is not None:
        return describe_link(session, r)

    match = find_matching_transaction(session, r, settled_on)
    if match is not None:
        r.settled_transaction = match
        r.settled_transaction_autocreated = False
        return _describe(
            LINKED,
            match,
            reason="An imported transaction already covered this payback.",
        )

    pm = _settlement_account(session, r)
    if pm is None:
        return LedgerAction(
            action=NOTHING,
            reason=(
                f"No active {r.currency.value} checking or cash account to post "
                f"this through, so nothing was written to the ledger."
            ),
        )

    txn = _build_transaction(session, r, pm, settled_on, settled_by_user_id)
    duplicate = _existing_signature(session, txn)
    if duplicate is not None:
        if _is_claimed(session, duplicate.id):
            # Another receivable already owns that row, and one ledger entry
            # cannot settle two of them. Inserting would trip
            # uq_transaction_signature, so say so instead of failing or
            # quietly under-recording the money.
            return LedgerAction(
                action=NOTHING,
                reason=(
                    f"Ledger entry #{duplicate.id} is identical to the one this "
                    f"would create and already settles another receivable. Post "
                    f"this payback manually on /transactions."
                ),
            )
        r.settled_transaction = duplicate
        r.settled_transaction_autocreated = False
        return _describe(
            LINKED,
            duplicate,
            reason="An identical ledger entry already existed.",
        )

    session.add(txn)
    session.flush()
    r.settled_transaction = txn
    r.settled_transaction_autocreated = True
    return _describe(CREATED, txn)


def reverse_settlement(session: Session, r: Receivable) -> LedgerAction:
    """Undo whatever `post_settlement` did: delete the row we created, or let
    go of the imported one. An imported transaction is never deleted — it is
    the bank's record of a real movement and predates the receivable's link to
    it. Caller commits."""
    txn = (
        session.get(Transaction, r.settled_transaction_id)
        if r.settled_transaction_id is not None
        else None
    )
    autocreated = r.settled_transaction_autocreated

    r.settled_transaction = None
    r.settled_transaction_autocreated = False
    session.flush()

    if txn is None:
        return LedgerAction(
            action=NOTHING, reason="There was no ledger entry to reverse."
        )
    if not autocreated:
        return _describe(
            UNLINKED,
            txn,
            reason="The imported transaction was kept; only the link was removed.",
        )
    action = _describe(DELETED, txn)
    session.delete(txn)
    session.flush()
    return action
