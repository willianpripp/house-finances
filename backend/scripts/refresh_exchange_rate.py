"""Fetch the latest PTAX closing USD/BRL rate and insert it as that day's
commercial rate, unless a row for that rate_date already exists — the
never-overwrite rule in services/exchange_rates.py applies here exactly as
it does to a manual entry: historical reports look up "the latest rate whose
rate_date <= reference day" (see services/reports.py), so an existing row for
that date must never be touched, whoever or whatever created it.

PTAX has no weekend/holiday rows, so "the latest published rate" is not
always today's: ptax_client.fetch_latest_closing_rate() already walks back to
the most recent business day. This script inserts under THAT date (not
today's calendar date) — which is also the correct date for the
lookup-by-<=-reference-day rule above, not just a fallback.

Invoked daily at 18:30 America/New_York by `finances-exchange-rate.timer`
(BCB publishes the PTAX close around 13:00-13:15 BRT; 18:30 America/New_York
leaves a wide safety margin across both timezones' DST — see the timer file).
Run by hand the same way the timer does:

    ssh lab 'cd /srv/lab/finances/repo/docker && \
        docker compose run --rm -T app python scripts/refresh_exchange_rate.py'

Exit status: 0 on success — including the no-op case where a row (manual or
a prior auto run) already covers the date — or 1 on failure (PTAX
unreachable or returned something unparseable), so the timer's LAST_RESULT
and a future Telegram alert can tell silence from a real problem.

--backfill YYYY-MM-DD..YYYY-MM-DD fills any missing business-day rows in
that range from the same PTAX API, one HTTP call for the whole range. Same
never-overwrite rule: an existing row (any source) for a date in the range
is left untouched. Use it after an outage that made the daily run miss a
day, or to seed historical rates the database never had — the gap gets
fixed by asking BCB again, not by typing the numbers in:

    ssh lab 'cd /srv/lab/finances/repo/docker && \
        docker compose run --rm -T app python scripts/refresh_exchange_rate.py \
        --backfill 2026-07-01..2026-07-31'
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.services.exchange_rates import create_rate, get_rate_by_date
from app.services.ptax_client import (
    PtaxError,
    fetch_closing_rates_range,
    fetch_latest_closing_rate,
)


def _refresh_today() -> int:
    try:
        ptax = fetch_latest_closing_rate()
    except PtaxError as exc:
        print(f"PTAX fetch failed: {exc}", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        existing = get_rate_by_date(session, ptax.rate_date)
        if existing is not None:
            print(
                f"exchange rate for {ptax.rate_date} already exists "
                f"(id={existing.id}, source={existing.source}); "
                f"never-overwrite rule applies, nothing to do"
            )
            return 0

        try:
            row = create_rate(
                session, rate_date=ptax.rate_date, commercial=ptax.venda, source="ptax"
            )
        except ValueError as exc:
            # Lost a race (a manual entry or a concurrent run landed the row
            # between the check above and here). Never-overwrite still holds.
            print(f"exchange rate insert skipped: {exc}")
            return 0

    print(
        f"inserted exchange rate for {row.rate_date}: commercial={row.commercial} "
        f"BRL/USD (PTAX venda, source=ptax)"
    )
    return 0


def _parse_range(spec: str) -> tuple[date, date]:
    parts = spec.split("..")
    if len(parts) != 2:
        raise ValueError(f"expected YYYY-MM-DD..YYYY-MM-DD, got {spec!r}")
    start = date.fromisoformat(parts[0])
    end = date.fromisoformat(parts[1])
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    return start, end


def _backfill(start: date, end: date) -> int:
    try:
        rates = fetch_closing_rates_range(start, end)
    except PtaxError as exc:
        print(f"PTAX backfill fetch failed: {exc}", file=sys.stderr)
        return 1

    inserted = 0
    skipped = 0
    with SessionLocal() as session:
        for ptax in rates:
            existing = get_rate_by_date(session, ptax.rate_date)
            if existing is not None:
                skipped += 1
                continue
            try:
                create_rate(
                    session, rate_date=ptax.rate_date, commercial=ptax.venda, source="ptax"
                )
                inserted += 1
            except ValueError:
                # Lost a race between the check above and the insert.
                # Never-overwrite still holds.
                skipped += 1

    print(
        f"backfill {start}..{end}: inserted {inserted}, skipped {skipped} "
        f"day(s) already covered (never-overwrite rule applies)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    if argv and argv[0] == "--backfill":
        if len(argv) < 2:
            print("--backfill requires a YYYY-MM-DD..YYYY-MM-DD range", file=sys.stderr)
            return 1
        try:
            start, end = _parse_range(argv[1])
        except ValueError as exc:
            print(f"invalid --backfill range: {exc}", file=sys.stderr)
            return 1
        return _backfill(start, end)
    return _refresh_today()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
