"""Adapt Plaid transactions into v2's parser output structures so they flow
through the EXISTING import preview/commit (importer.py for cards,
checking_importer.py for checking). No parallel ingest engine — Plaid is just
another source that produces a ParseResult / CheckingParseResult.

Fetch is by date window (since the clean-start anchor) per account via
/transactions/get; the importer's signature dedup makes re-review idempotent,
so no cursor state is needed here. Balances are refreshed separately
(plaid_balances), so checking previews set skip_snapshot=True.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.config import settings
from app.models import PaymentMethod
from app.services.crypto import decrypt
from app.services.parsers.checking import (
    CheckingActivity,
    CheckingClass,
    CheckingParseResult,
    MatchRules,
    classify_description,
)
from app.services.parsers.types import ParsedTransaction, ParseResult
from app.services.plaid_client import get_client

# Plaid personal_finance_category.primary values that are not spending.
_PAYMENT_OR_TRANSFER = {"LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT"}

# Some tap-and-go transit systems post a small pre-authorization per tap and
# later CONSOLIDATE the day's rides into a single real fare. The pre-auths never
# reconcile 1:1 (N pre-auths → 1 consolidated charge, so Plaid sends no
# pending_transaction_id link) and would linger as orphan rows double-counting
# the fare. Any line from the operator below the single-ride fare is treated as
# a pre-auth and dropped; the consolidated charge comes through as its own
# posted transaction.
# Both come from configuration (TRANSIT_PREAUTH_KEYWORD, TRANSIT_FARE_MIN):
# which operator a household rides is deployment data, not code. Unset means
# the filter is off, which is the right default for anyone but its deployer.


def _is_transit_preauth(desc: str, amount: Decimal) -> bool:
    keyword = settings.transit_preauth_keyword.strip().lower()
    if not keyword:
        return False
    return keyword in desc.lower() and abs(amount) < Decimal(settings.transit_fare_min)


def clean_start() -> date:
    return date.fromisoformat(settings.plaid_start_date)


def _pfc_primary(tx: Any) -> str:
    pfc = getattr(tx, "personal_finance_category", None)
    return (getattr(pfc, "primary", None) or "").upper() if pfc else ""


def fetch_account_transactions(
    access_token_enc: str, plaid_account_id: str, since: date, until: date
) -> list[Any]:
    """All transactions for one account in [since, until], paginated."""
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import (
        TransactionsGetRequestOptions,
    )

    client = get_client()
    token = decrypt(access_token_enc)
    out: list[Any] = []
    offset = 0
    while True:
        req = TransactionsGetRequest(
            access_token=token,
            start_date=since,
            end_date=until,
            options=TransactionsGetRequestOptions(
                account_ids=[plaid_account_id], count=500, offset=offset
            ),
        )
        resp = client.transactions_get(req)
        out.extend(resp.transactions)
        total = resp.total_transactions
        if len(out) >= total or not resp.transactions:
            break
        offset = len(out)
    return out


def to_card_parseresult(txns: list[Any], pm: PaymentMethod) -> ParseResult:
    """Plaid card tx → ParseResult. Card sign matches v2 (positive=charge),
    so amount passes through. Payments/refunds-as-transfers go to `payments`
    (not stored; balance comes from Plaid)."""
    transactions: list[ParsedTransaction] = []
    payments: list[ParsedTransaction] = []
    skipped = 0
    for tx in txns:
        amount = Decimal(str(tx.amount))
        desc = (getattr(tx, "name", None) or "").strip() or "(no description)"
        if _is_transit_preauth(desc, amount):
            skipped += 1
            continue
        row = ParsedTransaction(
            transaction_date=tx.date,
            description=desc,
            amount=amount,
            is_payment=_pfc_primary(tx) in _PAYMENT_OR_TRANSFER,
            raw={
                "plaid_transaction_id": tx.transaction_id,
                "pending_transaction_id": getattr(tx, "pending_transaction_id", None),
                "pending": bool(getattr(tx, "pending", False)),
            },
        )
        (payments if row.is_payment else transactions).append(row)
    return ParseResult(
        transactions=transactions, payments=payments, parser="plaid_card", skipped=skipped
    )


def to_checking_parseresult(
    txns: list[Any],
    pm: PaymentMethod,
    *,
    since: date,
    until: date,
    ending_balance: Decimal,
    rules: MatchRules,
) -> CheckingParseResult:
    """Plaid checking tx → CheckingParseResult. Sign is flipped to bank
    convention (+ deposit, - withdrawal). Provisional class via v2's keyword
    classifier, with one Plaid-aware tweak: an unmatched CREDIT defaults to
    EXTRA_INCOME (user rule: "recebida = extra"). skip_snapshot=True since
    balances are refreshed separately."""
    activities: list[CheckingActivity] = []
    for tx in txns:
        # Plaid depository: positive = money out; flip to bank (+ deposit).
        amount = -Decimal(str(tx.amount))
        desc = (getattr(tx, "name", None) or "").strip() or "(no description)"
        cls, hint = classify_description(desc, amount, rules=rules)
        # Plaid's own personal_finance_category beats v2's PDF-oriented keyword
        # tables for Plaid's transaction wording ("Online Transfer / Payment:
        # Debit/Credit"). Only override the generic SPENDING fallback — keep
        # whatever the keyword classifier positively recognized (SALARY,
        # TAX_PAYMENT, CC_PAYMENT, FIXED_MATCH).
        if cls == CheckingClass.SPENDING:
            pfc = _pfc_primary(tx)
            d = desc.lower()
            if pfc == "TRANSFER_IN":
                # ATM cash deposits AND mobile/check deposits both arrive as
                # TRANSFER_IN_DEPOSIT — only the description tells them apart.
                # A mobile/check deposit is external money (refund, deposited
                # check) → extra income; cash/ATM and account transfers are the
                # user moving their own money in → internal transfer.
                if "mobile deposit" in d or ("deposit" in d and "atm" not in d and "cash" not in d):
                    cls = CheckingClass.EXTRA_INCOME
                else:
                    cls = CheckingClass.INTERNAL_TRANSFER
            elif pfc == "TRANSFER_OUT":
                cls = CheckingClass.INTERNAL_TRANSFER
            elif pfc == "LOAN_PAYMENTS":
                cls = CheckingClass.CC_PAYMENT
            elif amount > 0:
                # Genuine unmatched credit (not a Plaid transfer) → extra income
                # ("recebida = extra"). The insert is idempotent per plaid tx.
                cls = CheckingClass.EXTRA_INCOME
        activities.append(
            CheckingActivity(
                activity_date=tx.date,
                description=desc,
                amount=amount,
                running_balance=None,
                classification=cls,
                match_hint=hint,
                raw_lines=[tx.transaction_id],
                plaid_transaction_id=tx.transaction_id,
                pending=bool(getattr(tx, "pending", False)),
                pending_transaction_id=getattr(tx, "pending_transaction_id", None),
            )
        )
    return CheckingParseResult(
        parser="plaid_checking",
        account_name=pm.name,
        period_start=since,
        period_end=until,
        beginning_balance=Decimal("0"),
        ending_balance=ending_balance,
        activities=activities,
        skip_snapshot=True,
    )
