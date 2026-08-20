"""Plaid pending -> posted: one purchase must never become two ledger rows.

The production failure these tests pin: a card purchase ingested while it was
pending stayed in the ledger after Plaid replaced it with the posted version,
which arrived with a NEW transaction id, a different descriptor and a later
date. Nothing matched the two, so the month counted the purchase twice.

Plaid links them with `pending_transaction_id` on the posted transaction.
`app/services/pending_supersede.py` is the one place that decides what that
link may overwrite (transaction id, descriptor, date, amount, pending flag)
and what it must preserve (the row id, and with it a human's categorization
and any receivable pointing at the row).

No HTTP anywhere: the Plaid pull is monkeypatched, and the transactions are
plain objects shaped like plaid-python's model (attribute access, float
amount, `date`, `name`, `pending`, `pending_transaction_id`,
`personal_finance_category.primary`).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    Currency,
    ImportLog,
    ImportSource,
    Merchant,
    PaymentMethod,
    PaymentMethodType,
    PlaidItem,
    PlaidItemStatus,
    PlaidSeenTransaction,
    Receivable,
    Transaction,
    User,
)

ACCOUNT_ID = "plaid-acct-supersede"
PENDING_TX = "plaid-tx-pending-1"
POSTED_TX = "plaid-tx-posted-1"
PENDING_DESC = "YARN SHOP"          # the provisional descriptor
POSTED_DESC = "YARNSHOP/CRAFTS"     # the same purchase, once captured
CHARGE = Decimal("12.71")
CHARGE_WITH_TIP = Decimal("15.25")  # pending -> posted amounts legitimately move
PENDING_DAY = date(2026, 8, 14)
POSTED_DAY = date(2026, 8, 18)


def _tx(
    *,
    transaction_id: str,
    amount: Decimal,
    day: date,
    name: str,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    pfc: str = "GENERAL_MERCHANDISE",
) -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id=transaction_id,
        amount=float(amount),
        date=day,
        name=name,
        pending=pending,
        pending_transaction_id=pending_transaction_id,
        personal_finance_category=SimpleNamespace(primary=pfc),
    )


def _pending_tx() -> SimpleNamespace:
    return _tx(
        transaction_id=PENDING_TX, amount=CHARGE, day=PENDING_DAY,
        name=PENDING_DESC, pending=True,
    )


def _posted_tx(amount: Decimal = CHARGE) -> SimpleNamespace:
    return _tx(
        transaction_id=POSTED_TX, amount=amount, day=POSTED_DAY,
        name=POSTED_DESC, pending_transaction_id=PENDING_TX,
    )


def _make_mapped_pm(db, name: str, pm_type: PaymentMethodType):
    """A payment method fed by Plaid, plus the Item it hangs off.

    The access token is a stub and is never decrypted: every test replaces the
    fetch, so no Plaid call and no Fernet key is involved.
    """
    user_id = db.scalar(select(User.id).order_by(User.id))
    item = PlaidItem(
        user_id=user_id,
        item_id=f"item-{name}",
        institution_id="ins_test",
        institution_name="Test Bank",
        access_token="stub",
        status=PlaidItemStatus.ACTIVE,
    )
    db.add(item)
    db.flush()
    pm = PaymentMethod(
        name=name,
        type=pm_type,
        currency=Currency.USD,
        plaid_item_id=item.id,
        plaid_account_id=ACCOUNT_ID,
    )
    db.add(pm)
    db.commit()
    return pm, item


def _cleanup(db, pm: PaymentMethod, item: PlaidItem, merchant_ids_before: set[int]) -> None:
    for txn in db.scalars(
        select(Transaction).where(Transaction.payment_method_id == pm.id)
    ).all():
        for r in db.scalars(
            select(Receivable).where(Receivable.settled_transaction_id == txn.id)
        ).all():
            r.settled_transaction_id = None
        db.delete(txn)
    db.commit()
    for seen in db.scalars(
        select(PlaidSeenTransaction).where(PlaidSeenTransaction.payment_method_id == pm.id)
    ).all():
        db.delete(seen)
    for log in db.scalars(
        select(ImportLog).where(ImportLog.payment_method_id == pm.id)
    ).all():
        db.delete(log)
    db.commit()
    for mid in set(db.scalars(select(Merchant.id)).all()) - merchant_ids_before:
        db.delete(db.get(Merchant, mid))
    db.delete(pm)
    db.delete(item)
    db.commit()


@pytest.fixture
def plaid_card(db):
    pm, item = _make_mapped_pm(db, "Supersede Test Card", PaymentMethodType.CREDIT_CARD)
    before = set(db.scalars(select(Merchant.id)).all())
    yield pm
    _cleanup(db, pm, item, before)


@pytest.fixture
def plaid_checking(db):
    pm, item = _make_mapped_pm(db, "Supersede Test Checking", PaymentMethodType.CHECKING)
    before = set(db.scalars(select(Merchant.id)).all())
    yield pm
    _cleanup(db, pm, item, before)


# ---------------------------------------------------------------- card helpers

def _card_preview(db, pm, txns):
    from app.services.importer import build_preview
    from app.services.plaid_import import to_card_parseresult

    return build_preview(
        db,
        filename=f"plaid:{pm.name}",
        payment_method_id=pm.id,
        pre_parsed=to_card_parseresult(txns, pm),
    )


def _card_commit(db, pm, txns, skip_indices=None):
    from app.services.importer import commit_import
    from app.services.plaid_import import to_card_parseresult

    return commit_import(
        db,
        filename=f"plaid:{pm.name}",
        payment_method_id=pm.id,
        pre_parsed=to_card_parseresult(txns, pm),
        source_override=ImportSource.PLAID,
        skip_indices=skip_indices,
    )


def _rows(db, pm) -> list[Transaction]:
    return list(
        db.scalars(
            select(Transaction)
            .where(Transaction.payment_method_id == pm.id)
            .order_by(Transaction.id)
        ).all()
    )


def _human_edits(db, txn: Transaction) -> tuple[int, int, int]:
    """Simulate the two things a human puts on a ledger row that a
    delete-and-insert would destroy: a corrected category/merchant, and a
    receivable settled by this row."""
    from app.models import Category

    market = db.scalar(select(Merchant).where(Merchant.name == "Market"))
    groceries_id = db.scalar(select(Category.id).where(Category.name == "Groceries"))
    txn.merchant_id = market.id
    txn.category_id = groceries_id
    receivable = db.scalars(select(Receivable).order_by(Receivable.id)).first()
    receivable.settled_transaction_id = txn.id
    db.commit()
    return market.id, groceries_id, receivable.id


# ------------------------------------------------------------------ card tests

def test_posted_updates_pending_row_in_place(db, plaid_card):
    """The prod scenario, in two reviews: the pending row was committed, the
    posted version arrives later. One row, still the same row."""
    _card_commit(db, plaid_card, [_pending_tx()])
    rows = _rows(db, plaid_card)
    assert len(rows) == 1
    pending_row = rows[0]
    original_id = pending_row.id
    assert pending_row.pending is True
    assert pending_row.plaid_transaction_id == PENDING_TX

    merchant_id, category_id, receivable_id = _human_edits(db, pending_row)

    result = _card_commit(db, plaid_card, [_posted_tx()])
    assert result.pending_reconciled == 1
    assert result.transactions_created == 0
    assert result.duplicates_skipped == 0

    rows = _rows(db, plaid_card)
    assert len(rows) == 1, "the posted version must not add a second row"
    row = rows[0]
    assert row.id == original_id, "the row keeps its identity"
    assert row.plaid_transaction_id == POSTED_TX
    assert row.description == POSTED_DESC
    assert row.transaction_date == POSTED_DAY
    assert Decimal(row.amount) == CHARGE
    assert row.pending is False
    # The human's work survives: the provider owns the fact, not its filing.
    assert row.merchant_id == merchant_id
    assert row.category_id == category_id
    settled = db.get(Receivable, receivable_id)
    assert settled.settled_transaction_id == original_id


def test_amount_change_updates_in_place_and_is_flagged(db, plaid_card):
    """A tip or fuel hold moves the amount between pending and posted. Same
    update in place; the preview says the figure is about to move."""
    _card_commit(db, plaid_card, [_pending_tx()])
    original_id = _rows(db, plaid_card)[0].id

    preview = _card_preview(db, plaid_card, [_posted_tx(CHARGE_WITH_TIP)])
    row = preview.transactions[0]
    assert row.supersedes_transaction_id == original_id
    assert row.amount_changed is True
    assert row.supersedes_prior_amount == CHARGE

    result = _card_commit(db, plaid_card, [_posted_tx(CHARGE_WITH_TIP)])
    assert result.pending_reconciled == 1
    rows = _rows(db, plaid_card)
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert Decimal(rows[0].amount) == CHARGE_WITH_TIP
    assert rows[0].pending is False


@pytest.mark.parametrize("posted_first", [True, False])
def test_pending_and_posted_in_one_batch_land_once(db, plaid_card, posted_first):
    """Both versions in the same pull — an issuer can keep the pending
    transaction listed for days after the posted one appears, and the review
    window spans the whole history. Committing both is what duplicated the
    purchase in production. Order must not matter: the sessions do not
    autoflush, so a per-row lookup cannot see the row added a moment earlier."""
    batch = [_posted_tx(), _pending_tx()] if posted_first else [_pending_tx(), _posted_tx()]

    result = _card_commit(db, plaid_card, batch)
    assert result.transactions_created == 1
    assert result.pending_superseded == 1

    rows = _rows(db, plaid_card)
    assert len(rows) == 1
    assert rows[0].plaid_transaction_id == POSTED_TX
    assert rows[0].description == POSTED_DESC
    assert rows[0].pending is False


def test_posted_with_no_matching_pending_inserts_normally(db, plaid_card):
    """`pending_transaction_id` pointing at nothing in the ledger (the pending
    row was dismissed, or predates the clean-start anchor) is a plain insert,
    not a dropped row."""
    result = _card_commit(db, plaid_card, [_posted_tx()])
    assert result.transactions_created == 1
    assert result.pending_reconciled == 0
    assert result.pending_superseded == 0

    rows = _rows(db, plaid_card)
    assert len(rows) == 1
    assert rows[0].plaid_transaction_id == POSTED_TX
    assert rows[0].pending is False


def test_plaid_transaction_id_dedup_still_works(db, plaid_card):
    """Re-reviewing the same window is still idempotent per Plaid id — the
    supersede path must not have opened a second door for the same row."""
    _card_commit(db, plaid_card, [_posted_tx()])
    again = _card_commit(db, plaid_card, [_posted_tx()])
    assert again.transactions_created == 0
    assert again.duplicates_skipped == 1
    assert len(_rows(db, plaid_card)) == 1

    # And re-committing the posted row after it already superseded a pending
    # one hits the same guard rather than superseding a second time.
    third = _card_commit(db, plaid_card, [_posted_tx()])
    assert third.duplicates_skipped == 1
    assert third.pending_reconciled == 0
    assert len(_rows(db, plaid_card)) == 1


def test_preview_carries_the_supersede_state(db, plaid_card):
    """The review stage shows the supersede as its own row state: not a plain
    new transaction, and not a duplicate either."""
    _card_commit(db, plaid_card, [_pending_tx()])
    original_id = _rows(db, plaid_card)[0].id

    preview = _card_preview(db, plaid_card, [_posted_tx()])
    row = preview.transactions[0]
    assert row.supersedes_transaction_id == original_id
    assert row.superseded_by_posted is False
    assert row.is_duplicate is False
    assert row.amount_changed is False
    assert preview.new_count == 0, "an update in place is not a new ledger row"


def test_preview_marks_the_pending_row_superseded_in_batch(db, plaid_card):
    """Both versions in one window: the pending row is shown as superseded so
    the preview does not promise an insert that commit will drop."""
    preview = _card_preview(db, plaid_card, [_posted_tx(), _pending_tx()])
    posted, pending = preview.transactions
    assert pending.superseded_by_posted is True
    assert pending.is_pending is True
    assert posted.superseded_by_posted is False
    assert preview.new_count == 1


def test_unticking_the_posted_row_keeps_the_pending_one(db, plaid_card):
    """The batch pre-pass only covers rows the commit will process. Skipping
    the posted row must not silently drop the pending row the user kept."""
    batch = [_posted_tx(), _pending_tx()]
    result = _card_commit(db, plaid_card, batch, skip_indices={0})
    assert result.transactions_created == 1
    assert result.pending_superseded == 0
    rows = _rows(db, plaid_card)
    assert len(rows) == 1
    assert rows[0].plaid_transaction_id == PENDING_TX
    assert rows[0].pending is True


# -------------------------------------------------------------- checking tests

def _checking_parseresult(db, pm, txns):
    from app.services.match_rules import load_match_rules
    from app.services.plaid_import import to_checking_parseresult

    return to_checking_parseresult(
        txns, pm,
        since=PENDING_DAY, until=POSTED_DAY,
        ending_balance=Decimal("0"),
        rules=load_match_rules(db),
    )


def test_checking_posted_updates_pending_row_in_place(db, plaid_checking):
    """Debit-card purchases come through the checking importer, which carries
    the same guarantee (and flips the sign on the way in)."""
    from app.services.checking_importer import build_checking_preview, commit_checking_import

    commit_checking_import(
        db, filename=f"plaid:{plaid_checking.name}",
        payment_method_id=plaid_checking.id,
        pre_parsed=_checking_parseresult(db, plaid_checking, [_pending_tx()]),
        source_override=ImportSource.PLAID,
    )
    rows = _rows(db, plaid_checking)
    assert len(rows) == 1
    original_id = rows[0].id
    assert rows[0].pending is True
    assert Decimal(rows[0].amount) == CHARGE  # debit stored as a positive charge

    preview = build_checking_preview(
        db, filename=f"plaid:{plaid_checking.name}",
        payment_method_id=plaid_checking.id,
        pre_parsed=_checking_parseresult(db, plaid_checking, [_posted_tx(CHARGE_WITH_TIP)]),
    )
    activity = preview.activities[0]
    assert activity.supersedes_transaction_id == original_id
    assert activity.amount_changed is True
    assert activity.is_duplicate is False
    assert f"#{original_id}" in activity.will_action
    assert "pending" in activity.will_action.lower()

    result = commit_checking_import(
        db, filename=f"plaid:{plaid_checking.name}",
        payment_method_id=plaid_checking.id,
        pre_parsed=_checking_parseresult(db, plaid_checking, [_posted_tx(CHARGE_WITH_TIP)]),
        source_override=ImportSource.PLAID,
    )
    assert result.pending_reconciled == 1
    assert result.transactions_created == 0

    rows = _rows(db, plaid_checking)
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].plaid_transaction_id == POSTED_TX
    assert rows[0].description == POSTED_DESC
    assert rows[0].transaction_date == POSTED_DAY
    assert Decimal(rows[0].amount) == CHARGE_WITH_TIP
    assert rows[0].pending is False


def test_checking_pending_and_posted_in_one_batch_land_once(db, plaid_checking):
    from app.services.checking_importer import build_checking_preview, commit_checking_import

    batch = [_posted_tx(), _pending_tx()]
    preview = build_checking_preview(
        db, filename=f"plaid:{plaid_checking.name}",
        payment_method_id=plaid_checking.id,
        pre_parsed=_checking_parseresult(db, plaid_checking, batch),
    )
    assert preview.activities[1].superseded_by_posted is True
    assert "superseded" in preview.activities[1].will_action.lower()

    result = commit_checking_import(
        db, filename=f"plaid:{plaid_checking.name}",
        payment_method_id=plaid_checking.id,
        pre_parsed=_checking_parseresult(db, plaid_checking, batch),
        source_override=ImportSource.PLAID,
    )
    assert result.transactions_created == 1
    assert result.pending_superseded == 1
    rows = _rows(db, plaid_checking)
    assert len(rows) == 1
    assert rows[0].plaid_transaction_id == POSTED_TX


# ------------------------------------------------------------- review endpoint

def test_review_endpoint_exposes_the_supersede_state(client, db, plaid_card, monkeypatch):
    """End to end through /review, with the Plaid pull replaced: the row the
    UI renders carries the supersede state, so 'replaces pending row N' is
    visible before the user commits."""
    from app.routers import plaid as plaid_router
    from app.services import plaid_import

    _card_commit(db, plaid_card, [_pending_tx()])
    original_id = _rows(db, plaid_card)[0].id

    monkeypatch.setattr(plaid_router, "is_configured", lambda: True)
    monkeypatch.setattr(
        plaid_import,
        "fetch_account_transactions",
        lambda access_token_enc, plaid_account_id, since, until: [_posted_tx(CHARGE_WITH_TIP)],
    )

    r = client.get(f"/api/plaid/accounts/{ACCOUNT_ID}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "card"
    row = body["preview"]["transactions"][0]
    assert row["supersedes_transaction_id"] == original_id
    assert row["amount_changed"] is True
    assert row["is_duplicate"] is False
    assert row["already_imported"] is False


# --------------------------------------------------------------------- Pluggy

def test_pluggy_has_no_pending_link_so_pending_rows_are_dropped():
    """Pluggy's payload carries a `status` of PENDING or POSTED and no field
    linking the posted row to the pending one it replaces, so there is nothing
    to supersede: the adapter drops PENDING rows instead. Pinned here because
    the Plaid work above is what tempts someone to 'fix' Pluggy the same way.
    """
    from app.services import pluggy_import

    pm = SimpleNamespace(name="BR Checking", currency=Currency.BRL)
    txns = [
        {"id": "pluggy-1", "description": "COMPRA PENDENTE", "amount": 30,
         "type": "DEBIT", "date": "2026-08-14", "status": "PENDING"},
        {"id": "pluggy-2", "description": "COMPRA CONFIRMADA", "amount": 30,
         "type": "DEBIT", "date": "2026-08-18", "status": "POSTED"},
    ]
    result = pluggy_import.to_card_parseresult(txns, pm)
    kept = [t.raw.get("pluggy_transaction_id") for t in result.transactions]
    assert kept == ["pluggy-2"]
    assert all("pending_transaction_id" not in (t.raw or {}) for t in result.transactions)
