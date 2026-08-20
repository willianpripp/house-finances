"""Inline merchant creation for manual transactions.

The add-transaction form only offered EXISTING merchants, so a one-off
purchase (a mini PC bought on Facebook Marketplace, paid from a non-provider
account) had to be recorded under a wrong existing merchant. `merchant_name`
is now an alternative to `merchant_id` on POST/PATCH /api/transactions, with
create-or-get resolved through the same helper the importers use
(`app.services.categorizer.get_or_create_merchant`), matching
case-insensitively so "facebook marketplace" reuses "Facebook Marketplace"
instead of spawning a twin.

Amounts and merchant names here are invented, like everything in
factories.py.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Category, Merchant, PaymentMethod, User
from app.services.categorizer import get_or_create_merchant
from tests.factories import PRIMARY_NAME

NEW_MERCHANT_AMOUNT = Decimal("55.55")
DUP_SIGNATURE_AMOUNT = Decimal("66.66")


def _id(db, model, name: str) -> int:
    return db.scalar(select(model.id).where(model.name == name))


def _drop(db, model, row_id: int) -> None:
    row = db.get(model, row_id)
    if row is not None:
        db.delete(row)
        db.commit()


def _txn_body(db, merchant_field: dict, amount: Decimal, day: int) -> dict:
    # uq_transaction_signature includes created_by_user_id, and Postgres
    # treats two NULLs as distinct (never colliding) — an owner is required
    # here so the signature tests below actually exercise the constraint.
    return {
        "transaction_date": f"2026-08-{day:02d}",
        "category_id": _id(db, Category, "Groceries"),
        "payment_method_id": _id(db, PaymentMethod, "Main Checking"),
        "owner_user_id": _id(db, User, PRIMARY_NAME),
        "amount": str(amount),
        **merchant_field,
    }


# ------------------------------------------------- get_or_create_merchant unit

def test_get_or_create_merchant_reuses_case_insensitive_match(db):
    """Same helper the importers call. A differently-cased retype must reuse
    the row, not spawn a near-duplicate merchant."""
    groceries = _id(db, Category, "Groceries")
    created = get_or_create_merchant(db, "Facebook Marketplace", groceries)
    db.commit()
    try:
        reused = get_or_create_merchant(db, "facebook marketplace", groceries)
        db.commit()
        assert reused.id == created.id

        also_reused = get_or_create_merchant(db, "FACEBOOK MARKETPLACE", groceries)
        assert also_reused.id == created.id

        matches = db.scalars(
            select(Merchant).where(Merchant.name.ilike("facebook marketplace"))
        ).all()
        assert len(matches) == 1, "case variants must resolve to a single row"
    finally:
        _drop(db, Merchant, created.id)


def test_get_or_create_merchant_exact_match_is_also_reused(db):
    groceries = _id(db, Category, "Groceries")
    market_id = _id(db, Merchant, "Market")
    same = get_or_create_merchant(db, "Market", groceries)
    assert same.id == market_id


# --------------------------------------------------- manual create, new name

def test_manual_create_with_new_merchant_name_creates_and_links(client, db):
    """The user story: no existing merchant fits, so the form sends a name
    instead of an id and the API creates the merchant and links the row."""
    body = _txn_body(db, {"merchant_name": "Facebook Marketplace Seller"}, NEW_MERCHANT_AMOUNT, 10)
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["merchant_name"] == "Facebook Marketplace Seller"

    merchant = db.scalar(
        select(Merchant).where(Merchant.name == "Facebook Marketplace Seller")
    )
    assert merchant is not None
    assert out["merchant_id"] == merchant.id
    # the manual transaction's own category becomes the new merchant's default
    assert merchant.default_category_id == body["category_id"]

    try:
        assert client.delete(f"/api/transactions/{out['id']}").status_code == 204
    finally:
        _drop(db, Merchant, merchant.id)


def test_manual_create_with_existing_name_different_case_reuses_row(client, db):
    """Typing an existing merchant's name in a different case must not spawn
    a duplicate; it links to the existing row."""
    market_id = _id(db, Merchant, "Market")
    body = _txn_body(db, {"merchant_name": "market"}, NEW_MERCHANT_AMOUNT, 11)
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["merchant_id"] == market_id
    client.delete(f"/api/transactions/{r.json()['id']}")


def test_manual_create_requires_merchant_id_or_merchant_name(client, db):
    body = _txn_body(db, {}, NEW_MERCHANT_AMOUNT, 12)
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 400, r.text
    assert "merchant_id or merchant_name" in r.json()["detail"]


def test_manual_create_merchant_id_wins_over_merchant_name(client, db):
    """If a client somehow sends both, the id (an unambiguous reference) is
    used and the name is ignored rather than silently creating a merchant."""
    market_id = _id(db, Merchant, "Market")
    body = _txn_body(
        db,
        {"merchant_id": market_id, "merchant_name": "Should Not Be Created"},
        NEW_MERCHANT_AMOUNT,
        13,
    )
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["merchant_id"] == market_id
    assert db.scalar(
        select(Merchant).where(Merchant.name == "Should Not Be Created")
    ) is None
    client.delete(f"/api/transactions/{r.json()['id']}")


# ---------------------------------------------------------------- PATCH edit

def test_patch_can_rename_a_transaction_onto_a_new_merchant(client, db):
    body = _txn_body(db, {"merchant_id": _id(db, Merchant, "Market")}, NEW_MERCHANT_AMOUNT, 14)
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    try:
        r = client.patch(
            f"/api/transactions/{txn_id}", json={"merchant_name": "Yard Sale Larry"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["merchant_name"] == "Yard Sale Larry"
        merchant = db.scalar(select(Merchant).where(Merchant.name == "Yard Sale Larry"))
        assert merchant is not None
        assert r.json()["merchant_id"] == merchant.id
    finally:
        client.delete(f"/api/transactions/{txn_id}")
        _drop(db, Merchant, _id(db, Merchant, "Yard Sale Larry") or -1)


# --------------------------------------------------- uq_transaction_signature

def test_signature_dedupe_still_applies_when_merchant_name_resolves_to_existing_row(
    client, db
):
    """uq_transaction_signature keys on (date, merchant_id, amount, payment
    method, owner). Resolving "MARKET" case-insensitively must land on the
    SAME merchant_id as "Market" — if it instead spawned a second merchant,
    this second insert would silently succeed and double-count the row
    instead of colliding with the constraint."""
    market_id = _id(db, Merchant, "Market")
    body = _txn_body(
        db, {"merchant_id": market_id}, DUP_SIGNATURE_AMOUNT, 15
    )
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    try:
        dup_body = {**body, "merchant_id": None, "merchant_name": "MARKET"}
        with pytest.raises(IntegrityError):
            client.post("/api/transactions", json=dup_body)
    finally:
        client.delete(f"/api/transactions/{txn_id}")


def test_signature_allows_a_genuinely_different_merchant_on_the_same_day(client, db):
    """Sanity check the counter-case: a new, distinct merchant_name on the
    same date/amount/payment method as an existing row is NOT a collision."""
    body = _txn_body(
        db, {"merchant_id": _id(db, Merchant, "Market")}, DUP_SIGNATURE_AMOUNT, 16
    )
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 201, r.text
    txn_id = r.json()["id"]
    try:
        other_body = {**body, "merchant_id": None, "merchant_name": "A Genuinely Different Merchant"}
        r2 = client.post("/api/transactions", json=other_body)
        assert r2.status_code == 201, r2.text
        client.delete(f"/api/transactions/{r2.json()['id']}")
        _drop(db, Merchant, _id(db, Merchant, "A Genuinely Different Merchant") or -1)
    finally:
        client.delete(f"/api/transactions/{txn_id}")
