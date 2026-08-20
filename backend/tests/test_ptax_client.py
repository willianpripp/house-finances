"""PTAX client: payload parsing and the weekend/holiday fallback. httpx.get is
monkeypatched throughout — this suite asserts a no-network guard (see
test_no_external_assets.py's sibling concern for templates), so nothing here
may reach the real olinda.bcb.gov.br."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services import ptax_client
from app.services.ptax_client import (
    PtaxError,
    fetch_closing_rates_range,
    fetch_latest_closing_rate,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _row(day: str, venda: str, hour: str = "13:10:22.642754"):
    return {
        "cotacaoCompra": float(venda) - 0.0006,
        "cotacaoVenda": float(venda),
        "dataHoraCotacao": f"{day} {hour}",
    }


def test_fetch_parses_ptax_payload(monkeypatch):
    """A single business day in the window: parsed straight through."""
    payload = {"value": [_row("2026-08-19", "5.1714")]}

    def fake_get(url, params=None, timeout=None):
        assert "CotacaoDolarPeriodo" in url
        assert params["$format"] == "json"
        return _FakeResponse(200, payload)

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    rate = fetch_latest_closing_rate(date(2026, 8, 19))
    assert rate.rate_date == date(2026, 8, 19)
    assert rate.venda == Decimal("5.1714")


def test_weekend_fallback_picks_last_business_day(monkeypatch):
    """Reference date is a Monday (2026-08-17 is a Monday); PTAX's window has
    no Sat/Sun rows, and Friday's row is not last in the list (defends the
    max()-by-date choice over trusting API ordering)."""
    payload = {
        "value": [
            _row("2026-08-13", "5.1859"),
            _row("2026-08-14", "5.2236"),  # Friday: the answer
        ]
    }

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, payload)

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    rate = fetch_latest_closing_rate(date(2026, 8, 17))
    assert rate.rate_date == date(2026, 8, 14)
    assert rate.venda == Decimal("5.2236")


def test_fetch_raises_on_empty_window(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, {"value": []})

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="no published rate"):
        fetch_latest_closing_rate(date(2026, 8, 19))


def test_fetch_raises_on_malformed_row(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, {"value": [{"dataHoraCotacao": "2026-08-19 13:10:22"}]})

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="missing/malformed"):
        fetch_latest_closing_rate(date(2026, 8, 19))


def test_fetch_raises_on_http_error(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(503, text="service unavailable")

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="HTTP 503"):
        fetch_latest_closing_rate(date(2026, 8, 19))


def test_fetch_raises_when_response_has_no_value_list(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, {"unexpected": "shape"})

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="no usable 'value' list"):
        fetch_latest_closing_rate(date(2026, 8, 19))


# ---------- fetch_closing_rates_range: the --backfill mode's fetch ----------

def test_range_fetch_returns_one_rate_per_published_row(monkeypatch):
    """No weekend rows in the payload (BCB never publishes them) — the range
    function returns exactly what BCB gave back, one row per business day,
    not just the latest."""
    payload = {
        "value": [
            _row("2026-08-17", "5.1700"),  # Monday
            _row("2026-08-18", "5.1750"),  # Tuesday
            _row("2026-08-19", "5.1714"),  # Wednesday
        ]
    }

    def fake_get(url, params=None, timeout=None):
        assert "CotacaoDolarPeriodo" in url
        return _FakeResponse(200, payload)

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    rates = fetch_closing_rates_range(date(2026, 8, 17), date(2026, 8, 19))
    assert [r.rate_date for r in rates] == [
        date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19),
    ]
    assert [r.venda for r in rates] == [
        Decimal("5.1700"), Decimal("5.1750"), Decimal("5.1714"),
    ]


def test_range_fetch_raises_on_empty_window(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, {"value": []})

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="no published rate"):
        fetch_closing_rates_range(date(2026, 8, 17), date(2026, 8, 19))


def test_range_fetch_raises_on_malformed_row(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(200, {"value": [{"dataHoraCotacao": "2026-08-19 13:10:22"}]})

    monkeypatch.setattr(ptax_client.httpx, "get", fake_get)

    with pytest.raises(PtaxError, match="missing/malformed"):
        fetch_closing_rates_range(date(2026, 8, 17), date(2026, 8, 19))
