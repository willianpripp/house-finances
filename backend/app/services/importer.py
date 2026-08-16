"""Statement import flow: parse -> categorize -> preview -> commit.

The preview phase is read-only: it never mutates the DB. Commit creates an
import_log, the transactions, any new merchants.

PAYMENT/AUTOPAY rows parsed from card statements are surfaced in the
preview for visual reconciliation, but they DO NOT write to
`credit_card_balances` — the checking importer (PDF or manual paste) is
the single source of truth for balance reductions, so that a card balance
only ever drops when money actually left a tracked account.
"""
from __future__ import annotations

import re as _re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CategorizationRule,
    Category,
    CategoryType,
    Currency,
    ImportLog,
    ImportSource,
    Merchant,
    PaymentMethod,
    RecurrenceKind,
    Transaction,
    User,
)
from app.services.categorizer import Categorizer
from app.services.match_rules import load_match_rules
from app.services.parsers import detect, run_cc_parser
from app.services.parsers.types import ParseResult
from app.services.recurrence import (
    amount_matches_prior,
    find_prior_recurring,
    propagation_for_new_row,
)

DEFAULT_OWNER_USER_ID = 1


class TransactionPreviewRow(BaseModel):
    transaction_date: date
    description: str
    amount: Decimal
    merchant_name: str
    category_name: str
    matched_keyword: str | None
    is_duplicate: bool
    is_new_merchant: bool
    owner_user_id: int
    in_intra_file_group: bool  # multiple rows in this file share date+merchant+amount
    is_pending: bool = False   # Plaid pending (provisional) — amount may change when it posts
    already_imported: bool = False  # handled in a prior review (in transactions or seen-set)


class PaymentPreviewRow(BaseModel):
    transaction_date: date
    description: str
    amount: Decimal


class ImportPreview(BaseModel):
    parser: str
    payment_method_id: int
    payment_method_name: str
    currency: Currency
    filename: str

    transactions: list[TransactionPreviewRow]
    payments: list[PaymentPreviewRow]

    new_count: int
    duplicate_count: int
    skipped_count: int
    new_merchants: list[str]


class ImportCommitResult(BaseModel):
    import_log_id: int
    transactions_created: int
    duplicates_skipped: int
    new_merchants_created: int
    rules_created: int = 0
    rules_skipped: list[str] = []  # human-readable reason per skipped rule


# Recognise installment markers in the parsed description so the importer can
# pre-fill installment_current / installment_total without manual editing.
# Each pattern requires an explicit context cue so bare dates like "03/27"
# (the MM/DD posting date some statements print) aren't mis-parsed as
# installment 3/27.
#   "Parcela X/Y" / "Parcela: X/Y"          — explicit BR/PT marker
#   "(X/Y)"                                  — parenthesized
#   "X de Y"                                 — written Portuguese
_INSTALLMENT_PATTERNS = (
    _re.compile(r"\bparcela\s*:?\s*(\d{1,2})\s*/\s*(\d{1,3})\b", _re.IGNORECASE),
    _re.compile(r"\((\d{1,2})\s*/\s*(\d{1,3})\)"),
    _re.compile(r"\b(\d{1,2})\s+de\s+(\d{1,3})\b", _re.IGNORECASE),
)


_KEYWORD_TOKEN_PATTERN = _re.compile(r"[A-Za-z]{3,}")


def derive_rule_keyword(description: str) -> str | None:
    """Best-effort keyword from a raw description for auto-rule creation.

    Strategy: lowercase the description, pull the first two alphabetic tokens
    of length >= 3 (skipping pure-digit chunks like card numbers, dates,
    transaction IDs). Returns None when nothing usable is found.

    The categorizer matches via case-insensitive substring, so "athentic
    brewing" will hit any description containing those two words in order.
    """
    if not description:
        return None
    tokens = _KEYWORD_TOKEN_PATTERN.findall(description.lower())
    if not tokens:
        return None
    return " ".join(tokens[:2])


def _detect_installment(desc: str) -> tuple[int, int] | None:
    """Return (current, total) if the description carries an installment marker.
    Total must be between 2 and 60 and current <= total. Returns None for 1/1
    or values out of range."""
    for pat in _INSTALLMENT_PATTERNS:
        for m in pat.finditer(desc):
            try:
                cur, tot = int(m.group(1)), int(m.group(2))
            except ValueError:
                continue
            if 1 <= cur <= tot and 2 <= tot <= 60:
                return cur, tot
    return None


