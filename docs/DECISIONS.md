# Engineering decisions

This app is on its fourth version. The interesting parts of the story are not
the features, they are the three times the architecture was wrong and what it
cost to find out.

Each entry below is written the same way: the context, the options that were
actually on the table, the choice, and what happened afterwards.

The short version:

| Version | What it was | Fate |
|---|---|---|
| **v1** | Spreadsheet as the database, Python scripts around it | Retired, its data migrated once into v2 |
| **v2** | Rebuild on Postgres with an explicit schema | Became the app |
| **v3** | Clean-slate rewrite around bank auto-pull, parsers deleted | **Killed one week after going live** |
| **v2.5** | v2 with v3's bank-sync layer grafted in | Current |

---

## 1. v1: a spreadsheet with scripts around it

**Context.** The first version was an Excel workbook plus a handful of Python
scripts: an importer that read credit-card statement exports, a keyword-to-
category map, an interactive prompt for updating account balances, and an HTML
report generator. It worked. It produced a real monthly report every month for
several months, and the category map and the statement parsers it grew were
genuinely good.

**What broke.** Four things, all structural rather than fixable:

1. **Currency was inferred from strings.** With no currency column, the only way
   to know whether a row was in USD or BRL was to match substrings in the
   merchant name. That is wrong the first time a foreign merchant is paid with a
   domestic card, and it is silently wrong, which is worse.
2. **History was not stable.** Annual reports recalculated every past month at
   the current exchange rate. Opening the same report two weeks later gave
   different numbers for a month that had closed.
3. **Everything downstream was typed in by hand.** Card balances, savings
   balances and the loan's remaining principal were prompts, once a month.
4. **No audit trail and no querying.** "How much went to dining last quarter"
   meant a pivot table, and deduplication was a best-effort scan rather than a
   constraint.

**The decision it forced.** Not "improve v1". The three real problems were all
consequences of the storage layer having no schema, so the fix had to be a
schema.

---

## 2. v2: rebuild on an explicit schema

**Context.** v1's parsers and category map were worth keeping. Its data model
was not.

**Options.**

- **A. Patch v1.** Add a currency column to the sheet, freeze exchange rates per
  month by convention. Cheapest, keeps every other problem.
- **B. Move to an off-the-shelf budgeting app.** Fastest, but none of them model
  a two-currency household with gross-versus-net income and per-card statement
  cycles, and the data would live somewhere else.
- **C. Rebuild on Postgres, port the parsers.**

**Choice: C**, with an explicit list of what to port and what to redo. Ported:
the statement parsers, the effective-exchange-rate formula, the category map as
seed rows, the report layout. Redone from scratch: currency as an explicit
column constrained to match the payment method, debt-payment detection keyed on
the payment method id instead of description matching, and deduplication as a
Postgres unique constraint rather than application logic.

**What happened.** v2 reached feature parity and then went well past it: a
checking-statement importer that drives card balances, savings snapshots and
withholding reconciliation as side effects of one commit, monthly and annual
reports, recurrence kinds for fixed expenses (indefinite, contract with an end
date, installment series), and a warnings page. The single most valuable
property turned out to be the one that sounds least exciting: **reports are
recomputed live on every read** from transactions, exchange rates and snapshots.
There is no "close the month" button, because a closed month is a cache, and a
cache of a number you can derive is a bug waiting to happen.

---

## 3. v2's frontend: server-rendered, no build step

**Context.** The app needed roughly fifteen pages with modest interactivity:
previews with per-row controls, a few charts, some inline editing.

**Options.** A single-page app in React or Vue, or server-rendered templates
with small islands of client-side behavior.

**Choice:** Jinja2 templates, HTMX for partial updates, Alpine.js for per-page
state, Tailwind and Chart.js as single files loaded from a CDN. **No bundler and
no `node_modules`.**

**What happened.** The constraint has held for the whole life of the project,
including a full second UI for phones. The reason to keep it is not purity, it
is that this app has to still start in five years on a machine nobody has
maintained, and a frozen build toolchain is the part most likely to make that
false. The cost is real but small: some templates are large, and there is no
component reuse across them beyond Jinja includes.

One correction came later, from the same reasoning taken one step further. Those
four libraries were still being fetched from public CDNs at page load, which
meant the whole UI rendered unstyled and dead whenever the house was offline, so
they were downloaded once, committed under `backend/app/static/vendor/` with
their upstream licence files, and served by the app. A test now fails if any
template points a script or stylesheet tag back at a CDN. "No build step" and
"no runtime dependency on someone else's uptime" are not the same property, and
only the first one was actually being enforced.

---

## 4. v3: the clean-slate rewrite around bank auto-pull

