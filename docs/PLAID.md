# Bank sync design

The app can pull transactions and balances from two aggregators: **Plaid** for
US institutions and **Pluggy** for Brazilian ones. Both are optional. With no
credentials configured the Connections page tells you which environment
variables are missing, and every other page behaves exactly as it does with
them, because manual import is a complete, first-class path rather than a
fallback.

This document is the design, the traps that were found the hard way, and how to
run it with your own sandbox keys.

---

## The one rule: preview then commit

**A bank row never enters the ledger without a human seeing it first.**

The first version of this integration did what most bank-sync integrations do:
pull on startup and write straight to the transactions table. That was removed.
The provider is a **source**, not a second ingest engine. Provider transactions
are adapted into exactly the same objects the file parsers produce
(`ParseResult` for cards, `CheckingParseResult` for checking accounts), so the
bank sync goes through the same categorizer, the same duplicate marking, the
same per-row overrides and the same commit function as a PDF statement.

The consequences are worth stating explicitly, because they are the whole
design:

| Operation | Automatic? | Why |
|---|---|---|
| Balance refresh | **Yes**, at application boot, and on demand from the Connections page | A balance is a read of a fact. It writes a snapshot row, not a ledger row. |
| Transaction ingest | **No**, ever | A ledger row is a claim about what a charge *was*: which merchant, which category, whose it is, whether it is one purchase or the first of twelve installments. A provider cannot know that. |

What the reviewer gets per row: skip, change merchant, change category, change
owner, split into N installments, or reroute a misclassified row (for example,
marking a checking withdrawal as a card payment rather than spending).

### Uncheck means dismiss

An early version treated an unchecked row as "not this time". Because the review
window is anchored at a fixed start date rather than a moving cursor, that row
resurfaced on **every** future review, forever. Commit now records every
reviewed provider id in a `*_seen_transactions` table whether it was checked or
not, so unchecking is a permanent dismissal. Recovering from an accidental
dismissal means deleting the id from that table by hand, which is the correct
level of friction.

The seen-transactions tables exist separately from the ledger for exactly this
reason: some provider rows produce no ledger row at all (internal transfers,
interest, card payments) and still must never be shown again.

---

## Scope and what is stored

**Read-only, minimum product scope.** Transactions and balances only. No
authentication product, no identity, no transfer or payment initiation. If the
credentials leak, the blast radius is read access to transaction history and
balances, not the ability to move money.

**Plaid access tokens are encrypted at rest with Fernet.** The key lives in the
`FERNET_KEY` environment variable, never in the database and never in the
repository, so a database dump is not enough to talk to anyone's bank. Two
practical notes learned in production:

- The key is part of "is Plaid configured", not an optional extra. Without it,
  every stored token is undecryptable and calls fail at token load rather than
  at the API. A health check that ignored it once reported a healthy
  integration that could not make a single call.
- For an existing install, `FERNET_KEY` must be **the** key already in use.
  A different key does not degrade gracefully; it makes every stored token
  permanently unreadable, and the only recovery is re-linking every institution,
  which mints new items, orphans the existing rows and resets the sync anchor.
  If decryption fails, the key is wrong. Restore it, do not re-link.

**Pluggy stores no per-connection secret at all**, so no encryption is involved
on that side. Its authentication is app-level: client id and secret are
exchanged for a short-lived API key that is cached in memory and refreshed on a
401. Connections carry only their item id, which is not a credential.

---

## Deduplication: two layers, on purpose

Deduplication has to survive three different things: re-running the same pull,
importing a PDF that overlaps a pull, and re-importing the same PDF twice.

**Layer 1: provider transaction id, within a source.** Every provider-fed row
stores its provider transaction id (`plaid_transaction_id` or
`pluggy_transaction_id`), each under its **own partial unique index**. This
catches re-pulls exactly, and it catches them even when the row's stored amount
no longer matches the bank line, which happens whenever the reviewer split one
charge into installments. The id is checked in both the preview and the commit,
so a concurrent second reviewer cannot slip a duplicate past.

**Layer 2: signature dedup, across sources.** Provider ids never match across
sources: the same real purchase gets one id from Plaid and a different one from
a PDF import, and a different one again from Pluggy. So a second guard is needed
and it is the one v2 already had, a unique constraint on
`(date, merchant, amount, payment method, owner)`, applied as a partial unique
index over the rows that have no provider id.

