"""Push finance deadlines to the family-calendar app as reminders.

Reminders only: this script never writes to the finances database. It reads the
three feeds that carry a date (expiring contracts / final installments, credit
spend-goal deadlines), turns each into a reminder with a
deterministic `external_id`, and POSTs it. See `app/services/calendar_push.py`
for the id scheme and why it is the whole safety story.

Safe to run as often as you like. The whole push set is recomputed every run,
so the same reminder is POSTed daily until its date passes; the calendar
answers `created: false` after the first time and this reports those as skips.

Invoked daily at 07:00 America/New_York by `finances-calendar-push.timer` on
lab, half an hour after the balance refresh so a due-soon reminder reflects the
morning's balances. Run by hand the same way the timer does:

    ssh lab 'cd /srv/lab/finances/repo/docker && \
        docker compose run --rm -T app python scripts/push_calendar_reminders.py'

Exit status, so the timer's LAST_RESULT can tell silence from success:
  0  every reminder in the set was created or already present (an empty set
     counts: no upcoming deadlines is a normal, healthy day)
  1  the calendar is not configured, or at least one POST failed
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.services.calendar_push import is_configured, push_all


def main() -> int:
    if not is_configured():
        print(
            "calendar push not configured; set CAL_API_URL and CAL_API_KEY",
            file=sys.stderr,
        )
        return 1

    with SessionLocal() as session:
        summary = push_all(session)

    print(
        f"calendar: reminders={summary.reminders} created={summary.created} "
        f"skipped={summary.skipped} failed={summary.failed}"
    )
    for outcome in summary.outcomes:
        suffix = f" — {outcome.detail}" if outcome.detail else ""
        print(f"  {outcome.status:8} {outcome.external_id}{suffix}")

    if summary.failed:
        print(f"{summary.failed} reminder(s) failed to push", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
