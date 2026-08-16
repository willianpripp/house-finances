"""Write Pluggy-reported balances into v2's snapshot tables. Mirror of
plaid_balances.py, same rules:

- CREDIT_CARD → `credit_card_balances` row (positive = owed; Pluggy CREDIT
  accounts report the used amount positive — confirmed against real data
  before any card was mapped).
- CHECKING/SAVINGS/INVESTMENT → `savings_snapshots` keyed on
  `account_name == pm.name`. The casing must match `payment_methods.name`
  verbatim, or the same account is aggregated twice in the report.
- At most one row per account per day: a fresh pull replaces the same-day
  row.

Only mapped accounts are written (mapping already enforced the currency
match). Auto-refresh wins over manual reductions for mapped cards, same as
Plaid: the provider is the source of truth once an account is mapped.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    CreditCardBalance,
    PaymentMethod,
    PaymentMethodType,
    PluggyItem,
    SavingsSnapshot,
)
from app.services import pluggy_client


def refresh_balances_for_item(session: Session, item: PluggyItem) -> dict[str, Any]:
    try:
        accounts = pluggy_client.list_accounts(item.item_id)
    except pluggy_client.PluggyError as exc:
        item.last_sync_error = str(exc)[:500]
        session.commit()
        return {"refreshed": 0, "skipped_unmapped": 0, "error": str(exc)[:500]}

    refreshed = 0
    skipped_unmapped = 0
    today = date.today()
    changes: list[dict[str, Any]] = []

    for acc in accounts:
        balance = acc.get("balance")
        if balance is None:
            continue

        pm = session.scalar(
            select(PaymentMethod).where(PaymentMethod.pluggy_account_id == acc["id"])
        )
        if pm is None:
            skipped_unmapped += 1
            continue

        target = Decimal(str(balance))

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

    # Investments: aggregate the item's positions into one daily
    # snapshot under the tracking PM. Positions churn (new CDB id per
    # money-box deposit, closed ones linger as zeros), so per-position
    # mapping would be noise — the SUM is the balance that matters.
    if item.investments_payment_method_id is not None:
        inv_pm = session.get(PaymentMethod, item.investments_payment_method_id)
        if inv_pm is not None:
            try:
                positions = pluggy_client.list_investments(item.item_id)
            except pluggy_client.PluggyError as exc:
                item.last_sync_error = str(exc)[:500]
                session.commit()
                return {
                    "refreshed": refreshed,
                    "skipped_unmapped": skipped_unmapped,
                    "changes": changes,
                    "error": str(exc)[:500],
                }
            total = sum(
                (Decimal(str(p["balance"])) for p in positions if p.get("balance")),
                Decimal("0"),
            )
            old = session.scalar(
                select(SavingsSnapshot.balance)
                .where(SavingsSnapshot.account_name == inv_pm.name,
                       func.date(SavingsSnapshot.recorded_at) < today)
                .order_by(SavingsSnapshot.recorded_at.desc()).limit(1)
            )
            changes.append({
                "name": inv_pm.name,
                "currency": inv_pm.currency.value if hasattr(inv_pm.currency, "value") else str(inv_pm.currency),
                "old": float(old) if old is not None else None,
                "new": float(total),
                "delta": float(total - old) if old is not None else None,
            })
            session.execute(
                delete(SavingsSnapshot).where(
                    SavingsSnapshot.account_name == inv_pm.name,
                    func.date(SavingsSnapshot.recorded_at) == today,
                )
            )
            session.add(
                SavingsSnapshot(
                    account_name=inv_pm.name, currency=inv_pm.currency, balance=total
                )
            )
            refreshed += 1

    item.last_sync_at = datetime.now(timezone.utc)
    item.last_sync_error = None
    session.commit()
    return {"refreshed": refreshed, "skipped_unmapped": skipped_unmapped, "changes": changes}


def refresh_balances_for_all_items(session: Session) -> dict[str, Any]:
    items = session.scalars(select(PluggyItem)).all()
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
