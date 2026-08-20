"""The /api/income HTTP surface: one endpoint survives the page removal.

Mirrors what the exchange-rates write endpoints removal asserted the same day:
the write endpoints on a monthly total are GONE, not hidden behind a flag or a
disabled button, so nothing can quietly type a number into a derived table.

`GET /api/income` and `GET /api/income/receipts` existed only to feed the
`/income` page (both UIs). That page was removed 2026-08-20 (the owner's call):
with the total fully derived from receipts, a read-only ledger view had
nothing left to do that the monthly report doesn't already show, and grepping
templates/scripts/services/tests turned up no other consumer of either GET.
Both endpoints went with the page; asserting the underlying reads/writes now
goes through `services/income` directly rather than through the HTTP layer
(see `test_income_receipts.py`).

`DELETE /api/income/receipts/{id}` is the one surviving route: the escape
hatch for a wrong receipt, same shape as the one kept on `/api/exchange-rates`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models import Currency, IncomeEntry, IncomeReceipt, IncomeSource
from app.services import income as income_service

YEAR = 2033


def _money(raw) -> Decimal:
    """JSON amounts arrive as a string or a number depending on the serializer;
    compare values, not representations."""
    return Decimal(str(raw))


def _receipt(db, *, month: int, amount: str, description: str):
    income_service.record_receipt(
        db,
        income_service.ReceiptDraft(
            source=IncomeSource.EXTRA_BRL,
            year=YEAR,
            month=month,
            receipt_date=date(YEAR, month, 4),
            amount=Decimal(amount),
            currency=Currency.BRL,
            provenance=income_service.PROVENANCE_STATEMENT,
            payment_method_id=None,
            description=description,
        ),
    )
    income_service.recompute_month(db, YEAR, month, IncomeSource.EXTRA_BRL)
    db.commit()


def test_create_endpoint_is_gone(client):
    res = client.post(
        "/api/income",
        json={
            "year": YEAR,
            "month": 1,
            "source": "extra_brl",
            "amount": "10.00",
            "currency": "BRL",
        },
    )
    assert res.status_code in (404, 405)


def test_read_endpoints_removed_with_the_page(client):
    """`GET /api/income` and `GET /api/income/receipts` fed only the `/income`
    page; both are gone now that the page is. Confirms they are not merely
    hidden but actually absent, same bar the write-endpoint tests hold."""
    assert client.get(f"/api/income?year={YEAR}&month=1").status_code == 404
    assert client.get(f"/api/income/receipts?year={YEAR}&month=1").status_code == 404


def test_patch_endpoint_is_gone(client, db):
    db.add(
        IncomeEntry(
            year=YEAR,
            month=1,
            source=IncomeSource.EXTRA_USD,
            amount=Decimal("10.00"),
            currency=Currency.USD,
        )
    )
    db.commit()
    entry = db.scalar(
        select(IncomeEntry).filter_by(year=YEAR, month=1, source=IncomeSource.EXTRA_USD)
    )

    res = client.patch(f"/api/income/{entry.id}", json={"amount": "999.00"})
    assert res.status_code in (404, 405)

    db.expire_all()
    assert Decimal(
        db.get(IncomeEntry, entry.id).amount
    ) == Decimal("10.00")


def test_delete_endpoint_on_a_monthly_total_is_gone(client, db):
    db.add(
        IncomeEntry(
            year=YEAR,
            month=6,
            source=IncomeSource.EXTRA_USD,
            amount=Decimal("10.00"),
            currency=Currency.USD,
        )
    )
    db.commit()
    entry = db.scalar(
        select(IncomeEntry).filter_by(year=YEAR, month=6, source=IncomeSource.EXTRA_USD)
    )

    res = client.delete(f"/api/income/{entry.id}")
    assert res.status_code in (404, 405)

    db.expire_all()
    assert db.get(IncomeEntry, entry.id) is not None


def test_deleting_a_receipt_rederives_the_month(client, db):
    _receipt(db, month=4, amount="80.00", description="Keeper")
    _receipt(db, month=4, amount="45.00", description="Wrong one")

    wrong = db.scalar(
        select(IncomeReceipt).filter_by(
            year=YEAR, month=4, source=IncomeSource.EXTRA_BRL, description="Wrong one"
        )
    )
    res = client.delete(f"/api/income/receipts/{wrong.id}")
    assert res.status_code == 200
    assert _money(res.json()["entry"]["amount"]) == Decimal("80.00")

    db.expire_all()
    entry = db.scalar(
        select(IncomeEntry).filter_by(year=YEAR, month=4, source=IncomeSource.EXTRA_BRL)
    )
    assert Decimal(entry.amount) == Decimal("80.00")


def test_deleting_the_last_receipt_removes_the_derived_total(client, db):
    _receipt(db, month=5, amount="15.00", description="Only one")
    only = db.scalar(
        select(IncomeReceipt).filter_by(
            year=YEAR, month=5, source=IncomeSource.EXTRA_BRL
        )
    )

    res = client.delete(f"/api/income/receipts/{only.id}")
    assert res.status_code == 200
    assert res.json() == {"entry": None}

    db.expire_all()
    assert db.scalar(
        select(IncomeEntry).filter_by(year=YEAR, month=5, source=IncomeSource.EXTRA_BRL)
    ) is None


def test_deleting_an_unknown_receipt_is_a_404(client):
    assert client.delete("/api/income/receipts/99999999").status_code == 404