def _existing_owner_ids(
    session: Session,
    *,
    txn_date: date,
    amount: Decimal,
    payment_method_id: int,
    merchant_id: int | None = None,
) -> set[int]:
    """Owner ids of DB rows that should treat this incoming row as a duplicate.

    Two passes, results merged:

    1. Exact match on (date, amount, payment_method). Catches re-imports of
       the same statement file even when categorization rules changed.
    2. FIXED-category match on (same calendar month, amount, payment_method,
       MERCHANT). Catches the rolled-vs-statement pattern: a placeholder
       created by the monthly rollover lands on day 1, and the actual fatura
       charge lands on day 14 — same recurring bill (same merchant) but the day
       differs. The merchant match is essential: without it a coincidental
       same-amount charge (e.g. a $2.50 transit fare) false-positives against an
       unrelated FIXED bill of the same amount (Google Services $2.50) and gets
       silently dropped. Only runs when the incoming merchant is known (a brand
       new merchant has no prior FIXED bill to match).
    """
    import calendar as _cal
    owners: set[int] = set()
    exact = session.execute(
        select(Transaction.created_by_user_id).filter_by(
            transaction_date=txn_date,
            amount=amount,
            payment_method_id=payment_method_id,
        )
    ).all()
    owners.update(r[0] for r in exact if r[0] is not None)

    if merchant_id is not None:
        month_start = txn_date.replace(day=1)
        month_end = txn_date.replace(day=_cal.monthrange(txn_date.year, txn_date.month)[1])
        fixed = session.execute(
            select(Transaction.created_by_user_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.payment_method_id == payment_method_id,
                Transaction.merchant_id == merchant_id,
                Transaction.amount == amount,
                Transaction.transaction_date.between(month_start, month_end),
                Category.type == CategoryType.FIXED,
            )
        ).all()
        owners.update(r[0] for r in fixed if r[0] is not None)
    return owners


def _plaid_id_exists(session: Session, plaid_transaction_id: str | None) -> bool:
    """A row already carries this Plaid transaction id. Catches re-review even
    when a split changed the stored amount (the bank line is $70, the stored
    installment is $11.67), which the amount-based signature dedup would miss."""
    if not plaid_transaction_id:
        return False
    return session.scalar(
        select(Transaction.id).where(
            Transaction.plaid_transaction_id == plaid_transaction_id
        ).limit(1)
    ) is not None


def _pluggy_id_exists(session: Session, pluggy_transaction_id: str | None) -> bool:
    """Pluggy mirror of _plaid_id_exists. Only guards re-pulls of the Pluggy
    source; cross-source duplicates are the signature dedupe's job."""
    if not pluggy_transaction_id:
        return False
    return session.scalar(
        select(Transaction.id).where(
            Transaction.pluggy_transaction_id == pluggy_transaction_id
        ).limit(1)
    ) is not None


def _list_user_ids(session: Session) -> list[int]:
    return list(session.scalars(select(User.id).order_by(User.id)).all())


