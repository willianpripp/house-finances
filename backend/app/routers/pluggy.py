"""Pluggy connection management.

Registration is by item id, not by list: the Pluggy API deliberately has no
list-items endpoint, so the id arrives either from the in-app Connect widget
callback or pasted manually for connections authorized on meu.pluggy.ai.
Registering validates the id against the API, snapshots connector/status, and
surfaces accounts already mapped under a DIFFERENT item: re-authorization can
create a new item pointing at the same accounts, and identity lives on the
account id, not the item id.

Mapping a payment method here already flips it to Pluggy-fed (manual import
409s, Plaid semantics); Review/Commit per account is the separate flow.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from typing import Any

from app.db import get_db
from app.models import (
    ImportSource,
    PaymentMethod,
    PaymentMethodType,
    PluggyItem,
    PluggySeenTransaction,
    User,
)
from app.services import pluggy_client
from app.services.connections import mapped_payment_method_names
from app.services.match_rules import load_match_rules

router = APIRouter(prefix="/api/pluggy", tags=["pluggy"])


class PluggyItemOut(BaseModel):
    id: int
    item_id: str
    connector_name: str
    status: str
    last_sync_at: str | None
    last_sync_error: str | None
    # Every item reports the same connector name, so this is what tells two
    # connections apart on /connections.
    mapped_payment_methods: list[str] = []


class PluggyAccountOut(BaseModel):
    pluggy_account_id: str
    name: str
    type: str
    subtype: str | None
    currency: str | None
    balance: float | None
    payment_method_id: int | None
    payment_method_name: str | None


class RegisterItemRequest(BaseModel):
    item_id: str
    user_id: int | None = None


class RegisterItemResponse(BaseModel):
    id: int
    connector_name: str
    status: str
    accounts: list[PluggyAccountOut]
    # Account ids under this item that are ALREADY mapped to a payment
    # method through a different item — the re-auth-duplicate trap. Nothing
    # is remapped automatically; the UI shows these for a human decision.
    conflicts: list[str]


def _require_configured() -> None:
    if not pluggy_client.is_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Pluggy credentials not configured. Set PLUGGY_CLIENT_ID and "
                "PLUGGY_CLIENT_SECRET in .env."
            ),
        )


def _accounts_out(db: Session, accounts: list[dict]) -> list[PluggyAccountOut]:
    out: list[PluggyAccountOut] = []
    for acc in accounts:
        pm = db.scalar(
            select(PaymentMethod).where(PaymentMethod.pluggy_account_id == acc["id"])
        )
        out.append(
            PluggyAccountOut(
                pluggy_account_id=acc["id"],
                name=acc.get("name") or "(unnamed)",
                type=acc.get("type") or "",
                subtype=acc.get("subtype"),
                currency=acc.get("currencyCode"),
                balance=acc.get("balance"),
                payment_method_id=pm.id if pm else None,
                payment_method_name=pm.name if pm else None,
            )
        )
    return out


class ConnectTokenRequest(BaseModel):
    # Present = update mode: re-authorize this existing item in the widget
    # instead of creating a duplicate connection (trap #3 prevention).
    item_id: str | None = None


@router.post("/connect_token")
def connect_token(body: ConnectTokenRequest | None = None) -> dict[str, str]:
    """Mint the short-lived token the Connect widget boots from.
    The widget's onSuccess callback returns the item id, which the frontend
    feeds straight into POST /items — same path as a pasted id."""
    _require_configured()
    try:
        token = pluggy_client.create_connect_token(body.item_id if body else None)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"accessToken": token}


@router.get("/items", response_model=list[PluggyItemOut])
def list_items(db: Session = Depends(get_db)) -> list[PluggyItemOut]:
    items = db.scalars(select(PluggyItem).order_by(PluggyItem.created_at.desc())).all()
    mapped = mapped_payment_method_names(db, PaymentMethod.pluggy_item_id)
    return [
        PluggyItemOut(
            id=i.id,
            item_id=i.item_id,
            connector_name=i.connector_name,
            status=i.status,
            last_sync_at=i.last_sync_at.isoformat() if i.last_sync_at else None,
            last_sync_error=i.last_sync_error,
            mapped_payment_methods=mapped.get(i.id, []),
        )
        for i in items
    ]


@router.post("/items", response_model=RegisterItemResponse)
def register_item(body: RegisterItemRequest, db: Session = Depends(get_db)) -> RegisterItemResponse:
    _require_configured()

    existing = db.scalar(select(PluggyItem).where(PluggyItem.item_id == body.item_id))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Item already registered as #{existing.id} ({existing.connector_name}).",
        )

    try:
        info = pluggy_client.get_item(body.item_id)
        accounts = pluggy_client.list_accounts(body.item_id)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if body.user_id is not None:
        user = db.get(User, body.user_id)
    else:
        user = db.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    connector = info.get("connector") or {}
    item = PluggyItem(
        user_id=user.id,
        item_id=body.item_id,
        connector_id=connector.get("id"),
        connector_name=connector.get("name") or "",
        status=info.get("status") or "",
    )
    db.add(item)

    conflicts = [
        acc["id"]
        for acc in accounts
        if db.scalar(
            select(PaymentMethod).where(PaymentMethod.pluggy_account_id == acc["id"])
        )
        is not None
    ]

    db.commit()
    db.refresh(item)
    return RegisterItemResponse(
        id=item.id,
        connector_name=item.connector_name,
        status=item.status,
        accounts=_accounts_out(db, accounts),
        conflicts=conflicts,
    )


@router.get("/items/{item_id}/accounts", response_model=list[PluggyAccountOut])
def list_item_accounts(item_id: int, db: Session = Depends(get_db)) -> list[PluggyAccountOut]:
    _require_configured()
    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    try:
        info = pluggy_client.get_item(item.item_id)
        accounts = pluggy_client.list_accounts(item.item_id)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    item.status = info.get("status") or item.status
    db.commit()
    return _accounts_out(db, accounts)


class MapAccountRequest(BaseModel):
    pluggy_account_id: str
    payment_method_id: int


@router.post("/items/{item_id}/map")
def map_account(item_id: int, body: MapAccountRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    """Mapping is the flip: from this moment the PM is Pluggy-fed and manual
    import 409s for it. Only map after a read-only preview of the account
    was validated against real data."""
    _require_configured()
    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail="PaymentMethod not found")
    if pm.plaid_account_id:
        raise HTTPException(
            status_code=409,
            detail=f"'{pm.name}' is already fed by Plaid — one provider per payment method.",
        )
    other = db.scalar(
        select(PaymentMethod).where(
            PaymentMethod.pluggy_account_id == body.pluggy_account_id,
            PaymentMethod.id != pm.id,
        )
    )
    if other is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Account already mapped to '{other.name}'. Unmap it first.",
        )

    try:
        accounts = pluggy_client.list_accounts(item.item_id)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    acc = next((a for a in accounts if a["id"] == body.pluggy_account_id), None)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found under this item")
    acc_currency = (acc.get("currencyCode") or "").upper()
    if acc_currency and acc_currency != pm.currency.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Currency mismatch: account is {acc_currency}, "
                f"'{pm.name}' is {pm.currency.value}."
            ),
        )

    pm.pluggy_item_id = item.id
    pm.pluggy_account_id = body.pluggy_account_id
    db.commit()
    return {"status": "ok"}


class InvestmentsOut(BaseModel):
    total: float
    currency: str | None
    active_positions: int
    total_positions: int
    payment_method_id: int | None
    payment_method_name: str | None


@router.get("/items/{item_id}/investments", response_model=InvestmentsOut)
def get_investments(item_id: int, db: Session = Depends(get_db)) -> InvestmentsOut:
    """Aggregate of the item's investment positions (separate Pluggy product
    from /accounts) plus the current tracking PM, if any."""
    _require_configured()
    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    try:
        positions = pluggy_client.list_investments(item.item_id)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    active = [p for p in positions if p.get("balance")]
    pm = (
        db.get(PaymentMethod, item.investments_payment_method_id)
        if item.investments_payment_method_id is not None
        else None
    )
    return InvestmentsOut(
        total=float(sum(p["balance"] for p in active)),
        currency=(active[0].get("currencyCode") if active else None),
        active_positions=len(active),
        total_positions=len(positions),
        payment_method_id=pm.id if pm else None,
        payment_method_name=pm.name if pm else None,
    )


class TrackInvestmentsRequest(BaseModel):
    # None = stop tracking (no more snapshots; existing ones stay).
    payment_method_id: int | None = None


@router.post("/items/{item_id}/track-investments")
def track_investments(
    item_id: int, body: TrackInvestmentsRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Point the item's aggregated investments balance at a PM. Balance
    only — investments never produce ledger rows, so this is NOT the manual
    import flip that account mapping is."""
    _require_configured()
    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    if body.payment_method_id is None:
        item.investments_payment_method_id = None
        db.commit()
        return {"status": "ok"}
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail="PaymentMethod not found")
    if pm.plaid_account_id or pm.pluggy_account_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{pm.name}' is already fed by a provider account — investments "
                "tracking needs its own payment method."
            ),
        )
    other = db.scalar(
        select(PluggyItem).where(
            PluggyItem.investments_payment_method_id == pm.id,
            PluggyItem.id != item.id,
        )
    )
    if other is not None:
        raise HTTPException(
            status_code=409,
            detail=f"'{pm.name}' already tracks investments of item #{other.id}.",
        )
    item.investments_payment_method_id = pm.id
    db.commit()
    return {"status": "ok"}


