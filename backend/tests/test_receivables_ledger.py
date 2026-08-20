"""Settling a receivable moves the ledger, not just the flag.

Every figure here is invented for the fictional fixture household (see
tests/factories.py). Nothing in this file is anyone's real amount.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from app.models import Category, Currency, Merchant, PaymentMethod, Person, Transaction, User
from app.services.auth import COOKIE_NAME, create_token
from app.services.reports import monthly_report

SETTLE_DAY = "2026-07-25"

# Fictional shares, all distinguishable from each other and from the fixture's
# own figures so a mismatched assertion is obvious.
SHARE = Decimal("40.00")
OWED_BACK = Decimal("26.00")
SPLIT_SHARE = Decimal("18.50")
BRL_SHARE = Decimal("40.00")  # same number as SHARE on purpose: currency isolation


def _pm(db, name: str) -> PaymentMethod:
    return db.scalar(select(PaymentMethod).filter_by(name=name))


def _person_id(db, name: str) -> int:
    return db.scalar(select(Person.id).filter_by(name=name))


def _imported_payback(db, *, account: PaymentMethod, amount: Decimal, on: date) -> Transaction:
    """A bank line already in the ledger, as an import would have left it."""
    txn = Transaction(
        transaction_date=on,
        merchant_id=db.scalar(select(Merchant.id).filter_by(name="Market")),
        category_id=db.scalar(select(Category.id).filter_by(name="Groceries")),
        payment_method_id=account.id,
        amount=amount,
        currency=account.currency,
        description="TRANSFER FROM A FRIEND",
        created_by_user_id=1,
    )
    db.add(txn)
    db.commit()
    return txn


def _create(client, **overrides) -> list[dict]:
    body = {
        "description": "Groceries share",
        "charge_date": "2026-07-09",
        "currency": "USD",
        "direction": "OWED_TO_ME",
        "shares": [],
    }
    body.update(overrides)
    res = client.post("/api/receivables", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _settle(client, rid: int, settled: bool = True, on: str = SETTLE_DAY) -> dict:
    payload = {"settled": settled}
    if settled:
        payload["settled_on"] = on
    res = client.patch(f"/api/receivables/{rid}/settle", json=payload)
    assert res.status_code == 200, res.text
    return res.json()


def test_settling_owed_to_me_posts_a_refund_and_never_income(client, db):
    """The charge is already in the ledger as spending; the payback nets it
    back down. Income must not move — recording it as extra income would
    overstate both income and spending for the month by the same share."""
    card = _pm(db, "Rewards Card")
    created = _create(
        client,
        payment_method_id=card.id,
        shares=[{"person_id": _person_id(db, "Person A"), "amount": str(SHARE)}],
    )
    rid = created[0]["id"]
    db.rollback()
    before = monthly_report(db, 2026, 7).totals
    try:
        result = _settle(client, rid)
        ledger = result["ledger"]

        assert ledger["action"] == "created"
        assert Decimal(ledger["amount"]) == -SHARE, "a payback is a refund, not a charge"
        assert ledger["currency"] == "USD"
        # Never the card the charge sat on: a negative row there would walk
        # down the derived card debt even though the statement still owes it.
        assert ledger["account_name"] == "Main Checking"
        # Inherited from the charge it refunds, so it nets that very line.
        assert ledger["category_name"] == "Groceries"
        assert result["receivable"]["settled"] is True
        assert result["receivable"]["settled_transaction_id"] == ledger["transaction_id"]
        assert result["receivable"]["settled_transaction_autocreated"] is True

        db.rollback()
        txn = db.get(Transaction, ledger["transaction_id"])
        assert txn.transaction_date == date(2026, 7, 25), "dated the day the money arrived"
        assert txn.currency is Currency.USD
        assert txn.merchant.name == "Person A"

        after = monthly_report(db, 2026, 7).totals
        assert after.total_spending_usd == before.total_spending_usd - SHARE
        assert after.gross_income_usd == before.gross_income_usd
    finally:
        client.delete(f"/api/receivables/{rid}")


def test_settling_i_owe_posts_the_expense_on_the_payback_date(client, db):
    """Nothing was in the ledger while the debt was open, so this side posts a
    real expense, dated when we actually handed the money over."""
    created = _create(
        client,
        description="Concert ticket share",
        direction="I_OWE",
        shares=[{"person_id": _person_id(db, "Person B"), "amount": str(OWED_BACK)}],
    )
    rid = created[0]["id"]
    db.rollback()
    before = monthly_report(db, 2026, 7).totals
    try:
        ledger = _settle(client, rid)["ledger"]

        assert ledger["action"] == "created"
        assert Decimal(ledger["amount"]) == OWED_BACK, "money out is a positive charge"
        assert ledger["account_name"] == "Main Checking"

        db.rollback()
        after = monthly_report(db, 2026, 7).totals
        assert after.total_spending_usd == before.total_spending_usd + OWED_BACK
        assert after.gross_income_usd == before.gross_income_usd
    finally:
        client.delete(f"/api/receivables/{rid}")


def test_settling_as_a_non_primary_user_credits_that_user(client, db):
    """The demo's exact failure: production only worked because the household's
    user happened to be id 1. Settling must credit whoever is actually logged
    in, not a fixed id — this user is created with id 7 specifically so a
    regression back to a hardcoded 1 fails loudly."""
    other = User(id=7, email="other@example.test", name="Other User")
    db.add(other)
    db.commit()
    # id=7 was assigned explicitly, bypassing the identity sequence. Resync it
    # so a later test's auto-assigned user id can never collide with this one.
    db.execute(
        text("SELECT setval(pg_get_serial_sequence('users', 'id'), (SELECT MAX(id) FROM users))")
    )
    db.commit()

    client.cookies.set(COOKIE_NAME, create_token(other.id))
    try:
        created = _create(
            client,
            shares=[{"person_id": _person_id(db, "Person A"), "amount": str(SHARE)}],
        )
        rid = created[0]["id"]
        try:
            result = _settle(client, rid)
            ledger = result["ledger"]
            assert ledger["action"] == "created"

            db.rollback()
            txn = db.get(Transaction, ledger["transaction_id"])
            assert txn.created_by_user_id == other.id
        finally:
            client.delete(f"/api/receivables/{rid}")
    finally:
        client.cookies.set(COOKIE_NAME, create_token(1))
        db.rollback()
        db.delete(db.get(User, other.id))
        db.commit()


def test_settling_links_an_already_imported_payback_instead_of_duplicating(client, db):
    """The Zelle landed before the user pressed the button. Creating a second
    row would net the charge down twice."""
    checking = _pm(db, "Main Checking")
    imported_id = _imported_payback(
        db, account=checking, amount=-SHARE, on=date(2026, 7, 24)
    ).id

    created = _create(
        client,
        shares=[{"person_id": _person_id(db, "Person A"), "amount": str(SHARE)}],
    )
    rid = created[0]["id"]
    try:
        result = _settle(client, rid)
        ledger = result["ledger"]

        assert ledger["action"] == "linked"
        assert ledger["transaction_id"] == imported_id
        assert result["receivable"]["settled_transaction_autocreated"] is False

        db.rollback()
        refunds = db.scalars(
            select(Transaction).where(
                Transaction.payment_method_id == checking.id,
                Transaction.amount == -SHARE,
            )
        ).unique().all()
        assert len(refunds) == 1, "the imported row must not be duplicated"
    finally:
        client.delete(f"/api/receivables/{rid}")
        db.rollback()
        still_there = db.get(Transaction, imported_id)
        assert still_there is not None, "an imported transaction is never deleted"
        db.delete(still_there)
        db.commit()


def test_unsettling_deletes_the_row_we_created(client, db):
    created = _create(
        client,
        shares=[{"person_id": _person_id(db, "Person A"), "amount": str(SHARE)}],
    )
    rid = created[0]["id"]
    try:
        posted = _settle(client, rid)["ledger"]
        assert posted["action"] == "created"
        txn_id = posted["transaction_id"]

        result = _settle(client, rid, settled=False)
        assert result["ledger"]["action"] == "deleted"
        assert result["ledger"]["transaction_id"] == txn_id
        assert result["receivable"]["settled"] is False
        assert result["receivable"]["settled_transaction_id"] is None

        db.rollback()
        assert db.get(Transaction, txn_id) is None
    finally:
        client.delete(f"/api/receivables/{rid}")


def test_unsettling_only_unlinks_an_imported_transaction(client, db):
    checking = _pm(db, "Main Checking")
    imported_id = _imported_payback(
        db, account=checking, amount=-OWED_BACK, on=date(2026, 7, 26)
    ).id

    created = _create(
        client,
        shares=[{"person_id": _person_id(db, "Person A"), "amount": str(OWED_BACK)}],
    )
    rid = created[0]["id"]
    try:
        assert _settle(client, rid)["ledger"]["action"] == "linked"

        result = _settle(client, rid, settled=False)
        assert result["ledger"]["action"] == "unlinked"
        assert result["receivable"]["settled_transaction_id"] is None

        db.rollback()
        assert db.get(Transaction, imported_id) is not None
    finally:
        client.delete(f"/api/receivables/{rid}")
        db.rollback()
        db.delete(db.get(Transaction, imported_id))
        db.commit()


def test_a_brl_receivable_never_posts_or_matches_in_usd(client, db):
    """Hard rule: the currencies never mix. A same-amount USD payback sitting
    right in the match window must be invisible to a BRL receivable, and the
    posted row must land on a BRL account."""
    decoy_id = _imported_payback(
        db,
        account=_pm(db, "Main Checking"),
        amount=-BRL_SHARE,
        on=date(2026, 7, 23),
    ).id

    created = _create(
        client,
        description="Farmacia share",
        currency="BRL",
        shares=[{"person_id": _person_id(db, "Person B"), "amount": str(BRL_SHARE)}],
    )
    rid = created[0]["id"]
    try:
        ledger = _settle(client, rid)["ledger"]

        assert ledger["action"] == "created", "the USD row must not have matched"
        assert ledger["currency"] == "BRL"
        assert ledger["account_name"] == "Foreign Checking"

        db.rollback()
        txn = db.get(Transaction, ledger["transaction_id"])
        assert txn.currency is Currency.BRL
        assert txn.payment_method.currency is Currency.BRL
    finally:
        client.delete(f"/api/receivables/{rid}")
        db.rollback()
        db.delete(db.get(Transaction, decoy_id))
        db.commit()


def test_split_shares_settle_independently(client, db):
    """Two equal shares of one bill are two receivables and two ledger rows.
    They must not collapse into one entry, or half the money goes unrecorded."""
    created = _create(
        client,
        description="Pizza night share",
        shares=[
            {"person_id": _person_id(db, "Person A"), "amount": str(SPLIT_SHARE)},
            {"person_id": _person_id(db, "Person B"), "amount": str(SPLIT_SHARE)},
        ],
    )
    assert created[0]["group_id"] and created[0]["group_id"] == created[1]["group_id"]
    first, second = created[0]["id"], created[1]["id"]
    db.rollback()
    before = monthly_report(db, 2026, 7).totals
    try:
        one = _settle(client, first)
        assert one["ledger"]["action"] == "created"

        open_row = next(
            r for r in client.get("/api/receivables").json() if r["id"] == second
        )
        assert open_row["settled"] is False
        assert open_row["settled_transaction_id"] is None

        two = _settle(client, second)
        assert two["ledger"]["action"] == "created"
        assert two["ledger"]["transaction_id"] != one["ledger"]["transaction_id"]

        db.rollback()
        after = monthly_report(db, 2026, 7).totals
        assert after.total_spending_usd == before.total_spending_usd - 2 * SPLIT_SHARE
    finally:
        for rid in (first, second):
            client.delete(f"/api/receivables/{rid}")


def test_deleting_a_settled_receivable_takes_its_autocreated_row_with_it(client, db):
    created = _create(
        client,
        shares=[{"person_id": _person_id(db, "Person A"), "amount": str(SHARE)}],
    )
    rid = created[0]["id"]
    txn_id = _settle(client, rid)["ledger"]["transaction_id"]

    assert client.delete(f"/api/receivables/{rid}").status_code == 204
    db.rollback()
    assert db.get(Transaction, txn_id) is None


MOBI_UA = {"user-agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobi Safari"}


def test_both_uis_report_created_versus_linked(client):
    """The distinction only helps if the user sees it. Desktop and phone both
    render the ledger outcome and both flag a row that is already posted."""
    for headers in ({}, MOBI_UA):
        body = client.get("/receivables", headers=headers).text
        assert "ledgerNote(l)" in body, headers
        assert "if (l.action === 'created')" in body, headers
        assert "if (l.action === 'linked')" in body, headers
        assert "r.settled_transaction_autocreated" in body, headers
