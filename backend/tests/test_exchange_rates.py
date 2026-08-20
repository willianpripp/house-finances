"""Exchange rate service/router: settings-driven spread/iof, and the
manual-vs-ptax source tag on rows however they were created. create_rate()
has no HTTP surface (2026-08-20: manual entry removed, only the PTAX
scripts call it) — router coverage here is limited to what is still
exposed: GET (list) and DELETE. GET /defaults and the dedicated
/exchange-rates page are both gone (2026-08-20: the page was read-only
dead weight, one path for data the monthly report already shows;
/defaults had no caller left once the page was cut). The list GET stays
because /assets, /savings and /income fetch it directly. The PTAX refresh
script's never-overwrite behaviour and --backfill mode have their own
coverage in test_refresh_exchange_rate.py; the PTAX payload parsing/
fallback logic in test_ptax_client.py."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import ExchangeRate
from app.services.exchange_rates import compute_effective, create_rate


# ---------- compute_effective: unchanged behaviour after the constants move ----------

def test_effective_computation_unchanged():
    # 5.0000 * 1.015 * 1.011 = 5.130825 -> rounds to 5.1308 (ROUND_HALF_UP)
    assert compute_effective(
        Decimal("5.0000"), Decimal("0.015"), Decimal("0.011")
    ) == Decimal("5.1308")


# ---------- spread/iof now come from settings, not hardcoded module constants ----------

def test_create_rate_uses_settings_defaults_not_hardcoded(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "exchange_rate_spread", "0.02")
    monkeypatch.setattr(settings, "exchange_rate_iof", "0.005")

    row = create_rate(db, rate_date=date(2026, 8, 19), commercial=Decimal("5.10"))
    try:
        assert row.spread == Decimal("0.02")
        assert row.iof == Decimal("0.005")
        assert row.effective == compute_effective(
            Decimal("5.10"), Decimal("0.02"), Decimal("0.005")
        )
    finally:
        db.delete(db.get(ExchangeRate, row.id))
        db.commit()


def test_defaults_endpoint_is_gone(client):
    """GET /defaults existed only to feed the removed /exchange-rates page
    (spread/IOF preview). No other consumer ever called it. The path
    coincidentally matches the DELETE /{rate_id} pattern (untyped at the
    routing layer), so a disallowed-method 405 is as valid a "not there" as
    a 404 — same convention as test_router_has_no_create_or_edit_endpoint."""
    r = client.get("/api/exchange-rates/defaults")
    assert r.status_code in (404, 405)


# ---------- manual vs auto origin ----------

def test_create_rate_defaults_source_to_manual(db):
    """create_rate()'s default is unreachable from the running app (both
    callers, the daily refresh and --backfill, always pass source='ptax'),
    but it is what a historical/import row without an explicit source would
    get, so it stays covered."""
    row = create_rate(db, rate_date=date(2026, 8, 19), commercial=Decimal("5.10"))
    try:
        assert row.source == "manual"
    finally:
        db.delete(db.get(ExchangeRate, row.id))
        db.commit()


def test_create_rate_accepts_ptax_source(db):
    row = create_rate(
        db, rate_date=date(2026, 8, 19), commercial=Decimal("5.10"), source="ptax"
    )
    try:
        assert row.source == "ptax"
    finally:
        db.delete(db.get(ExchangeRate, row.id))
        db.commit()


# ---------- the read-only HTTP surface ----------

def test_router_has_no_create_or_edit_endpoint(client):
    """The manual create/PATCH HTTP surface is gone, not just hidden from
    the UI. A matching-path route with a disallowed method 405s; no route
    at all 404s — either is an acceptable "not there" here."""
    r = client.post(
        "/api/exchange-rates",
        json={"rate_date": "2026-08-19", "commercial": "5.10"},
    )
    assert r.status_code in (404, 405)

    r = client.patch("/api/exchange-rates/1", json={"commercial": "5.11"})
    assert r.status_code in (404, 405)


def test_router_delete_still_works_as_the_correction_path(client, db):
    """DELETE survives: it is not manual entry, it is how a bad auto-fetched
    row gets cleared so the next refresh/backfill run can re-fill the date."""
    row = create_rate(
        db, rate_date=date(2026, 8, 19), commercial=Decimal("5.10"), source="ptax"
    )
    r = client.delete(f"/api/exchange-rates/{row.id}")
    assert r.status_code == 204

    # Fresh read through the API (not the `db` fixture's own session/identity
    # map, which would still show the row it just loaded above as present).
    ids = [r["id"] for r in client.get("/api/exchange-rates").json()]
    assert row.id not in ids


def test_page_route_is_gone(client):
    """The dedicated /exchange-rates page is removed (2026-08-20): it was a
    read-only listing of data the monthly report already shows, and rates
    are now fully automated (PTAX auto-fetch, never hand-typed)."""
    r = client.get("/exchange-rates")
    assert r.status_code == 404