Owner is part of the signature deliberately, so that two genuinely separate
same-day, same-amount charges on the same card can coexist when they belong to
different people. The preview's duplicate matching is looser than the database
constraint (it ignores the merchant), which catches re-imports even when the
categorization rules changed in between and therefore renamed the merchant. The
trade-off is accepted and documented: two legitimate same-day, same-amount
purchases on the same card by the same person collide in the preview and must be
un-skipped by hand.

**A hard guard closes the loop:** any payment method mapped to a provider
account rejects manual import with HTTP 409, across all import endpoints. Once
an account is fed by a provider, there is exactly one way in.

---

## Balances versus transactions

The application refreshes balances in two places: once inside its startup
lifespan, and whenever someone presses **Refresh balances** on the Connections
page (`POST /api/plaid/refresh-balances`, `POST /api/pluggy/refresh-balances`,
and the per-item variants of both). Both paths write snapshot rows (a card
balance row for credit cards, a savings snapshot for deposit accounts), one per
account per day, and never synthetic transactions: a second refresh on the same
day replaces that day's row rather than appending to it.

Two operational facts follow from this:

- **Restarting the app calls the provider.** It is safe, since transactions are
  untouched, but it is not free: one restart writes a snapshot row per mapped
  account against real institutions. Never restart casually to see whether that
  fixes something.
- **Card balances are derived, not stored.** The balance table is a sparse
  snapshot, so the current balance is the last recorded snapshot plus the
  transactions posted since it, excluding future-dated projections. The pages
  that show debt never expose the raw stale row.

There is **no scheduler in this repository**: nothing refreshes on a timer by
itself. On an always-on install that matters, because savings figures then move
only when someone restarts the app or presses the button, which makes
month-over-month deltas a function of the deployment schedule rather than of
money. If you want a periodic refresh, add it outside the app by having your own
scheduler POST the refresh endpoint, which needs a session cookie like every
other route behind the auth middleware.

---

## Exactly one instance may hold the credentials

This is a hard operational rule, not a preference.

Two instances sharing the same provider credentials will each pull the same
window, each maintain their own view of what has been seen, and each write
snapshot rows. The result is duplicated ledger rows, a corrupted notion of which
transactions have already been reviewed, and two divergent balance histories for
the same account.

The rule caused real work when this app moved from a workstation to a home
server: the old instance was not just stopped, it was retired. Its environment
file was emptied, its database cluster was stopped and masked so it could not be
started accidentally, and the databases were dumped and archived. Only after
proving the old instance had made no writes since the migration was the new one
allowed to pull.

If you run this yourself: **one instance, one set of credentials.** A staging
copy gets sandbox keys, never a second copy of the production ones.

---

## How Pluggy mirrors the pattern

Pluggy is a second source, not a second engine. The table below is the whole
mapping.

| Plaid | Role | Pluggy equivalent |
|---|---|---|
| `plaid_items` (encrypted access token, cursor, status) | one row per institution connection | `pluggy_items`, simpler: no per-connection secret, so no encryption |
| `payment_methods.plaid_account_id` | non-null means "fed by the provider"; manual import returns 409 | `payment_methods.pluggy_account_id`, identical semantics |
| `plaid_seen_transactions` | provider ids already reviewed, including no-op rows | `pluggy_seen_transactions` |
| `transactions.plaid_transaction_id` | partial unique, dedup within the source | `transactions.pluggy_transaction_id` |
| `services/plaid_client.py` | thin API wrapper | `services/pluggy_client.py` (hand-rolled httpx, no SDK) |
| `services/plaid_import.py` | provider rows into `ParseResult` / `CheckingParseResult` | `services/pluggy_import.py`, the same two adapters |
| `PLAID_START_DATE` | clean-start anchor; earlier rows are dropped on ingest | `PLUGGY_START_DATE` |
| boot and on-demand balance refresh | balances only, never transactions | the same two paths, over Pluggy accounts |

Three Pluggy-specific behaviors are worth explaining, because each one is a bug
if you get it wrong.

### Sign normalization from the type field, not the sign

The sandbox connector **inverts both the sign and the debit/credit type** of
real accounts: a card purchase arrives negative and typed `CREDIT` in the
sandbox, and positive and typed `DEBIT` in a real account. An implementation
that trusts the raw sign passes every sandbox test and then books every real
purchase as income.

The adapter therefore ignores the sign entirely and normalizes from the `type`
field alone: `DEBIT` is money out, `CREDIT` is money in. The mapping shipped
behind a read-only preview and was validated against one real account before any
account was allowed to be mapped, which is the only reason the inversion was
caught before it wrote anything.