def build_preview(
    session: Session,
    *,
    file_content: bytes | None = None,
    filename: str,
    payment_method_id: int,
    default_owner_user_id: int = DEFAULT_OWNER_USER_ID,
    pre_parsed: ParseResult | None = None,
) -> ImportPreview:
    if pre_parsed is None:
        if file_content is None:
            raise ValueError("file_content is required when pre_parsed is not provided")
        detected = detect(filename)
        if detected is None:
            raise ValueError(f"No parser matches filename '{filename}'")
        _, parse_fn = detected
        parse_result = run_cc_parser(
            parse_fn, file_content, load_match_rules(session).holder_names
        )
    else:
        parse_result = pre_parsed

    pm = session.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payment_method_id} not found")
    categorizer = Categorizer(session)
    user_ids = _list_user_ids(session) or [default_owner_user_id]

    # Pre-compute intra-file grouping so we can suggest distinct owners.
    intra_groups: dict[tuple[date, str, Decimal], list[int]] = {}
    classifications = []
    for idx, parsed in enumerate(parse_result.transactions):
        match = categorizer.classify(parsed.description, parsed.amount)
        classifications.append(match)
        key = (parsed.transaction_date, match.merchant_name, parsed.amount)
        intra_groups.setdefault(key, []).append(idx)

    rows: list[TransactionPreviewRow] = []
    new_merchants: set[str] = set()
    duplicate_count = 0

    for idx, parsed in enumerate(parse_result.transactions):
        match = classifications[idx]
        key = (parsed.transaction_date, match.merchant_name, parsed.amount)
        group = intra_groups[key]
        position_in_group = group.index(idx)
        in_group = len(group) > 1

        existing_owners = _existing_owner_ids(
            session,
            txn_date=parsed.transaction_date,
            amount=parsed.amount,
            payment_method_id=payment_method_id,
            merchant_id=match.merchant_id,
        )

        # Owner suggestion:
        # - Default owner unless this is a 2nd+ row of an intra-file group, in
        #   which case rotate through users to avoid collisions with earlier
        #   siblings and DB rows.
        # - Single rows that already exist in DB for the default owner are
        #   flagged DUP — don't try to "fix" a re-import by rotating.
        if in_group and position_in_group > 0:
            used_owners = set(existing_owners)
            for sibling_idx in group[:position_in_group]:
                used_owners.add(rows[sibling_idx].owner_user_id)
            suggested_owner = default_owner_user_id
            if suggested_owner in used_owners:
                for uid in user_ids:
                    if uid not in used_owners:
                        suggested_owner = uid
                        break
            is_dup = suggested_owner in used_owners
        else:
            suggested_owner = default_owner_user_id
            is_dup = suggested_owner in existing_owners

        if not is_dup and (
            _plaid_id_exists(session, (parsed.raw or {}).get("plaid_transaction_id"))
            or _pluggy_id_exists(session, (parsed.raw or {}).get("pluggy_transaction_id"))
        ):
            is_dup = True

        if is_dup:
            duplicate_count += 1

        is_new_merchant = match.merchant_id is None
        if is_new_merchant:
            new_merchants.add(match.merchant_name)

        rows.append(TransactionPreviewRow(
            transaction_date=parsed.transaction_date,
            description=parsed.description,
            amount=parsed.amount,
            merchant_name=match.merchant_name,
            category_name=match.category_name,
            matched_keyword=match.keyword,
            is_duplicate=is_dup,
            is_new_merchant=is_new_merchant,
            owner_user_id=suggested_owner,
            in_intra_file_group=in_group,
            is_pending=bool((parsed.raw or {}).get("pending")),
        ))

    payment_rows = [
        PaymentPreviewRow(
            transaction_date=p.transaction_date,
            description=p.description,
            amount=p.amount,
        )
        for p in parse_result.payments
    ]

    return ImportPreview(
        parser=parse_result.parser,
        payment_method_id=payment_method_id,
        payment_method_name=pm.name,
        currency=pm.currency,
        filename=filename,
        transactions=rows,
        payments=payment_rows,
        new_count=sum(1 for r in rows if not r.is_duplicate),
        duplicate_count=duplicate_count,
        skipped_count=parse_result.skipped,
        new_merchants=sorted(new_merchants),
    )


class CardContractConversion(BaseModel):
    """Per-row directive from the preview: split a single card charge into the
    first transaction of an N-installment CONTRACT series. Mirrors the
    checking-side CheckingContractConversion — only 1/N is written now
    (`amount/N`, recurrence CONTRACT); the rest roll in monthly. Used for
    annual renewals the user wants spread out (e.g. a prepaid phone plan → 12,
    Car Insurance → 6)."""
    index: int
    installments: int
    contract_end_date: date | None = None
    category_id: int | None = None  # optional override