**Context.** After several months of running v2, the actual cost of the app was
clear, and it was not the code. It was the monthly ritual: download statements
from a dozen institutions, name the files so the detector picks the right
parser, upload, review, commit. If a bank API could deliver the same
transactions automatically, most of v2's machinery would become dead weight.

**Options.**

- **A. Add bank sync to v2** as one more import source.
- **B. Start a clean repo designed around auto-pull**, and drop everything that
  only existed to support manual importing.

**Choice: B.** The reasoning at the time was explicit and, on paper, sound: if
transactions arrive from an API with a stable provider id, then the statement
parsers, the preview machinery, the import logs, the manual snapshot tables, the
rollover system and the per-row owner attribution are all solving a problem that
no longer exists. v3 was scaffolded in a new repo with the same stack but a
much smaller schema, a cursor-based sync, encrypted access tokens, and a single
user row that existed only to satisfy the provider's API. v2 was put into code
freeze and kept running as the daily app until cutover.

**What happened, at first.** v3 went from empty repo to production in about a
week. Several institutions were linked, transactions flowed, balances refreshed,
a dashboard and a rules editor were built, and a second provider was integrated
for the Brazilian side and validated end to end in sandbox. It looked like the
right call.

---

## 5. Why v3 was killed, and its best part grafted onto v2

**This is the decision the rest of the project turned on.**

**Context.** One week after v3 went live, two facts landed in the same session.

*Fact one: auto-pull does not cover everything, and never will.* A group of
store cards sharing one issuer's backend simply would not connect. Investigation
established that this was not a transient bug: those institutions ride the
aggregator's legacy credential-scraping path rather than a real API, so the
connection breaks whenever the institution changes its login page and stays
broken until the aggregator rebuilds it. There were documented break-and-rebuild
cycles going back years. Switching aggregators does not help, because the
alternatives ride the same scraping path for the same institutions; one
competitor had zero coverage of them at all. The regulation that would have
forced a stable API had been enjoined in court with no binding deadline. And one
more card issuer was simply not supported by anyone.

So the load-bearing assumption of v3, that auto-pull replaces parsers, was
**false**. Statement parsers were going to be needed permanently, for a minority
of accounts, forever.

*Fact two: what deleting them had cost.* Once parsers had to come back, the
question became what else v3 was missing. The list was long: income tracking
across multiple sources, tax tracking, recurrence kinds, contract end dates,
installment series, fixed-expense rollover, statement-balance versus current
-balance tracking, per-row owner attribution, import logs, exchange-rate
ingestion and cross-currency totals (the schema table existed but was unused, so
foreign accounts did not sum into net worth at all), and the entire historical
ledger, which v3 had deliberately not migrated. There were also zero tests.

**Options.**

- **A. Finish v3.** Port the parsers back in minimal scope, then port income,
  taxes, recurrence, rollover, reports parity, and backfill history from v2.
  Faithful to the plan.
- **B. Keep both.** v2 for reports and manual accounts, v3 for auto-pull.
  Two ledgers, two schemas, reconciliation forever. Rejected immediately.
- **C. Kill v3 and graft its bank-sync layer onto v2.** Copy the working
  parts (the API client, the token encryption, the sync service, the balance
  refresh, the connections UI) into a copy of the production-ready v2, and
  keep everything v2 already had.

**Choice: C.** The honest way to state the trade is this: v3's contribution was
about 1,500 lines of provider integration. v2's contribution was a year of
domain modelling that had been validated against real money. Option A meant
rewriting the second to preserve the first. Option C meant moving the first into
the second, which is the same amount of integration work and none of the
rewriting. The sunk cost of v3 was a week; the sunk cost of throwing away v2's
ledger semantics would have been the whole project.

**What happened.** The graft (called v2.5, a copy of v2 so the running v2 stayed
untouched) was built and verified end to end **in a single day**: a migration
adding the provider tables, the account-mapping columns and the provider
transaction id with its own partial unique index; the client, encryption, sync
and balance services carried over; the connections UI; and a hard guard that
returns HTTP 409 if anyone tries a manual import for an account that a provider
feeds. The first full pull across four institutions landed the expected
transactions with the non-spending rows correctly skipped, and re-running it
added nothing, which is the only test of a dedup strategy that counts.

v3 was archived the same week. The lesson recorded at the time, and the reason
this section exists: **a rewrite's premise deserves the same skepticism as its
implementation.** "Auto-pull covers everything" was checkable in an afternoon,
before a line of v3 was written, by trying to link the awkward institutions
first instead of the easy ones. Building the risky integration first is not the
same as building the easy one first and hoping.

---

## 6. Bank sync is a source, not a second ingest engine

**Context.** The first version of the graft did what v3 did: pull transactions
on boot and write them straight to the ledger. Within days that was recognized
as a violation of v2's central rule that nothing enters the ledger without a
preview.

