"""Plaid transactions/sync ingestion, adapted to v2's data model.

`pull_all_items()` runs on app boot and on-demand from /connections. For
each active PlaidItem it pulls the /transactions/sync delta and writes into
v2's `transactions` table — reusing v2's Categorizer for merchant/category.

Differences from v3's sync (deliberate, to fit v2):
- Every Plaid row is owned by the primary user (`created_by_user_id = 1`). The 2-user
  split exists only to disambiguate same-(date,merchant,amount,card) manual
  rows; Plaid's unique `transaction_id` makes that moot, so a fixed owner is
  correct.
- `transactions.currency` is taken from the payment_method (v2's hard rule:
  tx.currency == pm.currency), not from Plaid's iso_currency_code.
- Dedup/upsert is on `plaid_transaction_id` (the partial unique index).
- **Only CREDIT_CARD payment methods ingest transactions.** Checking/savings
  Plaid accounts contribute their balance only (via plaid_balances) — pulling
  their transactions would mix salary deposits / internal transfers into the
  ledger, which v2 models as income_entries, not transactions.
- CC payments and transfers are skipped (Plaid is the balance authority; a
  payment must not also be a spending row).
- No `posted_date` / `raw_description` / `pending` columns exist in v2.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from plaid.exceptions import ApiException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ImportLog,
    ImportSource,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    PlaidItem,
    PlaidItemStatus,
    Transaction,
)
from app.config import settings
from app.services.categorizer import Categorizer
from app.services.crypto import decrypt
from app.services.plaid_client import get_client

# The 2-user model is only for manual same-signature disambiguation; Plaid
# rows carry stable ids, so a fixed owner is correct.
PLAID_OWNER_USER_ID = 1


def _clean_start() -> date:
    """Clean-start anchor: Plaid tx before this date are dropped (prior
    months are frozen)."""
    return date.fromisoformat(settings.plaid_start_date)

# Plaid personal_finance_category.primary values that are NOT spending and
# must not become ledger rows (Plaid balance already reflects them).
_NON_SPENDING_PRIMARY = {"LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT"}


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _is_non_spending(tx: Any) -> bool:
    pfc = getattr(tx, "personal_finance_category", None)
    primary = (getattr(pfc, "primary", None) or "").upper() if pfc else ""
    return primary in _NON_SPENDING_PRIMARY


def pull_all_items(session: Session) -> dict[str, Any]:
    """Iterate every active PlaidItem, pull deltas, then refresh balances.
    Returns a small summary dict for logging / UI feedback."""
    items = list(
        session.scalars(
            select(PlaidItem).where(PlaidItem.status == PlaidItemStatus.ACTIVE)
        ).all()
    )
    summary: dict[str, Any] = {
        "items_synced": 0, "items_failed": 0,
        "added": 0, "modified": 0, "removed": 0, "adopted": 0,
        "skipped_unmapped": 0, "skipped_non_spending": 0, "skipped_pre_anchor": 0,
        "balances_refreshed": 0,
    }
    if not items:
        return summary

    categorizer = Categorizer(session)
    for item in items:
        try:
            counts = _sync_one_item(session, item, categorizer)
            summary["items_synced"] += 1
            for k in ("added", "modified", "removed", "adopted",
                      "skipped_unmapped", "skipped_non_spending", "skipped_pre_anchor"):
                summary[k] += counts[k]
            item.last_sync_error = None
            session.commit()
        except Exception as exc:
            session.rollback()
            summary["items_failed"] += 1
            fresh = session.get(PlaidItem, item.id)
            if fresh is not None:
                fresh.last_sync_error = f"{type(exc).__name__}: {exc}"[:1000]
                session.commit()

    try:
        from app.services.plaid_balances import refresh_balances_for_all_items
        bal = refresh_balances_for_all_items(session)
        summary["balances_refreshed"] = bal["refreshed"]
    except Exception:
        session.rollback()

    return summary


def _sync_one_item(
    session: Session, item: PlaidItem, categorizer: Categorizer
) -> dict[str, Any]:
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    client = get_client()
    access_token = decrypt(item.access_token)
    cursor: str | None = item.last_cursor
    counts = {
        "added": 0, "modified": 0, "removed": 0, "adopted": 0,
        "skipped_unmapped": 0, "skipped_non_spending": 0, "skipped_pre_anchor": 0,
    }
    skipped_accounts: set[str] = set()

    # One audit row per item per pull run; tx link back via import_log_id.
    log = ImportLog(
        filename=f"plaid:{item.institution_name}",
        source=ImportSource.PLAID,
        user_id=PLAID_OWNER_USER_ID,
        payment_method_id=None,
    )
    session.add(log)
    session.flush()

    while True:
        req_kwargs: dict[str, Any] = {"access_token": access_token}
        if cursor is not None:
            req_kwargs["cursor"] = cursor
        try:
            resp = client.transactions_sync(TransactionsSyncRequest(**req_kwargs))
        except ApiException as exc:
            _handle_api_error(item, exc)
            raise

        def _tally(tx: Any, *, default: str) -> None:
            outcome = _ingest_tx(session, tx, categorizer, log)
            if outcome == "unmapped":
                counts["skipped_unmapped"] += 1
                skipped_accounts.add(tx.account_id)
            elif outcome == "non_spending":
                counts["skipped_non_spending"] += 1
            elif outcome == "pre_anchor":
                counts["skipped_pre_anchor"] += 1
            elif outcome == "adopted":
                counts["adopted"] += 1
            else:
                counts[default] += 1

        for tx in resp.added:
            _tally(tx, default="added")
        for tx in resp.modified:
            _tally(tx, default="modified")
        for removed in resp.removed:
            if _remove_tx(session, removed.transaction_id):
                counts["removed"] += 1

        cursor = resp.next_cursor
        if not resp.has_more:
            break

    item.last_cursor = cursor
    item.last_sync_at = _utcnow()
    item.last_skipped_unmapped = (item.last_skipped_unmapped or 0) + counts["skipped_unmapped"]

    log.transaction_count = counts["added"] + counts["modified"] + counts["adopted"]
    log.skipped_count = (
        counts["skipped_unmapped"]
        + counts["skipped_non_spending"]
        + counts["skipped_pre_anchor"]
    )

    counts["skipped_accounts"] = sorted(skipped_accounts)
    return counts


def _handle_api_error(item: PlaidItem, exc: ApiException) -> None:
    body = (exc.body or "").lower() if hasattr(exc, "body") else ""
    if "item_login_required" in body:
        item.status = PlaidItemStatus.LOGIN_REQUIRED


def _ingest_tx(
    session: Session,
    tx: Any,
    categorizer: Categorizer,
    log: ImportLog,
) -> str | None:
    """Persist/update one Plaid tx into v2's transactions table.

    Returns:
      - "unmapped"      → account not yet mapped to a payment_method
      - "non_spending"  → checking/savings account, or a payment/transfer row
      - "pre_anchor"    → dated before the clean-start anchor (frozen months)
      - "adopted"       → matched a pre-existing manual row; stamped its id
      - None            → ingested (added or modified)
    """
    pm = session.scalar(
        select(PaymentMethod).where(PaymentMethod.plaid_account_id == tx.account_id)
    )
    if pm is None:
        return "unmapped"

    # Only credit cards ingest transactions; other account types contribute
    # balance only. Payments/transfers never become ledger rows.
    if pm.type != PaymentMethodType.CREDIT_CARD or _is_non_spending(tx):
        return "non_spending"

    # Clean-start anchor: prior months are frozen, drop older tx.
    if tx.date < _clean_start():
        return "pre_anchor"

    amount = Decimal(str(tx.amount))
    txn_date: date = tx.date

    # Already a Plaid-managed row → update in place (handles modified).
    existing = session.scalar(
        select(Transaction).where(Transaction.plaid_transaction_id == tx.transaction_id)
    )
    if existing is not None:
        description = (getattr(tx, "name", None) or "").strip() or "(no description)"
        match = categorizer.classify(description)
        merchant = (
            session.get(Merchant, match.merchant_id)
            if match.merchant_id is not None
            else categorizer.get_or_create_merchant(match.merchant_name, match.category_id)
        )
        existing.payment_method_id = pm.id
        existing.merchant_id = merchant.id
        existing.category_id = match.category_id
        existing.transaction_date = txn_date
        existing.amount = amount
        existing.currency = pm.currency
        existing.description = description[:500]
        existing.import_log_id = log.id
        return None

    # Dedup against a pre-existing MANUAL row (v2 had some June entries). Match
    # the importer's signature (date, amount, payment_method). Adopt it by
    # stamping the Plaid id so it isn't duplicated and future syncs track it;
    # keep the user's existing categorization/owner.
    manual = session.scalar(
        select(Transaction).where(
            Transaction.payment_method_id == pm.id,
            Transaction.transaction_date == txn_date,
            Transaction.amount == amount,
            Transaction.plaid_transaction_id.is_(None),
        )
    )
    if manual is not None:
        manual.plaid_transaction_id = tx.transaction_id
        manual.import_log_id = log.id
        return "adopted"

    # Genuinely new.
    description = (getattr(tx, "name", None) or "").strip() or "(no description)"
    match = categorizer.classify(description)
    merchant = (
        session.get(Merchant, match.merchant_id)
        if match.merchant_id is not None
        else categorizer.get_or_create_merchant(match.merchant_name, match.category_id)
    )
    session.add(
        Transaction(
            plaid_transaction_id=tx.transaction_id,
            payment_method_id=pm.id,
            merchant_id=merchant.id,
            category_id=match.category_id,
            transaction_date=txn_date,
            amount=amount,
            currency=pm.currency,
            description=description[:500],
            import_log_id=log.id,
            created_by_user_id=PLAID_OWNER_USER_ID,
        )
    )
    return None


def _remove_tx(session: Session, plaid_transaction_id: str) -> bool:
    existing = session.scalar(
        select(Transaction).where(Transaction.plaid_transaction_id == plaid_transaction_id)
    )
    if existing is not None:
        session.delete(existing)
        return True
    return False
