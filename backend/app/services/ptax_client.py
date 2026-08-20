"""Client for the Banco Central do Brasil PTAX closing-rate API (Olinda open
data portal, no API key). Fetches the daily USD/BRL commercial "venda" (sell)
rate that services/exchange_rates.py stores as `commercial`.

PTAX has no weekend/holiday rows (the underlying market does not settle on
those days), so a plain "give me today" call breaks on a Monday after a
Friday holiday, the day after Carnaval, etc. Rather than maintain a
Brazilian holiday calendar, we ask BCB for a short *window* ending on the
reference date (CotacaoDolarPeriodo) and take the latest row in it —
whatever business day BCB most recently published becomes the answer.

Style matches services/pluggy_client.py: bare httpx (no SDK — one endpoint),
a module-level error class, and a fixed timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

BASE_URL = "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata"
_TIMEOUT = 30.0
# The longest ordinary gap is a Friday holiday plus the weekend (3 days);
# doubled for safety around multi-day stretches like Carnaval or year-end.
_LOOKBACK_DAYS = 10


class PtaxError(RuntimeError):
    """PTAX API-level failure with enough context to log usefully."""


@dataclass
class PtaxRate:
    rate_date: date
    venda: Decimal  # commercial sell rate, BRL per USD


def _format(d: date) -> str:
    return d.strftime("%m-%d-%Y")


def _get_period(start: date, end: date) -> list[dict[str, Any]]:
    """GET CotacaoDolarPeriodo(dataInicial=..., dataFinalCotacao=...) — the
    date range itself is an OData function parameter embedded in the path,
    not a query param; $format/$select are the query string."""
    url = (
        f"{BASE_URL}/CotacaoDolarPeriodo(dataInicial='{_format(start)}',"
        f"dataFinalCotacao='{_format(end)}')"
    )
    params = {"$format": "json", "$select": "cotacaoCompra,cotacaoVenda,dataHoraCotacao"}
    resp = httpx.get(url, params=params, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise PtaxError(f"PTAX GET failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()["value"]
    except (KeyError, ValueError) as exc:
        raise PtaxError(f"PTAX response had no usable 'value' list: {resp.text[:200]}") from exc


def fetch_latest_closing_rate(reference_date: date | None = None) -> PtaxRate:
    """Most recent published PTAX closing (venda/sell) rate at or before
    reference_date (default: today). Raises PtaxError if the lookback window
    has no published rate at all (BCB outage, or a reference_date far enough
    in the past that _LOOKBACK_DAYS undershoots it)."""
    end = reference_date or date.today()
    start = end - timedelta(days=_LOOKBACK_DAYS)
    rows = _get_period(start, end)
    if not rows:
        raise PtaxError(
            f"PTAX has no published rate between {start} and {end} "
            f"(BCB outage, or the lookback window needs widening)"
        )
    # Ascending by date in practice, but sort defensively rather than trust
    # the API's ordering — we only ever want the latest one.
    latest = max(rows, key=lambda row: str(row.get("dataHoraCotacao", "")))
    return _row_to_rate(latest)


def _row_to_rate(row: dict[str, Any]) -> PtaxRate:
    try:
        venda = Decimal(str(row["cotacaoVenda"]))
        rate_date = date.fromisoformat(str(row["dataHoraCotacao"])[:10])
    except (KeyError, ValueError, TypeError) as exc:
        raise PtaxError(f"PTAX row missing/malformed fields: {row}") from exc
    return PtaxRate(rate_date=rate_date, venda=venda)


def fetch_closing_rates_range(start: date, end: date) -> list[PtaxRate]:
    """Every PTAX closing rate BCB published between start and end
    (inclusive), one entry per business day actually published — weekends
    and BR holidays simply have no row, same as fetch_latest_closing_rate's
    window. Used by the refresh script's --backfill mode to fill several
    missing days in one HTTP call instead of one request per day.

    Raises PtaxError if the window has nothing published at all (BCB outage,
    or a range with no business day in it)."""
    rows = _get_period(start, end)
    if not rows:
        raise PtaxError(f"PTAX has no published rate between {start} and {end}")
    return [_row_to_rate(row) for row in rows]
