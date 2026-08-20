"""One writer per fact: the manual paths that must refuse a provider-fed
account, and the ones that must stay open.

Every guard answers the same four questions:

1. linked account: refused with 409, and the message names the writer;
2. unlinked account: still works (there the manual path is the only path);
3. DELETE: still works either way (cleanup is not a second writer);
4. the fields a human still owns: still editable.

Amounts here are invented, like everything in `factories.py`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    Category,
    CreditCardBalance,
    Currency,
    Merchant,
    PaymentMethod,
    SavingsSnapshot,
    Transaction,
)

# Fictional figures, distinguishable from each other and from anything real.
SNAPSHOT_BALANCE = Decimal("4321.00")
CARD_BALANCE = Decimal("777.00")
MANUAL_AMOUNT = Decimal("33.33")
PROVIDER_AMOUNT = Decimal("44.44")

PLAID_ACC = "provider-guard-plaid-account"
PLUGGY_ACC = "provider-guard-pluggy-account"


def _pm(db, name: str) -> PaymentMethod:
    return db.scalar(select(PaymentMethod).where(PaymentMethod.name == name))


def _id(db, model, name: str) -> int:
    return db.scalar(select(model.id).where(model.name == name))


def _drop(db, model, row_id: int) -> None:
    row = db.get(model, row_id)
    if row is not None:
        db.delete(row)
        db.commit()


@pytest.fixture
def plaid_card(db):
    """The fixture household's USD card, temporarily fed by Plaid."""
    pm = _pm(db, "Rewards Card")
    pm.plaid_account_id = PLAID_ACC
    db.commit()
    yield pm
    pm.plaid_account_id = None
    db.commit()


@pytest.fixture
def pluggy_checking(db):
    """The fixture household's BRL checking, temporarily fed by Pluggy."""
    pm = _pm(db, "Foreign Checking")
    pm.pluggy_account_id = PLUGGY_ACC
    db.commit()
    yield pm
    pm.pluggy_account_id = None
    db.commit()


# ---------------------------------------------------------------- savings

