"""Pluggy Chunk 1: item registration, account mapping, and the flip that
blocks manual import. The Pluggy API itself is monkeypatched — these tests
pin OUR behavior (guards, conflict surfacing, mapping semantics), not
Pluggy's."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.models import PaymentMethod, PluggyItem

ITEM_UUID = "a1b2c3d4-0000-0000-0000-000000000001"
ACC_BRL = "acc-brl-0001"
ACC_USD = "acc-usd-0002"


@pytest.fixture
def pluggy_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pluggy_client_id", "test-client-id")
    monkeypatch.setattr(settings, "pluggy_client_secret", "test-secret")


@pytest.fixture
def fake_api(monkeypatch):
    """In-memory Pluggy: one item exposing a BRL checking and a USD account."""
    from app.services import pluggy_client

    item_info = {
        "id": ITEM_UUID,
        "connector": {"id": 612, "name": "Test Bank"},
        "status": "UPDATED",
    }
    accounts = [
        {
            "id": ACC_BRL,
            "name": "Conta Corrente",
            "type": "BANK",
            "subtype": "CHECKING_ACCOUNT",
            "currencyCode": "BRL",
            "balance": 123.45,
        },
        {
            "id": ACC_USD,
            "name": "Global Account",
            "type": "BANK",
            "subtype": "CHECKING_ACCOUNT",
            "currencyCode": "USD",
            "balance": 10.0,
        },
    ]

    def get_item(item_id):
        if item_id != ITEM_UUID:
            raise pluggy_client.PluggyError(f"Pluggy: /items/{item_id} not found (404)")
        return item_info

    def list_accounts(item_id):
        if item_id != ITEM_UUID:
            raise pluggy_client.PluggyError(f"Pluggy: /items/{item_id} not found (404)")
        return accounts

    # Investments: separate product. Churn is the point — zeroed closed
    # positions linger next to the active ones; only the sum matters.
    investments = [
        {"id": "inv-1", "name": "CDB", "type": "FIXED_INCOME", "balance": 0,
         "currencyCode": "BRL"},
        {"id": "inv-2", "name": "CDB", "type": "FIXED_INCOME", "balance": 801.36,
         "currencyCode": "BRL"},
        {"id": "inv-3", "name": "CDB", "type": "FIXED_INCOME", "balance": 104.37,
         "currencyCode": "BRL"},
    ]

    def list_investments(item_id):
        if item_id != ITEM_UUID:
            raise pluggy_client.PluggyError(f"Pluggy: /items/{item_id} not found (404)")
        return investments

    monkeypatch.setattr(pluggy_client, "get_item", get_item)
    monkeypatch.setattr(pluggy_client, "list_accounts", list_accounts)
    monkeypatch.setattr(pluggy_client, "list_investments", list_investments)


@pytest.fixture
def registered_item(client, db, pluggy_configured, fake_api):
    r = client.post("/api/pluggy/items", json={"item_id": ITEM_UUID})
    assert r.status_code == 200, r.text
    yield r.json()
    item = db.scalar(select(PluggyItem).where(PluggyItem.item_id == ITEM_UUID))
    if item is not None:
        client.delete(f"/api/pluggy/items/{item.id}")


def _pm(db, name: str) -> PaymentMethod:
    return db.scalar(select(PaymentMethod).where(PaymentMethod.name == name))


def test_register_requires_configuration(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pluggy_client_id", "")
    monkeypatch.setattr(settings, "pluggy_client_secret", "")
    r = client.post("/api/pluggy/items", json={"item_id": ITEM_UUID})
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_connect_token_requires_configuration(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pluggy_client_id", "")
    monkeypatch.setattr(settings, "pluggy_client_secret", "")
    r = client.post("/api/pluggy/connect_token", json={})
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]


def test_connect_token_minted_and_update_mode_passes_item_id(
    client, pluggy_configured, monkeypatch
):
    from app.services import pluggy_client

    calls = []

    def create_connect_token(item_id=None):
        calls.append(item_id)
        return "ct-token-123"

    monkeypatch.setattr(pluggy_client, "create_connect_token", create_connect_token)

    r = client.post("/api/pluggy/connect_token", json={})
    assert r.status_code == 200
    assert r.json() == {"accessToken": "ct-token-123"}

    r = client.post("/api/pluggy/connect_token", json={"item_id": ITEM_UUID})
    assert r.status_code == 200
    assert calls == [None, ITEM_UUID]


def test_connect_token_api_failure_is_502(client, pluggy_configured, monkeypatch):
    from app.services import pluggy_client

    def create_connect_token(item_id=None):
        raise pluggy_client.PluggyError("Pluggy auth failed: HTTP 401")

    monkeypatch.setattr(pluggy_client, "create_connect_token", create_connect_token)
    r = client.post("/api/pluggy/connect_token", json={})
    assert r.status_code == 502


def test_register_validates_against_api_and_persists(registered_item, db):
    assert registered_item["connector_name"] == "Test Bank"
    assert registered_item["status"] == "UPDATED"
    assert len(registered_item["accounts"]) == 2
    assert registered_item["conflicts"] == []
    assert db.scalar(select(PluggyItem).where(PluggyItem.item_id == ITEM_UUID)) is not None


def test_register_same_item_twice_is_409(registered_item, client):
    r = client.post("/api/pluggy/items", json={"item_id": ITEM_UUID})
    assert r.status_code == 409


def test_register_unknown_item_is_502_and_writes_nothing(
    client, db, pluggy_configured, fake_api
):
    r = client.post("/api/pluggy/items", json={"item_id": "nope"})
    assert r.status_code == 502
    assert db.scalar(select(PluggyItem).where(PluggyItem.item_id == "nope")) is None


def test_item_list_carries_its_mapped_payment_methods(registered_item, client, db):
    """In production every item reports the same connector name (real banks
    connect THROUGH MeuPluggy, never directly), so the mapped payment methods
    are the only thing that tells two connections apart on /connections."""
    item_pk = registered_item["id"]

    listed = next(i for i in client.get("/api/pluggy/items").json() if i["id"] == item_pk)
    assert listed["mapped_payment_methods"] == []

    for account_id, pm_name in ((ACC_BRL, "Foreign Checking"), (ACC_USD, "Main Checking")):
        r = client.post(
            f"/api/pluggy/items/{item_pk}/map",
            json={"pluggy_account_id": account_id, "payment_method_id": _pm(db, pm_name).id},
        )
        assert r.status_code == 200, r.text

    listed = next(i for i in client.get("/api/pluggy/items").json() if i["id"] == item_pk)
    assert listed["mapped_payment_methods"] == ["Foreign Checking", "Main Checking"]


def test_map_rejects_currency_mismatch(registered_item, client, db):
    pm = _pm(db, "Main Checking")  # USD
    r = client.post(
        f"/api/pluggy/items/{registered_item['id']}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
    )
    assert r.status_code == 400
    assert "Currency mismatch" in r.json()["detail"]


def test_map_rejects_plaid_fed_pm(registered_item, client, db):
    pm = _pm(db, "Foreign Checking")
    pm.plaid_account_id = "plaid-acc-x"
    db.commit()
    try:
        r = client.post(
            f"/api/pluggy/items/{registered_item['id']}/map",
            json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
        )
        assert r.status_code == 409
        assert "one provider per payment method" in r.json()["detail"]
    finally:
        pm.plaid_account_id = None
        db.commit()


def test_map_flips_pm_and_blocks_manual_import(registered_item, client, db):
    pm = _pm(db, "Foreign Checking")  # BRL
    r = client.post(
        f"/api/pluggy/items/{registered_item['id']}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
    )
    assert r.status_code == 200, r.text

    db.refresh(pm)
    assert pm.pluggy_account_id == ACC_BRL
    assert pm.pluggy_item_id == registered_item["id"]

    # The flip: manual import 409s from this moment (decision 3, 2026-08-08).
    r = client.post(
        "/api/imports/preview",
        data={"payment_method_id": str(pm.id)},
        files={"file": ("statement.csv", b"Date,Description,Amount\n", "text/csv")},
    )
    assert r.status_code == 409
    assert "Pluggy" in r.json()["detail"]


def test_register_surfaces_reauth_duplicate_conflict(
    registered_item, client, db, monkeypatch
):
    """Trap #3: a second item exposing an already-mapped account id must be
    flagged, not silently accepted."""
    from app.services import pluggy_client

    pm = _pm(db, "Foreign Checking")
    client.post(
        f"/api/pluggy/items/{registered_item['id']}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
    )

    other_uuid = "a1b2c3d4-0000-0000-0000-000000000002"
    monkeypatch.setattr(
        pluggy_client,
        "get_item",
        lambda _id: {"id": other_uuid, "connector": {"id": 612, "name": "Test Bank"}, "status": "UPDATED"},
    )
    monkeypatch.setattr(
        pluggy_client,
        "list_accounts",
        lambda _id: [
            {"id": ACC_BRL, "name": "Conta Corrente", "type": "BANK",
             "subtype": "CHECKING_ACCOUNT", "currencyCode": "BRL", "balance": 123.45}
        ],
    )
    r = client.post("/api/pluggy/items", json={"item_id": other_uuid})
    assert r.status_code == 200
    assert r.json()["conflicts"] == [ACC_BRL]

    item = db.scalar(select(PluggyItem).where(PluggyItem.item_id == other_uuid))
    client.delete(f"/api/pluggy/items/{item.id}")


# ---------- Chunk 2: adapter (pure) ----------

def _tx(id_, desc, amount, type_, *, category="", status="POSTED", date_="2026-08-05"):
    return {
        "id": id_, "description": desc, "amount": amount, "type": type_,
        "category": category, "status": status, "date": f"{date_}T00:00:00.000Z",
    }


def test_checking_adapter_normalizes_sign_from_type_only(db):
    """Trap #1: the raw sign is never trusted — DEBIT is money out and CREDIT
    is money in, whatever sign the connector sent."""
    from decimal import Decimal

    from app.services.parsers.checking import CheckingClass, MatchRules
    from app.services.pluggy_import import to_checking_parseresult

    pm = _pm(db, "Foreign Checking")
    txns = [
        _tx("t1", "PADARIA DO ZE", 50.0, "DEBIT"),          # real-account shape
        _tx("t2", "COMPRA NO DEBITO", -30.0, "DEBIT"),      # sandbox shape, same meaning
        _tx("t3", "TRANSF RECEBIDA PIX", 200.0, "CREDIT"),
        _tx("t4", "PENDENTE QUALQUER", 10.0, "DEBIT", status="PENDING"),
    ]
    pr = to_checking_parseresult(
        txns, pm, since=date(2026, 8, 1), until=date(2026, 8, 8), rules=MatchRules()
    )
    assert pr.skip_snapshot is True
    assert len(pr.activities) == 3  # PENDING dropped: no pending->posted link
    a1, a2, a3 = pr.activities
    assert a1.amount == Decimal("-50")
    assert a2.amount == Decimal("-30")
    assert a3.amount == Decimal("200")
    assert a1.classification == CheckingClass.SPENDING
    assert a3.classification == CheckingClass.EXTRA_INCOME  # unmatched credit
    assert a1.pluggy_transaction_id == "t1"


def test_checking_adapter_same_person_transfer_is_internal(db):
    from app.services.parsers.checking import CheckingClass, MatchRules
    from app.services.pluggy_import import to_checking_parseresult

    pm = _pm(db, "Foreign Checking")
    txns = [_tx("t5", "APLICACAO CDB", 500.0, "DEBIT", category="Same person transfer")]
    pr = to_checking_parseresult(
        txns, pm, since=date(2026, 8, 1), until=date(2026, 8, 8), rules=MatchRules()
    )
    assert pr.activities[0].classification == CheckingClass.INTERNAL_TRANSFER


def test_card_adapter_charges_payments_refunds(db):
    from decimal import Decimal

    from app.services.pluggy_import import to_card_parseresult

    pm = _pm(db, "Foreign Card")
    txns = [
        _tx("c1", "MERCADO LIVRE", 120.0, "DEBIT"),
        _tx("c2", "Pagamento recebido", 800.0, "CREDIT"),
        _tx("c3", "ESTORNO COMPRA", 40.0, "CREDIT"),  # refund stays in the ledger
    ]
    pr = to_card_parseresult(txns, pm)
    assert [t.description for t in pr.transactions] == ["MERCADO LIVRE", "ESTORNO COMPRA"]
    assert pr.transactions[0].amount == Decimal("120")
    assert pr.transactions[1].amount == Decimal("-40")
    assert len(pr.payments) == 1
    assert pr.payments[0].amount == Decimal("-800")
    assert (pr.transactions[0].raw or {})["pluggy_transaction_id"] == "c1"


# ---------- Chunk 2: review/commit endpoints ----------

@pytest.fixture
def mapped_checking(registered_item, client, db):
    pm = _pm(db, "Foreign Checking")
    r = client.post(
        f"/api/pluggy/items/{registered_item['id']}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
    )
    assert r.status_code == 200, r.text
    return pm


def test_review_commit_roundtrip_and_seen_dedupe(
    mapped_checking, client, db, monkeypatch
):
    from app.models import PluggySeenTransaction, Transaction
    from app.services import pluggy_import

    txns = [
        _tx("rc1", "PADARIA DO ZE", 50.0, "DEBIT"),
        _tx("rc2", "APLICACAO CDB", 500.0, "DEBIT", category="Same person transfer"),
    ]
    monkeypatch.setattr(
        pluggy_import, "fetch_account_transactions", lambda *a, **k: txns
    )

    r = client.get(f"/api/pluggy/accounts/{ACC_BRL}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "checking"
    acts = body["preview"]["activities"]
    assert len(acts) == 2
    assert not any(a["already_imported"] for a in acts)

    r = client.post(f"/api/pluggy/accounts/{ACC_BRL}/commit", json={})
    assert r.status_code == 200, r.text

    row = db.scalar(
        select(Transaction).where(Transaction.pluggy_transaction_id == "rc1")
    )
    assert row is not None
    assert float(row.amount) == 50.0  # checking debit -> positive charge
    # No-op rows (internal transfer) land in the seen-set so re-pulls hide them.
    seen = set(db.scalars(select(PluggySeenTransaction.pluggy_transaction_id)))
    assert {"rc1", "rc2"} <= seen

    r = client.get(f"/api/pluggy/accounts/{ACC_BRL}/review")
    assert all(a["already_imported"] for a in r.json()["preview"]["activities"])

    # Second commit is a no-op: the pluggy id guard catches the re-pull.
    r = client.post(f"/api/pluggy/accounts/{ACC_BRL}/commit", json={})
    assert r.status_code == 200
    count = db.scalars(
        select(Transaction).where(Transaction.pluggy_transaction_id == "rc1")
    ).all()
    assert len(count) == 1

    # Cleanup: this fixture world is shared across tests in the session.
    db.delete(row)
    for sid in ("rc1", "rc2"):
        s = db.get(PluggySeenTransaction, sid)
        if s is not None:
            db.delete(s)
    db.commit()


def test_unchecked_rows_are_dismissed_permanently(
    mapped_checking, client, db, monkeypatch
):
    """Uncheck = dismiss (decision 2026-08-14): a skipped row enters the
    seen-set at commit, creates NO transaction, and never resurfaces."""
    from app.models import PluggySeenTransaction, Transaction
    from app.services import pluggy_import

    txns = [
        _tx("dp1", "COMPRA MERCADO", 80.0, "DEBIT"),
        _tx("dp2", "EMPRESTIMO JORDAN", 500.0, "DEBIT"),
    ]
    monkeypatch.setattr(
        pluggy_import, "fetch_account_transactions", lambda *a, **k: txns
    )

    r = client.get(f"/api/pluggy/accounts/{ACC_BRL}/review")
    assert len(r.json()["preview"]["activities"]) == 2

    # Commit with the loan row (index 1) unchecked.
    r = client.post(
        f"/api/pluggy/accounts/{ACC_BRL}/commit", json={"skip_indices": [1]}
    )
    assert r.status_code == 200, r.text

    assert db.scalar(
        select(Transaction).where(Transaction.pluggy_transaction_id == "dp2")
    ) is None
    seen = set(db.scalars(select(PluggySeenTransaction.pluggy_transaction_id)))
    assert "dp2" in seen

    # Re-review: the dismissed row is marked, not offered as new again.
    r = client.get(f"/api/pluggy/accounts/{ACC_BRL}/review")
    acts = r.json()["preview"]["activities"]
    assert all(a["already_imported"] for a in acts)

    # Cleanup.
    row = db.scalar(
        select(Transaction).where(Transaction.pluggy_transaction_id == "dp1")
    )
    if row is not None:
        db.delete(row)
    for sid in ("dp1", "dp2"):
        s = db.get(PluggySeenTransaction, sid)
        if s is not None:
            db.delete(s)
    db.commit()


def test_review_unmapped_account_is_404(client, pluggy_configured):
    r = client.get("/api/pluggy/accounts/never-mapped/review")
    assert r.status_code == 404


# ---------- Chunk 2: balances ----------

def test_balance_refresh_writes_savings_snapshot(
    mapped_checking, client, db, monkeypatch
):
    from app.models import SavingsSnapshot

    r = client.post("/api/pluggy/refresh-balances")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == 1
    assert body["refreshed"] == 1  # ACC_BRL is mapped; ACC_USD is skipped
    assert body["skipped_unmapped"] == 1

    snap = db.scalars(
        select(SavingsSnapshot).where(SavingsSnapshot.account_name == mapped_checking.name)
    ).all()
    assert any(float(s.balance) == 123.45 for s in snap)
    for s in snap:
        if float(s.balance) == 123.45:
            db.delete(s)
    db.commit()


# ---------- Chunk 4: investments ----------

def test_investments_aggregate_counts_and_sum(registered_item, client):
    r = client.get(f"/api/pluggy/items/{registered_item['id']}/investments")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == pytest.approx(905.73)
    assert body["active_positions"] == 2
    assert body["total_positions"] == 3
    assert body["payment_method_id"] is None


def test_track_investments_guards_and_roundtrip(registered_item, client, db):
    item_pk = registered_item["id"]
    inv_pm = PaymentMethod(
        name="RDB Test", type=_pm(db, "Foreign Checking").type,
        currency=_pm(db, "Foreign Checking").currency,
    )
    db.add(inv_pm)
    db.commit()

    # Guard: a provider-fed PM cannot double as the investments PM.
    fed = _pm(db, "Foreign Checking")
    client.post(
        f"/api/pluggy/items/{item_pk}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": fed.id},
    )
    r = client.post(
        f"/api/pluggy/items/{item_pk}/track-investments",
        json={"payment_method_id": fed.id},
    )
    assert r.status_code == 409

    # Roundtrip: track, visible in GET, untrack.
    r = client.post(
        f"/api/pluggy/items/{item_pk}/track-investments",
        json={"payment_method_id": inv_pm.id},
    )
    assert r.status_code == 200, r.text
    body = client.get(f"/api/pluggy/items/{item_pk}/investments").json()
    assert body["payment_method_id"] == inv_pm.id

    r = client.post(
        f"/api/pluggy/items/{item_pk}/track-investments", json={"payment_method_id": None}
    )
    assert r.status_code == 200
    body = client.get(f"/api/pluggy/items/{item_pk}/investments").json()
    assert body["payment_method_id"] is None

    db.delete(inv_pm)
    db.commit()


def test_balance_refresh_writes_investments_snapshot(registered_item, client, db):
    from app.models import SavingsSnapshot

    inv_pm = PaymentMethod(
        name="RDB Snapshot Test", type=_pm(db, "Foreign Checking").type,
        currency=_pm(db, "Foreign Checking").currency,
    )
    db.add(inv_pm)
    db.commit()
    r = client.post(
        f"/api/pluggy/items/{registered_item['id']}/track-investments",
        json={"payment_method_id": inv_pm.id},
    )
    assert r.status_code == 200, r.text

    r = client.post(f"/api/pluggy/items/{registered_item['id']}/refresh-balances")
    assert r.status_code == 200, r.text

    snaps = db.scalars(
        select(SavingsSnapshot).where(SavingsSnapshot.account_name == inv_pm.name)
    ).all()
    assert len(snaps) == 1
    assert float(snaps[0].balance) == pytest.approx(905.73)

    for s in snaps:
        db.delete(s)
    db.delete(inv_pm)
    db.commit()


def test_unregister_unmaps_and_reenables_manual_import(
    client, db, pluggy_configured, fake_api
):
    r = client.post("/api/pluggy/items", json={"item_id": ITEM_UUID})
    item_pk = r.json()["id"]
    pm = _pm(db, "Foreign Checking")
    client.post(
        f"/api/pluggy/items/{item_pk}/map",
        json={"pluggy_account_id": ACC_BRL, "payment_method_id": pm.id},
    )

    r = client.delete(f"/api/pluggy/items/{item_pk}")
    assert r.status_code == 200

    db.expire_all()
    pm = _pm(db, "Foreign Checking")
    assert pm.pluggy_account_id is None
    assert pm.pluggy_item_id is None
    assert db.scalar(select(PluggyItem).where(PluggyItem.item_id == ITEM_UUID)) is None