**Options.** Keep auto-ingest with a "review afterwards" screen, or route the
provider through the existing preview.

**Choice:** route it through. Provider transactions are adapted into exactly the
same preview objects the file parsers produce, so the bank sync reuses the
categorizer, the duplicate marking, the per-row overrides, the split-into-N
control and the commit path. Boot-time auto-pull of transactions was deleted;
only balances refresh automatically, because a balance is a read of a fact, not
a write to the ledger.

**What happened.** The connections page grew a per-account **Review then
Commit** flow, and it became the most-used screen in the app. A later
refinement, from real use: unchecking a row in the review now means "dismiss
permanently" rather than "not this time". The original semantics meant an
unchecked row resurfaced on every future review forever, because the review
window is anchored at a fixed start date.

Details are in [PLAID.md](PLAID.md).

---

## 7. Refuse to boot on schema drift

**Context.** The app was containerized and moved onto a small always-on server.
It answered on its port from the first night and was **unusable for a week
without anyone noticing.** The container image was built from the latest code
while the database stayed two migrations behind, so queries referenced a renamed
column and a table that did not exist yet.

The reason nobody noticed is the interesting part: **every page returned HTTP
200.** The page shell renders server-side, and the data call underneath it
fails. A monitoring check that asserts a 200 would have stayed green the entire
time. It looked like data loss; nothing had been lost, all tables and rows were
intact throughout.

**Options.**

- **A. Migrate automatically on boot.** Removes the failure mode entirely, and
  means an unreviewed migration can run against the ledger during any 3 a.m.
  restart.
- **B. Log a warning and serve anyway.** Nobody reads logs of a healthy-looking
  app. This is what already happened, implicitly.
- **C. Refuse to start**, and make migration an explicit, separate command.

**Choice: C.** A schema guard runs first in the application lifespan, compares
the database's Alembic revision against the code's head, and exits with both
revisions and the exact fix command in the log if they differ. There is one
emergency environment-variable hatch for the case where you knowingly need to
serve a drifted database. The deploy wrapper was built at the same time and
**aborts before restarting** when migrations are pending, so production stays on
the old, working image rather than being upgraded into a broken state.

**What happened.** The failure mode is now a crash loop with a fix command in
it, which is the correct trade: an app that will not start gets fixed in
minutes, an app that returns 200 and no data gets fixed in a week. The one
consequence worth documenting is that you cannot `exec` into a container that
refuses to start, so the repair path has to be a one-off `run` container.

Two smaller rules came out of the same incident and stuck: **run a linter pass
for undefined names before every deploy** (a missing import raises only when its
line executes, so it survives every import-time check and can ship), and **200
is not verification** (the deploy step sweeps every GET route and then a human
loads pages and checks the console).

---

## 8. Hand-rolled auth, and the lockout that followed

**Context.** The app had been behind a reverse-proxy basic-auth prompt. Once it
was reachable from phones, it needed a real login with per-user identity, so the
UI could greet the right person and attribute rows correctly.

**Options.** The standard FastAPI auth library, or roughly 150 lines of JWT
handling split between a service and a router.

**Choice: hand-rolled.** Not for fun: the obvious library is async-SQLAlchemy
only, and this app is synchronous end to end. Adopting it meant either an async
migration of the data layer or maintaining two session styles, for the same JWT
outcome. A signed token in an httpOnly cookie, middleware over every route
(browsers get redirected to the login page, API callers get a 401 JSON), and a
small exempt list: the login and logout routes themselves, the container health
check, static files, the favicon and the web manifest. The manifest exemption is
load-bearing and easy to miss: Add to Home Screen must work before you log in.

There is no registration and no password-reset flow, on purpose. This is a
household app with a fixed, known set of users; passwords are set by a script.

**What happened.** The first person to try it was locked out, and the root cause
was one attribute. The login field was `<input type="email">`, so the browser
silently refused to submit anything that was not an email address, and the app
was being tested with usernames. Nothing in the logs, no error, just a form that
would not submit. The fix was to accept **either** the email or the username,
case-insensitively, and to stop constraining the field by type. Once identity
was real, a second bug surfaced immediately: the home greeting was hardcoded to
one name, so the second user was greeted as the first. Both are the same class
of mistake, an assumption that there is only ever one user, surviving into code
that has two.

The reverse-proxy basic auth was removed the same day rather than kept as a
second layer, with a note in the proxy config: if the app's own auth ever goes
away, basic auth goes back first.

---

## 9. The household is configuration, not code

**Context.** After a year, personal facts had accumulated in the source: enum
values named after specific people, salary detection keyed on a name string, a
pay-raise schedule as a Python constant, statement classification keyword tables
inline in the parsers, and a mapping from income sources to specific account
names. The app could only ever be used by one household, and none of it could be
shown to anyone.

