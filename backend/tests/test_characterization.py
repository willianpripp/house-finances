"""Characterization tests: they pin down what the app does *today*.

Their job is to make the Phase 1 refactor (the public-export plan — pulling the
household out of the code and into config) provable rather than hopeful. They
assert current behaviour, not desired behaviour; when Phase 1 changes an
interface on purpose, the matching assertion changes with it and the diff shows
exactly what moved.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import IncomeSource, ReceivableDirection
from app.services.reports import monthly_report
from app.services.rollover import preview_rollover
from tests.factories import (
    ACCOUNTANT_FEE,
    CAR_INSTALLMENT,
    FED_WITHHOLDING,
    PARTNER_GROSS,
    PARTNER_NAME,
    PARTNER_SALARY,
    PRIMARY_NAME,
    PRIMARY_SALARY,
    STATE_WITHHOLDING,
)


# Report fields are named after the household members today. Phase 1 renames
# them to roles; these four accessors are the only lines the tests need then.
def _primary_salary(r):
    return r.totals.primary_salary_usd


def _partner_salary(r):
    return r.totals.partner_salary_usd


def _taxes_primary(r):
    return r.totals.taxes_primary_usd


def _taxes_partner(r):
    return r.totals.taxes_partner_usd


# --------------------------------------------------------------- income model
def test_income_sources_are_role_based_not_person_based():
    """Phase 1, Class A: no household member is named in the income model."""
    assert PRIMARY_SALARY.value == "primary_salary"
    assert PARTNER_SALARY.value == "partner_salary"
    assert {s.value for s in IncomeSource} == {
        "primary_salary",
        "partner_salary",
        "rents_brazil",
        "extra_usd",
        "extra_brl",
    }


def test_household_names_are_fictional_and_nothing_breaks():
    """Phase 1's finish line. The fixture household is fictional, and the whole
    suite still passes — proof that no code path matches on a member's name.
    The source sweep that used to live here is now
    `test_match_rules.test_no_member_name_survives_anywhere_in_the_app`, which
    walks every module and template instead of three services."""
    assert PRIMARY_NAME == "Alex Costa"
    assert PARTNER_NAME == "Sam Costa"


# ------------------------------------------------------------ monthly reports
def test_monthly_report_totals(db):
    r = monthly_report(db, 2026, 7)
    t = r.totals

    assert _partner_salary(r) == PARTNER_GROSS
    # Foreign salary converts at the effective rate (the effective rate).
    assert _primary_salary(r) > Decimal("1700")
    assert t.gross_income_usd == _primary_salary(r) + _partner_salary(r)

    # USD withholdings attach to the partner, foreign taxes to the primary earner.
    assert _taxes_partner(r) == FED_WITHHOLDING + STATE_WITHHOLDING
    assert _taxes_primary(r) > Decimal("0")
    assert t.net_income_usd == t.gross_income_usd - t.taxes_usd


def test_monthly_report_is_recomputed_not_snapshotted(db):
    """Reports read live from transactions; only Jan-Apr 2026 are frozen."""
    first = monthly_report(db, 2026, 7)
    second = monthly_report(db, 2026, 7)
    assert first.totals.gross_income_usd == second.totals.gross_income_usd


# -------------------------------------------------------------------- rollover
def test_rollover_rolls_installments_and_indefinite_tax_fees(db):
    """Locks in the 2026-08-01 fix: the Taxes branch keys on recurrence_kind,
    so a fixed monthly fee filed under Taxes rolls, and a variable tax payment
    does not."""
    preview = preview_rollover(db, 2026, 7)
    by_merchant = {i.merchant_name: i for i in preview.items}

    assert "Accountant" in by_merchant, "INDEFINITE Taxes fee must roll"
    assert by_merchant["Accountant"].amount == ACCOUNTANT_FEE
    assert by_merchant["Accountant"].suggested_target_date == date(2026, 8, 14)

    assert "Car Loan" in by_merchant, "INSTALLMENT series must roll"
    assert by_merchant["Car Loan"].amount == CAR_INSTALLMENT

    assert "Federal Withholding" in by_merchant
    assert "State Withholding" in by_merchant

    assert "Tax Authority" not in by_merchant, "variable tax payment must not roll"
    assert "Streaming Co" not in by_merchant, "non-Taxes INDEFINITE comes from the importer"
    assert "Market" not in by_merchant, "VARIABLE rows never roll"


def test_rollover_targets_the_next_month(db):
    preview = preview_rollover(db, 2026, 7)
    assert (preview.target_year, preview.target_month) == (2026, 8)


# ---------------------------------------------------------------- receivables
def test_receivables_summary_nets_both_directions(client):
    rows = client.get("/api/receivables/summary").json()
    by_person = {r["person_name"]: r for r in rows}

    a = by_person["Person A"]
    assert Decimal(a["owed_to_me"]) == Decimal("80.00")
    assert Decimal(a["i_owe"]) == Decimal("30.00")
    assert Decimal(a["net_amount"]) == Decimal("50.00")
    assert a["open_count"] == 2

    b = by_person["Person B"]
    assert Decimal(b["net_amount"]) == Decimal("45.00")


def test_receivables_summary_never_mixes_currencies(client):
    """A person owing in USD and BRL gets one summary row per currency;
    the nets are never summed across currencies."""
    people = client.get("/api/receivables/people").json()
    person_a = next(p for p in people if p["name"] == "Person A")
    created = client.post(
        "/api/receivables",
        json={
            "description": "Farmacia",
            "charge_date": "2026-07-21",
            "currency": "BRL",
            "direction": "OWED_TO_ME",
            "shares": [{"person_id": person_a["id"], "amount": "200.00"}],
        },
    ).json()
    try:
        rows = client.get("/api/receivables/summary").json()
        a_rows = {r["currency"]: r for r in rows if r["person_name"] == "Person A"}
        assert set(a_rows) == {"USD", "BRL"}
        assert Decimal(a_rows["USD"]["net_amount"]) == Decimal("50.00")
        assert Decimal(a_rows["BRL"]["net_amount"]) == Decimal("200.00")
        assert a_rows["BRL"]["owed_to_me_count"] == 1
    finally:
        client.delete(f"/api/receivables/{created[0]['id']}")


def test_receivables_direction_filter(client):
    owed = client.get(
        "/api/receivables", params={"direction": ReceivableDirection.OWED_TO_ME.value}
    ).json()
    mine = client.get(
        "/api/receivables", params={"direction": ReceivableDirection.I_OWE.value}
    ).json()

    assert len(owed) == 2
    assert len(mine) == 1
    assert all(r["direction"] == "OWED_TO_ME" for r in owed)


def test_i_owe_rows_never_carry_one_of_our_cards(client):
    created = client.post(
        "/api/receivables",
        json={
            "description": "Taxi",
            "charge_date": "2026-07-20",
            "currency": "USD",
            "direction": "I_OWE",
            "payment_method_id": 1,
            "shares": [{"person_id": 1, "amount": "12.00"}],
        },
    ).json()
    assert created[0]["payment_method_id"] is None
    client.delete(f"/api/receivables/{created[0]['id']}")


# ------------------------------------------------------------ household config
def test_salary_level_lookup_picks_the_level_in_force(db):
    """A raise applies from its effective month on; earlier months keep the
    older gross, which is what makes historical reconciliation correct."""
    from app.models import HouseholdRole
    from app.services import household
    from tests.factories import PARTNER_GROSS_AFTER_RAISE

    member = household.member_by_role(db, HouseholdRole.PARTNER)
    assert household.gross_for_month(member, 2026, 7) == PARTNER_GROSS
    assert household.gross_for_month(member, 2026, 8) == PARTNER_GROSS_AFTER_RAISE
    assert household.gross_for_month(member, 2027, 3) == PARTNER_GROSS_AFTER_RAISE


def test_member_resolves_from_the_parser_match_key(db):
    """The importer finds the earner via config, never via users.name."""
    from app.services import household

    member = household.member_by_match_key(db, PARTNER_NAME.split()[0])
    assert member is not None
    assert member.has_withholdings is True
    assert member.salary_income_source is PARTNER_SALARY
    assert household.member_by_match_key(db, "nobody") is None


def test_withholding_merchants_come_from_config(db):
    from app.models import HouseholdRole
    from app.services import household

    member = household.member_by_role(db, HouseholdRole.PARTNER)
    assert household.withholding_merchant_names(db, member) == (
        "Federal Withholding",
        "State Withholding",
    )


def test_salary_labels_reach_both_uis_from_the_household_config(client):
    """The income and monthly-report pages used to spell the members out in
    their JavaScript. Both UIs now take the labels from `household_members`,
    so renaming a member in the database moves the label on both screens."""
    phone = {"user-agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobi Safari"}
    for headers in ({}, phone):
        for path in ("/income", "/reports/monthly"):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, path
            assert f"{PRIMARY_NAME.split()[0]} Salary" in response.text, path
            assert f"{PARTNER_NAME.split()[0]} Salary" in response.text, path