def commit_import(
    session: Session,
    *,
    file_content: bytes | None = None,
    filename: str,
    payment_method_id: int,
    user_id: int | None = None,
    owner_user_ids: list[int] | None = None,
    default_owner_user_id: int = DEFAULT_OWNER_USER_ID,
    skip_indices: set[int] | None = None,
    category_overrides: list[int | None] | None = None,
    merchant_overrides: list[int | None] | None = None,
    new_merchant_names: list[str | None] | None = None,
    save_rule_flags: list[bool] | None = None,
    save_rule_amount_flags: list[bool] | None = None,
    contract_conversions: dict[int, CardContractConversion] | None = None,
    pre_parsed: ParseResult | None = None,
    source_override: ImportSource | None = None,
) -> ImportCommitResult:
    if pre_parsed is None:
        if file_content is None:
            raise ValueError("file_content is required when pre_parsed is not provided")
        detected = detect(filename)
        if detected is None:
            raise ValueError(f"No parser matches filename '{filename}'")
        source, parse_fn = detected
        parse_result = run_cc_parser(
            parse_fn, file_content, load_match_rules(session).holder_names
        )
    else:
        if source_override is None:
            raise ValueError("source_override is required when pre_parsed is provided")
        source = source_override
        parse_result = pre_parsed

    pm = session.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payment_method_id} not found")
    categorizer = Categorizer(session)

    # When the parser recovered statement-close / due-date from the statement
    # header, persist them per-card. Idempotent: only write when the day
    # actually changes.
    if parse_result.statement_close_date is not None:
        new_close = parse_result.statement_close_date.day
        if pm.statement_close_day != new_close:
            pm.statement_close_day = new_close
    if parse_result.due_date is not None:
        new_due = parse_result.due_date.day
        if pm.due_day != new_due:
            pm.due_day = new_due

    import_log = ImportLog(
        filename=filename,
        source=source,
        transaction_count=0,
        skipped_count=parse_result.skipped,
        user_id=user_id,
        payment_method_id=payment_method_id,
    )
    session.add(import_log)
    session.flush()

    inserted = 0
    duplicates = 0
    new_merchants = 0
    rules_created = 0
    reconciled = 0  # pending rows updated in place by a posted version
    rules_skipped: list[str] = []
    seen_signatures: set[tuple[date, int, Decimal, int, int]] = set()
    skip_set = skip_indices or set()
    convs = contract_conversions or {}

    def _override_at(arr: list | None, idx: int):
        if arr is None or idx >= len(arr):
            return None
        return arr[idx]

    for idx, parsed in enumerate(parse_result.transactions):
        if idx in skip_set:
            continue
        match = categorizer.classify(parsed.description, parsed.amount)

        # Category override (None = keep categorizer's pick).
        cat_override = _override_at(category_overrides, idx)
        category_id = cat_override if cat_override else match.category_id

        # Merchant override: explicit existing id, or a new-name request, or
        # fall back to the categorizer suggestion.
        merch_override = _override_at(merchant_overrides, idx)
        new_name = _override_at(new_merchant_names, idx)
        if merch_override:
            merchant_id = merch_override
        elif new_name:
            merchant = categorizer.get_or_create_merchant(new_name.strip(), category_id)
            new_merchants += 1
            merchant_id = merchant.id
        elif match.merchant_id is None:
            merchant = categorizer.get_or_create_merchant(match.merchant_name, category_id)
            new_merchants += 1
            merchant_id = merchant.id
        else:
            merchant_id = match.merchant_id

        owner_id = (
            owner_user_ids[idx]
            if owner_user_ids is not None and idx < len(owner_user_ids) and owner_user_ids[idx]
            else default_owner_user_id
        )

        plaid_tx_id = (parsed.raw or {}).get("plaid_transaction_id")
        pluggy_tx_id = (parsed.raw or {}).get("pluggy_transaction_id")
        is_pending = bool((parsed.raw or {}).get("pending"))

        # Pending→posted reconciliation: if this posted row replaces a
        # previously-committed pending one (Plaid links them via
        # pending_transaction_id), update that row in place (new id + final
        # amount/date) instead of inserting — guarantees no duplicate when a
        # pending charge later posts (often with a changed amount).
        pending_src_id = (parsed.raw or {}).get("pending_transaction_id")
        if pending_src_id:
            prior = session.scalar(
                select(Transaction).where(Transaction.plaid_transaction_id == pending_src_id)
            )
            if prior is not None:
                prior.plaid_transaction_id = plaid_tx_id
                prior.amount = parsed.amount
                prior.transaction_date = parsed.transaction_date
                prior.pending = is_pending  # posted version clears pending
                reconciled += 1
                continue

        existing_owners = _existing_owner_ids(
            session,
            txn_date=parsed.transaction_date,
            amount=parsed.amount,
            payment_method_id=payment_method_id,
            merchant_id=merchant_id,
        )
        signature = (parsed.transaction_date, merchant_id, parsed.amount, payment_method_id, owner_id)
        is_dup = (
            owner_id in existing_owners
            or signature in seen_signatures
            or _plaid_id_exists(session, plaid_tx_id)
            or _pluggy_id_exists(session, pluggy_tx_id)
        )
        if is_dup:
            duplicates += 1
        elif idx in convs:
            # Split into the first of an N-installment CONTRACT series; the
            # rest roll in monthly (mirrors the checking-side conversion).
            seen_signatures.add(signature)
            conv = convs[idx]
            n = max(1, conv.installments)
            per = (abs(parsed.amount) / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            conv_category_id = conv.category_id if conv.category_id is not None else category_id
            session.add(Transaction(
                transaction_date=parsed.transaction_date,
                merchant_id=merchant_id,
                category_id=conv_category_id,
                payment_method_id=payment_method_id,
                amount=per,
                currency=pm.currency,
                description=parsed.description[:500],
                installment_current=1,
                installment_total=n,
                installment_value=per,
                recurrence_kind=RecurrenceKind.CONTRACT,
                contract_end_date=conv.contract_end_date,
                import_log_id=import_log.id,
                created_by_user_id=owner_id,
                plaid_transaction_id=plaid_tx_id,
                pluggy_transaction_id=pluggy_tx_id,
                pending=is_pending,
            ))
            inserted += 1
        else:
            seen_signatures.add(signature)
            inst = _detect_installment(parsed.description or "")
            installment_current = inst[0] if inst else 1
            installment_total = inst[1] if inst else 1
            installment_value = parsed.amount if inst else None
            recurrence_kind = None
            contract_end_date = None

            # History-based recurrence propagation. When the
            # resulting category is FIXED and the description carried no
            # installment markers, look up a prior FIXED row with the same
            # (merchant, payment_method). If found, copy recurrence_kind /
            # contract_end_date / installment counter so the row lands
            # already tagged — saves a manual /transactions edit later.
            if inst is None:
                cat = session.get(Category, category_id)
                if cat is not None and cat.type == CategoryType.FIXED:
                    prior = find_prior_recurring(
                        session,
                        merchant_id=merchant_id,
                        payment_method_id=payment_method_id,
                        before_date=parsed.transaction_date,
                    )
                    if prior is not None and amount_matches_prior(prior, parsed.amount):
                        prop = propagation_for_new_row(prior, parsed.transaction_date)
                        if prop is not None:
                            recurrence_kind = prop.recurrence_kind
                            contract_end_date = prop.contract_end_date
                            installment_current = prop.installment_current
                            installment_total = prop.installment_total
                            installment_value = prop.installment_value

            session.add(Transaction(
                transaction_date=parsed.transaction_date,
                merchant_id=merchant_id,
                category_id=category_id,
                payment_method_id=payment_method_id,
                amount=parsed.amount,
                currency=pm.currency,
                description=parsed.description[:500],
                installment_current=installment_current,
                installment_total=installment_total,
                installment_value=installment_value,
                recurrence_kind=recurrence_kind,
                contract_end_date=contract_end_date,
                import_log_id=import_log.id,
                created_by_user_id=owner_id,
                plaid_transaction_id=plaid_tx_id,
                pluggy_transaction_id=pluggy_tx_id,
                pending=is_pending,
            ))
            inserted += 1

        # Auto-rule creation. Runs regardless of dup/insert so the
        # user can correct a category on a previously-imported row and still
        # get a rule for future imports of the same description. Requires
        # save_rule_flag + an actual user-driven override.
        if _override_at(save_rule_flags, idx):
            user_changed_cat = cat_override is not None and cat_override != match.category_id
            user_changed_merch = (merch_override is not None) or bool(new_name)
            if not (user_changed_cat or user_changed_merch):
                continue
            keyword = derive_rule_keyword(parsed.description)
            if keyword is None:
                rules_skipped.append(
                    f"row {idx}: could not derive keyword from description"
                )
                continue
            # Scope the rule to this transaction's amount when the user
            # asked for it (e.g. "google" $70 → Fiber vs $2.50 → Services).
            # NULL = matches any amount. Dedup is per (keyword, amount).
            rule_amount = abs(parsed.amount) if _override_at(save_rule_amount_flags, idx) else None
            dup_rule = session.scalar(
                select(CategorizationRule).filter_by(keyword=keyword, amount=rule_amount)
            )
            if dup_rule is not None:
                scope = f" @ ${rule_amount}" if rule_amount is not None else ""
                rules_skipped.append(
                    f"row {idx}: keyword '{keyword}'{scope} already used by rule #{dup_rule.id}"
                )
                continue
            if session.get(Merchant, merchant_id) is None:
                rules_skipped.append(f"row {idx}: merchant {merchant_id} missing")
                continue
            session.add(CategorizationRule(
                keyword=keyword,
                merchant_id=merchant_id,
                category_id=category_id,
                amount=rule_amount,
                priority=100,
            ))
            rules_created += 1

    import_log.transaction_count = inserted
    session.flush()
    session.commit()

    return ImportCommitResult(
        import_log_id=import_log.id,
        transactions_created=inserted,
        duplicates_skipped=duplicates,
        new_merchants_created=new_merchants,
        rules_created=rules_created,
        rules_skipped=rules_skipped,
    )