**Options.**

- **A. Scrub on export.** Keep the app as it is; a script copies whitelisted
  paths into a public tree and rewrites the names. Fast to start, but it is a
  translation layer maintained forever, and every future change can leak
  through it.
- **B. Generalize the app itself**, then exporting is a straight copy.

**Choice: B**, accepting that it required a migration against a live database
because enum values had to be renamed in place.

The refactor moved four things into tables: household members with roles
(primary and partner) and the key their statements are matched by; pay levels
keyed by the month a level takes effect; which withholding merchants belong to
which member; and the statement classification keyword rules, which had been
hardcoded, including one boilerplate prefix that was literally a person's name
in a string comparison. The parsers now receive the rule set as an argument and
never touch the database.

**What happened.** The app got better in a way that had nothing to do with
privacy. Pay levels as rows fixed a real bug class: when the constant was stale,
the importer read a raise as a tax cut, because it kept the old gross, saw a
larger net deposit, and shrank the withholdings proportionally to make the
arithmetic work. It did this silently, and it had happened in production. With
levels as dated rows, a raise is an insert, past months keep reconciling against
the gross that applied at the time, and editing a past level is explicitly the
wrong move.

It is also what makes the demo in the README possible: seeding a fictional
household is now just inserting rows, not forking the code.

---

## 10. A separate phone UI, not responsive CSS

**Context.** The app is genuinely used from a phone, mostly to check a balance
or approve a bank review while away from a desk. The desktop layout at 412px
wide was survivable and unpleasant: dense tables, hover-dependent affordances,
modals taller than the viewport.

**Options.**

- **A. Responsive CSS.** One set of templates, breakpoints, careful reflow.
  No duplication.
- **B. A separate set of phone templates** sharing the same routes, context and
  API.
- **C. A native or hybrid app.** Rejected without much discussion: a second
  toolchain and an app store for an app used by two people.

**Choice: B.** The judgment was that the two UIs are not the same screen at
different widths, they are different interactions. The desktop bank review is a
table with inline controls; the phone version is a full-screen flow with one
card per transaction and a per-row edit disclosure. The desktop rollover is a
modal with editable amounts; the phone version is a bottom sheet where amounts
roll as suggested and editing is deliberately left to the desktop. Responsive
CSS can make a table narrow, but it cannot make it a different interaction.

The mechanics that keep the duplication honest:

- Routes, context and API are shared. One render helper swaps the template.
- Every phone rule is scoped under a single class, so the two stylesheets can
  never affect each other.
- Detection is the User-Agent's `Mobi` token, overridable in both directions by
  a cookie, so either UI can be reached from either device.
- Pages that have no phone template fall back to the desktop one, kept usable by
  a narrow-screen safety net.
- **Every new feature ships on both UIs in the same change.** This is the rule
  that decides whether option B ages well or rots.

**What happened.** Twelve screens have a phone template today. Four of them were
hand-built first to establish the pattern, and the remaining eight were
converted in parallel against those examples, then reviewed page by page against
the desktop originals for behavioral parity. No API contract drift was found,
which is the direct payoff of sharing routes and context rather than building a
second backend. Six real defects were found and fixed in review, all of them
touch-specific: a modal that evaluated its bindings while idle, self-tinted
cards that were unreadable in dark mode on two of the pages, a missing in-flight
guard on a delete, errors rendered inside a closed sheet, and a value that lived
in a desktop tooltip and was therefore invisible on touch.

Three pages are deliberately desktop-only: file imports, the rules editor and
the guide. Not everything should be done on a phone, and pretending otherwise
costs more than admitting it.

---

## 11. One migration for the public repo, real history for the private one

**Context.** This repository is the public portfolio version of an app that
handles real money for a real household. The private repository keeps its full
migration history, thirty-plus incremental migrations, many of which name real
accounts in their data-backfill steps.

**Options.** Carry the history and rewrite the sensitive parts, or replace it
with a single baseline.

**Choice:** a single `0001_initial` that creates the entire current schema, with
no data seeds inside it. Nobody reads thirty incremental migrations in a
portfolio repository, and every one of them is leak surface.

**What happened.** Regenerating a baseline from the models turned out to be
subtler than autogenerating it. A schema is not only what the models declare, it
is also everything the migration chain has done to the database over time: enum
label ordering (a value renamed in place keeps its original position, which the
model's declaration order does not reflect), server defaults set by a migration
rather than the model, physical column order (a column added later sits at the
end of the real table), non-default constraint and index names, and the partial
unique indexes that autogenerate would flatten into one plain unique constraint.

So the acceptance bar is mechanical rather than visual: build two scratch
databases, apply the real chain to one and the baseline to the other, dump both
schemas and diff them after normalizing the noise. It prints identical, or it
fails.