class RefreshBalancesResponse(BaseModel):
    items: int
    refreshed: int
    skipped_unmapped: int
    changes: list[dict]


@router.post("/refresh-balances", response_model=RefreshBalancesResponse)
def refresh_balances(db: Session = Depends(get_db)) -> RefreshBalancesResponse:
    _require_configured()
    from app.services.pluggy_balances import refresh_balances_for_all_items

    totals = refresh_balances_for_all_items(db)
    return RefreshBalancesResponse(**totals)


@router.post("/items/{item_id}/refresh-balances")
def refresh_item_balances(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_configured()
    from app.services.pluggy_balances import refresh_balances_for_item

    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    return refresh_balances_for_item(db, item)


# ---------- Per-account review/commit (feeds v2's existing importers) ----------
# Mirror of the Plaid flow in routers/plaid.py: fetch the window since the
# clean-start anchor, adapt to a ParseResult / CheckingParseResult, and run
# the same preview/commit the manual importers use.


def _resolve_account(db: Session, pluggy_account_id: str) -> tuple[PaymentMethod, PluggyItem]:
    pm = db.scalar(
        select(PaymentMethod).where(PaymentMethod.pluggy_account_id == pluggy_account_id)
    )
    if pm is None:
        raise HTTPException(status_code=404, detail="Account not mapped to a payment method")
    item = db.get(PluggyItem, pm.pluggy_item_id) if pm.pluggy_item_id else None
    if item is None:
        raise HTTPException(status_code=400, detail="Account has no linked Pluggy item")
    return pm, item


def _seen_pluggy_ids(db: Session, ids: list[str]) -> set[str]:
    """Subset of `ids` already handled in a prior review (committed, even if no-op)."""
    ids = [i for i in ids if i]
    if not ids:
        return set()
    rows = db.scalars(
        select(PluggySeenTransaction.pluggy_transaction_id).where(
            PluggySeenTransaction.pluggy_transaction_id.in_(ids)
        )
    ).all()
    return set(rows)


def _record_seen(db: Session, pm_id: int, ids: list[str]) -> None:
    existing = _seen_pluggy_ids(db, ids)
    added = False
    for pid in ids:
        if pid and pid not in existing:
            db.add(PluggySeenTransaction(pluggy_transaction_id=pid, payment_method_id=pm_id))
            existing.add(pid)
            added = True
    if added:
        db.commit()


def _fetch_window(db: Session, pluggy_account_id: str):
    from datetime import date as _date

    from app.services.pluggy_import import clean_start, fetch_account_transactions

    pm, item = _resolve_account(db, pluggy_account_id)
    since, until = clean_start(), _date.today()
    try:
        txns = fetch_account_transactions(pluggy_account_id, since, until)
    except pluggy_client.PluggyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return pm, item, since, until, txns


def _build_pluggy_preview(db: Session, pluggy_account_id: str):
    from app.services.checking_importer import build_checking_preview
    from app.services.importer import build_preview
    from app.services.pluggy_import import to_card_parseresult, to_checking_parseresult

    pm, item, since, until, txns = _fetch_window(db, pluggy_account_id)
    if pm.type == PaymentMethodType.CREDIT_CARD:
        pr = to_card_parseresult(txns, pm)
        preview = build_preview(db, filename=f"pluggy:{pm.name}", payment_method_id=pm.id, pre_parsed=pr)
        return pm, "card", preview, pr
    pr = to_checking_parseresult(txns, pm, since=since, until=until, rules=load_match_rules(db))
    preview = build_checking_preview(db, filename=f"pluggy:{pm.name}", payment_method_id=pm.id, pre_parsed=pr)
    return pm, "checking", preview, pr


@router.get("/accounts/{pluggy_account_id}/review")
def review_account(pluggy_account_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _require_configured()
    pm, kind, preview, pr = _build_pluggy_preview(db, pluggy_account_id)
    if kind == "card":
        pairs = [(r, (pr.transactions[i].raw or {}).get("pluggy_transaction_id"))
                 for i, r in enumerate(preview.transactions)]
    else:
        pairs = [(r, pr.activities[i].pluggy_transaction_id)
                 for i, r in enumerate(preview.activities)]
    seen = _seen_pluggy_ids(db, [pid for _, pid in pairs if pid])
    for r, pid in pairs:
        r.already_imported = bool(getattr(r, "is_duplicate", False) or (pid and pid in seen))
    return {"kind": kind, "payment_method_id": pm.id, "payment_method_name": pm.name, "preview": preview}


class PluggySplitIn(BaseModel):
    index: int
    installments: int
    contract_end_date: str | None = None
    category_id: int | None = None


class PluggyCommitIn(BaseModel):
    skip_indices: list[int] | None = None
    category_overrides: list[int | None] | None = None
    merchant_overrides: list[int | None] | None = None
    new_merchant_names: list[str | None] | None = None
    owner_user_ids: list[int] | None = None
    save_rule_flags: list[bool] | None = None
    save_rule_amount_flags: list[bool] | None = None
    splits: list[PluggySplitIn] | None = None
    cc_payment_overrides: list[int | None] | None = None  # checking: row idx -> card pm id
    save_transfer_rule_flags: list[bool] | None = None  # checking: remember amount→category


@router.post("/accounts/{pluggy_account_id}/commit")
def commit_account(
    pluggy_account_id: str, body: PluggyCommitIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_configured()
    from datetime import date as _date

    from app.services.checking_importer import (
        CheckingContractConversion,
        commit_checking_import,
    )
    from app.services.importer import CardContractConversion, commit_import
    from app.services.pluggy_import import to_card_parseresult, to_checking_parseresult

    pm, item, since, until, txns = _fetch_window(db, pluggy_account_id)
    skip = set(body.skip_indices or [])

    def _end(s: PluggySplitIn):
        return _date.fromisoformat(s.contract_end_date) if s.contract_end_date else None

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
            db, filename=f"pluggy:{pm.name}", payment_method_id=pm.id,
            pre_parsed=pr, source_override=ImportSource.PLUGGY,
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
        # recovery = delete its pluggy_seen_transactions row by hand).
        _record_seen(db, pm.id, [
            (pr.transactions[i].raw or {}).get("pluggy_transaction_id")
            for i in range(len(pr.transactions))
        ])
        return {"kind": "card", "transactions_created": result.transactions_created,
                "duplicates_skipped": result.duplicates_skipped}
    else:
        pr = to_checking_parseresult(txns, pm, since=since, until=until, rules=load_match_rules(db))
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
        commit_checking_import(
            db, filename=f"pluggy:{pm.name}", payment_method_id=pm.id,
            pre_parsed=pr, source_override=ImportSource.PLUGGY,
            skip_indices=skip, contract_conversions=convs,
            category_overrides=cat_overrides,
            merchant_overrides=merch_overrides,
            new_merchant_names=new_merch_names,
            cc_payment_overrides=cc_pay_overrides,
            save_transfer_rule_flags=save_transfer_set,
        )
        # Same dismiss semantics as the card path above.
        _record_seen(db, pm.id, [
            pr.activities[i].pluggy_transaction_id
            for i in range(len(pr.activities))
        ])
        return {"kind": "checking", "result": "ok"}


@router.delete("/items/{item_id}")
def unregister_item(item_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    """Removes the registration and unmaps its payment methods, which
    re-enables manual import for them. The Pluggy-side consent is untouched
    (revoke that on meu.pluggy.ai)."""
    item = db.get(PluggyItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Pluggy item not found")
    for pm in db.scalars(
        select(PaymentMethod).where(PaymentMethod.pluggy_item_id == item.id)
    ):
        pm.pluggy_item_id = None
        pm.pluggy_account_id = None
    db.delete(item)
    db.commit()
    return {"status": "ok"}