### Pending rows are dropped

Pluggy provides no link between a pending row and the posted row that replaces
it. Plaid does (it sends the pending transaction's id on the posted one), which
is what lets the Plaid side ingest a pending row and reconcile it in place when
it posts.

Without that link, a committed pending row cannot be reconciled: when the charge
posts it arrives under a new id, layer-1 dedup does not recognize it, and the
purchase is counted twice. So the Pluggy adapter drops pending rows on sight.
They appear once, as posted rows, on the next review. The cost is a day or two
of latency on very recent charges. The alternative is silent double counting.

### Identity lives on the account id, not the item id

Re-authorizing an institution can produce a **new** item pointing at the same
accounts. If mapping identity were keyed on the item, the same real account
would end up mapped twice and every transaction would be handled twice.

Item registration therefore checks whether any account under the new item is
already mapped to a payment method and surfaces that instead of silently
double-mapping.

A related quirk shapes the UI: the Pluggy API deliberately does not list your
existing connections, so an item id must be captured at creation time. The app
captures it from the Connect widget's success callback, and also accepts a
pasted item id for connections authorized outside the app. There is no webhook
receiver, on purpose: the app runs on a home LAN with no public ingress, and
exposing it just to catch creation events is not worth it when both capture
paths already exist.

---

## Classification

Both adapters classify a provider row before it reaches the preview, using the
same keyword tables the file parsers use (which live in a database table, not in
the source, so they are editable without a deploy).

The provider's own category is consulted **only** to override the generic
"spending" fallback, never to override a keyword match. Provider categories are
useful for the rows the keyword tables have never seen and unreliable for the
rows they have.

Non-spending rows are recognized and skipped rather than ingested. On the Plaid
side those are the loan-payment and transfer categories; a card payment reduces
the card balance instead of becoming a ledger row, and internal transfers and
interest are skipped entirely, since the balance is the authority for both.

One class of provider noise deserves a mention because it is invisible until it
is not: some merchants post small pre-authorizations that are later consolidated
into a single real charge without any pending-to-posted link (transit tap-and-go
is the common case). Those pre-authorizations never reconcile one to one and
would linger as orphan rows double-counting the fare, so they are filtered by a
targeted rule at the adapter. If your institutions do something similar, this is
the layer to extend.

---

## Running it with your own keys

Everything below is optional. Skip it and the app works, minus the Connections
page.

### Plaid (US)

1. Create a free account at `dashboard.plaid.com` and take the **sandbox**
   client id and secret. Sandbox needs no approval and provides fake
   institutions with fake transactions.
2. Generate a Fernet key:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. Set the variables:
   ```dotenv
   PLAID_CLIENT_ID=...
   PLAID_SECRET=...
   PLAID_ENV=sandbox
   PLAID_START_DATE=2026-06-01
   FERNET_KEY=...
   ```
4. Restart, open `/connections`, click connect, and use the sandbox
   credentials Plaid documents for its test institutions.
5. Map each provider account to a payment method, then use **Review** on that
   account. Nothing is written until you press Commit.

`PLAID_ENV=production` means live bank credentials. Keep it on `sandbox` for
anything experimental. Note also that Plaid's old free "development" tier no
longer exists; a limited free trial lives inside the production environment
instead, which is worth knowing before planning around tier names you read in
older articles.

### Pluggy (Brazil)

1. Create an application at `dashboard.pluggy.ai` for the client id and secret.
   The free application includes a sandbox connector.
2. Set the variables:
   ```dotenv
   PLUGGY_CLIENT_ID=...
   PLUGGY_CLIENT_SECRET=...
   PLUGGY_START_DATE=2026-08-01
   ```
3. Restart and use the Pluggy section of `/connections`.

Two things that will cost you an afternoon if nobody tells you. First, the
free path to **real** Brazilian accounts is not the API application directly:
you connect the banks as a consumer at `meu.pluggy.ai` and then authorize your
application to read them, which is a separate handshake per institution and easy
to forget. Second, the paginated transactions endpoint has a versioned successor
and the older one now returns HTTP 410; the newer one uses cursor pagination and
does not accept a server-side date filter, so the review window is filtered
client-side.

### Verifying it works

The test that actually proves the dedup strategy is not the first pull, it is
the second one. Review and commit an account, then immediately review it again.
The correct result is an empty review and zero new rows. If the second pull adds
anything, layer 1 is not doing its job.
