"""Write Plaid-reported balances into v2's snapshot tables.

Plaid is the balance authority for connected accounts. On each pull we call
/accounts/balance/get and record the bank-reported balance into the tables
v2 already derives net-worth and debt from — NO synthetic transactions, NO
initial_balance column (those are v3's model).

- CREDIT_CARD  → `credit_card_balances` row. Plaid's `current` for a card is
  the amount owed (positive); v2's `credit_card_balances.balance` is also
  positive-owed, so no sign flip. v2's `debts.latest_card_balance_live`
  reads the latest row + charges since, so this seeds it correctly.
- CHECKING/SAVINGS/INVESTMENT → `savings_snapshots` row keyed on
  `account_name == pm.name`. The casing must match `payment_methods.name`
  verbatim, or the same account is aggregated twice in the report.

At most one row per account per day: a fresh pull replaces the same-day row
so we don't accumulate clutter, and the "latest row wins" derivation stays
accurate.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from plaid.exceptions import ApiException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    CreditCardBalance,
    PaymentMethod,
    PaymentMethodType,
    PlaidItem,
    SavingsSnapshot,
)
from app.services.crypto import decrypt
from app.services.plaid_client import get_client


def refresh_balances_for_item(session: Session, item: PlaidItem) -> dict[str, Any]:
    from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

    client = get_client()
    access_token = decrypt(item.access_token)
    try:
        resp = client.accounts_balance_get(
            AccountsBalanceGetRequest(access_token=access_token)
        )
    except ApiException as exc:
        return {"refreshed": 0, "skipped_unmapped": 0, "error": f"plaid: {exc}"[:500]}

    refreshed = 0
    skipped_unmapped = 0
    today = date.today()
    changes: list[dict[str, Any]] = []

    for acc in resp.accounts:
        plaid_balance = acc.balances.current
        if plaid_balance is None:
            continue

        pm = session.scalar(
            select(PaymentMethod).where(PaymentMethod.plaid_account_id == acc.account_id)
        )
        if pm is None:
            skipped_unmapped += 1
            continue

        target = Decimal(str(plaid_balance))

        # Capture the prior balance (most recent snapshot before today) for the
        # before→after table the UI shows.
        if pm.type == PaymentMethodType.CREDIT_CARD:
            old = session.scalar(
                select(CreditCardBalance.balance)
                .where(CreditCardBalance.payment_method_id == pm.id,
                       func.date(CreditCardBalance.recorded_at) < today)
                .order_by(CreditCardBalance.recorded_at.desc()).limit(1)
            )
        else:
            old = session.scalar(
                select(SavingsSnapshot.balance)
                .where(SavingsSnapshot.account_name == pm.name,
                       func.date(SavingsSnapshot.recorded_at) < today)
                .order_by(SavingsSnapshot.recorded_at.desc()).limit(1)
            )
        changes.append({
            "name": pm.name,
            "currency": pm.currency.value if hasattr(pm.currency, "value") else str(pm.currency),
            "old": float(old) if old is not None else None,
            "new": float(target),
            "delta": float(target - (old if old is not None else Decimal("0"))) if old is not None else None,
        })

        if pm.type == PaymentMethodType.CREDIT_CARD:
            session.execute(
                delete(CreditCardBalance).where(
                    CreditCardBalance.payment_method_id == pm.id,
                    func.date(CreditCardBalance.recorded_at) == today,
                )
            )
            session.add(
                CreditCardBalance(payment_method_id=pm.id, balance=target)
            )
        else:
            session.execute(
                delete(SavingsSnapshot).where(
                    SavingsSnapshot.account_name == pm.name,
                    func.date(SavingsSnapshot.recorded_at) == today,
                )
            )
            session.add(
                SavingsSnapshot(
                    account_name=pm.name, currency=pm.currency, balance=target
                )
            )
        refreshed += 1

    session.commit()
    return {"refreshed": refreshed, "skipped_unmapped": skipped_unmapped, "changes": changes}


def refresh_balances_for_all_items(session: Session) -> dict[str, Any]:
    items = session.scalars(select(PlaidItem)).all()
    totals: dict[str, Any] = {"items": 0, "refreshed": 0, "skipped_unmapped": 0, "changes": []}
    for item in items:
        result = refresh_balances_for_item(session, item)
        if "error" in result:
            continue
        totals["items"] += 1
        totals["refreshed"] += result["refreshed"]
        totals["skipped_unmapped"] += result["skipped_unmapped"]
        totals["changes"].extend(result.get("changes", []))
    return totals
