"""Shared helpers for the /connections page.

Both providers list their items with a bank-ish name that does not identify
the connection on its own: Plaid can hold two Items at the same institution,
and every Pluggy item reports the same connector ("MeuPluggy", id 200) because
real BR banks are reached THROUGH it, never directly. The mapped payment
methods are what actually tells two items apart, so the list endpoints carry
them as a subtitle.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import PaymentMethod


def mapped_payment_method_names(
    db: Session, item_id_column: InstrumentedAttribute[int | None]
) -> dict[int, list[str]]:
    """Payment-method names per provider item, keyed by item id.

    `item_id_column` is `PaymentMethod.plaid_item_id` or
    `PaymentMethod.pluggy_item_id`; both are set at mapping time alongside the
    provider account id.
    """
    rows = db.execute(
        select(item_id_column, PaymentMethod.name)
        .where(item_id_column.is_not(None))
        .order_by(PaymentMethod.name)
    ).all()
    grouped: dict[int, list[str]] = {}
    for item_id, name in rows:
        grouped.setdefault(item_id, []).append(name)
    return grouped
