"""Squashed baseline schema for the public-repo export.

This is an EXPORT ARTIFACT, not part of the private repo's real migration
history. It is regenerated at export time
from the current models via autogenerate against an empty database, then
hand-patched so its result is schema-identical to running the private repo's
full chain (`alembic upgrade head`) — verified by `make verify-baseline`
(scripts/lab/verify_public_baseline.sh). A fresh public clone runs this single
file instead of replaying 30+ incremental migrations that only make sense
against the private repo's real history.

ONE deliberate divergence, and the verify script knows about it: the
`import_source` enum type carries only the labels a tree with three parsers
can emit, so it is a strict SUBSET of the private chain's type rather than
equal to it (the export ships three reference parsers, so the full institution label
list said out loud what shipping eleven parsers only implied). The private
chain is not touched: prod's `import_logs` rows keep their labels and no
migration renames or drops a value. Everything else in this file is still
compared for exact equality.

Column order, constraint names and index definitions below intentionally
follow the private chain's actual on-disk result rather than the models'
declaration order — several columns were added by later ALTER TABLE
migrations and so sit at the end of their table on the real database, and a
few enum labels were relabeled in place by a later migration, which keeps a
label at its original position rather than the model's. The surviving
`import_source` labels keep the private type's relative order for the same
reason, which is also what makes the subset check above a straight
`comm` of two sorted label lists.

`transactions` dedup is a 3-way split of PARTIAL unique indexes (manual
signature dedup vs. Plaid vs. Pluggy provider ids), not the single plain
UNIQUE constraint autogenerate would suggest from the model's
`__table_args__` alone — see 2026_06_11_1200-plaid_auto_pull.py and
2026_08_08_1300-pluggy_auto_pull.py in the private chain.

`payment_methods` and `pluggy_items` have a genuine FK cycle
(`payment_methods.pluggy_item_id` -> `pluggy_items.id`,
`pluggy_items.investments_payment_method_id` -> `payment_methods.id`), so the
two `payment_methods` provider-linkage FKs are added after both target
tables exist rather than inline in its `CREATE TABLE`.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("type", sa.Enum("VARIABLE", "FIXED", name="category_type"), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("icon", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("exclude_from_spending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("default_category_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["default_category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("commercial", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("spread", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("iof", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("effective", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rate_date"),
    )

    # plaid_item_id / pluggy_item_id FKs deferred below: pluggy_items has a
    # FK back to payment_methods (investments_payment_method_id), a genuine
    # cycle that CREATE TABLE alone can't express.
    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "type",
            sa.Enum("CREDIT_CARD", "CHECKING", "SAVINGS", "INVESTMENT", "CASH", "OTHER", name="payment_method_type"),
            nullable=False,
        ),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("paid_from_payment_method_id", sa.Integer(), nullable=True),
        sa.Column("statement_close_day", sa.Integer(), nullable=True),
        sa.Column("due_day", sa.Integer(), nullable=True),
        sa.Column("plaid_item_id", sa.Integer(), nullable=True),
        sa.Column("plaid_account_id", sa.String(length=100), nullable=True),
        sa.Column("pluggy_item_id", sa.Integer(), nullable=True),
        sa.Column("pluggy_account_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["paid_from_payment_method_id"],
            ["payment_methods.id"],
            ondelete="SET NULL",
            name="fk_payment_methods_paid_from",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("plaid_account_id", name="uq_payment_methods_plaid_account_id"),
        sa.UniqueConstraint("pluggy_account_id", name="uq_payment_methods_pluggy_account_id"),
    )

    op.create_table(
        "plaid_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=100), nullable=False),
        sa.Column("institution_id", sa.String(length=50), nullable=False),
        sa.Column("institution_name", sa.String(length=200), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("last_cursor", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "LOGIN_REQUIRED", "REVOKED", name="plaid_item_status"),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_skipped_unmapped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_plaid_items_item_id"),
    )
    op.create_index(op.f("ix_plaid_items_user_id"), "plaid_items", ["user_id"], unique=False)

    op.create_table(
        "pluggy_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("connector_id", sa.Integer(), nullable=True),
        sa.Column("connector_name", sa.String(length=200), server_default="", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("investments_payment_method_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["investments_payment_method_id"],
            ["payment_methods.id"],
            ondelete="SET NULL",
            name="fk_pluggy_items_investments_payment_method_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uq_pluggy_items_item_id"),
    )
    op.create_index(op.f("ix_pluggy_items_user_id"), "pluggy_items", ["user_id"], unique=False)

    # Deferred payment_methods provider-linkage FKs (see cycle note above).
    op.create_foreign_key(
        "payment_methods_plaid_item_id_fkey",
        "payment_methods",
        "plaid_items",
        ["plaid_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_payment_methods_pluggy_item_id",
        "payment_methods",
        "pluggy_items",
        ["pluggy_item_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("relation", sa.String(length=40), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_people_name"),
    )

    op.create_table(
        "household_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Enum("PRIMARY", "PARTNER", name="household_role"), nullable=False),
        sa.Column("match_key", sa.String(length=80), nullable=False),
        sa.Column(
            "salary_income_source",
            sa.Enum(
                "PRIMARY_SALARY", "PARTNER_SALARY", "RENTS_BRAZIL", "EXTRA_USD", "EXTRA_BRL", name="income_source"
            ),
            nullable=False,
        ),
        sa.Column("has_withholdings", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("salary_checking_pm_id", sa.Integer(), nullable=True),
        sa.Column("salary_day_of_month", sa.Integer(), server_default="99", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["salary_checking_pm_id"], ["payment_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_key"),
        sa.UniqueConstraint("role"),
        sa.UniqueConstraint("user_id"),
    )

    op.create_table(
        "salary_levels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("effective_year", sa.Integer(), nullable=False),
        sa.Column("effective_month", sa.Integer(), nullable=False),
        sa.Column("gross", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), server_default="USD", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["household_members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "effective_year", "effective_month", name="uq_salary_level_member_month"),
    )
    op.create_index(op.f("ix_salary_levels_member_id"), "salary_levels", ["member_id"], unique=False)

    op.create_table(
        "withholding_merchants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["household_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "merchant_id", name="uq_withholding_member_merchant"),
    )
    op.create_index(op.f("ix_withholding_merchants_member_id"), "withholding_merchants", ["member_id"], unique=False)

    op.create_table(
        "transfer_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_method_id", "amount", name="uq_transfer_rule_pm_amount"),
    )
    op.create_index(op.f("ix_transfer_rules_payment_method_id"), "transfer_rules", ["payment_method_id"], unique=False)

    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(length=100), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("keyword", "amount", name="uq_rule_keyword_amount"),
    )

    op.create_table(
        "credit_card_balances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=False),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("statement", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_credit_card_balances_payment_method_id"), "credit_card_balances", ["payment_method_id"], unique=False
    )

    op.create_table(
        "import_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            # The ONE place this baseline is deliberately not a copy of the
            # private chain's schema: the labels are the private type's, minus
            # the ten belonging to parsers this tree does not ship
            # Relative order is preserved, so
            # the set is a strict subset and `make verify-baseline` asserts
            # exactly that instead of equality for this one type.
            sa.Enum(
                "CITI",
                "AMAZON",
                "CAR_LOAN",
                "MANUAL",
                "NUBANK_CREDITO",
                "CHECKING_NUBANK",
                "PLAID",
                "PLUGGY",
                name="import_source",
            ),
            nullable=False,
        ),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_import_logs_payment_method_id"), "import_logs", ["payment_method_id"], unique=False)

    op.create_table(
        "income_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "PRIMARY_SALARY", "PARTNER_SALARY", "RENTS_BRAZIL", "EXTRA_USD", "EXTRA_BRL", name="income_source"
            ),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), nullable=False),
        sa.Column("exchange_rate_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["exchange_rate_id"], ["exchange_rates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "month", "source", name="uq_income_period_source"),
    )
    op.create_index(op.f("ix_income_entries_year"), "income_entries", ["year"], unique=False)

    op.create_table(
        "monthly_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("primary_salary_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("partner_salary_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("rents_brazil_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("extra_income_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("fixed_spending_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("variable_spending_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("taxes_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("net_income_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("surplus_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("total_savings_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("total_debt_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("net_worth_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("exchange_rate_id", sa.Integer(), nullable=True),
        sa.Column("is_finalized", sa.Boolean(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["exchange_rate_id"], ["exchange_rates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("year", "month", name="uq_snapshot_period"),
    )
    op.create_index(op.f("ix_monthly_snapshots_year"), "monthly_snapshots", ["year"], unique=False)

    op.create_table(
        "receivables",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), server_default="USD", nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("store", sa.String(length=120), nullable=True),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("charge_date", sa.Date(), nullable=False),
        sa.Column("settled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("OWED_TO_ME", "I_OWE", name="receivable_direction"),
            server_default="OWED_TO_ME",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_receivables_direction"), "receivables", ["direction"], unique=False)
    op.create_index(op.f("ix_receivables_group_id"), "receivables", ["group_id"], unique=False)
    op.create_index(op.f("ix_receivables_person_id"), "receivables", ["person_id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("merchant_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("installment_current", sa.Integer(), nullable=False),
        sa.Column("installment_total", sa.Integer(), nullable=False),
        sa.Column("installment_value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("import_log_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "recurrence_kind",
            sa.Enum("INDEFINITE", "CONTRACT", "INSTALLMENT", "EXTRA_PRINCIPAL", name="recurrence_kind"),
            nullable=True,
        ),
        sa.Column("contract_end_date", sa.Date(), nullable=True),
        sa.Column("plaid_transaction_id", sa.String(length=100), nullable=True),
        sa.Column("pending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("pluggy_transaction_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["import_log_id"], ["import_logs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_transactions_transaction_date"), "transactions", ["transaction_date"], unique=False)
    # Dedup is a 3-way split (not a single UNIQUE constraint): manual/parsed
    # rows dedupe on the owner-based signature only when NEITHER provider id
    # is set; Plaid and Pluggy rows each dedupe on their own stable id, so two
    # legitimately identical provider charges (e.g. two same-day metro
    # tickets) can coexist. Mirrors 2026_06_11_1200-plaid_auto_pull.py and
    # 2026_08_08_1300-pluggy_auto_pull.py in the private chain.
    op.create_index(
        "uq_transaction_signature_manual",
        "transactions",
        ["transaction_date", "merchant_id", "amount", "payment_method_id", "created_by_user_id"],
        unique=True,
        postgresql_where=sa.text("plaid_transaction_id IS NULL"),
    )
    op.create_index(
        "uq_transactions_plaid_transaction_id",
        "transactions",
        ["plaid_transaction_id"],
        unique=True,
        postgresql_where=sa.text("plaid_transaction_id IS NOT NULL"),
    )
    op.create_index(
        "uq_transactions_pluggy_transaction_id",
        "transactions",
        ["pluggy_transaction_id"],
        unique=True,
        postgresql_where=sa.text("pluggy_transaction_id IS NOT NULL"),
    )

    op.create_table(
        "plaid_seen_transactions",
        sa.Column("plaid_transaction_id", sa.String(length=255), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("plaid_transaction_id"),
    )

    op.create_table(
        "pluggy_seen_transactions",
        sa.Column("pluggy_transaction_id", sa.String(length=64), nullable=False),
        sa.Column("payment_method_id", sa.Integer(), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_method_id"], ["payment_methods.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("pluggy_transaction_id"),
    )

    op.create_table(
        "savings_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_name", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), nullable=False),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_savings_snapshots_account_name"), "savings_snapshots", ["account_name"], unique=False)

    op.create_table(
        "statement_match_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("keyword", sa.String(length=120), nullable=False),
        sa.Column("match_hint", sa.String(length=100), server_default="", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="100", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("classification", "keyword"),
    )
    op.create_index(
        op.f("ix_statement_match_rules_classification"), "statement_match_rules", ["classification"], unique=False
    )

    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.Enum("VEHICLE", "PROPERTY", "ELECTRONICS", "JEWELRY", "OTHER", name="asset_kind"), nullable=False),
        sa.Column("location", sa.String(length=120), nullable=True),
        sa.Column("acquired_date", sa.Date(), nullable=True),
        sa.Column("current_value", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Enum("USD", "BRL", name="currency"), nullable=False),
        sa.Column("last_valued_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_service_date", sa.Date(), nullable=True),
        sa.Column("next_service_due_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "car_loan_payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("posting_date", sa.Date(), nullable=False),
        sa.Column("payment_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("principal_paid", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("interest_paid", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("new_balance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_car_loan_payments_posting_date"), "car_loan_payments", ["posting_date"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_car_loan_payments_posting_date"), table_name="car_loan_payments")
    op.drop_table("car_loan_payments")
    op.drop_table("assets")
    op.drop_index(op.f("ix_statement_match_rules_classification"), table_name="statement_match_rules")
    op.drop_table("statement_match_rules")
    op.drop_index(op.f("ix_savings_snapshots_account_name"), table_name="savings_snapshots")
    op.drop_table("savings_snapshots")
    op.drop_table("pluggy_seen_transactions")
    op.drop_table("plaid_seen_transactions")
    op.drop_index("uq_transactions_pluggy_transaction_id", table_name="transactions")
    op.drop_index("uq_transactions_plaid_transaction_id", table_name="transactions")
    op.drop_index("uq_transaction_signature_manual", table_name="transactions")
    op.drop_index(op.f("ix_transactions_transaction_date"), table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_receivables_person_id"), table_name="receivables")
    op.drop_index(op.f("ix_receivables_group_id"), table_name="receivables")
    op.drop_index(op.f("ix_receivables_direction"), table_name="receivables")
    op.drop_table("receivables")
    op.drop_index(op.f("ix_monthly_snapshots_year"), table_name="monthly_snapshots")
    op.drop_table("monthly_snapshots")
    op.drop_index(op.f("ix_income_entries_year"), table_name="income_entries")
    op.drop_table("income_entries")
    op.drop_index(op.f("ix_import_logs_payment_method_id"), table_name="import_logs")
    op.drop_table("import_logs")
    op.drop_index(op.f("ix_credit_card_balances_payment_method_id"), table_name="credit_card_balances")
    op.drop_table("credit_card_balances")
    op.drop_table("categorization_rules")
    op.drop_index(op.f("ix_transfer_rules_payment_method_id"), table_name="transfer_rules")
    op.drop_table("transfer_rules")
    op.drop_index(op.f("ix_withholding_merchants_member_id"), table_name="withholding_merchants")
    op.drop_table("withholding_merchants")
    op.drop_index(op.f("ix_salary_levels_member_id"), table_name="salary_levels")
    op.drop_table("salary_levels")
    op.drop_table("household_members")
    op.drop_table("people")

    # Drop the deferred FKs before the tables they point at (FK cycle note
    # in upgrade()).
    op.drop_constraint("fk_payment_methods_pluggy_item_id", "payment_methods", type_="foreignkey")
    op.drop_constraint("payment_methods_plaid_item_id_fkey", "payment_methods", type_="foreignkey")
    op.drop_index(op.f("ix_pluggy_items_user_id"), table_name="pluggy_items")
    op.drop_table("pluggy_items")
    op.drop_index(op.f("ix_plaid_items_user_id"), table_name="plaid_items")
    op.drop_table("plaid_items")
    op.drop_table("payment_methods")
    op.drop_table("exchange_rates")
    op.drop_table("merchants")
    op.drop_table("categories")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_name in (
        "recurrence_kind",
        "receivable_direction",
        "plaid_item_status",
        "payment_method_type",
        "import_source",
        "income_source",
        "household_role",
        "currency",
        "category_type",
        "asset_kind",
    ):
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)