def test_savings_create_refused_for_provider_fed_account(pluggy_checking, client):
    r = client.post(
        "/api/savings/snapshots",
        json={
            "account_name": pluggy_checking.name,
            "currency": "BRL",
            "balance": str(SNAPSHOT_BALANCE),
        },
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Pluggy" in detail
    assert "Refresh balances" in detail  # the 409 names the automatic writer


def test_savings_create_still_works_for_an_unlinked_account(client, db):
    r = client.post(
        "/api/savings/snapshots",
        json={
            "account_name": "Main Checking",
            "currency": "USD",
            "balance": str(SNAPSHOT_BALANCE),
        },
    )
    assert r.status_code == 201, r.text
    _drop(db, SavingsSnapshot, r.json()["id"])


def test_savings_guard_is_case_sensitive_like_the_report(pluggy_checking, client, db):
    """The report aggregates per `account_name` string, so a differently cased
    name is a different bucket, not the fact the provider owns. What catches
    that is the casing rule in CLAUDE.md, not this guard."""
    r = client.post(
        "/api/savings/snapshots",
        json={
            "account_name": pluggy_checking.name.upper(),
            "currency": "BRL",
            "balance": str(SNAPSHOT_BALANCE),
        },
    )
    assert r.status_code == 201, r.text
    _drop(db, SavingsSnapshot, r.json()["id"])


def test_savings_patch_refused_and_delete_allowed(pluggy_checking, client, db):
    """A row the refresh owns: PATCH refused, DELETE allowed (the escape hatch
    the 409 points at)."""
    snap = SavingsSnapshot(
        account_name=pluggy_checking.name,
        currency=Currency.BRL,
        balance=SNAPSHOT_BALANCE,
    )
    db.add(snap)
    db.commit()

    r = client.patch(f"/api/savings/snapshots/{snap.id}", json={"balance": "1.00"})
    assert r.status_code == 409, r.text
    assert "Pluggy" in r.json()["detail"]

    assert client.delete(f"/api/savings/snapshots/{snap.id}").status_code == 204


def test_savings_patch_cannot_rename_a_manual_row_into_a_provider_account(
    pluggy_checking, client, db
):
    snap = SavingsSnapshot(
        account_name="Scratch Account",
        currency=Currency.BRL,
        balance=SNAPSHOT_BALANCE,
    )
    db.add(snap)
    db.commit()
    try:
        r = client.patch(
            f"/api/savings/snapshots/{snap.id}",
            json={"account_name": pluggy_checking.name},
        )
        assert r.status_code == 409, r.text

        # No provider account at either end of the same edit: still allowed.
        r = client.patch(f"/api/savings/snapshots/{snap.id}", json={"balance": "12.00"})
        assert r.status_code == 200, r.text
    finally:
        _drop(db, SavingsSnapshot, snap.id)


# ------------------------------------------------------ credit card balances

def test_card_balance_create_refused_for_provider_fed_card(plaid_card, client):
    r = client.post(
        "/api/debts/cards/balances",
        json={"payment_method_id": plaid_card.id, "balance": str(CARD_BALANCE)},
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Plaid" in detail
    assert "Refresh balances" in detail


def test_card_balance_create_still_works_for_an_unlinked_card(client, db):
    r = client.post(
        "/api/debts/cards/balances",
        json={"payment_method_id": _pm(db, "Foreign Card").id, "balance": str(CARD_BALANCE)},
    )
    assert r.status_code == 201, r.text
    assert client.delete(f"/api/debts/cards/balances/{r.json()['id']}").status_code == 204


def test_card_balance_patch_refused_and_delete_allowed(plaid_card, client, db):
    row = CreditCardBalance(payment_method_id=plaid_card.id, balance=CARD_BALANCE)
    db.add(row)
    db.commit()

    r = client.patch(f"/api/debts/cards/balances/{row.id}", json={"balance": "1.00"})
    assert r.status_code == 409, r.text
    assert "Plaid" in r.json()["detail"]

    assert client.delete(f"/api/debts/cards/balances/{row.id}").status_code == 204


def test_card_balance_patch_cannot_move_a_row_onto_a_provider_fed_card(
    plaid_card, client, db
):
    row = CreditCardBalance(
        payment_method_id=_pm(db, "Foreign Card").id, balance=CARD_BALANCE
    )
    db.add(row)
    db.commit()
    try:
        r = client.patch(
            f"/api/debts/cards/balances/{row.id}",
            json={"payment_method_id": plaid_card.id},
        )
        assert r.status_code == 409, r.text

        r = client.patch(f"/api/debts/cards/balances/{row.id}", json={"balance": "2.00"})
        assert r.status_code == 200, r.text
    finally:
        _drop(db, CreditCardBalance, row.id)


# ------------------------------------------------------------- transactions

def _txn_body(db, pm: PaymentMethod, amount: Decimal, day: int = 15) -> dict:
    return {
        "transaction_date": f"2026-07-{day:02d}",
        "merchant_id": _id(db, Merchant, "Market"),
        "category_id": _id(db, Category, "Groceries"),
        "payment_method_id": pm.id,
        "amount": str(amount),
    }


def _provider_row(db, pm: PaymentMethod, day: int, **provider_ids) -> Transaction:
    """A ledger row as a provider commit flow writes it: stamped with the
    provider transaction id that makes the provider its owner."""
    txn = Transaction(
        transaction_date=date(2026, 7, day),
        merchant_id=_id(db, Merchant, "Market"),
        category_id=_id(db, Category, "Groceries"),
        payment_method_id=pm.id,
        amount=PROVIDER_AMOUNT,
        currency=pm.currency,
        **provider_ids,
    )
    db.add(txn)
    db.commit()
    return txn


def test_transaction_create_refused_for_provider_fed_payment_method(
    plaid_card, client, db
):
    r = client.post("/api/transactions", json=_txn_body(db, plaid_card, MANUAL_AMOUNT))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Plaid" in detail
    assert "Review then Commit" in detail  # the 409 names the automatic writer


def test_transaction_create_still_works_on_an_unlinked_payment_method(client, db):
    """Cash and unlinked accounts have no automatic writer, so there the manual
    path is the only path and must stay open."""
    r = client.post(
        "/api/transactions", json=_txn_body(db, _pm(db, "Foreign Card"), MANUAL_AMOUNT)
    )
    assert r.status_code == 201, r.text
    assert client.delete(f"/api/transactions/{r.json()['id']}").status_code == 204


@pytest.fixture
def provider_row(db):
    txn = _provider_row(
        db, _pm(db, "Rewards Card"), 16, plaid_transaction_id="provider-guard-txn-1"
    )
    yield txn
    _drop(db, Transaction, txn.id)


def test_provider_row_allows_reclassification_but_not_the_fact(provider_row, client, db):
    rent = _id(db, Category, "Rent")

    # Classification is human judgement ABOUT the fact: allowed.
    r = client.patch(f"/api/transactions/{provider_row.id}", json={"category_id": rent})
    assert r.status_code == 200, r.text
    assert r.json()["category_id"] == rent
    assert r.json()["provider"] == "Plaid"

    # The fact itself: refused, and the 409 says who owns it.
    r = client.patch(f"/api/transactions/{provider_row.id}", json={"amount": "99.99"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Plaid" in detail and "amount" in detail

    for field, value in (
        ("transaction_date", "2026-07-20"),
        ("payment_method_id", _pm(db, "Main Checking").id),
    ):
        r = client.patch(f"/api/transactions/{provider_row.id}", json={field: value})
        assert r.status_code == 409, (field, r.text)


def test_resending_the_providers_own_values_is_not_a_change(provider_row, client, db):
    """Both UIs post the whole row while editing its category. Re-sending the
    provider's own values is not a second write, so it must not 409."""
    r = client.patch(
        f"/api/transactions/{provider_row.id}",
        json={
            "amount": str(PROVIDER_AMOUNT),
            "transaction_date": "2026-07-16",
            "payment_method_id": provider_row.payment_method_id,
            "description": "reviewed",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "reviewed"


def test_provider_row_split_is_refused(provider_row, client):
    """Split rewrites the amount, so it is the same fact reached by another
    door."""
    r = client.post(
        f"/api/transactions/{provider_row.id}/split", json={"installments": 2}
    )
    assert r.status_code == 409, r.text
    assert "Plaid" in r.json()["detail"]


def test_provider_row_delete_is_allowed(pluggy_checking, client, db):
    txn = _provider_row(
        db, pluggy_checking, 17, pluggy_transaction_id="provider-guard-txn-2"
    )
    assert client.delete(f"/api/transactions/{txn.id}").status_code == 204


def test_manual_row_cannot_be_moved_onto_a_provider_fed_payment_method(
    plaid_card, client, db
):
    """Otherwise the create guard is one PATCH away from useless: add the row
    on an unlinked account, then reassign it to the linked one."""
    r = client.post(
        "/api/transactions",
        json=_txn_body(db, _pm(db, "Foreign Card"), MANUAL_AMOUNT, day=19),
    )
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    try:
        r = client.patch(
            f"/api/transactions/{txn_id}", json={"payment_method_id": plaid_card.id}
        )
        assert r.status_code == 409, r.text
        assert "Plaid" in r.json()["detail"]
    finally:
        client.delete(f"/api/transactions/{txn_id}")


def test_provider_row_merchant_edit_can_create_a_new_merchant(provider_row, client, db):
    """Merchant is not in PROVIDER_OWNED_TRANSACTION_FIELDS: category, merchant
    and notes stay the human's on a provider-ingested row. `merchant_name` —
    the "New merchant..." option in both UIs — is the same kind of edit as
    `merchant_id` and must stay open here too, fictional-PC-on-Marketplace
    story included."""
    r = client.patch(
        f"/api/transactions/{provider_row.id}",
        json={"merchant_name": "Provider Row Marketplace Seller"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["merchant_name"] == "Provider Row Marketplace Seller"
    assert r.json()["provider"] == "Plaid"  # still a provider row otherwise

    merchant = db.scalar(
        select(Merchant).where(Merchant.name == "Provider Row Marketplace Seller")
    )
    assert merchant is not None
    assert r.json()["merchant_id"] == merchant.id
    # The row itself is dropped by the `provider_row` fixture teardown; drop
    # it here too so the new merchant (still referenced until then) can go.
    _drop(db, Transaction, provider_row.id)
    _drop(db, Merchant, merchant.id)


def test_a_manual_row_on_a_linked_account_stays_editable(plaid_card, client, db):
    """Rows written before the account was linked carry no provider
    transaction id, so the manual path that wrote them still owns them. The
    guard keys on the row's origin, not on the account's current linkage."""
    txn = Transaction(
        transaction_date=date(2026, 7, 18),
        merchant_id=_id(db, Merchant, "Market"),
        category_id=_id(db, Category, "Groceries"),
        payment_method_id=plaid_card.id,
        amount=MANUAL_AMOUNT,
        currency=Currency.USD,
    )
    db.add(txn)
    db.commit()
    try:
        r = client.patch(f"/api/transactions/{txn.id}", json={"amount": "34.34"})
        assert r.status_code == 200, r.text
        assert r.json()["provider"] is None
    finally:
        _drop(db, Transaction, txn.id)


# --------------------------------------------------- the guard that predates

def test_manual_import_guard_is_unchanged(plaid_card, client):
    """The oldest of these guards, refactored onto the shared predicate.
    /guide quotes its wording, so it must still read exactly as it did."""
    r = client.post(
        "/api/imports/preview",
        data={"payment_method_id": str(plaid_card.id)},
        files={"file": ("statement.csv", b"Date,Description,Amount\n", "text/csv")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == (
        f"'{plaid_card.name}' is auto-pulled via Plaid — manual import is "
        f"disabled for it. Use Connections → Pull now instead."
    )


def test_payment_methods_list_exposes_the_provider(plaid_card, pluggy_checking, client):
    """The one field both UIs read to hide a form that could only 409."""
    rows = client.get("/api/payment-methods?active_only=false").json()
    by_name = {m["name"]: m for m in rows}
    assert by_name[plaid_card.name]["provider"] == "Plaid"
    assert by_name[pluggy_checking.name]["provider"] == "Pluggy"
    assert by_name["Main Checking"]["provider"] is None
