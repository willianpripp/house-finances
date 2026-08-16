"""Plaid Link + sync endpoints (v2.5).

Flow:
1. Frontend → POST /api/plaid/link_token → returns short-lived link_token.
2. User logs in via Plaid Link widget → frontend gets public_token.
3. Frontend → POST /api/plaid/exchange { public_token } → backend exchanges
   for long-lived access_token, persists encrypted PlaidItem.
4. Frontend → POST /api/plaid/sync → triggers pull_all_items().
5. Frontend → GET /api/plaid/items → lists connected items + their status.

To add an account opened *after* a bank was first connected, POST
/api/plaid/link_token with `{item_id}` — that returns an update-mode token
which reopens Link on the existing Item with the account picker enabled. The
access_token does not change, so there is no /exchange step afterwards.

Ingestion writes into v2's transactions/balances tables (see plaid_sync /
plaid_balances).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.connections import mapped_payment_method_names
from app.services.match_rules import load_match_rules
from app.models import ImportSource, PaymentMethod, PaymentMethodType, PlaidItem, PlaidItemStatus, User
from app.services.crypto import encrypt
from app.services.plaid_client import get_client, is_configured
from app.services.plaid_balances import refresh_balances_for_all_items


router = APIRouter(prefix="/api/plaid", tags=["plaid"])


class LinkTokenResponse(BaseModel):
    link_token: str


class LinkTokenRequest(BaseModel):
    # When set, the token opens Link in *update mode* for that existing Item
    # instead of connecting a new bank. Needed because an Item's account set is
    # frozen at Link time: a card opened after the original connection is not
    # in `accounts_get` until the user re-consents with account selection.
    item_id: int | None = None


@router.post("/link_token", response_model=LinkTokenResponse)
def create_link_token(
    body: LinkTokenRequest | None = None,
    db: Session = Depends(get_db),
) -> LinkTokenResponse:
    if not is_configured():
        raise HTTPException(
            status_code=400,
            detail="Plaid credentials not configured. Set PLAID_CLIENT_ID and PLAID_SECRET in .env.",
        )

    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_update import LinkTokenCreateRequestUpdate
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.products import Products
    from plaid.model.country_code import CountryCode

    from app.services.crypto import decrypt as _decrypt

    user = db.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise HTTPException(status_code=500, detail="No user in DB.")

    item = None
    if body is not None and body.item_id is not None:
        item = db.get(PlaidItem, body.item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Plaid Item not found")

    client = get_client()
    kwargs: dict[str, Any] = dict(
        client_name="House Finances v2.5",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id=str(user.id)),
    )
    if item is not None:
        # Update mode: `products` must be omitted, and account_selection_enabled
        # forces the account-picker pane so newly opened accounts can be added.
        kwargs["access_token"] = _decrypt(item.access_token)
        kwargs["update"] = LinkTokenCreateRequestUpdate(account_selection_enabled=True)
    else:
        kwargs["products"] = [Products("transactions")]

    resp = client.link_token_create(LinkTokenCreateRequest(**kwargs))
    return LinkTokenResponse(link_token=resp.link_token)


class ExchangeRequest(BaseModel):
    public_token: str


class ExchangeResponse(BaseModel):
    item_id: int
    institution_name: str
    accounts_discovered: int


@router.post("/exchange", response_model=ExchangeResponse)
def exchange_public_token(body: ExchangeRequest, db: Session = Depends(get_db)) -> ExchangeResponse:
    if not is_configured():
        raise HTTPException(status_code=400, detail="Plaid credentials not configured.")

    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.accounts_get_request import AccountsGetRequest
    from plaid.model.item_get_request import ItemGetRequest
    from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
    from plaid.model.country_code import CountryCode

    client = get_client()
    exchange_resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token)
    )
    access_token = exchange_resp.access_token
    plaid_item_id_str = exchange_resp.item_id

    item_get_resp = client.item_get(ItemGetRequest(access_token=access_token))
    institution_id = item_get_resp.item.institution_id
    inst_resp = client.institutions_get_by_id(
        InstitutionsGetByIdRequest(
            institution_id=institution_id,
            country_codes=[CountryCode("US")],
        )
    )
    institution_name = inst_resp.institution.name

    user = db.scalar(select(User).order_by(User.id).limit(1))
    assert user is not None

    item = PlaidItem(
        user_id=user.id,
        item_id=plaid_item_id_str,
        institution_id=institution_id,
        institution_name=institution_name,
        access_token=encrypt(access_token),
        status=PlaidItemStatus.ACTIVE,
    )
    db.add(item)
    db.flush()

    accounts_resp = client.accounts_get(AccountsGetRequest(access_token=access_token))
    accounts_discovered = len(accounts_resp.accounts)
    db.commit()

    return ExchangeResponse(
        item_id=item.id,
        institution_name=institution_name,
        accounts_discovered=accounts_discovered,
    )


class RefreshBalancesResponse(BaseModel):
    items: int
    refreshed: int
    skipped_unmapped: int = 0
    changes: list[dict] = []


@router.post("/refresh-balances", response_model=RefreshBalancesResponse)
def trigger_refresh_balances(db: Session = Depends(get_db)) -> RefreshBalancesResponse:
    """Refresh bank-reported balances for all items (snapshots only; no
    transactions). Transactions are pulled via the per-account review flow."""
    if not is_configured():
        raise HTTPException(status_code=400, detail="Plaid credentials not configured.")
    return RefreshBalancesResponse(**refresh_balances_for_all_items(db))


class PlaidItemOut(BaseModel):
    id: int
    institution_name: str
    status: str
    last_sync_at: str | None
    last_skipped_unmapped: int = 0
    last_sync_error: str | None = None
    # Two Items at the same institution share an institution_name; this is
    # what tells them apart on /connections.
    mapped_payment_methods: list[str] = []


class PlaidAccountOut(BaseModel):
    plaid_account_id: str
    name: str
    type: str
    subtype: str | None
    mask: str | None
    payment_method_id: int | None
    payment_method_name: str | None


@router.get("/items", response_model=list[PlaidItemOut])
def list_items(db: Session = Depends(get_db)) -> list[PlaidItemOut]:
    items = db.scalars(select(PlaidItem).order_by(PlaidItem.created_at.desc())).all()
    mapped = mapped_payment_method_names(db, PaymentMethod.plaid_item_id)
    return [
        PlaidItemOut(
            id=i.id,
            institution_name=i.institution_name,
            status=i.status.value,
            last_sync_at=i.last_sync_at.isoformat() if i.last_sync_at else None,
            last_skipped_unmapped=i.last_skipped_unmapped,
            last_sync_error=i.last_sync_error,
            mapped_payment_methods=mapped.get(i.id, []),
        )
        for i in items
    ]


@router.post("/items/{item_id}/refresh-balances")
def refresh_item_balances(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.plaid_balances import refresh_balances_for_item

    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plaid Item not found")
    return refresh_balances_for_item(db, item)


@router.post("/items/{item_id}/reset-cursor")
def reset_item_cursor(item_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    """Clear the cursor so the next sync re-pulls every tx Plaid has.
    Use after mapping previously-unmapped accounts."""
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plaid Item not found")
    item.last_cursor = None
    item.last_skipped_unmapped = 0
    db.commit()
    return {"status": "ok"}


@router.get("/items/{item_id}/accounts", response_model=list[PlaidAccountOut])
def list_item_accounts(item_id: int, db: Session = Depends(get_db)) -> list[PlaidAccountOut]:
    from plaid.model.accounts_get_request import AccountsGetRequest
    from app.services.crypto import decrypt as _decrypt

    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plaid Item not found")

    client = get_client()
    resp = client.accounts_get(AccountsGetRequest(access_token=_decrypt(item.access_token)))
    out: list[PlaidAccountOut] = []
    for acc in resp.accounts:
        pm = db.scalar(
            select(PaymentMethod).where(PaymentMethod.plaid_account_id == acc.account_id)
        )
        out.append(
            PlaidAccountOut(
                plaid_account_id=acc.account_id,
                name=acc.name,
                type=str(acc.type),
                subtype=str(acc.subtype) if acc.subtype else None,
                mask=acc.mask,
                payment_method_id=pm.id if pm else None,
                payment_method_name=pm.name if pm else None,
            )
        )
    return out


class MapAccountRequest(BaseModel):
    plaid_account_id: str
    payment_method_id: int


@router.post("/items/{item_id}/map")
def map_account(item_id: int, body: MapAccountRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Plaid Item not found")
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail="PaymentMethod not found")
    pm.plaid_item_id = item.id
    pm.plaid_account_id = body.plaid_account_id
    db.commit()
    return {"status": "ok"}


# ---------- Per-account review/commit (feeds v2's existing importers) ----------

def _resolve_account(db: Session, plaid_account_id: str) -> tuple[PaymentMethod, PlaidItem]:
    pm = db.scalar(
        select(PaymentMethod).where(PaymentMethod.plaid_account_id == plaid_account_id)
    )
    if pm is None:
        raise HTTPException(status_code=404, detail="Account not mapped to a payment method")
    item = db.get(PlaidItem, pm.plaid_item_id) if pm.plaid_item_id else None
    if item is None:
        raise HTTPException(status_code=400, detail="Account has no linked Plaid item")
    return pm, item


def _seen_plaid_ids(db: Session, ids: list[str]) -> set[str]:
    """Subset of `ids` already handled in a prior review (committed, even if no-op)."""
    ids = [i for i in ids if i]
    if not ids:
        return set()
    from app.models import PlaidSeenTransaction
    rows = db.scalars(
        select(PlaidSeenTransaction.plaid_transaction_id).where(
            PlaidSeenTransaction.plaid_transaction_id.in_(ids)
        )
    ).all()
    return set(rows)


def _record_seen(db: Session, pm_id: int, ids: list[str]) -> None:
    """Mark these Plaid tx ids as handled so future re-pulls hide them."""
    from app.models import PlaidSeenTransaction
    existing = _seen_plaid_ids(db, ids)
    added = False
    for pid in ids:
        if pid and pid not in existing:
            db.add(PlaidSeenTransaction(plaid_transaction_id=pid, payment_method_id=pm_id))
            existing.add(pid)
            added = True
    if added:
        db.commit()


def _build_plaid_preview(db: Session, plaid_account_id: str):
    """Fetch the account's window and build the matching v2 preview."""
    from datetime import date
    from decimal import Decimal
    from app.services.plaid_import import (
        clean_start, fetch_account_transactions, to_card_parseresult, to_checking_parseresult,
    )
    from app.services.importer import build_preview
    from app.services.checking_importer import build_checking_preview

    pm, item = _resolve_account(db, plaid_account_id)
    since, until = clean_start(), date.today()
    txns = fetch_account_transactions(item.access_token, plaid_account_id, since, until)
    if pm.type == PaymentMethodType.CREDIT_CARD:
        pr = to_card_parseresult(txns, pm)
        preview = build_preview(db, filename=f"plaid:{pm.name}", payment_method_id=pm.id, pre_parsed=pr)
        return pm, "card", preview, pr
    pr = to_checking_parseresult(txns, pm, since=since, until=until, ending_balance=Decimal("0"), rules=load_match_rules(db))
    preview = build_checking_preview(db, filename=f"plaid:{pm.name}", payment_method_id=pm.id, pre_parsed=pr)
    return pm, "checking", preview, pr


