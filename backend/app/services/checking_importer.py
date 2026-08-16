"""Checking-account statement import flow.

Differs from credit-card imports in two ways: most lines are not transactions
(transfers, CC payments, salary deposits) and the statement carries a
period-end balance that becomes a `savings_snapshots` row.

The parser produces a provisional classification by description keyword. This
service refines each activity with DB context (CC dedup, salary
reconciliation) and applies side effects on commit:

| Class             | Side effect on commit                                          |
|-------------------|----------------------------------------------------------------|
| SPENDING          | insert Transaction (categorizer-driven merchant/category)     |
| TAX_PAYMENT       | insert Transaction in `Taxes` category                         |
| CC_PAYMENT        | append CreditCardBalance for the matched card (dedup ±4 days) |
| SALARY            | adjust same-month withholding FIXED rows proportionally       |
| INTEREST          | skip — folded into period-end snapshot                         |
| INTERNAL_TRANSFER | skip                                                            |
| (always)          | append SavingsSnapshot for period_end + ImportLog audit       |

Default for every classification is to ACT. The import_log notes lists each
adjustment so the user can audit instead of typing.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Category,
    CategoryType,
    CreditCardBalance,
    Currency,
    HouseholdMember,
    ImportLog,
    IncomeEntry,
    IncomeSource,
    Merchant,
    PaymentMethod,
    SavingsSnapshot,
    Transaction,
    TransferRule,
    User,
)
from app.models.enums import ImportSource, RecurrenceKind
from app.services import household
from app.services.categorizer import Categorizer
from app.services.match_rules import load_match_rules
from app.services.parsers import (
    CheckingActivity,
    CheckingClass,
    CheckingParseResult,
    detect_checking,
)
from app.services.parsers.checking import normalize_description

DEFAULT_OWNER_USER_ID = 1
TAXES_CATEGORY_NAME = "Taxes"
CC_DEDUP_WINDOW_DAYS = 4  # ±4 days around the checking debit date
CC_DEDUP_TOLERANCE = Decimal("1.00")  # ±$1 on payment amount
FIXED_MATCH_TOLERANCE = Decimal("1.00")  # ±$1 on amount or installment_value
WITHHOLDING_MERCHANT_FRAGMENTS = ("Withholding",)
# Household identity (who earns what, which deposits are theirs, their pay
# levels, which merchants are their withholdings) lives in the database — see
# `app/services/household.py` and the `household_members` / `salary_levels` /
# `withholding_merchants` tables. Nothing in this module knows a member's name.


# ---------- Pydantic shapes for preview/commit ----------

class CCPaymentInfo(BaseModel):
    card_payment_method_id: int | None
    card_payment_method_name: str
    already_recorded: bool   # True when a similar payment already shows on the card's recent balance history
    note: str = ""


class FixedMatchInfo(BaseModel):
    transaction_id: int
    merchant_name: str
    db_amount: Decimal
    db_installment_value: Decimal | None
    matched_by: str   # "amount" or "installment_value"


class HistoryPropagationInfo(BaseModel):
    """Set when the SPENDING row will be inserted as FIXED via prior-month
    history. Caller knows we'll copy recurrence_kind, contract
    end date, and installment counter from the most-recent prior FIXED row
    on the same (merchant, payment_method)."""
    prior_transaction_id: int
    prior_date: date_type
    merchant_name: str
    category_name: str
    recurrence_kind: str | None
    installment_current: int
    installment_total: int
    contract_end_date: date_type | None


class SalaryReconciliationOut(BaseModel):
    owner: str
    deposit_net: Decimal
    expected_gross: Decimal
    implied_total_withholdings: Decimal
    db_total_withholdings: Decimal
    variance: Decimal
    requires_review: bool
    proposed_adjustments: list[dict]  # [{transaction_id, merchant_name, current, new}]
    income_missing: bool = False  # no income_entries row yet; commit will auto-create it


class CheckingPreviewActivity(BaseModel):
    activity_date: date_type
    description: str
    amount: Decimal
    classification: CheckingClass
    match_hint: str
    will_action: str  # human-readable summary of what commit will do
    transaction_merchant: str | None = None
    transaction_category: str | None = None
    transaction_is_new_merchant: bool = False
    is_duplicate: bool = False  # True when a same-signature transaction already exists
    duplicate_transaction_id: int | None = None
    already_imported: bool = False  # handled in a prior review (in transactions or seen-set)
    # Transfer-rule pre-mapping (by pm+amount): the UI pre-fills the category
    # override so a recurring fixed bill paid by transfer just needs confirming.
    suggested_category_id: int | None = None
    suggested_category_name: str | None = None
    suggested_merchant_id: int | None = None
    cc_payment: CCPaymentInfo | None = None
    salary: SalaryReconciliationOut | None = None
    fixed_match: FixedMatchInfo | None = None
    history_promotion: HistoryPropagationInfo | None = None
    pending: bool = False  # Plaid pending (provisional) — amount may change when it posts


class CheckingImportPreview(BaseModel):
    parser: str
    payment_method_id: int
    payment_method_name: str
    currency: Currency
    filename: str
    period_start: date_type
    period_end: date_type
    beginning_balance: Decimal
    ending_balance: Decimal
    activities: list[CheckingPreviewActivity]

    snapshot_account_name: str
    snapshot_balance: Decimal
    skip_snapshot: bool = False


class CheckingImportCommitResult(BaseModel):
    import_log_id: int
    transactions_created: int
    cc_payments_applied: int
    cc_payments_deduped: int
    fixed_matched: int
    salary_adjustments: int
    snapshot_created: bool
    log_notes: list[str]


class CheckingContractConversion(BaseModel):
    """Per-row directive from the preview: convert a SPENDING activity into the
    first transaction of an N-installment CONTRACT series.

    On commit, the original row's full amount is split: only the first
    per-installment value lands as a transaction (so the bank ledger and the
    spending bucket don't show the lump sum). The remaining N-1 rows are
    produced later by the normal monthly Rollover flow.
    """
    index: int
    installments: int
    contract_end_date: date_type | None = None
    category_id: int | None = None  # optional override (e.g. force Health Insurance)


# ---------- Helpers ----------

def _list_credit_cards(session: Session) -> dict[str, PaymentMethod]:
    rows = session.scalars(
        select(PaymentMethod).where(PaymentMethod.type == "credit_card")
    ).all()
    return {pm.name: pm for pm in rows}


def _cc_payment_already_applied(
    session: Session, card_id: int, payment_amount: Decimal, activity_date: date_type
) -> bool:
    """Heuristic: did a CreditCardBalance pair near `activity_date` already
    record a delta within tolerance of `payment_amount`?

    Walks the *entire* per-card ledger chronologically. For each row whose
    `recorded_at` falls inside the ±N-day window of `activity_date`, the
    delta is computed against the IMMEDIATELY PRECEDING row (which may be
    *outside* the window). Bug fix 2026-05-22: previously the SQL filter
    restricted rows to those inside the window, which silently dropped any
    dedupe target when only one row landed there — the function would then
    return False and the importer would re-apply an already-booked payment.
    """
    rows = session.execute(
        select(CreditCardBalance.balance, CreditCardBalance.recorded_at)
        .where(CreditCardBalance.payment_method_id == card_id)
        .order_by(CreditCardBalance.recorded_at)
    ).all()
    window_low = activity_date - timedelta(days=CC_DEDUP_WINDOW_DAYS)
    window_high = activity_date + timedelta(days=CC_DEDUP_WINDOW_DAYS)
    activity_ym = (activity_date.year, activity_date.month)
    target = abs(payment_amount)
    prev_balance: Decimal | None = None
    for r in rows:
        recorded_at = r.recorded_at
        recorded_date = recorded_at.date() if hasattr(recorded_at, "date") else recorded_at
        # Accept the row as a dedupe candidate when it's either within the
        # ±4-day window OR in the same calendar month as the activity.
        # Reason: previous import paths recorded `recorded_at` at
        # `datetime.now()` (the import-run time) instead of the activity
        # date, leaving the ledger row outside the tight ±4d window when
        # the import ran days later. Same-month is a safer fallback that
        # still avoids matching last/next month's recurring payment.
        in_window = window_low <= recorded_date <= window_high
        in_same_month = (recorded_date.year, recorded_date.month) == activity_ym
        if prev_balance is not None and (in_window or in_same_month):
            delta = prev_balance - Decimal(r.balance)  # positive = payment
            if delta > 0 and abs(delta - target) <= CC_DEDUP_TOLERANCE:
                return True
        prev_balance = Decimal(r.balance)
    return False


def _fixed_rows_for_period(
    session: Session, payment_method_id: int, period_start: date_type, period_end: date_type
) -> list[Transaction]:
    """All FIXED transactions on this payment_method whose date falls within
    the statement period — widened to the calendar months containing
    `period_start` and `period_end`.

    Why widen: paste flows cover only the days the user copied (e.g.,
    5/04-5/19) and would miss a Rent FIXED row dated 5/01 (outside the
    paste's tight bounds) → `_match_fixed` couldn't catch the rent debit
    when it appeared in the paste on 5/04, surfacing as a fake SPENDING.
    Widening to full calendar months gives the matcher the right pool
    without breaking statement-period (PDF) flows.

    `_match_fixed` itself still filters per-activity-month to avoid
    matching the same recurring bill across two months when a statement
    spans a boundary."""
    import calendar as _cal
    widened_start = period_start.replace(day=1)
    last_day = _cal.monthrange(period_end.year, period_end.month)[1]
    widened_end = period_end.replace(day=last_day)
    return list(session.scalars(
        select(Transaction)
        .join(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.payment_method_id == payment_method_id,
            Transaction.transaction_date >= widened_start,
            Transaction.transaction_date <= widened_end,
            Category.type == CategoryType.FIXED,
        )
    ).all())


def _match_fixed(
    activity_amount: Decimal,
    fixed_rows: list[Transaction],
    activity_date: date_type | None = None,
) -> tuple[Transaction, str] | None:
    """Find a single FIXED row whose amount or installment_value is within
    tolerance of the absolute activity amount. Returns (row, matched_by) or
    None when no match or ambiguous.

    When `activity_date` is given, the candidate pool is narrowed to FIXED
    rows in the same calendar month as the activity. This prevents
    cross-month false positives when `_fixed_rows_for_period` widens to
    multi-month spans (paste flows, statements crossing month boundaries
    on the same recurring merchant)."""
    target = abs(activity_amount)
    candidate_pool = fixed_rows
    if activity_date is not None:
        ay, am = activity_date.year, activity_date.month
        candidate_pool = [
            r for r in fixed_rows
            if (r.transaction_date.year, r.transaction_date.month) == (ay, am)
        ]
    by_amount = [
        r for r in candidate_pool
        if abs(Decimal(r.amount) - target) <= FIXED_MATCH_TOLERANCE
    ]
    if len(by_amount) == 1:
        return by_amount[0], "amount"
    by_installment = [
        r for r in candidate_pool
        if r.installment_value is not None
        and abs(Decimal(r.installment_value) - target) <= FIXED_MATCH_TOLERANCE
    ]
    if len(by_installment) == 1:
        return by_installment[0], "installment_value"
    return None


def _withholding_rows_for_month(
    session: Session, year: int, month: int, owner_id: int
) -> list[Transaction]:
    """FIXED transactions for the period whose merchant name contains a
    withholding keyword. Owner-agnostic by design: historical seeded data
    has all of the partner's withholdings owned by the primary user (default owner from
    migration), and tightening the filter would surface a false zero. The
    salary reconciliation is keyed on the salary-source -> merchant pair,
    which is unambiguous."""
    rows = session.execute(
        select(Transaction)
        .join(Merchant, Merchant.id == Transaction.merchant_id)
        .join(Category, Category.id == Transaction.category_id)
        .where(Category.type == CategoryType.FIXED)
    ).scalars().all()
    out: list[Transaction] = []
    for r in rows:
        if r.transaction_date.year != year or r.transaction_date.month != month:
            continue
        m_name = r.merchant.name
        if any(frag in m_name for frag in WITHHOLDING_MERCHANT_FRAGMENTS):
            out.append(r)
    return out


def _income_for_period(
    session: Session, year: int, month: int, source: IncomeSource
) -> IncomeEntry | None:
    return session.scalar(
        select(IncomeEntry).filter_by(year=year, month=month, source=source)
    )


def _resolve_owner_user_id(session: Session, owner_name: str) -> int | None:
    if not owner_name:
        return None
    user = session.scalar(select(User).where(User.name.ilike(owner_name)))
    return user.id if user else None


def _build_salary_reconciliation(
    session: Session, activity: CheckingActivity
) -> SalaryReconciliationOut | None:
    if activity.classification != CheckingClass.SALARY:
        return None
    member = household.member_by_match_key(session, activity.match_hint)
    if member is None:
        return None
    # Only members whose pay arrives net of FIXED withholding rows are
    # reconciled. A gross deposit (taxes paid separately, later, as their own
    # TAX_PAYMENT activities) returns None and takes the auto-create path on
    # commit instead.
    if not member.has_withholdings:
        return None
    owner_name = member.display_name
    income_source = member.salary_income_source
    owner_id = member.user_id

    # User's convention: salary deposited at end of month X is the income that
    # funds month X+1 — `income_entries` rows and the matching FIXED
    # withholdings live in month X+1, so reconciliation targets month+1.
    if activity.activity_date.month == 12:
        period_year, period_month = activity.activity_date.year + 1, 1
    else:
        period_year, period_month = activity.activity_date.year, activity.activity_date.month + 1
    income = _income_for_period(session, period_year, period_month, income_source)
    # Gross is fixed per pay level (`salary_levels`); only the withholdings move
    # month to month. When the income_entries row for the target month does not
    # exist yet, reconcile against the configured level and let commit
    # auto-create the row.
    income_missing = income is None

    scheduled = household.gross_for_month(member, period_year, period_month)
    if income is None and scheduled is None:
        return None
    expected_gross = Decimal(income.amount) if income is not None else scheduled
    deposit_net = abs(activity.amount)
    implied_total = expected_gross - deposit_net

    db_rows = _withholding_rows_for_month(session, period_year, period_month, owner_id)
    db_total = sum((Decimal(r.amount) for r in db_rows), start=Decimal("0"))

    variance = db_total - implied_total
    requires_review = (
        expected_gross > 0 and abs(variance) / expected_gross > Decimal("0.20")
    )

    proposed: list[dict] = []
    if db_rows and db_total > 0 and not requires_review:
        for r in db_rows:
            ratio = Decimal(r.amount) / db_total
            new_amount = (implied_total * ratio).quantize(Decimal("0.01"))
            proposed.append(
                {
                    "transaction_id": r.id,
                    "merchant_name": r.merchant.name,
                    "current": str(Decimal(r.amount)),
                    "new": str(new_amount),
                }
            )

    return SalaryReconciliationOut(
        owner=owner_name,
        deposit_net=deposit_net,
        expected_gross=expected_gross,
        implied_total_withholdings=implied_total,
        db_total_withholdings=db_total,
        variance=variance,
        requires_review=requires_review,
        proposed_adjustments=proposed,
        income_missing=income_missing,
    )


def _will_action_summary(
    classification: CheckingClass,
    activity: CheckingActivity,
    cc: CCPaymentInfo | None,
    salary: SalaryReconciliationOut | None,
    fixed: FixedMatchInfo | None,
    history: "HistoryPropagationInfo | None" = None,
    is_duplicate: bool = False,
    duplicate_tx_id: int | None = None,
    member: "HouseholdMember | None" = None,
) -> str:
    if is_duplicate and classification in (CheckingClass.SPENDING, CheckingClass.TAX_PAYMENT):
        suffix = f" #{duplicate_tx_id}" if duplicate_tx_id is not None else ""
        return f"Skip — DUP (same date/merchant/amount already in DB{suffix})"
    if classification == CheckingClass.FIXED_MATCH and fixed is not None:
        return f"Skip — matches {fixed.merchant_name} FIXED #{fixed.transaction_id} (${fixed.db_amount})"
    if classification == CheckingClass.SPENDING:
        if history is not None:
            rk = history.recurrence_kind or "FIXED"
            inst = (
                f" {history.installment_current}/{history.installment_total}"
                if history.installment_total > 1
                else ""
            )
            return (
                f"Insert as FIXED {rk}{inst} via history "
                f"({history.merchant_name}, prior {history.prior_date})"
            )
        return f"Insert transaction (${abs(activity.amount)})"
    if classification == CheckingClass.TAX_PAYMENT:
        return f"Insert as Taxes (${abs(activity.amount)})"
    if classification == CheckingClass.CC_PAYMENT:
        if cc is None or cc.card_payment_method_id is None:
            return "Skip — card not recognized"
        if cc.already_recorded:
            return f"Skip — already on {cc.card_payment_method_name} (last imports)"
        return f"Reduce {cc.card_payment_method_name} balance by ${abs(activity.amount)}"
    if classification == CheckingClass.SALARY:
        if salary is None:
            if member is not None and not member.has_withholdings:
                target_m = 1 if activity.activity_date.month == 12 else activity.activity_date.month + 1
                target_y = activity.activity_date.year + (1 if activity.activity_date.month == 12 else 0)
                month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[target_m - 1]
                return (
                    f"Record as {member.salary_income_source.name} income for "
                    f"{month_name} {target_y} (${abs(activity.amount)})"
                )
            return "Skip — no income_entries match"
        if salary.requires_review:
            return f"⚠ Variance {salary.variance} — review withholdings before commit"
        prefix = (
            f"Create {salary.owner.upper()}_SALARY income ${salary.expected_gross} + "
            if salary.income_missing
            else ""
        )
        return f"{prefix}Adjust withholdings to total ${salary.implied_total_withholdings} (variance {salary.variance})"
    if classification == CheckingClass.RENT_DEPOSIT:
        target_m = 1 if activity.activity_date.month == 12 else activity.activity_date.month + 1
        target_y = activity.activity_date.year + (1 if activity.activity_date.month == 12 else 0)
        month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[target_m - 1]
        return f"Record as RENTS_BRAZIL income for {month_name} {target_y} (${abs(activity.amount)})"
    if classification == CheckingClass.EXTRA_INCOME:
        month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[activity.activity_date.month - 1]
        return f"Record as EXTRA income for {month_name} {activity.activity_date.year} (${abs(activity.amount)})"
    if classification == CheckingClass.INTEREST:
        return f"Fold into snapshot (+${activity.amount})"
    if classification == CheckingClass.INTERNAL_TRANSFER:
        return "Skip"
    return "Skip"


# ---------- Preview ----------

def build_checking_preview(
    session: Session,
    *,
    file_content: bytes | None = None,
    filename: str = "",
    payment_method_id: int,
    pre_parsed: CheckingParseResult | None = None,
) -> CheckingImportPreview:
    """Either pass `file_content + filename` (PDF flow, auto-detected) or
    pass a `pre_parsed` `CheckingParseResult` (manual paste flow)."""
    pm = session.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payment_method_id} not found")

    if pre_parsed is not None:
        parsed = pre_parsed
    else:
        detected = detect_checking(filename)
        if detected is None:
            raise ValueError(f"No checking parser matches filename '{filename}'")
        _, parse_fn = detected
        if file_content is None:
            raise ValueError("file_content required when pre_parsed is not provided")
        parsed = parse_fn(file_content, load_match_rules(session))
    cards = _list_credit_cards(session)
    categorizer = Categorizer(session)
    fixed_rows = _fixed_rows_for_period(
        session, payment_method_id, parsed.period_start, parsed.period_end
    )
    # Transfer rules: map a recurring transfer to a category by exact amount.
    transfer_rules = {
        Decimal(r.amount): r
        for r in session.scalars(
            select(TransferRule).where(TransferRule.payment_method_id == payment_method_id)
        ).all()
    }

    activities: list[CheckingPreviewActivity] = []
    for a in parsed.activities:
        cc_info: CCPaymentInfo | None = None
        salary_out: SalaryReconciliationOut | None = None
        fixed_info: FixedMatchInfo | None = None
        merch_name: str | None = None
        cat_name: str | None = None
        is_new = False
        is_dup_signature = False
        dup_tx_id: int | None = None
        effective_class = a.classification

        if a.classification == CheckingClass.CC_PAYMENT:
            card = cards.get(a.match_hint)
            already = False
            if card is not None:
                already = _cc_payment_already_applied(
                    session, card.id, abs(a.amount), a.activity_date
                )
            cc_info = CCPaymentInfo(
                card_payment_method_id=card.id if card else None,
                card_payment_method_name=card.name if card else a.match_hint or "(unknown)",
                already_recorded=already,
            )
        elif a.classification == CheckingClass.SALARY:
            salary_out = _build_salary_reconciliation(session, a)
        history_info: HistoryPropagationInfo | None = None
        if a.classification in (CheckingClass.SPENDING, CheckingClass.TAX_PAYMENT):
            # Try to match a same-period FIXED transaction first; promotes the
            # row to FIXED_MATCH so the importer skips it instead of creating
            # a duplicate. Applies to both SPENDING and TAX_PAYMENT — the
            # latter previously skipped this branch and would duplicate
            # against rolled-forward FIXED Taxes placeholders.
            fmatch = _match_fixed(a.amount, fixed_rows, a.activity_date)
            if fmatch is not None:
                row, matched_by = fmatch
                effective_class = CheckingClass.FIXED_MATCH
                fixed_info = FixedMatchInfo(
                    transaction_id=row.id,
                    merchant_name=row.merchant.name,
                    db_amount=Decimal(row.amount),
                    db_installment_value=(
                        Decimal(row.installment_value) if row.installment_value is not None else None
                    ),
                    matched_by=matched_by,
                )
            else:
                match = categorizer.classify(normalize_description(a.description), a.amount)
                merch_name = match.merchant_name
                cat_name = (
                    match.category_name if a.classification == CheckingClass.SPENDING else TAXES_CATEGORY_NAME
                )
                is_new = match.merchant_id is None

                # Dedupe check: same (date, merchant, amount, payment_method,
                # owner) row already in DB → flag as DUP so the preview
                # doesn't lie about an insert that commit will actually skip.
                if match.merchant_id is not None:
                    amount_signed = -a.amount  # checking convention: debit→positive txn
                    existing_dup = session.scalar(
                        select(Transaction.id).where(
                            Transaction.transaction_date == a.activity_date,
                            Transaction.merchant_id == match.merchant_id,
                            Transaction.amount == amount_signed,
                            Transaction.payment_method_id == payment_method_id,
                            Transaction.created_by_user_id == DEFAULT_OWNER_USER_ID,
                        )
                    )
                    if existing_dup is not None:
                        is_dup_signature = True
                        dup_tx_id = existing_dup
                    else:
                        is_dup_signature = False
                        dup_tx_id = None
                else:
                    is_dup_signature = False
                    dup_tx_id = None

                # Surface history-based propagation in the preview
                # so the user sees the row will be inserted as FIXED with
                # copied recurrence metadata.
                if a.classification == CheckingClass.SPENDING and match.merchant_id is not None:
                    from app.services.recurrence import (
                        amount_matches_prior,
                        find_prior_recurring,
                        propagation_for_new_row,
                    )
                    prior = find_prior_recurring(
                        session,
                        merchant_id=match.merchant_id,
                        payment_method_id=payment_method_id,
                        before_date=a.activity_date,
                    )
                    if prior is not None and amount_matches_prior(prior, a.amount):
                        prop = propagation_for_new_row(prior, a.activity_date)
                        if prop is not None:
                            history_info = HistoryPropagationInfo(
                                prior_transaction_id=prior.id,
                                prior_date=prior.transaction_date,
                                merchant_name=prior.merchant.name,
                                category_name=prior.category.name,
                                recurrence_kind=(
                                    prop.recurrence_kind.value if prop.recurrence_kind else None
                                ),
                                installment_current=prop.installment_current,
                                installment_total=prop.installment_total,
                                contract_end_date=prop.contract_end_date,
                            )

        if not is_dup_signature and (
            _plaid_id_exists(session, a.plaid_transaction_id)
            or _pluggy_id_exists(session, a.pluggy_transaction_id)
        ):
            is_dup_signature = True

        # Transfer-rule suggestion: a recurring transfer matched by exact amount.
        sug_cat_id = sug_cat_name = sug_merch_id = None
        if a.classification == CheckingClass.INTERNAL_TRANSFER:
            trule = transfer_rules.get(abs(a.amount))
            if trule is not None:
                sug_cat_id = trule.category_id
                sug_cat_name = trule.category.name
                sug_merch_id = trule.merchant_id

        activities.append(
            CheckingPreviewActivity(
                activity_date=a.activity_date,
                description=a.description,
                amount=a.amount,
                classification=effective_class,
                match_hint=a.match_hint,
                will_action=_will_action_summary(
                    effective_class, a, cc_info, salary_out, fixed_info, history_info,
                    is_duplicate=is_dup_signature, duplicate_tx_id=dup_tx_id,
                    member=household.member_by_match_key(session, a.match_hint),
                ),
                transaction_merchant=merch_name,
                transaction_category=cat_name,
                transaction_is_new_merchant=is_new,
                is_duplicate=is_dup_signature,
                duplicate_transaction_id=dup_tx_id,
                suggested_category_id=sug_cat_id,
                suggested_category_name=sug_cat_name,
                suggested_merchant_id=sug_merch_id,
                cc_payment=cc_info,
                salary=salary_out,
                fixed_match=fixed_info,
                history_promotion=history_info,
                pending=a.pending,
            )
        )

    return CheckingImportPreview(
        parser=parsed.parser,
        payment_method_id=payment_method_id,
        payment_method_name=pm.name,
        currency=pm.currency,
        filename=filename,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        beginning_balance=parsed.beginning_balance,
        ending_balance=parsed.ending_balance,
        activities=activities,
        snapshot_account_name=pm.name,
        snapshot_balance=parsed.ending_balance,
        skip_snapshot=parsed.skip_snapshot,
    )


# ---------- Commit ----------

def _taxes_category_id(session: Session) -> int:
    cat = session.scalar(select(Category).filter_by(name=TAXES_CATEGORY_NAME))
    if cat is None:
        raise RuntimeError(f"Category '{TAXES_CATEGORY_NAME}' missing — seed reference data")
    return cat.id


def commit_checking_import(
    session: Session,
    *,
    file_content: bytes | None = None,
    filename: str = "",
    payment_method_id: int,
    user_id: int | None = None,
    skip_indices: set[int] | None = None,
    contract_conversions: dict[int, CheckingContractConversion] | None = None,
    category_overrides: dict[int, int] | None = None,
    merchant_overrides: dict[int, int] | None = None,
    new_merchant_names: dict[int, str] | None = None,
    cc_payment_overrides: dict[int, int] | None = None,
    save_transfer_rule_flags: set[int] | None = None,
    pre_parsed: CheckingParseResult | None = None,
    source_override: str | None = None,
) -> CheckingImportCommitResult:
    """Either pass `file_content + filename` (PDF flow) or `pre_parsed +
    source_override` (manual paste flow). `source_override` sets the
    source on the audit log.

    `category_overrides` (idx -> category_id) lets the user rescue a row the
    classifier got wrong: any activity with an override is inserted as a normal
    SPENDING transaction in that category, regardless of its auto-classification
    (e.g. a Plaid "Online Transfer / Payment: Debit" that's really a purchase)."""
    pm = session.get(PaymentMethod, payment_method_id)
    if pm is None:
        raise ValueError(f"Payment method {payment_method_id} not found")

    if pre_parsed is not None:
        parsed = pre_parsed
        source = source_override or ImportSource.MANUAL
    else:
        detected = detect_checking(filename)
        if detected is None:
            raise ValueError(f"No checking parser matches filename '{filename}'")
        source, parse_fn = detected
        if file_content is None:
            raise ValueError("file_content required when pre_parsed is not provided")
        parsed = parse_fn(file_content, load_match_rules(session))
    cards = _list_credit_cards(session)
    categorizer = Categorizer(session)
    fixed_rows = _fixed_rows_for_period(
        session, payment_method_id, parsed.period_start, parsed.period_end
    )

    log = ImportLog(
        filename=filename,
        source=source,
        transaction_count=0,
        skipped_count=parsed.skipped,
        user_id=user_id,
        payment_method_id=payment_method_id,
    )
    session.add(log)
    session.flush()

    transactions_created = 0
    cc_applied = 0
    cc_deduped = 0
    fixed_matched = 0
    salary_adjustments = 0
    notes: list[str] = []
    taxes_category_id = _taxes_category_id(session)

    skip_set = skip_indices or set()
    convs = contract_conversions or {}
    cat_overrides = category_overrides or {}
    merch_overrides = merchant_overrides or {}
    new_merch_names = new_merchant_names or {}
    cc_pay_overrides = cc_payment_overrides or {}
    for idx, a in enumerate(parsed.activities):
        if idx in skip_set:
            continue
        # Pending→posted reconciliation: a posted row that replaces a
        # previously-committed pending one updates that row in place (new id +
        # final amount/date) instead of inserting — guarantees no duplicate.
        if a.pending_transaction_id:
            prior = session.scalar(
                select(Transaction).where(
                    Transaction.plaid_transaction_id == a.pending_transaction_id
                )
            )
            if prior is not None:
                prior.plaid_transaction_id = a.plaid_transaction_id
                prior.amount = -a.amount  # checking sign: debit→positive charge
                prior.transaction_date = a.activity_date
                prior.pending = a.pending  # posted version clears pending
                notes.append(
                    f"Pending→posted {a.activity_date} ${abs(a.amount)}: "
                    f"reconciled existing row (no duplicate)"
                )
                continue
        # User reclassified this row in the preview: insert it as a normal
        # SPENDING transaction in the chosen category, regardless of how it was
        # auto-classified (rescues Plaid mislabels — e.g. an "Online Transfer /
        # Payment: Debit" that's actually a purchase or an extra car payment).
        # User marked this row as a payment to a specific card → reduce
        # that card's balance (manual cards aren't refreshed from Plaid balances).
        pay_card_id = cc_pay_overrides.get(idx)
        if pay_card_id is not None:
            applied, note = _apply_explicit_cc_payment(session, a, pay_card_id)
            if applied is True:
                cc_applied += 1
            elif applied is False:
                cc_deduped += 1
            if note:
                notes.append(note)
            continue
        override_cat = cat_overrides.get(idx)
        if override_cat is not None:
            # Remember this transfer's amount → category for future reviews, so
            # next month it comes pre-mapped (runs regardless of whether this
            # month's row dedups or inserts).
            if save_transfer_rule_flags and idx in save_transfer_rule_flags:
                _upsert_transfer_rule(
                    session, pm.id, abs(a.amount), override_cat, merch_overrides.get(idx)
                )
            # A user-promoted row (e.g. an INTERNAL_TRANSFER reclassified as
            # "Insert as Rent") must not duplicate a same-period FIXED already
            # in the ledger. Match within the chosen category so two unrelated
            # FIXED items of equal value don't collide.
            cat_fixed = [r for r in fixed_rows if r.category_id == override_cat]
            fmatch = _match_fixed(a.amount, cat_fixed, a.activity_date)
            if fmatch is not None:
                row, matched_by = fmatch
                fixed_matched += 1
                notes.append(
                    f"User-reclassified FIXED match {a.activity_date} "
                    f"${abs(a.amount)} -> {row.merchant.name} #{row.id} "
                    f"(by {matched_by}); skipped (no duplicate)"
                )
                continue
            transactions_created += _insert_transaction(
                session, a, pm, categorizer, log.id, category_override_id=override_cat,
                merchant_override_id=merch_overrides.get(idx),
                new_merchant_name=new_merch_names.get(idx),
            )
            notes.append(
                f"User-reclassified {a.activity_date} ${abs(a.amount)} -> "
                f"category #{override_cat} (was {a.classification.value})"
            )
            continue
        # Re-run FIXED_MATCH detection in commit so a SPENDING activity that
        # would create a duplicate is skipped (preview promotes it to
        # FIXED_MATCH but commit re-derives so user can't bypass via API).
        if a.classification == CheckingClass.SPENDING:
            conv = convs.get(idx)
            if conv is not None:
                inserted = _insert_contract_first(
                    session, a, pm, categorizer, log.id, conv
                )
                transactions_created += inserted
                notes.append(
                    f"Contract split {a.activity_date} ${abs(a.amount)} -> "
                    f"{conv.installments}x (1/{conv.installments} written, rest rolls in)"
                )
                continue
            fmatch = _match_fixed(a.amount, fixed_rows, a.activity_date)
            if fmatch is not None:
                row, matched_by = fmatch
                fixed_matched += 1
                notes.append(
                    f"FIXED match {a.activity_date} ${abs(a.amount)} -> "
                    f"{row.merchant.name} #{row.id} (by {matched_by}); skipped"
                )
                continue
            # History-based propagation. If we know this
            # (merchant, payment_method) pair from a prior FIXED row, copy
            # the recurrence metadata forward instead of inserting a bare
            # SPENDING row. Replaces what rollover used to do for
            # CONTRACT / INDEFINITE / non-installment FIXED rows.
            promoted, promote_note = _try_promote_from_history(
                session, a, pm, categorizer, log.id
            )
            if promoted:
                transactions_created += promoted
                notes.append(promote_note)
                continue
            transactions_created += _insert_transaction(
                session, a, pm, categorizer, log.id, category_override_id=None
            )
        elif a.classification == CheckingClass.TAX_PAYMENT:
            # Mirror the SPENDING path: a real tax payment that lines up with
            # a rolled-forward FIXED Taxes placeholder should skip rather
            # than duplicate.
            fmatch = _match_fixed(a.amount, fixed_rows, a.activity_date)
            if fmatch is not None:
                row, matched_by = fmatch
                fixed_matched += 1
                notes.append(
                    f"TAX FIXED match {a.activity_date} ${abs(a.amount)} -> "
                    f"{row.merchant.name} #{row.id} (by {matched_by}); skipped"
                )
                continue
            transactions_created += _insert_transaction(
                session, a, pm, categorizer, log.id, category_override_id=taxes_category_id
            )
        elif a.classification == CheckingClass.EXTRA_INCOME:
            note = _record_extra_income(session, a, pm)
            if note:
                notes.append(note)
        elif a.classification == CheckingClass.CC_PAYMENT:
            applied, note = _apply_cc_payment(session, a, cards)
            if applied is True:
                cc_applied += 1
                notes.append(note)
            elif applied is False:
                cc_deduped += 1
                notes.append(note)
        elif a.classification == CheckingClass.SALARY:
            member = household.member_by_match_key(session, a.match_hint)
            if member is not None and member.has_withholdings:
                # Fixed gross → auto-create the income_entries row when the
                # month hasn't been seeded yet, then reconcile withholdings.
                notes.append(_ensure_partner_salary(session, a, member))
            recon = _build_salary_reconciliation(session, a)
            if recon and recon.proposed_adjustments and not recon.requires_review:
                count = _apply_withholding_adjustments(session, recon)
                if count:
                    salary_adjustments += count
                    notes.append(
                        f"Salary reconciliation ({recon.owner} {a.activity_date}): "
                        f"adjusted {count} withholding rows to total "
                        f"${recon.implied_total_withholdings} (variance {recon.variance})"
                    )
            elif member is not None and not member.has_withholdings:
                # Gross deposit, no withholding rows to rebalance: just record
                # the income for month+1.
                notes.append(_record_primary_salary(session, a, pm))
        elif a.classification == CheckingClass.RENT_DEPOSIT:
            note = _record_rent_deposit(session, a, pm)
            if note:
                notes.append(note)
        # INTEREST and INTERNAL_TRANSFER deliberately fall through.

    if parsed.skip_snapshot:
        notes.append(f"Snapshot skipped (parser opt-out for {pm.name})")
        snapshot_created = False
    else:
        snapshot_created = _append_snapshot(session, pm, parsed)
        if snapshot_created:
            notes.append(
                f"Snapshot {pm.name} on {parsed.period_end}: ${parsed.ending_balance}"
            )

    log.transaction_count = transactions_created
    session.flush()
    session.commit()

    return CheckingImportCommitResult(
        import_log_id=log.id,
        transactions_created=transactions_created,
        cc_payments_applied=cc_applied,
        cc_payments_deduped=cc_deduped,
        fixed_matched=fixed_matched,
        salary_adjustments=salary_adjustments,
        snapshot_created=snapshot_created,
        log_notes=notes,
    )


def _try_promote_from_history(
    session: Session,
    a: CheckingActivity,
    pm: PaymentMethod,
    categorizer: Categorizer,
    import_log_id: int,
) -> tuple[int, str | None]:
    """If the (merchant, payment_method) pair has a prior FIXED
    row in the last few months, copy its recurrence metadata forward into a
    new row for `a.activity_date`. Replaces the proactive rollover for
    CONTRACT / INDEFINITE / non-installment FIXED rows.

    Returns (inserted_count, audit_note). (0, None) signals "no prior
    history found or series ended — caller falls back to plain SPENDING"."""
    from app.services.recurrence import (
        amount_matches_prior,
        find_prior_recurring,
        propagation_for_new_row,
    )

    match = categorizer.classify(normalize_description(a.description), a.amount)
    if match.merchant_id is None:
        # No persisted merchant yet → no history to find.
        return 0, None

    prior = find_prior_recurring(
        session,
        merchant_id=match.merchant_id,
        payment_method_id=pm.id,
        before_date=a.activity_date,
    )
    if prior is None:
        return 0, None

    # Amount sanity check: propagation is only valid when the activity's
    # magnitude matches the prior's recurring amount or installment_value.
    # Otherwise we'd mis-tag e.g. a $300 car-loan extra as if it were the
    # $425.00 parcela just because the merchant/payment_method matches.
    if not amount_matches_prior(prior, a.amount):
        return 0, None

    propagation = propagation_for_new_row(prior, a.activity_date)
    if propagation is None:
        return 0, None

    amount_signed = -a.amount  # checking debit -> positive transaction
    owner_id = DEFAULT_OWNER_USER_ID

    if _plaid_id_exists(session, a.plaid_transaction_id) or _pluggy_id_exists(
        session, a.pluggy_transaction_id
    ):
        return 0, None

    existing = session.scalar(
        select(Transaction).filter_by(
            transaction_date=a.activity_date,
            merchant_id=prior.merchant_id,
            amount=amount_signed,
            payment_method_id=pm.id,
            created_by_user_id=owner_id,
        )
    )
    if existing is not None:
        return 0, None

    session.add(
        Transaction(
            transaction_date=a.activity_date,
            merchant_id=prior.merchant_id,
            category_id=propagation.category_id,
            payment_method_id=pm.id,
            amount=amount_signed,
            currency=pm.currency,
            description=a.description[:500],
            recurrence_kind=propagation.recurrence_kind,
            contract_end_date=propagation.contract_end_date,
            installment_current=propagation.installment_current,
            installment_total=propagation.installment_total,
            installment_value=propagation.installment_value,
            import_log_id=import_log_id,
            created_by_user_id=owner_id,
            plaid_transaction_id=a.plaid_transaction_id,
            pluggy_transaction_id=a.pluggy_transaction_id,
            pending=a.pending,
        )
    )
    rk = propagation.recurrence_kind
    rk_str = rk.value if rk is not None else "FIXED"
    if rk == RecurrenceKind.INSTALLMENT:
        rk_str += f" {propagation.installment_current}/{propagation.installment_total}"
    return 1, (
        f"History-propagated FIXED {a.activity_date} {prior.merchant.name} "
        f"${abs(amount_signed)} ({rk_str}) from prior {prior.transaction_date}"
    )


def _plaid_id_exists(session: Session, plaid_transaction_id: str | None) -> bool:
    """A row already carries this Plaid transaction id — catches re-review even
    when a split changed the stored amount (bank line vs stored installment)."""
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


def _insert_transaction(
    session: Session,
    a: CheckingActivity,
    pm: PaymentMethod,
    categorizer: Categorizer,
    import_log_id: int,
    category_override_id: int | None,
    merchant_override_id: int | None = None,
    new_merchant_name: str | None = None,
) -> int:
    match = categorizer.classify(normalize_description(a.description), a.amount)
    category_id = category_override_id if category_override_id is not None else match.category_id

    # Merchant: explicit existing id, or a new-name request (rescue label like
    # "Gym membership, annual"), else the categorizer's pick.
    if merchant_override_id is not None:
        merchant_id = merchant_override_id
    elif new_merchant_name and new_merchant_name.strip():
        merchant_id = categorizer.get_or_create_merchant(new_merchant_name.strip(), category_id).id
    elif match.merchant_id is None:
        merchant_id = categorizer.get_or_create_merchant(match.merchant_name, category_id).id
    else:
        merchant_id = match.merchant_id
    # Transactions convention: positive = charge, negative = refund.
    # On a checking statement, debits are negative-signed (outflow) and credits
    # are positive (inflow). Flip the sign so a -100 withdrawal lands as a +100
    # charge and a +302 entrada that fell through to SPENDING lands as a
    # -302 refund instead of a phantom debit.
    amount_signed = -a.amount
    owner_id = DEFAULT_OWNER_USER_ID

    if _plaid_id_exists(session, a.plaid_transaction_id) or _pluggy_id_exists(
        session, a.pluggy_transaction_id
    ):
        return 0

    # Dedup against existing transactions with the same signature.
    existing = session.scalar(
        select(Transaction).filter_by(
            transaction_date=a.activity_date,
            merchant_id=merchant_id,
            amount=amount_signed,
            payment_method_id=pm.id,
            created_by_user_id=owner_id,
        )
    )
    if existing is not None:
        return 0

    session.add(
        Transaction(
            transaction_date=a.activity_date,
            merchant_id=merchant_id,
            category_id=category_id,
            payment_method_id=pm.id,
            amount=amount_signed,
            currency=pm.currency,
            description=a.description[:500],
            installment_current=1,
            installment_total=1,
            import_log_id=import_log_id,
            created_by_user_id=owner_id,
            plaid_transaction_id=a.plaid_transaction_id,
            pluggy_transaction_id=a.pluggy_transaction_id,
            pending=a.pending,
        )
    )
    return 1


def _insert_contract_first(
    session: Session,
    a: CheckingActivity,
    pm: PaymentMethod,
    categorizer: Categorizer,
    import_log_id: int,
    conv: CheckingContractConversion,
) -> int:
    """Materialize the first installment of a CONTRACT N× series at import.

    The bank line was a single lump-sum debit (the upfront payment), but the
    user models this expense as N monthly chunks. Only 1/N is written here;
    rollover will create 2/N..N/N during normal monthly operation.
    """
    from decimal import ROUND_HALF_UP

    match = categorizer.classify(normalize_description(a.description), a.amount)
    if match.merchant_id is None:
        merchant = categorizer.get_or_create_merchant(match.merchant_name, match.category_id)
        merchant_id = merchant.id
    else:
        merchant_id = match.merchant_id

    category_id = conv.category_id if conv.category_id is not None else match.category_id
    n = conv.installments
    total = abs(Decimal(a.amount))
    per = (total / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    owner_id = DEFAULT_OWNER_USER_ID

    if _plaid_id_exists(session, a.plaid_transaction_id) or _pluggy_id_exists(
        session, a.pluggy_transaction_id
    ):
        return 0

    existing = session.scalar(
        select(Transaction).filter_by(
            transaction_date=a.activity_date,
            merchant_id=merchant_id,
            amount=per,
            payment_method_id=pm.id,
            created_by_user_id=owner_id,
        )
    )
    if existing is not None:
        return 0

    session.add(
        Transaction(
            transaction_date=a.activity_date,
            merchant_id=merchant_id,
            category_id=category_id,
            payment_method_id=pm.id,
            amount=per,
            currency=pm.currency,
            description=(a.description or "")[:500],
            installment_current=1,
            installment_total=n,
            installment_value=per,
            recurrence_kind=RecurrenceKind.CONTRACT,
            contract_end_date=conv.contract_end_date,
            import_log_id=import_log_id,
            created_by_user_id=owner_id,
            plaid_transaction_id=a.plaid_transaction_id,
            pluggy_transaction_id=a.pluggy_transaction_id,
            pending=a.pending,
        )
    )
    return 1


def _apply_cc_payment(
    session: Session,
    a: CheckingActivity,
    cards: dict[str, PaymentMethod],
) -> tuple[bool | None, str]:
    """Returns (True=applied, False=deduped, None=skipped) and an audit note."""
    card = cards.get(a.match_hint)
    if card is None:
        return None, f"CC payment {a.activity_date} ${abs(a.amount)} — no matching card for '{a.match_hint}'"
    if card.plaid_account_id is not None:
        return None, (
            f"Skipped CC payment {a.activity_date} ${abs(a.amount)} on {card.name} "
            f"— Plaid card, balance auto-refreshed (manual reduction would be overwritten)"
        )

    payment_amount = abs(a.amount)
    if _cc_payment_already_applied(session, card.id, payment_amount, a.activity_date):
        return False, f"Deduped CC payment {a.activity_date} ${payment_amount} on {card.name} (already on balance history)"

    latest = session.scalar(
        select(CreditCardBalance)
        .filter_by(payment_method_id=card.id)
        .order_by(CreditCardBalance.recorded_at.desc())
    )
    prev_balance = Decimal(latest.balance) if latest else Decimal("0")
    new_balance = prev_balance - payment_amount
    session.add(
        CreditCardBalance(
            payment_method_id=card.id,
            balance=new_balance,
            statement=latest.statement if latest else None,
            recorded_at=datetime.now(),
        )
    )
    return True, f"Reduced {card.name} balance ${prev_balance} -> ${new_balance} (-${payment_amount} on {a.activity_date})"


def _upsert_transfer_rule(
    session: Session, pm_id: int, amount: Decimal, category_id: int, merchant_id: int | None
) -> None:
    """Create/update the (payment_method, amount) → category transfer rule."""
    existing = session.scalar(
        select(TransferRule).where(
            TransferRule.payment_method_id == pm_id,
            TransferRule.amount == amount,
        )
    )
    if existing is not None:
        existing.category_id = category_id
        existing.merchant_id = merchant_id
    else:
        session.add(TransferRule(
            payment_method_id=pm_id, amount=amount,
            category_id=category_id, merchant_id=merchant_id,
        ))


def _apply_explicit_cc_payment(
    session: Session, a: CheckingActivity, card_id: int
) -> tuple[bool | None, str]:
    """The user flagged this checking row as a payment to a specific card.
    Reduce that card's balance (manual cards have no Plaid balance refresh).
    Idempotent via the same ±4d/±$1 dedup as auto-detected CC payments."""
    card = session.get(PaymentMethod, card_id)
    if card is None:
        return None, f"CC payment {a.activity_date} ${abs(a.amount)} — card #{card_id} not found"
    if card.plaid_account_id is not None:
        return None, (
            f"Skipped CC payment {a.activity_date} ${abs(a.amount)} on {card.name} "
            f"— Plaid card, balance auto-refreshed (manual reduction would be overwritten)"
        )
    payment_amount = abs(a.amount)
    if _cc_payment_already_applied(session, card.id, payment_amount, a.activity_date):
        return False, f"Deduped CC payment {a.activity_date} ${payment_amount} on {card.name} (already on balance history)"
    latest = session.scalar(
        select(CreditCardBalance)
        .filter_by(payment_method_id=card.id)
        .order_by(CreditCardBalance.recorded_at.desc())
    )
    prev_balance = Decimal(latest.balance) if latest else Decimal("0")
    new_balance = prev_balance - payment_amount
    session.add(
        CreditCardBalance(
            payment_method_id=card.id,
            balance=new_balance,
            statement=latest.statement if latest else None,
            recorded_at=datetime.now(),
        )
    )
    return True, f"CC payment {a.activity_date} ${payment_amount} → {card.name} balance ${prev_balance} -> ${new_balance}"


def _apply_withholding_adjustments(session: Session, recon: SalaryReconciliationOut) -> int:
    count = 0
    for adj in recon.proposed_adjustments:
        t = session.get(Transaction, adj["transaction_id"])
        if t is None:
            continue
        new_val = Decimal(adj["new"])
        # Only adjust if the change is meaningful.
        if abs(Decimal(t.amount) - new_val) < Decimal("0.01"):
            continue
        t.amount = new_val
        # If the row is part of an installment series, keep installment_value
        # in sync with the FIXED amount.
        if t.installment_total > 1:
            t.installment_value = new_val
        count += 1
    return count


def _record_primary_salary(
    session: Session, a: CheckingActivity, pm: PaymentMethod
) -> str:
    """Auto-create income_entries.PRIMARY_SALARY for month+1 (lag-1 convention).

    A gross-deposit member's pay varies month-to-month; they have
    no US-style withholdings to adjust, so the existing
    _build_salary_reconciliation flow returns None for him. This helper
    creates the income row with amount = deposit value when missing.
    Idempotent: a pre-existing row for the period is left untouched.
    """
    if a.activity_date.month == 12:
        period_year, period_month = a.activity_date.year + 1, 1
    else:
        period_year, period_month = a.activity_date.year, a.activity_date.month + 1

    existing = session.scalar(
        select(IncomeEntry).filter_by(
            year=period_year, month=period_month, source=IncomeSource.PRIMARY_SALARY
        )
    )
    amount = abs(a.amount)
    month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[period_month - 1]
    if existing is not None:
        return (
            f"SALARY {a.activity_date} ${amount}: income_entries for "
            f"{month_name} {period_year} PRIMARY_SALARY already set to "
            f"${Decimal(existing.amount)} — left as-is"
        )

    session.add(
        IncomeEntry(
            year=period_year,
            month=period_month,
            source=IncomeSource.PRIMARY_SALARY,
            amount=amount,
            currency=pm.currency,
            exchange_rate_id=None,
        )
    )
    return (
        f"SALARY {a.activity_date} ${amount}: created income_entries for "
        f"{month_name} {period_year} PRIMARY_SALARY"
    )


def _ensure_partner_salary(session: Session, a: CheckingActivity, member) -> str:
    """Auto-create the member's income row for month+1 (lag-1 convention).

    Their gross is fixed per pay level (`salary_levels`) — only the withholdings
    move month to month, so unlike a variable deposit the amount is never taken
    from the statement. Mirrors _record_primary_salary. Idempotent: a
    pre-existing row for the period is left untouched.
    """
    if a.activity_date.month == 12:
        period_year, period_month = a.activity_date.year + 1, 1
    else:
        period_year, period_month = a.activity_date.year, a.activity_date.month + 1

    source = member.salary_income_source
    existing = session.scalar(
        select(IncomeEntry).filter_by(
            year=period_year, month=period_month, source=source
        )
    )
    month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[period_month - 1]
    if existing is not None:
        return (
            f"SALARY {a.activity_date}: income_entries for {month_name} "
            f"{period_year} {source.name} already ${Decimal(existing.amount)} — left as-is"
        )

    gross = household.gross_for_month(member, period_year, period_month)
    if gross is None:
        return (
            f"SALARY {a.activity_date}: no salary level configured for "
            f"{member.display_name} in {month_name} {period_year} — income row not created"
        )
    session.add(
        IncomeEntry(
            year=period_year,
            month=period_month,
            source=source,
            amount=gross,
            currency=Currency.USD,
            exchange_rate_id=None,
        )
    )
    return (
        f"SALARY {a.activity_date}: created income_entries for {month_name} "
        f"{period_year} {source.name} ${gross}"
    )


def _record_extra_income(
    session: Session, a: CheckingActivity, pm: PaymentMethod
) -> str:
    """Auto-create / accumulate `income_entries.EXTRA_USD` or `EXTRA_BRL`
    for the deposit month.

    Unlike SALARY / RENT_DEPOSIT (lag-1), ad-hoc Pix/refunds are booked
    against the calendar month they arrived. UNIQUE(year, month, source)
    no longer collides across currencies — USD and BRL extras live in
    separate buckets. Within the same currency we ACCUMULATE: if a row
    already exists, we add the new amount to it (multiple Pix in the same
    month sum up). User feedback in 2026-05-22 session."""
    if a.amount <= 0:
        return (
            f"EXTRA_INCOME {a.activity_date} ${a.amount}: skipped — debit "
            f"(only positive Transferência Recebida feeds EXTRA)"
        )
    year, month = a.activity_date.year, a.activity_date.month
    source = (
        IncomeSource.EXTRA_USD if pm.currency == Currency.USD else IncomeSource.EXTRA_BRL
    )
    # Session has autoflush=False; flush so a row created by a previous
    # activity in the same paste is visible (otherwise two same-month BRL
    # Pix in one import would each insert -> UNIQUE violation).
    session.flush()
    existing = session.scalar(
        select(IncomeEntry).filter_by(year=year, month=month, source=source)
    )
    amount = abs(a.amount)
    month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[month - 1]
    if existing is not None:
        # Plaid re-pulls the full window on every commit, so accumulating would
        # inflate on re-sync (it once ballooned June EXTRA_USD to $9,000). For
        # a Plaid-sourced row, leave the existing total untouched (idempotent).
        # Manual paste keeps accumulating (multiple Pix in a month sum up).
        if a.plaid_transaction_id is not None:
            return (
                f"EXTRA_INCOME {a.activity_date} ${amount} {pm.currency.value}: "
                f"{month_name} {year} {source.value} already set to "
                f"${Decimal(existing.amount)} — left as-is (Plaid, idempotent)"
            )
        prev = Decimal(existing.amount)
        existing.amount = prev + amount
        return (
            f"EXTRA_INCOME {a.activity_date} +${amount} {pm.currency.value}: "
            f"accumulated into {month_name} {year} {source.value} "
            f"(${prev} -> ${prev + amount})"
        )
    session.add(
        IncomeEntry(
            year=year,
            month=month,
            source=source,
            amount=amount,
            currency=pm.currency,
            exchange_rate_id=None,
        )
    )
    return (
        f"EXTRA_INCOME {a.activity_date} ${amount} {pm.currency.value}: "
        f"created income_entries for {month_name} {year} {source.value}"
    )


def _record_rent_deposit(
    session: Session, a: CheckingActivity, pm: PaymentMethod
) -> str:
    """Auto-create income_entries.RENTS_BRAZIL for month+1 (lag-1 convention).

    Idempotent: if a row already exists for the target (year, month, RENTS_BRAZIL)
    we leave it alone — the user owns that value once it's set.
    """
    if a.amount <= 0:
        return (
            f"RENT_DEPOSIT {a.activity_date} ${a.amount}: skipped — debit "
            f"(payer's name on an outbound transfer, not an incoming deposit)"
        )
    if a.activity_date.month == 12:
        period_year, period_month = a.activity_date.year + 1, 1
    else:
        period_year, period_month = a.activity_date.year, a.activity_date.month + 1

    existing = session.scalar(
        select(IncomeEntry).filter_by(
            year=period_year, month=period_month, source=IncomeSource.RENTS_BRAZIL
        )
    )
    amount = abs(a.amount)
    month_name = ("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")[period_month - 1]
    if existing is not None:
        return (
            f"RENT_DEPOSIT {a.activity_date} ${amount}: income_entries for "
            f"{month_name} {period_year} RENTS_BRAZIL already set to "
            f"${Decimal(existing.amount)} — left as-is"
        )

    session.add(
        IncomeEntry(
            year=period_year,
            month=period_month,
            source=IncomeSource.RENTS_BRAZIL,
            amount=amount,
            currency=pm.currency,
            exchange_rate_id=None,
        )
    )
    return (
        f"RENT_DEPOSIT {a.activity_date} ${amount}: created income_entries for "
        f"{month_name} {period_year} RENTS_BRAZIL"
    )


def _append_snapshot(session: Session, pm: PaymentMethod, parsed: CheckingParseResult) -> bool:
    """Append a SavingsSnapshot for period_end with the ending balance.
    Idempotent — skip if a snapshot for this account on the same date already
    exists with the same balance."""
    period_end_dt = datetime.combine(parsed.period_end, datetime.min.time())
    existing = session.scalar(
        select(SavingsSnapshot).where(
            SavingsSnapshot.account_name == pm.name,
            SavingsSnapshot.recorded_at >= period_end_dt,
            SavingsSnapshot.recorded_at < period_end_dt + timedelta(days=1),
        )
    )
    if existing is not None and Decimal(existing.balance) == Decimal(parsed.ending_balance):
        return False
    session.add(
        SavingsSnapshot(
            account_name=pm.name,
            currency=pm.currency,
            balance=parsed.ending_balance,
            recorded_at=period_end_dt,
        )
    )
    return True
