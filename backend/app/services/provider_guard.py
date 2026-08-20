"""One writer per fact.

A `payment_methods` row carrying a `plaid_account_id` or a `pluggy_account_id`
is fed by that provider, and the provider owns two facts about it:

- the account's **balance**, written by the balance refresh (on boot and from
  the "Refresh balances" button in /connections) into `savings_snapshots` or
  `credit_card_balances`;
- the account's **transactions**, written by the per-account Review then
  Commit flow in /connections.

Where a manual path would write the same fact, it is refused with 409. Two
writers for one balance means the number is whichever path ran last. Two
writers for one transaction means double counting, because the manual row does
not collide with the provider's version of the same purchase: the signature
dedupe keys on merchant, and the provider's merchant string differs from what
a human types.

Every guard NAMES the automatic writer, so the 409 teaches the rule instead of
only blocking. Four things are deliberately not guarded:

- **DELETE**, on any of these tables. Removing a wrong row is cleanup, not a
  second writer, and it is the escape hatch the 409 messages point at.
- **Manual writes on a payment method with no provider id.** Cash and unlinked
  accounts have no automatic writer, so there the manual path is the only path.
- **Classification on a provider-ingested transaction.** Category, merchant and
  notes are human judgement *about* a fact, not the fact; the provider owns
  amount, date and payment method.
- **Reads.** Nothing here runs on a GET.

The 409 status and the wording live here rather than in each router because the
status is part of the documented contract (/guide tells the user a provider-fed
account answers manual writes with a 409) and six call sites re-wording it
would drift. That is why this is the one service module that imports
HTTPException.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentMethod, Transaction

PLAID = "Plaid"
PLUGGY = "Pluggy"

# What the provider owns on a row it ingested, mapped to the words the 409 uses.
PROVIDER_OWNED_TRANSACTION_FIELDS: dict[str, str] = {
    "amount": "amount",
    "transaction_date": "date",
    "payment_method_id": "payment method",
}


def provider_for_payment_method(pm: PaymentMethod | None) -> str | None:
    """Which auto-pull feeds this payment method, or None for a manual one."""
    if pm is None:
        return None
    if pm.plaid_account_id:
        return PLAID
    if pm.pluggy_account_id:
        return PLUGGY
    return None


def provider_for_transaction(txn: Transaction | None) -> str | None:
    """Which auto-pull ingested this ledger row, or None if a human/parser did.

    Keyed on the provider transaction id rather than on the row's payment
    method: a row that predates the account being linked was written by the
    manual path and stays editable by it.
    """
    if txn is None:
        return None
    if txn.plaid_transaction_id:
        return PLAID
    if txn.pluggy_transaction_id:
        return PLUGGY
    return None


def payment_method_by_account_name(
    session: Session, account_name: str
) -> PaymentMethod | None:
    """Resolve `savings_snapshots.account_name` (free text) to its payment
    method.

    The match is case-SENSITIVE, and that is the point. The savings report
    aggregates per `account_name` string, so "Foo HYSA" and "FOO HYSA" are
    two accounts to it: only the exact spelling the refresh writes (`pm.name`
    verbatim) names the fact the provider owns. Folding case here would refuse
    a name that lands in a different bucket anyway, and hide the casing bug
    that the CLAUDE.md casing rule exists to catch.
    """
    return session.scalar(
        select(PaymentMethod).where(PaymentMethod.name == account_name)
    )


def _refuse_balance(pm: PaymentMethod, provider: str) -> None:
    raise HTTPException(
        status_code=409,
        detail=(
            f"Balances for '{pm.name}' come from {provider}: the balance "
            f"refresh on boot and the Refresh balances button in Connections "
            f"own this number. Typing one here would give the account two "
            f"writers. Deleting a wrong row is still allowed."
        ),
    )


def guard_savings_snapshot_write(session: Session, account_name: str) -> None:
    """POST/PATCH /api/savings/snapshots for a provider-fed account."""
    pm = payment_method_by_account_name(session, account_name.strip())
    provider = provider_for_payment_method(pm)
    if provider is not None:
        _refuse_balance(pm, provider)


def guard_card_balance_write(session: Session, payment_method_id: int) -> None:
    """POST/PATCH /api/debts/cards/balances for a provider-fed card."""
    pm = session.get(PaymentMethod, payment_method_id)
    provider = provider_for_payment_method(pm)
    if provider is not None:
        _refuse_balance(pm, provider)


def guard_transaction_create(session: Session, payment_method_id: int) -> None:
    """POST /api/transactions, and any PATCH that moves a row onto a
    provider-fed payment method (same fact, same double count)."""
    pm = session.get(PaymentMethod, payment_method_id)
    provider = provider_for_payment_method(pm)
    if provider is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"'{pm.name}' is fed by {provider}: its transactions come from the "
            f"per-account Review then Commit flow in Connections, which owns "
            f"this ledger. A manual row would not collide with {provider}'s "
            f"version of the same purchase (the merchant differs), so the "
            f"spending would count twice."
        ),
    )


def guard_transaction_patch(
    session: Session, txn: Transaction, changes: Mapping[str, Any]
) -> None:
    """PATCH /api/transactions/{id}.

    Two rules, in this order:

    1. On a provider-ingested row, the provider owns amount, date and payment
       method. Only an actual change is refused, so the UIs may keep posting
       the whole row while editing its category.
    2. Any row being moved onto a provider-fed payment method is refused, or
       the create guard would be one PATCH away from useless.
    """
    provider = provider_for_transaction(txn)
    if provider is not None:
        changed = [
            label
            for field, label in PROVIDER_OWNED_TRANSACTION_FIELDS.items()
            if field in changes
            and changes[field] is not None
            and changes[field] != getattr(txn, field)
        ]
        if changed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{provider} ingested this transaction and owns its "
                    f"amount, date and payment method; the Review then Commit "
                    f"flow in Connections is the only writer for those, so the "
                    f"change to {' and '.join(changed)} is refused. Category, "
                    f"merchant and notes are yours to edit."
                ),
            )

    target_pm_id = changes.get("payment_method_id")
    if target_pm_id is not None and target_pm_id != txn.payment_method_id:
        guard_transaction_create(session, target_pm_id)


def guard_transaction_split(txn: Transaction) -> None:
    """POST /api/transactions/{id}/split rewrites the row's amount, so it is
    the same fact the PATCH guard protects, reached by another door."""
    provider = provider_for_transaction(txn)
    if provider is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"{provider} ingested this transaction and owns its amount; "
            f"splitting rewrites it. Re-pull the account in Connections if the "
            f"amount is wrong."
        ),
    )