@router.get("/accounts/{plaid_account_id}/review")
def review_account(plaid_account_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not is_configured():
        raise HTTPException(status_code=400, detail="Plaid credentials not configured.")
    pm, kind, preview, pr = _build_plaid_preview(db, plaid_account_id)
    # Flag rows already handled in a prior review (in transactions, or in the
    # seen-set for no-op rows that leave no ledger trace). The UI hides these
    # by default so a re-pull surfaces only genuinely new activity.
    if kind == "card":
        pairs = [(r, (pr.transactions[i].raw or {}).get("plaid_transaction_id"))
                 for i, r in enumerate(preview.transactions)]
    else:
        pairs = [(r, pr.activities[i].plaid_transaction_id)
                 for i, r in enumerate(preview.activities)]
    seen = _seen_plaid_ids(db, [pid for _, pid in pairs if pid])
    for r, pid in pairs:
        r.already_imported = bool(getattr(r, "is_duplicate", False) or (pid and pid in seen))
    return {"kind": kind, "payment_method_id": pm.id, "payment_method_name": pm.name, "preview": preview}


class PlaidSplitIn(BaseModel):
    index: int
    installments: int
    contract_end_date: str | None = None
    category_id: int | None = None


class PlaidCommitIn(BaseModel):
    skip_indices: list[int] | None = None
    category_overrides: list[int | None] | None = None
    merchant_overrides: list[int | None] | None = None
    new_merchant_names: list[str | None] | None = None
    owner_user_ids: list[int] | None = None
    save_rule_flags: list[bool] | None = None
    save_rule_amount_flags: list[bool] | None = None  # scope rule to the row's amount
    splits: list[PlaidSplitIn] | None = None
    cc_payment_overrides: list[int | None] | None = None  # checking: row idx -> card pm id
    save_transfer_rule_flags: list[bool] | None = None  # checking: remember amount→category


@router.post("/accounts/{plaid_account_id}/commit")
def commit_account(
    plaid_account_id: str, body: PlaidCommitIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    if not is_configured():
        raise HTTPException(status_code=400, detail="Plaid credentials not configured.")
    from datetime import date
    from decimal import Decimal
    from app.services.plaid_import import (
        clean_start, fetch_account_transactions, to_card_parseresult, to_checking_parseresult,
    )
    from app.services.importer import commit_import, CardContractConversion
    from app.services.checking_importer import commit_checking_import, CheckingContractConversion

    pm, item = _resolve_account(db, plaid_account_id)
    since, until = clean_start(), date.today()
    txns = fetch_account_transactions(item.access_token, plaid_account_id, since, until)
    skip = set(body.skip_indices or [])

    def _end(s: PlaidSplitIn):
        return date.fromisoformat(s.contract_end_date) if s.contract_end_date else None

    if pm.type == PaymentMethodType.CREDIT_CARD:
        pr = to_card_parseresult(txns, pm)
        convs = {
            s.index: CardContractConversion(
                index=s.index, installments=s.installments,
                contract_end_date=_end(s), category_id=s.category_id,
            )
            for s in (body.splits or [])
        }
        result = commit_import(
            db, filename=f"plaid:{pm.name}", payment_method_id=pm.id,
            pre_parsed=pr, source_override=ImportSource.PLAID,
            skip_indices=skip,
            category_overrides=body.category_overrides,
            merchant_overrides=body.merchant_overrides,
            new_merchant_names=body.new_merchant_names,
            owner_user_ids=body.owner_user_ids,
            save_rule_flags=body.save_rule_flags,
            save_rule_amount_flags=body.save_rule_amount_flags,
            contract_conversions=convs,
        )
        # ALL reviewed ids enter the seen-set, including unchecked rows:
        # uncheck = dismiss, the row never resurfaces (decision 2026-08-14;
        # recovery = delete its plaid_seen_transactions row by hand).
        _record_seen(db, pm.id, [
            (pr.transactions[i].raw or {}).get("plaid_transaction_id")
            for i in range(len(pr.transactions))
        ])
        return {"kind": "card", "transactions_created": result.transactions_created,
                "duplicates_skipped": result.duplicates_skipped}
    else:
        pr = to_checking_parseresult(txns, pm, since=since, until=until, ending_balance=Decimal("0"), rules=load_match_rules(db))
        convs = {
            s.index: CheckingContractConversion(
                index=s.index, installments=s.installments,
                contract_end_date=_end(s), category_id=s.category_id,
            )
            for s in (body.splits or [])
        }
        cat_overrides = {
            i: c for i, c in enumerate(body.category_overrides or []) if c is not None
        }
        merch_overrides = {
            i: c for i, c in enumerate(body.merchant_overrides or []) if c is not None
        }
        new_merch_names = {
            i: n for i, n in enumerate(body.new_merchant_names or []) if n
        }
        cc_pay_overrides = {
            i: c for i, c in enumerate(body.cc_payment_overrides or []) if c is not None
        }
        save_transfer_set = {
            i for i, f in enumerate(body.save_transfer_rule_flags or []) if f
        }
        result = commit_checking_import(
            db, filename=f"plaid:{pm.name}", payment_method_id=pm.id,
            pre_parsed=pr, source_override=ImportSource.PLAID,
            skip_indices=skip, contract_conversions=convs,
            category_overrides=cat_overrides,
            merchant_overrides=merch_overrides,
            new_merchant_names=new_merch_names,
            cc_payment_overrides=cc_pay_overrides,
            save_transfer_rule_flags=save_transfer_set,
        )
        # Same dismiss semantics as the card path above.
        _record_seen(db, pm.id, [
            pr.activities[i].plaid_transaction_id
            for i in range(len(pr.activities))
        ])
        return {"kind": "checking", "result": "ok"}
